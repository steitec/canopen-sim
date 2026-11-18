import struct

import pytest

from canopen_sim.comm_objects import add_communication_objects
from canopen_sim.models.io import IoModel
from canopen_sim.node import Node
from canopen_sim.od import ObjectDictionary
from canopen_sim.signals import Signal, Source


def build(model):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020191, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="IO")
    for v in model.objects():
        od.add(v)
    node = Node(1, od, "io")
    model.bind(od, node.emcy)
    node.boot(0.0)
    return od, node


def codes(frames):
    return [struct.unpack("<H", f.data[:2])[0] for f in frames]


def test_io_objects():
    m = IoModel()
    od, _ = build(m)
    assert od.get(0x6000, 0) == 1 and od.get(0x6401, 0) == 2
    assert od.entry(0x6000, 1).access == "ro" and od.entry(0x6000, 1).pdo_mappable
    assert od.entry(0x6200, 1).access == "rw"
    assert od.entry(0x6411, 2).access == "rw" and od.entry(0x6401, 2).access == "ro"


def test_io_loopback():
    m = IoModel()
    od, _ = build(m)
    od.set(0x6200, 1, 0xA5)
    od.set(0x6411, 2, -1234)
    assert m.tick(0.1, 1.0) == []
    assert od.get(0x6000, 1) == 0xA5
    assert od.get(0x6401, 2) == -1234


def test_io_signal_drives_analog_input():
    sig = Signal([Source("constant", {"value": 1000.0})], start=0.0)
    m = IoModel(signals={"ai1": sig})
    od, _ = build(m)
    od.set(0x6411, 1, 55)
    m.tick(0.1, 1.0)
    assert od.get(0x6401, 1) == 1000
    od.set(0x6411, 2, 77)
    m.tick(0.1, 2.0)
    assert od.get(0x6401, 2) == 77  # not driven: loopback


def test_io_wire_break_emcy_and_clear():
    m = IoModel()
    od, node = build(m)
    od.set(0x6200, 1, 0xFF)
    m.tick(0.1, 1.0)
    out = m.inject("wire_break", now=1.0, channel=3)
    assert codes(out) == [0xFF01]
    assert out[0].data[2] == 0x81
    m.tick(0.1, 1.1)
    assert od.get(0x6000, 1) == 0xFF & ~(1 << 2)
    assert m.faults() == ["wire_break:digital:3"]
    out = m.clear("wire_break", now=2.0)
    assert codes(out) == [0]
    m.tick(0.1, 2.1)
    assert od.get(0x6000, 1) == 0xFF
    assert m.faults() == [] and node.emcy.active() == []


def test_io_wire_break_analog_reads_zero():
    m = IoModel()
    od, _ = build(m)
    od.set(0x6411, 1, 500)
    m.inject("wire_break", now=0.0, channel=1, type="analog")
    m.tick(0.1, 0.1)
    assert od.get(0x6401, 1) == 0


def test_io_short_circuit():
    m = IoModel()
    od, _ = build(m)
    out = m.inject("short_circuit", now=0.0, channel=2)
    assert codes(out) == [0x2320]
    m.tick(0.1, 0.1)
    assert od.get(0x6000, 1) == 0x02
    m.inject("short_circuit", now=0.2, channel=1, type="analog")
    m.tick(0.1, 0.3)
    assert od.get(0x6401, 1) == 32767


def test_io_over_range_and_clear_all():
    m = IoModel()
    od, node = build(m)
    out = m.inject("over_range", now=0.0, channel=2)
    assert codes(out) == [0x3110] and out[0].data[2] == 0x05
    m.tick(0.1, 0.1)
    assert od.get(0x6401, 2) == 32767
    m.inject("wire_break", now=0.2, channel=1)
    out = m.clear(None, now=1.0)
    assert codes(out) == [0, 0] or codes(out) == [0]
    assert node.emcy.active() == [] and m.faults() == []


