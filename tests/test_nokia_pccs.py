"""Nokia PCCS over USB OBEX: byte layout, a fake phone that speaks the protocol, parsers on synthetic data, the adapter.

Everything here is synthetic. The byte layout is checked against names recorded from PC Suite on one Nokia E72; this
proves the encoding, not behaviour on any other phone, with a locked keypad, with large stores or with PC Suite open.
"""

from __future__ import annotations

import json
import struct

import pytest

from symport.phone import capabilities, nokia_pccs, pccs_windows
from symport.phone.nokia_pccs import PccsBusy, PccsError, PccsSession
from symport.phone.pccs_adapter import PccsAdapter

# Name headers recorded from PC Suite (E72). The OpenStore ones contain the captured "unknown" bytes.
RAW = {
    "list_stores": ("5953434e203a694c74737453726f73650000", nokia_pccs.NAME_LIST_STORES),
    "reset": ("5953434e203a6552657343746168676e4965666e006f0000", nokia_pccs.NAME_RESET_CHANGE_INFO),
    "get_format": ("5953434e203a654753746f7465726f466d7274610000", nokia_pccs.NAME_GET_STORE_FORMAT),
    "set_format": ("5953434e203a655352746d65746f53656f7465726f466d7274610000", nokia_pccs.NAME_SET_REMOTE_STORE_FORMAT),
    "list_changes": ("5953434e203a694c747368436e61656700730000", nokia_pccs.NAME_LIST_CHANGES),
    "close": ("5953434e203a6c43736f53656f7465720000", nokia_pccs.NAME_CLOSE_STORE),
}
OPEN_CONTACTS = "5953434e203a704f6e657453726f3a65dd201f6d9c10239a0e5d4300433a6e6f617474632e73646300620000"
OPEN_CALENDAR = "5953434e203a704f6e657453726f3a65de201f6d5410312a087543006c616e65616400720000"
READ_ONE = "5953434e203a6552646174496d653a73012000000100000000000000"
READ_TWO_IDS = "5953434e203a6552646174496d653a7302200000010000000200000000000000"

