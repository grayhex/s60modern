"""Bridge Driver & Connection Doctor, rescan/hotplug, safe export and the (stubbed-Tk) diagnostics window (#76)."""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from symport.bridge import doctor, tray
from symport.bridge import main as bridge_main
from symport.bridge.main import Agent, pem_fingerprint
from symport.bridge.settings import load_key, save_config

ROOT = Path(__file__).resolve().parents[1]
USB_OK = {"vid": "0421", "pid": "0001", "class": "Ports", "status": "ok"}
USB_BAD = {**USB_OK, "status": "problem"}
COM = {"port": "COM7", "model_profile": None, "vendor": "Nokia", "source": "x"}
DRIVE = {"root": "E:/", "model_profile": None, "source": "x"}
BT = {"name": "Nokia E52", "address": "00:11:22:33:44:55", "model_profile": None, "source": "x", "connected": None}


def host(**kw):
    return {"usb": [], "serial": [], "volumes": [], "bluetooth": [], "diagnostics": [], **kw}


@pytest.mark.parametrize(
    ("snapshot", "com", "identified", "expected"),
    [
        (host(), {}, 0, "no-usb"),                                        # (a) nothing on USB: cable / mode / port
        (host(usb=[USB_BAD]), {}, 0, "usb-driver-problem"),               # (b) seen, Device Manager problem
        (host(usb=[USB_OK]), {}, 0, "no-com"),                            # (c) driver fine, no COM port
        (host(usb=[USB_OK], serial=[COM]), {"COM7": "busy"}, 0, "com-busy"),   # (d) another program holds it
        (host(usb=[USB_OK], serial=[COM]), {"COM7": "free"}, 0, "gammu-not-identified"),   # (e)
        (host(usb=[USB_OK], volumes=[DRIVE]), {}, 0, "mass-storage-only"),  # (f) files only
        (host(bluetooth=[BT]), {}, 0, "bluetooth-only"),
        (host(usb=[USB_OK], serial=[COM]), {}, 1, "ready"),
    ],
)
def test_each_failure_is_a_separate_status_with_its_own_action(snapshot, com, identified, expected):
    report = doctor.diagnose(snapshot, com, identified, parts=[])
    assert report["phone"]["code"] == expected
    assert (report["phone"]["action"] is None) == (expected == "ready")
    if expected != "ready":
        assert all(len(text) > 10 for text in report["phone"]["action"]), "RU and EN instructions both exist"
    # PnP presence alone is never "ready"
    assert doctor.diagnose(host(usb=[USB_OK]), {}, 0, parts=[])["phone"]["code"] != "ready"


WPD = {"vid": "0421", "pid": "0223", "class": "WPD", "status": "ok", "mi": None, "problem_code": None}


def test_an_mtp_phone_is_not_reported_as_a_missing_com_port():
    """Real report (Nokia E72, Windows 11): one WPD device, no COM port, no drive letter, driver 'OK'."""
    report = doctor.diagnose(host(usb=[WPD]), {}, 0, parts=[])
    assert report["phone"]["code"] == "mtp-only"
    english = report["phone"]["action"][0]
    assert "Mass storage" in english and "COM port" in english and "driver" in english
    assert "WPD" in doctor.export_text(report) and "(WPD)" in report["checks"][0]["summary"][0]
    # a real serial interface next to it is a different situation
    both = doctor.diagnose(host(usb=[WPD, {**WPD, "class": "Ports", "mi": "01"}]), {}, 0, parts=[])
    assert both["phone"]["code"] == "no-com"
    assert doctor.diagnose(host(usb=[WPD], volumes=[DRIVE]), {}, 0, parts=[])["phone"]["code"] == "mass-storage-only"


def test_device_manager_error_code_is_explained_without_identifiers():
    bad = {**USB_BAD, "class": "unknown", "mi": "00", "problem_code": 28}
    report = doctor.diagnose(host(usb=[bad]), {}, 0, parts=[])
    assert report["phone"]["code"] == "usb-driver-problem"
    assert "error 28" in report["phone"]["action"][0] and "ошибку 28" in report["phone"]["action"][1]
    text = doctor.export_text(report)
    assert "interface 00" in text and "Device Manager code 28" in text


