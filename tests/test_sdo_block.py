import struct

from canopen_sim.od import (
    BAD_BLOCK_SIZE, CRC_ERROR, DataType, ObjectDictionary, Variable,
)
from canopen_sim.sdo import SdoServer, crc16


def make():
    od = ObjectDictionary()
    od.add(Variable(0x2001, 0, "text", DataType.VISIBLE_STRING, "rw", ""))
    od.add(Variable(0x2002, 0, "big", DataType.OCTET_STRING, "rw", b""))
    return od, SdoServer(od, 5)


def abort_of(frame):
    assert frame[0] == 0x80
    return struct.unpack_from("<I", frame, 4)[0]


def segs(payload):
    chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
    return [bytes([i]) + c.ljust(7, b"\0") for i, c in enumerate(chunks, 1)]


def init_dl(s, size, cc=True):
    return s.handle(bytes([0xC0 | (4 if cc else 0) | 2, 0x01, 0x20, 0x00]) + struct.pack("<I", size), 0.0)


def test_crc16_check_value():
    assert crc16(b"123456789") == 0x31C3


def test_block_download_with_crc():
    od, s = make()
    payload = bytes((i * 7) & 0xFF for i in range(1000))
    (r,) = s.handle(bytes([0xC6, 0x02, 0x20, 0x00]) + struct.pack("<I", len(payload)), 0.0)
    assert r[0] == 0xA4
    sg = segs(payload)
    for f in sg[:127]:
        out = s.handle(f, 0.1)
    assert out[0][:3] == bytes([0xA2, 127, 127])
    rest = [bytes([i]) + f[1:] for i, f in enumerate(sg[127:], 1)]
    for f in rest[:-1]:
        assert s.handle(f, 0.2) == []
    (a,) = s.handle(bytes([0x80 | rest[-1][0]]) + rest[-1][1:], 0.2)
    assert a[:3] == bytes([0xA2, len(rest), 127])
    n = len(sg) * 7 - len(payload)
    (e,) = s.handle(bytes([0xC1 | (n << 2)]) + struct.pack("<H", crc16(payload)) + bytes(5), 0.3)
    assert e[0] == 0xA1
    assert od.get(0x2002, 0) == payload and s.state == "idle"


def test_block_download_bad_crc_aborts():
    od, s = make()
    payload = b"hello world"
    s.handle(bytes([0xC6, 0x01, 0x20, 0x00]) + struct.pack("<I", len(payload)), 0.0)
    f = segs(payload)
    s.handle(f[0], 0.1)
    s.handle(bytes([0x80 | f[1][0]]) + f[1][1:], 0.1)
    (r,) = s.handle(bytes([0xC1 | (3 << 2), 0, 0]) + bytes(6), 0.2)
    assert abort_of(r) == CRC_ERROR
    assert od.get(0x2001, 0) == ""


def test_block_download_no_crc_ignores_crc_field():
    od, s = make()
    s.handle(bytes([0xC2, 0x01, 0x20, 0x00]) + struct.pack("<I", 3), 0.0)
    s.handle(bytes([0x81]) + b"abc" + bytes(4), 0.1)
    (r,) = s.handle(bytes([0xC1 | (4 << 2), 0xFF, 0xFF]) + bytes(5), 0.2)
    assert r[0] == 0xA1 and od.get(0x2001, 0) == "abc"


def test_block_download_bad_sequence():
    od, s = make()
    payload = bytes(range(35))  # 5 segments
    s.handle(bytes([0xC6, 0x02, 0x20, 0x00]) + struct.pack("<I", 35), 0.0)
    f = segs(payload)
    assert s.handle(f[0], 0.1) == []
    assert s.handle(f[1], 0.1) == []
    (a,) = s.handle(f[3], 0.1)  # seqno 4, expected 3 -> ack last good
    assert a[:3] == bytes([0xA2, 2, 127])
    assert s.handle(f[4], 0.1) == []  # discarded
    # client restarts sub-block at seqno 1 with the data from ackseq+1
    again = [bytes([i + 1]) + f[i + 2][1:] for i in range(3)]
    assert s.handle(again[0], 0.2) == [] and s.handle(again[1], 0.2) == []
    (a,) = s.handle(bytes([0x80 | 3]) + again[2][1:], 0.2)
    assert a[:3] == bytes([0xA2, 3, 127])
    (e,) = s.handle(bytes([0xC1, *struct.pack("<H", crc16(payload))]) + bytes(5), 0.3)
    assert e[0] == 0xA1 and od.get(0x2002, 0) == payload