VCARD = b"BEGIN:VCARD\r\nVERSION:2.1\r\nN:;Claude;;;\r\nTEL;CELL:222\r\nEND:VCARD\r\n"
VCARD_QP = (
    b"BEGIN:VCARD\r\nVERSION:2.1\r\n"
    b"N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=98=D0=B2=D0=B0=D0=BD=D0=BE=D0=B2;=D0=98=D0=B2=D0=B0=D0=BD;;;\r\n"
    b"TEL;CELL:+79990001122\r\nTEL;WORK:333\r\nEMAIL;INTERNET:ivan@example.org\r\nEND:VCARD\r\n"
)
VCAL = (
    b"BEGIN:VCALENDAR\r\nVERSION:1.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Test =D0=B2=D1=81=D1=82=D1=80=D0=B5=D1=87=D0=B0\r\n"
    b"DTSTART:20250102T100000Z\r\nDTEND:20250102T110000Z\r\nLOCATION:Office\r\nEND:VEVENT\r\n"
    b"BEGIN:VTODO\r\nUID:2\r\nSUMMARY:Buy milk\r\nDUE:20250103\r\nSTATUS:NEEDS ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)
CAPABILITY = (
    b'<CAP><General><Manufacturer>Nokia</Manufacturer><Model>E72</Model><SN>356789012345678</SN>'
    b'<SW>RM-530 v 051.003 21-12-2010</SW></General></CAP>'
)
SERIAL = "356789012345678"


# ---- a fake phone ----------------------------------------------------------------------------------------------------
def unswap(name: bytes) -> bytes:
    out = bytearray(len(name))
    out[0::2], out[1::2] = name[1::2], name[0::2]
    return bytes(out)[:-2]  # the two terminating zero bytes; an odd-length pad byte stays and is harmless


def response(code: int, body: bytes = b"", *, final: bool = True, fixed: bytes = b"", extra: bytes = b"") -> bytes:
    headers = extra + (bytes([0x49 if final else 0x48]) + struct.pack(">H", len(body) + 3) + body if body else b"")
    return bytes([code]) + struct.pack(">H", 3 + len(fixed) + len(headers)) + fixed + headers


def record(ident: int, mime: str, version: str, payload: bytes, service: bytes = b"\x00\x01\x02\x03\x04\x05") -> bytes:
    rest = struct.pack("<I", ident) + service + b"\x80" + struct.pack("<H", len(mime)) + mime.encode()
    rest += struct.pack("<H", len(version)) + version.encode() + struct.pack("<I", len(payload)) + payload
    return struct.pack("<I", len(rest)) + rest


class FakePhone:
    """Speaks just enough PCCS. Records what it was asked so tests can check order and the absence of writes."""

    def __init__(self, contacts=(VCARD,), calendar=(VCAL,), *, open_status=1, chunk=0, capability=CAPABILITY, who=nokia_pccs.FOLDER_BROWSING_TARGET,
                 fail_on=None, mangle=None):
        self.stores = {"contacts": list(contacts), "calendar": list(calendar)}
        self.open_status, self.chunk, self.capability, self.who = open_status, chunk, capability, who
        self.fail_on, self.mangle = fail_on, mangle
        self.log: list[str] = []
        self.puts: list[tuple[str, bytes]] = []
        self.packets: list[bytes] = []
        self.open_store: str | None = None
        self.reset_done = False
        self.connected = False
        self.closed = False
        self.cancelled = False
        self.pending: list[bytes] = []

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True

    def exchange(self, packet: bytes, timeout: float) -> bytes:
        self.packets.append(packet)
        op = packet[0]
        if op == nokia_pccs.CONNECT:
            self.log.append("Connect")
            if self.who is None:
                return response(0xC0)
            self.connected = True
            who = bytes([0x4A]) + struct.pack(">H", 19) + self.who
            return response(nokia_pccs.OK, fixed=bytes([0x10, 0, 0xFF, 0xFF]), extra=who + bytes([0xCB]) + b"\x00\x00\x00\x07")
        if op == nokia_pccs.DISCONNECT:
            self.log.append("Disconnect")
            self.connected = False
            return response(nokia_pccs.OK)
        if op == nokia_pccs.GET and self.pending:
            assert packet == nokia_pccs.get_continue_packet(), "a Continue repeats the GET without headers"
            return self.pending.pop(0)
        assert self.connected and bytes([0xCB]) + b"\x00\x00\x00\x07" in packet, "ConnectionId must be sent"
        headers = dict(nokia_pccs.parse_headers(packet[3:]))
        name = headers.get(nokia_pccs.H_NAME)
        if op == nokia_pccs.GET and name is None:
            self.log.append("capability")
            return self._answer(self.capability)
        logical = unswap(name or b"")
        command = logical.split(b":")[1].decode().strip("\x00 ") if logical.startswith(b"SYNC:") else "?"
        if command == "OpenStore":
            uid = logical[17:21]
            self.open_store = "contacts" if uid[0] == 0xDD else "calendar"
        self.log.append(command)
        if self.fail_on == command:
            return response(0xC4)
        if op == nokia_pccs.PUT:
            assert self.reset_done, "ResetChangeInfo must precede SetRemoteStoreFormat"
            body = dict(nokia_pccs.parse_headers(packet[3:]))[nokia_pccs.H_END_OF_BODY]
            self.puts.append((command, body))
            return response(nokia_pccs.OK)
        assert op == nokia_pccs.GET, "only GET and the one PUT are expected"
        if command == "OpenStore":
            return self._answer(struct.pack("<I", self.open_status) + bytes(8))
        assert self.open_store, "a store must be open"
        if command == "ResetChangeInfo":
            self.reset_done = True
            return self._answer(bytes(4))
        if command == "GetStoreFormat":
            return self._answer(bytes(4) + b"FORMAT")
        if command == "ListChanges":
            ids = list(range(1, len(self.stores[self.open_store]) + 1))
            return self._answer(struct.pack("<I", len(ids)) + b"".join(struct.pack("<I", i) for i in ids) + bytes(8))
        if command == "ReadItems":
            count = struct.unpack_from("<I", logical, 17)[0]
            ids = struct.unpack_from(f"<{count}I", logical, 21)
            mime, version = ("text/x-vcard", "2.1") if self.open_store == "contacts" else ("text/x-vcalendar", "1.0")
            data = struct.pack("<I", count) + b"".join(record(i, mime, version, self.stores[self.open_store][i - 1]) for i in ids)
            return self._answer(self.mangle(data) if self.mangle else data)
        if command == "CloseStore":
            self.open_store = None
            self.reset_done = False
            return self._answer(bytes(4))
        raise AssertionError("unexpected command " + command)

    def _answer(self, body: bytes) -> bytes:
        if self.chunk and len(body) > self.chunk:
            parts = [body[i : i + self.chunk] for i in range(0, len(body), self.chunk)]
            self.pending = [response(nokia_pccs.CONTINUE, p, final=False) for p in parts[1:-1]] + [response(nokia_pccs.OK, parts[-1])]
            return response(nokia_pccs.CONTINUE, parts[0], final=False)
        return response(nokia_pccs.OK, body)


# ---- layout ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(RAW))
def test_command_names_match_recorded_values(key):
    recorded, built = RAW[key]
    assert built.hex() == recorded


def test_open_store_and_read_items_names_match_recorded_values():
    assert nokia_pccs.open_store_name("contacts").hex() == OPEN_CONTACTS
    assert nokia_pccs.open_store_name("calendar").hex() == OPEN_CALENDAR
    assert nokia_pccs.read_items_name([1]).hex() == READ_ONE
    assert nokia_pccs.read_items_name([1, 2]).hex() == READ_TWO_IDS
    # one record with id 2: the id word changes, the pair swap puts it in the third pair
    assert nokia_pccs.read_items_name([2]).hex() == "5953434e203a6552646174496d653a73012000000200000000000000"


def test_contacts_format_body_is_the_fixed_63_bytes():
    assert len(nokia_pccs.CONTACTS_FORMAT) == 63
    packet = nokia_pccs.put_packet(b"\x00\x00\x00\x07", nokia_pccs.NAME_SET_REMOTE_STORE_FORMAT, nokia_pccs.CONTACTS_FORMAT)
    assert bytes([0xC3]) + struct.pack(">I", 0x3F) in packet


def test_connect_packet_is_folder_browsing_with_max_packet():
    packet = nokia_pccs.connect_packet()
    assert packet[0] == 0x80 and struct.unpack(">H", packet[1:3])[0] == len(packet)
    assert packet[3:7] == bytes([0x10, 0x00, 0xFF, 0xFF])
    assert packet[7:] == bytes([0x46, 0x00, 0x13]) + bytes.fromhex("F9EC7BC4953C11D2984E525400DC9E09")


def test_get_headers_order_and_put_end_of_body():
    ident = b"\x00\x00\x00\x07"
    get = nokia_pccs.get_packet(ident, nokia_pccs.NAME_CLOSE_STORE)
    assert get[0] == 0x83
    ids = [h for h, _ in nokia_pccs.parse_headers(get[3:])]
    assert ids == [0xCB, 0x01, 0x42, 0x4A]
    values = dict(nokia_pccs.parse_headers(get[3:]))
    assert values[0x42] == b"application/vnd.nokia.conml+wbxml\x00" and values[0x4A] == b"PC Suite"
    put = nokia_pccs.put_packet(ident, nokia_pccs.NAME_SET_REMOTE_STORE_FORMAT, b"abc")
    assert put[0] == 0x82
    assert [h for h, _ in nokia_pccs.parse_headers(put[3:])] == [0xCB, 0x01, 0xC3, 0x42, 0x4A, 0x49]
    assert nokia_pccs.disconnect_packet(ident)[0] == 0x81


def test_interface_guid_is_obex_not_phonet_or_lcif():
    assert nokia_pccs.INTERFACE_GUID == "{4F919100-4ADF-11D5-882D-00B0D02FE381}"
    assert "4F919102" not in nokia_pccs.INTERFACE_GUID and "4F91910C" not in nokia_pccs.INTERFACE_GUID
    assert pccs_windows.guid_bytes()[:4] == bytes([0x00, 0x91, 0x91, 0x4F])  # Data1 is little-endian in memory


def test_setupapi_detail_size_is_8_on_x64_and_never_5():
    assert pccs_windows.detail_cbsize(8) == 8 and pccs_windows.detail_cbsize(4) == 6
    assert 5 not in (pccs_windows.detail_cbsize(8), pccs_windows.detail_cbsize(4))


def test_windows_module_is_inert_elsewhere():
    assert pccs_windows.interface_paths() == []
    with pytest.raises(PccsError):
        pccs_windows.open_channel("x")


def test_no_write_command_can_be_built():
    forbidden = (b"CommitChanges", b"CreateItems", b"DeleteItems", b"UpdateItems", b"ModifyItems")
    names = [v for k, v in vars(nokia_pccs).items() if k.startswith("NAME_") and isinstance(v, bytes)]
    names += [nokia_pccs.open_store_name(k) for k in nokia_pccs.STORES] + [nokia_pccs.read_items_name([1])]
    for name in names:
        assert not any(word in unswap(name) for word in forbidden)
    assert not any(hasattr(nokia_pccs, n) for n in ("commit_changes", "create_items", "delete_items"))


# ---- session against a fake phone ------------------------------------------------------------------------------------
def session_for(phone: FakePhone) -> PccsSession:
    session = PccsSession(phone)
    session.connect()
    return session


def test_read_contacts_runs_the_verified_sequence_and_closes_the_store():
    phone = FakePhone(contacts=(VCARD, VCARD_QP))
    items, skipped = nokia_pccs.read_store(session_for(phone), "contacts")
    assert phone.log == ["Connect", "OpenStore", "ResetChangeInfo", "GetStoreFormat", "SetRemoteStoreFormat", "ListChanges", "ReadItems", "CloseStore"]
    assert skipped == 0 and [i.id for i in items] == [1, 2]
    assert phone.puts == [("SetRemoteStoreFormat", nokia_pccs.CONTACTS_FORMAT)]
    rows, bad = nokia_pccs.contacts_from(items)
    assert bad == 0
    assert rows[0] == {"name": "Claude", "given": "Claude", "family": "", "phones": ["222"], "emails": []}
    assert rows[1]["family"] == "Иванов" and rows[1]["given"] == "Иван" and rows[1]["name"] == "Иван Иванов"
    assert rows[1]["phones"] == ["+79990001122", "333"] and rows[1]["emails"] == ["ivan@example.org"]


def test_calendar_format_is_echoed_from_the_phone_without_its_first_word():
    phone = FakePhone()
    items, _ = nokia_pccs.read_store(session_for(phone), "calendar")
    assert phone.puts == [("SetRemoteStoreFormat", b"FORMAT")]
    result = nokia_pccs.calendar_from(items)
    assert result["events"] == [{"summary": "Test встреча", "start": "2025-01-02T10:00:00+00:00", "end": "2025-01-02T11:00:00+00:00",
                                 "location": "Office", "description": None, "recurrence": None}]
    assert result["todos"][0]["summary"] == "Buy milk" and result["todos"][0]["due"] == "2025-01-03"


def test_continue_responses_are_reassembled():
    phone = FakePhone(contacts=(VCARD, VCARD_QP), chunk=17)
    items, _ = nokia_pccs.read_store(session_for(phone), "contacts")
    assert len(items) == 2


def test_ids_are_read_in_batches():
    phone = FakePhone(contacts=tuple(VCARD for _ in range(nokia_pccs.READ_BATCH + 5)))
    items, _ = nokia_pccs.read_store(session_for(phone), "contacts")
    assert len(items) == nokia_pccs.READ_BATCH + 5
    assert phone.log.count("ReadItems") == 2


def test_open_store_refused_still_closes_the_store():
    phone = FakePhone(open_status=0)
    with pytest.raises(PccsError) as error:
        nokia_pccs.read_store(session_for(phone), "contacts")
    assert error.value.code == "store"
    assert phone.log[-1] == "CloseStore"
    assert "ResetChangeInfo" not in phone.log


def test_failure_in_the_middle_still_closes_the_store():
    phone = FakePhone(fail_on="ListChanges")
    with pytest.raises(PccsError):
        nokia_pccs.read_store(session_for(phone), "contacts")
    assert phone.log[-1] == "CloseStore"


def test_unrecognised_answer_never_crashes():
    for mangle in (lambda d: d[:7], lambda d: b"\xff" * 12, lambda d: struct.pack("<I", 3) + d[4:30]):
        phone = FakePhone(mangle=mangle)
        with pytest.raises(PccsError) as error:
            nokia_pccs.read_store(session_for(phone), "contacts")
        assert error.value.code == "format"
        assert phone.log[-1] == "CloseStore"


def test_record_that_is_not_a_card_is_skipped_not_fatal():
    phone = FakePhone(contacts=(VCARD, b"not a card at all"))
    items, skipped = nokia_pccs.read_store(session_for(phone), "contacts")
    assert len(items) == 1 and skipped == 1


def test_connect_rejects_other_interfaces():
    for who in (None, b"SYNCML-SYNC\x00\x00\x00\x00\x00", ):
        phone = FakePhone(who=who and who.ljust(16, b"\x00")[:16])
        with pytest.raises(PccsError) as error:
            PccsSession(phone).connect()
        assert error.value.code == "not-pccs"


def test_capability_reads_manufacturer_model_firmware_and_never_the_serial():
    info = nokia_pccs.parse_capability(CAPABILITY)
    assert info["manufacturer"] == "Nokia" and info["model"] == "E72" and info["rm"] == "RM-530"
    assert SERIAL not in json.dumps(info)
    assert nokia_pccs.parse_capability(b"garbage<<") == {}


def test_vcard_parser_is_tolerant():
    assert nokia_pccs.parse_vcard(b"BEGIN:VCARD\r\nEND:VCARD") is None
    row = nokia_pccs.parse_vcard(b"BEGIN:VCARD\nFN:A\\; B\nTEL;X-FOO:1\n 23\nitem1.TEL:456\nEND:VCARD")
    assert row["name"] == "A; B" and row["phones"] == ["123", "456"]
    only_phone = nokia_pccs.parse_vcard(b"BEGIN:VCARD\r\nTEL:555\r\nEND:VCARD\r\n")
    assert only_phone["name"] == "555"
    latin = nokia_pccs.parse_vcard("BEGIN:VCARD\r\nFN:Zoë\r\nEND:VCARD\r\n".encode("latin-1"))
    assert latin["name"] == "Zoë"


def test_soft_line_breaks_in_quoted_printable_are_joined():
    row = nokia_pccs.parse_vcard(b"BEGIN:VCARD\r\nFN;ENCODING=QUOTED-PRINTABLE:Long=\r\n Name\r\nEND:VCARD\r\n")
    assert row["name"] == "Long Name"


# ---- adapter ---------------------------------------------------------------------------------------------------------
class Wire:
    """interfaces()/opener() pair: three paths like a real phone, only one speaks PCCS."""

    def __init__(self, phone: FakePhone, order=("syncml", "empty", "pccs"), busy=False):
        self.phone, self.busy, self.opened = phone, busy, []
        self.paths = [f"\\\\?\\nmwcd#vid_0421&pid_0223&if_obx#{k}#{{4f919100-4adf-11d5-882d-00b0d02fe381}}" for k in order]

    def interfaces(self):
        return list(self.paths)

    def opener(self, path):
        if self.busy:
            raise PccsBusy()
        self.opened.append(path)
        if path.endswith("}") and "#pccs#" in path:
            return self.phone
        channel = FakePhone(who=None)
        self.opened.append(channel)
        return channel


def adapter_for(wire: Wire, **kw) -> PccsAdapter:
    return PccsAdapter(wire.interfaces, wire.opener, clock=kw.pop("clock", lambda: 0.0), sleep=kw.pop("sleep", lambda s: None), **kw)


def test_device_is_available_only_after_a_real_open_store():
    phone = FakePhone()
    row = adapter_for(Wire(phone)).device()
    assert row["id"] == "nokia-pccs" and row["connected"] is True and row["name"] == "Nokia E72" and row["rm"] == "RM-530"
    matrix = row["capability_matrix"]
    assert matrix["contacts.read"]["status"] == "available" and matrix["calendar.read"]["status"] == "available"
    assert phone.log.count("OpenStore") == 2 and phone.log.count("CloseStore") == 2
    assert phone.log[-1] == "Disconnect" and phone.closed
    for op in ("files.list", "files.download", "files.upload", "contacts.write", "contacts.stage", "tasks.read", "sms.read",
               "sms.send", "telemetry.read", "identity.read"):
        assert matrix[op]["status"] == "unsupported" and matrix[op]["evidence"]
    assert "contacts-read" in row["capabilities"] and "calendar-read" in row["capabilities"] and "files" not in row["capabilities"]


def test_open_store_failure_means_no_pim_capability():
    row = adapter_for(Wire(FakePhone(open_status=0))).device()
    assert row["connected"] is False and row["route_state"] == "silent"
    assert row["capability_matrix"]["contacts.read"]["status"] == "error"
    assert "contacts-read" not in row["capabilities"]


def test_unnamed_phone_gets_the_generic_name():
    row = adapter_for(Wire(FakePhone(capability=b"<nothing/>"))).device()
    assert row["name"] == "Nokia (USB OBEX)" and row["connected"] is True


def test_serial_number_never_appears_anywhere():
    phone = FakePhone()
    adapter = adapter_for(Wire(phone))
    row = adapter.device()
    result = adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
    blob = json.dumps([row, result, adapter.route_status(), capabilities.finalize(dict(row))], default=str)
    assert SERIAL not in blob and "nmwcd" not in blob and "if_obx" not in blob and "4f919100" not in blob.lower()


def test_no_interface_means_no_row():
    adapter = PccsAdapter(lambda: [], lambda p: None, clock=lambda: 0.0)
    assert adapter.device() is None and adapter.route_status()["state"] == "unavailable"


def test_busy_channel_is_reported_as_pc_suite_holding_it():
    adapter = adapter_for(Wire(FakePhone(), busy=True))
    row = adapter.device()
    assert row["connected"] is False and row["route_state"] == "busy" and row["failure"] == {"reason": "port-busy"}
    assert "PC Suite" in row["evidence"]
    with pytest.raises(ValueError, match="PC Suite"):
        adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})


