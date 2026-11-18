import struct

from canopen_sim import frames
from canopen_sim.comm_objects import PdoSpec, add_communication_objects, add_pdo_objects
from canopen_sim.frames import Frame
from canopen_sim.node import Node, NmtState
from canopen_sim.od import (
    INCOMPATIBLE, NOT_MAPPABLE, PDO_LENGTH, DataType, ObjectDictionary, SdoAbort, Variable,
)

ID = 5
TPDO1, RPDO1 = 0x185, 0x205
SYNC = Frame(frames.SYNC, b"")


def make(tpdos=(), rpdos=()):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020192, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="SIM", heartbeat_ms=0,
    )
    od.add(Variable(0x2000, 0, "a", DataType.UNSIGNED16, "rw", 0x1234, pdo_mappable=True))
    od.add(Variable(0x2001, 0, "b", DataType.UNSIGNED8, "rw", 7, pdo_mappable=True))
    od.add(Variable(0x2002, 0, "flag", DataType.BOOLEAN, "rw", 0, pdo_mappable=True))
    od.add(Variable(0x2003, 0, "nomap", DataType.UNSIGNED8, "rw", 0))
    od.add(Variable(0x2004, 0, "big", DataType.UNSIGNED64, "rw", 0, pdo_mappable=True))
    od.add(Variable(0x2005, 0, "wo", DataType.UNSIGNED8, "wo", 0, pdo_mappable=True))
    od.add(Variable(0x2006, 0, "lim", DataType.UNSIGNED8, "rw", 0, high=10, pdo_mappable=True))
    add_pdo_objects(od, ID, tpdos=list(tpdos), rpdos=list(rpdos))
    node = Node(ID, od, "n")
    node.boot(0.0)
    return od, node


def start(node, now=0.0):
    return node.handle(Frame(frames.NMT, bytes([1, ID])), now)


def tp(**kw):
    kw.setdefault("mapping", [(0x2000, 0, 16)])
    return PdoSpec(0, **kw)


def test_defaults_and_objects():
    od, node = make([tp()], [PdoSpec(1, [(0x2001, 0, 8)])])
    assert od.get(0x1800, 1) == TPDO1
    assert od.get(0x1A00, 0) == 1 and od.get(0x1A00, 1) == 0x20000010
    assert od.get(0x1401, 1) == 0x305
    assert RPDO1 not in node.listen_ids()
    assert 0x305 in node.listen_ids()


def test_event_tpdo_on_change():
    od, node = make([tp()])
    out = start(node)
    assert out == [Frame(TPDO1, b"\x34\x12")]  # current values once
    assert node.poll(0.1) == []
    od.set(0x2000, 0, 0xABCD)
    assert node.poll(0.2) == [Frame(TPDO1, b"\xcd\xab")]
    assert node.poll(0.3) == []


def test_event_tpdo_on_sdo_write_change():
    od, node = make([tp()])
    start(node)
    od.sdo_write(0x2000, 0, struct.pack("<H", 5))
    assert node.poll(0.1) == [Frame(TPDO1, b"\x05\x00")]


def test_inhibit_time_limits_rate():
    od, node = make([tp(inhibit_100us=1000)])  # 100 ms
    start(node, 0.0)
    od.set(0x2000, 0, 1)
    assert node.poll(0.05) == []
    assert node.poll(0.1) == [Frame(TPDO1, b"\x01\x00")]
    od.set(0x2000, 0, 2)
    assert node.poll(0.15) == []
    assert node.poll(0.2) == [Frame(TPDO1, b"\x02\x00")]


def test_event_timer_resends():
    od, node = make([tp(event_timer_ms=100)])
    start(node, 0.0)
    assert node.poll(0.05) == []
    assert node.poll(0.1) == [Frame(TPDO1, b"\x34\x12")]
    assert node.poll(0.15) == []
    assert node.poll(0.2) == [Frame(TPDO1, b"\x34\x12")]


def test_sync_every_second():
    _, node = make([tp(transmission_type=2)])
    assert start(node) == []
    assert node.handle(SYNC, 0.1) == []
    assert node.handle(SYNC, 0.2) == [Frame(TPDO1, b"\x34\x12")]
    assert node.handle(SYNC, 0.3) == []
    assert node.handle(SYNC, 0.4) == [Frame(TPDO1, b"\x34\x12")]


