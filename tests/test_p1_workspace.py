"""P1 uses actual package/compiler/filesystem code and explicit transport doubles, never a handset claim."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from symport.catalog.service import AppCatalog
from symport.phone.diagnostics import DeviceDiagnostics
from symport.phone.files import MountedVolumes
from symport.phone.service import DeviceManager
from symport.sis.reader import extract, inspect
from symport.sis.writer import SisFile, SisPackage, build
from symport.theme.studio import ThemeStudio
from symport.web import Workbench
from tests.helpers import s60_tools
from tests.test_catalog import FixtureOpener, catalog_fixture, public_address, wait_job
from tests.test_web import _http, http_server, repo  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def collections(tmp_path):
    sources = catalog_fixture(tmp_path)
    volume = tmp_path / "card"
    volume.mkdir()
    manager = DeviceManager(tmp_path, MountedVolumes({"card": volume}))
    service = AppCatalog(tmp_path, manager)
    service.downloader.resolver = public_address

    # Every download fixture has different MIDlet bytes, selected by its curated filename.
    class Opener:
        def open(self, request, timeout):
            from tests.test_catalog import jar_bytes

            name = request.full_url.rsplit("/", 1)[-1].split(".")[0]
            return FixtureOpener(jar_bytes(name)).open(request, timeout)

    service.downloader.opener = Opener()
    return service, sources, volume


def collection(service, ids=None):
    return service.execute(
        {
            "action": "collection-save",
            "name": "Мой набор",
            "profile": "nokia-e52",
            "ids": ids or ["example"],
        }
    )


def test_collections_roundtrip_pins_dependencies_and_safe_import(collections):
    service, sources, _ = collections
    c = collection(service)
    assert [a["id"] for a in c["apps"]] == ["runtime", "example"]
    assert c["apps"][0]["artifacts"][0]["sha256"] == sources["runtime"]["artifacts"][0]["sha256"]
    doc = service.execute({"action": "collection-export", "id": c["id"]})
    doc.update(
        url="https://evil.invalid/file",
        command="do not execute",
        installed=True,
        device="private-imei",
    )
    imported = service.execute({"action": "collection-import", "document": doc})
    assert imported["id"] != c["id"]
    assert not {"url", "command", "installed", "device"} & imported.keys()
    plan = service.execute(
        {"action": "recovery-plan", "id": imported["id"], "profile": "nokia-e72"}
    )
    assert plan["geometry_changed"] and plan["target"]["screen"] == [320, 240]
    assert plan["transfer_ids"] == ["runtime", "example"] and plan["installation"] == "not-tested"
    assert not plan["problems"]
    assert (
        len(
            AppCatalog(service.root.parents[2], service.devices).execute({"action": "collections"})[
                "collections"
            ]
        )
        == 2
    )


def test_recovery_binds_preview_and_transfers_only_pinned_bytes(collections):
    service, _, volume = collections
    c = collection(service)
    req = {"id": c["id"], "device": "card", "profile": "nokia-e52"}
    p = service.execute({"action": "recovery-plan", **req})
    assert p["transfer_ids"] == ["runtime", "example"]
    with pytest.raises(ValueError, match="current recovery preview"):
        service.execute(
            {"action": "recovery-transfer", **req, "confirm": True, "preview_sha256": "bad"}
        )
    outcome = service.execute(
        {
            "action": "recovery-transfer",
            **req,
            "confirm": True,
            "preview_sha256": p["preview_sha256"],
        }
    )
    job = wait_job(service, outcome["job"]["id"])
    assert all(i["stage"] == "awaiting-installation" for i in job["items"])
    assert len(list(volume.glob("*.jar"))) == 2
    with pytest.raises(ValueError, match="current recovery preview"):
        service.execute(
            {
                "action": "recovery-transfer",
                **req,
                "confirm": True,
                "preview_sha256": p["preview_sha256"],
            }
        )
    service.catalog.sources["runtime"]["artifacts"][0]["sha256"] = "0" * 64
    changed = service.execute({"action": "recovery-plan", **req})
    assert changed["transfer_ids"] == []
    assert [r["status"] for r in changed["rows"]] == ["changed", "blocked-dependency"]
    with pytest.raises(ValueError, match="current recovery preview"):
        service.execute(
            {
                "action": "recovery-transfer",
                **req,
                "confirm": True,
                "preview_sha256": p["preview_sha256"],
            }
        )


def test_partial_recovery_requires_ack_and_respects_model(collections):
    service, _, _ = collections
    c = collection(service, ["runtime", "manual", "blocked"])
    req = {"id": c["id"], "device": "card", "profile": "nokia-e52"}
    p = service.execute({"action": "recovery-plan", **req})
    assert p["partial"] and p["transfer_ids"] == ["runtime"]
    with pytest.raises(ValueError, match="partial set"):
        service.execute(
            {
                "action": "recovery-transfer",
                **req,
                "confirm": True,
                "preview_sha256": p["preview_sha256"],
            }
        )
    service.devices.transport.devices = lambda: [
        __import__("tests.capfake", fromlist=["row"]).row(
            {"id": "card", "connected": True, "model": "Nokia E72"},
            untested_writes=["packages.transfer"],
        )
    ]
    p = service.execute({"action": "recovery-plan", **req})
    assert any("differs" in r for r in p["problems"])
    with pytest.raises(ValueError):
        service.execute({"action": "collection-delete", "id": c["id"]})


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema="other"),
        lambda d: d.update(profile="android"),
        lambda d: d["apps"].append(d["apps"][0]),
        lambda d: d["apps"][0].update(id="../path"),
        lambda d: d["apps"][0]["artifacts"][0].update(name="../bad.jar"),
        lambda d: d["apps"][0]["artifacts"][0].update(sha256="unverified"),
    ],
)
def test_collection_import_rejects_unsafe_manifests(collections, change):
    service, _, _ = collections
    doc = service.execute({"action": "collection-export", "id": collection(service)["id"]})
    change(doc)
    with pytest.raises(ValueError):
        service.execute({"action": "collection-import", "document": doc})


class ObservedTransport:
    def __init__(self):
        self.observed = {
            "id": "private-connection",
            "model": "Nokia E52",
            "rm": "RM-469",
            "firmware": "081.003",
            "connected": True,
            "transport": "test-double",
            "capability_matrix": __import__("tests.capfake", fromlist=["row"]).row(
                {}, available=["identity.read"]
            )["capability_matrix"],
            "imei": "123456789012345",
            "battery": 0,
        }
        self.details = {
            "battery": 0,
            "signal": 80,
            "free": 1000,
            "total": 2000,
            "life_timer_hours": None,
            "messages": ["PRIVATE SMS"],
        }

    def devices(self):
        return [dict(self.observed)]

    def execute(self, data):
        if isinstance(self.details, Exception):
            raise self.details
        return self.details


def test_diagnostics_actual_zero_unknown_series_exports_and_privacy(tmp_path):
    transport = ObservedTransport()
    manager = DeviceManager(tmp_path, transport)
    diagnostics = DeviceDiagnostics(tmp_path, manager)
    req = {"device": "private-connection"}
    first = diagnostics.execute({"action": "sample", **req})["sample"]
    assert first["battery"] == 0 and first["signal"] == 80 and first["life_timer_hours"] is None
    transport.details = {"battery": float("nan"), "signal": 101, "free": 3000, "total": 2000}
    second = diagnostics.execute({"action": "sample", **req})["sample"]
    assert second["battery"] is None and second["signal"] is None and second["free"] is None
    assert second["reasons"]
    transport.observed["model"] = "=Nokia E72"
    transport.observed["rm"] = "RM-530"
    third = diagnostics.execute({"action": "sample", **req})["sample"]
    assert first["series"] != third["series"] and third["reasons"]
    raw = base64.b64decode(diagnostics.execute({"action": "export", **req})["content"]).decode()
    assert (
        "symport.diagnostics/1" in raw and "123456789012345" not in raw and "PRIVATE SMS" not in raw
    )
    assert "private-connection" not in raw and "imei" not in raw.lower()
    csv = base64.b64decode(
        diagnostics.execute({"action": "export", **req, "format": "csv"})["content"]
    ).decode()
    assert "'=Nokia E72" in csv
    assert "PRIVATE" not in manager.journal.read_text()
    transport.details = ValueError("secret IMEI 123456789012345")
    result = diagnostics.execute({"action": "sample", **req})
    assert "123456789012345" not in json.dumps(result)
    with pytest.raises(ValueError):
        diagnostics.execute({"action": "clear", **req})
    diagnostics.execute({"action": "clear", **req, "confirm": True})
    assert diagnostics.execute({"action": "history", **req}) == {"measurements": []}


def png(width=48, height=48, color="#1595ab"):
    stream = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode()


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    if not s60_tools():
        pytest.skip(
            "Native compiler integration runs in the theme/browser CI job after tools/s60/build.sh"
        )
    root = tmp_path_factory.mktemp("compiled-themes")
    studio = ThemeStudio(root)
    outputs = {}
    for profile in ["nokia-e52", "nokia-e72"]:
        p = studio.execute({"action": "create", "profile": profile, "name": "P1 Theme"})
        p = studio.execute(
            {
                "action": "wallpaper",
                "id": p["id"],
                "revision": p["revision"],
                "content": png(640, 480),
            }
        )
        p = studio.execute(
            {
                "action": "icon",
                "id": p["id"],
                "revision": p["revision"],
                "uid": "100058ec",
                "content": png(),
            }
        )
        p = studio.execute(
            {"action": "build", "id": p["id"], "revision": p["revision"], "confirm_unsigned": True}
        )
        outputs[profile] = studio.file(p["id"])[1]
        assert p["built_revision"] == p["revision"] and p["build"]["installation"] == "not-tested"
        assert {r["kind"] for r in p["resources"]} == {"skn", "mbm", "mif"}
        mbm = next(r for r in p["resources"] if r["kind"] == "mbm")
        assert any(
            (b["width"], b["height"]) == ((240, 320) if profile == "nokia-e52" else (320, 240))
            for b in mbm["bitmaps"]
        )
    return outputs


def test_theme_import_bitmap_replace_repack_and_same_bytes_handoff(tmp_path, compiled):
    studio = ThemeStudio(tmp_path)
    raw = compiled["nokia-e52"]
    p = studio.execute(
        {"action": "import", "name": "Repacked Theme", "content": base64.b64encode(raw).decode()}
    )
    req = {"id": p["id"], "revision": p["revision"]}
    assert p["mode"] == "imported" and not p["blockers"]
    assert (studio.folder(p["id"]) / "original.sis").read_bytes() == raw
    with pytest.raises(ValueError, match="opaque"):
        studio.execute({"action": "save", **req, "palette": p["palette"]})
    mbm = next(r for r in p["resources"] if r["kind"] == "mbm")
    view = studio.execute({"action": "bitmap-preview", **req, "resource": mbm["id"], "index": 0})
    assert Image.open(io.BytesIO(base64.b64decode(view["content"]))).size == (240, 320)
    p = studio.execute(
        {
            "action": "bitmap-replace",
            **req,
            "resource": mbm["id"],
            "index": 0,
            "content": png(color="#ff3020"),
        }
    )
    assert p["revision"] == req["revision"] + 1
    p = studio.execute(
        {"action": "build", "id": p["id"], "revision": p["revision"], "confirm_unsigned": True}
    )
    name, rebuilt, kind = studio.file(p["id"])
    assert kind == "theme" and inspect(rebuilt)["package"]["version"] == [1, 0, 1]
    assert hashlib.sha256(rebuilt).hexdigest() == p["build"]["sha256"]
    before, after = extract(raw), extract(rebuilt)
    assert {k for k in before if before[k] != after[k]} == {
        next(k for k in before if k.endswith(".mbm"))
    }
    mbm_target = next(k for k in before if k.endswith(".mbm"))
    decoded = []
    for label, payload in [("before", before[mbm_target]), ("after", after[mbm_target])]:
        directory = tmp_path / label
        directory.mkdir()
        container = directory / "theme.mbm"
        container.write_bytes(payload)
        targets = [directory / f"{i}.bmp" for i in range(len(mbm["bitmaps"]))]
        subprocess.run(
            [str(ROOT / "tools/s60/bin/bmconv"), "-u", str(container), *[str(p) for p in targets]],
            check=True,
            capture_output=True,
            timeout=20,
        )
        decoded.append([Image.open(p).convert("RGB").tobytes() for p in targets])
    assert decoded[0][0] != decoded[1][0]
    assert decoded[0][1:] == decoded[1][1:]  # No collateral bitmap/mask changes.
    volume = tmp_path / "phone-card"
    volume.mkdir()
    w = Workbench(tmp_path)
    w.theme_studio = studio
    w.devices.transport = MountedVolumes({"card": volume})
    report = w.inspector.execute(
        {
            "action": "analyze",
            "source": "theme-studio",
            "reference": p["id"],
            "kind": "theme",
            "target": {"model": "Nokia E52", "os": "9.3"},
        }
    )
    assert report["sha256"] == p["build"]["sha256"]
    result = w.inspector.execute(
        {
            "action": "transfer",
            "id": report["id"],
            "device": "card",
            "path": name,
            "confirm": True,
            "ack_unverified": True,
        }
    )
    assert (volume / name).read_bytes() == rebuilt
    assert result["status"] == "transferred-not-installed"
    assert w.inspector.report(report["id"])["transfer"]["physical_installation"] == "not-tested"


def test_theme_validation_revision_build_guards_and_opaque_resources(tmp_path):
    studio = ThemeStudio(tmp_path)
    p = studio.execute({"action": "create", "name": "Safe Theme"})
    req = {"id": p["id"], "revision": p["revision"]}
    with pytest.raises(ValueError, match="Build"):
        studio.file(p["id"])
    with pytest.raises(ValueError, match="acknowledge"):
        studio.execute({"action": "build", **req})
    with pytest.raises(ValueError):
        studio.execute({"action": "save", **req, "palette": {"bg": "url(evil)"}})
    palette = dict(p["palette"], text="#112233")
    p = studio.execute({"action": "save", **req, "palette": palette})
    assert p["contrast"] and p["palette"]["text"] == "#112233"
    with pytest.raises(ValueError, match="changed"):
        studio.execute({"action": "save", **req, "palette": palette})
    with pytest.raises(ValueError):
        studio.execute({"action": "project", "id": "../../outside"})
    with pytest.raises(ValueError):
        studio.mif(struct.pack("<4I", 0x34232342, 2, 16, 1025))
    bad = build(
        SisPackage(
            0xE1234567,
            "Not a theme",
            "Test",
            files=[SisFile("!:\\sys\\bin\\test.exe", b"not executed")],
        )
    )
    imported = studio.execute(
        {"action": "import", "name": "Unsafe paths", "content": base64.b64encode(bad).decode()}
    )
    assert imported["blockers"]
    with pytest.raises(ValueError, match="blocked"):
        studio.execute(
            {"action": "build", "id": imported["id"], "revision": 1, "confirm_unsigned": True}
        )


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/theme-studio", {"action": "list"}),
        ("/api/diagnostics", {"action": "history", "device": "card"}),
        ("/api/catalog", {"action": "collections"}),
    ],
)
def test_p1_http_endpoints_remain_authenticated(http_server, path, body):  # noqa: F811
    state, port = http_server
    assert _http(port, "POST", path, body)[0] == 403
    headers = {"X-Symport-Token": state.token}
    status, raw = _http(port, "POST", path, body, headers)
    assert status == 200, raw
    assert _http(port, "POST", path, body, {**headers, "Origin": "https://evil.invalid"})[0] == 403


def test_recovery_preview_expires_and_reports_conflicts_and_unknown_runtime(collections):
    import time

    service, _, _ = collections
    service.catalog.entries["example"]["runtime_requirements"] = [
        {"name": "Qt", "min_version": "4.7.3", "status": "unverified"}
    ]
    c = collection(service)
    req = {"id": c["id"], "device": "card", "profile": "nokia-e52"}
    p = service.execute({"action": "recovery-plan", **req})
    assert p["rows"][1]["runtime_requirements"][0]["status"] == "unverified"
    service.collections.previews[p["preview_sha256"]]["created"] = time.monotonic() - 601
    with pytest.raises(ValueError, match="current recovery preview"):
        service.execute(
            {
                "action": "recovery-transfer",
                **req,
                "confirm": True,
                "preview_sha256": p["preview_sha256"],
            }
        )
    service.catalog.entries["example"]["conflicts_with"] = ["runtime"]
    assert service.execute({"action": "recovery-plan", **req})["problems"]


def test_inspector_reuses_e72_profile_and_rejects_other_firmware_inventory(tmp_path):
    from tests.helpers import make_e32

    raw = build(
        SisPackage(
            0xE1234567, "E72 probe", "Test", files=[SisFile("!:\\sys\\bin\\probe.exe", make_e32())]
        )
    )
    w = Workbench(tmp_path)
    request = {
        "action": "analyze",
        "source": "upload",
        "name": "probe.sis",
        "content": base64.b64encode(raw).decode(),
        "target": {"model": "Nokia E72", "os": "9.3", "firmware": "091.004"},
    }
    report = w.inspector.execute(request)
    assert report["binaries"][0]["checks"]
    assert not any("profile unknown" in s for s in report["limitations"])
    with pytest.raises(ValueError, match="must match"):
        w.inspector.execute(
            dict(
                request,
                firmware_profile={
                    "schema": 1,
                    "device": "nokia-e52",
                    "firmware_version": "091.004",
                    "dlls": {},
                },
            )
        )


def test_p1_catalog_sources_remain_honest_and_do_not_offer_stubs():
    from symport.catalog.model import Catalog

    catalog = Catalog(ROOT / "catalog")
    for key in [
        "opera-mini",
        "fshell",
        "qremotecontrol",
        "internet-radio",
        "rotate-it",
        "nano-counter",
        "filelist",
    ]:
        entry = catalog.entries[key]
        assert entry["delivery"] == "manual" and not entry["e52_evidence"]
        assert entry["compatibility"] not in (
            "verified-on-e52",
            "verified-on-s60",
            "likely-compatible",
        )
        assert not any("stub" in a["name"].lower() for a in catalog.sources[key]["artifacts"])
    for key in ["rotate-it", "nano-counter", "filelist"]:
        assert catalog.entries[key]["compatibility"] == "unsupported"
        assert catalog.sources[key]["inspection"]["execution"] == "not-tested"
    assert catalog.entries["xplore"]["delivery"] == "automatic"


def test_legacy_sets_migrate_without_fabricated_version_pins(collections):
    service, _, _ = collections
    legacy = service.root / "sets.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"Old set": ["example", "runtime"]}))
    reopened = AppCatalog(service.root.parents[2], service.devices)
    saved = reopened.execute({"action": "collections"})["collections"][0]
    assert saved["apps"][0]["version"] == "legacy-unpinned"
    p = reopened.execute({"action": "recovery-plan", "id": saved["id"]})
    assert not p["transfer_ids"] and legacy.exists()
    revised = reopened.execute(
        {
            "action": "collection-save",
            "id": saved["id"],
            "name": saved["name"],
            "profile": "nokia-e52",
            "ids": ["example"],
        }
    )
    assert revised["apps"][0]["artifacts"]
