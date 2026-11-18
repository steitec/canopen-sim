import struct

import pytest

from canopen_sim.comm_objects import add_communication_objects
from canopen_sim.models.encoder import EncoderModel
from canopen_sim.node import Node
from canopen_sim.od import ObjectDictionary


def build(model):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020196, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="ENC")
    for v in model.objects():
        od.add(v)
    node = Node(1, od, "enc")
    model.bind(od, node.emcy)
    node.boot(0.0)
    return od, node


def test_encoder_objects():
    m = EncoderModel(resolution=1000)
    od, _ = build(m)
    assert od.get(0x6001, 0) == 1000
    assert od.entry(0x6004, 0).access == "ro" and od.entry(0x6004, 0).pdo_mappable
    assert od.entry(0x6030, 1).pdo_mappable and od.get(0x6030, 0) == 1
    assert od.entry(0x6000, 0).access == "rw"


def test_encoder_position_from_speed():
    m = EncoderModel(resolution=4096, speed_rpm=60.0)  # 1 rev/s
    od, _ = build(m)
    m.tick(0.5, 0.5)
    assert od.get(0x6004, 0) == 2048
    assert od.get(0x6030, 1) == 60
    m.tick(0.5, 1.0)
    assert od.get(0x6004, 0) == 4096  # multiturn: no wrap at one turn


def test_encoder_preset():
    m = EncoderModel()
    od, _ = build(m)
    od.sdo_write(0x6003, 0, struct.pack("<I", 12345))
    assert od.get(0x6004, 0) == 12345


def test_encoder_singleturn_wrap():
    m = EncoderModel(resolution=1000, multiturn=False, speed_rpm=60.0)
    od, _ = build(m)
    m.tick(1.25, 1.25)
    assert od.get(0x6004, 0) == 250


def test_encoder_direction_bit():
    m = EncoderModel(resolution=1000, multiturn=False, speed_rpm=60.0)
    od, _ = build(m)
    od.sdo_write(0x6000, 0, struct.pack("<H", 1))
    m.tick(0.25, 0.25)
    assert od.get(0x6004, 0) == 750
    assert od.get(0x6030, 1) == -60


def test_encoder_multiturn_wraps_at_2_32():
    m = EncoderModel(resolution=4096)
    od, _ = build(m)
    od.sdo_write(0x6003, 0, struct.pack("<I", 2**32 - 10))
    m.inject("position_jump", delta=20)
    assert od.get(0x6004, 0) == 10


def test_encoder_sensor_error_freezes_and_emcy():
    m = EncoderModel(resolution=1000, speed_rpm=60.0)
    od, node = build(m)
    m.tick(0.5, 0.5)
    out = m.inject("sensor_error", now=0.5)
    assert struct.unpack("<H", out[0].data[:2])[0] == 0x5051
    assert out[0].data[2] & 0x01
    m.tick(0.5, 1.0)
    assert od.get(0x6004, 0) == 500
    assert m.faults() == ["sensor_error"]
    out = m.clear("sensor_error", now=2.0)
    assert out[0].data[:2] == b"\0\0" and node.emcy.active() == []
    m.tick(0.5, 2.5)
    assert od.get(0x6004, 0) == 1000


def test_encoder_singleturn_sensor_code():
    m = EncoderModel(multiturn=False)
    build(m)
    out = m.inject("sensor_error", now=0.0)
    assert struct.unpack("<H", out[0].data[:2])[0] == 0x5050


def test_encoder_position_jump():
    m = EncoderModel(resolution=1000, speed_rpm=0.0)
    od, node = build(m)
    assert m.inject("position_jump", delta=-5) == []
    assert od.get(0x6004, 0) == 2**32 - 5
    assert node.emcy.active() == []


def test_encoder_freeze_silent():
    m = EncoderModel(resolution=1000, speed_rpm=60.0)
    od, node = build(m)
    assert m.inject("freeze") == []
    m.tick(0.5, 0.5)
    assert od.get(0x6004, 0) == 0 and node.emcy.active() == []
    assert m.clear("freeze") == []
    m.tick(0.5, 1.0)
    assert od.get(0x6004, 0) == 500


def test_encoder_unknown_fault():
    m = EncoderModel()
    build(m)
    with pytest.raises(ValueError, match="sensor_error"):
        m.inject("bogus")
    with pytest.raises(ValueError):
        m.clear("bogus")
    with pytest.raises(ValueError):
        m.inject("position_jump", delta="x")
    assert m.variables()["speed_rpm"] == 0.0