def test_nas_link_states_and_tls_failures_stay_separate_from_usb():
    for reason in ("tls-untrusted", "tls-hostname", "tls-expired", "tls-error", "unreachable", "lease-expired",
                   "lease-missing", "bad-token", "rejected", "server-error"):
        report = doctor.diagnose(host(usb=[USB_OK], serial=[COM]), {"COM7": "free"}, 1,
                                 {"state": "error", "reason": reason, "pinned": False}, "https://192.168.1.5:8765", [])
        assert report["link"]["code"] == reason and report["link"]["action"], reason
        assert report["phone"]["code"] == "ready", "a TLS problem is not reported as a USB problem"
    assert doctor.diagnose(host(), {}, 0, None, None, [])["link"]["code"] == "not-paired"
    ok = doctor.diagnose(host(), {}, 0, {"state": "connected", "reason": None, "pinned": True}, "https://nas:8765", [])
    assert ok["link"]["code"] == "connected" and ok["link"]["pinned"]
    assert "pinned" in doctor.window_model(ok, False)["link_summary"]
    assert doctor.window_model(ok, True)["link_summary"].endswith("(сертификат закреплён)")


def test_export_has_no_private_identifiers():
    report = doctor.diagnose(
        host(usb=[{**USB_OK, "serial": "SECRET123", "instance": "USB\\VID_0421&PID_0001\\5&2a"}],
             serial=[{**COM, "port": "COM7"}], diagnostics=["note"]),
        {"COM7": "busy"}, 0, {"state": "connected", "reason": None, "pinned": True}, "https://192.168.1.5:8765",
    )
    text = doctor.export_text(report)
    assert "0421:0001" in text and "COM7" not in text
    for forbidden in ("SECRET123", "192.168", "VID_0421", "5&2a", "Users", "token", "key"):
        assert forbidden not in text.replace("pyserial", ""), forbidden
    assert "scheme https" in text
    assert json.dumps(report).count("SECRET123") == 0


def test_components_are_found_by_import_not_by_installed_python():
    parts = {p["name"]: p for p in doctor.components()}
    assert parts["pyserial"]["required"] and not parts["python-gammu"]["required"]
    assert all({"name", "present", "required", "purpose"} <= set(p) for p in parts.values())


def test_pinning_needs_no_cryptography_and_the_exe_does_not_ship_it():
    """The frozen Bridge is built with --exclude-module cryptography (see tools/windows/build_bridge.py)."""
    assert "--exclude-module', 'cryptography'" in (ROOT / "tools/windows/build_bridge.py").read_text(encoding="utf-8")
    for path in (ROOT / "symport/bridge").glob("*.py"):
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith(("#", '"""')))
        assert not re.search(r"^\s*(import|from)\s+cryptography", code, re.M), path.name
    assert pem_fingerprint(bridge_main.SELF_TEST_CA) == bridge_main.SELF_TEST_CA_SHA256
    with pytest.raises(ValueError):
        pem_fingerprint(bridge_main.SELF_TEST_CA * 2)
    with pytest.raises(ValueError):
        pem_fingerprint("not a certificate")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    instance = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    yield instance
    instance.close()


def test_usb_hotplug_forces_a_rescan_and_rescan_request_is_consumed(agent, monkeypatch):
    rows = {"usb": []}
    monkeypatch.setattr(bridge_main, "host_candidates", lambda: host(usb=list(rows["usb"])))
    monkeypatch.setattr(bridge_main, "usb_devices", lambda: (list(rows["usb"]), None))
    assert agent.refresh_host(force=True) is False and agent.status["usb_seen"] == 0
    rows["usb"] = [USB_OK]                                   # the phone is plugged in
    agent._usb_at = 0                                        # the 10 s poll is due
    assert agent.refresh_host() is True and agent.status["usb_seen"] == 1
    agent._usb_at = 0
    assert agent.refresh_host() is False                     # unchanged → no loop
    marker = agent.root / "rescan.request"
    assert agent._rescan_requested() is False
    marker.write_text("1")
    assert agent._rescan_requested() is True and not marker.exists()


def test_snapshot_contains_only_the_safe_report(agent, monkeypatch):
    monkeypatch.setattr(bridge_main, "host_candidates",
                        lambda: host(usb=[{**USB_BAD, "serial": "SECRET123"}], diagnostics=[]))
    agent.refresh_host(force=True)
    agent.write_snapshot()
    data = json.loads((agent.root / "diagnostics.json").read_text(encoding="utf-8"))
    assert data["phone"]["code"] == "usb-driver-problem" and "SECRET123" not in json.dumps(data)
    assert data["usb"] == [{"vid": "0421", "pid": "0001", "class": "Ports", "status": "problem", "mi": None,
                            "problem_code": None}]


