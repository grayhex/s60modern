"""Loopback-only HTTP API reusing DeviceManager, with bounded authenticated requests."""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from symport.phone.bridge_security import host_allowed, key_allowed, origin_allowed

from . import VERSION

MAX_BODY = 90 * 1024 * 1024
DANGEROUS = {"delete", "restore", "personal-apply", "sms-send"}


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 8

    def __init__(self, manager, key: str, port=24652, origins=(), approve=None):
        if len(key) < 32:
            raise ValueError("Bridge key must contain at least 32 characters")
        self.manager, self.key, self.origins = manager, key, set(origins)
        self.approve = approve
        self.pair = None
        self.relay_state = None  # callable(origin) -> {"state", "reason"} | None, set by the Agent
        self.pair_lock = threading.Lock()
        self.last_pair = 0.0
        self.requests = threading.BoundedSemaphore(8)
        self.clients = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, address):
        if not self.clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.clients.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.clients.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "SymPortBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return  # never log URLs, tokens, payloads or private exception strings

    def reply(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin")
        if origin and (
            origin_allowed(origin, self.server.origins) or self.path == "/api/bridge/pair"
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if len(self.headers.get_all("Host", [])) != 1 or not host_allowed(
            self.headers.get("Host", ""), self.server.server_port
        ):
            self.reply({"error": "Host not allowed"}, 403)
            return False
        if len(self.headers.get_all("Origin", [])) > 1 or not origin_allowed(
            self.headers.get("Origin"), self.server.origins
        ):
            self.reply({"error": "Origin not enrolled"}, 403)
            return False
        if not key_allowed(self.headers.get("X-Symport-Bridge-Key", ""), self.server.key):
            self.reply({"error": "Bridge authorization required"}, 403)
            return False
        return True

    def do_OPTIONS(self):
        if self.path == "/api/bridge/pair":
            from .relay import validate_origin

            try:
                validate_origin(self.headers.get("Origin", ""))
                if (
                    len(self.headers.get_all("Host", [])) != 1
                    or len(self.headers.get_all("Origin", [])) != 1
                    or not host_allowed(self.headers.get("Host", ""), self.server.server_port)
                ):
                    raise ValueError("Invalid host")
            except ValueError:
                return self.reply({"error": "Invalid origin"}, 403)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if (
            not host_allowed(self.headers.get("Host", ""), self.server.server_port)
            or not self.headers.get("Origin")
            or not origin_allowed(self.headers.get("Origin"), self.server.origins)
        ):
            return self.reply({"error": "Origin not enrolled"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Symport-Bridge-Key")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/api/bridge/health":
            return self.reply(
                {
                    "schema": "symport.bridge/1",
                    "version": VERSION,
                    "transport": "loopback-http",
                    "firmware_install": False,
                }
            )
        return self.reply({"error": "Not found"}, 404)

    def pair(self):
        from .relay import validate_origin

        try:
            origin = validate_origin(self.headers.get("Origin", ""))
            if (
                len(self.headers.get_all("Host", [])) != 1
                or len(self.headers.get_all("Origin", [])) != 1
                or not host_allowed(self.headers.get("Host", ""), self.server.server_port)
            ):
                raise ValueError("Invalid host")
            if (
                self.headers.get("Transfer-Encoding")
                or len(self.headers.get_all("Content-Length", [])) != 1
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
            ):
                raise ValueError("JSON required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid body")
            data = json.loads(self.rfile.read(length))
            if isinstance(data, dict) and data.get("probe") is True and data.get("origin") == origin:
                # Reachability check for the first-connection wizard: no prompt, no state change,
                # and nothing but the Bridge version is disclosed to any enrolled-or-not web origin.
                return self.reply(
                    {"probe": True, "version": VERSION, "pair_available": bool(self.server.pair)}
                )
            if isinstance(data, dict) and data.get("relay_status") is True and data.get("origin") == origin:
                # Lets the wizard explain why a paired Bridge is not connected yet (e.g. untrusted certificate).
                state = self.server.relay_state(origin) if self.server.relay_state else None
                return self.reply({"relay": state})
            if (
                not isinstance(data, dict)
                or data.get("origin") != origin
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", str(data.get("token", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("ca_sha256", "0" * 64)))
            ):
                raise ValueError("Invalid pairing")
            if not self.server.pair or not self.server.pair_lock.acquire(blocking=False):
                return self.reply({"error": "Local pairing unavailable or busy"}, 409)
            try:
                if time.monotonic() - self.server.last_pair < 15:
                    return self.reply({"error": "Wait before requesting access again"}, 429)
                self.server.last_pair = time.monotonic()
                extra = (data["ca_sha256"],) if data.get("ca_sha256") else ()
                if not self.server.pair(origin, data["token"], *extra):
                    return self.reply({"error": "Access was declined on the PC"}, 403)
                return self.reply({"paired": True, "version": VERSION})
            finally:
                self.server.pair_lock.release()
        except (ValueError, OSError):
            return self.reply({"error": "Invalid pairing request"}, 400)

    def do_POST(self):
        self.close_connection = True  # unread denied bodies cannot become a second request
        if self.path == "/api/bridge/pair":
            return self.pair()
        if not self.authorized():
            return
        if self.path != "/api/bridge":
            return self.reply({"error": "Not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reply({"error": "Invalid length"}, 400)
        if (
            self.headers.get("Transfer-Encoding")
            or len(self.headers.get_all("Content-Length", [])) != 1
            or not 0 < length <= MAX_BODY
        ):
            return self.reply({"error": "Invalid length"}, 413)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply({"error": "JSON required"}, 415)
        if not self.server.requests.acquire(blocking=False):
            return self.reply({"error": "Bridge busy"}, 429)
        try:
            self.connection.settimeout(30)
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply({"error": "Incomplete request"}, 400)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return self.reply({"error": "JSON object required"}, 400)
            if data.get("action") in DANGEROUS:
                if data.get("confirm") is not True:
                    return self.reply({"error": "Explicit confirmation required"}, 400)
                if not self.server.approve or not self.server.approve(data):
                    return self.reply(
                        {"error": "The local phone operator did not approve this operation"}, 403
                    )
            return self.reply(self.server.manager.execute(data))
        except (ValueError, OSError):
            return self.reply(
                {
                    "error": "The transport could not complete the request. Check connection diagnostics; read the phone before retrying a write"
                },
                400,
            )
        finally:
            self.server.requests.release()
