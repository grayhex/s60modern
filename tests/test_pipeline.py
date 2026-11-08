"""Regression tests for the existing build pipeline (needs g++; builds skc/bmconv)."""
import fcntl
import json
import shutil
import subprocess
import sys

import pytest

from symport.theme import reproduce
from tests.helpers import REPO

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ required")


@pytest.fixture(scope="module")
def tools():
    skc, bm = REPO / "tools" / "s60" / "bin" / "skc", REPO / "tools" / "s60" / "bin" / "bmconv"
    # build.sh recreates a shared scratch directory, so two pytest-xdist workers building at once break each other.
    # Serialise the build and re-check under the lock: the worker that waited finds the tools already built.
    lock = REPO / "tools" / "s60" / ".build.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not (skc.exists() and bm.exists()):
            subprocess.run(["bash", str(REPO / "tools" / "s60" / "build.sh")], check=True, capture_output=True)
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
