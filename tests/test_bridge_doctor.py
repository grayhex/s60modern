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
    from tests.tk_stub import Tk

    stub = Tk(monkeypatch)
    if report is not None:
        (tmp_path / "diagnostics.json").write_text(json.dumps(report), encoding="utf-8")
    assert tray.diagnostics_window(tmp_path, 24652) == tray.SETTINGS_CLOSED
    created = {text: command for text, command, _ in stub.buttons}
    return stub.root, stub.bound, created, stub.protocols, stub


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


PORTABLE = {"index": 0, "storages": ["Phone memory", "Memory card"]}


def test_a_readable_mtp_phone_is_not_a_dead_end():
    """E72 on Windows 11: WPD only, but Windows lists its storages → files are available; PIM is not."""
    snapshot = host(usb=[WPD], portable=[PORTABLE])
    report = doctor.diagnose(snapshot, {}, 0, parts=[])
    assert report["phone"]["code"] == "mtp-files" and report["phone"]["status"] == doctor.INFO
    states = {r["id"]: r["status"] for r in report["routes"]}
    assert states == {"mtp": "ok", "mass-storage": "info", "gammu": "info", "pccs": "info"}
    action = report["phone"]["action"][0]
    assert "contacts" in action and "COM port" in action and "Mass storage" in action
    # WPD without readable storage stays the cautionary status, never "files available"
    assert doctor.diagnose(host(usb=[WPD], portable=[{"index": 0, "storages": []}]), {}, 0, parts=[])["phone"]["code"] == "mtp-only"
    assert doctor.diagnose(host(usb=[WPD]), {}, 0, parts=[])["phone"]["code"] == "mtp-only"


def test_three_routes_are_independent():
    both = doctor.diagnose(host(usb=[WPD], portable=[PORTABLE], volumes=[DRIVE]), {}, 0, parts=[])
    assert {r["id"]: r["status"] for r in both["routes"]}["mass-storage"] == "ok"
    ready = doctor.diagnose(host(usb=[USB_OK], serial=[COM]), {"COM7": "free"}, 1, parts=[])
    assert {r["id"]: r["status"] for r in ready["routes"]}["gammu"] == "ok"
    silent = doctor.diagnose(host(usb=[WPD], portable=[PORTABLE]), {}, 0, parts=[], dku2="no-response")
    pc = next(r for r in silent["routes"] if r["id"] == "gammu")
    assert pc["status"] == "warn" and "DKU-2" in pc["summary"][0] and "not guaranteed" in pc["action"][0]
    assert silent["dku2"] == "no-response"
    identified = doctor.diagnose(host(usb=[WPD], portable=[PORTABLE]), {}, 0, parts=[], dku2="identified:dku2")
    assert identified["dku2"] == "identified"
    assert "mtp (" in doctor.export_text(silent)


def test_dku2_probe_is_read_only_rate_limited_and_honest(agent, monkeypatch):
    monkeypatch.setattr(bridge_main, "is_windows", lambda: True)
    monkeypatch.setattr(bridge_main.gammu_native, "available", lambda: True)
    agent.host = host(usb=[WPD])
    agent.config["auto_usb"] = True
    calls = []

    def silent(config, action, **kw):
        calls.append((Path(config).read_text(), action))
        raise ValueError("no answer")

    monkeypatch.setattr(bridge_main.gammu_native, "invoke", silent)
    assert agent._dku2_profiles([]) == [] and agent.dku2_state == "no-response"
    assert [a for _, a in calls] == ["identify", "identify"], "identify only — nothing is written to the phone"
    assert [c.split("connection = ")[1].strip() for c, _ in calls] == ["dku2", "dku2phonet"]
    assert agent._dku2_profiles([]) == [] and len(calls) == 2, "not retried while the USB device set is unchanged"
    agent._dku2_key = None                                   # Rescan
    agent._dku2_profiles([])
    assert len(calls) == 4

    def answers(config, action, **kw):
        return {"manufacturer": "Nokia", "model": "E72"}

    monkeypatch.setattr(bridge_main.gammu_native, "invoke", answers)
    agent._dku2_key = None
    found = agent._dku2_profiles([])
    assert found and found[0]["kind"] == "gammu-usb" and agent.dku2_state.startswith("identified")
    # a COM port, no working USB device or a hand-written Gammu config bypass the probe
    assert agent._dku2_profiles([{"port": "COM3"}]) == []
    agent.host = host()
    assert agent._dku2_profiles([]) == []
    agent.config["gammu_config"] = "C:/x.cfg"
    agent.host = host(usb=[WPD])
    agent._dku2_key = None
    assert agent._dku2_profiles([]) == []