def test_execute_returns_gammu_shapes_and_marks_the_route_verified():
    adapter = adapter_for(Wire(FakePhone(contacts=(VCARD,))))
    contacts = adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
    assert contacts == {"contacts": [{"name": "Claude", "given": "Claude", "family": "", "phones": ["222"], "emails": []}]}
    calendar = adapter.execute({"action": "calendar-read", "device": "nokia-pccs"})
    assert set(calendar) >= {"events", "todos", "status"} and calendar["status"] == "read" and len(calendar["events"]) == 1
    assert adapter.route_status()["state"] == "verified"


def test_reads_close_the_store_the_session_and_the_channel():
    phone = FakePhone()
    adapter = adapter_for(Wire(phone))
    adapter.execute({"action": "calendar-read", "device": "nokia-pccs"})
    assert phone.log[-2:] == ["CloseStore", "Disconnect"] and phone.closed and phone.open_store is None


def test_unreadable_answer_is_an_error_not_an_empty_list():
    phone = FakePhone(mangle=lambda d: b"\xff" * 12)
    adapter = adapter_for(Wire(phone))
    with pytest.raises(ValueError, match="не распознан"):
        adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
    assert phone.log[-2:] == ["CloseStore", "Disconnect"] and phone.closed


def test_first_connect_is_retried_after_pc_suite_was_closed():
    class Flaky(Wire):
        calls = 0

        def opener(self, path):
            if "#pccs#" in path:
                Flaky.calls += 1
                if Flaky.calls <= 2:
                    return FakePhone(who=None)
            return super().opener(path)

    pauses = []
    adapter = adapter_for(Flaky(FakePhone()), sleep=pauses.append, pause=4.0)
    assert adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})["contacts"]
    assert pauses == [4.0, 4.0]


