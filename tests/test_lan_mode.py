"""Home LAN mode (issue #72): fail-closed configuration, mandatory login, strict Host/Origin, CSRF, sessions."""
from __future__ import annotations

import http.client
import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

import symport.web as web
from symport.access import (
    AccessConfig,
    AccessConfigError,
    LoginLimiter,
    SessionStore,
    hash_password,
    load_access_config,
    verify_password,
)
from symport.web import Workbench, make_handler

PASSWORD = "a-long-private-secret"
NAS = "192.168.1.5"
GOOD = {"SYMPORT_ACCESS_MODE": "lan", "SYMPORT_BIND": NAS, "SYMPORT_ADMIN_PASSWORD": PASSWORD}


def load(**overrides):
    env = dict(GOOD)
    env.update(overrides)
    return load_access_config({k: v for k, v in env.items() if v is not None}, port=8765)


# ---------------------------------------------------------------- configuration

def test_lan_config_accepts_exact_nas_address_and_warns_about_plain_http():
    config = load()
    assert config.lan and config.allowed_hosts == {f"{NAS}:8765"}
    assert config.tls and config.allowed_origins == {f"https://{NAS}:8765"}, "HTTPS is the Home LAN default"
    assert not config.warnings
    plain = load(SYMPORT_TLS="off")
    assert not plain.tls and plain.allowed_origins == {f"http://{NAS}:8765"}
    assert any("Plain HTTP" in w for w in plain.warnings)
    assert verify_password(PASSWORD, config.password_hash) and not verify_password("wrong", config.password_hash)


def test_lan_ip_change_is_a_config_change_not_a_wildcard():
    assert load(SYMPORT_BIND="192.168.1.77").allowed_hosts == {"192.168.1.77:8765"}


@pytest.mark.parametrize("overrides", [
    {"SYMPORT_ADMIN_PASSWORD": None},                      # missing secret
    {"SYMPORT_ADMIN_PASSWORD": ""},
    {"SYMPORT_ADMIN_PASSWORD": "short"},                   # weak
    {"SYMPORT_ADMIN_PASSWORD": "changeme"},
    {"SYMPORT_ADMIN_PASSWORD": "aaaaaaaaaaaaaaaa"},
    {"SYMPORT_ADMIN_PASSWORD": "change-this-password"},
    {"SYMPORT_BIND": "0.0.0.0"},                           # wildcard needs an explicit exact host
    {"SYMPORT_BIND": "127.0.0.1"},                         # loopback is not a LAN address
    {"SYMPORT_BIND": None},
    {"SYMPORT_BIND": "*"},
    {"SYMPORT_BIND": "http://192.168.1.5"},
    {"SYMPORT_LAN_HOST": "192.168.1.5:8765"},
    {"SYMPORT_LAN_HOST": "bad host"},
    {"SYMPORT_HTTP_PORT": "9000"},                         # port and listener must agree
    {"SYMPORT_ACCESS_MODE": "open"},
    {"SYMPORT_TLS": "maybe"},
    {"SYMPORT_TLS_CERT_FILE": "/only/the/cert.pem"},
    {"SYMPORT_PUBLIC_ORIGIN": "http://nas.local"},
    {"SYMPORT_ADMIN_PASSWORD_HASH": hash_password("another-secret-123")},  # both password and hash
    {"SYMPORT_SESSION_IDLE_MINUTES": "0"},
])
def test_lan_config_fails_closed(overrides):
    with pytest.raises(AccessConfigError):
        load(**overrides)


def test_bind_all_interfaces_alone_never_opens_or_disables_auth():
    with pytest.raises(AccessConfigError):
        load_access_config({"SYMPORT_BIND": "0.0.0.0"}, port=8765)       # localhost mode would be half-working
    with pytest.raises(AccessConfigError):
        load(SYMPORT_BIND="0.0.0.0")
    config = load(SYMPORT_BIND="0.0.0.0", SYMPORT_LAN_HOST="nas.home")
    assert config.lan and config.allowed_hosts == {"nas.home:8765"}      # still exact, still protected
    assert any("0.0.0.0" in w for w in config.warnings)


def test_localhost_is_the_unchanged_default():
    config = load_access_config({}, port=8765)
    assert not config.lan and config.allowed_hosts == {"127.0.0.1:8765", "localhost:8765"}
    proxied = load_access_config({"SYMPORT_BIND": "0.0.0.0", "SYMPORT_PUBLIC_ORIGIN": "https://s.example.org"}, port=8765)
    assert "s.example.org" in proxied.allowed_hosts and not proxied.lan