# ---- silent COM port, AT back-off, DKU-2 after silent COM ports (real E72 report: COM8 modem silent, COM7 Java comm) ----

MODEM = {"port": "COM8", "kind": "modem", "model_profile": None, "vendor": "Nokia", "source": "x"}
JAVA = {"port": "COM7", "kind": "javacomm", "model_profile": None, "vendor": "Nokia", "source": "x"}


def test_a_port_that_opens_but_does_not_answer_is_com_silent_not_just_free():
    snapshot = host(usb=[WPD, {**WPD, "class": "Modem", "mi": "00"}], serial=[MODEM, JAVA])
    report = doctor.diagnose(snapshot, {"COM8": "silent"}, 0, parts=[], dku2="no-response")
    assert report["phone"]["code"] == "com-silent" and report["phone"]["status"] == doctor.WARN
    english, russian = report["phone"]["action"]
    for expected in ("off and on", "reconnect the cable", "after the phone has finished starting", "another cable", "DKU-2 connection did not answer"):
        assert expected in english, expected
    for expected in ("Выключите и включите", "переподключите кабель", "после полной загрузки", "другой кабель", "DKU-2 тоже не ответило"):
        assert expected in russian, expected
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["com-busy"]["status"] == doctor.WARN and "silent" in checks["com-busy"]["summary"][0]
    assert "free" not in checks["com-busy"]["summary"][0] and checks["gammu"]["status"] == doctor.WARN
    assert "(+1 Java comm, not used)" in checks["com"]["summary"][0], "the Java comm port is listed but not counted as an AT port"
    pc = next(r for r in report["routes"] if r["id"] == "gammu")
    assert pc["status"] == doctor.WARN and "silent" in pc["summary"][0] and "DKU-2" in pc["action"][0]
    # the DKU-2 sentence follows the real probe result
    assert "has not been tried yet" in doctor.diagnose(snapshot, {"COM8": "silent"}, 0, parts=[])["phone"]["action"][0]
    assert "DKU-2" not in doctor.diagnose(snapshot, {"COM8": "silent"}, 0, parts=[], dku2="identified:dku2")["phone"]["action"][0]
    # busy still wins over silent; a free, unprobed port stays the older generic status
    assert doctor.diagnose(snapshot, {"COM8": "busy"}, 0, parts=[])["phone"]["code"] == "com-busy"
    assert doctor.diagnose(snapshot, {"COM8": "free"}, 0, parts=[])["phone"]["code"] == "gammu-not-identified"
    # a lone Java comm port is not an AT route at all
    assert doctor.diagnose(host(usb=[WPD], serial=[JAVA]), {}, 0, parts=[])["phone"]["code"] != "gammu-not-identified"
    text = doctor.export_text(report)
    assert "com-silent" in text and "COM8" not in text and "COM7" not in text
    for forbidden in ("IMEI", "serial number", "instance", "VID_0421"):
        assert forbidden.lower() not in text.lower(), forbidden


def test_com_state_reports_silent_only_for_a_port_that_opens(monkeypatch):
    import types

    class Port:
        def __init__(self, name, timeout=None):
            self.name = name

        def close(self):
            pass

    exceptions = types.SimpleNamespace(SerialException=type("SerialException", (Exception,), {}))
    fake = types.ModuleType("serial")
    fake.Serial, fake.SerialException = Port, exceptions.SerialException
    monkeypatch.setitem(sys.modules, "serial", fake)
    assert doctor.com_state("COM8") == "free"
    assert doctor.com_state("COM8", silent=True) == "silent"

    def busy(name, timeout=None):
        raise fake.SerialException("could not open port 'COM8': PermissionError(13, 'Access is denied.')")

    fake.Serial = busy
    assert doctor.com_state("COM8", silent=True) == "busy", "a held port is busy, not silent"


