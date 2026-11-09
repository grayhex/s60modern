"""What the tray shows: icon state, tooltip and notifications, derived from the Doctor report (pure; no phone data).

The report holds no IMEI, serial number, path or address; the only phone-related text added here is the model name
Windows-side discovery already shows in the tray menu (e.g. “Nokia E72”), passed in by the caller.
"""

from __future__ import annotations

from . import doctor
from . import routes as route_model

NONE, SILENT, READY, LINK_ERROR = "no-phone", "silent", "ready", "link-error"
COLORS = {NONE: (128, 128, 128), SILENT: (240, 160, 0), READY: (0, 160, 60), LINK_ERROR: (200, 30, 30)}
LINK_FINE = ("connected", "connecting", "not-paired")
LEASE_WARNING = 600  # seconds before the SymPort access ends


def tray_state(report: dict) -> str:
    """link-error > ready > silent > no-phone. A paired link that fails is shown even while a phone works."""
    link = report.get("link", {})
    if link.get("code") not in LINK_FINE:
        return LINK_ERROR
    states = {r["id"]: r.get("state") for r in report.get("routes", [])}
    if any(state in route_model.USABLE for state in states.values()):
        return READY
    if report.get("usb") or any(state in ("silent", "busy") for state in states.values()):
        return SILENT
    return NONE


def tooltip(report: dict, label: str | None, russian: bool) -> str:
    """“Nokia E72 · contacts and calendar (USB OBEX) · SymPort connected” (Windows shows at most 127 characters)."""
    state = tray_state(report)
    jobs = doctor.jobs_text(report.get("selection") or {}, russian)
    link = report.get("link", {})
    if link.get("code") == "connected":
        link_text = "SymPort подключён" if russian else "SymPort connected"
    elif link.get("code") in LINK_FINE:
        link_text = "SymPort не подключён" if russian else "SymPort not connected"
    else:
        link_text = "SymPort: ошибка связи" if russian else "SymPort: link error"
    if state == NONE:
        phone = "Телефон не найден" if russian else "No phone found"
    elif state == SILENT:
        phone = (label or "Nokia") + (" · не отвечает" if russian else " · not answering")
    else:
        phone = label or "Nokia"
    parts = [phone, jobs, link_text] if state == READY and jobs else [phone, link_text]
    return " · ".join(p for p in parts if p)[:127]


def badge(image, state: str):
    """The application icon with a small coloured dot for the state (needs Pillow)."""
    from PIL import ImageDraw

    icon = image.convert("RGBA").copy()
    width, height = icon.size
    radius = max(4, width // 5)
    centre = (width - radius - 1, height - radius - 1)
    draw = ImageDraw.Draw(icon)
    draw.ellipse(
        [centre[0] - radius - 2, centre[1] - radius - 2, centre[0] + radius + 2, centre[1] + radius + 2], fill=(255, 255, 255, 255)
    )
    draw.ellipse([centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius], fill=(*COLORS[state], 255))
    return icon


class Notifier:
    """Turns two consecutive reports into toasts: connect, disconnect, route change, SymPort link, access ending.

    The first report is only remembered (starting the Bridge is not an event). Nothing is stored on disk: after a restart
    the next report is a new baseline."""

    def __init__(self, russian: bool):
        self.russian = russian
        self.previous: dict | None = None
        self.warned = False

    def update(self, report: dict, label: str | None = None) -> list[str]:
        ru = self.russian
        now = {
            "state": tray_state(report),
            "selection": {job: (report.get("selection") or {}).get(job) for job in route_model.JOBS},
            "link": report.get("link", {}).get("code"),
        }
        toasts: list[str] = []
        before, self.previous = self.previous, now
        if before is not None:
            was, is_ = before["state"] == READY, now["state"] == READY
            jobs = doctor.jobs_text(now["selection"], ru)
            if is_ and not was:
                toasts.append(((label or "Nokia") + " подключён" if ru else (label or "Nokia") + " connected") + (f": {jobs}" if jobs else ""))
            elif was and not is_:
                toasts.append("Телефон отключён" if ru else "Phone disconnected")
            elif is_ and was and before["selection"] != now["selection"] and jobs:
                toasts.append(f"Маршрут изменён: {jobs}" if ru else f"Route changed: {jobs}")
            if before["link"] != now["link"]:
                if now["link"] == "connected":
                    toasts.append("SymPort подключён" if ru else "SymPort connected")
                elif before["link"] == "connected":
                    toasts.append("Связь с SymPort потеряна" if ru else "SymPort link lost")
        left = report.get("link", {}).get("expires_in")
        if isinstance(left, int):
            if left > LEASE_WARNING:
                self.warned = False  # a new access period
            elif not self.warned and report.get("link", {}).get("code") == "connected":
                self.warned = True
                toasts.append(
                    "Доступ SymPort закончится через 10 минут. Откройте «Подключения» → «Переподключиться к SymPort»."
                    if ru
                    else "SymPort access ends in 10 minutes. Open Connections → “Reconnect to SymPort”."
                )
        return toasts
