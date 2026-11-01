"""崩溃记录：未处理异常与致命错误的本地证据。

窗口化 EXE 没有控制台，异常直接退出时不留痕迹；这里把未捕获异常、线程异常、
无法抛出的异常和 faulthandler 捕获的致命错误写成独立文本，便于事后定位。
记录不包含文档正文、认证票据或完整请求参数。
"""
import atexit
import datetime
import faulthandler
import logging
import os
from pathlib import Path
import platform
import sys
import threading
import traceback

from config import config_path

LOGGER = logging.getLogger('knowledge.crash')
KEEP = 20
MAX_TEXT = 64 * 1024
MAX_PREVIEW = 8192
ROLE_LABELS = {'startup': '启动', 'background': '常驻后台', 'bridge': 'MCP 桥接',
               'launcher': '管理页面启动器', 'fatal': '致命错误'}
LOCK = threading.Lock()
CONTEXT = {'role': 'startup', 'root': None}
PREVIOUS = {}
HOOKS = {}
FATAL = {'stream': None, 'path': None}


def data_folder(root=None):
    """日志与崩溃记录所在目录；与 runtime.configure_logging 使用同一规则。"""
    if root:
        candidate = Path(root) / '.data'
        if candidate.parent.is_dir():
            return candidate
    return config_path().parent


def crash_folder(root=None):
    return data_folder(root) / 'crashes'


def install(role, root=None):
    """安装异常钩子；重复调用只更新角色与知识库路径，不重复包装。"""
    with LOCK:
        CONTEXT['role'] = role
        CONTEXT['root'] = Path(root) if root else None
    if not PREVIOUS:
        PREVIOUS.update(excepthook=sys.excepthook, thread=threading.excepthook,
                        unraisable=sys.unraisablehook)
    sys.excepthook = _on_uncaught
    threading.excepthook = _on_thread
    sys.unraisablehook = _on_unraisable


def record(role, exc_type, exc_value, tb, note=None, thread=None, root=None):
    """写入一份崩溃记录，返回路径；写入失败返回 None（崩溃处理不能再次抛出）。"""
    try:
        folder = crash_folder(root if root is not None else CONTEXT['root'])
        folder.mkdir(parents=True, exist_ok=True)
        text = format_record(role, exc_type, exc_value, tb, note, thread,
                             root if root is not None else CONTEXT['root'])
        with LOCK:
            path = _unique(folder, role)
            path.write_text(text, encoding='utf-8')
            _prune(folder)
        LOGGER.info('已写入崩溃记录: %s', path)
        return path
    except Exception as exc:  # 崩溃处理不能再抛出，失败只记录
        LOGGER.error('崩溃记录写入失败: %s: %s', type(exc).__name__, exc)
        return None


def record_exception(role, exc, note=None, root=None):
    return record(role, type(exc), exc, exc.__traceback__, note=note, root=root)


def record_current(exc, note=None):
    """按当前进程角色记录；供服务内部判定为致命的异常使用。"""
    return record_exception(CONTEXT['role'], exc, note=note)


def format_record(role, exc_type, exc_value, tb, note=None, thread=None, root=None):
    stamp = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    lines = [
        f'记录时间: {stamp}',
        f'进程角色: {role}（{ROLE_LABELS.get(role, "未知角色")}）',
        f'进程: PID {os.getpid()}，父进程 {os.getppid()}',
        f'程序: {sys.executable}{"（打包运行）" if getattr(sys, "frozen", False) else ""}',
        f'启动参数: {_arguments()}',
        f'Python: {platform.python_version()}',
        f'平台: {platform.platform()}',
        f'线程: {thread or threading.current_thread().name}',
        f'知识库: {root or "未配置"}',
        f'配置文件: {config_path()}',
        f'异常: {summary(exc_type, exc_value)}',
    ]
    if note:
        lines.append(f'说明: {note}')
    lines.append('')
    text = '\n'.join(lines) + '\n' + ''.join(traceback.format_exception(exc_type, exc_value, tb))
    stacks = _thread_stacks()
    if stacks:
        text += '\n其他线程栈:\n' + stacks
    tail = _log_tail(data_folder(root))
    if tail:
        text += '\n最近日志（service.log 末尾）:\n' + tail
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + '\n…记录超过 64 KiB，已截断\n'
    return text


def summary(exc_type, exc_value):
    module = getattr(exc_type, '__module__', '')
    name = getattr(exc_type, '__name__', str(exc_type))
    if module and module not in ('builtins', '__main__'):
        name = f'{module}.{name}'
    message = str(exc_value)
    if len(message) > 2000:
        message = message[:2000] + '…'
    return f'{name}: {message}' if message else name


def records(limit=5, root=None):
    """最近的崩溃记录，最新的在前；只解析文件头，正文最多返回 8 KiB。"""
    folder = crash_folder(root if root is not None else CONTEXT['root'])
    result = []
    for path in _files(folder):
        if len(result) >= limit:
            break
        try:
            stat = path.stat()
            if not stat.st_size:
                continue
            text = _read(path)
        except OSError:
            continue
        fatal = path.name.startswith('fatal-')
        role = 'fatal' if fatal else _header(text, '进程角色').split('（')[0].strip()
        error = _first_line(text) if fatal else _header(text, '异常')
        result.append(dict(name=path.name, created=_iso(stat.st_mtime),
                           role=role or 'unknown', role_label=ROLE_LABELS.get(role, role or '未知'),
                           fatal=fatal, error=error or '无异常摘要', size=stat.st_size,
                           truncated=stat.st_size > MAX_PREVIEW, text=text))
    return result


