"""Nokia PCCS over the USB OBEX interface: read-only contacts and calendar of an S60 3rd Edition phone (E72).

The Nokia USB driver (nmwcd) exposes three interfaces to the PC; the one with GUID {4F919100-...} speaks OBEX.
PC Suite talks to the phone's sync engine through it: OBEX GET/PUT with the *Folder Browsing* target, a WBXML
content type and a "command" carried in the Name header, written as ASCII with every pair of bytes swapped.
No COM port, no Gammu, no Phonet and no LCIF are involved.

Only reading is implemented. ``CommitChanges``, ``CreateItems``, ``DeleteItems`` and every other write are
deliberately absent from this module; ``test_nokia_pccs`` checks that no builder can produce them.

Verified by hand on one Nokia E72 (S60 3.2): Connect, OpenStore, ResetChangeInfo, GetStoreFormat,
SetRemoteStoreFormat, ListChanges, ReadItems with one and two records, CloseStore. CI checks only the byte
layout against recorded values and the parsers on synthetic data; it proves nothing about other phones, paged
reads of large stores or a phone with a locked keypad.

This module is pure logic (stdlib only, no I/O of its own): the channel to the phone is an object with
``exchange(packet, timeout)``, ``cancel()`` and ``close()`` (see ``pccs_windows`` for the Windows one), so tests use stubs.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Protocol

INTERFACE_GUID = "{4F919100-4ADF-11D5-882D-00B0D02FE381}"  # NOT Phonet {4F919102-...} and NOT LCIF {4F91910C-...}
FOLDER_BROWSING_TARGET = bytes.fromhex("F9EC7BC4953C11D2984E525400DC9E09")
CONML_TYPE = b"application/vnd.nokia.conml+wbxml\x00"
CAPABILITY_TYPE = b"x-obex/capability\x00"
WHO = b"PC Suite"

# OBEX opcodes and header ids
CONNECT, DISCONNECT, PUT, GET = 0x80, 0x81, 0x82, 0x83
OK, CONTINUE = 0xA0, 0x90
H_NAME, H_TYPE, H_TARGET, H_WHO, H_BODY, H_END_OF_BODY, H_LENGTH, H_CONNECTION_ID = 0x01, 0x42, 0x46, 0x4A, 0x48, 0x49, 0xC3, 0xCB

MAX_BODY = 8 * 1024 * 1024  # one response; a phone book of thousands of contacts is far below this
MAX_ROUNDS = 4096
MAX_IDS = 20000
READ_BATCH = 20  # ids per ReadItems; only one and two ids are verified on a phone
MAX_TEXT = 300

# Store parameters: UID (little-endian), unknown 4 bytes (captured from PC Suite; zeros also work), store name.
STORES = {
    "contacts": (bytes([0xDD, 0x6D, 0x1F, 0x10]), bytes.fromhex("9c9a235d"), "C:Contacts.cdb"),
    "calendar": (bytes([0xDE, 0x6D, 0x1F, 0x10]), bytes.fromhex("542a3175"), "Calendar"),
}
# Fixed SetRemoteStoreFormat body for Contacts (63 bytes, as sent by PC Suite). Do not change.
CONTACTS_FORMAT = bytes.fromhex(
    "010800000020436f6e746163747300000000000000003f000100010c00000032746578742f782d7663617264030000000e322e310000000000000000000000"
)

# Fixed reasons; raw text from the OS or the phone is never forwarded.
REASONS = {
    "busy": "The USB OBEX channel is held by another program (close Nokia PC Suite)",
    "no-interface": "Windows shows no Nokia USB OBEX interface",
    "silent": "The Nokia USB interfaces did not answer an OBEX connection",
    "not-pccs": "No Nokia USB interface accepted the PC Suite sync target",
    "timeout": "The phone did not answer in time",
    "refused": "The phone refused the request",
    "store": "The phone did not open the requested store",
    "format": "The phone answer was not recognised",
    "closed": "The connection was closed",
}


class PccsError(Exception):
    """Carries only a fixed reason code from ``REASONS``; never raw device or OS text."""

    def __init__(self, code: str):
        super().__init__(REASONS.get(code, code))
        self.code = code


class PccsBusy(PccsError):
    def __init__(self):
        super().__init__("busy")


class Channel(Protocol):
    def exchange(self, packet: bytes, timeout: float) -> bytes: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


# ---- command names ---------------------------------------------------------------------------------------------------
def command_name(logical: bytes) -> bytes:
    """Name header value: the logical ASCII command padded to an even length, two zero bytes added, each pair swapped."""
    data = bytes(logical)
    if len(data) % 2:
        data += b"\x00"
    data += b"\x00\x00"
    out = bytearray(len(data))
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def open_store_name(kind: str) -> bytes:
    uid, unknown, name = STORES[kind]
    text = name.encode("ascii")
    return command_name(b"SYNC: OpenStore:" + b"\x20" + uid + unknown + struct.pack("<H", len(text)) + text)


def read_items_name(ids: list[int]) -> bytes:
    return command_name(b"SYNC: ReadItems:" + b"\x20" + struct.pack("<I", len(ids)) + b"".join(struct.pack("<I", i) for i in ids))


NAME_LIST_STORES = command_name(b"SYNC: ListStores")
NAME_RESET_CHANGE_INFO = command_name(b"SYNC: ResetChangeInfo")
NAME_GET_STORE_FORMAT = command_name(b"SYNC: GetStoreFormat")
NAME_SET_REMOTE_STORE_FORMAT = command_name(b"SYNC: SetRemoteStoreFormat")
NAME_LIST_CHANGES = command_name(b"SYNC: ListChanges")
NAME_CLOSE_STORE = command_name(b"SYNC: CloseStore")


# ---- OBEX packets ----------------------------------------------------------------------------------------------------
def _header_bytes(hid: int, value: bytes) -> bytes:
    return bytes([hid]) + struct.pack(">H", len(value) + 3) + value


def _packet(opcode: int, fixed: bytes, headers: bytes) -> bytes:
    return bytes([opcode]) + struct.pack(">H", 3 + len(fixed) + len(headers)) + fixed + headers


def connect_packet() -> bytes:
    """Connect, OBEX 1.0, max packet 0xFFFF, Target = Folder Browsing."""
    return _packet(CONNECT, bytes([0x10, 0x00]) + struct.pack(">H", 0xFFFF), _header_bytes(H_TARGET, FOLDER_BROWSING_TARGET))


def disconnect_packet(connection_id: bytes) -> bytes:
    return _packet(DISCONNECT, b"", bytes([H_CONNECTION_ID]) + connection_id)


def get_packet(connection_id: bytes, name: bytes | None = None, type_: bytes = CONML_TYPE, who: bool = True) -> bytes:
    headers = bytes([H_CONNECTION_ID]) + connection_id
    if name is not None:
        headers += _header_bytes(H_NAME, name)
    headers += _header_bytes(H_TYPE, type_)
    if who:
        headers += _header_bytes(H_WHO, WHO)
    return _packet(GET, b"", headers)


def get_continue_packet() -> bytes:
    return _packet(GET, b"", b"")


def put_packet(connection_id: bytes, name: bytes, body: bytes) -> bytes:
    headers = bytes([H_CONNECTION_ID]) + connection_id + _header_bytes(H_NAME, name)
    headers += bytes([H_LENGTH]) + struct.pack(">I", len(body)) + _header_bytes(H_TYPE, CONML_TYPE) + _header_bytes(H_WHO, WHO)
    return _packet(PUT, b"", headers + _header_bytes(H_END_OF_BODY, body))


def parse_headers(data: bytes) -> list[tuple[int, bytes]]:
    headers, pos = [], 0
    while pos < len(data):
        hid = data[pos]
        kind = hid >> 6
        if kind in (0, 1):
            if pos + 3 > len(data):
                raise PccsError("format")
            size = struct.unpack_from(">H", data, pos + 1)[0]
            if size < 3 or pos + size > len(data):
                raise PccsError("format")
            headers.append((hid, data[pos + 3 : pos + size]))
            pos += size
        else:
            width = 1 if kind == 2 else 4
            if pos + 1 + width > len(data):
                raise PccsError("format")
            headers.append((hid, data[pos + 1 : pos + 1 + width]))
            pos += 1 + width
    return headers


def parse_response(packet: bytes, fixed: int = 0) -> tuple[int, list[tuple[int, bytes]]]:
    """(response code, headers). ``fixed`` bytes of version/flags/maxlen follow the length in a Connect answer."""
    if len(packet) < 3 or struct.unpack_from(">H", packet, 1)[0] != len(packet):
        raise PccsError("format")
    return packet[0], parse_headers(packet[3 + fixed :])


# ---- session ---------------------------------------------------------------------------------------------------------
class PccsSession:
    """One OBEX connection on one channel. Every method is a single request/answer; nothing here writes to the phone's stores."""

    def __init__(self, channel: Channel, timeout: float = 20.0, connect_timeout: float = 5.0):
        self.channel = channel
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.connection_id: bytes | None = None

    def connect(self) -> None:
        answer = self.channel.exchange(connect_packet(), self.connect_timeout)
        code, headers = parse_response(answer, fixed=4)
        if code != OK:
            raise PccsError("not-pccs")
        who = next((v for h, v in headers if h == H_WHO), b"")
        ident = next((v for h, v in headers if h == H_CONNECTION_ID), b"")
        if who != FOLDER_BROWSING_TARGET or len(ident) != 4:
            raise PccsError("not-pccs")  # the SyncML or an empty interface
        self.connection_id = ident

    def disconnect(self) -> None:
        if self.connection_id is None:
            return
        ident, self.connection_id = self.connection_id, None
        try:
            self.channel.exchange(disconnect_packet(ident), 5.0)
        except PccsError:
            pass

    def _ident(self) -> bytes:
        if self.connection_id is None:
            raise PccsError("closed")
        return self.connection_id

    def get(self, name: bytes | None, type_: bytes = CONML_TYPE, who: bool = True) -> bytes:
        """GET with Continue handling; returns the concatenated Body/EndOfBody."""
        packet = get_packet(self._ident(), name, type_, who)
        body = b""
        for _ in range(MAX_ROUNDS):
            code, headers = parse_response(self.channel.exchange(packet, self.timeout))
            body += b"".join(v for h, v in headers if h in (H_BODY, H_END_OF_BODY))
            if len(body) > MAX_BODY:
                raise PccsError("format")
            if code == OK:
                return body
            if code != CONTINUE:
                raise PccsError("refused")
            packet = get_continue_packet()
        raise PccsError("format")

    def put(self, name: bytes, body: bytes) -> bytes:
        code, headers = parse_response(self.channel.exchange(put_packet(self._ident(), name, body), self.timeout))
        if code not in (OK, CONTINUE):
            raise PccsError("refused")
        return b"".join(v for h, v in headers if h in (H_BODY, H_END_OF_BODY))


