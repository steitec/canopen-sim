import struct

import pytest

from canopen_sim import frames
from canopen_sim.comm_objects import PdoSpec, add_communication_objects, add_pdo_objects
from canopen_sim.faults import FaultError, FaultSet
from canopen_sim.frames import Frame
from canopen_sim.models.io import IoModel
from canopen_sim.node import Node
from canopen_sim.od import DataType, ObjectDictionary, Variable

ID = 5
TPDO1, TPDO2 = 0x185, 0x285
SDO_RX, SDO_TX = 0x605, 0x585


def make(heartbeat_ms=100, model=None, tpdos=(), ttype=255):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020192, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="SIM-DRIVE-001", heartbeat_ms=heartbeat_ms)
    od.add(Variable(0x2000, 0, "a", DataType.UNSIGNED16, "rw", 0x1234, pdo_mappable=True))
    od.add(Variable(0x2001, 0, "b", DataType.UNSIGNED16, "rw", 0x0A0B, pdo_mappable=True))
    od.add(Variable(0x2002, 0, "text", DataType.VISIBLE_STRING, "rw", ""))
    if model:
        for v in model.objects():
            od.add(v)
    specs = [PdoSpec(n, [(0x2000 + n, 0, 16)], transmission_type=ttype) for n in tpdos]
    add_pdo_objects(od, ID, tpdos=specs, rpdos=[])
    node = Node(ID, od, "n", model=model)
    node.boot(0.0)
    return od, node


def req(node, data, now=0.0):
    return node.handle(Frame(SDO_RX, bytes(data).ljust(8, b"\0")), now)


def read(node, index, sub=0, now=0.0):
    return req(node, [0x40, index & 0xFF, index >> 8, sub], now)


def start(node, now=0.0):
    return node.handle(Frame(frames.NMT, bytes([1, ID])), now)


def abort_code(frame):
    assert frame.data[0] == 0x80
    return struct.unpack_from("<I", frame.data, 4)[0]


SYNC = Frame(frames.SYNC, b"")


def test_mute():
    od, node = make(tpdos=[0])
    node.inject("mute")
    assert read(node, 0x1000) == []
    assert node.poll(0.2) == []
    assert node.state.name == "PRE_OPERATIONAL"
    assert start(node) == []
    assert node.state.name == "OPERATIONAL"  # state still evolves
    assert node.handle(SYNC, 0.3) == [] and node.poll(0.4) == []
    node.clear("mute")
    assert [f.cob_id for f in read(node, 0x1000, now=0.5)] == [SDO_TX]
    assert node.poll(0.6)[0].cob_id == 0x705


def test_no_heartbeat():
    _, node = make(tpdos=[0])
    assert [f.cob_id for f in node.poll(0.1)] == [0x705]
    node.inject("no_heartbeat")
    assert node.poll(0.2) == []
    assert [f.cob_id for f in read(node, 0x1000)] == [SDO_TX]  # SDO unaffected
    assert "no_heartbeat" in node.faults()
    node.clear("no_heartbeat")
    assert [f.cob_id for f in node.poll(0.3)] == [0x705]


def test_restart_sends_bootup():
    od, node = make()
    od.set(0x2000, 0, 7)
    out = node.inject("restart", now=1.0)
    assert out == [Frame(0x705, b"\x00")]
    assert od.get(0x2000, 0) == 0x1234  # defaults restored
    assert "restart" not in node.faults()


def test_double_bootup():
    _, node = make()
    node.inject("double_bootup")
    out = node.handle(Frame(frames.NMT, bytes([0x81, ID])), 1.0)
    assert out == [Frame(0x705, b"\x00")] * 2
    out = node.handle(Frame(frames.NMT, bytes([0x81, ID])), 2.0)
    assert out == [Frame(0x705, b"\x00")]  # one-shot
    node.inject("double_bootup")
    assert node.boot(3.0) == [Frame(0x705, b"\x00")] * 2


def test_sdo_abort_for_index_only():
    _, node = make()
    node.inject("sdo_abort", code=0x06010000, index=0x2000)
    (r,) = read(node, 0x2000)
    assert abort_code(r) == 0x06010000 and r.data[1:4] == bytes([0x00, 0x20, 0x00])
    assert [f.data[0] for f in read(node, 0x1000)] == [0x43]  # other index unaffected
    node.inject("sdo_abort")  # all requests, default code
    assert abort_code(read(node, 0x1000)[0]) == 0x08000000
    node.clear("sdo_abort")
    assert read(node, 0x1000)[0].data[0] == 0x43


