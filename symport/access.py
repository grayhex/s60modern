"""Access policy of the SymPort web server: Localhost (default) or Home LAN.

Localhost keeps the original contract (loopback Host/Origin, per-process token, SSH tunnel or an
authenticated HTTPS reverse proxy in front). Home LAN is an explicit opt-in: the server is published on one
specific LAN address, accepts only that exact Host/Origin and requires its own login before any API.
Every configuration mistake is a startup error; nothing falls back to an open server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MODE_LOCALHOST = "localhost"
MODE_LAN = "lan"

MIN_PASSWORD_LENGTH = 12
_WEAK = {
    "password", "passw0rd", "changeme", "change-me", "change_me", "admin", "administrator", "symport",
    "qwerty", "letmein", "welcome", "default", "secret", "nokia", "symbian", "123456",
}
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")

SESSION_COOKIE = "symport_session"


class AccessConfigError(ValueError):
    """The access configuration is unsafe or inconsistent; the server must not start."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wildcard(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def validate_host_name(value: str, label: str) -> str:
    """One bare IP address or DNS name: no scheme, port, path, wildcard, userinfo."""
    value = value.strip().lower()
    if not value:
        raise AccessConfigError(f"{label} is empty")
    if any(ch in value for ch in "/@*?# \t") or "://" in value:
        raise AccessConfigError(f"{label} must be a bare IP address or host name (no scheme, port or path): {value!r}")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        if ":" in value or not _HOSTNAME.fullmatch(value):
            raise AccessConfigError(f"{label} is not a valid IP address or host name: {value!r}") from None
        return value
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_loopback:
        raise AccessConfigError(f"{label} must be a concrete LAN address, not {value}")
    return str(parsed)


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccessConfigError(f"The admin password is too short (minimum {MIN_PASSWORD_LENGTH} characters)")
    lowered = password.lower().strip()
    if lowered in _WEAK or len(set(password)) < 5 or lowered.rstrip("0123456789!") in _WEAK:
        raise AccessConfigError("The admin password is too weak or a well-known default; choose your own")
    if lowered.startswith(("change", "replace", "your-", "your_", "your ", "example", "<")):
        raise AccessConfigError("The admin password looks like a placeholder from the documentation; choose your own")


