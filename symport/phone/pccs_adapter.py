"""Nokia PCCS over USB OBEX as a device-manager connection: read-only contacts and calendar.

The channel is opened per operation and closed at once; nothing stays open between requests. The probe behind
``device()`` is a real Connect + OpenStore + ResetChangeInfo + CloseStore, so ``contacts.read`` / ``calendar.read``
become *available* only after the phone answered for that store; they never rest on the mere presence of an
interface. Everything else is *unsupported* with a fixed reason. The phone's IMEI/serial is never read.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import time

from . import capabilities, nokia_pccs
from .nokia_pccs import PccsBusy, PccsError, PccsSession

SOURCE = "nokia-pccs"
# Seconds a probe result is reused, by state. A busy or silent channel is retried more slowly than a missing one.
TTL = {"unavailable": 3.0, "busy": 10.0, "silent": 20.0, "available": 60.0, "verified": 60.0}
MESSAGES = {
    "busy": "Канал USB OBEX занят другой программой. Закройте Nokia PC Suite и повторите.",
    "no-interface": "Windows не показывает интерфейс Nokia USB OBEX. Проверьте режим USB и драйвер Nokia.",
    "silent": "Телефон не ответил на USB OBEX. Разблокируйте экран, выберите режим PC Suite и повторите.",
    "not-pccs": "Ни один интерфейс Nokia USB не принял синхронизацию PC Suite.",
    "timeout": "Телефон не ответил вовремя. Повторите.",
    "refused": "Телефон отклонил запрос.",
    "store": "Телефон не открыл это хранилище.",
    "format": "Формат ответа телефона не распознан. Данные не показаны.",
    "closed": "Соединение с телефоном закрыто.",
}
UNSUPPORTED = "Маршрут USB OBEX читает только контакты и календарь; файлы, SMS и запись недоступны"
STATE_OF = {"busy": "busy", "no-interface": "unavailable", "silent": "silent", "not-pccs": "silent", "timeout": "silent"}
CAPS_REASON = {
    "files": "Not implemented for the USB OBEX route; use Mass storage or MTP for files",
    "sms": "SMS are not exposed by the PCCS sync route",
    "write": "The USB OBEX route is read-only: SymPort sends no CommitChanges, CreateItems or DeleteItems",
    "identity": "The route reads no IMEI or serial number; the model name comes from the OBEX capability object",
    "other": "Not implemented for the USB OBEX route",
}


class PccsAdapter:
    id = "nokia-pccs"

    def __init__(self, interfaces=None, opener=None, clock=time.monotonic, sleep=time.sleep, attempts: int = 3, pause: float = 4.0):
        if interfaces is None or opener is None:
            from . import pccs_windows

            interfaces = interfaces or pccs_windows.interface_paths
            opener = opener or pccs_windows.open_channel
        self.interfaces, self.opener, self.clock, self.sleep = interfaces, opener, clock, sleep
        self.attempts, self.pause = attempts, pause
        self.lock = threading.Lock()  # one conversation with the phone at a time
        self._at = -1e9
        self._state, self._reason = "unavailable", "no-interface"
        self._domains = {"contacts": False, "calendar": False}
        self._verified: set[str] = set()
        self._identity: dict = {}
        self._active = None
        self._closing = False

    # ---- connection ------------------------------------------------------------------------------------------------
    def _connect_once(self) -> tuple[PccsSession, object]:
        try:
            paths = self.interfaces()
        except (OSError, ValueError):
            paths = []
        if not paths:
            raise PccsError("no-interface")
        busy = opened = timed_out = False
        for path in paths:
            try:
                channel = self.opener(path)
            except PccsBusy:
                busy = True
                continue
            except (PccsError, OSError):
                continue
            opened = True
            session = PccsSession(channel)
            try:
                session.connect()
            except PccsError as exc:
                timed_out = timed_out or exc.code in ("timeout", "closed")
                channel.close()  # the SyncML or an empty interface: ignore it and try the next
                continue
            except (OSError, struct.error, ValueError):
                channel.close()
                continue
            self._active = channel
            return session, channel
        if busy and not opened:
            raise PccsBusy()
        raise PccsError("silent" if timed_out or not opened else "not-pccs")

    def _connect(self, attempts: int) -> tuple[PccsSession, object]:
        """Find the interface that speaks PCCS. After PC Suite was closed the first Connect can fail: retry a few times."""
        error = PccsError("silent")
        for attempt in range(attempts):
            if self._closing:
                raise PccsError("closed")
            try:
                return self._connect_once()
            except PccsError as exc:
                error = exc
                if exc.code not in ("silent", "not-pccs") or attempt == attempts - 1:
                    break
                self.sleep(self.pause)
        raise error

    def _release(self, session: PccsSession | None, channel) -> None:
        try:
            if session is not None:
                session.disconnect()
        except (PccsError, OSError):
            pass
        try:
            channel.close()
        except OSError:
            pass
        self._active = None

    # ---- probe -----------------------------------------------------------------------------------------------------
    def _set(self, state: str, reason: str | None, domains: dict | None = None, identity: dict | None = None) -> None:
        self._state, self._reason = state, reason or ""
        self._domains = domains or {"contacts": False, "calendar": False}
        if state in ("busy", "unavailable"):
            self._identity = {}
            self._verified.clear()
        elif identity is not None:
            self._identity = identity
        self._at = self.clock()

    def probe(self, force: bool = False) -> None:
        if self._closing:
            return
        if not force and self.clock() - self._at < TTL.get(self._state, 20.0):
            return
        if not self.lock.acquire(blocking=False):
            return  # a read is running: keep the last answer, never talk to the phone twice at once
        try:
            try:
                session, channel = self._connect(1)
            except PccsError as exc:
                self._set(STATE_OF.get(exc.code, "silent"), exc.code)
                return
            try:
                identity = self._identify(session)
                domains = {}
                for kind in nokia_pccs.STORES:
                    try:
                        nokia_pccs.open_only(session, kind)
                        domains[kind] = True
                    except (PccsError, OSError, struct.error):
                        domains[kind] = False
            except (PccsError, OSError, struct.error):
                self._set("silent", "silent")
                return
            finally:
                self._release(session, channel)
            if any(domains.values()):
                self._set("verified" if self._verified else "available", None, domains, identity)
            else:
                self._set("silent", "store", domains, identity)
        finally:
            self.lock.release()

    @staticmethod
    def _identify(session: PccsSession) -> dict:
        """Optional: manufacturer, model, software version. A phone that does not answer the capability object is still usable."""
        try:
            return nokia_pccs.parse_capability(session.get(None, nokia_pccs.CAPABILITY_TYPE, who=False))
        except (PccsError, OSError, struct.error):
            return {}

    # ---- device row ------------------------------------------------------------------------------------------------
    def matrix(self) -> capabilities.Matrix:
        m = capabilities.Matrix()
        labels = {"contacts": "contacts.read", "calendar": "calendar.read"}
        for kind, op in labels.items():
            if self._domains.get(kind):
                note = "verified by a read in this session" if kind in self._verified else "no records were read yet"
                m.available(op, SOURCE, f"The phone opened the {kind} store over USB OBEX ({note})")
            elif self._state in ("busy", "silent"):
                m.error(op, SOURCE, nokia_pccs.REASONS.get(self._reason, nokia_pccs.REASONS["silent"]))
            else:
                m.untested(op, SOURCE, "The store was not probed", attemptable=False)
        groups = {
            "files": ("files.list", "files.download", "files.upload", "files.stage", "files.mkdir", "files.delete", "media.read", "storage.read", "packages.transfer", "packages.opp-push"),
            "sms": ("sms.read", "sms.send"),
            "write": ("contacts.write", "contacts.stage"),
            "identity": ("identity.read",),
            "other": ("tasks.read", "telemetry.read"),
        }
        for group, ops in groups.items():
            for op in ops:
                m.unsupported(op, SOURCE, CAPS_REASON[group])
        return m

    def device(self) -> dict | None:
        self.probe()
        if self._state == "unavailable":
            return None  # no Nokia USB OBEX interface: nothing is invented
        connected = self._state in ("available", "verified")
        model = self._identity.get("model")
        name = "Nokia (USB OBEX)"
        if model:
            name = model if model.lower().startswith("nokia") else f"Nokia {model}"
        row = {
            "connection_identity": hashlib.sha256("\x1f".join(["pccs", str(model), str(self._identity.get("rm")), str(self._identity.get("firmware"))]).encode()).hexdigest(),
            "id": self.id,
            "name": name,
            "transport": "pccs",
            "connection_kind": "usb",
            "connected": connected,
            "manufacturer": self._identity.get("manufacturer") or "Nokia",
            "model": model,
            "rm": self._identity.get("rm"),
            "firmware": self._identity.get("firmware"),
            "battery": None,
            "route_state": self._state,
            "evidence": (
                "Nokia USB OBEX (PC Suite sync): the phone opened its stores; contacts and calendar can be read, nothing is written"
                if connected
                else nokia_pccs.REASONS.get(self._reason, nokia_pccs.REASONS["silent"])
            ),
        }
        if not connected:
            row["failure"] = {"reason": "port-busy" if self._state == "busy" else "no-response"}
        return capabilities.apply(row, self.matrix())

    def route_status(self) -> dict:
        """What the Doctor and the route chooser need; no interface path and no phone data."""
        return {"state": self._state, "reason": self._reason, "contacts": self._domains["contacts"], "calendar": self._domains["calendar"], "verified": sorted(self._verified)}

    # ---- operations ------------------------------------------------------------------------------------------------
    def execute(self, data: dict) -> dict:
        action = data.get("action")
        if action == "connection-diagnose":
            return {"connection": self.device()}
        kind = {"contacts-read": "contacts", "calendar-read": "calendar"}.get(str(action))
        if kind is None:
            raise ValueError(UNSUPPORTED)
        return self._read(kind)

    def _read(self, kind: str) -> dict:
        if self._closing:
            raise ValueError(MESSAGES["closed"])
        if not self.lock.acquire(blocking=False):
            raise ValueError("USB OBEX занят другой операцией")
        try:
            session = channel = None
            try:
                session, channel = self._connect(self.attempts)
                items, skipped = nokia_pccs.read_store(session, kind)
                if skipped and not items:
                    raise PccsError("format")  # every record was unreadable: say so instead of "empty"
            except PccsError as exc:
                self._set(STATE_OF.get(exc.code, self._state if self._state != "unavailable" else "silent"), exc.code, self._domains)
                raise ValueError(MESSAGES.get(exc.code, MESSAGES["silent"])) from None
            except (OSError, struct.error):
                self._set("silent", "closed", self._domains)
                raise ValueError(MESSAGES["closed"]) from None
            finally:
                if channel is not None:
                    self._release(session, channel)
            self._verified.add(kind)
            self._set("verified", None, {**self._domains, kind: True})
            if kind == "contacts":
                rows, _ = nokia_pccs.contacts_from(items)
                return {"contacts": rows}
            return {**nokia_pccs.calendar_from(items), "status": "read"}
        finally:
            self.lock.release()

    def close(self) -> None:
        """Bridge shutdown: cancel a running read and wait until its CloseStore/Disconnect have been sent."""
        self._closing = True
        channel = self._active
        if channel is not None:
            try:
                channel.cancel()
            except OSError:
                pass
        if self.lock.acquire(timeout=10):
            self.lock.release()
