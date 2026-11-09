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


# ---- recognising a Nokia that Windows 11 lists under USB\MS_COMP_MTP (real E72 path has no VID) -----------------------

E72_PATH = r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\\?\usb#ms_comp_mtp#9&1fe3119&0&00#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
OLD_PATH = r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\\?\usb#vid_0421&pid_0223#5&2a3b&0&1#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
NOKIA_PARENT = r"USB\VID_0421&PID_0223\5&2A3B&0&1"


def test_ms_comp_mtp_path_becomes_the_pnp_instance_id():
    assert wpd.ms_comp_mtp_instance(E72_PATH) == r"USB\MS_COMP_MTP\9&1FE3119&0&00", "'#' becomes '\\', case is normalised"
    assert wpd.ms_comp_mtp_instance(E72_PATH.upper()) == r"USB\MS_COMP_MTP\9&1FE3119&0&00"
    assert wpd.ms_comp_mtp_instance(OLD_PATH) is None, "the VID route needs no parent lookup"
    for other in ("", "usb#ms_comp_mtp#", r"\\?\usb#vid_04e8&pid_6860#x#{1}", "ms_comp_mtp", "usb#ms_comp_mtp##{1}"):
        assert wpd.ms_comp_mtp_instance(other) is None, other


@pytest.mark.parametrize(("parent", "nokia"), [
    (NOKIA_PARENT, True),
    (r"usb\vid_0421&pid_0223\5&2a3b&0&1", True),                 # case-insensitive
    (r"USB\VID_0421&PID_0223&MI_00\6&3F&0&0000", True),         # a composite interface of a Nokia
    (r"USB\VID_04E8&PID_6860\RF1234567", False),                # another vendor's phone
    (r"USB\VID_0421X&PID_0223\x", False),
    (r"USB\VID_0421&PID_02\x", False),                          # malformed product id
    (r"USB\ROOT_HUB30\4&2", False),
    (r"ACPI\PNP0A08\0", False),
    ("", False), (None, False),
])
def test_only_a_nokia_usb_parent_qualifies(parent, nokia):
    assert wpd.is_nokia_parent(parent) is nokia


def select(items, parents):
    """Reference model of the script's Test-Nokia: the same shared patterns, with PnP parents given as a dict."""
    import re

    chosen = []
    for path in items:
        if re.search(wpd.PATH_VID, path):
            chosen.append(path)
            continue
        instance = wpd.ms_comp_mtp_instance(path)
        if instance and wpd.is_nokia_parent(parents.get(instance)):    # unknown parent → not offered
            chosen.append(path)
    return sorted(chosen)


def test_a_foreign_mtp_device_is_not_offered_but_the_nokia_one_is():
    foreign = r"::{20D0}\\\?\usb#ms_comp_mtp#7&aaaa&0&00#{6ac2}"
    nameless = r"::{20D0}\\\?\usb#ms_comp_mtp#8&bbbb&0&00#{6ac2}"
    parents = {r"USB\MS_COMP_MTP\9&1FE3119&0&00": NOKIA_PARENT, r"USB\MS_COMP_MTP\7&AAAA&0&00": r"USB\VID_04E8&PID_6860\RF1"}
    assert select([foreign, E72_PATH, nameless, OLD_PATH], parents) == sorted([E72_PATH, OLD_PATH]), \
        "foreign vendor and 'parent unknown' are not offered; the VID path still works"
    assert select([E72_PATH], {}) == [], "no parent answer, no device (as before)"


def test_the_script_uses_the_shared_patterns_and_the_documented_parent_property():
    script = wpd.PRELUDE
    assert wpd.PATH_VID in script and wpd.PATH_MS_COMP_MTP in script and wpd.NOKIA_PARENT in script
    assert "@@" not in script, "every placeholder was filled"
    assert "DEVPKEY_Device_Parent" in script and "Get-PnpDeviceProperty" in script, "locale-independent parent lookup"
    assert "$_.IsFolder" in script and "Sort-Object { [string]$_.Path }" in script, "stable order: index means the same device"
    assert "S60 Handset" not in script and "FriendlyName" not in script, "never by name"
    # the foreign-device rule: ms_comp_mtp alone does not qualify — the parent check decides
    assert script.index("-match '" + wpd.NOKIA_PARENT) > script.index("-match '" + wpd.PATH_MS_COMP_MTP)
    for combined in (wpd.PROBE, wpd.LIST, wpd.DOWNLOAD):
        assert combined.startswith(wpd.PRELUDE), "PROBE, LIST and DOWNLOAD select devices the same way"


def test_no_identifier_leaves_the_script_or_reaches_the_row():
    """The probe emits only index and storage names; instance ids, serials and paths stay inside PowerShell."""
    for script in (wpd.PROBE, wpd.LIST):
        output = script.split("ConvertTo-Json")[-1]
        assert "Path" not in output and "InstanceId" not in output and "serial" not in output.lower(), output
    probe_body = wpd.PROBE.split("foreach ($device in (Get-Devices))")[1]
    assert "storages" in probe_body and "index" in probe_body and "InstanceId" not in probe_body

    leaking = FakeWindows(devices=[{"index": 0, "storages": ["Memory card"], "path": E72_PATH, "instance": NOKIA_PARENT,
                                    "serial": "SECRET123"}])
    row = WpdAdapter(leaking).device()
    text = json.dumps(row)
    for forbidden in ("SECRET123", "ms_comp_mtp", "MS_COMP_MTP", "VID_0421", "20D04FE0", E72_PATH):
        assert forbidden not in text, forbidden
    assert row["model"] is None and row["rm"] is None and row["firmware"] is None
    assert row["capability_matrix"]["contacts.read"]["status"] == "unsupported"
    assert row["capability_matrix"]["files.upload"]["status"] == "unsupported"


def test_writes_stay_refused_for_the_new_device_kind():
    windows = FakeWindows()
    for action in ("upload", "mkdir", "delete", "transfer", "contacts-stage", "sms-send"):
        with pytest.raises(ValueError):
            WpdAdapter(windows).execute({"action": action, "path": "Memory card/x", "confirm": True, "content": "AA=="})
    assert windows.calls == []