def hash_password(password: str, *, n: int = 2**14, r: int = 8, p: int = 1, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)
    enc = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${n}${r}${p}${enc(salt)}${enc(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt, validate=True),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _read_secret(env: dict[str, str], name: str) -> str:
    direct = env.get(name, "")
    path = env.get(name + "_FILE", "").strip()
    if direct.strip() and path:
        raise AccessConfigError(f"Set either {name} or {name}_FILE, not both")
    if path:
        try:
            direct = Path(path).read_text(encoding="utf-8").strip("\r\n")
        except OSError as exc:
            raise AccessConfigError(f"Cannot read {name}_FILE ({path}): {exc.strerror or exc}") from None
    return direct


@dataclass(frozen=True)
class AccessConfig:
    mode: str = MODE_LOCALHOST
    bind: str = "127.0.0.1"
    port: int = 8765
    hosts: tuple[str, ...] = ()
    password_hash: str = ""
    public_origin: str = ""
    idle_seconds: int = 2 * 3600
    absolute_seconds: int = 12 * 3600
    tls: bool = False  # SymPort itself serves HTTPS (Home LAN default)
    tls_cert: str = ""  # operator-supplied certificate; empty = SymPort's own local CA
    tls_key: str = ""
    tls_dir: str = ""
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def lan(self) -> bool:
        return self.mode == MODE_LAN

    @property
    def _plain_hosts(self) -> frozenset[str]:
        if self.lan:
            return frozenset(f"[{h}]:{self.port}" if ":" in h else f"{h}:{self.port}" for h in self.hosts)
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Exact Host header values (host:port) accepted for the web UI."""
        extra = {urlsplit(self.public_origin).netloc} if self.public_origin else set()
        return self._plain_hosts | extra

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Exact browser Origin values: plain http for the local/LAN address, https for the public proxy."""
        extra = {self.public_origin} if self.public_origin else set()
        scheme = "https" if self.tls else "http"
        return frozenset(f"{scheme}://{host}" for host in self._plain_hosts) | extra


def _origin(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        return ""
    return f"https://{parts.netloc}"


def load_access_config(env: dict[str, str] | None = None, *, port: int) -> AccessConfig:
    """Validate the environment. Raises AccessConfigError instead of ever returning an unsafe combination."""
    env = dict(os.environ if env is None else env)
    mode = env.get("SYMPORT_ACCESS_MODE", "").strip().lower() or MODE_LOCALHOST
    if mode not in (MODE_LOCALHOST, MODE_LAN):
        raise AccessConfigError(f"SYMPORT_ACCESS_MODE must be 'localhost' or 'lan', not {mode!r}")
    # The in-container --host is always 0.0.0.0; the address that matters is what the operator publishes.
    published = env.get("SYMPORT_BIND", "").strip() or "127.0.0.1"
    public_raw = env.get("SYMPORT_PUBLIC_ORIGIN", "").strip()
    public = _origin(public_raw)
    if public_raw and not public:
        raise AccessConfigError("SYMPORT_PUBLIC_ORIGIN must be an https origin without path, e.g. https://symport.example.org")
    http_port = env.get("SYMPORT_HTTP_PORT", "").strip()
    if http_port and http_port != str(port):
        raise AccessConfigError(f"SYMPORT_HTTP_PORT={http_port} does not match the listening port {port}")
    if not 1 <= port <= 65535:
        raise AccessConfigError("Invalid port")

    tls_setting = env.get("SYMPORT_TLS", "").strip().lower() or "auto"
    if tls_setting not in ("auto", "on", "off"):
        raise AccessConfigError("SYMPORT_TLS must be 'auto', 'on' or 'off'")
    tls_cert = env.get("SYMPORT_TLS_CERT_FILE", "").strip()
    tls_key = env.get("SYMPORT_TLS_KEY_FILE", "").strip()
    if bool(tls_cert) != bool(tls_key):
        raise AccessConfigError("Set both SYMPORT_TLS_CERT_FILE and SYMPORT_TLS_KEY_FILE, or neither")
    if mode == MODE_LOCALHOST and (tls_setting == "on" or tls_cert):
        raise AccessConfigError("SYMPORT_TLS applies to Home LAN mode; Localhost mode is served on loopback "
                                "(use an HTTPS reverse proxy with SYMPORT_PUBLIC_ORIGIN for HTTPS)")

    if mode == MODE_LOCALHOST:
        if not _is_loopback(published) and not public:
            raise AccessConfigError(
                f"SYMPORT_BIND={published} publishes the port beyond this computer, but Localhost mode rejects every "
                "non-local address. For a home network set SYMPORT_ACCESS_MODE=lan (with an admin password), "
                "otherwise keep SYMPORT_BIND=127.0.0.1 (use an SSH tunnel or an HTTPS proxy with SYMPORT_PUBLIC_ORIGIN).")
        return AccessConfig(MODE_LOCALHOST, published, port, (), "", public)

    # --- Home LAN: everything below must hold or the server does not start.
    warnings: list[str] = []
    lan_host = env.get("SYMPORT_LAN_HOST", "").strip()
    if _is_loopback(published):
        raise AccessConfigError("Home LAN mode needs SYMPORT_BIND set to the NAS LAN address (e.g. 192.168.1.5); "
                                "127.0.0.1 is reachable only from the NAS itself")
    hosts: list[str] = []
    if _is_wildcard(published):
        if not lan_host:
            raise AccessConfigError("Home LAN mode refuses SYMPORT_BIND=0.0.0.0 without SYMPORT_LAN_HOST. "
                                    "Prefer SYMPORT_BIND=<NAS LAN IP>; with 0.0.0.0 you must name the exact "
                                    "address or host name browsers use in SYMPORT_LAN_HOST.")
        warnings.append("SYMPORT_BIND=0.0.0.0 publishes the port on every NAS interface; "
                        "prefer the concrete LAN IP and never forward this port on the router.")
    else:
        hosts.append(validate_host_name(published, "SYMPORT_BIND"))
    if lan_host:
        extra = validate_host_name(lan_host, "SYMPORT_LAN_HOST")
        if extra not in hosts:
            hosts.append(extra)
    if not hosts:
        raise AccessConfigError("Home LAN mode has no allowed host")

    password = _read_secret(env, "SYMPORT_ADMIN_PASSWORD")
    given_hash = env.get("SYMPORT_ADMIN_PASSWORD_HASH", "").strip()
    if password and given_hash:
        raise AccessConfigError("Set either SYMPORT_ADMIN_PASSWORD or SYMPORT_ADMIN_PASSWORD_HASH, not both")
    if given_hash:
        if not re.fullmatch(r"scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+", given_hash):
            raise AccessConfigError("SYMPORT_ADMIN_PASSWORD_HASH is not a scrypt hash made by "
                                    "`python -m symport.access hash`")
        encoded = given_hash
    elif password:
        check_password_strength(password)
        encoded = hash_password(password)
    else:
        raise AccessConfigError("Home LAN mode requires an admin password: set SYMPORT_ADMIN_PASSWORD "
                                "(or SYMPORT_ADMIN_PASSWORD_FILE). The server will not start without one.")
    # HTTPS is the default for Home LAN: the Windows Bridge pairs only with an https origin. Behind an HTTPS
    # reverse proxy (SYMPORT_PUBLIC_ORIGIN) the proxy terminates TLS, so SymPort serves plain HTTP to it.
    tls = tls_setting == "on" or (tls_setting == "auto" and not public)
    if not public and not tls:
        warnings.append("Plain HTTP (SYMPORT_TLS=off): the Windows Bridge cannot pair with it, and the password, session cookie and phone data can be read by anyone who can "
                        "capture traffic on this network. Use only on a network you trust, or put an HTTPS "
                        "reverse proxy in front and set SYMPORT_PUBLIC_ORIGIN.")

    def seconds(name: str, default: int, low: int, high: int) -> int:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw) * 60
        except ValueError:
            raise AccessConfigError(f"{name} must be a whole number of minutes") from None
        if not low <= value <= high:
            raise AccessConfigError(f"{name} must be between {low // 60} and {high // 60} minutes")
        return value

    idle = seconds("SYMPORT_SESSION_IDLE_MINUTES", 2 * 3600, 5 * 60, 24 * 3600)
    absolute = seconds("SYMPORT_SESSION_MAX_MINUTES", 12 * 3600, 5 * 60, 7 * 24 * 3600)
    return AccessConfig(MODE_LAN, published, port, tuple(hosts), encoded, public, idle, max(idle, absolute),
                        tls, tls_cert, tls_key, env.get("SYMPORT_TLS_DIR", "").strip(), tuple(warnings))


@dataclass
class Session:
    csrf: str
    created: float
    seen: float
    client: str


class SessionStore:
    """In-memory sessions (a server restart signs everybody out). Only token hashes are kept."""

    def __init__(self, idle: int, absolute: int, clock=time.monotonic):
        self.idle, self.absolute, self.clock = idle, absolute, clock
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, client: str) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(secrets.token_urlsafe(32), now, now, client)
        with self._lock:
            self._purge(now)
            self._items[self._key(token)] = session
        return token, session

    def _purge(self, now: float) -> None:
        for key, item in list(self._items.items()):
            if now - item.seen > self.idle or now - item.created > self.absolute:
                del self._items[key]

    def get(self, token: str) -> Session | None:
        if not token:
            return None
        now = self.clock()
        with self._lock:
            item = self._items.get(self._key(token))
            if item is None:
                return None
            if now - item.seen > self.idle or now - item.created > self.absolute:
                del self._items[self._key(token)]
                return None
            item.seen = now
            return item

    def revoke(self, token: str) -> None:
        with self._lock:
            self._items.pop(self._key(token), None)

    def revoke_all(self) -> None:
        with self._lock:
            self._items.clear()


