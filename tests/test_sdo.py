import struct

import pytest

from canopen_sim import frames
from canopen_sim.od import (
    BAD_COMMAND, LENGTH_MISMATCH, TIMEOUT, TOGGLE, TOO_LONG, DataType,
    ObjectDictionary, Variable,
)
from canopen_sim.sdo import MAX_VARIABLE_LENGTH, SDO_TIMEOUT, SdoServer


def make():
    od = ObjectDictionary()
    od.add(Variable(0x1000, 0, "device type", DataType.UNSIGNED32, "const", 0x00020192))
    od.add(Variable(0x1008, 0, "name", DataType.VISIBLE_STRING, "const", "SIM-DRIVE-001"))
    od.add(Variable(0x2000, 0, "u16", DataType.UNSIGNED16, "rw", 0))
    od.add(Variable(0x2001, 0, "text", DataType.VISIBLE_STRING, "rw", ""))
    return od, SdoServer(od, 5)


def abort_of(frame):
    assert frame[0] == 0x80
    return struct.unpack_from("<I", frame, 4)[0]


def test_frame_cob_ids():
    assert frames.sdo_tx(5) == 0x585 and frames.sdo_rx(5) == 0x605
    assert frames.tpdo(1, 5) == 0x185 and frames.tpdo(4, 5) == 0x485
    assert frames.rpdo(1, 5) == 0x205 and frames.rpdo(4, 5) == 0x505
    assert frames.emcy(5) == 0x085 and frames.heartbeat(5) == 0x705


