"""End to end: SymPort in Home LAN + HTTPS mode ⇄ Windows Bridge (same code as the tray app) ⇄ a mounted phone volume.

No phone is attached: the "phone" is a directory registered as a Bridge volume, exactly how a Nokia mass-storage
drive is exposed. The test drives the same HTTP calls the browser makes (probe, pair, connection, manager).
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time

import pytest

import symport.web as web
from symport import tls
from symport.access import AccessConfig, hash_password
from symport.bridge.main import Agent
from symport.bridge.settings import load_key, save_config
from symport.web import Workbench, make_handler

PASSWORD = "a-long-private-secret"


def lan_address() -> str | None:
    """A second loopback address stands in for the NAS: it is inside the CA's name constraints, needs no network and
    is a different address from the Bridge's own 127.0.0.1."""
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.2", 0))
        return "127.0.0.2"
    except OSError:
        return None


@pytest.fixture
def stack(tmp_path, monkeypatch):
    ip = lan_address()
    if not ip:
        pytest.skip("127.0.0.2 cannot be bound on this system")
    monkeypatch.setattr(web.time, "sleep", lambda _s: None)
    for variable in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)  # the relay must reach the NAS directly, not via a CI proxy
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE", "SYMPORT_BRIDGE_URL", "SYMPORT_MGO_URL"):
        monkeypatch.setenv(variable, "")
    monkeypatch.setenv("SYMPORT_DEVICE_ROOTS", "{}")

    # --- SymPort, as the container runs it: Home LAN, HTTPS from its own local CA.
    repo = tmp_path / "repo"
    (repo / "workspace").mkdir(parents=True)
    material = tls.ensure_certificates(tmp_path / "tls", [ip])
    with socket.socket() as probe:
        probe.bind((ip, 0))
        port = probe.getsockname()[1]
    config = AccessConfig("lan", ip, port, (ip,), hash_password(PASSWORD, n=2**10), tls=True)
    state = Workbench(repo, config)
    state.tls_ca = material.ca_file
    server = tls.TlsThreadingHTTPServer((ip, port), make_handler(state))
    server.tls_context = tls.server_context(material)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # --- The Windows Bridge on "the PC" trusts that CA, the way installing it into Windows does.
    monkeypatch.setenv("SSL_CERT_FILE", str(material.ca_file))
    phone = tmp_path / "phone"
    (phone / "Images").mkdir(parents=True)
    (phone / "Images" / "IMG_0001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    root = tmp_path / "bridge"
    root.mkdir()
    save_config(root, {"schema": 1, "auto_usb": False, "volumes": {"phone": str(phone)}})
    agent = Agent(root, load_key(root), port=0, approve=lambda _data: True, auto=False)
    agent.start()
    try:
        yield ip, port, agent, state, ssl.create_default_context(cafile=str(material.ca_file))
    finally:
        agent.close()
        server.shutdown()
        server.server_close()


def https(ip, port, context, method, path, body=None, headers=None, timeout=60):
    conn = http.client.HTTPSConnection(ip, port, timeout=timeout, context=context)
    hdr = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=hdr)
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    cookies = [v.split(";")[0] for k, v in response.getheaders() if k.lower() == "set-cookie"]
    conn.close()
    return response.status, data, cookies


def bridge_post(agent, origin, body):
    port = agent.server.server_port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", "/api/bridge/pair", json.dumps(body),
                 {"Content-Type": "application/json", "Origin": origin, "Host": f"127.0.0.1:{port}"})
    response = conn.getresponse()
    result = response.status, json.loads(response.read() or b"{}")
    conn.close()
    return result


def test_browser_to_bridge_to_phone_over_https(stack):
    ip, port, agent, state, context = stack
    origin = f"https://{ip}:{port}"

    # 1. The Bridge sees the HTTPS SymPort origin (this is what the wizard's "Bridge not found" check does).
    assert bridge_post(agent, origin, {"origin": origin, "probe": True})[1]["probe"] is True
    # The previous plain-HTTP LAN origin was refused by the Bridge, which is why pairing failed before HTTPS.
    assert bridge_post(agent, f"http://{ip}:{port}", {"origin": f"http://{ip}:{port}", "probe": True})[0] in (400, 403)

    # 2. Sign in to SymPort over HTTPS and open a Bridge lease.
    status, session, cookies = https(ip, port, context, "POST", "/api/auth/login", {"password": PASSWORD}, {"Origin": origin})
    assert status == 200
    headers = {"Origin": origin, "Cookie": "; ".join(cookies), "X-Symport-Token": session["csrf"]}
    status, lease, _ = https(ip, port, context, "POST", "/api/connection", {"action": "enable"}, headers)
    assert status == 200 and lease["relay_token"]

    # 3. Pair: the Bridge asks the PC operator (approved here) and starts calling back over verified TLS.
    status, paired = bridge_post(agent, origin, {"origin": origin, "token": lease["relay_token"]})
    assert status == 200 and paired["paired"] is True

    # 4. Through SymPort the phone volume is visible and its files can be listed.
    deadline = time.monotonic() + 30
    devices = []
    while time.monotonic() < deadline:
        status, found, _ = https(ip, port, context, "POST", "/api/manager", {"action": "discover"}, headers)
        devices = [d["id"] for d in found.get("devices", [])] if status == 200 else []
        if "phone" in devices:
            break
        time.sleep(0.5)
    assert "phone" in devices, found
    status, listing, _ = https(ip, port, context, "POST", "/api/manager",
                               {"action": "list", "device": "phone", "path": "Images"}, headers)
    assert status == 200 and any(item["name"] == "IMG_0001.jpg" for item in listing["entries"]), listing

    # 5. Without the login, none of it is reachable.
    assert https(ip, port, context, "POST", "/api/manager", {"action": "discover"}, {"Origin": origin})[0] == 401

    # 6. Revoking the web access on the PC stops the relay.
    agent.revoke()
    assert agent.relay is None
