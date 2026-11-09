"""Outbound HTTPS relay, enrolled by the local operator. No inbound Internet port."""

from __future__ import annotations

import ipaddress
import json
import ssl
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .server import DANGEROUS, MAX_BODY


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_origin(origin):
    try:
        if (
            not isinstance(origin, str)
            or len(origin) > 256
            or any(ord(c) <= 32 or ord(c) > 126 for c in origin)
        ):
            raise ValueError("Invalid origin")
        value = urlsplit(origin)
        if value.port is not None and not 1 <= value.port <= 65535:
            raise ValueError("Invalid port")
        valid = value.scheme == "https" or (
            value.scheme == "http" and value.hostname in ("127.0.0.1", "localhost")
        )
        if (
            not valid
            or value.username
            or value.password
            or value.path
            or value.query
            or value.fragment
            or not value.hostname
        ):
            raise ValueError("Use an HTTPS SymPort origin, or a local HTTP Workbench")
        return origin
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid SymPort origin") from exc


def is_local_network(host: str) -> bool:
    """Private/loopback/link-local addresses and home-network names: reached directly, never through a proxy."""
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        return host == "localhost" or host.endswith((".local", ".lan", ".home.arpa", ".home", ".internal"))


LEASE_CODES = ("lease-missing", "lease-expired", "bad-token")


def classify(error: BaseException) -> str:
    """A short, secret-free reason the operator can act on. TLS, network and lease problems stay distinct."""
    if isinstance(error, HTTPError):
        if error.code in (401, 403):
            try:
                code = json.loads(error.read(2048)).get("code")
            except (ValueError, OSError, AttributeError):
                code = None
            return code if code in LEASE_CODES else "rejected"
        return "server-error"
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return {62: "tls-hostname", 10: "tls-expired"}.get(getattr(reason, "verify_code", None), "tls-untrusted")
    if isinstance(reason, ssl.SSLError):
        return "tls-error"
    return "unreachable"


class OutboundRelay:
    def __init__(self, agent, origin, token, ca_file: Path | None = None):
        self.agent, self.origin, self.token = agent, validate_origin(origin), token
        self.stop = threading.Event()
        # state: connecting → connected | error. `reason` is one of classify() and carries no URL or secret.
        self.state, self.reason, self.changed = "connecting", None, time.monotonic()
        self.pinned = ca_file is not None  # verification uses the CA the operator approved at pairing
        handlers = [NoRedirect()]
        host = urlsplit(self.origin).hostname or ""
        if is_local_network(host):
            handlers.append(ProxyHandler({}))  # a system/corporate proxy must not sit between the PC and the NAS
        if self.origin.startswith("https://"):
            context = ssl.create_default_context()  # Windows certificate store (or SSL_CERT_FILE)
            if ca_file is not None:
                context.load_verify_locations(cafile=str(ca_file))  # the CA the operator approved at pairing
            from urllib.request import HTTPSHandler

            handlers.append(HTTPSHandler(context=context))
        self.opener = build_opener(*handlers)
        self.thread = threading.Thread(target=self.run, daemon=True, name="bridge-outbound")

    def call(self, action, body):
        request = Request(
            self.origin + "/api/bridge-relay/" + action,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Symport-Relay-Key": self.token},
        )
        with self.opener.open(request, timeout=30) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("Oversized relay request")
        return json.loads(raw)

    def mark(self, state, reason=None):
        if (state, reason) != (self.state, self.reason):
            self.state, self.reason, self.changed = state, reason, time.monotonic()

    def public_state(self):
        return {"state": self.state, "reason": self.reason, "pinned": self.pinned}

    def run(self):
        while not self.stop.is_set() and not self.agent.stop.is_set():
            try:
                message = self.call("poll", {})
                self.mark("connected")
                work = message.get("request")
                if not work:
                    continue
                if self.stop.is_set():
                    break
                data = work["data"]
                try:
                    if data.get("action") in DANGEROUS and (
                        data.get("confirm") is not True
                        or not self.agent.server.approve
                        or not self.agent.server.approve(data)
                    ):
                        raise ValueError("Local approval required")
                    result = self.agent.manager.execute(data)
                except (ValueError, OSError):
                    result = {
                        "error": "The local transport could not complete the operation. Check Bridge diagnostics; read the phone before retrying a write."
                    }
                if self.stop.is_set() or self.agent.stop.is_set():
                    break  # revoked sessions receive no more private results
                # A lost result is never retried: no accidental repeated SMS/writes.
                self.call("result", {"id": work["id"], "result": result})
            except HTTPError as exc:
                self.mark("error", classify(exc))
                if exc.code in (401, 403):
                    break
                if self.stop.wait(3):
                    break
            except Exception as exc:
                self.mark("error", classify(exc))
                if self.stop.wait(3):
                    break