_TAG = {"manufacturer": r"Manufacturer", "model": r"Model", "firmware": r"SW(?:-Version)?"}


def parse_capability(raw: bytes) -> dict:
    """Manufacturer, Model (and RM-nnn) and SW version from the OBEX capability object.

    Only these three tags are read. ``<SN>`` (the IMEI) is never looked at, copied or kept: the raw answer is dropped."""
    text = raw.decode("utf-8", errors="replace")
    out: dict = {}
    for key, tag in _TAG.items():
        found = re.search(rf"<{tag}>([^<]{{1,64}})</", text)
        if found:
            value = re.sub(r"[\x00-\x1f\x7f]", "", found.group(1)).strip()
            if value:
                out[key] = value
    rm = re.search(r"RM-\d{1,4}", out.get("model", "") + " " + out.get("firmware", ""))
    if rm:
        out["rm"] = rm.group()
    return out


# ---- logical answers -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Item:
    id: int
    mime: str
    version: str
    payload: bytes


def parse_open_store(data: bytes) -> None:
    if len(data) < 4 or struct.unpack_from("<I", data)[0] != 1:
        raise PccsError("store")


def parse_list_changes(data: bytes) -> list[int]:
    if len(data) < 4:
        raise PccsError("format")
    count = struct.unpack_from("<I", data)[0]
    if count > MAX_IDS or len(data) < 4 + 4 * count:
        raise PccsError("format")
    return list(struct.unpack_from(f"<{count}I", data, 4))


