"""First-connection diagnostics: Bridge probe, USB visibility, driver/port failure reasons. No phone."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from symport.bridge import discovery
from symport.phone import failure
from symport.phone.gammu_adapter import GammuAdapter
from tests.test_windows_bridge import bridge, call  # noqa: F401


def test_probe_discloses_only_the_version_and_never_prompts(bridge):  # noqa: F811
    prompts = []
    bridge.pair = lambda origin, token: prompts.append(token) or True
    origin = "https://symport.example"
    status, body = call(bridge, {"origin": origin, "probe": True}, {"Origin": origin}, "/api/bridge/pair")
    assert status == 200 and body == {"probe": True, "version": body["version"], "pair_available": True}
    assert prompts == []
    # A probe from another origin than the declared one, or a bad host, is refused like pairing.
    assert call(bridge, {"origin": "https://other.example", "probe": True}, {"Origin": origin}, "/api/bridge/pair")[0] == 400
    assert call(bridge, {"origin": origin, "probe": True}, {"Origin": origin, "Host": "evil.invalid"}, "/api/bridge/pair")[0] == 400
    # A probe cannot be used to skip the approval: without probe=true the token rules still apply.
    assert call(bridge, {"origin": origin, "token": "short"}, {"Origin": origin}, "/api/bridge/pair")[0] == 400
    assert prompts == []


def test_probe_reports_when_pairing_is_unavailable(bridge):  # noqa: F811
    origin = "https://symport.example"
    status, body = call(bridge, {"origin": origin, "probe": True}, {"Origin": origin}, "/api/bridge/pair")
    assert status == 200 and body["pair_available"] is False


def fake_sysfs(tmp_path, devices):
    for name, vid, pid, driver in devices:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "idVendor").write_text(vid + "\n")
        (folder / "idProduct").write_text(pid + "\n")
        (folder / "serial").write_text("PRIVATE-SERIAL\n")
        interface = tmp_path / f"{name}:1.0"
        interface.mkdir()
        if driver:
            os.symlink(tmp_path, interface / "driver")


@pytest.mark.skipif(os.name == "nt", reason="sysfs")
def test_usb_scan_separates_unplugged_from_missing_driver(tmp_path):
    assert discovery.usb_devices(str(tmp_path)) == ([], None)
    fake_sysfs(tmp_path, [("1-1", "0421", "0123", False), ("1-2", "8086", "0001", True), ("2-3", "0421", "0456", True)])
    rows, reason = discovery.usb_devices(str(tmp_path))
    assert reason is None
    assert {(r["vid"], r["pid"], r["status"]) for r in rows} == {("0421", "0123", "problem"), ("0421", "0456", "ok")}
    assert "PRIVATE-SERIAL" not in json.dumps(rows)


@pytest.mark.skipif(os.name == "nt", reason="sysfs")
def test_usb_scan_reports_unavailable_host(tmp_path):
    rows, reason = discovery.usb_devices(str(tmp_path / "missing"))
    assert rows == [] and "unavailable" in reason


def test_windows_usb_scan_keeps_only_vid_pid_class_status(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    # This test covers the PowerShell fallback; on a real Windows runner SetupAPI would otherwise answer first.
    monkeypatch.setattr(discovery.winpnp, "enumerate_present", lambda *a, **k: None)
    output = json.dumps([
        {"Class": "Ports", "Status": "OK", "InstanceId": "USB\\VID_0421&PID_01A2\\SECRET-SERIAL-1"},
        {"Class": "USB", "Status": "Error", "InstanceId": "USB\\VID_0421&PID_01A2&MI_00\\SECRET-SERIAL-1"},
        {"Class": "USB", "Status": "OK", "InstanceId": "USB\\VID_1234&PID_0001\\X"},
    ])
    monkeypatch.setattr(discovery.children, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    rows, reason = discovery.usb_devices()
    assert reason is None and {(r["pid"], r["class"], r["status"]) for r in rows} == {
        ("01a2", "Ports", "ok"), ("01a2", "USB", "problem")}
    assert "SECRET" not in json.dumps(rows)
    monkeypatch.setattr(discovery.children, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert discovery.usb_devices()[0] == []


def test_host_report_has_environment_without_identifiers():
    host = discovery.host_candidates()
    assert set(host["environment"]) == {"bridge_version", "os", "python_gammu"}
    assert "usb" in host
    assert os.uname().nodename not in json.dumps(host) if hasattr(os, "uname") else True


@pytest.mark.parametrize("text,reason", [
    ("Gammu: ERR_DEVICELOCKED. The selected", "port-busy"),
    ("Error opening device, it is already in use by other application.", "port-busy"),
    ("Error opening device. Unknown, busy or no permissions.", "port-open"),
    ("Gammu: ERR_DEVICENOTEXIST", "port-missing"),
    ("Error opening device, it doesn't exist.", "port-missing"),
    ("Error opening device, you don't have permissions.", "port-permission"),
    ("No response in specified timeout. Probably phone not connected.", "no-response"),
    ("Gammu: ERR_TIMEOUT.", "no-response"),
    ("Gammu: ERR_DEVICENODRIVER", "driver-missing"),
    ("something private about +49 123", "unknown"),
    (None, "unknown"),
])
def test_failure_reasons_are_fixed_and_private(text, reason):
    assert failure.classify(text) == reason
    assert reason in failure.REASONS


def run_cli(error_text):
    def fake(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gammu", stderr=error_text)

    return fake


def test_adapter_exposes_reason_not_raw_text(tmp_path, monkeypatch):
    monkeypatch.setattr("symport.phone.gammu_adapter.gammu_native.available", lambda: False)
    monkeypatch.setattr("symport.phone.gammu_adapter.subprocess.run",
                        run_cli("Error opening device, it is already in use by other application. +49 170 123"))
    row = GammuAdapter(tmp_path, "usb", "/c", kind="gammu-usb").device()
    assert not row["connected"] and row["failure"] == {"reason": "port-busy"}
    assert "+49" not in json.dumps(row)
    assert row["capabilities"] == []


def test_missing_gammu_executable_is_its_own_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("symport.phone.gammu_adapter.gammu_native.available", lambda: False)

    def missing(*a, **k):
        raise FileNotFoundError("gammu")

    monkeypatch.setattr("symport.phone.gammu_adapter.subprocess.run", missing)
    assert GammuAdapter(tmp_path, "usb", "/c", kind="gammu-usb").device()["failure"] == {"reason": "gammu-missing"}


def test_native_failure_code_survives_as_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("symport.phone.gammu_adapter.gammu_native.available", lambda: True)
    adapter = GammuAdapter(tmp_path, "usb", "/c", kind="gammu-usb")

    def fail(*a):
        raise ValueError("Gammu: ERR_TIMEOUT. The selected driver cannot complete this request.")

    adapter.personal.execute = fail
    monkeypatch.setattr("symport.phone.gammu_adapter.subprocess.run", run_cli(""))
    assert adapter.device()["failure"]["reason"] == "no-response"