def test_sdo_abort_keeps_segment_target_index():
    _, node = make()
    od = node.od
    od.set(0x2002, 0, "0123456789")
    assert read(node, 0x2002)[0].data[0] == 0x41
    node.inject("sdo_abort", index=0x2002)
    (r,) = req(node, [0x60])
    assert abort_code(r) == 0x08000000 and r.data[1:3] == bytes([0x02, 0x20])


def test_sdo_drop_once():
    od, node = make()
    node.inject("sdo_drop")
    assert req(node, [0x2B, 0x00, 0x20, 0x00, 0x55, 0x00]) == []
    assert od.get(0x2000, 0) == 0x1234  # state unchanged
    assert req(node, [0x2B, 0x00, 0x20, 0x00, 0x55, 0x00])[0].data[0] == 0x60
    assert od.get(0x2000, 0) == 0x55
    assert "sdo_drop" not in node.faults()


def test_sdo_bad_toggle():
    od, node = make()
    od.set(0x2002, 0, "0123456789ABCDEF")
    node.inject("sdo_bad_toggle")
    read(node, 0x2002)
    (r,) = req(node, [0x60])  # toggle 0 expected; answer must carry 1
    assert r.data[0] & 0x10 == 0x10 and r.data[1:8] == b"0123456"
    (r,) = req(node, [0x70])  # fault used up: toggle 1 answered correctly
    assert r.data[0] & 0x10 == 0x10 and r.data[1:8] == b"789ABCD"
    # download segment ack
    node.inject("sdo_bad_toggle")
    assert req(node, [0x21, 0x02, 0x20, 0x00, 9, 0, 0, 0])[0].data[0] == 0x60
    (r,) = req(node, [0x00, 0x41, 0x42, 0x43])
    assert r.data[0] == 0x30  # toggle inverted in ack of toggle 0


def test_delayed_answer_then_next_request():
    _, node = make()
    node.inject("sdo_delay", seconds=0.5)
    assert read(node, 0x1000, now=1.0) == []
    node.clear("sdo_delay")
    assert read(node, 0x1008, now=1.2) == []  # processed, answer queued behind
    assert [f for f in node.poll(1.4) if f.cob_id == SDO_TX] == []
    out = [f for f in node.poll(1.5) if f.cob_id == SDO_TX]
    assert [f.data[1:3] for f in out] == [bytes([0x00, 0x10]), bytes([0x08, 0x10])]


def test_pdo_dropout_every_second():
    od, node = make(tpdos=[0, 1], ttype=1, heartbeat_ms=0)
    start(node)
    node.inject("pdo_dropout", every=2, pdo=1)
    seen = [[f.cob_id for f in node.handle(SYNC, t)] for t in (1.0, 2.0, 3.0, 4.0)]
    assert seen == [[TPDO1, TPDO2], [TPDO2], [TPDO1, TPDO2], [TPDO2]]


def test_pdo_dropout_all():
    od, node = make(tpdos=[0], ttype=1, heartbeat_ms=0)
    start(node)
    node.inject("pdo_dropout", every=3)
    counts = [len(node.handle(SYNC, t)) for t in range(1, 7)]
    assert counts == [1, 1, 0, 1, 1, 0]


def test_pdo_bad_length():
    od, node = make(tpdos=[0], ttype=1, heartbeat_ms=0)
    start(node)
    node.inject("pdo_bad_length")
    assert node.handle(SYNC, 1.0) == [Frame(TPDO1, b"\x34")]
    node.inject("pdo_bad_length", length=0)
    assert node.handle(SYNC, 2.0) == [Frame(TPDO1, b"")]
    node.inject("pdo_bad_length", length=1, pdo=2)
    assert node.handle(SYNC, 3.0) == [Frame(TPDO1, b"\x34\x12")]
    with pytest.raises(FaultError):
        node.inject("pdo_bad_length", length=9)
    node.clear("pdo_bad_length")
    assert node.handle(SYNC, 4.0) == [Frame(TPDO1, b"\x34\x12")]


def test_pdo_freeze():
    od, node = make(tpdos=[0], ttype=1, heartbeat_ms=0)
    start(node)
    node.inject("pdo_freeze")
    od.set(0x2000, 0, 0xBEEF)
    assert node.handle(SYNC, 1.0) == [Frame(TPDO1, b"\x34\x12")]  # still sent, old data
    node.clear("pdo_freeze")
    assert node.handle(SYNC, 2.0) == [Frame(TPDO1, b"\xef\xbe")]