def _record(data: bytes, pos: int) -> tuple[Item | None, int]:
    """One ReadItems record starting at ``pos``; returns (item or None when the payload is not a card, next position)."""
    if pos + 8 > len(data):
        raise PccsError("format")
    block = struct.unpack_from("<I", data, pos)[0]
    ident = struct.unpack_from("<I", data, pos + 4)[0]
    body = pos + 8
    # The service fields before the payload have no fixed length: find the 0x80 that is followed by a text MIME type.
    start = None
    for i in range(body, min(len(data) - 3, body + 64)):
        if data[i] == 0x80:
            size = struct.unpack_from("<H", data, i + 1)[0]
            if 5 <= size <= 40 and data[i + 3 : i + 8] == b"text/" and i + 3 + size <= len(data):
                start = i
                break
    if start is None:
        raise PccsError("format")
    size = struct.unpack_from("<H", data, start + 1)[0]
    mime_end = start + 3 + size
    mime = data[start + 3 : mime_end].decode("ascii", errors="replace")
    if mime_end + 2 > len(data):
        raise PccsError("format")
    vsize = struct.unpack_from("<H", data, mime_end)[0]
    version = data[mime_end + 2 : mime_end + 2 + vsize].decode("ascii", errors="replace")
    pos_len = mime_end + 2 + vsize
    if vsize > 16 or pos_len + 4 > len(data):
        raise PccsError("format")
    length = struct.unpack_from("<I", data, pos_len)[0]
    end = pos_len + 4 + length
    if length > MAX_BODY or end > len(data):
        raise PccsError("format")
    payload = data[pos_len + 4 : end]
    # Prefer the record's own block length when it is consistent with the payload end.
    nxt = pos + 4 + block
    nxt = nxt if end <= nxt <= len(data) else end
    if not re.match(rb"\s*BEGIN:V(?:CARD|CALENDAR)", payload, re.IGNORECASE) or not re.search(rb"END:V(?:CARD|CALENDAR)\s*$", payload, re.IGNORECASE):
        return None, nxt
    return Item(ident, mime, version, payload), nxt