def test_block_download_seqno_zero_acks_last_good():
    _, s = make()
    s.handle(bytes([0xC6, 0x02, 0x20, 0x00]) + struct.pack("<I", 21), 0.0)
    assert s.handle(bytes([1]) + bytes(7), 0.1) == []
    (a,) = s.handle(bytes([0]) + bytes(7), 0.1)
    assert a[:2] == bytes([0xA2, 1])


def test_block_upload():
    od, s = make()
    payload = bytes(range(30))
    od.set(0x2002, 0, payload)
    (r,) = s.handle(bytes([0xA4, 0x02, 0x20, 0x00, 4, 0, 0, 0]), 0.0)
    assert r == bytes([0xC6, 0x02, 0x20, 0x00]) + struct.pack("<I", 30)
    out = s.handle(bytes([0xA3]), 0.1)
    assert len(out) == 4 and [f[0] for f in out] == [1, 2, 3, 4]
    assert b"".join(f[1:] for f in out)[:28] == payload[:28]
    out = s.handle(bytes([0xA2, 4, 4]), 0.2)
    assert [f[0] for f in out] == [0x81]
    assert out[0][1:3] == payload[28:30]
    (e,) = s.handle(bytes([0xA2, 1, 4]), 0.3)
    assert e[0] == 0xC1 | (5 << 2)
    assert struct.unpack_from("<H", e, 1)[0] == crc16(payload)
    assert s.handle(bytes([0xA1]), 0.4) == [] and s.state == "idle"


def test_block_upload_resend_after_loss():
    od, s = make()
    payload = bytes(range(28))
    od.set(0x2002, 0, payload)
    s.handle(bytes([0xA4, 0x02, 0x20, 0x00, 4, 0, 0, 0]), 0.0)
    s.handle(bytes([0xA3]), 0.1)
    out = s.handle(bytes([0xA2, 2, 4]), 0.2)  # only 2 received
    assert [f[0] for f in out] == [1, 0x82]
    assert out[0][1:] == payload[14:21]


def test_block_upload_protocol_switch():
    od, s = make()
    od.set(0x2002, 0, bytes(range(10)))
    (r,) = s.handle(bytes([0xA4, 0x02, 0x20, 0x00, 4, 10, 0, 0]), 0.0)
    assert r[0] == 0x41 and struct.unpack_from("<I", r, 4)[0] == 10
    (r,) = s.handle(bytes([0x60]), 0.1)
    assert r[0] == 0x00 and r[1:] == bytes(range(7))


def test_block_size_zero_aborts():
    _, s = make()
    (r,) = s.handle(bytes([0xA4, 0x01, 0x20, 0x00, 0, 0, 0, 0]), 0.0)
    assert abort_of(r) == BAD_BLOCK_SIZE
    (r,) = s.handle(bytes([0xA4, 0x01, 0x20, 0x00, 128, 0, 0, 0]), 0.0)
    assert abort_of(r) == BAD_BLOCK_SIZE


def test_client_abort_resets_transfer_block():
    _, s = make()
    s.handle(bytes([0xC6, 0x02, 0x20, 0x00]) + struct.pack("<I", 21), 0.0)
    s.handle(bytes([1]) + bytes(7), 0.1)
    assert s.handle(bytes([0x80, 0x02, 0x20, 0x00, 0, 0, 0, 0]), 0.2) == []
    assert s.state == "idle" and s.poll(10.0) == []
    s.handle(bytes([0xA4, 0x01, 0x20, 0x00, 4, 0, 0, 0]), 0.3)
    assert s.handle(bytes([0x80, 0x01, 0x20, 0x00, 0, 0, 0, 0]), 0.4) == []
    assert s.state == "idle"
