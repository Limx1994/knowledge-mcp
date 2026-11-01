from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import pywintypes
import win32con
import win32gui

from tray import TrayIcon, OPEN_CRASHES, OPEN_PAGE, EXIT_APP


class TrayTests(unittest.TestCase):
    def test_separator_accepts_empty_string(self):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')
        finally:
            win32gui.DestroyMenu(menu)

    def test_real_window_menu_and_recovery(self):
        state = Mock()
        state.store.root = Path('D:/知识库')
        state.web.open_url.return_value = 'http://127.0.0.1:12345/#ticket=test-only'
        state.stopping = threading.Event()
        tray = TrayIcon(state)
        try:
            self.assertTrue(win32gui.IsWindow(tray.window))
            self.assertTrue(tray.added)
            with patch('tray.webbrowser.open', return_value=True) as browser:
                win32gui.SendMessage(tray.window, win32con.WM_COMMAND, OPEN_PAGE, 0)
                browser.assert_called_once_with(state.web.open_url.return_value)
                win32gui.SendMessage(tray.window, tray.callback, 0, win32con.WM_LBUTTONDBLCLK)
                self.assertEqual(browser.call_count, 2)
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (tray.window, 1))
            tray.added = False
            win32gui.SendMessage(tray.window, tray.taskbar_message, 0, 0)
            self.assertTrue(tray.added)
            win32gui.SendMessage(tray.window, win32con.WM_COMMAND, EXIT_APP, 0)
            state.request_stop.assert_called_once()
        finally:
            tray.close()
            win32gui.PumpWaitingMessages()


    def test_crash_folder_command(self):
        state = Mock()
        state.stopping = threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            state.store.root = Path(temp)
            tray = TrayIcon(state)
            try:
                with patch('tray.os.startfile') as startfile:
                    win32gui.SendMessage(tray.window, win32con.WM_COMMAND, OPEN_CRASHES, 0)
                startfile.assert_called_once()
                opened = Path(startfile.call_args[0][0])
                self.assertEqual(opened, Path(temp) / '.data' / 'crashes')
                self.assertTrue(opened.is_dir())
            finally:
                tray.close()
                win32gui.PumpWaitingMessages()


    def test_menu_foreground_failure_keeps_service(self):
        """置前失败是 UI 层问题，不能终止后台服务或断开 MCP 连接。"""
        state = Mock()
        state.store.root = Path('D:/知识库')
        state.web.open_url.return_value = 'http://127.0.0.1:12345/#ticket=test-only'
        state.stopping = threading.Event()
        tray = TrayIcon(state)
        try:
            with patch('tray.win32gui.SetForegroundWindow',
                       side_effect=pywintypes.error(0, 'SetForegroundWindow')):
                with patch('tray.win32gui.TrackPopupMenu'):
                    win32gui.SendMessage(tray.window, tray.callback, 0, win32con.WM_RBUTTONUP)
            self.assertFalse(state.stopping.is_set())
            state.request_stop.assert_not_called()
        finally:
            tray.close()
            win32gui.PumpWaitingMessages()


    def test_taskbar_rebuild_failure_keeps_service(self):
        """Explorer 重启竞态导致图标重建失败时，后台服务必须继续运行。"""
        state = Mock()
        state.store.root = Path('D:/知识库')
        state.web.open_url.return_value = 'http://127.0.0.1:12345/#ticket=test-only'
        state.stopping = threading.Event()
        tray = TrayIcon(state)
        try:
            with patch('tray.win32gui.Shell_NotifyIcon',
                       side_effect=pywintypes.error(-2147467259, 'Shell_NotifyIcon')):
                win32gui.SendMessage(tray.window, tray.taskbar_message, 0, 0)
            self.assertFalse(state.stopping.is_set())
            state.request_stop.assert_not_called()
        finally:
            tray.close()
            win32gui.PumpWaitingMessages()


    def test_unexpected_tray_error_keeps_service(self):
        """未预期的托盘异常只能记录，不能终止后台服务或断开 MCP 连接。"""
        state = Mock()
        state.store.root = Path('D:/知识库')
        state.web.open_url.return_value = 'http://127.0.0.1:12345/#ticket=test-only'
        state.stopping = threading.Event()
        tray = TrayIcon(state)
        try:
            with patch('tray.win32gui.GetCursorPos', side_effect=OSError('模拟菜单构建失败')):
                win32gui.SendMessage(tray.window, tray.callback, 0, win32con.WM_RBUTTONUP)
            self.assertFalse(state.stopping.is_set())
            state.request_stop.assert_not_called()
        finally:
            tray.close()
            win32gui.PumpWaitingMessages()


if __name__ == '__main__':
    unittest.main()