def parse_read_items(data: bytes) -> tuple[list[Item], int]:
    """(items, number of records that were not a vCard/vCalendar). A damaged answer raises ``PccsError('format')``."""
    if len(data) < 4:
        raise PccsError("format")
    count = struct.unpack_from("<I", data)[0]
    if count > MAX_IDS:
        raise PccsError("format")
    items, skipped, pos = [], 0, 4
    for _ in range(count):
        item, pos = _record(data, pos)
        if item is None:
            skipped += 1
        else:
            items.append(item)
    return items, skipped


def read_store(session: PccsSession, kind: str) -> tuple[list[Item], int]:
    """OpenStore → ResetChangeInfo → GetStoreFormat → SetRemoteStoreFormat → ListChanges → ReadItems → CloseStore.

    CloseStore is sent whenever OpenStore got an answer, also after a failure in between. Read-only: the only PUT is
    SetRemoteStoreFormat, which tells the phone which format the PC wants to receive; it changes no stored record."""
    opened = False
    try:
        answer = session.get(open_store_name(kind))
        opened = True
        parse_open_store(answer)
        session.get(NAME_RESET_CHANGE_INFO)  # mandatory right after OpenStore
        fmt = session.get(NAME_GET_STORE_FORMAT)
        session.put(NAME_SET_REMOTE_STORE_FORMAT, CONTACTS_FORMAT if kind == "contacts" else fmt[4:])
        ids = parse_list_changes(session.get(NAME_LIST_CHANGES))
        items: list[Item] = []
        skipped = 0
        for i in range(0, len(ids), READ_BATCH):
            part, bad = parse_read_items(session.get(read_items_name(ids[i : i + READ_BATCH])))
            items += part
            skipped += bad
        return items, skipped
    finally:
        if opened:
            try:
                session.get(NAME_CLOSE_STORE)
            except PccsError:
                pass


def open_only(session: PccsSession, kind: str) -> None:
    """Capability probe: a real OpenStore, ResetChangeInfo and CloseStore; no record is read."""
    opened = False
    try:
        answer = session.get(open_store_name(kind))
        opened = True
        parse_open_store(answer)
        session.get(NAME_RESET_CHANGE_INFO)
    finally:
        if opened:
            try:
                session.get(NAME_CLOSE_STORE)
            except PccsError:
                pass


# ---- vCard / vCalendar (tolerant, stdlib only) -----------------------------------------------------------------------
def _text(payload: bytes) -> str:
    payload = payload.removeprefix(b"\xef\xbb\xbf")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def _qp(line: str) -> bool:
    head = line.split(":", 1)[0].upper()
    return "QUOTED-PRINTABLE" in head


