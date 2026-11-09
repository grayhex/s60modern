"""N97 mini: documented target identity and real adapter boundaries, with explicit test doubles."""

from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from symport.catalog.compatibility import assessment
from symport.catalog.model import Catalog
from symport.deps import analyze_package
from symport.devices.profiles import load_profile
from symport.phone import capabilities
from symport.phone.files import MountedVolumes
from symport.phone.gammu_adapter import GammuAdapter
from symport.phone.models import documented_model, presentation
from symport.phone.obex import ObexAdapter
from symport.phone.registry import (
    collection_profiles,
    identify,
    selectable_profiles,
    theme_profiles,
)
from symport.phone.service import DeviceManager
from symport.theme.studio import ThemeStudio
from tests.helpers import REPO, make_e32
from tests.test_deps import sis
from tests.test_transports import PROFILE


@pytest.mark.parametrize(
    "name",
    [
        "N97 mini",
        "Nokia N97mini",
        "n97 MINI",
        "N97 mini-1",
        "N97mini-1",
        "Nokia N97  mini - 1 (RM-555)",
    ],
)
def test_specific_verified_name_forms_have_priority_over_n97(name):
    p = presentation(name)
    assert p["id"] == "nokia-n97-mini"
    assert documented_model(name)["model"] == "N97 MINI"
    assert p["symbian"] == "9.4" and p["s60"] == "5th"
    assert p["screen"]["width"] == 360 and p["screen"]["height"] == 640
    assert p["image"] == "/phones/nokia-n97-mini.png" and p["artwork"] is None
    assert p["measured"] is False and p["hardware"] == "not-tested"
    assert p["theme_studio"]["status"] == "unsupported"


def test_mini_has_a_separate_transparent_handset_asset_without_enabling_theme_geometry():
    profile = identify("N97 mini")
    path = REPO / "symport/web_assets" / profile["image"].lstrip("/")
    with Image.open(path) as image:
        assert image.mode == "RGBA" and image.height >= 1000
        assert image.getpixel((0, 0))[3] == 0
        left, top, right, bottom = image.getbbox()
        # Closed Mini body: ~52.5 mm / 113 mm, with no non-uniform stretch.
        assert abs((right - left) / (bottom - top) - 52.5 / 113) < 0.04
    assert profile["artwork"] is None and "nokia-n97-mini" not in theme_profiles()


@pytest.mark.parametrize("name", ["N97", "Nokia N97 (RM-505)", "N97 RM-555"])
def test_plain_n97_is_not_mini(name):
    assert identify(name) is None
    assert presentation(name)["id"] == "nokia-n97"
    assert presentation(name)["screen"] is None


@pytest.mark.parametrize(
    "name",
    ["N97 mini-2", "N97mini-9", "N97miniature", "XN97mini", "N97 mini N95-2", "N97 mini RM-159"],
)
def test_unknown_suffix_and_conflicts_do_not_promote_a_mini(name):
    assert identify(name) is None
    assert presentation(name) is None


def test_unknown_rm_and_product_code_are_not_identity_probes():
    assert identify("", rm="RM-555", product_code="unverified") is None
    assert identify("N97 mini", rm="RM-555")["identity_basis"] == "reported-model"
    p = load_profile(REPO / "profiles/nokia-n97-mini.toml")
    for key in (
        "device.rm",
        "device.product_code",
        "firmware.version",
        "storage.mounted_volume",
        "storage.free_bytes",
        "connection.write_permissions",
    ):
        assert p.fact(key)["status"] == "unknown" and p.value(key) in ("", None)
    assert p.value("display.open_resolution") == [640, 360]
    assert all(fact["source"] for _, fact in p.facts())


def test_5th_platform_has_no_automatic_third_edition_acceptance():
    target = {"model": "Nokia N97 mini", "os": "9.4"}
    report = analyze_package(sis(0xE0001111, "5th", platforms=(0x1028315F,)), target=target)
    assert report["platform_verdict"] == "supported-static"
    assert report["platforms"][0]["basis"] == "documented"
    assert report["levels"]["physical_device"] == "not-tested"
    for uid in (0x101F7961, 0x102032BE, 0x102752AE, 0x20022E6D):
        r = analyze_package(sis(0xE0001111, "other platform", platforms=(uid,)), target=target)
        assert r["platform_verdict"] == "unsupported" and r["verdict"] == "blocked"
    multi = analyze_package(
        sis(0xE0001111, "3rd/5th", platforms=(0x101F7961, 0x1028315F)), target=target
    )
    assert multi["platform_verdict"] == "supported-static"
    native = analyze_package(
        sis(
            0xE0001111,
            "needs firmware",
            platforms=(0x1028315F,),
            exe=make_e32(imports={"euser.dll": [1]}),
        ),
        target=target,
    )
    assert native["verdict"] == "unresolved" and "system-dll-unverified" in native["issues"]
    mismatch = analyze_package(
        sis(0xE0001111, "5th", platforms=(0x1028315F,)), target=dict(target, os="9.3")
    )
    assert mismatch["verdict"] == "blocked"


