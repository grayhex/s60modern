"""Windows PnP enumeration through SetupAPI/CfgMgr32 (ctypes): no PowerShell process, no window, no start-up cost.

Returns the same fields the PowerShell scripts of ``discovery`` returned, so the callers' filtering is unchanged. ``None``
means “not available here” (not Windows, or a call failed) and tells the caller to fall back to PowerShell.
Only present devices are listed. Serial numbers appear in instance ids; they are used for filtering by the callers and are
never returned outside the Bridge (the callers keep VID/PID/class/status only).
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator

DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
SPDRP_DEVICEDESC, SPDRP_CLASS, SPDRP_FRIENDLYNAME = 0x0, 0x7, 0xC
CR_SUCCESS = 0


def devinfo_size(pointer_size: int | None = None) -> int:
    """sizeof(SP_DEVINFO_DATA): DWORD + GUID + DWORD + ULONG_PTR (28 on x86, 32 on x64)."""
    return 32 if (pointer_size or ctypes.sizeof(ctypes.c_void_p)) == 8 else 28


class SetupApi:
    """The real SetupAPI/CfgMgr32 calls. Tests replace it with an object that has ``devices(enumerator)``."""

    def devices(self, enumerator: str) -> Iterator[dict]:
        import struct
        from ctypes import wintypes

        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        cfgmgr = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        handle = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.restype = handle
        setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
        setupapi.SetupDiEnumDeviceInfo.argtypes = [handle, wintypes.DWORD, ctypes.c_void_p]
        setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [handle, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            handle, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [handle]
        cfgmgr.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG), wintypes.DWORD, wintypes.ULONG]
        info = setupapi.SetupDiGetClassDevsW(None, enumerator, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
        if info in (None, ctypes.c_void_p(-1).value):
            raise OSError("SetupAPI could not list devices")
        size = devinfo_size()

        def text(entry, prop) -> str:
            buffer = ctypes.create_unicode_buffer(512)
            needed = wintypes.DWORD(0)
            if setupapi.SetupDiGetDeviceRegistryPropertyW(info, entry, prop, None, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
                return buffer.value
            return ""

        try:
            for index in range(2048):
                entry = ctypes.create_string_buffer(size)
                struct.pack_into("I", entry, 0, size)
                if not setupapi.SetupDiEnumDeviceInfo(info, index, entry):
                    break
                ident = ctypes.create_unicode_buffer(512)
                if not setupapi.SetupDiGetDeviceInstanceIdW(info, entry, ident, 512, None):
                    continue
                devinst = struct.unpack_from("I", entry, 20)[0]  # SP_DEVINFO_DATA.DevInst
                status, problem = wintypes.ULONG(0), wintypes.ULONG(0)
                ok = cfgmgr.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinst, 0) == CR_SUCCESS
                yield {
                    "InstanceId": ident.value,
                    "Class": text(entry, SPDRP_CLASS),
                    "FriendlyName": text(entry, SPDRP_FRIENDLYNAME) or text(entry, SPDRP_DEVICEDESC),
                    "ConfigManagerErrorCode": problem.value if ok else None,
                    "Status": "OK" if ok and not problem.value else "Error",
                }
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(info)


def enumerate_present(enumerator: str, prefix: str, api=None) -> list[dict] | None:
    """Present devices of one enumerator (``USB``, ``BTHENUM``) whose instance id starts with ``prefix`` (case-insensitive)."""
    if api is None and os.name != "nt":
        return None
    try:
        source = api or SetupApi()
        rows = [d for d in source.devices(enumerator) if str(d.get("InstanceId", "")).upper().startswith(prefix.upper())]
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return None  # the caller falls back to PowerShell
    return rows[:128]
