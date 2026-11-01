"""Launch the user-requested resident process independently of a client's Job."""
import ctypes
from ctypes import wintypes as w
from pathlib import Path
import subprocess

from knowledge_store import StoreError


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
                ('wShowWindow', w.WORD), ('cbReserved2', w.WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', StartupInfo), ('lpAttributeList', ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE),
                ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]


class DetachedProcess:
    def __init__(self, command, env):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self.api
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        api.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        api.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t,
                                                 ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        api.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        api.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                      w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                                      ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInfo)]
        shell = ctypes.WinDLL('user32', use_last_error=True)
        shell.GetShellWindow.restype = w.HWND
        shell.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        window = shell.GetShellWindow()
        if not window:
            raise StoreError('未找到 Windows 桌面 Shell，无法启动独立托盘后台')
        pid = w.DWORD()
        shell.GetWindowThreadProcessId(window, ctypes.byref(pid))
        parent = api.OpenProcess(0x0080, False, pid.value)
        if not parent:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = None
        initialized = False
        try:
            size = ctypes.c_size_t()
            api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            initialized = True
            parent_handle = w.HANDLE(parent)
            if not api.UpdateProcThreadAttribute(attributes, 0, 0x00020000, ctypes.byref(parent_handle),
                                                 ctypes.sizeof(parent_handle), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.lpAttributeList = ctypes.addressof(attributes)
            process = ProcessInfo()
            cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            block = ctypes.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(env.items())) + '\0\0')
            if not api.CreateProcessW(command[0], cmd, None, None, False, 0x08080400,
                                      block, str(Path.home()), ctypes.byref(startup), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
            api.CloseHandle(process.hThread)
            self.handle = process.hProcess
        finally:
            if initialized:
                api.DeleteProcThreadAttributeList(attributes)
            api.CloseHandle(parent)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
