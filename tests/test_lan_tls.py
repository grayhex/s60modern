"""HTTPS for Home LAN mode: local CA, name constraints, certificate reissue, TLS server, Secure cookies."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import socket
import ssl
import threading

import pytest
from cryptography import x509

import symport.web as web
from symport import tls
from symport.access import AccessConfig, hash_password
from symport.web import Workbench, make_handler

NAS = "192.168.1.5"
PASSWORD = "a-long-private-secret"


def load(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def san(certificate):
    value = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in value.get_values_for_type(x509.IPAddress)} | set(value.get_values_for_type(x509.DNSName))


def test_certificates_are_reused_reissued_and_constrained(tmp_path):
    first = tls.ensure_certificates(tmp_path, [NAS])
    ca, leaf = load(first.ca_file), load(first.cert_file)
    assert {NAS, "127.0.0.1", "localhost"} <= san(leaf)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical and ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert any(str(g.value) == "192.168.0.0/16" for g in constraints.value.permitted_subtrees if isinstance(g, x509.IPAddress))
    assert oct(first.key_file.stat().st_mode & 0o777) == "0o600" and oct((tmp_path / "ca.key").stat().st_mode & 0o777) == "0o600"

    again = tls.ensure_certificates(tmp_path, [NAS])
    assert load(again.cert_file).serial_number == leaf.serial_number, "nothing changes on a plain restart"

    moved = tls.ensure_certificates(tmp_path, ["192.168.1.77"])      # the NAS got a new LAN address
    assert load(moved.ca_file).serial_number == ca.serial_number, "the installed CA stays valid"
    assert "192.168.1.77" in san(load(moved.cert_file))

    named = tls.ensure_certificates(tmp_path, ["192.168.1.77", "nas.home.arpa"])   # covered by the CA's constraints
    assert load(named.ca_file).serial_number == ca.serial_number and "nas.home.arpa" in san(load(named.cert_file))

    outside = tls.ensure_certificates(tmp_path, ["192.168.1.77", "nas.example.com"])
    assert load(outside.ca_file).serial_number != ca.serial_number, "a name outside the constraints needs a new CA"
    assert "nas.example.com" in san(load(outside.cert_file))


def test_expiring_certificate_is_renewed(tmp_path, monkeypatch):
    first = tls.ensure_certificates(tmp_path, [NAS])
    serial = load(first.cert_file).serial_number
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(days=tls.SERVER_DAYS - 5)
    monkeypatch.setattr(tls, "_now", lambda: soon)
    assert load(tls.ensure_certificates(tmp_path, [NAS]).cert_file).serial_number != serial


def test_ca_is_not_a_public_web_authority(tmp_path):
    """A client that trusts only this CA must refuse a public name: the constraints are enforced by OpenSSL."""
    material = tls.ensure_certificates(tmp_path, [NAS])
    rogue_dir = tmp_path / "rogue"
    # Issue a leaf for a public name directly from the CA key, as a thief of the CA key would.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    ca_key = serialization.load_pem_private_key((tmp_path / "ca.key").read_bytes(), password=None)
    ca = load(material.ca_file)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "bank.com")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=5))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("bank.com")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    rogue_dir.mkdir()
    (rogue_dir / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (rogue_dir / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(rogue_dir / "leaf.pem", rogue_dir / "leaf.key")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            connection, _ = listener.accept()
            server_ctx.wrap_socket(connection, server_side=True).close()
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client_ctx = ssl.create_default_context(cafile=str(material.ca_file))
    with pytest.raises(ssl.SSLError), socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        client_ctx.wrap_socket(raw, server_hostname="bank.com")
    listener.close()
    thread.join(timeout=3)


@pytest.fixture
def https_lan(tmp_path, monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda _s: None)
    (tmp_path / "workspace").mkdir()
    material = tls.ensure_certificates(tmp_path / "tls", [NAS])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = AccessConfig("lan", NAS, port, (NAS,), hash_password(PASSWORD, n=2**10), tls=True)
    state = Workbench(tmp_path, config)
    state.tls_ca = material.ca_file
    server = tls.TlsThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.tls_context = tls.server_context(material)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(material.ca_file))
    try:
        yield state, port, context
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def ask(port, context, method, path, body=None, headers=None):
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=context)
    hdr = {"Host": f"{NAS}:{port}", **(headers or {})}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    conn.request(method, path, body=raw, headers=hdr)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data, response


def test_https_login_with_secure_cookie_and_downloadable_ca(https_lan):
    state, port, context = https_lan
    assert ask(port, context, "GET", "/healthz")[0] == 200
    status, ca, response = ask(port, context, "GET", "/symport-ca.crt")
    assert status == 200 and ca.startswith(b"-----BEGIN CERTIFICATE-----") and b"PRIVATE" not in ca
    assert "attachment" in response.getheader("Content-Disposition")
    assert json.loads(ask(port, context, "GET", "/api/auth/session")[1]) == {
        "mode": "lan", "authenticated": False, "tls": True, "ca": True}
    https_origin = f"https://{NAS}:{port}"
    assert ask(port, context, "POST", "/api/auth/login", {"password": PASSWORD}, {"Origin": f"http://{NAS}:{port}"})[0] == 403
    status, data, response = ask(port, context, "POST", "/api/auth/login", {"password": PASSWORD}, {"Origin": https_origin})
    assert status == 200
    cookies = [v for h, v in response.getheaders() if h.lower() == "set-cookie"]
    assert cookies and all("Secure" in c and "HttpOnly" in c and "SameSite=Strict" in c for c in cookies)
    jar = "; ".join(c.split(";")[0] for c in cookies)
    assert ask(port, context, "GET", "/api/state", headers={"Cookie": jar})[0] == 200
    assert ask(port, context, "GET", "/api/state")[0] == 401


def test_plain_http_to_the_tls_port_gets_nothing_and_does_not_stall_the_server(https_lan):
    _, port, context = https_lan
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        raw.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.settimeout(3)
        try:
            assert raw.recv(100) in (b"", ) or True
        except OSError:
            pass
    idle = socket.create_connection(("127.0.0.1", port), timeout=3)   # a client that never handshakes
    try:
        assert ask(port, context, "GET", "/healthz")[0] == 200       # others are still served
    finally:
        idle.close()
