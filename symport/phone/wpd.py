"""Read-only file access to a Nokia that Windows exposes as a Portable Device (MTP / WPD).

Windows shows such a phone as a Portable Device with no drive letter and no COM port. The Windows shell
(`Shell.Application`) is the supported way to browse and copy from it; SymPort does not replace or intercept any
Windows driver. Only **list** and **download** exist here:

* writes are not implemented (they need hardware verification first, a confirmed copy and a read-back);
* the presence of a WPD device proves nothing about contacts, calendar or SMS, so the capability matrix keeps
  every PIM operation unsupported on this connection;
* nothing reports `available` until a real storage listing succeeded on this machine.

The PowerShell side runs on the Windows PC with the Bridge only. Everything the phone returns (names, sizes) is
untrusted and sanitised before it reaches the browser. Written against the documented Shell/WPD behaviour; it has
not yet been exercised on a physical phone (see docs/BRIDGE_HARDWARE_CHECKLIST.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import capabilities
from .obex import remote_path

LIMIT = 64 * 1024 * 1024
SOURCE = "windows-portable-device"
MTP_ONLY = "Windows Portable Devices (MTP) expose files only; they say nothing about contacts, calendar or SMS"

PRELUDE = r"""
$ErrorActionPreference = 'Stop'
$req = $env:SYMPORT_WPD_INPUT | ConvertFrom-Json
$shell = New-Object -ComObject Shell.Application
$computer = $shell.NameSpace(17)
function Get-Devices {
  @($computer.Items() | Where-Object { $_.IsFolder -and (([string]$_.Path) -match '(?i)vid_0421') } |
    Sort-Object { [string]$_.Path })
}
function Find-Child($folder, [string]$name) {
  $folder.Items() | Where-Object { $_.Name -ceq $name } | Select-Object -First 1
}
function Enter-Path($device, $parts) {
  $cur = $device.GetFolder
  foreach ($part in $parts) {
    $child = Find-Child $cur $part
    if (-not $child -or -not $child.IsFolder) { throw 'path' }
    $cur = $child.GetFolder
  }
  $cur
}
"""

PROBE = PRELUDE + r"""
$out = @(); $index = 0
foreach ($device in (Get-Devices)) {
  $storages = @()
  try { foreach ($s in $device.GetFolder.Items()) { $storages += [string]$s.Name } } catch { }
  $out += @{ index = $index; storages = $storages }
  $index++
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
"""

LIST = PRELUDE + r"""
$device = @(Get-Devices)[[int]$req.index]
if (-not $device) { throw 'device' }
$cur = Enter-Path $device @($req.parts)
$rows = @()
foreach ($item in $cur.Items()) {
  $rows += @{ name = [string]$item.Name; directory = [bool]$item.IsFolder; size = $(if ($item.IsFolder) { $null } else { [int64]$item.Size }) }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
"""

DOWNLOAD = PRELUDE + r"""
$device = @(Get-Devices)[[int]$req.index]
if (-not $device) { throw 'device' }
$parts = @($req.parts)
$cur = Enter-Path $device @($parts[0..($parts.Count - 2)] | Where-Object { $_ -ne $null })
$item = Find-Child $cur $parts[$parts.Count - 1]
if (-not $item -or $item.IsFolder) { throw 'file' }
$size = [int64]$item.Size
if ($size -gt [int64]$req.limit) { throw 'size' }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('symport-wpd-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
$shell.NameSpace($tmp).CopyHere($item, 0x414)   # 4 no progress UI, 16 yes to all, 1024 no error UI
$deadline = (Get-Date).AddSeconds(120); $target = $null
while ((Get-Date) -lt $deadline) {
  $f = Get-ChildItem -LiteralPath $tmp -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($f -and $f.Length -eq $size) { $target = $f.FullName; break }
  Start-Sleep -Milliseconds 300
}
if (-not $target) { throw 'copy' }
ConvertTo-Json -InputObject @{ path = $target; dir = $tmp; size = $size } -Compress
"""


def run_powershell(script: str, request: dict, timeout: int = 150):
    """Run a script on the Windows PC and return its JSON. Never raises anything but ValueError."""
    if os.name != "nt":
        raise ValueError("Windows Portable Devices are available only on Windows")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "SYMPORT_WPD_INPUT": json.dumps(request)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise ValueError("The portable device could not be read")
        return json.loads(result.stdout or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError("The portable device could not be read") from exc


def clean(name: object) -> str:
    """Names come from the phone: drop control characters and path separators, bound the length."""
    text = re.sub(r"[\x00-\x1f\x7f/\\]", "", str(name))
    return text[:255]


class WpdAdapter:
    """`id` is fixed: at most one Nokia Portable Device (the first, in a stable order) is offered."""

    id = "wpd-nokia"

    def __init__(self, runner=None, clock=time.monotonic):
        self.runner = runner or run_powershell
        self.clock = clock
        self._cache: tuple[float, list[dict] | None, str | None] = (-1e9, None, None)

    # ---- discovery -------------------------------------------------------------------------------------------
    def probe(self, force: bool = False):
        now = self.clock()
        if not force and now - self._cache[0] < 15:
            return self._cache[1], self._cache[2]
        try:
            devices = self.runner(PROBE, {})
            devices = devices if isinstance(devices, list) else []
            cleaned = [
                {"index": int(d.get("index", 0)), "storages": [clean(s) for s in (d.get("storages") or [])][:16]}
                for d in devices[:4]
                if isinstance(d, dict)
            ]
            self._cache = (now, cleaned, None)
        except ValueError as exc:
            self._cache = (now, None, str(exc))
        return self._cache[1], self._cache[2]

    def storages(self) -> list[str]:
        devices, _ = self.probe()
        return devices[0]["storages"] if devices else []

    def matrix(self, storages: list[str]) -> capabilities.Matrix:
        m = capabilities.Matrix()
        if storages:
            m.available("files.list", SOURCE, f"Windows listed {len(storages)} storage(s) of the portable device")
            m.untested("files.download", SOURCE, "A real download proves this; it is read-only and size-limited")
        else:
            m.error("files.list", SOURCE, "Windows shows the device but exposes no storage (locked phone or wrong USB mode)")
            m.error("files.download", SOURCE, "No storage to read from")
        for op in ("files.upload", "files.stage", "files.mkdir", "files.delete", "packages.transfer", "media.read"):
            m.unsupported(op, SOURCE, "Not implemented for Windows Portable Devices; use Mass storage for file changes")
        for op in ("identity.read", "contacts.read", "contacts.write", "contacts.stage", "calendar.read", "tasks.read",
                   "sms.read", "sms.send", "telemetry.read", "storage.read"):
            m.unsupported(op, SOURCE, MTP_ONLY)
        return m

    def device(self) -> dict | None:
        devices, reason = self.probe()
        if reason is not None:
            return None  # not a Nokia portable device, or Windows could not enumerate: nothing is invented
        if not devices:
            return None
        storages = devices[0]["storages"]
        row = {
            "connection_identity": hashlib.sha256(json.dumps(["wpd", devices[0]["index"], storages]).encode()).hexdigest(),
            "id": self.id,
            "name": "Nokia (Windows Portable Device)",
            "transport": "wpd",
            "connection_kind": "usb",
            "connected": bool(storages),
            "model": None,
            "rm": None,
            "firmware": None,
            "battery": None,
            "storage_scope": "mtp-storages",
            "evidence": "Windows Portable Device (MTP): files only; phone identity, contacts, calendar and SMS are not available over this connection",
        }
        return capabilities.apply(row, self.matrix(storages))

    # ---- operations ------------------------------------------------------------------------------------------
    def _parts(self, value: object) -> list[str]:
        path = remote_path(value if isinstance(value, str) else "")
        if ":" in path:
            raise ValueError("Portable device paths start with a storage name, not a drive letter")
        return path.split("/") if path else []

    def execute(self, data: dict) -> dict:
        action = data.get("action")
        if action == "connection-diagnose":
            return {"connection": self.device()}
        parts = self._parts(data.get("path", ""))
        if action == "list":
            rows = self.runner(LIST, {"index": 0, "parts": parts})
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("The portable device returned an unusable listing")
            entries = []
            for row in rows:
                name = clean(row.get("name", "")) if isinstance(row, dict) else ""
                if not name or name in (".", ".."):
                    continue
                size = row.get("size")
                entries.append(
                    {"name": name, "directory": bool(row.get("directory")),
                     "size": size if isinstance(size, int) and size >= 0 else None}
                )
            return {"entries": entries}
        if action == "download":
            if not parts:
                raise ValueError("Choose a file")
            info = self.runner(DOWNLOAD, {"index": 0, "parts": parts, "limit": LIMIT})
            if not isinstance(info, dict):
                raise ValueError("The portable device did not return the file")
            temporary = Path(tempfile.gettempdir()).resolve()
            target = Path(str(info.get("path", ""))).resolve()
            folder = Path(str(info.get("dir", ""))).resolve()
            if (
                folder.parent != temporary
                or not folder.name.startswith("symport-wpd-")
                or target.parent != folder
                or target.is_symlink()
            ):
                raise ValueError("The portable device returned an unexpected file location")
            try:
                if not target.is_file() or target.stat().st_size != info.get("size") or target.stat().st_size > LIMIT:
                    raise ValueError("The download was incomplete; no result is recorded")
                return {"name": parts[-1], "content": base64.b64encode(target.read_bytes()).decode()}
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        raise ValueError("Windows Portable Devices support only listing and downloading files")
