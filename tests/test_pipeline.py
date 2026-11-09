"""Regression tests for the existing build pipeline (needs g++; builds skc/bmconv)."""
import json
import shutil
import subprocess
import sys

import pytest

from symport.theme import reproduce
from tests.helpers import REPO, s60_tools

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ required")


@pytest.fixture(scope="module")
def tools():
    skc, bm = REPO / "tools" / "s60" / "bin" / "skc", REPO / "tools" / "s60" / "bin" / "bmconv"
    s60_tools(build=True)
    return skc, bm


def test_legacy_entry_points_still_work(tools, tmp_path):
    """tools/build_theme.py runs directly, unchanged, in an isolated workspace."""
    pytest.importorskip("PIL")
    ws = tmp_path / "ws"
    shutil.copytree(REPO / "tools", ws / "tools", ignore=shutil.ignore_patterns("__pycache__", ".build"))
    r = subprocess.run([sys.executable, "tools/build_theme.py", "light"], cwd=ws, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    d = ws / "theme" / "dist" / "light"
    assert {p.suffix for p in d.iterdir()} == {".skn", ".mbm", ".mif", ".pkg"}
    golden = json.loads((REPO / "tests" / "golden" / "theme-structure.json").read_text())["light"]
    r = subprocess.run([sys.executable, str(REPO / "tools" / "s60" / "s60inspect.py"), str(d / "e52clearlight")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and f"mbm: {golden['mbm_bitmaps']} " in r.stdout
    assert f"mif: {golden['mif_icons']} " in r.stdout


def test_build_is_reproducible_after_timestamp_normalisation(tools, cfg):
    pytest.importorskip("PIL")
    rep = reproduce.check_reproducible(cfg, "dark")
    assert rep.status == "ok", rep.to_text()


def test_timestamp_normaliser_only_touches_info_chunk():
    import struct
    chunk = struct.pack("<IHH", 24, 11, 2) + b"\x01" * 8 + b"\x07" * 8
    a = b"HEAD" + chunk + b"TAIL"
    b = a.replace(b"\x01" * 8, b"\x02" * 8)
    assert reproduce.normalise_skn(a) == reproduce.normalise_skn(b)
    c = a.replace(b"\x07" * 8, b"\x08" * 8)        # a difference outside the timestamp must survive
    assert reproduce.normalise_skn(a) != reproduce.normalise_skn(c)