def test_acyclic_sync_only_after_change():
    od, node = make([tp(transmission_type=0)])
    start(node)
    assert node.handle(SYNC, 0.1) == []
    od.set(0x2000, 0, 9)
    assert node.poll(0.15) == []
    assert node.handle(SYNC, 0.2) == [Frame(TPDO1, b"\x09\x00")]
    assert node.handle(SYNC, 0.3) == []


def test_rpdo_writes_mapped_objects():
    od, node = make(rpdos=[PdoSpec(0, [(0x2000, 0, 16), (0x2001, 0, 8), (0x2002, 0, 1)])])
    start(node)
    assert node.handle(Frame(RPDO1, b"\x11\x22\x33\x01"), 0.1) == []
    assert od.get(0x2000, 0) == 0x2211 and od.get(0x2001, 0) == 0x33
    assert od.get(0x2002, 0) is True or od.get(0x2002, 0) == 1


def test_rpdo_writes_write_only_and_respects_limits():
    od, node = make(rpdos=[PdoSpec(0, [(0x2005, 0, 8), (0x2006, 0, 8)])])
    start(node)
    node.handle(Frame(RPDO1, bytes([4, 99])), 0.1)
    assert od.get(0x2005, 0) == 4
    assert od.get(0x2006, 0) == 0  # out of range, ignored


def test_rpdo_longer_than_mapping_accepted():
    od, node = make(rpdos=[PdoSpec(0, [(0x2001, 0, 8)])])
    start(node)
    assert node.handle(Frame(RPDO1, b"\x09\xff\xff"), 0.1) == []
    assert od.get(0x2001, 0) == 9


def test_short_rpdo_emcy_8210():
    od, node = make(rpdos=[PdoSpec(0, [(0x2000, 0, 16)])])
    start(node)
    out = node.handle(Frame(RPDO1, b"\x01"), 0.1)
    assert out == [Frame(0x085, b"\x10\x82\x11\x00\x00\x00\x00\x00")]
    assert od.get(0x2000, 0) == 0x1234
    assert od.get(0x1001, 0) & 0x11 == 0x11


def test_rpdo_deadline_emcy_8250():
    od, node = make(rpdos=[PdoSpec(0, [(0x2001, 0, 8)], event_timer_ms=100)])
    start(node, 0.0)
    assert node.poll(0.05) == []
    node.handle(Frame(RPDO1, b"\x01"), 0.08)
    assert node.poll(0.15) == []
    out = node.poll(0.2)
    assert out == [Frame(0x085, b"\x50\x82\x11\x00\x00\x00\x00\x00")]
    assert node.poll(0.5) == []  # reported once
    node.handle(Frame(RPDO1, b"\x01"), 0.6)
    assert node.poll(0.65) == []
    assert len(node.poll(0.7)) == 1


def test_no_pdo_outside_operational():
    od, node = make([tp()], [PdoSpec(0, [(0x2001, 0, 8)])])
    assert node.state == NmtState.PRE_OPERATIONAL
    od.set(0x2000, 0, 3)
    assert node.poll(0.1) == []
    assert node.handle(SYNC, 0.2) == []
    node.handle(Frame(RPDO1, b"\x09"), 0.3)
    assert od.get(0x2001, 0) == 7
    start(node, 0.4)
    node.handle(Frame(frames.NMT, bytes([2, ID])), 0.5)  # stop
    od.set(0x2000, 0, 4)
    assert node.poll(0.6) == []


def test_remap_valid_pdo_rejected():
    od, _ = make([tp()])
    for idx, sub, val in [(0x1A00, 0, 0), (0x1A00, 1, 0x20010008), (0x1800, 1, 0x1FF)]:
        try:
            od.sdo_write(idx, sub, struct.pack("<I" if sub or idx == 0x1800 else "<B", val))
        except SdoAbort as e:
            assert e.code == INCOMPATIBLE
        else:
            raise AssertionError("expected abort")
    assert od.get(0x1A00, 0) == 1


def w(od, idx, sub, val, fmt="<I"):
    od.sdo_write(idx, sub, struct.pack(fmt, val))


