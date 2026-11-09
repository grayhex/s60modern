"""Windows side of the Nokia USB OBEX route: SetupAPI enumeration and a raw file handle (ctypes, no PowerShell).

Only the interface class {4F919100-4ADF-11D5-882D-00B0D02FE381} is touched. The Phonet {4F919102-...} and LCIF
{4F91910C-...} interfaces of the same phone are never opened. A path looks like
``\\\\?\\nmwcd#vid_0421&pid_0223&if_obx#...#{4f919100-...}``; it is used for CreateFile and never shown or logged.

Nothing here runs on other systems: ``interface_paths`` returns [] and ``open_channel`` refuses outside Windows.
Written from the documented Win32 calls and checked by stubs; the real handle path is proven only on one E72.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import uuid

from . import nokia_pccs
from .nokia_pccs import PccsBusy, PccsError

DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 3, 0x80
ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION = 5, 32
READ_SIZE = 0xFFFF


def detail_cbsize(pointer_size: int | None = None) -> int:
    """SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: 8 on x64 (DWORD + padded WCHAR), 6 on x86. Not 5."""
    return 8 if (pointer_size or ctypes.sizeof(ctypes.c_void_p)) == 8 else 6


def guid_bytes(text: str = nokia_pccs.INTERFACE_GUID) -> bytes:
    """GUID in the in-memory (mixed-endian) layout Win32 expects."""
    return uuid.UUID(text.strip("{}")).bytes_le


def _api():
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.restype = handle
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [handle]
    kernel32.CreateFileW.restype = handle
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, handle]
    kernel32.ReadFile.argtypes = [handle, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WriteFile.argtypes = [handle, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.CancelIoEx.argtypes = [handle, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [handle]
    return setupapi, kernel32, wintypes


def interface_paths() -> list[str]:
    """Device paths of the Nokia USB OBEX interfaces that are present (usually three per phone: one speaks PCCS)."""
    if os.name != "nt":
        return []
    setupapi, _, wintypes = _api()
    guid = ctypes.create_string_buffer(guid_bytes(), 16)
    info = setupapi.SetupDiGetClassDevsW(guid, None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if info in (None, ctypes.c_void_p(-1).value):
        return []
    paths: list[str] = []
    try:
        # SP_DEVICE_INTERFACE_DATA: DWORD cbSize, GUID, DWORD Flags, ULONG_PTR Reserved
        size = 28 if ctypes.sizeof(ctypes.c_void_p) == 4 else 32  # 4 + 16 + 4, then a pointer-sized field
        for index in range(16):
            data = ctypes.create_string_buffer(size)
            struct.pack_into("I", data, 0, size)
            if not setupapi.SetupDiEnumDeviceInterfaces(info, None, guid, index, data):
                break
            needed = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(info, data, None, 0, ctypes.byref(needed), None)
            if not needed.value or needed.value > 4096:
                continue
            detail = ctypes.create_string_buffer(needed.value)
            struct.pack_into("I", detail, 0, detail_cbsize())
            if setupapi.SetupDiGetDeviceInterfaceDetailW(info, data, detail, needed.value, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


class WindowsChannel:
    """A raw handle on one OBEX interface. One request/answer at a time; the read has a timeout (CancelIoEx)."""

    def __init__(self, path: str):
        if os.name != "nt":
            raise PccsError("no-interface")
        _, self._k32, self._wt = _api()
        handle = self._k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            error = ctypes.get_last_error()
            raise PccsBusy() if error in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION) else PccsError("silent")
        self._handle = ctypes.c_void_p(handle)
        self._lock = threading.Lock()

    def _write(self, packet: bytes) -> None:
        written = self._wt.DWORD(0)
        if not self._k32.WriteFile(self._handle, packet, len(packet), ctypes.byref(written), None) or written.value != len(packet):
            raise PccsError("closed")

    def _read(self) -> bytes:
        out = b""
        while True:
            chunk = ctypes.create_string_buffer(READ_SIZE)
            got = self._wt.DWORD(0)
            if not self._k32.ReadFile(self._handle, chunk, READ_SIZE, ctypes.byref(got), None):
                raise PccsError("closed")
            out += chunk.raw[: got.value]
            if len(out) >= 3 and len(out) >= struct.unpack_from(">H", out, 1)[0]:
                return out
            if not got.value:
                raise PccsError("format")

    def exchange(self, packet: bytes, timeout: float) -> bytes:
        result: dict = {}

        def work():
            try:
                self._write(packet)
                result["data"] = self._read()
            except PccsError as exc:
                result["error"] = exc
            except OSError:
                result["error"] = PccsError("closed")

        with self._lock:
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                self.cancel()  # unblock ReadFile so the thread ends and the handle can be closed
                worker.join(3)
                raise PccsError("timeout")
        if "error" in result:
            raise result["error"]
        return result["data"]

    def cancel(self) -> None:
        if self._handle:
            self._k32.CancelIoEx(self._handle, None)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._k32.CancelIoEx(handle, None)
            self._k32.CloseHandle(handle)


def open_channel(path: str) -> WindowsChannel:
    return WindowsChannel(path)