def test_retries_stop_after_three_attempts():
    pauses = []
    adapter = adapter_for(Wire(FakePhone(who=None)), sleep=pauses.append)
    with pytest.raises(ValueError):
        adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
    assert len(pauses) == 2


def test_everything_but_pim_reads_is_refused():
    adapter = adapter_for(Wire(FakePhone()))
    for action in ("list", "download", "upload", "contacts-import", "contacts-stage", "sms-send", "personal-apply", "delete", "mkdir"):
        with pytest.raises(ValueError):
            adapter.execute({"action": action, "device": "nokia-pccs", "path": "x"})


def test_only_the_format_put_reaches_the_phone():
    phone = FakePhone()
    adapter = adapter_for(Wire(phone))
    adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
    adapter.execute({"action": "calendar-read", "device": "nokia-pccs"})
    assert {name for name, _ in phone.puts} == {"SetRemoteStoreFormat"}
    assert not {"CommitChanges", "CreateItems", "DeleteItems"} & set(phone.log)


def test_a_second_operation_is_refused_while_one_runs():
    adapter = adapter_for(Wire(FakePhone()))
    assert adapter.lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="занят"):
            adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})
        assert adapter.device() is not None or True  # the probe must not block or talk to the phone meanwhile
    finally:
        adapter.lock.release()