def test_remap_after_disable():
    od, node = make([tp(transmission_type=1)])
    start(node)
    w(od, 0x1800, 1, TPDO1 | 0x80000000)
    w(od, 0x1A00, 0, 0, "<B")
    w(od, 0x1A00, 1, 0x20010008)
    w(od, 0x1A00, 2, 0x20000010)
    w(od, 0x1A00, 0, 2, "<B")
    assert node.handle(SYNC, 0.1) == []  # still disabled
    w(od, 0x1800, 1, TPDO1)
    assert node.handle(SYNC, 0.2) == [Frame(TPDO1, b"\x07\x34\x12")]


def test_mapping_entry_needs_sub0_zero():
    od, _ = make([tp()])
    w(od, 0x1800, 1, TPDO1 | 0x80000000)
    try:
        w(od, 0x1A00, 1, 0x20010008)
    except SdoAbort as e:
        assert e.code == INCOMPATIBLE
    else:
        raise AssertionError


def _remap(od, entries):
    w(od, 0x1800, 1, TPDO1 | 0x80000000)
    w(od, 0x1A00, 0, 0, "<B")
    for i, e in enumerate(entries, 1):
        w(od, 0x1A00, i, e)
    w(od, 0x1A00, 0, len(entries), "<B")


def test_mapping_over_64_bits_rejected():
    od, _ = make([tp()])
    try:
        _remap(od, [0x20040040, 0x20010008])
    except SdoAbort as e:
        assert e.code == PDO_LENGTH
    else:
        raise AssertionError


def test_mapping_not_mappable_and_missing_rejected():
    od, _ = make([tp()])
    for entry, code in [(0x20030008, NOT_MAPPABLE), (0x30000008, 0x06020000)]:
        try:
            _remap(od, [entry])
        except SdoAbort as e:
            assert e.code == code
        else:
            raise AssertionError


def test_reset_node_restores_mapping():
    od, node = make([tp()])
    _remap(od, [0x20010008])
    w(od, 0x1800, 1, TPDO1)
    node.handle(Frame(frames.NMT, bytes([0x81, ID])), 1.0)
    assert od.get(0x1A00, 1) == 0x20000010 and od.get(0x1A00, 0) == 1


def test_mapping_zero_bits_rejected():
    od, _ = make([tp()])
    try:
        _remap(od, [0x20000000])
    except SdoAbort as e:
        assert e.code == PDO_LENGTH
    else:
        raise AssertionError


def test_mapping_bits_larger_than_object_rejected():
    od, _ = make([tp()])
    for entry in (0x20010010, 0x20000020, 0x20020009):
        try:
            _remap(od, [entry])
        except SdoAbort as e:
            assert e.code == PDO_LENGTH
        else:
            raise AssertionError(hex(entry))
    _remap(od, [0x20020001])  # BOOLEAN as 1 bit is fine


def test_rpdo_with_bad_mapping_never_raises():
    od, node = make(rpdos=[PdoSpec(0, [(0x2000, 0, 16)])])
    od.set(0x1600, 1, 0x20000000)  # zero bits, bypassing validation
    od.set(0x1600, 0, 1)
    start(node)
    node.handle(Frame(RPDO1, b"\x01\x02"), 0.1)
    od.set(0x1600, 1, 0x20000010)
    od.set(0x1600, 1, 0x20010020)  # wider than object
    node.handle(Frame(RPDO1, b"\x01\x02\x03\x04"), 0.2)


def test_pdo_emcy_honours_inhibit():
    od, node = make(rpdos=[PdoSpec(0, [(0x2000, 0, 16)], event_timer_ms=100)])
    od.set(0x1015, 0, 5000)  # 500 ms
    start(node, 0.0)
    out = node.handle(Frame(RPDO1, b"\x01"), 1.0)  # short: EMCY 0x8210 goes out
    assert [f.data[:2] for f in out] == [b"\x10\x82"]
    assert node.poll(1.2) == []  # deadline EMCY 0x8250 held back by inhibit
    assert node.poll(1.4) == []
    assert [f.data[:2] for f in node.poll(1.5)] == [b"\x50\x82"]
