"""Windows local pipe broker and persistent single-instance service."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import sys
import threading
import time

import anyio
import pywintypes
import win32api
import win32con
import win32file
import win32event
import win32pipe
import win32security
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from config import ConfigError, config_path, load_config
from knowledge_store import KnowledgeStore, StoreError, check_link

PIPE_NAME = r'\\.\pipe\LocalKnowledgeMcp.Service.v1'
MAX_MESSAGE = 32 * 1024 * 1024
LOGGER = logging.getLogger('knowledge.runtime')
KERNEL = ctypes.WinDLL('kernel32', use_last_error=True)
KERNEL.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
KERNEL.CancelIoEx.restype = wintypes.BOOL


def normalized_root(root):
    path = Path(root)
    if not path.is_absolute() or not path.is_dir():
        raise StoreError('--root 必须是已存在的绝对目录')
    # Resolve junctions/symlinks first so redirected profiles remain usable;
    # reparse points inside knowledge/ are still rejected by the store.
    path = path.resolve()
    for part in [path, *path.parents]:
        check_link(part)
    return path


def pipe_security():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, user)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.SECURITY_DESCRIPTOR = descriptor
    security.bInheritHandle = False
    return security


class PipeChannel:
    def __init__(self, handle, server=False):
        self.handle = handle
        self.server = server
        self.buffer = bytearray()
        self.write_guard = threading.Lock()

    def read_line(self):
        while b'\n' not in self.buffer:
            handle = self.handle
            if handle is None:
                return b''
            event = win32event.CreateEvent(None, True, False, None)
            overlap = pywintypes.OVERLAPPED()
            overlap.hEvent = event
            try:
                data = win32file.AllocateReadBuffer(65536)
                win32file.ReadFile(handle, data, overlap)
                count = win32file.GetOverlappedResult(handle, overlap, True)
                data = bytes(data[:count])
            except pywintypes.error as exc:
                if exc.winerror in (6, 109, 232, 233, 995):
                    return b''
                raise
            finally:
                event.Close()
            if not data:
                return b''
            self.buffer.extend(data)
            if len(self.buffer) > MAX_MESSAGE:
                raise StoreError('MCP 消息超过 32 MiB')
        end = self.buffer.index(10) + 1
        line = bytes(self.buffer[:end])
        del self.buffer[:end]
        return line

    def write(self, data):
        if len(data) > MAX_MESSAGE:
            raise StoreError('MCP 消息超过 32 MiB')
        with self.write_guard:
            handle = self.handle
            if handle is None:
                raise StoreError('管道连接已关闭')
            event = win32event.CreateEvent(None, True, False, None)
            overlap = pywintypes.OVERLAPPED()
            overlap.hEvent = event
            try:
                win32file.WriteFile(handle, data, overlap)
                win32file.GetOverlappedResult(handle, overlap, True)
            finally:
                event.Close()

    def send(self, data):
        self.write((json.dumps(data, ensure_ascii=False) + '\n').encode('utf-8'))
        if self.server:
            win32file.FlushFileBuffers(self.handle)

    def receive(self):
        line = self.read_line()
        if not line:
            raise StoreError('后台管道连接已断开')
        return json.loads(line)

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            if not KERNEL.CancelIoEx(int(handle), None):
                error = ctypes.get_last_error()
                if error not in (6, 1168):
                    LOGGER.warning('取消管道 I/O 失败，错误码 %s', error)
            handle.Close()


def connect_pipe(timeout=1):
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = win32file.CreateFile(PIPE_NAME, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                          0, None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED, None)
            return PipeChannel(handle)
        except pywintypes.error as exc:
            if exc.winerror not in (2, 231) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def control_request(root, command, timeout=3):
    channel = connect_pipe(timeout)
    try:
        payload = dict(command=command)
        payload['root'] = str(normalized_root(root)) if root else ''
        channel.send(payload)
        response = channel.receive()
        if not response.get('ok'):
            raise StoreError(response.get('error', '后台请求失败'))
        return response
    finally:
        channel.close()


class ServiceState:
    def __init__(self, store=None):
        self.store = store
        self.config_error = None
        self.configuring = False
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.guard = threading.Lock()
        self.channels = set()
        self.sessions = set()
        self.workers = set()
        self.web = None
        self.tray = None

    def status(self):
        with self.guard:
            store = self.store
            common = dict(ok=True, pid=os.getpid(),
                          launcher_pid=os.getppid() if getattr(sys, 'frozen', False) else os.getpid(),
                          connections=len(self.sessions), stopping=self.stopping.is_set(),
                          web_url=self.web.base_url if self.web else None,
                          tray_window=self.tray.window if self.tray else None,
                          config_path=str(config_path()), config_error=self.config_error,
                          configuring=self.configuring)
            if store is None:
                return dict(common, configured=False, root=None, documents=0,
                            database_bytes=0, last_sync=None)
            with store.guard:
                return dict(common, configured=True, root=str(store.root),
                            documents=store.conn.execute('SELECT count(*) FROM documents').fetchone()[0],
                            database_bytes=(store.data / 'knowledge.db').stat().st_size,
                            last_sync=getattr(store, 'last_sync', None))

    def request_stop(self):
        self.stopping.set()
        if self.tray:
            self.tray.stop()

    def serve(self):
        try:
            while not self.stopping.is_set():
                handle = win32pipe.CreateNamedPipe(
                    PIPE_NAME, win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                    win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 3000, pipe_security())
                self.ready.set()
                event = win32event.CreateEvent(None, True, False, None)
                overlap = pywintypes.OVERLAPPED()
                overlap.hEvent = event
                try:
                    win32pipe.ConnectNamedPipe(handle, overlap)
                    win32file.GetOverlappedResult(handle, overlap, True)
                except pywintypes.error as exc:
                    if exc.winerror != 535:
                        handle.Close()
                        raise
                finally:
                    event.Close()
                if self.stopping.is_set():
                    handle.Close()
                    break
                channel = PipeChannel(handle, server=True)
                with self.guard:
                    if self.stopping.is_set():
                        channel.close()
                        break
                    worker = threading.Thread(target=self.handle, args=(channel,), daemon=True)
                    self.channels.add(channel)
                    self.workers.add(worker)
                    try:
                        worker.start()
                    except Exception:
                        self.workers.discard(worker)
                        self.channels.discard(channel)
                        channel.close()
                        raise
        except Exception:
            LOGGER.exception('命名管道监听失败')
            self.request_stop()

    def handle(self, channel):
        try:
            header = channel.receive()
            command = header.get('command')
            header_root = header.get('root', '')
            store = self.store
            if command == 'mcp' and header_root and store is not None and (
                    os.path.normcase(header_root) != os.path.normcase(str(store.root))):
                channel.send(dict(ok=False, error='后台已使用其他知识库，不能切换或重复启动'))
                return
            if command == 'status':
                channel.send(self.status())
            elif command == 'shutdown':
                channel.send(dict(ok=True))
                self.request_stop()
            elif command == 'ticket' and self.web:
                channel.send(dict(ok=True, url=self.web.open_url()))
            elif command == 'mcp' and store is None:
                message = '正在配置知识库，请稍候' if self.configuring else '尚未配置知识库，请从托盘打开管理页面完成首次设置'
                channel.send(dict(ok=False, error=message))
            elif command == 'mcp' and not self.stopping.is_set():
                channel.send(dict(ok=True, pid=os.getpid()))
                asyncio.run(self.run_session(channel))
            else:
                channel.send(dict(ok=False, error='未知请求或服务正在退出'))
        except (pywintypes.error, StoreError, ValueError):
            LOGGER.info('本地管道连接结束或请求无效')
        except Exception as exc:
            LOGGER.error('管道会话失败: %s', type(exc).__name__)
        finally:
            try:
                channel.close()
            finally:
                with self.guard:
                    self.channels.discard(channel)
                    self.workers.discard(threading.current_thread())

    async def run_session(self, channel):
        from server import build_server
        server = build_server(self.store)
        adapter = TypeAdapter(JSONRPCMessage)
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        session = (loop, task)
        with self.guard:
            self.sessions.add(session)
        incoming, reader = anyio.create_memory_object_stream(16)
        writer, outgoing = anyio.create_memory_object_stream(16)

        async def receive():
            try:
                async with incoming:
                    while line := await anyio.to_thread.run_sync(channel.read_line, abandon_on_cancel=True):
                        await incoming.send(SessionMessage(adapter.validate_json(line)))
            finally:
                group.cancel_scope.cancel()

        async def send():
            async with outgoing:
                async for message in outgoing:
                    data = (message.message.model_dump_json(by_alias=True, exclude_unset=True) + '\n').encode('utf-8')
                    await anyio.to_thread.run_sync(lambda: channel.write(data), abandon_on_cancel=True)

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(receive)
                group.start_soon(send)
                await server._lowlevel_server.run(reader, writer,
                    server._lowlevel_server.create_initialization_options())
                group.cancel_scope.cancel()
        except asyncio.CancelledError:
            pass  # Service shutdown cancels established protocol sessions.
        finally:
            with self.guard:
                self.sessions.discard(session)

    def close(self):
        self.stopping.set()
        try:
            wake = connect_pipe(0.2)
            wake.close()
        except pywintypes.error as exc:
            if exc.winerror not in (2, 231):
                LOGGER.warning('唤醒监听退出失败，错误码 %s', exc.winerror)
        if self.web:
            self.web.close()
        with self.guard:
            sessions = list(self.sessions)
            channels = list(self.channels)
            workers = list(self.workers)
        for loop, task in sessions:
            loop.call_soon_threadsafe(task.cancel)
        for channel in channels:
            channel.close()
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                LOGGER.error('管道工作线程未在 5 秒内退出')
        if self.store is not None:
            with self.store.guard:
                self.store.close()


def configure_logging(root):
    folder = None
    if root:
        candidate = Path(root) / '.data'
        if candidate.parent.is_dir():
            folder = candidate
    if folder is None:
        folder = config_path().parent
        folder.mkdir(parents=True, exist_ok=True)
    if folder.exists():
        check_link(folder)
    folder.mkdir(exist_ok=True)
    path = folder / 'service.log'
    if path.exists():
        check_link(path)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    handler.addFilter(lambda record: not record.name.startswith('mcp'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # The SDK may include caller-supplied arguments in exception diagnostics.
    logging.getLogger('mcp').setLevel(logging.CRITICAL)


def run_background(root):
    from server import SingleInstance
    from web_admin import WebAdmin
    from tray import TrayIcon
    instance = SingleInstance()
    state = None
    listener = None
    explicit = root is not None
    config_error = None
    try:
        if explicit:
            root = normalized_root(root)
        else:
            try:
                root = load_config().get('root')
            except ConfigError as exc:
                config_error = str(exc)
                root = None
        configure_logging(root)
        state = ServiceState()
        state.config_error = config_error
        if root:
            try:
                state.store = KnowledgeStore(root)
            except Exception as exc:
                if explicit:
                    raise
                state.config_error = f'配置的知识库无法打开: {exc}'
                LOGGER.error('配置的知识库无法打开: %s', type(exc).__name__)
        state.web = WebAdmin(state)
        state.web.start()
        state.tray = TrayIcon(state)
        listener = threading.Thread(target=state.serve, daemon=True)
        listener.start()
        if not state.ready.wait(5):
            raise StoreError('管道监听启动超时')
        state.tray.run()
    finally:
        if state:
            state.close()
        if listener:
            listener.join(timeout=5)
        if state and state.tray:
            state.tray.close()
        instance.close()


def stdio_available():
    handle = win32api.GetStdHandle(-10)
    return bool(handle) and win32file.GetFileType(handle) == win32file.FILE_TYPE_PIPE


def standard_stream(kind):
    number = {'in': -10, 'out': -11, 'err': -12}[kind]
    handle = win32api.GetStdHandle(number)
    if not handle or win32file.GetFileType(handle) != win32file.FILE_TYPE_PIPE:
        raise StoreError('请由 MCP 客户端以 stdio 方式启动')
    process = win32api.GetCurrentProcess()
    copy = win32api.DuplicateHandle(process, handle, process, 0, False, win32con.DUPLICATE_SAME_ACCESS)
    mode = os.O_RDONLY if kind == 'in' else os.O_WRONLY
    fd = msvcrt.open_osfhandle(copy.Detach(), mode | os.O_BINARY)
    return os.fdopen(fd, 'rb' if kind == 'in' else 'wb')


def spawn_background(root=None):
    from win_launch import DetachedProcess
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(str(Path(__file__).with_name('server.py')))
    if root:
        command.extend(['--root', str(root)])
    command.append('--background')
    env = os.environ.copy()
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return DetachedProcess(command, env)


def run_bridge(root=None):
    root = normalized_root(root) if root else None
    source = standard_stream('in')
    target = standard_stream('out')
    channel = None
    process = None
    try:
        first = source.readline(MAX_MESSAGE + 1)
        if not first:
            raise StoreError('未收到 MCP 请求，后台未启动')
        if len(first) > MAX_MESSAGE:
            raise StoreError('MCP 消息超过 32 MiB')
        request = json.loads(first)
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
            raise StoreError('首条输入必须为有效 MCP JSON-RPC 请求')
        try:
            channel = connect_pipe(0.2)
        except pywintypes.error as exc:
            if exc.winerror not in (2, 231):
                raise
            process = spawn_background(root)
            deadline = time.monotonic() + 20
            while channel is None:
                try:
                    channel = connect_pipe(0.2)
                except pywintypes.error as retry:
                    if retry.winerror not in (2, 231):
                        raise
                    # Another bridge can win the singleton race while this
                    # bridge's background exits. Wait for the shared listener.
                    if time.monotonic() >= deadline:
                        log = (root / '.data' / 'service.log') if root else (config_path().parent / 'service.log')
                        raise StoreError(f'后台启动失败或超时，请检查 {log}') from retry
        channel.send(dict(command='mcp', root=str(root) if root else ''))
        header = channel.receive()
        if not header.get('ok'):
            raise StoreError(header.get('error', '后台拒绝连接'))
        channel.write(first)
        input_closed = threading.Event()

        def forward():
            try:
                while data := source.readline(MAX_MESSAGE + 1):
                    channel.write(data)
            except (OSError, pywintypes.error, StoreError):
                LOGGER.info('stdio 输入连接结束')
            finally:
                input_closed.set()
                channel.close()

        threading.Thread(target=forward, daemon=True).start()
        while data := channel.read_line():
            target.write(data)
            target.flush()
        if not input_closed.is_set():
            raise StoreError('后台连接已断开，请重新连接；未自动重放任何请求，写入结果请重新读取核实')
        return 0
    finally:
        if channel:
            channel.close()
        if process:
            process.close()
        target.close()
        # A daemon forwarding thread may still be blocked on this input handle.


def launch_page(root=None):
    import webbrowser
    try:
        try:
            control_request('', 'status', timeout=0.2)
        except pywintypes.error as exc:
            if exc.winerror not in (2, 231):
                raise
            process = spawn_background(root)
            process.close()
            deadline = time.monotonic() + 20
            while True:
                try:
                    control_request('', 'status', timeout=0.2)
                    break
                except pywintypes.error as retry:
                    if retry.winerror not in (2, 231) or time.monotonic() >= deadline:
                        raise StoreError('后台启动失败或超时，请检查配置文件目录中的 service.log') from retry
                    time.sleep(0.1)
        url = control_request('', 'ticket')['url']
        if not webbrowser.open(url):
            raise StoreError('无法打开默认浏览器，请从托盘菜单打开管理页面')
    except Exception as exc:
        report_failure(f'无法打开管理页面: {exc}')
        return 1
    return 0


def report_failure(message, background=False):
    if background:
        LOGGER.error('后台启动失败: %s', message)
        return
    try:
        stream = standard_stream('err')
    except (StoreError, OSError, pywintypes.error):
        ctypes.windll.user32.MessageBoxW(None, message, '知识库 MCP', 0x10)
    else:
        with stream:
            stream.write((message + '\n').encode('utf-8'))
            stream.flush()