def test_io_bad_inputs():
    m = IoModel()
    build(m)
    with pytest.raises(ValueError, match="wire_break"):
        m.inject("nope")
    with pytest.raises(ValueError):
        m.inject("wire_break", channel=99)
    with pytest.raises(ValueError):
        m.clear("nope")
    with pytest.raises(ValueError):
        m.inject("over_range", channel=1, type="digital")


def test_io_fault_survives_reset_communication():
    from canopen_sim.frames import Frame
    m = IoModel()
    od, node = build(m)
    node.model = m
    m.inject("wire_break", now=1.0, channel=3)
    out = node.handle(Frame(0x000, bytes([0x82, 1])), 2.0)
    assert out[0].cob_id == 0x701 and out[0].data == b"\x00"
    assert codes(out[1:]) == [0xFF01] and out[1].cob_id == 0x081
    assert out[1].data[2] == 0x81
    assert node.emcy.active() == [0xFF01]
    assert od.get(0x1001, 0) & 0x81 == 0x81
    assert codes(m.clear("wire_break", now=3.0)) == [0]
    assert node.emcy.active() == []


def build_pdo(model):
    from canopen_sim.comm_objects import PdoSpec, add_pdo_objects
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020191, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="IO", heartbeat_ms=0)
    for v in model.objects():
        od.add(v)
    add_pdo_objects(od, 1, tpdos=[
        PdoSpec(0, [(0x6000, 1, 8)], 255),
        PdoSpec(1, [(0x6401, 1, 16)], 255)], rpdos=[])
    node = Node(1, od, "io", model=model)
    node.boot(0.0)
    return od, node


def start(node, now=0.0):
    from canopen_sim import frames
    return node.handle(frames.Frame(frames.NMT, bytes([1, 1])), now)


def test_analog_interrupt_object():
    od, _ = build(IoModel())
    var = od.entry(0x6423, 0)
    assert var.access == "rw" and var.pdo_mappable and var.dtype.name == "BOOLEAN"
    assert od.get(0x6423, 0) is False or od.get(0x6423, 0) == 0
    with pytest.raises(Exception):
        build(IoModel(analog_inputs=0))[0].entry(0x6423, 0)


def test_analog_changes_gated_by_interrupt_enable():
    m = IoModel()
    od, node = build_pdo(m)
    start(node, 0.0)
    node.poll(0.1)  # initial event TPDOs already sent on entering OPERATIONAL
    od.set(0x6411, 1, 500)
    m.tick(0.1, 1.0)
    assert [f.cob_id for f in node.poll(1.0)] == []  # 0x6423 FALSE: no TPDO2
    od.set(0x6200, 1, 0x01)  # digital input change still goes out
    m.tick(0.1, 1.1)
    assert [f.cob_id for f in node.poll(1.1)] == [0x181]
    od.sdo_write(0x6423, 0, b"\x01")
    od.set(0x6411, 1, 600)
    m.tick(0.1, 2.0)
    out = node.poll(2.0)
    assert [f.cob_id for f in out] == [0x281] and out[0].data == struct.pack("<h", 600)


def test_warning_on_operational_with_interrupt_disabled():
    m = IoModel()
    od, node = build_pdo(m)
    out = start(node, 0.0)
    emcys = [f for f in out if f.cob_id == 0x81]
    assert codes(emcys) == [0x0080] and emcys[0].data[2] == 0x01
    assert node.state.name == "OPERATIONAL"
    assert od.get(0x1001, 0) == 0x01
    # set TRUE: warning cleared by EMCY 0x0000
    od.sdo_write(0x6423, 0, b"\x01")
    cleared = [f for f in m.tick(0.1, 1.0) if f.cob_id == 0x81]
    assert codes(cleared) == [0x0000] and od.get(0x1001, 0) == 0


def test_no_warning_when_interrupt_enabled():
    m = IoModel()
    od, node = build_pdo(m)
    od.sdo_write(0x6423, 0, b"\x01")
    assert [f for f in start(node, 0.0) if f.cob_id == 0x81] == []