def test_com_port_is_not_probed_while_the_phone_is_identified(agent, monkeypatch):
    monkeypatch.setattr(bridge_main, "host_candidates", lambda: host(serial=[COM]))
    probe = mock.Mock(return_value="busy")
    monkeypatch.setattr(doctor, "com_state", probe)
    agent.status["identified"] = 1
    agent.refresh_host(force=True)
    probe.assert_not_called()                                # an open Gammu session must not look "busy"
    agent.status["identified"] = 0
    agent.refresh_host(force=True)
    assert agent.com == {"COM7": "busy"}


# ---- the diagnostics window: Tk is stubbed (it is not available here); the logic and the close paths are real --------

def run_window(tmp_path, monkeypatch, report=None):
    bound, created, protocols = {}, {}, []
    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    root = mock.MagicMock(name="root")
    root.bind.side_effect = lambda key, fn: bound.__setitem__(key, fn)
    root.protocol.side_effect = lambda name, fn: protocols.append((name, fn))
    tk.Tk = lambda: root
    widget = lambda *a, **k: mock.MagicMock()  # noqa: E731
    tk.Text = widget
    tk.messagebox = mock.MagicMock()

    def button(parent, text="", command=None, **kw):
        created[text] = command
        return mock.MagicMock()

    ttk.Button, ttk.Frame, ttk.Label = button, widget, widget
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk.messagebox)
    if report is not None:
        (tmp_path / "diagnostics.json").write_text(json.dumps(report), encoding="utf-8")
    assert tray.diagnostics_window(tmp_path, 24652) == tray.SETTINGS_CLOSED
    return root, bound, created, protocols, tk


def test_diagnostics_window_closes_with_ok_escape_enter_and_x(tmp_path, monkeypatch):
    root, bound, created, protocols, _ = run_window(tmp_path, monkeypatch, doctor.diagnose(host(), {}, 0, parts=[]))
    assert ("WM_DELETE_WINDOW", root.destroy) in protocols, "the window's X closes it"
    assert created["Close"] is root.destroy, "the Close/OK button closes it"
    for key in ("<Escape>", "<Return>"):
        bound[key](None)
    assert root.destroy.call_count == 2
    root.mainloop.assert_called_once()                       # Tk owns this process' main thread; nothing else runs in it
    assert set(created) >= {"Rescan", "Copy diagnostics", "Open Device Manager", "Pair / Help", "Close"}


def test_rescan_button_asks_the_running_bridge_through_a_marker_file(tmp_path, monkeypatch):
    _, _, created, _, _ = run_window(tmp_path, monkeypatch)
    created["Rescan"]()
    assert (tmp_path / "rescan.request").exists()


def test_pair_help_opens_only_the_paired_https_origin(tmp_path, monkeypatch):
    report = doctor.diagnose(host(), {}, 0, {"state": "connected", "reason": None, "pinned": True},
                             "https://192.168.1.5:8765", [])
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    root, bound, created, _, tk = run_window(tmp_path, monkeypatch, report)
    # the stubbed mainloop returns at once, so run the periodic refresh once by hand
    after = [call.args[1] for call in root.after.call_args_list if call.args and call.args[0] == 1500]
    assert after, "the window refreshes itself from diagnostics.json"
    after[0]()
    created["Pair / Help"]()
    assert opened == ["https://192.168.1.5:8765/#phone/connection"]
    hostile = dict(report, link=dict(report["link"], origin="file:///C:/Windows/System32/calc.exe"))
    (tmp_path / "diagnostics.json").write_text(json.dumps(hostile), encoding="utf-8")
    opened.clear()
    root.after.reset_mock()
    root2, _, created2, _, tk2 = run_window(tmp_path, monkeypatch, hostile)
    [c.args[1] for c in root2.after.call_args_list if c.args and c.args[0] == 1500][0]()
    created2["Pair / Help"]()
    assert opened == [], "a tampered snapshot cannot make the Bridge open an arbitrary URL"
    tk2.messagebox.showinfo.assert_called()


def test_tray_diagnostics_uses_a_separate_process_and_a_flag_the_exe_understands(tmp_path):
    command = tray.diagnostics_command(tmp_path, 24652)
    assert "--diagnostics" in command and command[command.index("--data-dir") + 1] == str(tmp_path)
    seen = {}
    with mock.patch.object(tray, "diagnostics_window", lambda root, port: seen.update(root=root, port=port) or 10):
        assert bridge_main.main(["--diagnostics", "--data-dir", str(tmp_path), "--port", "24999"]) == 10
    assert seen == {"root": tmp_path.absolute(), "port": 24999}
