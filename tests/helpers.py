"""Test helpers: synthetic E32 images, fake external tools, minimal repo copies."""
from __future__ import annotations

import fcntl
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

from symport.analysis.e32 import header_crc
from symport.sis.fields import uid_checksum

REPO = Path(__file__).resolve().parent.parent


def s60_tools(build: bool = False) -> bool:
    """True when the native Nokia compilers (skc, bmconv) are complete.

    build.sh recreates a shared scratch directory and creates skc before bmconv, so under pytest-xdist a worker that
    merely checks `bin/skc` can see a half-built toolchain. Everything that touches the compilers goes through this
    lock: a worker waits for a build in progress instead of racing it, and `build=True` builds when they are missing."""
    skc, bmconv = REPO / "tools/s60/bin/skc", REPO / "tools/s60/bin/bmconv"
    with (REPO / "tools/s60/.build.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if build and not (skc.exists() and bmconv.exists()):
            subprocess.run(["bash", str(REPO / "tools/s60/build.sh")], check=True, capture_output=True)
        return skc.exists() and bmconv.exists()


def make_e32(*, uid3: int = 0xE0001234, dll: bool = False, cpu: int = 0x2001, kernel_eka2: bool = True,
             caps_low: int = 0, imports: dict[str, list[int]] | None = None,
             compression: int = 0, corrupt_crc: bool = False, abi_eabi: bool = True) -> bytes:
    """Build a small synthetic E32 V-header image (EKA2, ELF import format)."""
    header_size = 156
    code = bytearray(b"\x00" * 64)
    imports = imports or {}
    sect = bytearray(struct.pack("<I", 0))
    names = bytearray()
    slot = 16
    blocks = []
    for dll_name, ordinals in imports.items():
        offs = []
        for o in ordinals:
            struct.pack_into("<I", code, slot, o)
            offs.append(slot)
            slot += 4
        blocks.append((dll_name, offs))
    table = bytearray()
    names_base = 4 + sum(8 + 4 * len(o) for _, o in blocks)
    for dll_name, offs in blocks:
        table += struct.pack("<II", names_base + len(names), len(offs))
        for o in offs:
            table += struct.pack("<I", o)
        names += dll_name.encode() + b"\0"
    while len(names) % 4:
        names += b"\0"
    sect = bytearray(struct.pack("<I", 4 + len(table) + len(names)) + table + names)
    code_size = len(code)
    flags = 0x10000000 | 0x02000000 | (0x08 if abi_eabi else 0) | (0x20 if kernel_eka2 else 0)
    flags |= 1 if dll else 0
    h = bytearray(header_size)
    struct.pack_into("<III", h, 0, 0x10000079 if dll else 0x1000007A, 0x1000008D if dll else 0, uid3)
    struct.pack_into("<I", h, 12, uid_checksum(bytes(h[:12])))
    h[16:20] = b"EPOC"
    struct.pack_into("<I", h, 24, 0x00010000)
    struct.pack_into("<I", h, 28, compression)
    struct.pack_into("<I", h, 44, flags)
    struct.pack_into("<I", h, 48, code_size)
    struct.pack_into("<I", h, 56, 0x1000)
    struct.pack_into("<I", h, 60, 0x100000)
    struct.pack_into("<I", h, 64, 0x10000)
    struct.pack_into("<I", h, 76, 0x00400000)
    struct.pack_into("<I", h, 84, len(blocks))
    struct.pack_into("<I", h, 96, code_size)
    struct.pack_into("<I", h, 100, header_size)
    struct.pack_into("<I", h, 108, header_size + code_size if blocks else 0)
    struct.pack_into("<H", h, 122, cpu)
    struct.pack_into("<I", h, 124, code_size + len(sect))
    struct.pack_into("<II", h, 128, uid3, 0)
    struct.pack_into("<II", h, 136, caps_low, 0)
    crc = header_crc(bytes(h), header_size) ^ (0xDEAD if corrupt_crc else 0)
    struct.pack_into("<I", h, 20, crc)
    return bytes(h) + bytes(code) + (bytes(sect) if blocks else b"")


def write_tool(path: Path, body: str) -> Path:
    """Create an executable Python script usable as a fake external tool."""
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def repo_copy(dest: Path, with_src: bool = True) -> Path:
    """A minimal E52Modern checkout copy (theme data + tools/s60 scripts) for corruption tests."""
    (dest / "tools" / "s60").mkdir(parents=True)
    shutil.copy2(REPO / "tools" / "build_theme.py", dest / "tools" / "build_theme.py")
    for f in ("s60inspect.py", "mifbuild.py"):
        shutil.copy2(REPO / "tools" / "s60" / f, dest / "tools" / "s60" / f)
    (dest / "theme").mkdir()
    for f in ("manifest.json", "colours.json"):
        shutil.copy2(REPO / "theme" / f, dest / "theme" / f)
    shutil.copytree(REPO / "theme" / "dist", dest / "theme" / "dist")
    if with_src:
        shutil.copytree(REPO / "theme" / "src", dest / "theme" / "src")
    shutil.copytree(REPO / "profiles", dest / "profiles")
    return dest
