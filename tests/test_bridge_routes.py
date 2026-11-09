"""Routes to a Nokia (Gammu, USB OBEX, MTP, Mass storage), the choice between them and its Doctor/Agent wiring."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from symport.bridge import doctor, routes
from symport.bridge import main as bridge_main
from symport.bridge.main import Agent
from symport.bridge.settings import load_config, load_key, save_config
from symport.phone.local import LocalTransports
from symport.phone.service import DeviceManager

USB = {"vid": "0421", "pid": "0223", "class": "USB", "status": "ok", "mi": None, "problem_code": None}
WPD = {**USB, "class": "WPD"}
MODEM = {"port": "COM8", "kind": "modem", "model_profile": None, "vendor": "Nokia", "source": "x"}


def host(**kw):
    return {"usb": [], "serial": [], "volumes": [], "bluetooth": [], "portable": [], "diagnostics": [], **kw}


def states(gammu="unavailable", pccs="unavailable", mtp="unavailable", mass="unavailable"):
    return {"gammu": gammu, "pccs": pccs, "mtp": mtp, "mass-storage": mass}


# ---- the pure selection function -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("preference", "given", "tried", "expected"),
    [
        # auto: contacts/calendar → Gammu only when VERIFIED, otherwise USB OBEX
        ("auto", states(gammu="verified", pccs="available"), True, {"contacts": "gammu", "calendar": "gammu", "sms": "gammu"}),
        ("auto", states(gammu="available", pccs="available"), False, {"contacts": "pccs", "calendar": "pccs", "sms": "gammu"}),
        ("auto", states(gammu="silent", pccs="available"), True, {"contacts": "pccs", "calendar": "pccs", "sms": None}),
        ("auto", states(gammu="silent", pccs="verified"), True, {"contacts": "pccs", "calendar": "pccs", "sms": None}),
        ("auto", states(gammu="unavailable", pccs="available"), True, {"contacts": "pccs", "calendar": "pccs", "sms": None}),
        ("auto", states(gammu="silent", pccs="busy"), True, {"contacts": None, "calendar": None, "sms": None}),
        ("auto", states(gammu="silent", pccs="silent"), True, {"contacts": None, "calendar": None, "sms": None}),
        ("auto", states(), False, {"contacts": None, "calendar": None, "sms": None}),
        # explicit modes use only that route
        ("gammu", states(gammu="verified", pccs="available"), True, {"contacts": "gammu", "calendar": "gammu", "sms": "gammu"}),
        ("gammu", states(gammu="silent", pccs="available"), True, {"contacts": None, "calendar": None, "sms": None}),
        ("pccs", states(gammu="verified", pccs="available"), True, {"contacts": "pccs", "calendar": "pccs", "sms": None}),
        ("pccs", states(gammu="verified", pccs="busy"), True, {"contacts": None, "calendar": None, "sms": None}),
        ("mtp", states(gammu="verified", pccs="available", mtp="available"), True, {"contacts": None, "calendar": None, "sms": None}),
        ("nonsense", states(gammu="silent", pccs="available"), True, {"contacts": "pccs", "calendar": "pccs", "sms": None}),
    ],
)
def test_choice_of_route_for_contacts_calendar_and_sms(preference, given, tried, expected):
    chosen = routes.choose(preference, given, gammu_tried=tried)
    assert {job: chosen[job] for job in ("contacts", "calendar", "sms")} == expected


@pytest.mark.parametrize(
    ("preference", "given", "files"),
    [
        ("auto", states(mtp="available", mass="available"), "mass-storage"),
        ("auto", states(mtp="available"), "mtp"),
        ("auto", states(mtp="silent"), None),
        ("auto", states(), None),
        ("mtp", states(mtp="available", mass="available"), "mtp"),
        ("gammu", states(gammu="verified", mtp="available", mass="available"), None),
        ("pccs", states(pccs="verified", mtp="available", mass="available"), None),
    ],
)
def test_choice_of_route_for_files(preference, given, files):
    assert routes.choose(preference, given)["files"] == files


def test_explicit_mode_says_what_it_cannot_do():
    assert routes.choose("pccs", states(pccs="available"))["cannot"] == ["sms", "files"]
    assert routes.choose("gammu", states(gammu="verified"))["cannot"] == ["files"]
    assert routes.choose("mtp", states(mtp="available"))["cannot"] == ["contacts", "calendar", "sms"]
    assert routes.choose("auto", states(pccs="available"))["cannot"] == []


def test_gammu_dead_end_needs_all_attempts_over_and_usb_obex_working():
    assert routes.choose("auto", states(gammu="silent", pccs="available"), gammu_tried=True)["dead_end"] is True
    assert routes.choose("auto", states(gammu="silent", pccs="available"), gammu_tried=False)["dead_end"] is False
    assert routes.choose("auto", states(gammu="verified", pccs="available"), gammu_tried=True)["dead_end"] is False
    assert routes.choose("auto", states(gammu="silent", pccs="busy"), gammu_tried=True)["dead_end"] is False
    assert routes.choose("auto", states(gammu="silent"), gammu_tried=True)["dead_end"] is False


def test_a_running_operation_defers_the_switch_and_keeps_the_previous_choice():
    before = routes.choose("auto", states(gammu="silent", pccs="available"))
    during = routes.choose("auto", states(gammu="verified", pccs="available"), operation_in_progress=True, current=before)
    assert during["deferred"] is True and during["contacts"] == "pccs"
    after = routes.choose("auto", states(gammu="verified", pccs="available"), operation_in_progress=False, current=before)
    assert after["deferred"] is False and after["contacts"] == "gammu"
    # nothing to keep yet → the first choice is computed
    assert routes.choose("auto", states(pccs="available"), operation_in_progress=True)["contacts"] == "pccs"


def test_recommended_for_maps_jobs_to_routes():
    chosen = routes.choose("auto", states(gammu="silent", pccs="available", mass="available"))
    assert routes.recommended_for(chosen) == {"pccs": ["contacts", "calendar"], "mass-storage": ["files"]}


# ---- Gammu error codes -----------------------------------------------------------------------------------------------
def test_only_codes_from_the_fixed_list_survive():
    assert routes.gammu_code("Gammu: ERR_DEVICENOTEXIST. The selected driver cannot complete this request") == "ERR_DEVICENOTEXIST"
    assert routes.gammu_code("Gammu: ERR_TOTALLY_NEW. x") == "ERR_CONNECTION"
    assert routes.gammu_code("no code here, COM8 \\\\?\\usb#vid_0421") == ""
    assert routes.gammu_hint("ERR_NO_SUCH") is None and routes.gammu_hint("C:\\secret") is None
    for code in routes.GAMMU_CODES:
        english, russian = routes.gammu_hint(code)
        assert english and russian


def test_device_not_exist_over_dku2_points_to_usb_obex_in_both_languages():
    english, russian = routes.gammu_hint("ERR_DEVICENOTEXIST", dku2=True)
    assert "x64" in english and "USB OBEX" in english
    assert russian == "Gammu не находит Nokia-интерфейс; это известное ограничение x64-сборки Gammu, используйте маршрут USB OBEX."
    assert "x64" not in routes.gammu_hint("ERR_DEVICENOTEXIST")[0]


# ---- Doctor ----------------------------------------------------------------------------------------------------------
def test_four_routes_each_with_state_capabilities_and_reason():
    report = doctor.diagnose(host(usb=[WPD]), {}, 0, parts=[], dku2="no-response", gammu_error="ERR_DEVICENOTEXIST",
                             pccs={"state": "available", "contacts": True, "calendar": True}, gammu_tried=True)
    by_id = {r["id"]: r for r in report["routes"]}
    assert list(by_id) == ["gammu", "pccs", "mtp", "mass-storage"]
    assert by_id["gammu"]["state"] == "silent" and by_id["pccs"]["state"] == "available"
    assert by_id["pccs"]["capabilities"] == ["contacts", "calendar"] and by_id["pccs"]["read_only"] is True
    assert by_id["pccs"]["recommended_for"] == ["contacts", "calendar"] and by_id["gammu"]["recommended_for"] == []
    assert report["dead_end"] is True and report["gammu_error"] == "ERR_DEVICENOTEXIST"
    english, russian = by_id["gammu"]["action"]
    assert "ERR_DEVICENOTEXIST" in english and "known limit of the x64" in english
    assert "известное ограничение x64-сборки Gammu" in russian and "Контакты и календарь доступны по USB OBEX" in russian
    assert report["selection"] == {"contacts": "pccs", "calendar": "pccs", "sms": None, "files": None}


def test_busy_usb_obex_names_pc_suite():
    report = doctor.diagnose(host(usb=[USB]), {}, 0, parts=[], pccs={"state": "busy", "reason": "busy"})
    pccs = next(r for r in report["routes"] if r["id"] == "pccs")
    assert pccs["state"] == "busy" and pccs["status"] == doctor.FAIL
    assert "PC Suite" in pccs["action"][0] and "PC Suite" in pccs["action"][1]
    assert report["selection"]["contacts"] is None and report["dead_end"] is False


def test_unknown_gammu_error_text_never_reaches_the_report():
    report = doctor.diagnose(host(usb=[WPD]), {}, 0, parts=[], gammu_error="C:\\Users\\me\\secret.cfg")
    assert report["gammu_error"] == "" and "secret" not in json.dumps(report)


def test_exported_text_has_routes_preference_and_code_but_no_paths_or_serials():
    report = doctor.diagnose(host(usb=[WPD]), {}, 0, parts=[], dku2="no-response", gammu_error="ERR_TIMEOUT",
                             pccs={"state": "verified"}, preference="pccs", gammu_tried=True)
    text = doctor.export_text(report)
    assert "preference: pccs" in text and "pccs (verified)" in text and "ERR_TIMEOUT" in text and "used for: contacts, calendar" in text
    assert "nmwcd" not in text and "if_obx" not in text and "\\\\?\\" not in text


# ---- settings --------------------------------------------------------------------------------------------------------
def test_transport_preference_is_validated_and_defaults_to_auto(tmp_path):
    assert load_config(tmp_path)["transport_preference"] == "auto"
    for value in routes.PREFERENCES:
        save_config(tmp_path, {"schema": 1, "volumes": {}, "transport_preference": value})
        assert load_config(tmp_path)["transport_preference"] == value
    for bad in ("fastest", "", 3, None, "PCCS"):
        save_config(tmp_path, {"schema": 1, "volumes": {}, "transport_preference": bad})
        with pytest.raises(ValueError):
            load_config(tmp_path)
    save_config(tmp_path, {"schema": 1, "volumes": {}})
    assert load_config(tmp_path).get("transport_preference", "auto") == "auto"


# ---- Agent -----------------------------------------------------------------------------------------------------------
class FakePccs:
    def __init__(self, state="available"):
        self.state = state
        self.closed = 0

    def route_status(self):
        return {"state": self.state, "reason": "", "contacts": True, "calendar": True, "verified": []}

    def device(self):
        return None

    def close(self):
        self.closed += 1


@pytest.fixture
def agent(tmp_path, monkeypatch):
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    instance = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    instance.manager.transport.pccs = FakePccs()
    instance.host = host(usb=[WPD], serial=[MODEM])
    yield instance
    instance.close()


def journal(agent):
    path = agent.manager.journal
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_agent_chooses_usb_obex_when_gammu_is_silent_and_logs_the_switch_once(agent):
    agent.com = {"COM8": "free"}
    agent.at_failed.add("COM8")
    assert agent.update_route_selection() is True
    assert agent.selection["contacts"] == "pccs" and agent.selection["dead_end"] is True
    assert agent.route_hints["recommended_for"] == {"pccs": ["contacts", "calendar"]}
    assert [e["action"] for e in journal(agent)] == ["route-switch"]
    assert set(journal(agent)[0]) == {"time", "action", "status"}, "no phone data in the journal"
    assert agent.update_route_selection() is False and len(journal(agent)) == 1, "an unchanged choice is not logged again"


def test_agent_returns_to_gammu_when_it_becomes_verified(agent):
    agent.update_route_selection()
    agent.status["identified"] = 1
    assert agent.update_route_selection() is True
    assert agent.selection["contacts"] == "gammu" and agent.selection["dead_end"] is False
    assert [e["action"] for e in journal(agent)] == ["route-switch", "route-switch"]


def test_agent_never_switches_while_the_manager_is_busy(agent):
    agent.update_route_selection()
    first = dict(agent.selection)
    agent.status["identified"] = 1
    with agent.manager.lock:
        assert agent.update_route_selection() is False
    assert agent.selection == first, "the running operation keeps the route it started with"
    assert agent.update_route_selection() is True and agent.selection["contacts"] == "gammu"


@pytest.mark.parametrize(("preference", "contacts"), [("auto", "pccs"), ("pccs", "pccs"), ("gammu", None), ("mtp", None)])
def test_agent_honours_the_stored_preference(agent, preference, contacts):
    agent.config["transport_preference"] = preference
    agent.update_route_selection()
    assert agent.selection["contacts"] == contacts and agent.selection["preference"] == preference


def test_agent_remembers_the_real_gammu_error_code_and_reports_it(agent, monkeypatch):
    agent.config["auto_usb"] = True
    monkeypatch.setattr(bridge_main, "serial_candidates", lambda: ([MODEM], None))
    monkeypatch.setattr(bridge_main, "windows_nokia_volumes", lambda: ([], None))
    monkeypatch.setattr(bridge_main, "is_windows", lambda: False)

    def invoke(config, action, **kw):
        raise ValueError("Gammu: ERR_DEVICENOTEXIST. The selected driver cannot complete this request")

    monkeypatch.setattr(bridge_main.gammu_native, "invoke", invoke)
    agent.scan()
    assert agent.gammu_error == "ERR_DEVICENOTEXIST" and agent.gammu_tried() is True
    report = agent.doctor_report()
    assert report["gammu_error"] == "ERR_DEVICENOTEXIST"
    monkeypatch.setattr(bridge_main.gammu_native, "invoke", lambda c, a, **kw: {"manufacturer": "Nokia", "model": "E72"})
    agent._ports = None
    agent.at_failed.clear()
    agent.scan()
    assert agent.gammu_error == ""


def test_dku2_failure_code_reaches_the_doctor_with_the_x64_hint(agent, monkeypatch):
    monkeypatch.setattr(bridge_main, "is_windows", lambda: True)
    monkeypatch.setattr(bridge_main.gammu_native, "available", lambda: True)
    agent.host = host(usb=[WPD])
    agent.config["auto_usb"] = True

    def invoke(config, action, **kw):
        raise ValueError("Gammu: ERR_DEVICENOTEXIST. The selected driver cannot complete this request")

    monkeypatch.setattr(bridge_main.gammu_native, "invoke", invoke)
    agent._dku2_profiles([])
    assert agent.dku2_state == "no-response" and agent.gammu_error == "ERR_DEVICENOTEXIST"
    gammu = next(r for r in agent.doctor_report()["routes"] if r["id"] == "gammu")
    assert "x64" in gammu["action"][0] and "x64" in gammu["action"][1]


def test_replaced_transport_is_closed_and_inherits_the_route_hints(agent):
    old = agent.manager.transport
    spy = FakePccs()
    old.pccs = spy
    old.adapters["spy"] = spy
    agent.config["origins"] = []
    agent._signature = None  # force the reconfiguration path
    agent.refresh_config()
    assert spy.closed == 1 and agent.manager.transport is not old
    assert agent.manager.transport.route_hints is agent.route_hints


def test_closing_the_agent_closes_the_transport(tmp_path, monkeypatch):
    for variable in ("SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(variable, "")
    save_config(tmp_path, {"schema": 1, "auto_usb": False, "volumes": {}})
    instance = Agent(tmp_path, load_key(tmp_path), port=0, auto=False)
    spy = FakePccs()
    instance.manager.transport.adapters["spy"] = spy
    instance.close()
    assert spy.closed == 1


# ---- device rows and API ---------------------------------------------------------------------------------------------
def test_rows_carry_route_state_and_recommendation_but_no_phone_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMPORT_GAMMU_CONFIG", "")
    transports = LocalTransports(tmp_path)
    transports.route_hints = {"states": states(gammu="silent", pccs="available"), "recommended_for": {"pccs": ["contacts", "calendar"]},
                              "preference": "auto", "dead_end": True}
    transports.adapters["nokia-pccs"] = type("A", (), {"device": lambda self: {"id": "nokia-pccs", "transport": "pccs", "name": "Nokia E72", "connected": True,
                                                                              "capabilities": [], "capability_matrix": {}}})()
    rows = {r["id"]: r for r in transports.devices()}
    assert rows["nokia-pccs"]["route"] == "pccs" and rows["nokia-pccs"]["route_state"] == "available"
    assert rows["nokia-pccs"]["recommended_for"] == ["contacts", "calendar"]
    assert rows["nokia-pccs"]["dead_end"] is True and rows["nokia-pccs"]["route_preference"] == "auto"
    summary = {c["id"]: c for c in transports.last_connections}["nokia-pccs"]
    assert summary["route_state"] == "available" and summary["recommended_for"] == ["contacts", "calendar"] and summary["route"] == "pccs"
    assert "path" not in json.dumps(summary).lower()


def test_user_choice_in_the_app_is_journalled_without_touching_the_phone(tmp_path):
    class Transport:
        def devices(self):
            return [{"id": "nokia-pccs", "name": "Nokia E72", "connected": True, "capability_matrix": {}}]

        def execute(self, data):
            raise AssertionError("a route choice must not reach the phone")

    manager = DeviceManager(tmp_path, Transport())
    manager.execute({"action": "discover"})
    assert manager.execute({"action": "connection-select", "device": "nokia-pccs"}) == {"status": "selected", "device": "nokia-pccs"}
    with pytest.raises(ValueError):
        manager.execute({"action": "connection-select", "device": "nokia-ghost"})
    records = [json.loads(x) for x in manager.journal.read_text().splitlines()]
    assert [r["action"] for r in records] == ["connection-select"] and set(records[0]) == {"time", "action", "status"}


def test_settings_file_round_trip_keeps_the_preference(tmp_path):
    save_config(tmp_path, {"schema": 1, "volumes": {}, "transport_preference": "pccs"})
    assert Path(tmp_path / "settings.json").is_file() and load_config(tmp_path)["transport_preference"] == "pccs"


# ---- the settings window (Tk is stubbed; logic and save path are real) -----------------------------------------------
def run_settings(tmp_path, monkeypatch, pick=None):
    from symport.bridge import tray
    from tests.tk_stub import Tk

    stub = Tk(monkeypatch)
    monkeypatch.setattr(tray, "autostart", lambda enabled=None: False)
    monkeypatch.setattr("symport.bridge.doctor.windows_language_is_russian", lambda: False)
    if pick:
        stub.root.mainloop.side_effect = lambda: (stub.combobox[0].set(pick), stub.button("OK")())
    result = tray.settings_window(tmp_path, 24652)
    return result, {"combobox": stub.combobox}


def test_settings_window_offers_the_four_preferences_and_saves_the_chosen_one(tmp_path, monkeypatch):
    from symport.bridge import tray

    save_config(tmp_path, {"schema": 1, "volumes": {}, "transport_preference": "mtp"})
    result, created = run_settings(tmp_path, monkeypatch, pick=tray.TRANSPORT_LABELS["pccs"])
    variable, values = created["combobox"]
    assert values == list(tray.TRANSPORT_LABELS.values()) and len(values) == 4
    assert result == tray.SETTINGS_SAVED and load_config(tmp_path)["transport_preference"] == "pccs"


def test_settings_window_starts_on_the_stored_preference_and_defaults_to_auto(tmp_path, monkeypatch):
    from symport.bridge import tray

    save_config(tmp_path, {"schema": 1, "volumes": {}, "transport_preference": "gammu"})
    _, created = run_settings(tmp_path, monkeypatch)
    assert created["combobox"][0].get() == tray.TRANSPORT_LABELS["gammu"]
    (tmp_path / "settings.json").unlink()
    _, created = run_settings(tmp_path, monkeypatch)
    assert created["combobox"][0].get() == tray.TRANSPORT_LABELS["auto"]