def unfold(text: str) -> list[str]:
    """Join folded lines (leading blank) and quoted-printable soft breaks (a trailing '=')."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if lines and _qp(lines[-1]) and lines[-1].endswith("="):
            lines[-1] = lines[-1][:-1] + raw
        elif lines and raw[:1] in (" ", "\t"):
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _clean(value: str, limit: int = MAX_TEXT) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value).strip()[:limit]


def _split(value: str, sep: str) -> list[str]:
    parts, current, i = [], "", 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            current += value[i : i + 2]
            i += 2
        elif value[i] == sep:
            parts.append(current)
            current, i = "", i + 1
        else:
            current += value[i]
            i += 1
    return parts + [current]


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def properties(payload: bytes):
    """Yield (name, params, raw value) for each line; groups ("item1.TEL") are dropped, quoted-printable is decoded."""
    import quopri

    for line in unfold(_text(payload)):
        head, sep, value = line.partition(":")
        if not sep or not head.strip():
            continue
        pieces = head.split(";")
        name = pieces[0].strip().rsplit(".", 1)[-1].upper()
        params = {p.strip().upper() for p in pieces[1:]}
        charset = next((p.split("=", 1)[1] for p in params if p.startswith("CHARSET=")), "UTF-8")
        if "QUOTED-PRINTABLE" in params or "ENCODING=QUOTED-PRINTABLE" in params:
            raw = quopri.decodestring(value.encode("ascii", errors="replace"))
            try:
                value = raw.decode(charset)
            except (LookupError, UnicodeDecodeError):
                value = raw.decode("utf-8", errors="replace")
        yield name, params, value


def parse_vcard(payload: bytes) -> dict | None:
    """Gammu-compatible contact row ({name, given, family, phones, emails}) or None when it holds nothing usable."""
    full = given = family = ""
    phones: list[str] = []
    emails: list[str] = []
    for name, _, value in properties(payload):
        if name == "FN":
            full = _clean(_unescape(value))
        elif name == "N":
            parts = [_unescape(p) for p in _split(value, ";")] + ["", ""]
            family, given = _clean(parts[0]), _clean(parts[1])
        elif name == "TEL" and _clean(value) and len(phones) < 30:
            phones.append(_clean(value.removeprefix("tel:")))
        elif name == "EMAIL" and _clean(value) and len(emails) < 30:
            emails.append(_clean(value))
    display = full or " ".join(x for x in (given, family) if x) or next(iter(phones or emails), "")
    if not display:
        return None
    return {"name": display, "given": given, "family": family, "phones": list(dict.fromkeys(phones)), "emails": list(dict.fromkeys(emails))}


def _when(value: str) -> str | None:
    value = value.strip()
    found = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?", value)
    if not found:
        return _clean(value, 40) or None
    y, mo, d, h, mi, s, z = found.groups()
    if h is None:
        return f"{y}-{mo}-{d}"
    return f"{y}-{mo}-{d}T{h}:{mi}:{s or '00'}" + ("+00:00" if z else "")


def parse_vcalendar(payload: bytes) -> tuple[list[dict], list[dict], int]:
    """(events, todos, skipped components) of one vCalendar 1.0/iCalendar 2.0 object, in the keys of the Gammu calendar reader."""
    events, todos, skipped = [], [], 0
    current: dict | None = None
    kind = ""
    for name, _, value in properties(payload):
        if name == "BEGIN" and value.strip().upper() in ("VEVENT", "VTODO"):
            current, kind = {}, value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() not in ("VCALENDAR",):
            skipped += 1
        elif name == "END" and current is not None and value.strip().upper() == kind:
            (events if kind == "VEVENT" else todos).append(current)
            current = None
        elif current is not None:
            current.setdefault(name, value)
    out_events = [
        {
            "summary": _clean(_unescape(e.get("SUMMARY", ""))) or None,
            "start": _when(e["DTSTART"]) if e.get("DTSTART") else None,
            "end": _when(e["DTEND"]) if e.get("DTEND") else None,
            "location": _clean(_unescape(e.get("LOCATION", ""))) or None,
            "description": _clean(_unescape(e.get("DESCRIPTION", "")), 2000) or None,
            "recurrence": _clean(e.get("RRULE", "")) or None,
        }
        for e in events
    ]
    out_todos = [
        {
            "summary": _clean(_unescape(t.get("SUMMARY", ""))) or None,
            "due": _when(t["DUE"]) if t.get("DUE") else None,
            "status": _clean(t.get("STATUS", "")) or None,
            "priority": _clean(t.get("PRIORITY", "")) or None,
            "description": _clean(_unescape(t.get("DESCRIPTION", "")), 2000) or None,
        }
        for t in todos
    ]
    return out_events, out_todos, skipped


def contacts_from(items: list[Item]) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for item in items:
        row = parse_vcard(item.payload) if item.mime.lower().endswith("vcard") else None
        if row is None:
            skipped += 1
        else:
            rows.append(row)
    return rows, skipped


def calendar_from(items: list[Item]) -> dict:
    events: list[dict] = []
    todos: list[dict] = []
    skipped = 0
    for item in items:
        e, t, s = parse_vcalendar(item.payload)
        events += e
        todos += t
        skipped += s
        if not e and not t:
            skipped += 1
    return {"events": events, "todos": todos, "skipped_components": skipped}