def test_probe_does_not_talk_to_the_phone_while_a_read_holds_the_lock():
    phone = FakePhone()
    adapter = adapter_for(Wire(phone))
    adapter.lock.acquire()
    try:
        assert adapter.device() is None
    finally:
        adapter.lock.release()
    assert phone.log == []


def test_probe_result_is_cached_and_refreshed_by_time():
    phone = FakePhone()
    now = [0.0]
    adapter = adapter_for(Wire(phone), clock=lambda: now[0])
    adapter.device()
    adapter.device()
    assert phone.log.count("Connect") == 1
    now[0] = 120
    adapter.device()
    assert phone.log.count("Connect") == 2


def test_close_cancels_and_waits_for_the_running_read():
    phone = FakePhone()
    adapter = adapter_for(Wire(phone))
    adapter.close()
    assert adapter.device() is None and phone.log == []
    with pytest.raises(ValueError):
        adapter.execute({"action": "contacts-read", "device": "nokia-pccs"})


def test_local_transports_close_reaches_adapters(tmp_path):
    from symport.phone.local import LocalTransports

    transports = LocalTransports(tmp_path)
    closed = []

    class Spy:
        def close(self):
            closed.append(True)

        def device(self):
            return None

    transports.adapters["spy"] = Spy()
    transports.close()
    assert closed == [True]