def test_collections_and_catalog_keep_mini_evidence_separate():
    assert collection_profiles()["nokia-n97-mini"]["screen"] == [360, 640]
    assert "nokia-n97-mini" in selectable_profiles()
    catalog = Catalog(REPO / "catalog")
    assert all(
        e["model_compatibility"]["nokia-n97-mini"]["status"] != "verified"
        for e in catalog.entries.values()
    )
    entry = copy.deepcopy(catalog.entries["xplore"])
    entry.update(compatibility="verified-on-e52", e52_evidence=["https://example.com/e52-test"])
    assert assessment(entry, "nokia-n97-mini")["status"] == "not-tested"
    assert "s60-5th" in entry["platform_compatibility"]


@pytest.mark.parametrize("action", ["create", "import"])
def test_no_theme_can_be_created_or_imported_for_mini(tmp_path, action):
    studio = ThemeStudio(tmp_path)
    assert "nokia-n97-mini" not in theme_profiles()
    assert "nokia-n97-mini" not in studio.execute({"action": "list"})["profiles"]
    with pytest.raises(ValueError, match="S60 5th"):
        studio.execute(
            {"action": action, "profile": "nokia-n97-mini", "name": "Blocked", "content": "bad"}
        )
    assert not list(studio.root.glob("*/project.json"))


def test_existing_project_cannot_be_retargeted_to_mini_and_built(tmp_path):
    studio = ThemeStudio(tmp_path)
    p = studio.execute({"action": "create", "name": "test", "profile": "nokia-e52"})
    p["profile"] = "nokia-n97-mini"
    (studio.folder(p["id"]) / "project.json").write_text(json.dumps(p), encoding="utf-8")
    for action in ("project", "build", "export"):
        with pytest.raises(ValueError, match="S60 5th"):
            studio.execute(
                {
                    "action": action,
                    "id": p["id"],
                    "revision": p["revision"],
                    "confirm_unsigned": True,
                }
            )
    assert not (studio.folder(p["id"]) / "theme.sis").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="The isolated theme worker is Linux-only")
