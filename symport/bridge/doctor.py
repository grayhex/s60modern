"""Driver & Connection Doctor: turns raw host observations into separate, actionable statuses.

Pure logic (no GUI, no network): the tray window, the tests and the exported text all use `diagnose()`.
A Nokia that Windows enumerates over USB is a *candidate*; "ready" only follows a real Gammu answer.
Nothing here installs a driver, changes Windows settings or sends data anywhere.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
from urllib.parse import urlsplit

from . import VERSION

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
Text = tuple[str, str]  # (English, Russian)

# What the frozen exe has to contain for ordinary work (checked by importability, never by "Python is installed").
REQUIRED_COMPONENTS = (
    ("pyserial", "serial", "COM port enumeration"),
    ("Pillow", "PIL", "tray icon"),
    ("pystray", "pystray", "tray icon"),
    ("Tk", "tkinter", "settings and diagnostics windows"),
)
OPTIONAL_COMPONENTS = (
    ("python-gammu", "gammu", "talking to the phone over a COM port (PC Suite mode)"),
    ("websockets", "websockets", "optional status events"),
)


def components() -> list[dict]:
    rows = []
    for required, group in ((True, REQUIRED_COMPONENTS), (False, OPTIONAL_COMPONENTS)):
        for name, module, purpose in group:
            try:
                present = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                present = False
            rows.append({"name": name, "present": present, "required": required, "purpose": purpose})
    return rows


def com_state(port: str) -> str:
    """'free' | 'busy' | 'unavailable'. Opens the port without writing a byte, then closes it."""
    try:
        import serial
    except ImportError:
        return "unavailable"
    try:
        serial.Serial(port, timeout=0.2).close()
        return "free"
    except serial.SerialException as exc:
        text = str(exc).casefold()
        return "busy" if "access is denied" in text or "permission" in text or "busy" in text else "unavailable"
    except (OSError, ValueError):
        return "unavailable"


def _check(identifier: str, status: str, summary: Text, action: Text | None = None) -> dict:
    return {"id": identifier, "status": status, "summary": list(summary), "action": list(action) if action else None}


PHONE: dict[str, tuple[str, Text, Text | None]] = {
    "ready": (OK, ("A Nokia answered over its COM port.", "Nokia ответила через COM-порт."), None),
    "no-usb": (
        FAIL,
        ("Windows sees no Nokia on USB.", "Windows не видит Nokia по USB."),
        (
            "Use a data cable (a charge-only cable shows nothing), unlock the phone, choose “PC Suite” (or “Mass storage”) "
            "when it asks for the USB mode, try another USB port and close Nokia Suite.",
            "Используйте кабель для данных (зарядный ничего не покажет), разблокируйте телефон, на запрос режима USB выберите "
            "«PC Suite» (или «Накопитель»), попробуйте другой порт USB и закройте Nokia Suite.",
        ),
    ),
    "usb-driver-problem": (
        FAIL,
        (
            "A Nokia is on USB, but Windows reports a driver problem.",
            "Nokia найдена по USB, но Windows сообщает о проблеме драйвера.",
        ),
        (
            "Open Device Manager (button below) and look for the Nokia entry with a warning sign. The Nokia PC Suite / Nokia "
            "Connectivity driver is needed; take it only from Nokia/Microsoft sources and install it yourself. SymPort never "
            "installs drivers silently or downloads driver packs. File access may still work in “Mass storage” mode.",
            "Откройте «Диспетчер устройств» (кнопка ниже) и найдите запись Nokia с предупреждающим значком. Нужен драйвер Nokia "
            "PC Suite / Nokia Connectivity: берите его только у Nokia/Microsoft и ставьте сами. SymPort не ставит драйверы "
            "скрыто и не скачивает «драйверпаки». Работа с файлами может быть доступна в режиме «Накопитель».",
        ),
    ),
    "no-com": (
        WARN,
        (
            "A Nokia is on USB with a working driver, but there is no COM port.",
            "Nokia найдена по USB, драйвер работает, но COM-порта нет.",
        ),
        (
            "Choose “PC Suite” on the phone (not “Mass storage” or “Media transfer”) and reconnect the cable. "
            "Then press “Rescan”.",
            "Выберите на телефоне режим «PC Suite» (не «Накопитель» и не «Передача медиа») и переподключите кабель. "
            "Затем нажмите «Повторить поиск».",
        ),
    ),
    "com-busy": (
        FAIL,
        ("The Nokia COM port is busy.", "COM-порт Nokia занят."),
        (
            "Close Nokia Suite / PC Suite, Ovi Suite and any terminal program that holds the port, then press “Rescan”.",
            "Закройте Nokia Suite / PC Suite, Ovi Suite и программы-терминалы, занявшие порт, затем нажмите «Повторить поиск».",
        ),
    ),
    "gammu-not-identified": (
        WARN,
        (
            "A free Nokia COM port exists, but the phone did not identify itself.",
            "Свободный COM-порт Nokia есть, но телефон не представился.",
        ),
        (
            "Unlock the phone, keep “PC Suite” mode selected and press “Rescan”. If it stays this way, the model may need a "
            "specific connection profile (Settings → Gammu config). Reading identity alone does not prove contacts or files "
            "will work.",
            "Разблокируйте телефон, оставьте режим «PC Suite» и нажмите «Повторить поиск». Если не меняется, модели может "
            "понадобиться свой профиль подключения (Настройки → Gammu config). Чтение идентификатора само по себе не "
            "доказывает работу контактов и файлов.",
        ),
    ),
    "mass-storage-only": (
        INFO,
        (
            "Only the phone's memory card/drive is available (Mass storage).",
            "Доступен только накопитель телефона (режим Mass storage).",
        ),
        (
            "Files and backups work in this mode. Contacts, calendar and SMS need “PC Suite” mode with a COM port.",
            "Файлы и копии работают в этом режиме. Контактам, календарю и SMS нужен режим «PC Suite» с COM-портом.",
        ),
    ),
    "bluetooth-only": (
        INFO,
        (
            "A paired Nokia is known over Bluetooth, but nothing is attached over USB.",
            "Есть сопряжённая по Bluetooth Nokia, но по USB ничего не подключено.",
        ),
        (
            "Connect the USB cable, or use a configured Bluetooth profile (Settings → connection profiles). Pair the phone "
            "in Windows first.",
            "Подключите USB-кабель или используйте настроенный профиль Bluetooth (Настройки → профили подключения). "
            "Сначала сопрягите телефон в Windows.",
        ),
    ),
}

LINK: dict[str, tuple[str, Text, Text | None]] = {
    "not-paired": (
        INFO,
        ("This Bridge is not paired with a SymPort server.", "Этот Bridge не сопряжён с сервером SymPort."),
        (
            "Open SymPort → Connection → “Request access” in your browser and approve the dialog here.",
            "Откройте SymPort → Подключение → «Запросить доступ» в браузере и подтвердите окно здесь.",
        ),
    ),
    "connecting": (INFO, ("Connecting to SymPort…", "Подключаемся к SymPort…"), None),
    "connected": (OK, ("Connected to SymPort.", "Подключено к SymPort."), None),
    "tls-untrusted": (
        FAIL,
        (
            "Paired, but this PC does not trust the SymPort certificate.",
            "Сопряжено, но этот ПК не доверяет сертификату SymPort.",
        ),
        (
            "Pair again from SymPort (“Request access”): the Bridge shows the certificate fingerprint and, once you approve, "
            "trusts only that certificate. Installing it into Windows is optional.",
            "Сопрягите заново из SymPort («Запросить доступ»): Bridge покажет отпечаток сертификата и после вашего "
            "подтверждения будет доверять только ему. Ставить его в Windows не обязательно.",
        ),
    ),
    "tls-hostname": (
        FAIL,
        (
            "The SymPort certificate was issued for a different address.",
            "Сертификат SymPort выпущен для другого адреса.",
        ),
        (
            "The NAS address changed or you opened SymPort by another name. Restart SymPort (it reissues the certificate for "
            "its current address) and pair again from the address shown in its log.",
            "Адрес NAS изменился или SymPort открыт по другому имени. Перезапустите SymPort (он выпустит сертификат на "
            "текущий адрес) и сопрягите заново по адресу из его журнала.",
        ),
    ),
    "tls-expired": (
        FAIL,
        ("The SymPort certificate has expired.", "Срок действия сертификата SymPort истёк."),
        (
            "Restart SymPort (it renews its certificate) and pair again. Check the date and time on this PC and the NAS.",
            "Перезапустите SymPort (он обновит сертификат) и сопрягите заново. Проверьте дату и время на ПК и NAS.",
        ),
    ),
    "tls-error": (
        FAIL,
        ("The secure handshake with SymPort failed.", "Не удалось установить защищённое соединение с SymPort."),
        (
            "Open SymPort with https:// (not http://) and pair again.",
            "Откройте SymPort по https:// (не http://) и сопрягите заново.",
        ),
    ),
    "unreachable": (
        FAIL,
        ("SymPort cannot be reached from this PC.", "SymPort недоступен с этого ПК."),
        (
            "Check that the NAS is on, you are in the same network, the address/port are right and the Windows firewall or "
            "a VPN is not blocking the connection.",
            "Проверьте, что NAS включён, вы в той же сети, адрес и порт верны, а брандмауэр Windows или VPN не блокируют "
            "соединение.",
        ),
    ),
    "lease-expired": (
        FAIL,
        ("The access lease has expired or was revoked.", "Срок доступа истёк или он был отозван."),
        (
            "Request access again from SymPort (access lasts two hours and ends when SymPort restarts).",
            "Запросите доступ заново из SymPort (доступ действует два часа и сбрасывается при перезапуске SymPort).",
        ),
    ),
    "lease-missing": (
        FAIL,
        ("SymPort has no access lease for this Bridge.", "У SymPort нет выданного доступа для этого Bridge."),
        (
            "SymPort was restarted or the lease was closed. Request access again.",
            "SymPort перезапущен или доступ закрыт. Запросите доступ заново.",
        ),
    ),
    "bad-token": (
        FAIL,
        ("SymPort rejected the access token.", "SymPort отклонил токен доступа."),
        (
            "A newer request replaced this one. Request access again once and approve only the latest dialog.",
            "Его заменил более новый запрос. Запросите доступ один раз и подтверждайте только последнее окно.",
        ),
    ),
    "rejected": (
        FAIL,
        ("SymPort refused the connection.", "SymPort отказал в соединении."),
        ("Request access again from SymPort.", "Запросите доступ заново из SymPort."),
    ),
    "server-error": (
        FAIL,
        ("SymPort answered with an error.", "SymPort ответил ошибкой."),
        ("Check the SymPort container log on the NAS.", "Посмотрите журнал контейнера SymPort на NAS."),
    ),
}


def classify_phone(host: dict, com: dict[str, str], identified: int) -> str:
    usb = host.get("usb") or []
    serial = host.get("serial") or []
    volumes = host.get("volumes") or []
    bluetooth = host.get("bluetooth") or []
    if identified > 0:
        return "ready"
    if any(state == "busy" for state in com.values()):
        return "com-busy"
    if serial:
        return "gammu-not-identified"
    if volumes and not serial:
        return "mass-storage-only"
    if usb and any(row.get("status") == "problem" for row in usb):
        return "usb-driver-problem"
    if usb:
        return "no-com"
    if bluetooth:
        return "bluetooth-only"
    return "no-usb"


def classify_link(relay: dict | None) -> str:
    if not relay:
        return "not-paired"
    state = relay.get("state")
    if state == "connected":
        return "connected"
    if state == "error":
        reason = relay.get("reason")
        return reason if reason in LINK else "rejected"
    return "connecting"


def diagnose(host: dict, com: dict[str, str], identified: int, relay: dict | None = None, origin: str | None = None,
             parts: list[dict] | None = None) -> dict:
    """The complete report. `host` is symport.bridge.discovery.host_candidates(); `relay` is OutboundRelay.public_state()."""
    usb = host.get("usb") or []
    problems = [row for row in usb if row.get("status") == "problem"]
    serial = host.get("serial") or []
    volumes = host.get("volumes") or []
    bluetooth = host.get("bluetooth") or []
    parts = components() if parts is None else parts
    phone_code = classify_phone(host, com, identified)
    link_code = classify_link(relay)
    checks = [
        _check("usb", OK if usb else FAIL,
               (f"Nokia on USB: {len(usb)}" if usb else "No Nokia on USB", f"Nokia по USB: {len(usb)}" if usb else "Nokia по USB нет")),
        _check("driver", FAIL if problems else (OK if usb else INFO),
               ("USB driver reports a problem" if problems else "USB driver OK" if usb else "USB driver: nothing to check",
                "Драйвер USB сообщает о проблеме" if problems else "Драйвер USB в порядке" if usb else "Драйвер USB: проверять нечего")),
        _check("com", OK if serial else INFO,
               (f"Nokia COM ports: {len(serial)}" if serial else "No Nokia COM port",
                f"COM-портов Nokia: {len(serial)}" if serial else "COM-порта Nokia нет")),
        _check("com-busy", FAIL if "busy" in com.values() else (OK if com else INFO),
               ("COM port is busy" if "busy" in com.values() else "COM port is free" if com else "COM port: nothing to check",
                "COM-порт занят" if "busy" in com.values() else "COM-порт свободен" if com else "COM-порт: проверять нечего")),
        _check("gammu", OK if identified > 0 else (WARN if serial else INFO),
               ("The phone identified itself" if identified > 0 else "The phone has not identified itself",
                "Телефон представился" if identified > 0 else "Телефон пока не представился")),
        _check("storage", OK if volumes else INFO,
               (f"Mass-storage drives: {len(volumes)}" if volumes else "No Nokia drive",
                f"Накопителей Nokia: {len(volumes)}" if volumes else "Накопителя Nokia нет")),
        _check("bluetooth", OK if bluetooth else INFO,
               (f"Paired Nokia over Bluetooth: {len(bluetooth)}" if bluetooth else "No paired Nokia over Bluetooth",
                f"Сопряжено по Bluetooth: {len(bluetooth)}" if bluetooth else "Сопряжённых по Bluetooth Nokia нет")),
    ]
    status, summary, action = PHONE[phone_code]
    link_status, link_summary, link_action = LINK[link_code]
    return {
        "schema": "symport.bridge-doctor/1",
        "bridge_version": VERSION,
        "os": f"{platform.system()} {platform.release()}"[:60],
        "frozen": bool(getattr(sys, "frozen", False)),
        "phone": {"code": phone_code, "status": status, "summary": list(summary), "action": list(action) if action else None},
        "checks": checks,
        "usb": [{k: row.get(k) for k in ("vid", "pid", "class", "status")} for row in usb],
        "com": [{"state": state} for state in com.values()],
        "link": {
            "code": link_code,
            "status": link_status,
            "summary": list(link_summary),
            "action": list(link_action) if link_action else None,
            "pinned": bool(relay and relay.get("pinned")),
            "origin": origin if origin and link_code != "not-paired" else None,
        },
        "components": parts,
        "notes": [r for r in host.get("diagnostics", [])][:6],
    }


def export_text(report: dict) -> str:
    """Support text without IMEI, serial numbers, PnP instance IDs, paths, addresses, keys or tokens.

    Only versions, VID:PID, statuses and counts. The NAS address is reduced to its URL scheme."""
    link = report["link"]
    scheme = urlsplit(link["origin"]).scheme if link.get("origin") else None
    lines = [
        f"SymPort Bridge {report['bridge_version']} ({'installed exe' if report['frozen'] else 'source'}) · {report['os']}",
        f"Phone: {report['phone']['code']} — {report['phone']['summary'][0]}",
        f"SymPort link: {link['code']} — {link['summary'][0]}"
        + (f" (scheme {scheme}, certificate {'pinned' if link['pinned'] else 'system store'})" if scheme else ""),
        "",
        "Checks:",
    ]
    lines += [f"  [{c['status']}] {c['id']}: {c['summary'][0]}" for c in report["checks"]]
    lines += ["", "Nokia USB devices (VID:PID class status):"]
    lines += [f"  {u['vid']}:{u['pid']} {u['class']} {u['status']}" for u in report["usb"]] or ["  none"]
    lines += ["", "Components:"]
    lines += [
        f"  [{'ok' if p['present'] else ('MISSING' if p['required'] else 'absent')}] {p['name']}"
        f"{'' if p['required'] else ' (optional)'}"
        for p in report["components"]
    ]
    if report.get("notes"):
        lines += ["", "Notes:"] + [f"  {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def windows_language_is_russian() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF) == 0x19
    except (AttributeError, OSError):
        return False


MARKS = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


def _component_row(part: dict, russian: bool) -> tuple[str, str]:
    present, required = part["present"], part["required"]
    mark = "✓" if present else ("✗" if required else "·")
    if russian:
        state = "в составе программы" if present else ("отсутствует" if required else "не входит (необязательно)")
    else:
        state = "included" if present else ("missing" if required else "not included (optional)")
    return mark, f"{part['name']} — {state}"


def window_model(report: dict, russian: bool) -> dict:
    """What the diagnostics window shows, as plain data (so it can be tested without a GUI)."""
    pick = 1 if russian else 0
    link = report["link"]
    phone = report["phone"]
    return {
        "title": ("SymPort Bridge — диагностика" if russian else "SymPort Bridge — diagnostics") + " " + report["bridge_version"],
        "phone_mark": MARKS[phone["status"]],
        "phone_summary": phone["summary"][pick],
        "phone_action": phone["action"][pick] if phone["action"] else "",
        "link_mark": MARKS[link["status"]],
        "link_summary": link["summary"][pick]
        + ((" (сертификат закреплён)" if russian else " (certificate pinned)") if link["code"] == "connected" and link["pinned"] else ""),
        "link_action": link["action"][pick] if link["action"] else "",
        "link_origin": link.get("origin"),
        "rows": [(MARKS[c["status"]], c["summary"][pick]) for c in report["checks"]]
        + [_component_row(p, russian) for p in report["components"]],
    }
