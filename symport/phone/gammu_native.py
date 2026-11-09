"""Documented python-gammu API, isolated in a timeout-bounded worker.

No protocol implementation. Raw phone data travels over private stdin/stdout only.
The Gammu library debug log is disabled before connecting.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

MAX_RECORDS = 5000
DOMAINS = {
    "contacts": ("GetNextMemory", {"Type": "ME"}, "GetMemoryStatus", {"Type": "ME"}),
    "calendar": ("GetNextCalendar", {}, "GetCalendarStatus", {}),
    "tasks": ("GetNextToDo", {}, "GetToDoStatus", {}),
    "sms": ("GetNextSMS", {"Folder": 0}, "GetSMSStatus", {}),
}


class PhoneOperationError(ValueError):
    """A static, privacy-safe adapter diagnostic."""


def encode(value):
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode()}
    raise TypeError("Unsupported phone data type")


def decode(value):
    if set(value) == {"__datetime__"}:
        return datetime.fromisoformat(value["__datetime__"])
    if set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"], validate=True)
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, default=encode, sort_keys=True).encode()).hexdigest()


def available() -> bool:
    return importlib.util.find_spec("gammu") is not None


_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _run_worker(command, timeout, *, input=None, **popen):
    """subprocess.run that remembers its child, so a closing Bridge can stop an identify that holds a COM port."""
    with subprocess.Popen(command, **popen) as process:
        with _ACTIVE_LOCK:
            _ACTIVE.add(process)
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def terminate_workers() -> None:
    """Kill every running Gammu worker (Bridge shutdown). The waiting caller sees a failed request, nothing else."""
    with _ACTIVE_LOCK:
        running = list(_ACTIVE)
    for process in running:
        try:
            process.kill()
        except OSError:
            pass


def invoke(config: str, action: str, **values) -> dict:
    if not available():
        raise PhoneOperationError(
            "python-gammu is not installed on the PC Bridge. Use a Nokia Suite export, or install SymPort with the phone extra"
        )
    try:
        if getattr(sys, "frozen", False):
            return invoke_frozen(config, action, values)
        result = _run_worker(
            [sys.executable, "-m", __name__],
            900 if action == "batch" else 20 if action == "identify" else 180,
            input=json.dumps(dict(values, config=config, action=action), default=encode),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode or len(result.stdout) > 32 * 1024 * 1024:
            raise PhoneOperationError(
                "Gammu connection failed; reconnect and read the phone before retrying"
            )
        data = json.loads(result.stdout, object_hook=decode)
        if data.get("error"):
            raise PhoneOperationError(data["error"])
        return data
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise PhoneOperationError(
            "Gammu did not complete the operation. Read the phone again before retrying a write"
        ) from exc


def error_code(exc) -> str:
    name = type(exc).__name__
    return name if re.fullmatch("ERR_[A-Z_]+", name) else "ERR_CONNECTION"


def read_records(sm, domain: str) -> list[dict]:
    method, args, status_method, status_args = DOMAINS[domain]
    # A successful explicit zero count proves an empty store. ERR_EMPTY alone
    # cannot distinguish an unsupported service from a genuinely empty store.
    status = getattr(sm, status_method)(**status_args)
    count = (
        status.get("Used")
        if domain != "sms"
        else sum(status.get(k, 0) for k in ("SIMUsed", "PhoneUsed", "TemplatesUsed"))
    )
    if domain == "sms" and not any(k in status for k in ("SIMUsed", "PhoneUsed", "TemplatesUsed")):
        raise PhoneOperationError("SMS count is unavailable")
    if count == 0:
        return []
    result, seen, location = [], set(), None
    while True:
        try:
            value = getattr(sm, method)(
                **args, **({"Start": True} if location is None else {"Location": location})
            )
        except Exception as exc:
            if (
                error_code(exc) == "ERR_EMPTY"
                and result
                and (count is None or len(result) >= count)
            ):
                return result
            raise
        records = value if isinstance(value, list) else [value]
        if not records:
            raise PhoneOperationError("Empty iteration response")
        last = records[-1].get("Location")
        if last is None or last in seen or len(result) + len(records) > MAX_RECORDS:
            raise PhoneOperationError(
                "Phone data exceeds the safe read limit or iteration did not advance"
            )
        seen.add(last)
        result.extend(records)
        location = last


def identity(sm, telemetry=False) -> dict:
    def optional(method):
        try:
            return getattr(sm, method)()
        except Exception:
            return None

    model, firmware, imei = optional("GetModel"), optional("GetFirmware"), optional("GetIMEI")
    battery, signal, fs = (
        (
            optional("GetBatteryCharge"),
            optional("GetSignalQuality"),
            optional("GetFileSystemStatus"),
        )
        if telemetry
        else (None, None, None)
    )
    model = " / ".join(str(v) for v in model if v) if isinstance(model, (list, tuple)) else model
    firmware = firmware[0] if isinstance(firmware, (tuple, list)) and firmware else firmware
    rm = re.search(r"RM-\d+", model or "")

    def percent(value):
        return value if isinstance(value, (int, float)) and 0 <= value <= 100 else None

    return {
        "manufacturer": optional("GetManufacturer"),
        "model": model,
        "firmware": firmware,
        "rm": rm.group() if rm else None,
        "battery": percent((battery or {}).get("BatteryPercent")),
        "signal": percent((signal or {}).get("SignalPercent")),
        "free": (fs or {}).get("Free"),
        "total": None,
        "storage_scope": "gammu-filesystem",
        "imei": ("•" * max(0, len(imei) - 4) + imei[-4:]) if isinstance(imei, str) else None,
        "fingerprint": digest([model, firmware, imei]),
        "write_identity": bool(
            isinstance(imei, str) and re.fullmatch(r"[0-9]{14,17}", imei) and len(set(imei)) > 1
        ),
        "usb_mode": None,
        "life_timer_hours": None,
        "life_timer_reason": "The selected adapter does not expose the handset lifetime counter",
    }


def sms_verified(sm, current: dict) -> bool:
    proof_path = os.getenv("SYMPORT_VERIFIED_SMS_PROFILE")
    if not proof_path:
        return False
    try:
        with open(proof_path, encoding="utf-8") as source:
            proof = json.load(source)
        return (
            proof.get("fingerprint") == current["fingerprint"]
            and proof.get("connection") == sm.GetConfig(0).get("Connection")
            and proof.get("sms_send_verified") is True
        )
    except (OSError, ValueError, AttributeError):
        return False


def verified_entry(expected: dict, actual: dict) -> bool:
    if any(
        expected.get(k) != actual.get(k)
        for k in ("Type", "Priority", "MemoryType")
        if k in expected
    ):
        return False

    def stable(record):
        return [
            {k: e[k] for k in ("Type", "Value", "Location", "PictureType") if k in e}
            for e in record.get("Entries", [])
            if e.get("Type") not in ("LastModified", "LAST_MODIFIED")
        ]

    remaining = stable(actual)
    for entry in stable(expected):
        if entry not in remaining:
            return False
        remaining.remove(entry)
    return True


def write_entry(sm, data: dict) -> dict:
    domain = data["domain"]
    record = data["record"]
    method = {"contacts": "Memory", "calendar": "Calendar", "tasks": "ToDo"}[domain]
    location = record.get("Location")
    if location:
        before = getattr(sm, "Get" + method)(
            **(
                {"Type": "ME", "Location": location}
                if domain == "contacts"
                else {"Location": location}
            )
        )
        if digest(before) != data.get("before_hash"):
            raise PhoneOperationError("Record changed on the phone; preview again")
        getattr(sm, "Set" + method)(record)
    else:
        location = getattr(sm, "Add" + method)(record)
    after = getattr(sm, "Get" + method)(
        **({"Type": "ME", "Location": location} if domain == "contacts" else {"Location": location})
    )
    return {"record": after, "location": location}


def execute(sm, data: dict) -> dict:
    action = data["action"]
    if action == "details":
        result = identity(sm, telemetry=True)
        result["sms_send_verified"] = sms_verified(sm, result)
        try:
            result["connection"] = sm.GetConfig(0).get("Connection")
        except Exception:
            result["connection"] = None
        result["capability_status"] = {}
        for domain, (_, _, method, args) in DOMAINS.items():
            try:
                getattr(sm, method)(**args)
                result["capability_status"][domain] = {
                    "status": "available",
                    "reason": "Gammu storage status returned successfully",
                }
            except Exception as exc:
                result["capability_status"][domain] = {
                    "status": "unavailable",
                    "reason": error_code(exc),
                }
        return result
    if action == "imei":
        if data.get("confirm") is not True:
            raise PhoneOperationError("Confirm revealing the private device identifier")
        return {"imei": sm.GetIMEI()}
    domain = data.get("domain")
    if domain not in DOMAINS:
        raise PhoneOperationError("Unknown personal data store")
    current = identity(sm)
    if action == "identify":
        current["manufacturer"] = sm.GetManufacturer()
        return current
    if action == "read":
        return {"identity": current, "records": read_records(sm, domain)}
    if (
        data.get("confirm") is not True
        or data.get("fingerprint") != current["fingerprint"]
        or not current["write_identity"]
    ):
        raise PhoneOperationError("Phone identity changed; read it again")
    if action == "write" and domain != "sms":
        return write_entry(sm, data)
    if action == "batch" and domain != "sms":
        changes = data.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 500:
            raise PhoneOperationError("A sync batch must contain 1 to 500 entries")
        if digest(read_records(sm, domain)) != data.get("before_store_hash"):
            raise PhoneOperationError("The phone changed; preview a new plan")
        verified, started = 0, time.monotonic()
        for change in changes:
            if time.monotonic() - started > 600:
                break
            try:
                result = write_entry(sm, dict(change, domain=domain))
                if not verified_entry(change["record"], result["record"]):
                    break
                verified += 1
            except Exception:
                break  # Never expose private Gammu exception text or retry a write.
        return {
            "status": "verified-by-readback"
            if verified == len(changes)
            else "partial-or-unverified",
            "verified": verified,
            "requested": len(changes),
        }
    if action == "send" and domain == "sms":
        # Operator-enrolled exact device/driver validation, never enabled by a UI checkbox.
        if not sms_verified(sm, current):
            raise PhoneOperationError(
                "SMS sending requires an operator-verified profile for this phone and connection"
            )
        number, text = data.get("number"), data.get("text")
        if (
            not isinstance(number, str)
            or not re.fullmatch(r"\+?[0-9]{3,20}", number)
            or not isinstance(text, str)
            or not 1 <= len(text.encode("utf-16-be")) // 2 <= 70
        ):
            raise PhoneOperationError(
                "Use a valid phone number and one SMS of at most 70 UTF-16 units"
            )
        reference = sm.SendSMS(
            {
                "Number": number,
                "Text": text,
                "SMSC": {"Location": 1},
                "Coding": "Unicode_No_Compression",
            }
        )
        if type(reference) is not int or reference < 0:
            raise PhoneOperationError(
                "The SMS submission result is unknown; read the phone before retrying"
            )
        return {"status": "submitted-to-network", "reference": reference, "delivery": "unknown"}
    raise PhoneOperationError("Unsupported personal data operation")


def invoke_frozen(config: str, action: str, values: dict) -> dict:
    # PyInstaller windowed applications have no stdin/stdout. Use short-lived,
    # owner-private IPC files instead of spawning the UI recursively or logging data.
    ipc = Path(os.environ.get("SYMPORT_BRIDGE_DATA", tempfile.gettempdir())) / "ipc"
    ipc.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="symport-gammu-", dir=ipc) as temporary:
        source, target = Path(temporary) / "request.json", Path(temporary) / "response.json"
        with source.open("x", encoding="utf-8") as output:
            os.chmod(source, 0o600)
            json.dump(dict(values, config=config, action=action), output, default=encode)
        result = _run_worker(
            [sys.executable, "--gammu-worker", str(source), str(target)],
            900 if action == "batch" else 20 if action == "identify" else 180,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not target.is_file() or target.stat().st_size > 32 * 1024 * 1024:
            raise PhoneOperationError(
                "Gammu worker did not complete the request; read the phone before retrying"
            )
        data = json.loads(target.read_text(encoding="utf-8"), object_hook=decode)
        if data.get("error"):
            raise PhoneOperationError(data["error"])
        return data


def worker_files(source: Path, target: Path) -> int:
    """Run one request from the private IPC folder; the exit code is 0 on success.

    The Bridge may close in the middle of an identify and remove the folder (or kill this worker): that is not an error
    worth a traceback, so the vanished folder is a quiet non-zero exit."""
    try:
        if (
            source.parent != target.parent
            or not source.parent.name.startswith("symport-gammu-")
            or source.is_symlink()
            or target.exists()
            or source.stat().st_size > 32 * 1024 * 1024
        ):
            raise ValueError("Invalid private worker request")
        data = json.loads(source.read_text(encoding="utf-8"), object_hook=decode)
        result = process_request(data)
        with target.open("x", encoding="utf-8") as output:
            os.chmod(target, 0o600)
            json.dump(result, output, default=encode)
    except FileNotFoundError:
        return 1
    return 0


def process_request(data: dict) -> dict:
    sm = None
    try:
        import gammu

        if data.get("action") == "self-test":
            card = gammu.EncodeVCARD({'Location': 0, 'MemoryType': 'ME', 'Entries': [{'Type': 'Text_Name', 'Value': 'Offline codec check'}, {'Type': 'Number_Mobile', 'Value': '12345'}]})
            if 'BEGIN:VCARD' not in card or '12345' not in card:
                raise PhoneOperationError('Offline Gammu vCard codec check failed')
            return {"gammu": gammu.Version(), "status": "available", "vcard_codec": "passed"}
        gammu.SetDebugLevel("nothing")
        sm = gammu.StateMachine()
        sm.ReadConfig(Filename=data["config"])
        sm.SetDebugLevel("nothing")
        sm.Init()
        result = execute(sm, data)
    except PhoneOperationError as exc:
        result = {"error": str(exc)}
    except Exception as exc:
        code = error_code(exc)
        result = {
            "error": f"Gammu: {code}. The selected driver cannot complete this request. Use a Nokia Suite export or another configured connection"
        }
    finally:
        if sm is not None:
            try:
                sm.Terminate()
            except Exception:
                pass
    return result


def main():
    data = json.loads(sys.stdin.read(32 * 1024 * 1024), object_hook=decode)
    print(json.dumps(process_request(data), default=encode))


if __name__ == "__main__":
    main()