@pytest.fixture
def scanning(agent, monkeypatch):
    """An Agent that polls two fake Nokia ports; `identify` calls are recorded and always fail unless told otherwise."""
    agent.config["auto_usb"] = True
    state = {"ports": [MODEM, JAVA], "identify": [], "answer": None, "now": 1000.0}
    monkeypatch.setattr(bridge_main, "serial_candidates", lambda: (list(state["ports"]), None))
    monkeypatch.setattr(bridge_main, "windows_nokia_volumes", lambda: ([], None))
    monkeypatch.setattr(bridge_main.time, "monotonic", lambda: state["now"])

    def invoke(config, action, **kw):
        state["identify"].append(Path(config).read_text())
        if state["answer"]:
            return state["answer"]
        raise ValueError("no answer")

    monkeypatch.setattr(bridge_main.gammu_native, "invoke", invoke)
    monkeypatch.setattr(bridge_main, "is_windows", lambda: False)  # DKU-2 has its own tests
    return agent, state


def test_at_polling_never_opens_the_java_comm_port_and_asks_the_modem_first(scanning):
    agent, state = scanning
    state["ports"] = [JAVA, MODEM, {**MODEM, "port": "COM9", "kind": "serial"}]   # even if listed in another order
    agent.scan()
    devices = [text.split("device = ")[1].split("\n")[0] for text in state["identify"]]
    assert devices == ["COM8", "COM9"] and "COM7" not in devices, "Java comm is skipped, modem port first"
    assert all(text.endswith("connection = at\n") and "dku2" not in text for text in state["identify"])
    assert agent.at_failed == {"COM8", "COM9"}


def test_polling_backs_off_while_silent_and_starts_over_on_changes(scanning):
    agent, state = scanning
    delays = []
    for _ in range(7):
        agent._last_scan = 0 if not delays else agent._last_scan
        state["now"] += agent._scan_delay + 1
        agent.scan()
        delays.append(agent._scan_delay)
    assert delays == [60, 120, 240, 480, 600, 600, 600], "30 s doubles up to ten minutes while nothing answers"
    calls = len(state["identify"])
    state["now"] += 100
    agent.scan()
    assert len(state["identify"]) == calls, "no new identify before the backed-off delay has passed"

    state["ports"] = [MODEM]                       # the set of ports changed
    agent.scan()
    assert len(state["identify"]) == calls + 1 and agent._scan_delay == 60, "back-off restarts at 30 s (then doubles)"
    for _ in range(3):
        state["now"] += agent._scan_delay + 1
        agent.scan()
    assert agent._scan_delay == 480
    agent._reset_scan()                            # USB hotplug / Rescan (rescan.request) → immediately, from 30 s
    assert agent._scan_delay == 30
    before = len(state["identify"])
    agent.scan()
    assert len(state["identify"]) == before + 1

    (agent.root / "rescan.request").write_text("1")
    assert agent._rescan_requested() is True
    agent._reset_scan()
    assert agent._scan_delay == 30 and agent._ports is None


def test_an_answering_phone_keeps_the_normal_rhythm(scanning):
    agent, state = scanning
    state["answer"] = {"manufacturer": "Nokia", "model": "E72"}
    agent.scan()
    assert agent._scan_delay == 30 and agent.at_failed == set()
    assert agent._auto_profiles and agent._auto_profiles[0]["kind"] == "gammu-usb"


def test_no_identify_starts_while_another_operation_holds_the_manager(scanning):
    agent, state = scanning
    with agent.manager.lock:
        agent.scan()
    assert state["identify"] == [] and agent._ports is None, "skipped, and tried again on the next loop"
    agent.stop.set()
    agent.scan()
    assert state["identify"] == [], "nothing starts once the Bridge is closing"
    agent.stop.clear()
    agent.scan()
    assert len(state["identify"]) == 1


def test_busy_ports_are_not_probed(scanning):
    agent, state = scanning
    agent.com = {"COM8": "busy"}
    agent.scan()
    assert state["identify"] == [], "another program holds it: identify would only time out"


def test_dku2_is_tried_when_com_ports_exist_but_stay_silent(scanning, monkeypatch):
    agent, state = scanning
    monkeypatch.setattr(bridge_main, "is_windows", lambda: True)
    monkeypatch.setattr(bridge_main.gammu_native, "available", lambda: True)
    agent.host = host(usb=[WPD])
    agent.scan()
    modes = [text.split("connection = ")[1].strip() for text in state["identify"]]
    assert modes == ["at", "dku2", "dku2phonet"], "AT on the modem port first, then the Nokia-specific connections"
    assert agent.dku2_state == "no-response"
    # unchanged USB set → the cached result, no second DKU-2 attempt on the next (backed-off) cycle
    state["now"] += agent._scan_delay + 1
    agent.scan()
    assert [t.split("connection = ")[1].strip() for t in state["identify"]].count("dku2") == 1
    # Rescan tries it again; an AT answer makes DKU-2 unnecessary
    agent._reset_scan()
    agent._dku2_key = None
    state["answer"] = {"manufacturer": "Nokia", "model": "E72"}
    state["identify"].clear()
    agent.scan()
    assert [t.split("connection = ")[1].strip() for t in state["identify"]] == ["at"]


