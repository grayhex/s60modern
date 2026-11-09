"""N95 support uses documented identity and synthetic packages, never a handset attestation."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

from symport.catalog.compatibility import assessment
from symport.catalog.model import Catalog, validate_entry
from symport.devices.profiles import load_profile
from symport.phone.models import presentation
from symport.phone.registry import registry, selectable_profiles
from symport.sis.reader import inspect
from symport.theme.studio import ThemeStudio
from tests.helpers import REPO, make_e32, s60_tools
from tests.test_deps import sis


@pytest.mark.parametrize(
    "model,rm,expected",
    [
        ("Nokia N95-1", None, "nokia-n95"),
        ("N95-3", None, "nokia-n95"),
        ("N95-5", None, "nokia-n95"),
        ("N95", "RM-159", "nokia-n95"),
        ("N95", "RM-160", "nokia-n95"),
        ("", "RM-245", "nokia-n95"),
        ("N95 8GB", None, "nokia-n95-8gb"),
        ("N95-2", None, "nokia-n95-8gb"),
        ("N95-4", None, "nokia-n95-8gb"),
        ("N95-6", None, "nokia-n95-8gb"),
        ("N95", "RM-320", "nokia-n95-8gb"),
        ("", "RM-421", "nokia-n95-8gb"),
        ("N95", "RM-321", "nokia-n95-8gb"),
        ("Nokia N95", None, "nokia-n95-family"),
        ("N95-2", "RM-159", "nokia-n95-family"),
        ("N95", "RM-999", "nokia-n95-family"),
        ("N95-1 N95-2", None, "nokia-n95-family"),
    ],
)
def test_evidence_driven_identity(model, rm, expected):
    profile = presentation(model, rm=rm, product_code="unverified-code")
    assert profile["id"] == expected
    assert profile["symbian"] == "9.2"
    assert profile["measured"] is False
    assert profile["variant_known"] == (expected != "nokia-n95-family")
    assert "capabilities" not in profile and "free" not in profile
    assert profile["screen"] == {"width": 240, "height": 320, "orientation": "portrait"}


def test_unknown_model_and_product_code_do_not_identify_a_phone():
    assert presentation("a mounted folder", product_code="0534841") is None
    assert presentation("N95", product_code="0534841")["id"] == "nokia-n95-family"
    assert presentation("not-an-N95fiction") is None
    assert presentation("E52")["symbian"] == presentation("E72")["symbian"] == "9.3"


@pytest.mark.parametrize("key", ["nokia-n95", "nokia-n95-8gb"])
def test_profile_facts_and_assets(key):
    p = load_profile(REPO / f"profiles/{key}.toml")
    assert p.value("platform.symbian_version") == "9.2"
    assert p.value("packaging.platform_dependency") == "0x102032be"
    assert p.fact("firmware.version")["status"] == "unknown"
    assert p.fact("storage.mounted_volume")["status"] == "unknown"
    for field, fact in p.facts():
        assert fact["source"], field
    master = REPO / f"symport/web_assets/phones/{key}.svg"
    xml = ET.parse(master)
    assert xml.getroot().get("viewBox") == "0 0 1000 1868"
    # Each master has its own replaceable embedded PNG layer; masters never fetch external content.
    import base64

    embedded = [n.get("href") for n in xml.iter() if n.get("href")]
    assert len(embedded) == 1 and embedded[0].startswith("data:image/png;base64,")
    assert base64.b64decode(embedded[0].split(",", 1)[1], validate=True).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    screen = registry()[key]["artwork"]["screen"]
    assert 0 < screen["x"] < screen["x"] + screen["width"] < 1000
    assert 0 < screen["y"] < screen["y"] + screen["height"] < 1868
    assert abs(screen["width"] / screen["height"] - 240 / 320) < 0.001
    for ext in ("png", "webp"):
        with Image.open(master.with_suffix("." + ext)) as im:
            assert im.size == (1000, 1868) and im.mode == "RGBA"
            left, top, right, bottom = im.getbbox()
            assert 0 < left < 50 and 0 < top < 30 and right < 1000 and bottom < 1868
            assert 1.80 < (bottom - top) / (right - left) < 1.95
            assert im.getpixel((0, 0))[3] == 0
    assert registry()[key]["hardware"] == "not-tested"


def test_variants_have_separate_geometry_and_material_not_recolor():
    a, b = (registry()[k] for k in ("nokia-n95", "nokia-n95-8gb"))
    assert a["artwork"]["screen"] != b["artwork"]["screen"]
    assert a["storage"] != b["storage"]
    assert (
        hashlib.sha256((REPO / ("symport/web_assets" + a["image"])).read_bytes()).digest()
        != hashlib.sha256((REPO / ("symport/web_assets" + b["image"])).read_bytes()).digest()
    )


@pytest.mark.parametrize("model", ["Nokia N95-1", "Nokia N95 8GB", "N95"])
def test_fp1_cannot_be_promoted_to_fp2(model):
    from symport.deps import analyze_package

    for uid in (0x101F7961, 0x102032BE):
        report = analyze_package(sis(0xE0001111, "FP1", platforms=(uid,)), target={"model": model})
        assert report["platform_verdict"] == "supported-static"
        assert report["levels"]["physical_device"] == "not-tested"
    fp2 = analyze_package(sis(0xE0001111, "FP2"), target={"model": model})
    assert fp2["platform_verdict"] == "unsupported" and fp2["verdict"] == "blocked"
    unknown = analyze_package(
        sis(0xE0001111, "custom", platforms=(0xE0009999,)), target={"model": model}
    )
    assert unknown["platform_verdict"] == "unknown"
    mismatch = analyze_package(
        sis(0xE0001111, "mismatch", platforms=(0x102032BE,)), target={"model": model, "os": "9.3"}
    )
    assert mismatch["verdict"] == "blocked"


def test_incompatible_abi_and_unknown_abi_remain_distinct():
    from symport.deps import analyze_package

    args = {"target": {"model": "N95-1"}}
    for cpu in (0x1000, 0x4000):
        report = analyze_package(
            sis(0xE0001111, "binary", platforms=(0x102032BE,), exe=make_e32(cpu=cpu)), **args
        )
        assert "binary-abi-unsupported" in report["issues"]
        assert report["verdict"] == "blocked"
    report = analyze_package(
        sis(0xE0001111, "unknown", platforms=(0x102032BE,), exe=make_e32(cpu=0x8888)), **args
    )
    assert report["verdict"] == "unresolved"
    assert "binary-abi-unknown" in report["issues"]


def test_catalog_migration_preserves_e52_and_does_not_verify_n95():
    c = Catalog(REPO / "catalog")
    original = json.loads((REPO / "catalog/apps/xplore.json").read_text(encoding="utf-8"))
    entry = c.entries["xplore"]
    for key, value in original.items():
        assert entry[key] == value
    for key in selectable_profiles():
        assert entry["model_compatibility"][key]["status"] == ("unsupported" if key in ('nokia-n8','nokia-e7') else "not-tested")
    entry = copy.deepcopy(entry)
    entry.update(compatibility="verified-on-e52", e52_evidence=["https://example.com/e52-report"])
    assert assessment(entry, "nokia-e52")["status"] == "verified"
    assert assessment(entry, "nokia-n95")["status"] == "not-tested"
    entry["minimum_symbian"] = "9.3"
    assert assessment(entry, "nokia-n95-8gb")["status"] == "unsupported"
    entry["model_evidence"] = {"nokia-n95": {"status": "verified", "sources": []}}
    with pytest.raises(ValueError, match="SHA-256"):
        validate_entry(entry)


@pytest.mark.parametrize("key", ["nokia-n95", "nokia-n95-8gb"])
def test_real_fp1_theme_compiler_and_static_sis(tmp_path, key):
    if not s60_tools():
        pytest.skip("Nokia resource compilers are built by theme CI and Docker")
    studio = ThemeStudio(tmp_path)
    project = studio.execute(
        {"action": "create", "name": "FP1 test", "profile": key, "palette": "light"}
    )
    result = studio.execute(
        {
            "action": "build",
            "id": project["id"],
            "confirm_unsigned": True,
            "revision": project["revision"],
        }
    )
    # This exercises Nokia skc/bmconv and actual SIS parsing, not a mocked theme builder.
    import base64

    package = studio.execute({"action": "export", "id": project["id"]})
    raw = base64.b64decode(package["content"])
    info = inspect(raw)
    assert [d["uid"] for d in info["dependencies"]["items"] if d["kind"] == "target-device"] == [
        0x102032BE
    ]
    assert result["build"]["physical_device"] == "not-tested"


def test_identity_does_not_enroll_storage_or_grant_write_capabilities(tmp_path):
    from symport.phone.service import DeviceManager
    from tests.capfake import row

    observed = row(
        {
            "id": "phone",
            "transport": "gammu",
            "model": "Nokia N95",
            "rm": "RM-320",
            "connected": True,
            "free": None,
            "firmware": None,
        },
        available=["identity.read"],
    )

    class Transport:
        def devices(self):
            return [copy.deepcopy(observed)]

    manager = DeviceManager(tmp_path, Transport())
    result = manager.execute({"action": "discover"})["devices"][0]
    assert result["model_profile"]["id"] == "nokia-n95-8gb"
    assert result["free"] is None and result["firmware"] is None
    assert not {"files", "transfer", "contacts-write", "sms-send"}.intersection(
        result["capabilities"]
    )
    assert not result.get("root") and not result.get("volumes")


def test_nonzero_product_package_version_is_unknown(monkeypatch):
    from symport.deps import analyze_package
    from symport.sis import reader

    raw = sis(0xE0001111, "Version constraint", platforms=(0x102032BE,))
    inspect_original = reader.inspect

    def constrained(blob):
        report = inspect_original(blob)
        report["dependencies"]["items"][0]["from_version"] = [99, 0, 0]
        return report

    monkeypatch.setattr(reader, "inspect", constrained)
    report = analyze_package(raw, target={"model": "N95-1"})
    assert report["platform_verdict"] == "unknown"
    assert report["platforms"][0]["relation"] == "product-version-unknown"
    assert report["verdict"] == "unresolved"


def test_fp2_import_is_blocked_before_repack(tmp_path):
    import base64

    from symport.sis.writer import SisFile

    # Resource bytes are intentionally opaque here: this tests prerequisite preservation before edits.
    raw = sis(
        0xE0001111,
        "FP2 theme",
        platforms=(0x102752AE,),
        extra=[
            SisFile(
                "!:\\private\\10207114\\import\\e0001111\\theme.skn",
                (16).to_bytes(4, "little") + bytes(12),
            )
        ],
    )
    studio = ThemeStudio(tmp_path)
    project = studio.execute(
        {
            "action": "import",
            "name": "FP2 theme",
            "profile": "nokia-n95",
            "filename": "fp2.sis",
            "content": base64.b64encode(raw).decode(),
        }
    )
    assert any("platform/version constraints" in reason for reason in project["blockers"])
    with pytest.raises(ValueError, match="platform/version constraints"):
        studio.execute(
            {
                "action": "build",
                "id": project["id"],
                "revision": project["revision"],
                "confirm_unsigned": True,
            }
        )


def test_downloaded_fp2_sis_is_never_transferred_to_fp1(tmp_path):
    from symport.catalog.service import AppCatalog
    from symport.phone.files import MountedVolumes
    from symport.phone.service import DeviceManager
    from tests.test_catalog import (
        FixtureOpener,
        catalog_fixture,
        jar_bytes,
        public_address,
        wait_job,
    )

    catalog_fixture(tmp_path)
    entry_path = tmp_path / "catalog/apps/example.json"
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    entry["format"] = "sis"
    entry["dependencies"] = []
    entry_path.write_text(json.dumps(entry), encoding="utf-8")
    raw = sis(0xE0001111, "FP2 only")
    sources_path = tmp_path / "catalog/sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    artifact = sources["example"]["artifacts"][0]
    artifact.update(
        name="example.sis",
        url="https://github.com/author/project/releases/download/1/example.sis",
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    sources_path.write_text(json.dumps(sources))
    volume = tmp_path / "card"
    volume.mkdir()
    service = AppCatalog(tmp_path, DeviceManager(tmp_path, MountedVolumes({"card": volume})))
    service.downloader.resolver = public_address

    class Opener:
        def open(self, request, timeout):
            return FixtureOpener(
                raw if request.full_url.endswith(".sis") else jar_bytes("runtime")
            ).open(request, timeout)

    service.downloader.opener = Opener()
    job = service.execute(
        {
            "action": "transfer",
            "ids": ["example"],
            "profile": "nokia-n95",
            "device": "card",
            "confirm": True,
        }
    )
    result = wait_job(service, job["id"])
    assert result["profile"] == "nokia-n95"
    assert result["items"][0]["stage"] == "failed"
    assert "incompatible with the selected target" in result["items"][0]["error"]
    assert list(volume.iterdir()) == []
