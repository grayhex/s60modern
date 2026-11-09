"""HTTPS for Home LAN mode: a private local CA, a server certificate for the NAS address and a TLS-wrapped server.

Why: a browser may let an http page on a LAN address talk to the Windows Bridge on 127.0.0.1 only from a secure
context, and the Bridge itself only pairs with an https SymPort origin. The CA is created on the first start and
kept (0600) in the data volume. Its public certificate can be downloaded from the login page and installed once on
the computer that runs the browser and the Windows Bridge; it is restricted by X.509 name constraints to private
network addresses and the names you configured, so it cannot vouch for public web sites.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import ssl
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_NAME = "SymPort Local CA"
PRIVATE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16",
                    "fc00::/7", "fe80::/10", "::1/128")  # RFC 1918, loopback, CGNAT/VPN, link-local, IPv6 local
PRIVATE_DNS = ("localhost", ".local", ".lan", ".home.arpa", ".home", ".internal")
SERVER_DAYS = 397
RENEW_BEFORE_DAYS = 30


@dataclass(frozen=True)
class TlsMaterial:
    cert_file: Path
    key_file: Path
    ca_file: Path | None  # None when the operator supplied their own certificate


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _dns_covered(name: str, permitted: list[str]) -> bool:
    return any(name == p or (p.startswith(".") and name.endswith(p)) for p in permitted)


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
    temporary.replace(path)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _create_ca(directory: Path, extra_dns: list[str]) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SymPort"),
                      x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    permitted = [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETWORKS]
    dns = sorted(set(PRIVATE_DNS) | set(extra_dns))
    permitted += [x509.DNSName(d) for d in dns]
    now = _now()
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(directory / "ca.key", _key_pem(key))
    (directory / "ca.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "ca.json").write_text(json.dumps({"dns": dns}), encoding="utf-8")


def _san(hosts: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for host in [*hosts, "localhost", "127.0.0.1"]:
        entry = x509.IPAddress(ipaddress.ip_address(host)) if _is_ip(host) else x509.DNSName(host)
        if entry not in names:
            names.append(entry)
    return names


def _server_valid(directory: Path, hosts: list[str]) -> bool:
    try:
        certificate = x509.load_pem_x509_certificate((directory / "server.crt").read_bytes())
        ca = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes())
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(v) for v in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
        if not set(hosts) <= have or certificate.issuer != ca.subject:
            return False
        ca.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))
        return certificate.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS)
    except (OSError, ValueError, KeyError, x509.ExtensionNotFound, Exception):  # any damage → issue again
        return False


def ensure_certificates(directory: Path, hosts: list[str]) -> TlsMaterial:
    """Create (or reuse) the CA and a server certificate valid for `hosts`. The CA is replaced only when a configured
    DNS name is outside its name constraints; the operator then has to install the new CA again."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    hosts = [h.lower() for h in hosts]
    dns_hosts = [h for h in hosts if not _is_ip(h)]
    try:
        stored = json.loads((directory / "ca.json").read_text(encoding="utf-8"))["dns"]
        ca_ok = (directory / "ca.crt").is_file() and (directory / "ca.key").is_file() \
            and all(_dns_covered(h, stored) for h in dns_hosts)
    except (OSError, ValueError, KeyError):
        ca_ok = False
    if not ca_ok:
        for leftover in ("server.crt", "server.key"):
            (directory / leftover).unlink(missing_ok=True)
        _create_ca(directory, dns_hosts)
    if not _server_valid(directory, hosts):
        ca_key = serialization.load_pem_private_key((directory / "ca.key").read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes())
        key = ec.generate_private_key(ec.SECP256R1())
        now = _now()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0] if hosts else "symport")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName(_san(hosts)), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(directory / "server.key", _key_pem(key))
        (directory / "server.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return TlsMaterial(directory / "server.crt", directory / "server.key", directory / "ca.crt")


def server_context(material: TlsMaterial) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(material.cert_file, material.key_file)
    return context


class TlsThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose connections are TLS. The handshake runs in the request thread, so a slow or
    plain-HTTP client cannot stall the accept loop."""

    tls_context: ssl.SSLContext | None = None

    def get_request(self):
        connection, address = super().get_request()
        if self.tls_context is not None:
            connection.settimeout(30)
            connection = self.tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
        return connection, address

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return  # plain-HTTP probes, resets and aborted handshakes are not worth a traceback
        super().handle_error(request, client_address)
