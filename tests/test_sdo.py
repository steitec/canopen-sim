import struct

import pytest

from canopen_sim import frames
from canopen_sim.od import (
    BAD_COMMAND, TIMEOUT, TOGGLE, DataType, ObjectDictionary, Variable,
)
from canopen_sim.sdo import SDO_TIMEOUT, SdoServer


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
