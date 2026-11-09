"""Ephemeral, authenticated outbound PC bridge. No public phone-control endpoint.

Only the already authenticated Workbench owner may create a lease. Phone RPC
uses the same DeviceManager contract; queued payloads exist in memory only.
"""

from __future__ import annotations

import secrets
import threading
import time


class BrowserBridgeRelay:
    def __init__(self):
        self.condition = threading.Condition()
        self.token = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + 7200
        self.last_seen = 0.0
        self.pending = None
        self.result = None
        self.claimed = False
        self.closed = False
        self.connections = []
        self.polled = False

    def authorized(self, token):
        return (
            not self.closed
            and time.monotonic() < self.expires
            and isinstance(token, str)
            and secrets.compare_digest(token, self.token)
        )

    def status(self):
        return {
            "mode": "windows-bridge",
            "connected": not self.closed
            and time.monotonic() < self.expires
            and self.last_seen > time.monotonic() - 40,
            "expires_in": max(0, int(self.expires - time.monotonic())),
        }

    def remaining(self) -> int:
        """Seconds of access left; the Bridge shows it and warns shortly before the end. Nothing is stored anywhere."""
        return max(0, int(self.expires - time.monotonic()))

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.result = None
            self.condition.notify_all()

    def poll(self):
        with self.condition:
            self.last_seen = time.monotonic()
            # The first poll answers at once: the Bridge (and the wizard) learn immediately that the link works,
            # instead of after a full 20 s long-poll. Later polls wait for work as before.
            first, self.polled = not self.polled, True
            self.condition.wait_for(
                lambda: self.closed or (self.pending and not self.claimed), timeout=0 if first else 20
            )
            if self.closed:
                raise ValueError("Bridge access was revoked")
            self.last_seen = time.monotonic()
            if self.pending and not self.claimed:
                self.claimed = True  # never reissue an uncertain write on reconnect
                return {"request": self.pending, "expires_in": self.remaining()}
            return {"request": None, "expires_in": self.remaining()}

    def complete(self, data):
        with self.condition:
            if (
                not self.pending
                or data.get("id") != self.pending["id"]
                or not self.claimed
                or self.result is not None
            ):
                raise ValueError("Expired or duplicate bridge result")
            if not isinstance(data.get("result"), dict):
                raise ValueError("Invalid bridge result")
            self.result = data["result"]
            self.last_seen = time.monotonic()
            self.condition.notify_all()
        return {"accepted": True}

    def execute(self, data):
        timeout = (
            900
            if data.get("action")
            in (
                "personal-apply",
                "contacts-import",
                "software-transfer",
                "media-upload",
                "transfer",
            )
            else 180
        )
        with self.condition:
            if not self.status()["connected"]:
                raise ValueError("Windows Bridge is disconnected. Open Connection to reconnect.")
            if self.pending:
                raise ValueError("Bridge is busy")
            self.pending = {"id": secrets.token_hex(16), "data": data}
            self.claimed, self.result = False, None
            self.condition.notify_all()
            try:
                ready = self.condition.wait_for(
                    lambda: self.result is not None or self.closed, timeout
                )
                if not ready or self.closed:
                    raise ValueError("Bridge interrupted. Read the phone before retrying a write.")
                if "error" in self.result:
                    raise ValueError(str(self.result["error"])[:400])
                return self.result
            finally:
                self.pending, self.result = None, None
                self.claimed = False

    def devices(self):
        result = self.execute({"action": "discover"})
        self.connections = result.get("connections", [])
        return result.get("devices", [])

    def diagnostics(self):
        return self.connections