class LoginLimiter:
    """Failed-login throttling per client address plus a global ceiling."""

    def __init__(self, per_client: int = 5, overall: int = 30, window: int = 300, clock=time.monotonic):
        self.per_client, self.overall, self.window, self.clock = per_client, overall, window, clock
        self._failures: dict[str, list[float]] = {}
        self._all: list[float] = []
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        self._all = [t for t in self._all if t > cutoff]
        for key in list(self._failures):
            self._failures[key] = [t for t in self._failures[key] if t > cutoff]
            if not self._failures[key]:
                del self._failures[key]

    def retry_after(self, client: str) -> int:
        """Seconds to wait before another attempt is accepted; 0 when allowed."""
        now = self.clock()
        with self._lock:
            self._trim(now)
            mine = self._failures.get(client, [])
            waits = []
            if len(mine) >= self.per_client:
                waits.append(mine[0] + self.window - now)
            if len(self._all) >= self.overall:
                waits.append(self._all[0] + self.window - now)
            return max(1, int(max(waits)) + 1) if waits else 0

    def failed(self, client: str) -> None:
        now = self.clock()
        with self._lock:
            self._failures.setdefault(client, []).append(now)
            self._all.append(now)

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(prog="python -m symport.access", description="Home LAN mode helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash", help="print a SYMPORT_ADMIN_PASSWORD_HASH value (password read from stdin/prompt)")
    args = parser.parse_args(argv)
    if args.cmd == "hash":
        password = sys.stdin.readline().rstrip("\r\n") if not sys.stdin.isatty() else getpass.getpass("Admin password: ")
        try:
            check_password_strength(password)
        except AccessConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