def test_emcy_injection():
    od, node = make()
    out = node.inject("emcy", code=0xFF01, register=0x80, data="0102", now=1.0)
    assert out == [Frame(0x085, struct.pack("<HB", 0xFF01, 0x81) + b"\x01\x02\0\0\0")]
    assert "emcy" in node.faults() and 0xFF01 in node.emcy.active()
    out = node.clear("emcy", now=2.0)
    assert out[0].data[:2] == b"\0\0" and node.emcy.active() == []
    assert "emcy" not in node.faults()
    node.inject("emcy")
    assert node.emcy.active() == [0xFF00]
    assert node.clear(now=3.0)[0].cob_id == 0x085 and node.faults() == []
    with pytest.raises(FaultError):
        node.inject("emcy", data="00" * 6)
    with pytest.raises(FaultError):
        node.inject("emcy", data="xyz")


def test_model_faults_and_listing():
    node_model = IoModel()
    _, node = make(model=node_model)
    out = node.inject("wire_break", now=1.0, channel=1)
    assert out and out[0].cob_id == 0x085
    node.inject("emcy")
    assert len(node.faults()) == 2 and "emcy" in node.faults()
    assert any(f.startswith("wire_break") for f in node.faults())
    out = node.clear(now=2.0)
    assert out and node.faults() == [] and node.emcy.active() == []
    assert node.tick(0.1, 3.0) == []
    with pytest.raises(FaultError):
        node.inject("wire_break", channel=99)


def test_tick_without_model():
    _, node = make()
    assert node.tick(0.1, 1.0) == []


def test_unknown_fault_lists_known():
    _, node = make(model=IoModel())
    with pytest.raises(FaultError) as exc:
        node.inject("nonsense")
    msg = str(exc.value)
    assert isinstance(exc.value, ValueError)
    for name in ("mute", "sdo_abort", "pdo_freeze", "emcy", "wire_break", "over_range"):
        assert name in msg
    with pytest.raises(FaultError):
        node.clear("nonsense")


def test_bad_params_rejected():
    _, node = make()
    for kind, params in [
        ("pdo_dropout", {"every": 1}), ("pdo_dropout", {"every": True}),
        ("sdo_delay", {"seconds": -1}), ("sdo_abort", {"code": -1}),
        ("sdo_abort", {"index": 0x10000}), ("mute", {"x": 1}),
        ("pdo_freeze", {"pdo": 5}), ("sdo_drop", {"count": 0}),
    ]:
        with pytest.raises(FaultError):
            node.inject(kind, **params)
    assert node.faults() == []


def test_count_limits_firing():
    _, node = make()
    node.inject("sdo_abort", count=2)
    assert read(node, 0x1000)[0].data[0] == 0x80
    assert read(node, 0x1000)[0].data[0] == 0x80
    assert read(node, 0x1000)[0].data[0] == 0x43


def test_faultset_api():
    fs = FaultSet()
    fs.inject("sdo_drop")
    assert fs.active() == ["sdo_drop"]
    assert fs.take("sdo_drop") == {} and fs.take("sdo_drop") is None
    fs.inject("pdo_dropout", every=4)
    assert fs.take("pdo_dropout")["every"] == 4 and fs.take("pdo_dropout")
    fs.clear()
    assert fs.active() == []


def test_every_listed_fault_can_be_cleared():
    _, node = make(model=IoModel())
    node.inject("wire_break", channel=1)
    node.inject("wire_break", channel=2)
    node.inject("short_circuit", channel=1)
    node.inject("emcy")
    for key in list(node.faults()):
        node.clear(key)
        assert key not in node.faults()
    assert node.faults() == [] and node.emcy.active() == []


def test_clear_base_name_clears_all_channels():
    _, node = make(model=IoModel())
    node.inject("wire_break", channel=1)
    node.inject("wire_break", channel=2)
    node.inject("short_circuit", channel=1)
    node.clear("wire_break")
    assert node.faults() == ["short_circuit:digital:1"]
    with pytest.raises(FaultError):
        node.clear("wire_break:digital:9")


def test_delayed_answer_due_while_muted_is_dropped():
    _, node = make()
    node.inject("sdo_delay", seconds=0.5)
    assert read(node, 0x1000, now=1.0) == []
    node.clear("sdo_delay")
    node.inject("mute")
    assert node.poll(2.0) == []
    node.clear("mute")
    assert [f for f in node.poll(3.0) if f.cob_id == SDO_TX] == []


def test_delayed_answer_due_while_stopped_is_dropped():
    _, node = make()
    node.inject("sdo_delay", seconds=0.5)
    assert read(node, 0x1000, now=1.0) == []
    node.clear("sdo_delay")
    node.handle(Frame(frames.NMT, bytes([2, ID])), 1.1)
    assert [f for f in node.poll(2.0) if f.cob_id == SDO_TX] == []
    node.handle(Frame(frames.NMT, bytes([0x80, ID])), 2.1)
    assert [f for f in node.poll(3.0) if f.cob_id == SDO_TX] == []