def test_password_file_and_hash(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text(PASSWORD + "\n")
    config = load(SYMPORT_ADMIN_PASSWORD=None, SYMPORT_ADMIN_PASSWORD_FILE=str(secret))
    assert verify_password(PASSWORD, config.password_hash)
    hashed = hash_password("another-secret-123")
    assert verify_password("another-secret-123", load(SYMPORT_ADMIN_PASSWORD=None, SYMPORT_ADMIN_PASSWORD_HASH=hashed).password_hash)
    with pytest.raises(AccessConfigError):
        load(SYMPORT_ADMIN_PASSWORD=None, SYMPORT_ADMIN_PASSWORD_FILE=str(tmp_path / "missing"))


def test_main_refuses_to_start_on_bad_config(monkeypatch, capsys):
    monkeypatch.setenv("SYMPORT_ACCESS_MODE", "lan")
    monkeypatch.setenv("SYMPORT_BIND", NAS)
    monkeypatch.delenv("SYMPORT_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SYMPORT_ADMIN_PASSWORD_FILE", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        web.main(["--host", "0.0.0.0", "--port", "8765"])
    assert exit_info.value.code == 78 and "admin password" in capsys.readouterr().err


# ---------------------------------------------------------------- sessions and limiter

def test_sessions_expire_idle_and_absolute_and_can_be_revoked():
    now = [0.0]
    store = SessionStore(idle=100, absolute=300, clock=lambda: now[0])
    token, session = store.create("1.2.3.4")
    assert store.get(token) is session
    now[0] = 90
    assert store.get(token)                          # activity extends the idle window
    now[0] = 180
    assert store.get(token)
    now[0] = 270
    assert store.get(token)
    now[0] = 310                                     # absolute lifetime reached
    assert store.get(token) is None
    other, _ = store.create("1.2.3.4")
    now[0] = 500
    assert store.get(other) is None                  # idle
    third, _ = store.create("x")
    store.revoke(third)
    assert store.get(third) is None and store.get("") is None


def test_login_limiter_blocks_and_recovers():
    now = [0.0]
    limiter = LoginLimiter(per_client=3, overall=5, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.retry_after("a") == 0
        limiter.failed("a")
    assert limiter.retry_after("a") > 0 and limiter.retry_after("b") == 0
    limiter.failed("b")
    limiter.failed("c")
    assert limiter.retry_after("d") > 0               # global ceiling
    now[0] = 120
    assert limiter.retry_after("a") == 0 and limiter.retry_after("d") == 0


# ---------------------------------------------------------------- HTTP

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def lan(tmp_path, monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda _s: None)
    for variant in ("light", "dark"):
        (tmp_path / "build" / "symport" / "theme" / variant).mkdir(parents=True)
    (tmp_path / "workspace").mkdir()
    port = _free_port()
    config = AccessConfig("lan", NAS, port, (NAS,), hash_password(PASSWORD, n=2**10))
    state = Workbench(tmp_path, config)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def call(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdr = {"Host": host or f"{NAS}:{port}"}
    hdr.update(headers or {})
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    conn.request(method, path, body=raw, headers=hdr)
    response = conn.getresponse()
    data = response.read()
    result = response.status, data, response
    conn.close()
    return result


def sign_in(port, password=PASSWORD):
    origin = f"http://{NAS}:{port}"
    status, data, response = call(port, "POST", "/api/auth/login", {"password": password}, {"Origin": origin})
    cookies = {}
    for header, value in response.getheaders():
        if header.lower() == "set-cookie":
            name, _, rest = value.partition("=")
            cookies[name] = value
    return status, data, cookies


def session_headers(port, cookies, csrf):
    jar = "; ".join(v.split(";")[0] for v in cookies.values())
    return {"Cookie": jar, "X-Symport-Token": csrf, "Origin": f"http://{NAS}:{port}"}


def test_anonymous_sees_only_login_screen_and_health(lan):
    state, port = lan
    status, body, response = call(port, "GET", "/")
    assert status == 200 and b"login.js" in body and state.token.encode() not in body
    assert "nonce-" + state.token not in response.getheader("Content-Security-Policy")
    assert call(port, "GET", "/auth/login.js")[0] == 200 and call(port, "GET", "/auth/login.css")[0] == 200
    assert call(port, "GET", "/healthz")[0] == 200
    assert json.loads(call(port, "GET", "/api/auth/session")[1]) == {
        "mode": "lan", "authenticated": False, "tls": False, "ca": False}
    for path in ("/api/state", "/api/device/file?device=a&path=/x", "/api/media/file?device=a&path=/x",
                 "/api/media/export/abc", "/api/audio/a/b", "/api/catalog/archive?id=x", "/api/library/download?id=x",
                 "/api/download?id=x", "/api/device-report", "/api/preview/light", "/next/index.html", "/app.js",
                 "/translations.json"):
        status, body, _ = call(port, "GET", path)
        assert status == 401 and json.loads(body)["login"] is True, path
    status, _, _ = call(port, "GET", "/api/device/file?device=a&path=/x", headers={"Range": "bytes=0-10"})
    assert status == 401
    for path in ("/api/manager", "/api/run", "/api/backups", "/api/catalog", "/api/connection", "/api/diagnostics",
                 "/api/media/export", "/api/bridge", "/api/device-report", "/api/ringtone/upload"):
        status, _, _ = call(port, "POST", path, {"action": "x"}, {"Origin": f"http://{NAS}:{port}", "X-Symport-Token": state.token})
        assert status == 401, path
    assert state.bridge_relay is None and not state.sessions._items


def test_host_and_origin_allowlists_defeat_rebinding(lan):
    state, port = lan
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"192.168.1.6:{port}", f"{NAS}.evil.test:{port}",
                 f"evil.test:{port}", NAS, f"{NAS}:{port + 1}"):
        assert call(port, "GET", "/", host=host)[0] == 403, host
        assert call(port, "POST", "/api/auth/login", {"password": PASSWORD}, host=host)[0] == 403
    assert call(port, "GET", "/healthz", host=f"127.0.0.1:{port}")[0] == 200   # container healthcheck only
    assert call(port, "GET", "/api/state", host=f"127.0.0.1:{port}")[0] == 403
    status, data, _ = call(port, "POST", "/api/auth/login", {"password": PASSWORD}, {"Origin": "http://evil.test"})
    assert status == 403
    status, data, _ = call(port, "POST", "/api/auth/login", {"password": PASSWORD})
    assert status == 403 and b"Origin" in data               # a browser login must name its origin


def test_login_session_csrf_and_logout(lan):
    state, port = lan
    status, data, _ = sign_in(port, "wrong")
    assert status == 401 and json.loads(data)["error"] == "Wrong password"
    status, data, cookies = sign_in(port)
    assert status == 200
    csrf = json.loads(data)["csrf"]
    session_cookie = cookies["symport_session"]
    assert "HttpOnly" in session_cookie and "SameSite=Strict" in session_cookie and "Secure" not in session_cookie
    assert csrf not in session_cookie and state.token not in json.dumps(cookies)
    headers = session_headers(port, cookies, csrf)

    status, body, _ = call(port, "GET", "/api/state", headers={"Cookie": headers["Cookie"]})
    assert status == 200 and json.loads(body)["schema"] == "symport.web/1"
    assert json.loads(call(port, "GET", "/api/auth/session", headers={"Cookie": headers["Cookie"]})[1]) == {
        "mode": "lan", "authenticated": True, "tls": False, "ca": False, "csrf": csrf}

    # state-changing requests: CSRF header and Origin are both mandatory
    save = {"rm": "RM-469"}
    assert call(port, "POST", "/api/device-report", save, {k: v for k, v in headers.items() if k != "X-Symport-Token"})[0] == 403
    assert call(port, "POST", "/api/device-report", save, dict(headers, **{"X-Symport-Token": state.token}))[0] == 403
    assert call(port, "POST", "/api/device-report", save, {k: v for k, v in headers.items() if k != "Origin"})[0] == 403
    assert call(port, "POST", "/api/device-report", save, dict(headers, Origin="http://evil.test"))[0] == 403
    assert call(port, "POST", "/api/device-report", save, headers)[0] == 200
    # no cookie but a valid-looking CSRF header is still anonymous
    assert call(port, "POST", "/api/device-report", save, {"X-Symport-Token": csrf, "Origin": headers["Origin"]})[0] == 401

    # media routes accept the session, but not a missing media cookie
    only_session = {"Cookie": cookies["symport_session"].split(";")[0]}
    assert call(port, "GET", "/api/device/file?device=x&path=/a", headers=only_session)[0] == 403
    status, body, _ = call(port, "GET", "/api/device/file?device=x&path=/a", headers=dict(only_session, Range="bytes=0-1", **{"Cookie": headers["Cookie"]}))
    assert status == 400                                    # authorised; the unknown device is rejected by the handler

    # logout revokes the server session and clears the cookies
    assert call(port, "POST", "/api/auth/logout", {}, {k: v for k, v in headers.items() if k != "X-Symport-Token"})[0] == 403
    status, _, response = call(port, "POST", "/api/auth/logout", {}, headers)
    assert status == 200 and any("Max-Age=0" in v for h, v in response.getheaders() if h.lower() == "set-cookie")
    assert call(port, "GET", "/api/state", headers={"Cookie": headers["Cookie"]})[0] == 401
    assert call(port, "POST", "/api/device-report", save, headers)[0] == 401


def test_session_expiry_over_http(lan):
    state, port = lan
    now = [0.0]
    state.sessions.clock = lambda: now[0]
    _, data, cookies = sign_in(port)
    jar = {"Cookie": "; ".join(v.split(";")[0] for v in cookies.values())}
    assert call(port, "GET", "/api/state", headers=jar)[0] == 200
    now[0] = state.access.idle_seconds + 1
    assert call(port, "GET", "/api/state", headers=jar)[0] == 401


def test_brute_force_is_throttled_even_for_the_right_password(lan):
    _, port = lan
    for _ in range(5):
        assert sign_in(port, "guess-guess-guess")[0] == 401
    status, data, _ = sign_in(port)
    assert status == 429 and json.loads(data)["retry_after"] > 0


def test_bridge_relay_keeps_its_own_credentials(lan):
    state, port = lan
    # no relay lease: rejected by the relay key check, not by the login (and not accepted anonymously)
    status, data, _ = call(port, "POST", "/api/bridge-relay/poll", {}, {"X-Symport-Relay-Key": "x" * 40})
    assert status == 403 and b"Bridge lease" in data
    status, data, _ = call(port, "POST", "/api/bridge-relay/result", {}, {"Origin": f"http://{NAS}:{port}"})
    assert status == 403
    status, _, _ = call(port, "POST", "/api/bridge-relay/poll", {}, {"Origin": "http://evil.test"})
    assert status == 403
    assert call(port, "POST", "/api/bridge-relay/poll", {}, host="evil.test")[0] == 403
    # a pairing lease can only be created after signing in
    assert call(port, "POST", "/api/connection", {"action": "enable"}, {"Origin": f"http://{NAS}:{port}"})[0] == 401
    _, data, cookies = sign_in(port)
    headers = session_headers(port, cookies, json.loads(data)["csrf"])
    status, body, _ = call(port, "POST", "/api/connection", {"action": "enable"}, headers)
    assert status == 200
    token = json.loads(body)["relay_token"]
    assert state.bridge_relay.authorized(token)
    # the lease key (not the login) authorises relay traffic: it passes the key check and fails only on the payload
    status, _, _ = call(port, "POST", "/api/bridge-relay/result", {"bad": 1}, {"X-Symport-Relay-Key": token, "Origin": f"http://{NAS}:{port}"})
    assert status == 400
    # the legacy shared-key route is not an authentication bypass in LAN mode
    key = "k" * 40
    import os
    os.environ["SYMPORT_BRIDGE_KEY"] = key
    try:
        status, _, _ = call(port, "POST", "/api/bridge", {"action": "list"}, {"X-Symport-Bridge-Key": key, "Origin": f"http://{NAS}:{port}"})
    finally:
        del os.environ["SYMPORT_BRIDGE_KEY"]
    assert status == 401


def test_secure_cookie_behind_https_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda _s: None)
    (tmp_path / "workspace").mkdir()
    port = _free_port()
    config = AccessConfig("lan", NAS, port, (NAS,), hash_password(PASSWORD, n=2**10), "https://symport.example.org")
    state = Workbench(tmp_path, config)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, data, response = call(port, "POST", "/api/auth/login", {"password": PASSWORD},
                                      {"Origin": "https://symport.example.org"}, host="symport.example.org")
        assert status == 200
        cookies = [v for h, v in response.getheaders() if h.lower() == "set-cookie"]
        assert cookies and all("Secure" in c and "HttpOnly" in c and "SameSite=Strict" in c for c in cookies)
        assert call(port, "GET", "/api/state", host="symport.example.org")[0] == 401   # proxy auth alone is not a session
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_localhost_mode_is_unchanged(tmp_path):
    (tmp_path / "workspace").mkdir()
    state = Workbench(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        local = f"127.0.0.1:{port}"
        assert call(port, "GET", "/api/state", host=local)[0] == 200
        assert call(port, "GET", "/healthz", host=local)[0] == 200
        assert call(port, "GET", "/api/state", host=f"{NAS}:{port}")[0] == 403          # LAN is closed without opt-in
        assert call(port, "POST", "/api/device-report", {"rm": "x"}, {"X-Symport-Token": state.token}, host=local)[0] == 200
        assert call(port, "POST", "/api/auth/login", {"password": PASSWORD}, host=local)[0] in (403, 404)
        assert call(port, "GET", "/api/auth/session", host=local)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
