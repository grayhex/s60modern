"""Compact local Windows/Linux bridge; all phone operations reuse DeviceManager."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from symport.phone import DeviceManager, gammu_native
from symport.phone.connections import load_profiles
from symport.phone.local import LocalTransports

from . import VERSION, doctor, routes
from .discovery import (
    documented_model,
    host_candidates,
    serial_candidates,
    usb_devices,
    windows_nokia_volumes,
)
from .server import BridgeServer
from .settings import data_directory, load_config, load_key

SCAN_BASE, SCAN_MAX = 30, 600  # seconds between AT identify cycles: 30 → doubled while silent → 10 minutes


def is_windows() -> bool:
    return os.name == "nt"


def pem_fingerprint(pem: str) -> str:
    """SHA-256 of the DER form of a single PEM certificate (what SymPort announces); stdlib only."""
    import ssl

    if pem.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Expected exactly one certificate")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.strip())).hexdigest()


class Agent:
    def __init__(self, root: Path, key: str, port=24652, approve=None, auto=True):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ["SYMPORT_BRIDGE_DATA"] = str(root)
        os.environ["SYMPORT_BRIDGE_URL"] = ""  # this endpoint always owns local hardware
        self.manager = DeviceManager(root)
        self.config = load_config(root)
        self.stop = threading.Event()
        self.last_refresh = None
        self.status = {"state": "starting", "connected": 0, "identified": 0, "candidates": 0, "usb_seen": 0,
                       "diagnostics": []}
        self.host: dict | None = None  # last host_candidates() snapshot (USB/COM/drives/Bluetooth), never phone data
        self.host_at = 0.0
        self._usb_at = 0.0
        self._usb_signature = None
        self.com: dict[str, str] = {}
        self._com_at = 0.0
        # AT identify polling backs off while nothing answers (a phone that never answers must not be hammered through
        # its modem channel); it starts over on a port-set/USB change or Rescan.
        self._scan_delay = SCAN_BASE
        self.at_failed: set[str] = set()  # ports that opened but whose identify got no answer
        # Nokia-specific Gammu connection (DKU-2 / DKU-2 Phonet): found through the Nokia USB driver, no COM port needed.
        self.dku2_state = "not-tried"
        # Last Gammu error code (fixed list, see routes.GAMMU_CODES) of an identify that failed; '' when none or after a success.
        self.gammu_error = ""
        # Which route serves which job (routes.choose) and what the transport shows on its rows; changes are journalled.
        self.selection: dict | None = None
        self.selection_logged = False
        self.route_hints: dict = {"states": {}, "recommended_for": {}}
        self._dku2_key = None
        self._dku2_at = 0.0
        self._dku2_cache: list[dict] = []
        self._signature = None
        self._ports = None
        self._auto_profiles = []
        self._auto_roots = {}
        self._last_scan = 0
        self.server = BridgeServer(self.manager, key, port, self.config.get("origins", []), approve)
        self.auto = auto
        self.events = None
        self.started = False
        self.relay = None
        self._relays = []
        self._volume_identities = {}
        self._enrolled_roots = {}
        self._enrolled_identities = {}
        self.manager.before_execute = self.guard_volume
        self.refresh_config()
        self.server.pair = self.pair
        self.server.relay_state = self.relay_state

    def guard_volume(self, data):
        identifier = data.get("device")
        if identifier not in self._enrolled_roots or data.get("action") in (
            "list",
            "download",
            "backup",
            "restore-preview",
            "media-list",
            "media-info",
            "media-chunk",
            "media-thumbnail",
            "apps-inventory",
            "connection-diagnose",
        ):
            return
        rows, reason = windows_nokia_volumes()
        expected = self._enrolled_identities.get(identifier)
        if (
            reason
            or not expected
            or not any(
                r["root"] == self._enrolled_roots[identifier] and r["identity"] == expected
                for r in rows
            )
        ):
            raise ValueError(
                "The Nokia USB storage volume identity changed. Reconnect and discover the phone before writing."
            )

    def revoke(self):
        if self.relay:
            self.relay.stop.set()
            self.relay = None

    def relay_state(self, origin):
        relay = self.relay
        return relay.public_state() if relay and relay.origin == origin else None

    def pin_ca(self, origin, fingerprint):
        """Fetch SymPort's public CA from the origin the operator just approved and keep it only if it has exactly
        the fingerprint the web page announced. Verification against it replaces installing it in Windows.
        Standard library only: the frozen exe is built without `cryptography`."""
        import ssl
        from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

        from .relay import is_local_network

        if not origin.startswith("https://"):
            raise ValueError("A pinned CA applies to https origins")
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # public certificate; checked by hash below
        handlers = [HTTPSHandler(context=context)]
        if is_local_network(urlsplit(origin).hostname or ""):
            handlers.append(ProxyHandler({}))
        with build_opener(*handlers).open(Request(origin + "/symport-ca.crt"), timeout=15) as reply:
            raw = reply.read(16385)
        if len(raw) > 16384:
            raise ValueError("Unexpected certificate size")
        digest = pem_fingerprint(raw.decode("ascii"))
        if digest != fingerprint:
            raise ValueError("The certificate does not match the fingerprint shown by SymPort")
        target = self.root / "trusted-ca"
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = target / (digest + ".pem")
        path.write_text(raw.decode("ascii"), encoding="ascii")
        return path

    def pair(self, origin, token, ca_sha256=None):
        shown = (" Certificate fingerprint " + ca_sha256[:16] + "…") if ca_sha256 else ""
        if not self.server.approve or not self.server.approve(
            {
                "action": "Connect SymPort at "
                + origin
                + " for up to two hours. Phone data will be sent to this server."
                + shown
            }
        ):
            return False
        from .relay import OutboundRelay

        ca_file = self.pin_ca(origin, ca_sha256) if ca_sha256 else None
        self.revoke()
        self.relay = OutboundRelay(self, origin, token, ca_file)
        self._relays = [r for r in self._relays if r.thread.is_alive()]
        self._relays.append(self.relay)
        self.relay.thread.start()
        return True

    def refresh_config(self):
        config = load_config(self.root)
        profiles = load_profiles(config.get("connections_file"))
        profiles += self._auto_profiles
        roots = {**config.get("volumes", {}), **self._auto_roots}
        signature = hashlib.sha256(
            json.dumps([config, profiles, roots, self._volume_identities], sort_keys=True).encode()
        ).hexdigest()
        if signature != self._signature:
            with self.manager.lock:
                for identifier in set(self._enrolled_identities) | set(self._volume_identities):
                    if self._enrolled_identities.get(identifier) != self._volume_identities.get(identifier):
                        self.manager.observed.proofs.pop(identifier, None)
                        self.manager.rows.pop(identifier, None)
                self.config = config
                os.environ["SYMPORT_DEVICE_ROOTS"] = json.dumps(roots)
                os.environ["SYMPORT_GAMMU_CONFIG"] = config.get("gammu_config", "")
                generated = self.root / "connections.json"
                temporary = self.root / "connections.tmp"
                with temporary.open("w", encoding="utf-8") as file:
                    os.chmod(temporary, 0o600)
                    json.dump({"schema": 1, "connections": profiles}, file)
                temporary.replace(generated)
                os.environ["SYMPORT_CONNECTIONS_FILE"] = str(generated)
                previous = getattr(self.manager, "transport", None)
                self.manager.transport = LocalTransports(self.root, volume_identities=self._volume_identities)
                self.manager.transport.route_hints = getattr(self, "route_hints", {})
                closer = getattr(previous, "close", None)
                if callable(closer):
                    closer()  # a replaced transport must not keep a phone store or an interface open
                self._enrolled_roots = dict(self._auto_roots)
                self._enrolled_identities = dict(self._volume_identities)
                self.server.origins = set(config.get("origins", []))
                self._signature = signature
                self.last_refresh = None

    def _reset_scan(self):
        """USB hotplug or Rescan: look again at once and start the back-off over."""
        self._last_scan, self._ports, self._scan_delay = 0, None, SCAN_BASE

    def scan(self):
        candidates, reason = serial_candidates()
        ports = tuple(c["port"] for c in candidates)
        if ports != self._ports:
            self._scan_delay = SCAN_BASE
        if ports == self._ports and time.monotonic() - self._last_scan < self._scan_delay:
            return
        if self.stop.is_set() or self.manager.lock.locked():
            return  # closing, or another phone operation is running: do not start an identify on top of it
        self._ports = ports
        self._last_scan = time.monotonic()
        profiles = []
        if self.config.get("auto_usb", True) and not self.config.get("gammu_config"):
            # Modem port first; the Java comm port is not an AT port and is never opened.
            for candidate in [c for c in candidates if c["kind"] != "javacomm"]:
                if self.com.get(candidate["port"]) == "busy" or self.stop.is_set():
                    continue  # another program holds it: identify would only time out
                name = "auto-usb-" + hashlib.sha256(candidate["port"].encode()).hexdigest()[:8]
                config = self.root / (name + ".cfg")
                with config.open("w", encoding="utf-8") as file:
                    os.chmod(config, 0o600)
                    file.write("[gammu]\ndevice = " + candidate["port"] + "\nconnection = at\n")
                try:
                    with self.manager.lock:
                        observed = gammu_native.invoke(str(config), "identify")
                    profile = documented_model(str(observed.get("model") or ""))
                    if str(observed.get("manufacturer", "")).casefold() != "nokia" or not profile:
                        continue
                    self.at_failed.discard(candidate["port"])
                    self.gammu_error = ""
                    profiles.append(
                        {
                            "id": name,
                            "name": "Nokia " + profile["model"] + " · USB",
                            "kind": "gammu-usb",
                            "config": str(config),
                            "manufacturer": "Nokia",
                            "platform": "S60",
                            "edition": profile["edition"],
                        }
                    )
                except (ValueError, OSError) as exc:
                    self.at_failed.add(candidate["port"])  # the port opened but nobody answered
                    self.gammu_error = routes.gammu_code(exc) or self.gammu_error
                    continue
        volumes, disk_reason = (
            windows_nokia_volumes() if self.config.get("auto_usb", True) else ([], None)
        )
        roots = {}
        self._volume_identities = {}
        for item in volumes:
            if item["model_profile"]:
                identifier = "auto-card-" + item["root"][0].lower()
                roots[identifier] = item["root"]
                self._volume_identities[identifier] = item["identity"]
        profiles += self._dku2_profiles(profiles)
        self._scan_delay = SCAN_BASE if profiles else min(self._scan_delay * 2, SCAN_MAX)
        self._auto_profiles, self._auto_roots = profiles, roots
        self.status["candidates"] = len(candidates) + len(volumes)
        self.status["diagnostics"] = [r for r in (reason, disk_reason) if r]
        self.refresh_config()

    # ---- Driver & Connection Doctor support -------------------------------------------------------------------
    def _rescan_requested(self) -> bool:
        marker = self.root / "rescan.request"
        try:
            marker.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    @staticmethod
    def _usb_key(rows):
        return sorted((r["vid"], r["pid"], r["class"], r["status"]) for r in rows)

    def refresh_host(self, force=False) -> bool:
        """Keep a recent view of what Windows sees. USB is polled every 10 s (hotplug), everything else every 60 s or on
        demand. Returns True when the set of Nokia USB devices changed."""
        now = time.monotonic()
        changed = False
        if force or self.host is None or now - self.host_at >= 60:
            self.host, self.host_at, self._usb_at = host_candidates(), now, now
            key = self._usb_key(self.host["usb"])
            changed = self._usb_signature is not None and key != self._usb_signature
            self._usb_signature = key
        elif now - self._usb_at >= 10:
            usb, _ = usb_devices()
            self._usb_at = now
            key = self._usb_key(usb)
            changed = key != self._usb_signature
            self._usb_signature = key
            self.host["usb"] = usb
        self.status["usb_seen"] = len(self.host["usb"])
        # Probe COM ports only while nothing is identified and no phone operation holds the manager.
        ports = [p["port"] for p in self.host["serial"] if p.get("kind") != "javacomm"]
        if self.status.get("identified", 0) == 0 and (
            force or changed or set(self.com) != set(ports) or now - self._com_at >= self._scan_delay
        ) and self.manager.lock.acquire(blocking=False):
            try:
                self.com = {p: doctor.com_state(p, silent=p in self.at_failed) for p in ports}
                self._com_at = now
            finally:
                self.manager.lock.release()
        elif self.status.get("identified", 0) > 0:
            self.com = {}
        return changed

    def doctor_report(self) -> dict:
        relay = self.relay.public_state() if self.relay else None
        origin = self.relay.origin if self.relay else None
        # A port that opens fine but whose AT identify got no answer is "silent", not just "free".
        com = {p: ("silent" if state == "free" and p in self.at_failed else state) for p, state in self.com.items()}
        return doctor.diagnose(self.host or {}, com, int(self.status.get("identified", 0)), relay, origin,
                               dku2=self.dku2_state, gammu_error=self.gammu_error, pccs=self.pccs_status(),
                               preference=self.preference(), gammu_tried=self.gammu_tried(), selection=self.selection)

    # ---- routes: which way to the phone serves which job ------------------------------------------------------------
    def preference(self) -> str:
        value = self.config.get("transport_preference", "auto")
        return value if value in routes.PREFERENCES else "auto"

    def pccs_status(self) -> dict | None:
        adapter = getattr(self.manager.transport, "pccs", None)
        return adapter.route_status() if adapter is not None else None

    def gammu_tried(self) -> bool:
        """Every Gammu attempt is over: all AT ports answered nothing, or (no AT port) the DKU-2 connection did not answer."""
        ports = [p["port"] for p in (self.host or {}).get("serial", []) if p.get("kind") != "javacomm"]
        return (bool(ports) and all(p in self.at_failed for p in ports)) or self.dku2_state == "no-response"

    def route_states(self) -> dict:
        com = {p: ("silent" if state == "free" and p in self.at_failed else state) for p, state in self.com.items()}
        return doctor.route_states(self.host or {}, com, int(self.status.get("identified", 0)), self.dku2_state, self.pccs_status())

    def update_route_selection(self) -> bool:
        """Choose the route per job. Never while a phone operation holds the manager (the choice is kept and applied on the
        next pass). A real change is written to the operations journal: action, time, status, no phone data."""
        states = self.route_states()
        chosen = routes.choose(self.preference(), states, gammu_tried=self.gammu_tried(),
                               operation_in_progress=self.manager.lock.locked(), current=self.selection)
        if chosen.get("deferred"):
            return False
        before = {job: (self.selection or {}).get(job) for job in routes.JOBS}
        after = {job: chosen.get(job) for job in routes.JOBS}
        self.selection = chosen
        self.route_hints.clear()
        self.route_hints.update(states=states, recommended_for=routes.recommended_for(chosen), dead_end=chosen["dead_end"],
                                preference=chosen["preference"])
        if after != before and (self.selection_logged or any(after.values())):
            self.selection_logged = True
            try:
                self.manager.record("route-switch", "success")
            except OSError:
                pass
        return after != before

    def write_snapshot(self) -> None:
        """The diagnostics window (a separate process) reads this file; it contains only the safe report."""
        target = self.root / "diagnostics.json"
        temporary = self.root / "diagnostics.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as file:
                os.chmod(temporary, 0o600)
                json.dump(self.doctor_report(), file, ensure_ascii=False)
            temporary.replace(target)
        except OSError:
            pass

    DKU2_MODES = ("dku2", "dku2phonet")

    def _dku2_profiles(self, at_profiles) -> list[dict]:
        """Short, read-only Gammu identify over the Nokia-specific USB connection when Windows shows a working Nokia USB
        device and the AT route gave no answer: no COM port at all, or COM ports that stay silent. Tried again only when
        the USB device set changes or Rescan is pressed. Whether a given phone answers over DKU-2 with the installed
        Nokia driver is hardware-dependent and unverified: a failure just records `no-response`; nothing is changed in
        Windows."""
        if not is_windows() or at_profiles or self.stop.is_set() or not gammu_native.available():
            return []
        if not self.config.get("auto_usb", True) or self.config.get("gammu_config"):
            return []
        usb = [r for r in (self.host or {}).get("usb", []) if r.get("status") == "ok"]
        key = tuple(sorted((r["vid"], r["pid"], r["class"], r.get("mi") or "") for r in usb))
        if not key:
            self.dku2_state, self._dku2_cache = "not-tried", []
            return []
        if key == self._dku2_key and (self._dku2_cache or time.monotonic() - self._dku2_at < 3600):
            return self._dku2_cache
        self._dku2_key, self._dku2_at, found = key, time.monotonic(), []
        last_code = ""
        for mode in self.DKU2_MODES:
            config = self.root / f"auto-{mode}.cfg"
            with config.open("w", encoding="utf-8") as file:
                os.chmod(config, 0o600)
                file.write(f"[gammu]\nconnection = {mode}\n")
            try:
                with self.manager.lock:
                    observed = gammu_native.invoke(str(config), "identify")
            except (ValueError, OSError) as exc:
                last_code = routes.gammu_code(exc) or last_code
                continue
            profile = documented_model(str(observed.get("model") or ""))
            if str(observed.get("manufacturer", "")).casefold() == "nokia" and profile:
                found.append(
                    {
                        "id": "auto-" + mode,
                        "name": "Nokia " + profile["model"] + " · USB (" + mode + ")",
                        "kind": "gammu-usb",
                        "config": str(config),
                        "manufacturer": "Nokia",
                        "platform": "S60",
                        "edition": profile["edition"],
                    }
                )
                break
        self._dku2_cache = found
        self.gammu_error = "" if found else (last_code or self.gammu_error)
        self.dku2_state = ("identified:" + found[0]["id"].removeprefix("auto-")) if found else "no-response"
        return found

    def monitor(self):
        while not self.stop.is_set():
            try:
                forced = self._rescan_requested()
                if forced:
                    self._reset_scan()
                    self.last_refresh = None
                    self._com_at = 0.0
                    self._dku2_key = None  # Rescan: try the Nokia-specific connection again
                if self.auto:
                    self.scan()
                else:
                    self.refresh_config()
                if self.last_refresh is None or time.monotonic() - self.last_refresh >= 30:
                    data = self.manager.execute({"action": "discover"})
                    devices = data["devices"]
                    self.status.update(
                        state="ready",
                        connected=sum(d.get("connected") is True for d in devices),
                        identified=sum(
                            d.get("connected") is True and d.get("transport") != "mounted-volume" and bool(d.get("model"))
                            for d in devices
                        ),
                    )
                    self.last_refresh = time.monotonic()
                if self.refresh_host(forced):
                    # A Nokia was plugged in or removed: look again at once instead of waiting for the next cycle.
                    self._reset_scan()
                    self.last_refresh = None
            except (ValueError, OSError):
                self.status.update(state="connection-error", connected=0)
            self.update_route_selection()
            self.write_snapshot()
            self.stop.wait(3)

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True, name="bridge-http").start()
        self.started = True
        threading.Thread(target=self.monitor, daemon=True, name="bridge-discovery").start()
        if self.config.get("websocket", False):
            from .events import start_events

            try:
                self.events = start_events(self, self.server.key, self.server.server_port + 2)
            except OSError:
                self.status["diagnostics"].append(
                    "WebSocket status port is unavailable; authenticated HTTP remains active"
                )

    def close(self):
        self.stop.set()
        gammu_native.terminate_workers()  # an identify in flight must not keep a COM port open after the Bridge is gone
        closer = getattr(self.manager.transport, "close", None)
        if callable(closer):
            closer()  # cancels a running USB OBEX read; its CloseStore/Disconnect are sent before this returns
        self.revoke()
        if self.started:
            self.server.shutdown()
        self.server.server_close()
        if self.events:
            self.events.shutdown()
        # Do not orphan a confirmed write when the tray exits. No new work is accepted.
        for relay in self._relays:
            relay.stop.set()
            relay.thread.join()


# A public, test-only CA certificate (never used for a real server): lets the frozen exe prove, without `cryptography`,
# that it can fingerprint a PEM certificate and load it as the only trusted CA for the SymPort relay.
SELF_TEST_CA = """-----BEGIN CERTIFICATE-----
MIICsTCCAlegAwIBAgIUahAo54thfSVj0XII5fgT8ttQna4wCgYIKoZIzj0EAwIw
LTEQMA4GA1UECgwHU3ltUG9ydDEZMBcGA1UEAwwQU3ltUG9ydCBMb2NhbCBDQTAe
Fw0yNjExMDgwODU0MzBaFw0zNjExMDYwODU0MzBaMC0xEDAOBgNVBAoMB1N5bVBv
cnQxGTAXBgNVBAMMEFN5bVBvcnQgTG9jYWwgQ0EwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAARzPA4EyjGV6DA29kpgvSEJlljVhR5P2PzqlrBlWRvy6KOeKFvmBJX0
iUM8anEtPQPHcxfSht1PWa8Cbfnvo+Xfo4IBUzCCAU8wEgYDVR0TAQH/BAgwBgEB
/wIBADAOBgNVHQ8BAf8EBAMCAYYwggEIBgNVHR4BAf8Egf0wgfqggfcwCocICgAA
AP8AAAAwCocIrBAAAP/wAAAwCocIwKgAAP//AAAwCocIfwAAAP8AAAAwCocIZEAA
AP/AAAAwCocIqf4AAP//AAAwIocg/AAAAAAAAAAAAAAAAAAAAP4AAAAAAAAAAAAA
AAAAAAAwIocg/oAAAAAAAAAAAAAAAAAAAP/AAAAAAAAAAAAAAAAAAAAwIocgAAAA
AAAAAAAAAAAAAAAAAf////////////////////8wB4IFLmhvbWUwDIIKLmhvbWUu
YXJwYTALggkuaW50ZXJuYWwwBoIELmxhbjAIggYubG9jYWwwC4IJbG9jYWxob3N0
MB0GA1UdDgQWBBRxNRT4qIXsHulw7DAOJn71JRMKQzAKBggqhkjOPQQDAgNIADBF
AiEAtHqlSP7Dv1Std7ikWtVWEvCV8EvA2qruuzRLm/m4V0oCIDCqd8oxzMK6nxzy
bQB3B59H0vDcjTrLZOBFNPXbRtPf
-----END CERTIFICATE-----
"""
SELF_TEST_CA_SHA256 = "0e8dad2f5f8258d7b3c504dfccfb45374dac439d292262b2340e81b2e8a9ee23"


def self_test(report: Path | None):
    import base64
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener

    from symport.phone.registry import identify
    from symport.sis.writer import SisFile, SisPackage, build

    from .settings import protect, save_config
    assert identify("N95", rm="RM-320")["id"] == "nokia-n95-8gb"
    assert identify("N95")["variant_known"] is False
    assert identify("Nokia N97mini")["id"] == "nokia-n97-mini"
    assert identify("N97") is None
    assert identify('N8-00', rm='RM-596')['id'] == 'nokia-n8'
    assert identify('E7-00', rm='RM-626')['id'] == 'nokia-e7'
    assert identify('E7')['symbian'] is None
    assert identify('Nokia 7610')['id'] == 'nokia-7610' and identify('Nokia 7610 Supernova') is None

    with tempfile.TemporaryDirectory(prefix="symport-bridge-check-") as tmp:
        root = Path(tmp)
        os.environ["SYMPORT_GAMMU_CONFIG"] = ""
        os.environ["SYMPORT_CONNECTIONS_FILE"] = ""
        os.environ["SYMPORT_DEVICE_ROOTS"] = "{}"
        key = load_key(root)
        assert protect(protect(b"test-owner-credential"), True) == b"test-owner-credential"
        volume = root / "offline-files"
        volume.mkdir()
        save_config(
            root, {"schema": 1, "auto_usb": False, "volumes": {"offline-files": str(volume)}}
        )
        agent = Agent(root, key, port=0, auto=False)
        agent.start()
        base = f"http://127.0.0.1:{agent.server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def call(token, origin=None, host=None, data=None):
            headers = {"Content-Type": "application/json", "X-Symport-Bridge-Key": token}
            if origin:
                headers["Origin"] = origin
            if host:
                headers["Host"] = host
            request = Request(
                base + "/api/bridge",
                data=json.dumps(data or {"action": "discover"}).encode(),
                headers=headers,
            )
            return json.load(opener.open(request, timeout=30))

        try:
            observed = call(key)["devices"]
            assert len(observed) == 1 and observed[0]["model"] is None
            # Text-only SIS, generated in memory solely for this offline file test.
            # It is never installed or included as an artifact.
            sample = build(
                SisPackage(
                    0xE0E52002,
                    "OfflineCheck",
                    "SymPort",
                    files=[SisFile(r"c:\data\SymPortOfflineCheck.txt", b"offline text only")],
                )
            )
            encoded = base64.b64encode(sample).decode()
            call(
                key,
                data={
                    "action": "software-transfer",
                    "device": "offline-files",
                    "path": "offline-check.sis",
                    "content": encoded,
                    "confirm": True,
                },
            )
            copied = call(
                key,
                data={"action": "download", "device": "offline-files", "path": "offline-check.sis"},
            )
            assert copied["content"] == encoded
            for token, origin, host in [
                ("wrong", None, None),
                (key, "https://untrusted.invalid", None),
                (key, None, "rebind.invalid"),
            ]:
                try:
                    call(token, origin, host)
                    raise AssertionError("Denied request accepted")
                except HTTPError as exc:
                    assert exc.code == 403
            worker = (
                gammu_native.invoke("", "self-test")
                if gammu_native.available()
                else {"status": "unavailable"}
            )
            native_ui = "not-applicable"
            if os.name == "nt":
                import tkinter

                import pystray
                from PIL import Image

                asset = Path(__file__).resolve().parents[1] / "web_assets/brand/appmark-192.png"
                with Image.open(asset) as image:
                    icon = pystray.Icon("SymPortSelfTest", image.copy(), "SymPort self-test")
                    assert icon.title == "SymPort self-test"
                assert tkinter.Tcl().eval("info patchlevel")
                native_ui = "dependencies-loaded"
            # Components the installer promises, as found inside this very build; CA pinning without `cryptography`.
            import ssl as _ssl

            parts = doctor.components()
            missing = [p["name"] for p in parts if p["required"] and not p["present"]]
            if getattr(sys, "frozen", False) and os.name == "nt":
                assert not missing, "The build is missing required components: " + ", ".join(missing)
            assert pem_fingerprint(SELF_TEST_CA) == SELF_TEST_CA_SHA256
            _ssl.create_default_context().load_verify_locations(cadata=SELF_TEST_CA)
            sample = doctor.diagnose({"usb": [{"vid": "0421", "pid": "0001", "class": "USB", "status": "problem"}]}, {}, 0)
            assert sample["phone"]["code"] == "usb-driver-problem" and "serial number" not in doctor.export_text(sample).lower()
            from symport.phone.wpd import WpdAdapter

            fake = WpdAdapter(lambda script, request, timeout=150: [{"index": 0, "storages": ["Memory card"]}])
            row = fake.device()
            assert row and row["capability_matrix"]["contacts.read"]["status"] == "unsupported"
            assert row["capability_matrix"]["files.upload"]["status"] == "unsupported" and row["model"] is None
            result = {
                "wpd": "passed",
                "components": {p["name"]: p["present"] for p in parts},
                "ca_pinning": "passed",
                "doctor": "passed",
                "native_ui": native_ui,
                "sis_file_roundtrip": "passed",
                "schema": "symport.bridge.self-test/1",
                "version": VERSION,
                "http_auth": "passed",
                "origin": "passed",
                "dns_rebinding": "passed",
                "credential_protection": "passed",
                "gammu": worker,
                "physical_device": "not-tested",
                "model_registry": "passed",
            }
            if report:
                report.write_text(json.dumps(result, indent=2), encoding="utf-8")
            elif sys.stdout:
                print(json.dumps(result))
        finally:
            agent.close()


MUTEX_NAME = "SymPortBridgeSingleInstance"  # also the installer's AppMutex


def claim_single_instance():
    """Hold a named mutex for the process lifetime; False if another Bridge already runs."""
    if os.name != "nt":
        return True
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    claim_single_instance.handle = handle  # keep it open until exit
    return True


def main(argv=None):
    if len(sys.argv) == 4 and sys.argv[1] == "--gammu-worker":
        return gammu_native.worker_files(Path(sys.argv[2]), Path(sys.argv[3]))
    parser = argparse.ArgumentParser(description="SymPort local Nokia S60 Bridge")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=24652)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--settings", action="store_true", help="open the settings window (used by the tray)")
    parser.add_argument("--diagnostics", action="store_true", help="open the diagnostics window (used by the tray)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test(args.report)
    if args.diagnostics:
        from .tray import diagnostics_window

        return diagnostics_window((args.data_dir or data_directory()).absolute(), args.port)
    if args.settings:
        from .tray import settings_window

        return settings_window((args.data_dir or data_directory()).absolute(), args.port)
    if not args.headless and not claim_single_instance():
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, "SymPort Bridge is already running (see the tray icon).", "SymPort Bridge", 0x40
            )
        return
    root = (args.data_dir or data_directory()).absolute()
    key = load_key(root)

    def approve(data):
        if os.name != "nt":
            return False
        import ctypes

        return (
            ctypes.windll.user32.MessageBoxW(
                None,
                "SymPort requests: "
                + str(data["action"])
                + "\nApprove this operation on the connected phone?",
                "SymPort Bridge",
                0x124,
            )
            == 6
        )

    agent = None
    try:
        agent = Agent(root, key, args.port, approve=None if args.headless else approve)
        agent.start()
        if args.headless:
            while not agent.stop.wait(1):
                pass
        else:
            from .tray import run_tray

            run_tray(agent, key)
    except (ValueError, OSError):
        if os.name == "nt" and not args.headless:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "SymPort Bridge could not start. Check whether it is already running, the selected port is free and settings.json contains valid configuration.",
                "SymPort Bridge",
                0x10,
            )
        else:
            raise
    finally:
        if agent:
            agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
