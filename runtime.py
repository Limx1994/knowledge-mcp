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
import win32process
import win32security
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from config import ConfigError, config_path, load_config
import crashlog
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


class SpawnGate:
    """保证同一时刻只有一个进程负责启动后台。

    并发首连时每个桥接都会看到"管道不存在"，若各自 spawn_background，
    就会拉起 N 个完整后台，其中 N-1 必然死于单例 mutex。用命名互斥量
    选出一个启动者，其余桥接改为等待共享监听出现。
    """
    NAME = 'Global\\LocalKnowledgeMcp.Spawn.v1'

    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        self.handle = self.api.CreateMutexW(None, False, self.NAME)
        error = ctypes.get_last_error()
        if not self.handle:
            raise StoreError(f'无法取得启动互斥量，服务未启动: {ctypes.WinError(error)}')
        # 183=ERROR_ALREADY_EXISTS：别的桥接正在启动后台，本次不负责启动。
        self.owned = error != 183

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


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


def launcher_pid():
    """报告需要与后台一起等待的启动进程 PID。

    onefile 的解压父进程会等后台退出后清理并退出，必须一并等待；onedir 没有这一层，
    后台由桥接进程直接创建并以桌面 Shell 为父进程，os.getppid() 返回的是 explorer，
    等它退出只会超时。因此只有父进程镜像确实是本程序时才报告父 PID，否则报告自身。
    """
    if not getattr(sys, 'frozen', False):
        return os.getpid()
    parent = os.getppid()
    if parent <= 0:
        return os.getpid()
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, parent)
        try:
            image = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
    except pywintypes.error:
        # 父进程已退出或拒绝查询：没有可等待的启动进程。
        return os.getpid()
    return parent if image == sys.executable else os.getpid()


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
        root = self.store.root if self.store is not None else None
        crash = dict(crash_dir=str(crashlog.crash_folder(root)), crashes=crashlog.count(root))
        with self.guard:
            store = self.store
            common = dict(ok=True, pid=os.getpid(),
                          launcher_pid=launcher_pid(),
                          connections=len(self.sessions), stopping=self.stopping.is_set(),
                          web_url=self.web.base_url if self.web else None,
                          tray_window=self.tray.window if self.tray else None,
                          config_path=str(config_path()), config_error=self.config_error,
                          configuring=self.configuring, **crash)
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

    def _listener(self):
        """创建一个监听管道实例（句柄保持 OVERLAPPED，供 PipeChannel 异步读写）。"""
        return win32pipe.CreateNamedPipe(
            PIPE_NAME, win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
            win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 3000, pipe_security())

    def serve(self):
        try:
            handle = self._listener()
            while not self.stopping.is_set():
                self.ready.set()
                try:
                    # 阻塞式连接：客户端已先连上时立即返回。overlapped 形式在该场景下
                    # 会同步完成（535 被 pywin32 吞掉），此后的 GetOverlappedResult
                    # 将永久等待一个从未入队的 I/O，把监听线程彻底卡死。
                    win32pipe.ConnectNamedPipe(handle)
                except pywintypes.error as exc:
                    handle.Close()
                    # 535=调用前客户端已连接；232=客户端连上后立即断开。
                    # 两者都不是监听故障：换一个新实例继续接受连接。
                    if exc.winerror not in (535, 232):
                        raise
                    handle = self._listener()
                    continue
                # 先建立下一个监听实例再分发当前连接，消除零监听窗口；
                # 否则并发首连在分发间隙只能拿到 231/2。
                following = self._listener()
                with self.guard:
                    if self.stopping.is_set():
                        handle.Close()
                        following.Close()
                        break
                    channel = PipeChannel(handle, server=True)
                    worker = threading.Thread(target=self.handle, args=(channel,), daemon=True)
                    self.channels.add(channel)
                    self.workers.add(worker)
                    try:
                        worker.start()
                    except Exception:
                        self.workers.discard(worker)
                        self.channels.discard(channel)
                        channel.close()
                        following.Close()
                        raise
                handle = following
        except Exception as exc:
            LOGGER.exception('命名管道监听失败')
            crashlog.record_current(exc, note='命名管道监听失败，服务停止')
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
        try:
            if self.web:
                self.web.close()
        except Exception as exc:
            # 管理页面关闭失败不能阻断托盘关闭、worker 等待与单例释放。
            LOGGER.warning('关闭管理页面失败: %s', type(exc).__name__)
        with self.guard:
            sessions = list(self.sessions)
            channels = list(self.channels)
            workers = list(self.workers)
        for loop, task in sessions:
            loop.call_soon_threadsafe(task.cancel)
        for channel in channels:
            channel.close()
        if workers:
            # 统一 deadline 并发等待，避免 N 个卡住会话把关闭放大成 5s×N。
            deadline = time.monotonic() + 5
            for worker in workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            for worker in workers:
                if worker.is_alive():
                    LOGGER.error('管道工作线程未在 5 秒内退出')
        if self.store is not None:
            with self.store.guard:
                self.store.close()


def configure_logging(root):
    folder = crashlog.data_folder(root)
    if folder.exists():
        check_link(folder)
    folder.mkdir(parents=True, exist_ok=True)
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
        # 常驻后台没有控制台：访问冲突、栈溢出等致命错误只能靠 faulthandler 留证。
        crashlog.enable_fatal(root)
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
            gate = None
            if exc.winerror == 2:
                # 231=管道实例忙，后台已在运行，绝不能重复启动；只有"管道不存在"
                # 才可能真的没有后台，且仍需先抢到启动权，避免并发各拉一个后台。
                gate = SpawnGate()
                if gate.owned:
                    process = spawn_background(root)
            try:
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
            finally:
                if gate:
                    gate.close()
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
            if exc.winerror == 2:
                # 231=实例忙，后台已在运行；只有"管道不存在"才需要启动，
                # 且先用启动互斥量选出唯一启动者。
                gate = SpawnGate()
                try:
                    if gate.owned:
                        process = spawn_background(root)
                        process.close()
                finally:
                    gate.close()
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