def count(root=None):
    total = 0
    for path in _files(crash_folder(root if root is not None else CONTEXT['root'])):
        try:
            if path.stat().st_size:
                total += 1
        except OSError:
            continue
    return total


def clear(root=None):
    removed = 0
    for path in _files(crash_folder(root if root is not None else CONTEXT['root'])):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def enable_fatal(root=None):
    """让 faulthandler 把访问冲突等致命错误写入 crashes/fatal-<pid>.log。"""
    if FATAL['stream'] is not None:
        return FATAL['path']
    try:
        folder = crash_folder(root if root is not None else CONTEXT['root'])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'fatal-{os.getpid()}.log'
        stream = open(path, 'wb')
    except OSError as exc:
        LOGGER.error('致命错误记录不可用: %s', type(exc).__name__)
        return None
    try:
        faulthandler.enable(file=stream, all_threads=True)
    except (OSError, ValueError, RuntimeError) as exc:
        stream.close()
        LOGGER.error('faulthandler 启用失败: %s', type(exc).__name__)
        return None
    FATAL.update(stream=stream, path=path)
    atexit.register(close_fatal)
    return path


def close_fatal():
    """退出时关闭致命错误文件；没有发生崩溃的空文件直接删除。"""
    stream, path = FATAL['stream'], FATAL['path']
    FATAL.update(stream=None, path=None)
    if stream is None:
        return
    try:
        faulthandler.disable()
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        stream.close()
    except OSError:
        pass
    try:
        if path is not None and path.exists() and not path.stat().st_size:
            path.unlink()
    except OSError:
        pass


def _on_uncaught(exc_type, exc_value, tb):
    record(CONTEXT['role'], exc_type, exc_value, tb)
    _chain('excepthook', exc_type, exc_value, tb)


def _on_thread(args):
    thread = getattr(args, 'thread', None)
    record(CONTEXT['role'], args.exc_type, args.exc_value, args.exc_traceback,
           note='线程中未捕获的异常', thread=getattr(thread, 'name', None))
    _chain('thread', args)


def _on_unraisable(item):
    if sys.is_finalizing():
        # 解释器收尾阶段的析构异常不代表服务故障，不产生崩溃记录。
        return
    record(CONTEXT['role'], item.exc_type, item.exc_value, item.exc_traceback,
           note=item.err_msg or '无法抛出的异常')
    _chain('unraisable', item)


def _chain(name, *args):
    """记录后仍交给原钩子；窗口化运行时 stderr 为空，原钩子无处输出。"""
    hook = PREVIOUS.get(name)
    if hook is None or hook is HOOKS.get(name) or sys.stderr is None:
        return
    try:
        hook(*args)
    except Exception:  # 崩溃处理不能再抛出，否则丢失已写入的记录
        LOGGER.exception('原异常钩子执行失败')


HOOKS.update(excepthook=_on_uncaught, thread=_on_thread, unraisable=_on_unraisable)


def _files(folder):
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    items = []
    for path in entries:
        try:
            if (path.is_file() and path.suffix in ('.txt', '.log')
                    and path.name.startswith(('crash-', 'fatal-'))):
                items.append((path.stat().st_mtime, path))
        except OSError:
            continue
    items.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in items]


def _unique(folder, role):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    base = f'crash-{stamp}-{os.getpid()}-{_slug(role)}'
    path = folder / f'{base}.txt'
    index = 1
    while path.exists():
        path = folder / f'{base}-{index}.txt'
        index += 1
    return path


def _slug(role):
    cleaned = ''.join(char for char in str(role) if char.isalnum() or char in '-_')
    return cleaned[:24] or 'unknown'


def _prune(folder):
    for path in _files(folder)[KEEP:]:
        try:
            path.unlink()
        except OSError:
            continue


def _arguments():
    args = ' '.join(sys.argv[1:]) or '(无)'
    return args if len(args) <= 500 else args[:500] + '…'


def _thread_stacks(limit=16384):
    """其他线程的 Python 栈；死锁或后台线程异常时用于还原现场。"""
    current = threading.get_ident()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    chunks = []
    total = 0
    for ident, frame in list(sys._current_frames().items()):
        if ident == current:
            continue
        name = names.get(ident, f'线程 {ident}')
        chunk = f'--- {name} ---\n' + ''.join(traceback.format_stack(frame, limit=12))
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            chunks.append('…其余线程已省略\n')
            break
    return ''.join(chunks)


def _log_tail(folder, lines=40, limit=8192):
    path = Path(folder) / 'service.log'
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            content = stream.readlines()[-lines:]
    except OSError:
        return ''
    return ''.join(content)[-limit:]


def _read(path):
    with open(path, encoding='utf-8', errors='replace') as stream:
        return stream.read(MAX_PREVIEW)


def _header(text, key):
    prefix = f'{key}: '
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ''


def _first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ''


def _iso(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec='seconds')
