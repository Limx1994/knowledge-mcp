"""Native Windows notification-area icon and lifecycle menu."""
import logging
from pathlib import Path
import webbrowser

import win32api
import win32con
import win32gui
import pywintypes

LOGGER = logging.getLogger('knowledge.tray')
OPEN_PAGE = 1001
EXIT_APP = 1002


class TrayIcon:
    def __init__(self, state):
        self.state = state
        self.window = None
        self.icon = None
        self.added = False
        self.callback = win32con.WM_USER + 20
        self.taskbar_message = win32gui.RegisterWindowMessage('TaskbarCreated')
        self.class_name = 'LocalKnowledgeMcp.Tray.v1'
        self.module = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = self.module
        wc.lpszClassName = self.class_name
        wc.lpfnWndProc = self.on_message
        win32gui.RegisterClass(wc)
        try:
            self.window = win32gui.CreateWindow(self.class_name, '知识库 MCP', 0,
                                               0, 0, 0, 0, 0, 0, self.module, None)
            self.icon = win32gui.LoadImage(0, str(Path(__file__).resolve().parent / 'web' / 'book.ico'),
                                          win32con.IMAGE_ICON, 32, 32, win32con.LR_LOADFROMFILE)
            self.add_icon()
        except Exception:
            self.close()
            raise

    def tooltip(self):
        root = getattr(self.state.store, 'root', None)
        return ('知识库 MCP · ' + str(root))[:127] if root else '知识库 MCP · 未配置'

    def add_icon(self):
        data = (self.window, 1, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                self.callback, self.icon, self.tooltip())
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, data)
        self.added = True

    def refresh(self):
        if not self.added or not self.window:
            return
        data = (self.window, 1, win32gui.NIF_TIP, self.callback, self.icon, self.tooltip())
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, data)

    def open_page(self):
        try:
            if not webbrowser.open(self.state.web.open_url()):
                raise RuntimeError('默认浏览器未接受打开请求')
        except Exception as exc:
            LOGGER.error('打开浏览器失败: %s', type(exc).__name__)
            try:
                win32gui.MessageBox(self.window, '无法打开默认浏览器，请检查浏览器设置。', '知识库 MCP', win32con.MB_ICONERROR)
            except pywintypes.error as inner:
                LOGGER.error('提示框显示失败: %s', inner)

    def on_message(self, window, message, wparam, lparam):
        try:
            if message == self.taskbar_message:
                self.added = False
                try:
                    self.add_icon()
                except pywintypes.error as exc:
                    # Explorer 刚重启时图标可能暂时无法注册；不能终止后台服务。
                    LOGGER.info('托盘图标重建失败: %s', exc)
                return 0
            if message == self.callback:
                if lparam == win32con.WM_LBUTTONDBLCLK:
                    self.open_page()
                elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                    # 菜单属于 UI 层功能，任何 Win32 错误都不能终止后台服务。
                    try:
                        menu = win32gui.CreatePopupMenu()
                        try:
                            win32gui.AppendMenu(menu, win32con.MF_STRING, OPEN_PAGE, '打开管理页面')
                            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')
                            win32gui.AppendMenu(menu, win32con.MF_STRING, EXIT_APP, '退出')
                            x, y = win32gui.GetCursorPos()
                            win32gui.SetForegroundWindow(window)
                            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                                                   x, y, 0, window, None)
                            win32gui.PostMessage(window, win32con.WM_NULL, 0, 0)
                        finally:
                            win32gui.DestroyMenu(menu)
                    except pywintypes.error as exc:
                        LOGGER.info('托盘菜单显示失败，服务继续运行: %s', exc)
                return 0
            if message == win32con.WM_COMMAND:
                command = win32api.LOWORD(wparam)
                if command == OPEN_PAGE:
                    self.open_page()
                elif command == EXIT_APP:
                    self.state.request_stop()
                return 0
            if message == win32con.WM_CLOSE:
                self.state.stopping.set()
                win32gui.DestroyWindow(window)
                return 0
            if message == win32con.WM_DESTROY:
                if self.added:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (window, 1))
                    self.added = False
                self.window = None
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            # 托盘是 UI 层，不能因界面异常终止后台服务；服务仍可通过
            # 托盘“退出”、本地管道 shutdown 命令或任务管理器结束。
            LOGGER.exception('托盘消息处理失败，服务继续运行')
            return 0
        return win32gui.DefWindowProc(window, message, wparam, lparam)

    def run(self):
        win32gui.PumpMessages()

    def stop(self):
        if self.window and win32gui.IsWindow(self.window):
            win32gui.PostMessage(self.window, win32con.WM_CLOSE, 0, 0)

    def close(self):
        if self.window and win32gui.IsWindow(self.window):
            win32gui.DestroyWindow(self.window)
        if self.icon:
            win32gui.DestroyIcon(self.icon)
            self.icon = None
        win32gui.UnregisterClass(self.class_name, self.module)