def test_isolated_worker_rejects_mini_before_processing_resources(tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({"profile": "nokia-n97-mini"}))
    result = subprocess.run(
        [sys.executable, "-m", "symport.theme.studio_worker", str(REPO), str(tmp_path), "bitmap"],
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "S60 5th theme processing is not supported" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["project.json"]


@pytest.mark.parametrize("kind", ["gammu-usb", "gammu-bluetooth", "gammu-s60", "gammu-obex"])
def test_existing_gammu_driver_scopes_accept_mini_but_probe_stores_individually(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr("symport.phone.gammu_adapter.gammu_native.available", lambda: True)
    cfg = tmp_path / "gammurc"
    cfg.write_text("[gammu]\ndevice=COM7\nconnection=at\n")
    adapter = GammuAdapter(tmp_path, "phone", str(cfg), kind=kind)
    adapter.personal.execute = lambda *a: {
        "manufacturer": "Nokia",
        "model": "Nokia N97mini",
        "rm": None,
        "firmware": None,
        "battery": None,
        "sms_send_verified": False,
        "capability_status": {
            "contacts": {"status": "available", "reason": "synthetic store status call"},
            "sms": {"status": "unavailable", "reason": "ERR_NOTSUPPORTED"},
            "calendar": {"status": "unavailable", "reason": "ERR_TIMEOUT"},
        },
    }
    row = adapter.device()
    matrix = row["capability_matrix"]
    assert row["connected"] and row["platform_info"]["edition"] == "5th"
    assert matrix["contacts.read"]["status"] == "available"
    assert matrix["sms.read"]["status"] == "unsupported"
    assert matrix["calendar.read"]["status"] == "error"
    assert matrix["tasks.read"]["status"] == "not-tested"
    assert matrix["contacts.write"]["status"] == "not-tested"
    assert matrix["sms.send"]["attempt"] == "blocked"
    assert row["firmware"] is None and row["battery"] is None
    original = row["capability_fingerprint"]
    adapter.personal.plans["old-plan"] = {"private": "old target"}
    adapter.personal.snapshots["old-snapshot"] = {"private": "old target"}
    cfg.write_text("[gammu]\ndevice=COM9\nconnection=at\n")
    assert adapter.device()["capability_fingerprint"] != original
    assert str(cfg) not in json.dumps(row)
    assert not adapter.personal.plans and not adapter.personal.snapshots


def test_changed_gammu_config_invalidates_a_pending_write_without_another_discovery(tmp_path):
    cfg = tmp_path / "gammurc"
    cfg.write_text("[gammu]\ndevice=COM7\nconnection=at\n")
    adapter = GammuAdapter(tmp_path, "phone", str(cfg))
    adapter.bind_config(str(cfg))
    adapter.personal.snapshots["snapshot"] = {"created": time.time()}
    adapter.personal.plans["plan"] = {"created": time.time()}
    calls = []
    adapter.personal.invoke = lambda *args, **kwargs: calls.append((args, kwargs))
    cfg.write_text("[gammu]\ndevice=COM9\nconnection=at\n")
    with pytest.raises(ValueError, match="preview expired"):
        adapter.execute(
            {"action": "personal-apply", "plan": "plan", "snapshot": "snapshot", "confirm": True}
        )
    assert not adapter.personal.plans and not adapter.personal.snapshots
    assert not calls  # Nothing was read from or written to the newly configured phone.


def test_native_identity_separates_same_model_phones_on_the_same_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("symport.phone.gammu_adapter.gammu_native.available", lambda: True)
    cfg = tmp_path / "gammurc"
    cfg.write_text("[gammu]\ndevice=COM7\nconnection=at\n")
    adapter = GammuAdapter(tmp_path, "phone", str(cfg), kind="gammu-usb")
    details = {"model": "Nokia N97 mini", "manufacturer": "Nokia", "fingerprint": "phone-A"}
    adapter.personal.execute = lambda *args: dict(details)
    first = adapter.device()
    adapter.personal.plans["old-plan"] = {"private": "phone A"}
    adapter.personal.snapshots["old-snapshot"] = {"private": "phone A"}
    details["fingerprint"] = "phone-B"
    second = adapter.device()
    assert first["capability_fingerprint"] != second["capability_fingerprint"]
    assert first["connection_identity"] != second["connection_identity"]
    assert not adapter.personal.plans and not adapter.personal.snapshots


def test_replaced_windows_disk_on_same_letter_resets_only_its_old_proofs(tmp_path, monkeypatch):
    from symport.bridge.main import Agent

    agent = object.__new__(Agent)
    agent.root = tmp_path
    agent._signature = None
    agent._auto_profiles = []
    agent._auto_roots = {"auto-card-e": "E:/"}
    agent._volume_identities = {"auto-card-e": "old-device"}
    agent._enrolled_identities = dict(agent._volume_identities)
    agent.manager = SimpleNamespace(
        lock=threading.RLock(),
        observed=SimpleNamespace(proofs={"auto-card-e": {"write": "old-proof"}, "manual": {}}),
        rows={"auto-card-e": {"connected": True}, "manual": {}},
    )
    agent.server = SimpleNamespace(origins=set())
    monkeypatch.setattr("symport.bridge.main.load_config", lambda _: {"auto_usb": True})
    monkeypatch.setattr("symport.bridge.main.load_profiles", lambda _: [])

    def local_transports(_, *, volume_identities):
        return SimpleNamespace(volume_identities=dict(volume_identities))

    monkeypatch.setattr("symport.bridge.main.LocalTransports", local_transports)
    for key in ("SYMPORT_DEVICE_ROOTS", "SYMPORT_GAMMU_CONFIG", "SYMPORT_CONNECTIONS_FILE"):
        monkeypatch.setenv(key, "")
    agent.refresh_config()
    previous = agent._signature
    assert "auto-card-e" in agent.manager.observed.proofs
    agent._volume_identities = {"auto-card-e": "replacement-device"}
    agent.refresh_config()
    assert agent._signature != previous
    assert agent.manager.observed.proofs == {"manual": {}}
    assert agent.manager.rows == {"manual": {}}
    assert agent._enrolled_identities == agent._volume_identities
    assert agent.manager.transport.volume_identities == agent._volume_identities


def test_host_volume_identity_changes_fingerprint_even_if_path_and_stat_are_reused(tmp_path):
    first = MountedVolumes(
        {"slot": str(tmp_path)}, identities={"slot": "usb-A:volume-A"}
    ).devices()[0]
    same = MountedVolumes({"slot": str(tmp_path)}, identities={"slot": "usb-A:volume-A"}).devices()[
        0
    ]
    replacement = MountedVolumes(
        {"slot": str(tmp_path)}, identities={"slot": "usb-A:volume-B"}
    ).devices()[0]
    assert first["capability_fingerprint"] == same["capability_fingerprint"]
    assert first["capability_fingerprint"] != replacement["capability_fingerprint"]
    assert "volume-A" not in json.dumps(first) and str(tmp_path) not in json.dumps(first)


def test_enrolled_volume_is_not_mini_pim_or_internal_c_and_proofs_do_not_follow_a_new_root(
    tmp_path,
):
    a, b = tmp_path / "mass-memory", tmp_path / "card"
    a.mkdir()
    b.mkdir()
    manager = DeviceManager(tmp_path, MountedVolumes({"slot": str(a)}))
    row = manager.execute({"action": "discover"})["devices"][0]
    assert row["model_profile"] is None and row["model"] is None
    assert row["capability_matrix"]["contacts.read"]["status"] == "unsupported"
    assert row["capability_matrix"]["sms.send"]["status"] == "unsupported"
    assert row["capability_matrix"]["files.upload"]["status"] == "not-tested"
    manager.execute(
        {
            "action": "upload",
            "device": "slot",
            "path": "safe.txt",
            "content": base64.b64encode(b"safe").decode(),
            "confirm": True,
        }
    )
    first = manager.execute({"action": "discover"})["devices"][0]
    assert first["capability_matrix"]["files.upload"]["status"] == "available"
    manager.transport = MountedVolumes({"slot": str(b)})
    second = manager.execute({"action": "discover"})["devices"][0]
    assert second["capability_fingerprint"] != first["capability_fingerprint"]
    assert second["capability_matrix"]["files.upload"]["status"] == "not-tested"
    assert str(a) not in json.dumps(first) and str(b) not in json.dumps(second)
    assert not list(b.iterdir())
    with pytest.raises(ValueError):
        manager.execute(
            {
                "action": "upload",
                "device": "slot",
                "path": "C:/private/a",
                "content": "eA==",
                "confirm": True,
            }
        )


@pytest.mark.parametrize("kind", ["obexftp", "obex-opp"])
def test_bluetooth_services_do_not_prove_identity_or_pim_and_addresses_are_scoped(kind):
    profile = dict(PROFILE, id="mini-bt", name="User-selected N97 mini", edition="5th", kind=kind)

    def run(*a, **k):
        return b"<folder-listing/>"

    row = ObexAdapter(profile, run).device()
    other = ObexAdapter(dict(profile, address="11:22:33:44:55:66"), run).device()
    assert row["model"] is None
    assert row["capability_matrix"]["contacts.read"]["status"] == "unsupported"
    assert row["capability_matrix"]["sms.send"]["status"] == "unsupported"
    assert row["capability_fingerprint"] != other["capability_fingerprint"]
    assert profile["address"] not in json.dumps(row)
    if kind == "obex-opp":
        assert row["connected"] is False and row["configured"] is True
        assert row["capability_matrix"]["files.download"]["status"] == "unsupported"


def test_usb_mode_changes_invalidate_previous_results():
    row = {
        "id": "mini",
        "transport": "gammu",
        "connection_kind": "gammu-usb",
        "model": "Nokia N97 mini",
    }
    assert capabilities.fingerprint(dict(row, usb_mode="PC Suite")) != capabilities.fingerprint(
        dict(row, usb_mode="Mass Storage")
    )


def test_windows_serial_and_disk_candidates_are_only_hints(monkeypatch):
    import sys

    from symport.bridge import discovery

    port = SimpleNamespace(
        device="COM7",
        vid=0x0421,
        manufacturer="Nokia",
        description="N97mini USB Serial",
        product="Nokia",
    )
    ports = SimpleNamespace(comports=lambda: [port])
    tools = SimpleNamespace(list_ports=ports)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(tools=tools))
    monkeypatch.setitem(sys.modules, "serial.tools", tools)
    candidates, error = discovery.serial_candidates()
    assert not error and candidates[0]["model_profile"]["model"] == "N97 MINI"
    assert candidates[0]["model_profile"]["measured"] is False and not candidates[0].get(
        "connected"
    )
    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="nt", environ={"SystemDrive": "D:"}))

    def run(args, **kw):
        script = args[-1]
        assert "Win32_DiskDrive" in script and "InterfaceType -eq 'USB'" in script
        assert "DriveType -eq 3" in script and "$env:SystemDrive" in script
        assert "VolumeSerialNumber" in script  # A new card in the same handset is a new volume.
        rows = [
            {
                "root": "E:/",
                "description": "Nokia N97 mini USB Device",
                "identity": "SYNTHETIC-USB-ID",
            },
            {
                "root": "D:/",
                "description": "Nokia N97 mini USB Device",
                "identity": "SYSTEM-MUST-BE-IGNORED",
            },
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    monkeypatch.setattr(discovery.children, "run", run)
    disks, error = discovery.windows_nokia_volumes()
    assert not error and len(disks) == 1 and disks[0]["root"] == "E:/"
    assert disks[0]["model_profile"]["model"] == "N97 MINI"
    assert "SYNTHETIC-USB-ID" not in json.dumps(disks)
