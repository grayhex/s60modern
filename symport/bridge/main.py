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

from symport.phone import DeviceManager, gammu_native
from symport.phone.connections import load_profiles
from symport.phone.local import LocalTransports

from . import VERSION
from .discovery import documented_model, serial_candidates, windows_nokia_volumes
from .server import BridgeServer
from .settings import data_directory, load_config, load_key


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
        self.status = {"state": "starting", "connected": 0, "candidates": 0, "diagnostics": []}
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

    def pair(self, origin, token):
        if not self.server.approve or not self.server.approve(
            {
                "action": "Connect SymPort at "
                + origin
                + " for up to two hours. Phone data will be sent to this server."
            }
        ):
            return False
        from .relay import OutboundRelay

        self.revoke()
        self.relay = OutboundRelay(self, origin, token)
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
                self.manager.transport = LocalTransports(self.root, volume_identities=self._volume_identities)
                self._enrolled_roots = dict(self._auto_roots)
                self._enrolled_identities = dict(self._volume_identities)
                self.server.origins = set(config.get("origins", []))
                self._signature = signature
                self.last_refresh = None

    def scan(self):
        candidates, reason = serial_candidates()
        ports = tuple(c["port"] for c in candidates)
        if ports == self._ports and time.monotonic() - self._last_scan < 30:
            return
        self._ports = ports
        self._last_scan = time.monotonic()
        profiles = []
        if self.config.get("auto_usb", True) and not self.config.get("gammu_config"):
            for candidate in candidates:
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
                except (ValueError, OSError):
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
        self._auto_profiles, self._auto_roots = profiles, roots
        self.status["candidates"] = len(candidates) + len(volumes)
        self.status["diagnostics"] = [r for r in (reason, disk_reason) if r]
        self.refresh_config()

    def monitor(self):
        while not self.stop.is_set():
            try:
                if self.auto:
                    self.scan()
                else:
                    self.refresh_config()
                if self.last_refresh is None or time.monotonic() - self.last_refresh >= 30:
                    data = self.manager.execute({"action": "discover"})
                    self.status.update(
                        state="ready",
                        connected=sum(d.get("connected") is True for d in data["devices"]),
                    )
                    self.last_refresh = time.monotonic()
            except (ValueError, OSError):
                self.status.update(state="connection-error", connected=0)
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
            result = {
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
        gammu_native.worker_files(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description="SymPort local Nokia S60 Bridge")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=24652)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--settings", action="store_true", help="open the settings window (used by the tray)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test(args.report)
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
