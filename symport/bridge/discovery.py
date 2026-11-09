"""Nokia-only host enumeration. No pairing, jailbreak, driver installation or SMS."""

from __future__ import annotations

import json
import os
import re
import subprocess

# Documented device families, not measured firmware/OS facts. Other Nokia devices
# are reported as candidates until explicitly enrolled; S40/other vendors aren't promoted.
from symport.phone.models import documented_model


def serial_candidates():
    try:
        from serial.tools import list_ports
    except ImportError:
        return [], "pyserial is unavailable; configure an existing Gammu connection manually"
    result = []
    for port in list_ports.comports():
        description = " ".join(
            str(x or "") for x in (port.manufacturer, port.description, port.product)
        )
        if port.vid != 0x0421 and "nokia" not in description.casefold():
            continue
        if not isinstance(port.device, str) or not re.fullmatch(
            r"COM[0-9]{1,4}|/dev/[A-Za-z0-9_./-]{1,120}", port.device
        ):
            continue
        result.append(
            {
                "port": port.device,
                "model_profile": documented_model(description),
                "vendor": "Nokia",
                "source": "host-serial-enumeration",
            }
        )
    return result, None


def windows_nokia_volumes():
    if os.name != "nt":
        return [], None
    # CIM associations tie each logical drive to its physical Nokia USB device.
    # Drive letters and Bluetooth names alone are never treated as phone identity.
    script = """$ErrorActionPreference='Stop'; $items=@(); Get-CimInstance Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' -and ($_.PNPDeviceID -match 'NOKIA|VID_0421' -or $_.Model -match 'Nokia') } | ForEach-Object { $disk=$_; Get-CimAssociatedInstance -InputObject $disk -Association Win32_DiskDriveToDiskPartition | ForEach-Object { Get-CimAssociatedInstance -InputObject $_ -Association Win32_LogicalDiskToPartition | Where-Object { ($_.DriveType -eq 2 -or $_.DriveType -eq 3) -and $_.DeviceID -ne $env:SystemDrive } | ForEach-Object { $items+=@{root=($_.DeviceID+'/');description=$disk.Model;identity=($disk.PNPDeviceID+'|'+$_.VolumeSerialNumber)} } } }; ConvertTo-Json -InputObject @($items) -Compress"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("Enumeration failed")
        rows = json.loads(result.stdout)
        rows = rows if isinstance(rows, list) else [rows]
        return [
            {
                "root": r["root"],
                "model_profile": documented_model(r.get("description", "")),
                "source": "Nokia USB disk/CIM association",
                "identity": __import__("hashlib")
                .sha256(r.get("identity", "").encode())
                .hexdigest(),
            }
            for r in rows
            if isinstance(r, dict) and re.fullmatch("[D-Zd-z]:/", str(r.get("root", "")))
            and str(r["root"])[:2].upper() != os.environ.get("SystemDrive", "C:").upper()
        ], None
    except (OSError, ValueError, subprocess.SubprocessError):
        return (
            [],
            "Windows Nokia disk enumeration is unavailable; enroll the memory card explicitly",
        )


def paired_bluetooth_candidates():
    """Read paired host records only. Names are candidates, never phone identity."""
    import shutil

    try:
        if os.name == "nt":
            script = "$ErrorActionPreference='Stop'; $rows=@(Get-PnpDevice -Class Bluetooth -PresentOnly | Where-Object { $_.InstanceId -match '^BTHENUM\\\\DEV_[0-9A-F]{12}' -and $_.FriendlyName -match 'Nokia' } | Select-Object FriendlyName,InstanceId); ConvertTo-Json -InputObject @($rows) -Compress"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError("Bluetooth enumeration failed")
            rows = json.loads(result.stdout)
            rows = rows if isinstance(rows, list) else [rows]
            candidates = []
            for row in rows[:128]:
                match = re.search(r"DEV_([A-Fa-f0-9]{12})", row.get("InstanceId", ""))
                if match:
                    address = ":".join(match[1][i : i + 2] for i in range(0, 12, 2)).upper()
                    candidates.append(
                        {
                            "name": str(row.get("FriendlyName", "Nokia"))[:100],
                            "address": address,
                            "model_profile": documented_model(str(row.get("FriendlyName", ""))),
                            "source": "Windows paired PnP record",
                            "connected": None,
                        }
                    )
            return candidates, None
        executable = shutil.which("bluetoothctl")
        if not executable:
            return (
                [],
                "Bluetooth host tools are unavailable; pair and inspect services in your operating system",
            )
        result = subprocess.run(
            [executable, "devices", "Paired"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("Bluetooth enumeration failed")
        rows = []
        for line in result.stdout.splitlines()[:128]:
            match = re.fullmatch(r"Device ((?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}) (.{1,100})", line)
            if match and "nokia" in match[2].casefold():
                rows.append(
                    {
                        "name": match[2],
                        "address": match[1],
                        "model_profile": documented_model(match[2]),
                        "source": "BlueZ paired record",
                        "connected": None,
                    }
                )
        return rows, None
    except (OSError, ValueError, subprocess.SubprocessError):
        return (
            [],
            "Paired Nokia Bluetooth enumeration is unavailable; inspect pairing in the operating system",
        )


NOKIA_VID = "0421"


def usb_devices(sysfs: str = "/sys/bus/usb/devices"):
    """Nokia USB devices the host *sees*, whatever driver state. Only VID/PID/class/status.

    This is what separates "nothing is plugged in / charge-only cable" from "a Nokia is there but no
    driver or COM port". Serial numbers and device paths are never returned. A seen device is a
    candidate, not a confirmed phone.
    """
    rows: list[dict] = []
    if os.name == "nt":
        script = (
            "$ErrorActionPreference='Stop'; $rows=@(Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.InstanceId -match '^USB\\\\VID_0421' } | "
            "Select-Object Class,Status,InstanceId,ConfigManagerErrorCode); ConvertTo-Json -InputObject @($rows) -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError("USB enumeration failed")
            parsed = json.loads(result.stdout or "[]")
            parsed = parsed if isinstance(parsed, list) else [parsed]
        except (OSError, ValueError, subprocess.SubprocessError):
            return [], "Windows USB enumeration is unavailable; check Device Manager for a Nokia entry"
        for item in parsed[:64]:
            match = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", str(item.get("InstanceId")))
            if not match or match[1].upper() != NOKIA_VID.upper():
                continue
            # `mi` is the composite-interface number only (never the serial part of the instance id); `problem_code`
            # is Device Manager's error code (28 = no driver installed, 10 = cannot start, ...).
            interface = re.search(r"&MI_([0-9A-Fa-f]{2})", str(item.get("InstanceId")))
            code = item.get("ConfigManagerErrorCode")
            rows.append(
                {
                    "vid": match[1].lower(),
                    "pid": match[2].lower(),
                    "class": re.sub(r"[^A-Za-z0-9 ._-]", "", str(item.get("Class") or "unknown"))[:40],
                    "status": "ok" if str(item.get("Status")) == "OK" else "problem",
                    "mi": interface[1].lower() if interface else None,
                    "problem_code": code if isinstance(code, int) and code else None,
                    "source": "Windows PnP record",
                }
            )
    else:
        base = os.fspath(sysfs)
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return [], "USB enumeration is unavailable on this host"
        for entry in entries[:256]:
            if not re.fullmatch(r"[0-9]+-[0-9.]+", entry):
                continue
            try:
                with open(os.path.join(base, entry, "idVendor"), encoding="ascii") as handle:
                    vid = handle.read().strip().lower()
                if vid != NOKIA_VID:
                    continue
                with open(os.path.join(base, entry, "idProduct"), encoding="ascii") as handle:
                    pid = handle.read().strip().lower()
            except OSError:
                continue
            bound = any(
                os.path.islink(os.path.join(base, name, "driver"))
                for name in os.listdir(base)
                if name.startswith(entry + ":")
            )
            rows.append(
                {
                    "vid": vid,
                    "pid": re.sub(r"[^0-9a-f]", "", pid)[:4],
                    "class": "usb",
                    "status": "ok" if bound else "problem",
                    "source": "Linux sysfs",
                }
            )
    unique = {(r["vid"], r["pid"], r["class"], r["status"], r.get("mi")): r for r in rows}
    return list(unique.values()), None


def host_candidates():
    import platform

    from symport.phone import gammu_native

    from . import VERSION

    serial, serial_reason = serial_candidates()
    bluetooth, bluetooth_reason = paired_bluetooth_candidates()
    volumes, volume_reason = windows_nokia_volumes()
    usb, usb_reason = usb_devices()
    # Do not expose physical PNP identifiers, even hashed ones, to the browser.
    return {
        "serial": serial,
        "bluetooth": bluetooth,
        "volumes": [{k: v for k, v in r.items() if k != "identity"} for r in volumes],
        "usb": usb,
        "diagnostics": [
            r for r in (serial_reason, bluetooth_reason, volume_reason, usb_reason) if r
        ],
        "environment": {
            "bridge_version": VERSION,
            # OS family/release only; no host name, user name or paths.
            "os": f"{platform.system()} {platform.release()}"[:60],
            "python_gammu": gammu_native.available(),
        },
        "evidence": "Host records only; a device response or explicit enrollment is required before use",
    }
