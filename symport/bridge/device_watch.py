"""Hotplug through WM_DEVICECHANGE (a hidden top-level window on its own thread), with the timer poll as the fallback.

The window only *wakes* the Bridge: the Bridge then re-reads the USB list, and the AT back-off starts over only when the set of
Nokia USB devices really changed (see ``Agent.refresh_host``). Written from the documented Win32 calls and checked with stubs;
not verified on hardware (see the checklist). Where the window cannot be created, the 10 s USB poll still notices a plug event.
"""

from __future__ import annotations

import ctypes
import os
import threading

WM_DEVICECHANGE, WM_CLOSE, WM_DESTROY = 0x0219, 0x0010, 0x0002
DBT_DEVNODES_CHANGED, DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE = 0x0007, 0x8000, 0x8004
INTERESTING = (DBT_DEVNODES_CHANGED, DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE)


class DeviceWatcher:
    """``on_change`` is called from the window thread for every relevant message; it must only set an event."""

    def __init__(self, on_change, window_factory=None):
        self.on_change = on_change
        self.window_factory = window_factory or _windows_window
        self.window = None
        self.running = False

    def start(self) -> bool:
        if self.running:
            return True
        try:
            self.window = self.window_factory(self.notify)
        except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
            self.window = None
        self.running = self.window is not None
        return self.running

    def notify(self, message: int, wparam: int) -> bool:
        """Window procedure hook: True when the message was a plug/unplug change."""
        if message != WM_DEVICECHANGE or wparam not in INTERESTING:
            return False
        self.on_change()
        return True

    def stop(self) -> None:
        window, self.window, self.running = self.window, None, False
        if window is not None:
            window.stop()


class _Window:
    """The real hidden window. Created only on Windows."""

    def __init__(self, notify):
        self.notify = notify
        self.hwnd = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="bridge-devicewatch")
        self.thread.start()
        if not ready.wait(5) or not self.hwnd:
            raise OSError("The device-change window could not be created")

    def _run(self, ready: threading.Event) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        proc_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", proc_type), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32.DefWindowProcW.restype = wintypes.LPARAM
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        def procedure(hwnd, message, wparam, lparam):
            if message == WM_DEVICECHANGE:
                self.notify(message, wparam)
                return 1  # TRUE: keep the broadcast going
            if message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        callback = proc_type(procedure)  # must stay referenced while the window exists
        instance = kernel32.GetModuleHandleW(None)
        cls = WndClass()
        cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = callback, instance, "SymPortBridgeDeviceWatch"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            ready.set()
            return
        # A normal top-level window that is never shown: message-only windows do not receive device broadcasts.
        self.hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "SymPort Bridge", 0, 0, 0, 0, 0, None, None, instance, None)
        ready.set()
        if not self.hwnd:
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.UnregisterClassW(cls.lpszClassName, instance)

    def stop(self) -> None:
        if self.hwnd:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)  # DefWindowProc destroys the window; WM_DESTROY ends the loop
        self.thread.join(3)


def _windows_window(notify):
    if os.name != "nt":
        raise OSError("Device-change notifications need Windows")
    return _Window(notify)
