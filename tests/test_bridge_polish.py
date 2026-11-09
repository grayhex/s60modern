"""USB-bridge polish: native enumeration, hotplug, tray state/tooltip/toasts, the Connections window, lease and hygiene."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from symport.bridge import children, device_watch, discovery, doctor, status, tray, winpnp
from symport.bridge import main as bridge_main
from symport.bridge.main import Agent
from symport.bridge.relay import OutboundRelay
from symport.bridge.settings import load_config, load_key, save_config, set_preference
from tests.tk_stub import Tk

USB = {"vid": "0421", "pid": "0223", "class": "WPD", "status": "ok", "mi": None, "problem_code": None}


def host(**kw):
    return {"usb": [], "serial": [], "volumes": [], "bluetooth": [], "portable": [], "diagnostics": [], **kw}


def report_for(*, pccs="unavailable", gammu_ids=0, mass=False, link=None, usb=(USB,), preference="auto", expires=None, scan_at=None):
    relay = None if link is None else {"state": "connected" if link == "connected" else "error", "reason": None if link == "connected" else link,
                                       "pinned": True, "expires_in": expires}
    return doctor.diagnose(host(usb=list(usb), volumes=[{"root": "E:/"}] if mass else []), {}, gammu_ids, relay,
                           "https://192.168.1.5:8765" if relay else None, [], pccs={"state": pccs}, preference=preference, scan_at=scan_at)


# ---- native PnP enumeration ------------------------------------------------------------------------------------------
class FakeSetup:
    def __init__(self, devices):
        self.devices_ = devices
        self.asked = []

    def devices(self, enumerator):
        self.asked.append(enumerator)
        return iter(self.devices_)


def test_enumeration_filters_by_instance_prefix_and_signals_unavailability():
    api = FakeSetup([{"InstanceId": "USB\\VID_0421&PID_0223\\SERIAL1", "Class": "WPD"}, {"InstanceId": "usb\\vid_0421&pid_01a2&mi_00\\x"},
                     {"InstanceId": "USB\\VID_1234&PID_0001\\Y"}])
    rows = winpnp.enumerate_present("USB", "USB\\VID_0421", api)
    assert [r["InstanceId"].split("\\")[1] for r in rows] == ["VID_0421&PID_0223", "vid_0421&pid_01a2&mi_00"]
    assert api.asked == ["USB"]

    class Broken:
        def devices(self, enumerator):
            raise OSError("SetupAPI failed")

    assert winpnp.enumerate_present("USB", "USB\\VID_0421", Broken()) is None
    assert winpnp.devinfo_size(8) == 32 and winpnp.devinfo_size(4) == 28


@pytest.mark.skipif(os.name == "nt", reason="this is the non-Windows answer")
def test_enumeration_is_unavailable_outside_windows():
    assert winpnp.enumerate_present("USB", "USB\\VID_0421") is None, "not Windows → the caller falls back"


def test_usb_scan_uses_setupapi_without_starting_powershell(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    native = [
        {"InstanceId": "USB\\VID_0421&PID_0223\\SECRET-SERIAL", "Class": "WPD", "Status": "OK", "ConfigManagerErrorCode": 0},
        {"InstanceId": "USB\\VID_0421&PID_0223&MI_01\\SECRET-SERIAL", "Class": "USB", "Status": "Error", "ConfigManagerErrorCode": 28},
    ]
    monkeypatch.setattr(discovery.winpnp, "enumerate_present", lambda enumerator, prefix, api=None: native)
    monkeypatch.setattr(discovery.children, "run", lambda *a, **k: pytest.fail("PowerShell must not start"))
    rows, reason = discovery.usb_devices()
    assert reason is None
    assert {(r["pid"], r["class"], r["status"], r["mi"], r["problem_code"]) for r in rows} == {("0223", "WPD", "ok", None, None), ("0223", "USB", "problem", "01", 28)}
    assert "SECRET" not in json.dumps(rows)


def test_usb_scan_falls_back_to_powershell_when_setupapi_is_unavailable(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    monkeypatch.setattr(discovery.winpnp, "enumerate_present", lambda *a, **k: None)
    output = json.dumps([{"Class": "Ports", "Status": "OK", "InstanceId": "USB\\VID_0421&PID_01A2\\S"}])
    monkeypatch.setattr(discovery.children, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    assert discovery.usb_devices()[0][0]["pid"] == "01a2"


def test_bluetooth_scan_uses_setupapi_and_keeps_only_paired_nokia(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    native = [
        {"InstanceId": "BTHENUM\\DEV_001122334455\\7&1", "FriendlyName": "Nokia E72"},
        {"InstanceId": "BTHENUM\\DEV_AABBCCDDEEFF\\7&2", "FriendlyName": "Headset"},
        {"InstanceId": "BTHENUM\\{0000110B}_VID&0001\\7&3", "FriendlyName": "Nokia service"},
    ]
    monkeypatch.setattr(discovery.winpnp, "enumerate_present", lambda enumerator, prefix, api=None: native)
    monkeypatch.setattr(discovery.children, "run", lambda *a, **k: pytest.fail("PowerShell must not start"))
    rows, reason = discovery.paired_bluetooth_candidates()
    assert reason is None and [(r["name"], r["address"]) for r in rows] == [("Nokia E72", "00:11:22:33:44:55")]


# ---- child processes -------------------------------------------------------------------------------------------------
def test_tracked_children_are_ended_and_a_timeout_kills_the_helper():
    child = children.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    assert child in children.alive()
    assert children.terminate_all() == 1 and child.poll() is not None and children.alive() == []
    with pytest.raises(subprocess.TimeoutExpired):
        children.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.3, capture_output=True)
    assert children.alive() == [], "a helper that timed out is killed, not left running"
    done = children.run([sys.executable, "-c", "print('x')"], timeout=10, capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "x"


# ---- hotplug ---------------------------------------------------------------------------------------------------------
def test_device_watcher_reacts_only_to_plug_messages_and_can_be_stopped():
    seen = []
    stopped = []
    watcher = device_watch.DeviceWatcher(lambda: seen.append(1), lambda notify: SimpleNamespace(stop=lambda: stopped.append(1), notify=notify))
    assert watcher.start() is True and watcher.running
    assert watcher.notify(device_watch.WM_DEVICECHANGE, device_watch.DBT_DEVICEARRIVAL) is True
    assert watcher.notify(device_watch.WM_DEVICECHANGE, device_watch.DBT_DEVICEREMOVECOMPLETE) is True
    assert watcher.notify(device_watch.WM_DEVICECHANGE, device_watch.DBT_DEVNODES_CHANGED) is True
    assert watcher.notify(device_watch.WM_DEVICECHANGE, 0x0018) is False and watcher.notify(0x0001, device_watch.DBT_DEVICEARRIVAL) is False
    assert len(seen) == 3
    watcher.stop()
    assert stopped == [1] and not watcher.running
    watcher.stop()  # idempotent


def test_a_window_that_cannot_be_created_leaves_the_poll_as_the_fallback():
    def broken(notify):
        raise OSError("no window")

    watcher = device_watch.DeviceWatcher(lambda: None, broken)
    assert watcher.start() is False and not watcher.running


@pytest.mark.skipif(os.name == "nt", reason="creates a real window on Windows")
def test_no_window_outside_windows():
    assert device_watch.DeviceWatcher(lambda: None).start() is False


@pytest.fixture
def agent(tmp_path, monkeypatch):
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    instance = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    yield instance
    instance.close()


def test_plug_event_wakes_the_loop_and_rereads_usb_but_keeps_the_back_off(agent, monkeypatch):
    rows = {"usb": [dict(USB, **{"class": "Ports"})]}
    monkeypatch.setattr(bridge_main, "host_candidates", lambda: host(usb=list(rows["usb"])))
    monkeypatch.setattr(bridge_main, "usb_devices", lambda: (list(rows["usb"]), None))
    agent.refresh_host(force=True)
    agent._scan_delay = 600  # the PR-80 back-off after a silent phone
    agent.on_device_change()  # WM_DEVICECHANGE with the same USB set (noise)
    started = time.monotonic()
    agent._wait_for_work()
    assert time.monotonic() - started < 3 and not agent.hotplug.is_set() and agent._usb_at == 0.0
    assert agent.refresh_host() is False and agent._scan_delay == 600, "unchanged USB set: the back-off is not reset"
    rows["usb"] = []
    agent.on_device_change()
    agent._wait_for_work()
    assert agent.refresh_host() is True, "the unplug is noticed at once, not after the 10 s poll"


def test_timer_is_still_the_fallback_without_events(agent):
    started = time.monotonic()
    agent.stop.set()
    agent._wait_for_work()  # no event: waits (≤3 s) and returns
    assert time.monotonic() - started <= 3.5


def test_agent_starts_the_watcher_only_on_windows_and_stops_it(agent, monkeypatch):
    stops = []
    monkeypatch.setattr(bridge_main, "is_windows", lambda: True)
    agent.auto = True
    monkeypatch.setattr(bridge_main, "DeviceWatcher", lambda cb: SimpleNamespace(start=lambda: True, stop=lambda: stops.append(1)))
    monkeypatch.setattr(agent, "monitor", lambda: None)
    agent.start()
    assert agent.watcher is not None
    agent.close()
    assert stops == [1]


# ---- tray state, tooltip, toasts -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(usb=()), status.NONE),
        (dict(pccs="silent"), status.SILENT),
        (dict(pccs="busy"), status.SILENT),
        (dict(pccs="available"), status.READY),
        (dict(pccs="verified", link="connected"), status.READY),
        (dict(mass=True, usb=()), status.READY),
        (dict(pccs="available", link="tls-untrusted"), status.LINK_ERROR),
        (dict(usb=(), link="unreachable"), status.LINK_ERROR),
        (dict(usb=(), link="connected"), status.NONE),
    ],
)
def test_tray_state(kwargs, expected):
    assert status.tray_state(report_for(**kwargs)) == expected


def test_tooltip_names_the_phone_the_jobs_and_the_link():
    ready = report_for(pccs="available", link="connected")
    assert status.tooltip(ready, "Nokia E72", True) == "Nokia E72 · контакты и календарь (USB OBEX) · SymPort подключён"
    assert status.tooltip(ready, "Nokia E72", False) == "Nokia E72 · contacts and calendar (USB OBEX) · SymPort connected"
    assert status.tooltip(report_for(usb=()), None, False) == "No phone found · SymPort not connected"
    assert "not answering" in status.tooltip(report_for(pccs="silent"), "Nokia E72", False)
    assert "link error" in status.tooltip(report_for(link="tls-untrusted"), None, False)
    assert len(status.tooltip(ready, "N" * 500, False)) <= 127


def test_toasts_for_connect_disconnect_route_change_and_link():
    notifier = status.Notifier(False)
    assert notifier.update(report_for(usb=()), None) == [], "the first report is a baseline"
    connected = notifier.update(report_for(pccs="available", link="connected"), "Nokia E72")
    assert connected == ["Nokia E72 connected: contacts and calendar (USB OBEX)", "SymPort connected"]
    assert notifier.update(report_for(pccs="available", link="connected"), "Nokia E72") == [], "nothing changed → silence"
    linked = {"state": "connected", "reason": None, "pinned": True, "expires_in": None}
    gammu = doctor.diagnose(host(usb=[USB]), {}, 1, linked, "https://192.168.1.5:8765", [], pccs={"state": "available"})
    assert notifier.update(gammu, "Nokia E72") == ["Route changed: contacts, calendar and SMS (Gammu (PC Suite))"]
    notifier2 = status.Notifier(True)
    notifier2.update(report_for(pccs="available", link="connected"), "Nokia E72")
    assert notifier2.update(report_for(usb=(), link="connected"), None) == ["Телефон отключён"]
    assert notifier2.update(report_for(usb=(), link="unreachable"), None) == ["Связь с SymPort потеряна"]
    notifier3 = status.Notifier(False)
    notifier3.update(report_for(link="connected"), None)
    assert notifier3.update(report_for(link="unreachable"), None) == ["SymPort link lost"]


def test_access_warning_comes_once_ten_minutes_before_the_end_and_again_for_a_new_access():
    notifier = status.Notifier(True)
    texts = lambda left: notifier.update(report_for(link="connected", expires=left), None)  # noqa: E731
    assert texts(3600) == [] and texts(601) == []
    warning = texts(599)
    assert len(warning) == 1 and "10 минут" in warning[0] and "Переподключиться к SymPort" in warning[0]
    assert texts(300) == [] and texts(10) == []
    assert texts(7200) == [] and len(texts(500)) == 1, "a renewed access warns again"


def test_tray_view_updates_icon_tooltip_and_respects_the_notification_setting():
    pytest.importorskip("PIL")
    from PIL import Image

    icon = SimpleNamespace(icon=None, title=None, notified=[])
    icon.notify = lambda text, title: icon.notified.append(text)
    view = tray.TrayView(icon, Image.new("RGBA", (64, 64), (255, 255, 255, 255)), False)
    view.update(report_for(usb=()), None, True)
    assert icon.icon.getpixel((55, 55))[:3] == status.COLORS[status.NONE] and icon.title == "No phone found · SymPort not connected"
    view.update(report_for(pccs="available"), "Nokia E72", False)
    assert icon.icon.getpixel((55, 55))[:3] == status.COLORS[status.READY] and icon.notified == [], "toasts are off"
    view.update(report_for(usb=()), None, True)
    assert icon.notified == ["Phone disconnected"]
    assert {status.COLORS[s] for s in (status.NONE, status.SILENT, status.READY, status.LINK_ERROR)}.__len__() == 4


# ---- settings --------------------------------------------------------------------------------------------------------
def test_notifications_setting_is_validated_and_the_choice_is_stored(tmp_path):
    save_config(tmp_path, {"schema": 1, "volumes": {}, "notifications": False})
    assert load_config(tmp_path)["notifications"] is False
    save_config(tmp_path, {"schema": 1, "volumes": {}, "notifications": "no"})
    with pytest.raises(ValueError):
        load_config(tmp_path)
    save_config(tmp_path, {"schema": 1, "volumes": {}, "gammu_config": ""})
    assert set_preference(tmp_path, "pccs")["transport_preference"] == "pccs" and load_config(tmp_path)["gammu_config"] == ""
    with pytest.raises(ValueError):
        set_preference(tmp_path, "everything")
    assert load_config(tmp_path)["transport_preference"] == "pccs"


def test_a_preference_stored_by_the_window_is_applied_by_the_running_bridge(agent, tmp_path):
    set_preference(tmp_path, "pccs")
    agent._sync_preference()
    assert agent.preference() == "pccs"
    set_preference(tmp_path, "auto")
    agent._sync_preference()
    assert agent.preference() == "auto"


def test_grouped_settings_window_saves_notifications_and_route(tmp_path, monkeypatch):
    stub = Tk(monkeypatch)
    monkeypatch.setattr(tray, "autostart", lambda enabled=None: False)
    monkeypatch.setattr(doctor, "windows_language_is_russian", lambda: True)
    stub.root.mainloop.side_effect = lambda: (stub.combobox[0].set(tray.TRANSPORT_LABELS_RU["pccs"]), stub.variables[1].set(False), stub.button("OK")())
    assert tray.settings_window(tmp_path, 24652) == tray.SETTINGS_SAVED
    saved = load_config(tmp_path)
    assert saved["transport_preference"] == "pccs" and saved["notifications"] is False
    assert any("Отмена" == text for text, _, _ in stub.buttons) and stub.combobox[1] == list(tray.TRANSPORT_LABELS_RU.values())


# ---- the Connections window ------------------------------------------------------------------------------------------
def open_connections(tmp_path, monkeypatch, report, russian=False, pump=True):
    stub = Tk(monkeypatch)
    monkeypatch.setattr(doctor, "windows_language_is_russian", lambda: russian)
    (tmp_path / "diagnostics.json").write_text(json.dumps(report), encoding="utf-8")
    stub.root.mainloop.side_effect = lambda: None
    assert tray.diagnostics_window(tmp_path, 24652) == tray.SETTINGS_CLOSED
    return stub


def test_connections_window_says_it_in_one_sentence_and_lists_routes_with_use_buttons(tmp_path, monkeypatch):
    report = report_for(pccs="available", link="connected", expires=5400, scan_at=1_700_000_000)
    stub = open_connections(tmp_path, monkeypatch, report)
    stub.root.after.call_args_list  # noqa: B018
    refresh = next(c.args[1] for c in stub.root.after.call_args_list if c.args and c.args[0] == 1500)
    refresh()
    texts = " | ".join(stub.texts())
    assert "Phone found: contacts and calendar (USB OBEX)." in texts
    assert "USB OBEX — available  · in use" in texts and "Gammu (PC Suite) — not available" in texts
    assert "SymPort access: 1 h 30 min left" in texts and "Last search: " in texts
    use = {text: kw for text, _, kw in stub.buttons if text == "Use"}
    assert use and stub.button_state("Automatic (recommended)") == "disabled", "already automatic"
    states = [kw.get("state") for text, _, kw in stub.buttons if text == "Use"]
    assert states.count("normal") == 1 and states.count("disabled") == 2, "only the usable route can be chosen"


def test_use_button_stores_the_preference_and_automatic_returns(tmp_path, monkeypatch):
    save_config(tmp_path, {"schema": 1, "volumes": {}})
    stub = open_connections(tmp_path, monkeypatch, report_for(pccs="available"))
    next(c.args[1] for c in stub.root.after.call_args_list if c.args and c.args[0] == 1500)()
    usable = next(command for text, command, kw in stub.buttons if text == "Use" and kw.get("state") == "normal")
    usable()
    assert load_config(tmp_path)["transport_preference"] == "pccs"
    stub2 = open_connections(tmp_path, monkeypatch, report_for(pccs="available", preference="pccs"))
    next(c.args[1] for c in stub2.root.after.call_args_list if c.args and c.args[0] == 1500)()
    assert stub2.button_state("Automatic (recommended)") == "normal"
    stub2.button("Automatic (recommended)")()
    assert load_config(tmp_path)["transport_preference"] == "auto"


def test_rescan_shows_progress_until_the_bridge_answers(tmp_path, monkeypatch):
    stub = open_connections(tmp_path, monkeypatch, report_for())
    stub.button("Rescan")()
    assert (tmp_path / "rescan.request").exists()
    assert "Searching…" in stub.texts()
    (tmp_path / "rescan.request").unlink()  # the Bridge consumed it
    (tmp_path / "diagnostics.json").write_text(json.dumps(report_for(scan_at=1_700_000_100)), encoding="utf-8")
    import os as _os

    _os.utime(tmp_path / "diagnostics.json", (time.time() + 5, time.time() + 5))
    next(c.args[1] for c in stub.root.after.call_args_list if c.args and c.args[0] == 1500)()
    assert "Searching…" not in stub.texts() and any(text.startswith("Last search: ") for text in stub.texts())


def test_saved_report_is_a_text_file_without_personal_data(tmp_path, monkeypatch):
    report = report_for(pccs="available", link="connected")
    stub = open_connections(tmp_path, monkeypatch, report)
    next(c.args[1] for c in stub.root.after.call_args_list if c.args and c.args[0] == 1500)()
    target = tmp_path / "out" / "mine"
    target.parent.mkdir()
    stub.filedialog.asksaveasfilename.return_value = str(target)
    stub.button("Save report…")()
    written = (tmp_path / "out" / "mine.txt").read_text(encoding="utf-8")
    assert written == doctor.export_text(report)
    for forbidden in ("192.168", "nmwcd", "if_obx", "IMEI:", "SN>", "8765"):
        assert forbidden not in written
    stub.filedialog.asksaveasfilename.return_value = ""
    stub.button("Save report…")()  # cancelled: nothing else is written
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["mine.txt"]


def test_log_folder_button_opens_the_journal_folder(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(tray.os, "startfile", opened.append, raising=False)
    stub = open_connections(tmp_path, monkeypatch, report_for())
    stub.button("Open log folder")()
    assert opened == [str(tmp_path / "build" / "symport" / "device-manager")]


def test_reconnect_opens_only_the_saved_address(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    report = report_for(link="connected")
    stub = open_connections(tmp_path, monkeypatch, report, russian=True)
    next(c.args[1] for c in stub.root.after.call_args_list if c.args and c.args[0] == 1500)()
    stub.button("Переподключиться к SymPort")()
    assert opened == ["https://192.168.1.5:8765/#phone/connection"]
    hostile = dict(report, link=dict(report["link"], origin="file:///C:/Windows/System32/calc.exe"))
    stub2 = open_connections(tmp_path, monkeypatch, hostile, russian=True)
    next(c.args[1] for c in stub2.root.after.call_args_list if c.args and c.args[0] == 1500)()
    opened.clear()
    stub2.button("Переподключиться к SymPort")()
    assert opened == []


# ---- lease -----------------------------------------------------------------------------------------------------------
def test_server_reports_the_remaining_access_and_the_bridge_keeps_it_in_memory_only(tmp_path):
    from symport.phone.relay import BrowserBridgeRelay

    relay = BrowserBridgeRelay()
    answer = relay.poll()
    assert 7100 < answer["expires_in"] <= 7200 and answer["request"] is None

    class Agent_:
        stop = threading.Event()

    outbound = OutboundRelay(Agent_(), "http://127.0.0.1:1", "t")
    assert outbound.remaining() is None and "expires_in" not in outbound.public_state(), "the browser-facing state is unchanged"
    outbound.expires_at = time.monotonic() + 1800
    assert 1790 <= outbound.remaining() <= 1800


def test_access_is_not_restored_after_a_restart(tmp_path, monkeypatch):
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    first = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    first.pair("https://192.168.1.5:8765", "relay-token", None) if False else None
    first.relay = SimpleNamespace(public_state=lambda: {"state": "connected", "reason": None, "pinned": True}, remaining=lambda: 600, origin="https://192.168.1.5:8765",
                                  stop=threading.Event(), thread=SimpleNamespace(join=lambda: None))
    first.write_snapshot()
    first.close()
    second = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    try:
        assert second.relay is None and second.doctor_report()["link"]["code"] == "not-paired"
        assert second.doctor_report()["link"]["expires_in"] is None
        leftovers = [p.name for p in tmp_path.iterdir() if "relay" in p.name or "lease" in p.name or "token" in p.name]
        assert leftovers == []
    finally:
        second.close()


# ---- hygiene on close ------------------------------------------------------------------------------------------------
def test_closing_the_bridge_leaves_no_child_process_no_store_and_no_interface(tmp_path, monkeypatch):
    from tests.test_nokia_pccs import FakePhone, Wire, adapter_for

    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    instance = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    release, entered = threading.Event(), threading.Event()

    class Hanging(FakePhone):
        def exchange(self, packet, timeout):
            if self.log and self.log[-1] == "ListChanges" and not self.cancelled:
                entered.set()
                release.wait(10)
                if self.cancelled:
                    from symport.phone.nokia_pccs import PccsError

                    raise PccsError("closed")
            return super().exchange(packet, timeout)

        def cancel(self):
            super().cancel()
            release.set()

    phone = Hanging()
    adapter = adapter_for(Wire(phone))
    instance.manager.transport.adapters["nokia-pccs"] = adapter
    instance.manager.transport.pccs = adapter
    window = children.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    errors = []

    def read():
        try:
            adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
        except ValueError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=read)
    worker.start()
    assert entered.wait(5), "the read is in flight"
    instance.close()
    worker.join(10)
    assert not worker.is_alive()
    assert phone.cancelled and phone.closed, "the interface handle is closed"
    assert phone.log[-2:] == ["CloseStore", "Disconnect"] and phone.open_store is None, "the open store was closed on the phone"
    assert window.poll() is not None and children.alive() == [], "no child process is left"
    assert errors, "the interrupted read reports a failure instead of a result"