def test_expedited_upload():
    _, s = make()
    (r,) = s.handle(bytes([0x40, 0x00, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    assert r == bytes([0x43, 0x00, 0x10, 0x00]) + struct.pack("<I", 0x00020192)


def test_expedited_download():
    od, s = make()
    (r,) = s.handle(bytes([0x2B, 0x00, 0x20, 0x00, 0x34, 0x12, 0, 0]), 0.0)
    assert r == bytes([0x60, 0x00, 0x20, 0x00, 0, 0, 0, 0])
    assert od.get(0x2000, 0) == 0x1234


def test_segmented_upload_toggles():
    _, s = make()
    (r,) = s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    assert r[0] == 0x41 and struct.unpack_from("<I", r, 4)[0] == 13
    (r1,) = s.handle(bytes([0x60]) + bytes(7), 0.1)
    assert r1 == bytes([0x00]) + b"SIM-DRI"
    (r2,) = s.handle(bytes([0x70]) + bytes(7), 0.2)
    # 6 bytes left: n = 1, c = 1, toggle = 1
    assert r2[0] == 0x10 | (1 << 1) | 1
    assert r2[1:7] == b"VE-001"


def test_segmented_upload_exact_multiple_of_seven():
    od, s = make()
    od.set(0x1008, 0, "ABCDEFG")
    (r,) = s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    assert r[0] == 0x41
    (r1,) = s.handle(bytes([0x60]) + bytes(7), 0.1)
    assert r1 == bytes([0x01]) + b"ABCDEFG"


def test_segmented_download():
    od, s = make()
    text = b"HELLO-WORLD!"  # 12 bytes
    (r,) = s.handle(bytes([0x21, 0x01, 0x20, 0x00]) + struct.pack("<I", 12), 0.0)
    assert r[0] == 0x60
    (r,) = s.handle(bytes([0x00]) + text[:7], 0.1)
    assert r[0] == 0x20
    assert od.get(0x2001, 0) == ""  # not written before the last segment
    (r,) = s.handle(bytes([0x10 | (2 << 1) | 1]) + text[7:] + bytes(2), 0.2)
    assert r[0] == 0x30
    assert od.get(0x2001, 0) == "HELLO-WORLD!"


def test_segmented_download_without_size():
    od, s = make()
    s.handle(bytes([0x20, 0x01, 0x20, 0x00, 0, 0, 0, 0]), 0.0)
    s.handle(bytes([0x00]) + b"ABCDEFG", 0.1)
    s.handle(bytes([0x10 | (6 << 1) | 1]) + b"H" + bytes(6), 0.2)
    assert od.get(0x2001, 0) == "ABCDEFGH"


def test_wrong_toggle_aborts():
    _, s = make()
    s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    (r,) = s.handle(bytes([0x70]) + bytes(7), 0.1)  # first must be toggle 0
    assert abort_of(r) == TOGGLE
    assert r[1:4] == bytes([0x08, 0x10, 0x00])
    # idle again
    (r,) = s.handle(bytes([0x40, 0x00, 0x10, 0x00, 0, 0, 0, 0]), 0.2)
    assert r[0] == 0x43


def test_unknown_command_aborts():
    _, s = make()
    (r,) = s.handle(bytes([0xE0]) + bytes(7), 0.0)
    assert abort_of(r) == BAD_COMMAND


def test_segment_without_transfer_aborts():
    _, s = make()
    (r,) = s.handle(bytes([0x60]) + bytes(7), 0.0)
    assert abort_of(r) == BAD_COMMAND


def test_od_error_aborts_with_index():
    _, s = make()
    (r,) = s.handle(bytes([0x40, 0x99, 0x99, 0x00, 0, 0, 0, 0]), 0.0)
    assert r[0] == 0x80 and r[1:4] == bytes([0x99, 0x99, 0x00])


def test_transfer_timeout_aborts():
    _, s = make()
    s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 10.0)
    assert s.poll(10.0 + SDO_TIMEOUT - 0.1) == []
    (r,) = s.poll(10.0 + 1.1)
    assert abort_of(r) == TIMEOUT
    assert s.poll(20.0) == []
    (r,) = s.handle(bytes([0x40, 0x00, 0x10, 0x00, 0, 0, 0, 0]), 21.0)
    assert r[0] == 0x43


def test_client_abort_resets_transfer():
    _, s = make()
    s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    assert s.handle(bytes([0x80, 0x08, 0x10, 0x00]) + struct.pack("<I", 0x08000000), 0.1) == []
    (r,) = s.handle(bytes([0x40, 0x00, 0x10, 0x00, 0, 0, 0, 0]), 0.2)
    assert r[0] == 0x43
    assert s.poll(5.0) == []


def dl_init(s, idx, size=None, now=0.0, sub=0):
    if size is None:
        return s.handle(bytes([0x20, idx & 0xFF, idx >> 8, sub, 0, 0, 0, 0]), now)
    return s.handle(bytes([0x21, idx & 0xFF, idx >> 8, sub]) + struct.pack("<I", size), now)


def test_download_segment_wrong_toggle_aborts():
    _, s = make()
    dl_init(s, 0x2001, 12)
    (r,) = s.handle(bytes([0x10]) + b"ABCDEFG", 0.1)
    assert abort_of(r) == TOGGLE and r[1:4] == bytes([0x01, 0x20, 0x00])
    assert s.state == "idle"


def test_expedited_download_with_size_indicator_on_wire():
    od, s = make()
    # e=1, s=1, n=2 -> 2 bytes in 4
    (r,) = s.handle(bytes([0x23 | (2 << 2), 0x00, 0x20, 0x00, 0x34, 0x12, 0xFF, 0xFF]), 0.0)
    assert r == bytes([0x60, 0x00, 0x20, 0x00, 0, 0, 0, 0])
    assert od.get(0x2000, 0) == 0x1234


def test_expedited_download_without_size_bit():
    od, s = make()
    od.add(Variable(0x2002, 0, "u32", DataType.UNSIGNED32, "rw", 0))
    (r,) = s.handle(bytes([0x22, 0x02, 0x20, 0x00]) + struct.pack("<I", 0xCAFEBABE), 0.0)
    assert r[0] == 0x60
    assert od.get(0x2002, 0) == 0xCAFEBABE


def test_expedited_upload_short_sets_n_bits():
    _, s = make()
    (r,) = s.handle(bytes([0x40, 0x00, 0x20, 0x00, 0, 0, 0, 0]), 0.0)
    # u16: e=1, s=1, n=2
    assert r[0] == 0x40 | (2 << 2) | 0x03
    assert r[4:6] == b"\x00\x00"


def test_short_and_empty_frames():
    _, s = make()
    assert s.handle(b"", 0.0) == []
    (r,) = s.handle(bytes([0x40, 0x00, 0x10, 0x00]), 0.0)
    assert r[0] == 0x43
    assert len(r) == 8


def test_download_size_mismatch_too_short():
    od, s = make()
    dl_init(s, 0x2001, 12)
    s.handle(bytes([0x00]) + b"ABCDEFG", 0.1)
    (r,) = s.handle(bytes([0x10 | (4 << 1) | 1]) + b"HIJ" + bytes(4), 0.2)
    assert abort_of(r) == LENGTH_MISMATCH
    assert od.get(0x2001, 0) == ""
    assert s.state == "idle"


def test_download_surplus_data_aborts_too_long():
    od, s = make()
    dl_init(s, 0x2001, 8)
    s.handle(bytes([0x00]) + b"ABCDEFG", 0.1)
    (r,) = s.handle(bytes([0x10 | (4 << 1) | 1]) + b"HIJ" + bytes(4), 0.2)
    assert abort_of(r) == TOO_LONG
    assert od.get(0x2001, 0) == ""
    assert s.state == "idle"


def test_download_announced_size_above_cap_aborts():
    od, s = make()
    (r,) = dl_init(s, 0x2000, 8)  # u16 entry
    assert abort_of(r) == TOO_LONG
    (r,) = dl_init(s, 0x2001, MAX_VARIABLE_LENGTH + 1)
    assert abort_of(r) == TOO_LONG


def test_download_without_size_is_bounded_for_fixed_type():
    od, s = make()
    dl_init(s, 0x2000)  # u16: cap 2 bytes
    (r,) = s.handle(bytes([0x00]) + b"ABCDEFG", 0.1)
    assert abort_of(r) == TOO_LONG
    assert s.state == "idle"


def test_download_without_size_is_bounded_for_string():
    _, s = make()
    dl_init(s, 0x2001)
    tog = 0
    got = None
    for i in range(MAX_VARIABLE_LENGTH // 7 + 2):
        (r,) = s.handle(bytes([tog << 4]) + b"A" * 7, 0.0)
        if r[0] == 0x80:
            got = r
            break
        tog ^= 1
    assert got is not None and abort_of(got) == TOO_LONG
    assert s.state == "idle"


def test_download_timeout_and_activity_refresh():
    _, s = make()
    dl_init(s, 0x2001, 12, now=10.0)
    s.handle(bytes([0x00]) + b"ABCDEFG", 10.8)
    assert s.poll(11.5) == []
    (r,) = s.poll(11.9)
    assert abort_of(r) == TIMEOUT and r[1:4] == bytes([0x01, 0x20, 0x00])
    assert s.state == "idle"


def test_upload_activity_refresh():
    _, s = make()
    s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    s.handle(bytes([0x60]) + bytes(7), 0.8)
    assert s.poll(1.5) == []
    (r,) = s.poll(1.9)
    assert abort_of(r) == TIMEOUT


def test_completed_upload_returns_to_idle():
    _, s = make()
    s.handle(bytes([0x40, 0x08, 0x10, 0x00, 0, 0, 0, 0]), 0.0)
    s.handle(bytes([0x60]) + bytes(7), 0.1)
    (r,) = s.handle(bytes([0x70]) + bytes(7), 0.2)
    assert r[0] & 1
    assert s.state == "idle"
    (r,) = s.handle(bytes([0x60]) + bytes(7), 0.3)
    assert abort_of(r) == BAD_COMMAND
