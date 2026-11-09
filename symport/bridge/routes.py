"""Routes to a Nokia and the choice between them (pure logic, stdlib only; used by the Doctor, the Agent and the tray).

A *route* is one independent way to talk to the phone. The four routes never imply each other:

* ``gammu``        identity, contacts, calendar, SMS through Gammu (COM port or the Nokia-specific DKU-2 connection)
* ``pccs``         contacts and calendar, read-only, through the Nokia USB OBEX interface (no COM, no Gammu)
* ``mtp``          files, read-only, as a Windows Portable Device
* ``mass-storage`` files (and backups) on a drive letter

Each route has one state: ``unavailable`` (not there), ``busy`` (another program holds it), ``silent`` (found, the phone does
not answer), ``available`` (the phone answered a safe probe) or ``verified`` (a real operation completed in this session).
``choose`` picks which route serves which job; it never talks to the phone.
"""

from __future__ import annotations

import re

ROUTE_IDS = ("gammu", "pccs", "mtp", "mass-storage")
STATES = ("unavailable", "busy", "silent", "available", "verified")
USABLE = ("available", "verified")
PREFERENCES = ("auto", "gammu", "pccs", "mtp")
JOBS = ("contacts", "calendar", "sms", "files")
SERVES = {"gammu": ("contacts", "calendar", "sms"), "pccs": ("contacts", "calendar"), "mtp": ("files",), "mass-storage": ("files",)}
READ_ONLY = ("pccs", "mtp")

# Gammu error codes that may reach the Doctor. Anything else becomes "ERR_CONNECTION": raw Gammu or OS text is never shown.
GAMMU_CODES = {
    "ERR_DEVICENOTEXIST": (
        "Gammu does not find the Nokia interface.",
        "Gammu не находит Nokia-интерфейс.",
    ),
    "ERR_DEVICEBUSY": (
        "The port or interface is held by another program.",
        "Порт или интерфейс занят другой программой.",
    ),
    "ERR_DEVICELOCKED": (
        "The port or interface is locked by another program.",
        "Порт или интерфейс заблокирован другой программой.",
    ),
    "ERR_DEVICEOPENERROR": (
        "Gammu could not open the device.",
        "Gammu не смог открыть устройство.",
    ),
    "ERR_DEVICENOPERMISSION": (
        "Windows denied access to the device.",
        "Windows запретил доступ к устройству.",
    ),
    "ERR_DEVICENODRIVER": (
        "No driver is installed for the device.",
        "Для устройства не установлен драйвер.",
    ),
    "ERR_TIMEOUT": (
        "The phone did not answer in time.",
        "Телефон не ответил вовремя.",
    ),
    "ERR_NOTCONNECTED": (
        "Gammu is not connected to the phone.",
        "Gammu не подключён к телефону.",
    ),
    "ERR_DEVICEREADERROR": (
        "Reading from the device failed.",
        "Чтение с устройства не удалось.",
    ),
    "ERR_DEVICEWRITEERROR": (
        "Writing to the device failed.",
        "Запись в устройство не удалась.",
    ),
    "ERR_CONNECTION": (
        "Gammu could not talk to the phone.",
        "Gammu не смог связаться с телефоном.",
    ),
}
# ERR_DEVICENOTEXIST over the Nokia-specific (dku2) connection: the x64 Gammu build does not see the Nokia interface.
DKU2_NOT_FOUND = (
    "Gammu does not find the Nokia interface; this is a known limit of the x64 Gammu build, use the USB OBEX route.",
    "Gammu не находит Nokia-интерфейс; это известное ограничение x64-сборки Gammu, используйте маршрут USB OBEX.",
)


def gammu_code(text: object) -> str:
    """The Gammu error code found in a worker message, from the fixed list; '' when there is none."""
    for code in re.findall(r"ERR_[A-Z_]+", str(text or "")):
        return code if code in GAMMU_CODES else "ERR_CONNECTION"
    return ""


def gammu_hint(code: str, *, dku2: bool = False) -> tuple[str, str] | None:
    """EN/RU sentence for a code from the fixed list; None for anything else."""
    if code not in GAMMU_CODES:
        return None
    return DKU2_NOT_FOUND if code == "ERR_DEVICENOTEXIST" and dku2 else GAMMU_CODES[code]


def _usable(states: dict, route: str) -> bool:
    return states.get(route) in USABLE


def choose(preference: str, states: dict, *, gammu_tried: bool = False, operation_in_progress: bool = False,
           current: dict | None = None) -> dict:
    """Which route serves which job.

    ``auto``: contacts and calendar → Gammu when it is *verified*, otherwise USB OBEX; SMS → Gammu only; files → Mass
    storage, otherwise MTP (read-only). An explicit preference uses only that route and lists what it cannot do.
    While a phone operation runs, an existing choice is kept (``deferred``): the route never changes under a running read.
    """
    if operation_in_progress and current is not None:
        return {**current, "deferred": True}
    pref = preference if preference in PREFERENCES else "auto"
    out: dict = {"preference": pref, "contacts": None, "calendar": None, "sms": None, "files": None}
    if pref == "auto":
        pim = "gammu" if states.get("gammu") == "verified" else "pccs" if _usable(states, "pccs") else None
        out["contacts"] = out["calendar"] = pim
        out["sms"] = "gammu" if _usable(states, "gammu") else None
        out["files"] = next((r for r in ("mass-storage", "mtp") if _usable(states, r)), None)
    else:
        route = pref
        for job in SERVES[route]:
            out[job] = route if _usable(states, route) else None
    out["cannot"] = [job for job in JOBS if job not in SERVES.get(pref, JOBS)] if pref != "auto" else []
    # Gammu dead end: every Gammu attempt is over, it never became verified, and USB OBEX works.
    out["dead_end"] = bool(gammu_tried and states.get("gammu") != "verified" and _usable(states, "pccs"))
    out["deferred"] = False
    return out


def recommended_for(selection: dict) -> dict[str, list[str]]:
    """route id -> jobs it is recommended for, from a ``choose`` result."""
    result: dict[str, list[str]] = {}
    for job in JOBS:
        route = selection.get(job)
        if route:
            result.setdefault(route, []).append(job)
    return result
