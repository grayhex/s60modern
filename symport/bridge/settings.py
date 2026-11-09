"""Owner configuration and Windows current-user DPAPI-protected bridge credential."""

from __future__ import annotations

import ctypes
import json
import os
import secrets
from pathlib import Path

from . import routes


def data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "SymPortBridge"


def protect(raw: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        return raw
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows could not protect the bridge credential")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def load_key(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "bridge-key.dat"
    if path.is_symlink():
        raise ValueError("Invalid credential path")
    if path.exists():
        return protect(path.read_bytes(), True).decode("ascii")
    token = secrets.token_urlsafe(32)
    with path.open("xb") as target:
        os.chmod(path, 0o600)
        target.write(protect(token.encode("ascii")))
    return token


def load_config(root: Path) -> dict:
    path = root / "settings.json"
    if not path.exists():
        return {"schema": 1, "auto_usb": True, "origins": [], "gammu_config": "", "volumes": {}, "transport_preference": "auto"}
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid settings")
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(result, dict)
        or result.get("schema") != 1
        or not isinstance(result.get("volumes", {}), dict)
    ):
        raise ValueError("Invalid settings")
    from symport.phone.bridge_security import origin_allowed

    origins = result.get("origins", [])
    if (
        not isinstance(origins, list)
        or len(origins) > 8
        or any(not isinstance(o, str) or not origin_allowed(o, {o}) for o in origins)
    ):
        raise ValueError("Invalid allowed origins")
    for key in ("auto_usb", "websocket", "update_check"):
        if key in result and type(result[key]) is not bool:
            raise ValueError("Invalid boolean setting")
    if result.get("transport_preference", "auto") not in routes.PREFERENCES:
        raise ValueError("Invalid transport preference")
    for key in ("gammu_config", "connections_file"):
        if key in result and not isinstance(result[key], str):
            raise ValueError("Invalid configuration path")
    if any(
        not isinstance(k, str) or not isinstance(v, str) or not Path(v).is_absolute()
        for k, v in result.get("volumes", {}).items()
    ):
        raise ValueError("Use operator-owned absolute volume paths")
    return result


def save_config(root: Path, data: dict):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = root / ("settings-" + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            os.chmod(temporary, 0o600)
            json.dump(data, target, ensure_ascii=False, indent=2)
        temporary.replace(root / "settings.json")
    finally:
        temporary.unlink(missing_ok=True)
