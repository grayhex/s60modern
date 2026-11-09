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
import time
from urllib.parse import urlsplit

from . import VERSION
from . import routes as route_model

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


def com_state(port: str, silent: bool = False) -> str:
    """'free' | 'busy' | 'unavailable' | 'silent'. Opens the port without writing a byte, then closes it.

    `silent` is what the Bridge learned from a failed AT identify: the port opens and nobody holds it, but the phone does
    not answer on it. Opening alone cannot tell that from a healthy port."""
    try:
        import serial
    except ImportError:
        return "unavailable"
    try:
        serial.Serial(port, timeout=0.2).close()
        return "silent" if silent else "free"
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
    "mtp-files": (
        INFO,
        (
            "The phone's files are available through Windows (MTP, read-only). Contacts, calendar and SMS are not.",
            "Файлы телефона доступны через Windows (MTP, только чтение). Контакты, календарь и SMS — нет.",
        ),
        (
            "Open the file manager in SymPort. For contacts, calendar and SMS the Nokia PC Suite / Nokia Connectivity Cable "
            "driver has to provide a COM port (install it yourself from Nokia/Microsoft, choose “PC Suite”, replug). "
            "For changing files choose “Mass storage” on the phone (writes are not available over MTP).",
            "Откройте файловый менеджер в SymPort. Для контактов, календаря и SMS COM-порт должен дать драйвер Nokia PC Suite / "
            "Nokia Connectivity Cable (ставится вручную у Nokia/Microsoft; выберите «PC Suite» и переподключите кабель). "
            "Для изменения файлов выберите на телефоне «Mass storage» (запись по MTP не поддерживается).",
        ),
    ),
    "mtp-only": (
        WARN,
        (
            "Windows sees the phone as a media device (MTP / “Portable device”), but no storage can be read yet; no COM port, no drive letter.",
            "Windows видит телефон как медиа-устройство (MTP / «Переносное устройство»), но хранилище пока не читается; COM-порта и буквы диска нет.",
        ),
        (
            "Unlock the phone and press “Rescan”: a locked phone or the wrong USB mode shows no storage. For files choose “Mass "
            "storage” on the phone (the memory card then gets a drive letter). For contacts, calendar and SMS the Nokia PC Suite / "
            "Nokia Connectivity Cable driver has to provide a COM port: install it yourself (from Nokia/Microsoft), choose "
            "“PC Suite” and replug the cable. If the phone says “PC Suite” but Windows shows only a portable device, that driver "
            "is most likely missing.",
            "Разблокируйте телефон и нажмите «Повторить поиск»: у заблокированного телефона или при неверном режиме USB хранилища "
            "нет. Для файлов выберите на телефоне «Mass storage» (карта получит букву диска). Для контактов, календаря и SMS "
            "COM-порт должен дать драйвер Nokia PC Suite / Nokia Connectivity Cable: установите его сами (у Nokia/Microsoft), "
            "выберите «PC Suite» и переподключите кабель. Если на телефоне выбран «PC Suite», а Windows показывает только "
            "переносное устройство, скорее всего этого драйвера нет.",
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
    "com-silent": (
        WARN,
        (
            "The Nokia COM port opens and is free, but the phone does not answer on it.",
            "COM-порт Nokia открывается и свободен, но телефон на нём не отвечает.",
        ),
        (
            "Switch the phone off and on, reconnect the cable, and choose “PC Suite” only after the phone has finished starting. "
            "Try another cable or USB port. A phone that stays silent may not accept AT commands on this port at all.",
            "Выключите и включите телефон, переподключите кабель и выберите «PC Suite» только после полной загрузки телефона. "
            "Попробуйте другой кабель или порт USB. Телефон, который молчит, может вообще не принимать AT-команды на этом порту.",
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


def at_ports(host: dict) -> list[dict]:
    """The Nokia COM ports an AT identify makes sense on: the “Java comm” port is not one."""
    return [p for p in host.get("serial") or [] if p.get("kind") != "javacomm"]


def classify_phone(host: dict, com: dict[str, str], identified: int) -> str:
    usb = host.get("usb") or []
    serial = at_ports(host)
    volumes = host.get("volumes") or []
    bluetooth = host.get("bluetooth") or []
    if identified > 0:
        return "ready"
    if any(state == "busy" for state in com.values()):
        return "com-busy"
    if any(state == "silent" for state in com.values()):
        return "com-silent"
    if serial:
        return "gammu-not-identified"
    if volumes and not serial:
        return "mass-storage-only"
    if usb and any(row.get("status") == "problem" for row in usb):
        return "usb-driver-problem"
    if any(p.get("storages") for p in host.get("portable") or []):
        return "mtp-files"
    # A Nokia Portable Device without readable storage: Windows 11 lists it under USB\MS_COMP_MTP and may show no WPD-class USB
    # row, so the portable-device record counts too (locked phone or wrong mode: connected, nothing to read yet).
    if host.get("portable"):
        return "mtp-only"
    if usb and any(str(row.get("class")).upper() == "WPD" for row in usb) and not any(
        str(row.get("class")).lower() in ("ports", "modem") for row in usb
    ):
        return "mtp-only"
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


def gammu_state(host: dict, com: dict[str, str], identified: int, dku2: str) -> str:
    if identified > 0:
        return "verified"
    if "busy" in com.values():
        return "busy"
    if "silent" in com.values() or at_ports(host) or dku2 == "no-response":
        return "silent"
    return "unavailable"


def route_states(host: dict, com: dict[str, str], identified: int, dku2: str, pccs: dict | None) -> dict[str, str]:
    """One state per route (see ``routes.STATES``). None of them implies another."""
    usb = host.get("usb") or []
    portable = host.get("portable") or []
    if sum(len(p.get("storages") or []) for p in portable):
        mtp = "available"
    elif portable or any(str(r.get("class")).upper() == "WPD" for r in usb):
        mtp = "silent"
    else:
        mtp = "unavailable"
    return {
        "gammu": gammu_state(host, com, identified, dku2),
        "pccs": (pccs or {}).get("state") if (pccs or {}).get("state") in route_model.STATES else "unavailable",
        "mtp": mtp,
        "mass-storage": "available" if host.get("volumes") else "unavailable",
    }


STATUS_OF = {"verified": OK, "available": OK, "silent": WARN, "busy": FAIL, "unavailable": INFO}


def routes(host: dict, com: dict[str, str], identified: int, dku2: str, *, gammu_error: str = "", pccs: dict | None = None,
           selection: dict | None = None) -> list[dict]:
    """The four independent ways to a Nokia (Gammu, USB OBEX, MTP, Mass storage), each with its own state."""
    usb = host.get("usb") or []
    portable = host.get("portable") or []
    storages = sum(len(p.get("storages") or []) for p in portable)
    wpd_class = any(str(r.get("class")).upper() == "WPD" for r in usb)
    if storages:
        mtp = (("Files over Windows Portable Devices (MTP): available, read-only",
                "Файлы через Windows Portable Devices (MTP): доступны, только чтение"), None)
    elif portable or wpd_class:
        mtp = (("Windows shows a portable device but no readable storage",
                "Windows показывает переносное устройство, но хранилище недоступно"),
               ("Unlock the phone and choose “Media transfer” or “PC Suite”, then press “Rescan”.",
                "Разблокируйте телефон, выберите «Передача медиа» или «PC Suite» и нажмите «Повторить поиск»."))
    else:
        mtp = (("Files over MTP: no portable device", "Файлы через MTP: переносного устройства нет"), None)
    if host.get("volumes"):
        mass = (("Mass storage (drive letter): available", "Mass storage (буква диска): доступен"), None)
    else:
        mass = (("Mass storage (drive letter): not active", "Mass storage (буква диска): не активен"),
                ("Choose “Mass storage” on the phone for a drive letter (files and backups only).",
                 "Выберите на телефоне «Mass storage», чтобы получить букву диска (только файлы и копии)."))
    if identified > 0:
        pc = (("PC Suite / contacts and calendar: the phone answered", "PC Suite / контакты и календарь: телефон ответил"), None)
    elif "busy" in com.values():
        pc = (("PC Suite: the COM port is busy", "PC Suite: COM-порт занят"),
              ("Close Nokia Suite / Ovi Suite and terminal programs, then rescan.",
               "Закройте Nokia Suite / Ovi Suite и терминальные программы, затем повторите поиск."))
    elif "silent" in com.values():
        pc = (("PC Suite: the COM port opens but the phone is silent", "PC Suite: COM-порт открывается, но телефон молчит"),
              ("Switch the phone off and on, reconnect the cable and choose “PC Suite” after it has started; try another cable or "
               "port, then rescan." + (" The Nokia-specific DKU-2 connection did not answer either." if dku2 == "no-response" else ""),
               "Выключите и включите телефон, переподключите кабель, выберите «PC Suite» после загрузки; попробуйте другой кабель "
               "или порт и повторите поиск." + (" Nokia-специфичное подключение DKU-2 тоже не ответило." if dku2 == "no-response" else "")))
    elif at_ports(host):
        pc = (("PC Suite: a COM port exists but the phone did not answer", "PC Suite: COM-порт есть, но телефон не ответил"),
              ("Unlock the phone, keep “PC Suite” mode and rescan.", "Разблокируйте телефон, оставьте «PC Suite» и повторите поиск."))
    elif dku2 == "no-response":
        pc = (("PC Suite: no COM port, and the Nokia-specific Gammu connection (DKU-2) did not answer",
               "PC Suite: COM-порта нет, и Nokia-специфичное подключение Gammu (DKU-2) не ответило"),
              ("The Nokia Connectivity Cable driver is most likely missing or the phone is not in “PC Suite” mode. Install the "
               "driver yourself from Nokia/Microsoft, choose “PC Suite”, replug and rescan. Whether a given phone answers over "
               "DKU-2 is not guaranteed.",
               "Скорее всего, нет драйвера Nokia Connectivity Cable или телефон не в режиме «PC Suite». Поставьте драйвер сами "
               "(у Nokia/Microsoft), выберите «PC Suite», переподключите кабель и повторите поиск. Ответ телефона по DKU-2 "
               "не гарантирован."))
    else:
        pc = (("PC Suite / contacts and calendar: not available", "PC Suite / контакты и календарь: недоступны"),
              ("Needs the Nokia Connectivity Cable driver and “PC Suite” mode.",
               "Нужен драйвер Nokia Connectivity Cable и режим «PC Suite»."))
    states = route_states(host, com, identified, dku2, pccs)
    # The real Gammu error code (from a fixed list), with a hint, is added to every Gammu route that did not answer.
    hint = route_model.gammu_hint(gammu_error, dku2=dku2 == "no-response" and not at_ports(host)) if states["gammu"] != "verified" else None
    if hint:
        action = pc[1] or ("", "")
        pc = (pc[0], ((action[0] + " " if action[0] else "") + f"[{gammu_error}] " + hint[0],
                      (action[1] + " " if action[1] else "") + f"[{gammu_error}] " + hint[1]))
    pstate = states["pccs"]
    if pstate in route_model.USABLE:
        pccs_row = (("USB OBEX (PCCS): contacts and calendar are readable, read-only",
                     "USB OBEX (PCCS): контакты и календарь доступны, только чтение"), None)
    elif pstate == "busy":
        pccs_row = (("USB OBEX: the channel is held by another program", "USB OBEX: канал занят другой программой"),
                    ("Close Nokia PC Suite and rescan.", "Закройте Nokia PC Suite и повторите поиск."))
    elif pstate == "silent":
        pccs_row = (("USB OBEX: the Nokia interface is there, but the phone does not answer",
                     "USB OBEX: интерфейс Nokia есть, но телефон не отвечает"),
                    ("Unlock the phone, choose “PC Suite” and rescan. Right after PC Suite was closed the first connection can "
                     "fail; the Bridge retries.",
                     "Разблокируйте телефон, выберите «PC Suite» и повторите поиск. Сразу после закрытия PC Suite первое "
                     "подключение может не пройти; Bridge повторяет попытку."))
    else:
        pccs_row = (("USB OBEX: no Nokia USB OBEX interface", "USB OBEX: интерфейса Nokia USB OBEX нет"),
                    ("Needs the Nokia driver (nmwcd) and “PC Suite” mode.", "Нужен драйвер Nokia (nmwcd) и режим «PC Suite»."))
    if selection and selection.get("dead_end"):
        pc = (pc[0], ((pc[1][0] + " " if pc[1] else "") + "Contacts and calendar are available over USB OBEX.",
                      (pc[1][1] + " " if pc[1] else "") + "Контакты и календарь доступны по USB OBEX."))
    recommended = route_model.recommended_for(selection or {})
    rows = {"gammu": pc, "pccs": pccs_row, "mtp": mtp, "mass-storage": mass}
    return [
        {
            "id": identifier,
            "state": states[identifier],
            "status": STATUS_OF[states[identifier]],
            "summary": list(rows[identifier][0]),
            "action": list(rows[identifier][1]) if rows[identifier][1] else None,
            "capabilities": list(route_model.SERVES[identifier]),
            "read_only": identifier in route_model.READ_ONLY,
            "recommended_for": recommended.get(identifier, []),
        }
        for identifier in route_model.ROUTE_IDS
    ]


def diagnose(host: dict, com: dict[str, str], identified: int, relay: dict | None = None, origin: str | None = None,
             parts: list[dict] | None = None, dku2: str = "not-tried", *, gammu_error: str = "", pccs: dict | None = None,
             preference: str = "auto", gammu_tried: bool = False, selection: dict | None = None,
             scan_at: float | None = None) -> dict:
    """The complete report. `host` is symport.bridge.discovery.host_candidates(); `relay` is OutboundRelay.public_state()."""
    usb = host.get("usb") or []
    problems = [row for row in usb if row.get("status") == "problem"]
    serial = at_ports(host)
    java = len(host.get("serial") or []) - len(serial)  # “Java comm” ports: listed, never probed
    volumes = host.get("volumes") or []
    bluetooth = host.get("bluetooth") or []
    parts = components() if parts is None else parts
    classes = ", ".join(sorted({str(row.get("class") or "?") for row in usb}))
    phone_code = classify_phone(host, com, identified)
    link_code = classify_link(relay)
    states = route_states(host, com, identified, dku2, pccs)
    # The Agent passes its own (applied) selection; a bare report computes what the preference would choose now.
    selection = selection or route_model.choose(preference, states, gammu_tried=gammu_tried)
    checks = [
        _check("usb", OK if usb else FAIL,
               (f"Nokia on USB: {len(usb)} ({classes})" if usb else "No Nokia on USB",
                f"Nokia по USB: {len(usb)} ({classes})" if usb else "Nokia по USB нет")),
        _check("driver", FAIL if problems else (OK if usb else INFO),
               ("USB driver reports a problem" if problems else "USB driver OK" if usb else "USB driver: nothing to check",
                "Драйвер USB сообщает о проблеме" if problems else "Драйвер USB в порядке" if usb else "Драйвер USB: проверять нечего")),
        _check("com", OK if serial else INFO,
               (f"Nokia COM ports: {len(serial)}" + (f" (+{java} Java comm, not used)" if java else "") if serial
                else "No Nokia COM port" + (f" ({java} Java comm port, not an AT port)" if java else ""),
                f"COM-портов Nokia: {len(serial)}" + (f" (+{java} Java comm, не используется)" if java else "") if serial
                else "COM-порта Nokia нет" + (f" ({java} порт Java comm — не AT-порт)" if java else ""))),
        _check("com-busy", FAIL if "busy" in com.values() else WARN if "silent" in com.values() else (OK if com else INFO),
               ("COM port is busy" if "busy" in com.values()
                else "COM port opens but is silent (no AT answer)" if "silent" in com.values()
                else "COM port is free" if com else "COM port: nothing to check",
                "COM-порт занят" if "busy" in com.values()
                else "COM-порт открывается, но молчит (нет ответа на AT)" if "silent" in com.values()
                else "COM-порт свободен" if com else "COM-порт: проверять нечего")),
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
    if phone_code == "com-silent" and action:
        note = {
            "no-response": ("The Nokia-specific DKU-2 connection did not answer either.",
                            "Nokia-специфичное подключение DKU-2 тоже не ответило."),
            "not-tried": ("The Nokia-specific DKU-2 connection has not been tried yet; it runs after “Rescan”.",
                          "Nokia-специфичное подключение DKU-2 ещё не пробовалось; оно запускается после «Повторить поиск»."),
        }.get(dku2.split(":")[0])
        if note:
            action = (action[0] + " " + note[0], action[1] + " " + note[1])
    codes = sorted({row.get("problem_code") for row in problems if row.get("problem_code")})
    if phone_code == "usb-driver-problem" and codes and action:
        hint = {28: ("Windows reports error 28: no driver is installed for this device. ",
                     "Windows сообщает ошибку 28: для устройства не установлен драйвер. ")}
        extra = hint.get(codes[0], (f"Device Manager error code {codes[0]}. ", f"Код ошибки диспетчера устройств: {codes[0]}. "))
        action = (extra[0] + action[0], extra[1] + action[1])
    link_status, link_summary, link_action = LINK[link_code]
    return {
        "schema": "symport.bridge-doctor/1",
        "bridge_version": VERSION,
        "os": f"{platform.system()} {platform.release()}"[:60],
        "frozen": bool(getattr(sys, "frozen", False)),
        "phone": {"code": phone_code, "status": status, "summary": list(summary), "action": list(action) if action else None},
        "checks": checks,
        "routes": routes(host, com, identified, dku2, gammu_error=gammu_error, pccs=pccs, selection=selection),
        "preference": selection["preference"],
        "selection": {job: selection.get(job) for job in route_model.JOBS},
        "dead_end": bool(selection.get("dead_end")),
        "gammu_error": gammu_error if gammu_error in route_model.GAMMU_CODES else "",
        "scan_at": scan_at,
        "dku2": dku2.split(":")[0],
        "usb": [{k: row.get(k) for k in ("vid", "pid", "class", "status", "mi", "problem_code")} for row in usb],
        "com": [{"state": state} for state in com.values()],
        "link": {
            "code": link_code,
            "status": link_status,
            "summary": list(link_summary),
            "action": list(link_action) if link_action else None,
            "pinned": bool(relay and relay.get("pinned")),
            "origin": origin if origin and link_code != "not-paired" else None,
            "expires_in": relay.get("expires_in") if relay and link_code == "connected" else None,
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
    lines += ["", f"Routes (preference: {report.get('preference', 'auto')}):"]
    lines += [f"  [{r['status']}] {r['id']} ({r.get('state', '?')}): {r['summary'][0]}"
              + (f" · used for: {', '.join(r['recommended_for'])}" if r.get("recommended_for") else "")
              for r in report.get("routes", [])]
    if report.get("gammu_error"):
        lines.append(f"  Gammu error code: {report['gammu_error']}")
    lines += ["", "Nokia USB devices (VID:PID class status):"]
    lines += [
        f"  {u['vid']}:{u['pid']} {u['class']} {u['status']}"
        + (f" interface {u['mi']}" if u.get("mi") else "")
        + (f" Device Manager code {u['problem_code']}" if u.get("problem_code") else "")
        for u in report["usb"]
    ] or ["  none"]
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


ROUTE_NAMES = {
    "gammu": ("Gammu (PC Suite)", "Gammu (PC Suite)"),
    "pccs": ("USB OBEX", "USB OBEX"),
    "mtp": ("MTP (files)", "MTP (файлы)"),
    "mass-storage": ("Mass storage", "Mass storage"),
}
JOB_NAMES = {"contacts": ("contacts", "контакты"), "calendar": ("calendar", "календарь"), "sms": ("SMS", "SMS"), "files": ("files", "файлы")}
STATE_NAMES = {
    "unavailable": ("not available", "недоступен"),
    "busy": ("busy", "занят"),
    "silent": ("found, the phone is silent", "найден, телефон молчит"),
    "available": ("available", "доступен"),
    "verified": ("verified", "проверен"),
}
# Route id -> the transport_preference that uses only that route (Mass storage has none: it serves files in `auto`).
USE_VALUE = {"gammu": "gammu", "pccs": "pccs", "mtp": "mtp"}


def jobs_text(selection: dict, russian: bool) -> str:
    """“contacts and calendar (USB OBEX), files (Mass storage)” from a report's selection."""
    pick = 1 if russian else 0
    groups: dict[str, list[str]] = {}
    for job in route_model.JOBS:
        route = selection.get(job)
        if route:
            groups.setdefault(route, []).append(JOB_NAMES[job][pick])
    joiner = " и " if russian else " and "

    def names(jobs: list[str]) -> str:
        return jobs[0] if len(jobs) == 1 else ", ".join(jobs[:-1]) + joiner + jobs[-1]

    return ", ".join(f"{names(jobs)} ({ROUTE_NAMES[route][pick]})" for route, jobs in groups.items())


def plain_sentence(report: dict, russian: bool) -> str:
    """One plain-language sentence on top of the Connections window."""
    selection = report.get("selection") or {}
    states = {r["id"]: r.get("state") for r in report.get("routes", [])}
    jobs = jobs_text(selection, russian)
    if jobs:
        return f"Телефон найден: {jobs}." if russian else f"Phone found: {jobs}."
    if "busy" in states.values():
        return ("Телефон найден, но канал занят другой программой (например, Nokia PC Suite). Закройте её и повторите поиск."
                if russian else "The phone is there, but another program (for example Nokia PC Suite) holds the channel. Close it and search again.")
    if "silent" in states.values() or report.get("usb"):
        return ("Телефон найден, но не отвечает. Разблокируйте его, выберите режим PC Suite и повторите поиск."
                if russian else "The phone is there but does not answer. Unlock it, choose PC Suite mode and search again.")
    return ("Телефон по USB не найден. Проверьте кабель (не только для зарядки), разблокируйте телефон и повторите поиск."
            if russian else "No phone found on USB. Check the cable (not charge-only), unlock the phone and search again.")


def duration(seconds: int, russian: bool) -> str:
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes = rest // 60
    if russian:
        return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def window_model(report: dict, russian: bool) -> dict:
    """What the Connections window shows, as plain data (so it can be tested without a GUI)."""
    pick = 1 if russian else 0
    link = report["link"]
    phone = report["phone"]
    selection = report.get("selection") or {}
    serving = {route for route in selection.values() if route}
    preference = report.get("preference", "auto")
    left = link.get("expires_in")
    scan_at = report.get("scan_at")
    return {
        "title": ("SymPort Bridge — подключения" if russian else "SymPort Bridge — connections") + " " + report["bridge_version"],
        "sentence": plain_sentence(report, russian),
        "phone_mark": MARKS[phone["status"]],
        "phone_summary": phone["summary"][pick],
        "phone_action": phone["action"][pick] if phone["action"] else "",
        "link_mark": MARKS[link["status"]],
        "link_summary": link["summary"][pick]
        + ((" (сертификат закреплён)" if russian else " (certificate pinned)") if link["code"] == "connected" and link["pinned"] else ""),
        "link_action": link["action"][pick] if link["action"] else "",
        "link_origin": link.get("origin"),
        "link_connected": link["code"] == "connected",
        "lease": (("Доступ SymPort: осталось " if russian else "SymPort access: ") + duration(left, russian) + ("" if russian else " left"))
        if isinstance(left, int)
        else "",
        "scan": (time.strftime("%H:%M:%S", time.localtime(scan_at)) if isinstance(scan_at, (int, float)) else ""),
        "preference": preference,
        "route_rows": [
            {
                "id": r["id"],
                "mark": MARKS[r["status"]],
                "name": ROUTE_NAMES[r["id"]][pick],
                "state": STATE_NAMES.get(r.get("state"), ("?", "?"))[pick],
                "detail": r["summary"][pick],
                "used": r["id"] in serving,
                "use": USE_VALUE.get(r["id"]),
                "chosen": USE_VALUE.get(r["id"]) == preference,
                "usable": r.get("state") in route_model.USABLE,
            }
            for r in report.get("routes", [])
        ],
        "rows": [(MARKS[r["status"]], r["summary"][pick]) for r in report.get("routes", [])]
        + [("", "")]
        + [(MARKS[c["status"]], c["summary"][pick]) for c in report["checks"]]
        + [_component_row(p, russian) for p in report["components"]],
    }