def test_closing_the_bridge_stops_workers_first(agent, monkeypatch):
    order = []
    monkeypatch.setattr(bridge_main.gammu_native, "terminate_workers", lambda: order.append(("kill", agent.stop.is_set())))
    agent.close()
    assert order == [("kill", True)], "the stop flag is set before the workers are killed, so nothing restarts"


def test_com_probing_skips_the_java_port_and_is_throttled(agent, monkeypatch):
    opened = []
    monkeypatch.setattr(bridge_main, "host_candidates", lambda: host(serial=[MODEM, JAVA]))
    monkeypatch.setattr(bridge_main, "usb_devices", lambda: ([], None))
    monkeypatch.setattr(doctor, "com_state", lambda port, silent=False: opened.append(port) or "free")
    agent.refresh_host(force=True)
    assert opened == ["COM8"], "the Java comm port is never opened"
    agent.refresh_host()
    assert opened == ["COM8"], "not reopened on every cycle"
    agent.at_failed.add("COM8")
    agent.com = {"COM8": "free"}
    assert agent.doctor_report()["phone"]["code"] == "com-silent", "a failed AT identify turns 'free' into 'silent'"


def test_a_windows_11_mtp_phone_without_a_wpd_usb_row_is_still_recognised(monkeypatch):
    """E72: the portable device sits under USB\\MS_COMP_MTP; the Nokia USB rows carry class 'USB', not 'WPD'."""

    from symport.bridge import discovery

    monkeypatch.setattr(discovery, "os", types.SimpleNamespace(name="nt"))
    seen = {}

    def fake(script, request, timeout=150):
        from symport.phone import wpd

        seen["script"] = script
        assert script == wpd.PROBE
        return [{"index": 0, "storages": ["Phone memory", "Memory card", "x" * 400], "InstanceId": "USB\\MS_COMP_MTP\\9&1FE3119&0&00"}]

    monkeypatch.setattr("symport.phone.wpd.run_powershell", fake)
    portable, reason = discovery.portable_devices()
    assert reason is None and portable[0]["index"] == 0 and set(portable[0]) == {"index", "storages"}, "only index + storage names"
    assert len(portable[0]["storages"][2]) <= 255
    nokia_usb = {"vid": "0421", "pid": "0223", "class": "USB", "status": "ok", "mi": None, "problem_code": None}
    report = doctor.diagnose(host(usb=[nokia_usb], portable=portable), {}, 0, parts=[])
    assert report["phone"]["code"] == "mtp-files"
    assert {r["id"]: r["status"] for r in report["routes"]}["mtp"] == "ok"
    assert "read-only" in next(r for r in report["routes"] if r["id"] == "mtp")["summary"][0]
    text = doctor.export_text(report)
    assert "MS_COMP_MTP" not in text and "9&1FE3119" not in text and "Memory card" not in text


def test_a_locked_phone_is_connected_but_has_no_storage_not_invented_files():
    nokia_usb = {"vid": "0421", "pid": "0223", "class": "USB", "status": "ok", "mi": None, "problem_code": None}
    report = doctor.diagnose(host(usb=[nokia_usb], portable=[{"index": 0, "storages": []}]), {}, 0, parts=[])
    assert report["phone"]["code"] == "mtp-only", "connected, nothing to read: not mtp-files and not a dead-end no-com"
    mtp = next(r for r in report["routes"] if r["id"] == "mtp")
    assert mtp["status"] == "warn" and "no readable storage" in mtp["summary"][0]
    assert "Unlock the phone" in report["phone"]["action"][0] and "Разблокируйте телефон" in report["phone"]["action"][1]
    # no portable device at all, and no WPD row either: unchanged
    assert doctor.diagnose(host(usb=[nokia_usb]), {}, 0, parts=[])["phone"]["code"] == "no-com"
