"""Windows Portable Device (MTP) adapter: read-only files, honest capabilities, untrusted names (#76)."""
from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest

from symport.phone import wpd
from symport.phone.wpd import WpdAdapter


class FakeWindows:
    """Stands in for PowerShell: a phone with two storages and a few files."""

    def __init__(self, devices=None):
        self.calls = []
        self.devices = devices if devices is not None else [{"index": 0, "storages": ["Phone memory", "Memory card"]}]
        self.tree = {
            "Memory card": {"Images": {"IMG_1.jpg": b"\xff\xd8jpeg\xff\xd9"}, "notes.txt": b"hello"},
            "Phone memory": {},
        }
        self.leave = []

    def __call__(self, script, request, timeout=150):
        self.calls.append((script, request))
        if script == wpd.PROBE:
            return self.devices
        node = self.tree
        parts = request["parts"]
        if script == wpd.LIST:
            for part in parts:
                node = node[part]
            return [{"name": k, "directory": isinstance(v, dict), "size": None if isinstance(v, dict) else len(v)}
                    for k, v in node.items()]
        if script == wpd.DOWNLOAD:
            for part in parts[:-1]:
                node = node[part]
            payload = node[parts[-1]]
            folder = Path(tempfile.mkdtemp(prefix="symport-wpd-"))
            target = folder / parts[-1]
            target.write_bytes(payload)
            self.leave.append(folder)
            return {"path": str(target), "dir": str(folder), "size": len(payload)}
        raise AssertionError("unknown script")


def test_row_is_a_files_only_portable_device_without_phone_identity():
    adapter = WpdAdapter(FakeWindows())
    row = adapter.device()
    assert row["transport"] == "wpd" and row["connected"] is True and row["id"] == "wpd-nokia"
    assert row["model"] is None and row["rm"] is None and row["firmware"] is None, "WPD never invents a phone identity"
    matrix = row["capability_matrix"]
    assert matrix["files.list"]["status"] == "available"
    assert matrix["files.download"]["status"] == "not-tested", "a real download proves it"
    for op in ("contacts.read", "calendar.read", "sms.read", "sms.send", "identity.read", "contacts.write"):
        assert matrix[op]["status"] == "unsupported", op
    for op in ("files.upload", "files.stage", "files.delete", "packages.transfer", "install.run"):
        assert matrix[op]["status"] == "unsupported" and matrix[op]["attempt"] == "blocked", op
    assert "files" in row["capabilities"] and "personal-data" not in row["capabilities"]
    assert not any("contacts" in c or "sms" in c for c in row["capabilities"])


def test_no_device_or_a_failing_probe_offers_no_row():
    assert WpdAdapter(FakeWindows(devices=[])).device() is None

    def broken(script, request, timeout=150):
        raise ValueError("The portable device could not be read")

    assert WpdAdapter(broken).device() is None


def test_a_locked_phone_is_connected_false_not_available():
    row = WpdAdapter(FakeWindows(devices=[{"index": 0, "storages": []}])).device()
    assert row["connected"] is False
    assert row["capability_matrix"]["files.list"]["status"] == "error"


def test_list_and_download_roundtrip_and_cleanup():
    windows = FakeWindows()
    adapter = WpdAdapter(windows)
    root = adapter.execute({"action": "list", "path": ""})["entries"]
    assert {e["name"] for e in root} == {"Phone memory", "Memory card"}
    inner = adapter.execute({"action": "list", "path": "Memory card"})["entries"]
    assert {(e["name"], e["directory"]) for e in inner} == {("Images", True), ("notes.txt", False)}
    got = adapter.execute({"action": "download", "path": "Memory card/notes.txt"})
    assert got["name"] == "notes.txt" and base64.b64decode(got["content"]) == b"hello"
    assert not windows.leave[0].exists(), "the temporary copy is removed"
    nested = adapter.execute({"action": "download", "path": "Memory card/Images/IMG_1.jpg"})
    assert base64.b64decode(nested["content"]).startswith(b"\xff\xd8")


@pytest.mark.parametrize("action", ["upload", "transfer", "software-transfer", "media-upload", "mkdir", "delete",
                                    "restore", "contacts-stage", "sms-send", "personal-read"])
def test_writes_and_pim_are_refused(action):
    windows = FakeWindows()
    with pytest.raises(ValueError):
        WpdAdapter(windows).execute({"action": action, "path": "Memory card/x", "confirm": True, "content": "AA=="})
    assert windows.calls == [], "nothing reaches Windows for a refused action"


@pytest.mark.parametrize("path", ["/etc", "..", "a/../b", "C:/x", "a\\b", "Memory card/./x", ""])
def test_paths_are_relative_and_confined(path):
    adapter = WpdAdapter(FakeWindows())
    if path == "":
        assert adapter.execute({"action": "list", "path": path})["entries"]
        return
    with pytest.raises(ValueError):
        adapter.execute({"action": "list", "path": path})


def test_names_from_the_phone_are_sanitised():
    windows = FakeWindows()
    windows.tree = {"Memory card": {"ev\x00il\x1b[31m/..\\x.txt": b"x", "..": b"x", "": b"x", "normal.txt": b"1"}}
    names = [e["name"] for e in WpdAdapter(windows).execute({"action": "list", "path": "Memory card"})["entries"]]
    assert "normal.txt" in names and ".." not in names and "" not in names
    assert all("/" not in n and "\\" not in n and "\x00" not in n and "\x1b" not in n for n in names)


def test_a_download_outside_the_temp_folder_is_rejected(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("private")

    def hostile(script, request, timeout=150):
        if script == wpd.PROBE:
            return [{"index": 0, "storages": ["Memory card"]}]
        return {"path": str(outside), "dir": str(tmp_path), "size": 7}

    with pytest.raises(ValueError, match="unexpected file location"):
        WpdAdapter(hostile).execute({"action": "download", "path": "Memory card/secret.txt"})
    assert outside.exists(), "a hostile response cannot make the adapter delete anything"


def test_incomplete_download_is_not_returned():
    windows = FakeWindows()
    real = windows.__call__

    def short(script, request, timeout=150):
        info = real(script, request, timeout)
        if script == wpd.DOWNLOAD:
            info["size"] += 1
        return info

    with pytest.raises(ValueError, match="incomplete"):
        WpdAdapter(short).execute({"action": "download", "path": "Memory card/notes.txt"})


def test_probe_is_cached_briefly():
    windows = FakeWindows()
    now = [0.0]
    adapter = WpdAdapter(windows, clock=lambda: now[0])
    adapter.device()
    adapter.device()
    assert sum(1 for s, _ in windows.calls if s == wpd.PROBE) == 1
    now[0] = 30
    adapter.device()
    assert sum(1 for s, _ in windows.calls if s == wpd.PROBE) == 2


def test_scripts_only_read_and_never_touch_driver_or_registry():
    for script in (wpd.PROBE, wpd.LIST, wpd.DOWNLOAD):
        lowered = script.lower()
        for forbidden in ("pnputil", "devcon", "set-itemproperty", "reg add", "remove-item", "invoke-expression",
                          "downloadfile", "invoke-webrequest", "start-process"):
            assert forbidden not in lowered, forbidden
    assert "vid_0421" in wpd.PRELUDE.lower()
    assert json.dumps(wpd.LIST)  # plain text, no format placeholders to inject into
