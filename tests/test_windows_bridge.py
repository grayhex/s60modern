"""Shared loopback API, outbound relay and Windows credential self-checks."""

import json
import threading
from http.client import HTTPConnection

import pytest

from symport.bridge.main import self_test
from symport.bridge.relay import validate_origin
from symport.bridge.server import BridgeServer
from symport.bridge.settings import load_key, protect
from symport.phone.relay import BrowserBridgeRelay


class Manager:
    def execute(self, data):
        return {"devices": [], "action": data["action"]}


@pytest.fixture
def bridge():
    server = BridgeServer(Manager(), "x" * 32, port=0, origins=["https://symport.example"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(3)


def call(server, body, headers=None, path="/api/bridge"):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(
        "POST",
        path,
        json.dumps(body),
        {"Content-Type": "application/json", "X-Symport-Bridge-Key": "x" * 32, **(headers or {})},
    )
    r = connection.getresponse()
    result = r.status, json.loads(r.read())
    connection.close()
    return result


def test_auth_host_origin_and_dangerous_confirmation(bridge):
    assert call(bridge, {"action": "discover"})[0] == 200
    for headers in [
        {"Host": "attacker.invalid"},
        {"Origin": "https://attacker.invalid"},
        {"X-Symport-Bridge-Key": "wrong"},
    ]:
        assert call(bridge, {"action": "discover"}, headers)[0] == 403
    assert call(bridge, {"action": "delete", "confirm": True})[0] == 403
    bridge.approve = lambda data: True
    assert call(bridge, {"action": "delete", "confirm": True})[0] == 200
    assert call(bridge, {"action": "delete", "confirm": False})[0] == 400


def test_pair_requires_origin_and_local_approval(bridge):
    received = []
    bridge.pair = lambda origin, token: received.append((origin, token)) or True
    data = {"origin": "https://symport.example", "token": "y" * 32}
    assert call(bridge, data, path="/api/bridge/pair")[0] == 400
    assert (
        call(
            bridge,
            dict(data, origin="https://attacker.invalid"),
            {"Origin": data["origin"]},
            "/api/bridge/pair",
        )[0]
        == 400
    )
    assert call(bridge, data, {"Origin": data["origin"]}, "/api/bridge/pair")[0] == 200
    assert received == [(data["origin"], data["token"])]
    assert call(bridge, data, {"Origin": data["origin"]}, "/api/bridge/pair")[0] == 429


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.invalid",
        "https://user:pass@remote.invalid",
        "https://remote.invalid/path",
        "file:///tmp/x",
        "https://remote.invalid?x",
    ],
)
def test_origin_rejects_untrusted_url_forms(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_dpapi_private_key(tmp_path):
    assert protect(protect(b"private-owner-value"), True) == b"private-owner-value"
    key = load_key(tmp_path)
    assert len(key) >= 32 and load_key(tmp_path) == key


def test_relay_only_issues_once_and_revokes():
    relay = BrowserBridgeRelay()
    assert relay.authorized(relay.token) and not relay.authorized("wrong")
    relay.last_seen = __import__("time").monotonic()
    output = []
    thread = threading.Thread(target=lambda: output.append(relay.execute({"action": "discover"})))
    thread.start()
    work = relay.poll()["request"]
    assert work["data"]["action"] == "discover"
    relay.complete({"id": work["id"], "result": {"devices": []}})
    thread.join(3)
    assert output == [{"devices": []}]
    with pytest.raises(ValueError):
        relay.complete({"id": work["id"], "result": {}})
    relay.close()
    assert not relay.authorized(relay.token)


def test_local_self_test(tmp_path, monkeypatch):
    for name in (
        "SYMPORT_BRIDGE_URL",
        "SYMPORT_GAMMU_CONFIG",
        "SYMPORT_CONNECTIONS_FILE",
        "SYMPORT_DEVICE_ROOTS",
        "SYMPORT_BRIDGE_DATA",
    ):
        monkeypatch.setenv(name, "")
    path = tmp_path / "report.json"
    self_test(path)
    result = json.loads(path.read_text())
    assert result["http_auth"] == "passed" and result["dns_rebinding"] == "passed"
    assert result["physical_device"] == "not-tested"


def test_serial_discovery_filters_vendor_and_reports_only_candidates(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from symport.bridge.discovery import serial_candidates
    serial = ModuleType('serial')
    tools = ModuleType('serial.tools')
    tools.list_ports = SimpleNamespace(comports=lambda: [
        SimpleNamespace(vid=0x0421, manufacturer='Nokia', description='E52 USB', product='', device='COM7'),
        SimpleNamespace(vid=1, manufacturer='Other', description='Device', product='', device='COM8')])
    monkeypatch.setitem(sys.modules, 'serial', serial)
    monkeypatch.setitem(sys.modules, 'serial.tools', tools)
    candidates, error = serial_candidates()
    assert not error and [c['port'] for c in candidates] == ['COM7']
    assert candidates[0]['model_profile']['measured'] is False


def test_auto_volume_write_rechecks_physical_identity(monkeypatch):
    from symport.bridge.main import Agent
    agent = object.__new__(Agent)
    agent._enrolled_roots = {'auto-card-e': 'E:/'}
    agent._enrolled_identities = {'auto-card-e': 'original-disk-hash'}
    monkeypatch.setattr('symport.bridge.main.windows_nokia_volumes', lambda: ([{'root':'E:/','identity':'replacement-disk'}], None))
    with pytest.raises(ValueError, match='identity changed'):
        agent.guard_volume({'device':'auto-card-e','action':'upload'})
    # Reads do not change the phone; user-enrolled roots follow existing file safety.
    agent.guard_volume({'device':'auto-card-e','action':'list'})
    agent.guard_volume({'device':'manually-enrolled','action':'upload'})


def test_relay_expiry_blocks_new_work():
    import time
    relay = BrowserBridgeRelay()
    relay.expires = time.monotonic()-1
    assert not relay.authorized(relay.token) and not relay.status()['connected']
    with pytest.raises(ValueError, match='disconnected'):
        relay.execute({'action':'upload'})


def test_websocket_host_origin_auth_and_status_only():
    pytest.importorskip('websockets.sync.server')
    from types import SimpleNamespace

    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect

    from symport.bridge.events import start_events
    agent = SimpleNamespace(stop=threading.Event(), status={'state':'ready','connected':0}, server=SimpleNamespace(origins={'https://symport.example'}))
    events = start_events(agent, 'x'*32, 0)
    url = f'ws://127.0.0.1:{events.socket.getsockname()[1]}/api/bridge/events'
    try:
        with pytest.raises(InvalidStatus):
            connect(url, origin='https://untrusted.invalid', proxy=None)
        with pytest.raises(InvalidStatus):
            connect(url, additional_headers={'Host':'rebind.invalid'}, proxy=None)
        with connect(url, origin='https://symport.example', proxy=None) as socket:
            socket.send(json.dumps({'token':'wrong'}))
            with pytest.raises(ConnectionClosed):
                socket.recv(timeout=3)
        with connect(url, origin='https://symport.example', proxy=None) as socket:
            socket.send(json.dumps({'token':'x'*32}))
            result = json.loads(socket.recv(timeout=3))
            assert result == {'schema':'symport.bridge.events/1','type':'status','status':{'state':'ready','connected':0}}
    finally:
        agent.stop.set()
        events.shutdown()


def test_backend_pairing_lease_requires_owner_and_revokes(tmp_path):
    from http.server import ThreadingHTTPServer

    from symport.web import Workbench, make_handler
    from tests.test_web import _http
    state = Workbench(tmp_path)
    original = state.devices.transport
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    owner = {'X-Symport-Token': state.token}
    try:
        assert _http(port, 'POST', '/api/connection', {'action':'enable'})[0] == 403
        status, raw = _http(port, 'POST', '/api/connection', {'action':'enable'}, owner)
        assert status == 200
        lease = json.loads(raw)
        assert lease['relay_token'] != state.token
        assert _http(port, 'POST', '/api/bridge-relay/poll', {}, {'X-Symport-Relay-Key':'wrong'})[0] == 403
        state.bridge_relay.pending = {'id':'test', 'data':{'action':'discover'}}
        status, raw = _http(port, 'POST', '/api/bridge-relay/poll', {}, {'X-Symport-Relay-Key':lease['relay_token']})
        assert status == 200 and json.loads(raw)['request']['id'] == 'test'
        assert _http(port, 'POST', '/api/connection', {'action':'disconnect'}, owner)[0] == 200
        assert state.devices.transport is original
        assert _http(port, 'POST', '/api/bridge-relay/poll', {}, {'X-Symport-Relay-Key':lease['relay_token']})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_settings_window_runs_in_its_own_process(tmp_path, monkeypatch):
    """Tk used to share a thread with the tray icon and the window could not be closed. It now owns a process."""
    import sys

    from symport.bridge import main as bridge_main
    from symport.bridge import tray

    command = tray.settings_command(tmp_path, 24652)
    assert command[0] == sys.executable and "--settings" in command
    assert command[command.index("--data-dir") + 1] == str(tmp_path) and command[-1] == "24652"
    seen = {}
    monkeypatch.setattr(tray, "settings_window", lambda root, port: seen.update(root=root, port=port) or tray.SETTINGS_SAVED)
    assert bridge_main.main(["--settings", "--data-dir", str(tmp_path), "--port", "24999"]) == tray.SETTINGS_SAVED
    assert seen == {"root": tmp_path.absolute(), "port": 24999}


def test_relay_failures_are_classified_without_leaking_details():
    import ssl
    from urllib.error import HTTPError, URLError

    from symport.bridge.relay import classify, is_local_network

    def verify_error(code):
        error = ssl.SSLCertVerificationError("verify failed")
        error.verify_code = code
        return URLError(error)

    assert classify(verify_error(18)) == "tls-untrusted"
    assert classify(URLError(ssl.SSLCertVerificationError("self-signed"))) == "tls-untrusted"
    assert classify(verify_error(62)) == "tls-hostname"
    assert classify(verify_error(10)) == "tls-expired"
    assert classify(URLError(ssl.SSLError("handshake"))) == "tls-error"
    assert classify(URLError(ConnectionRefusedError())) == "unreachable"
    import io

    def refused(code):
        body = io.BytesIO(json.dumps({"error": "x", "code": code}).encode())
        return HTTPError("https://x", 403, "no", {}, body)

    assert classify(HTTPError("https://x", 403, "no", {}, None)) == "rejected"
    assert classify(refused("lease-expired")) == "lease-expired"
    assert classify(refused("bad-token")) == "bad-token"
    assert classify(refused("lease-missing")) == "lease-missing"
    assert classify(refused("something-else")) == "rejected"
    assert classify(HTTPError("https://x", 500, "no", {}, None)) == "server-error"
    assert all(is_local_network(h) for h in ("192.168.1.5", "10.0.0.2", "127.0.0.1", "nas.home.arpa", "localhost"))
    assert not any(is_local_network(h) for h in ("symport.example.org", "8.8.8.8"))
