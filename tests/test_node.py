import struct

import pytest

from canopen_sim import frames
from canopen_sim.comm_objects import add_communication_objects
from canopen_sim.frames import Frame
from canopen_sim.node import Node, NmtState
from canopen_sim.od import CANNOT_STORE, DataType, ObjectDictionary, Variable

ID = 5
SAVE = 0x65766173
LOAD = 0x64616F6C


def make(heartbeat_ms=0):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020192, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="SIM", heartbeat_ms=heartbeat_ms,
    )
    od.add(Variable(0x2000, 0, "u16", DataType.UNSIGNED16, "rw", 7))
    node = Node(ID, od, "n")
    return od, node


def nmt(cmd, target=ID):
    return Frame(frames.NMT, bytes([cmd, target]))


def sdo_write(index, sub, value, size=4):
    cmd = 0x23 | ((4 - size) << 2)
    data = bytes([cmd, index & 0xFF, index >> 8, sub]) + struct.pack("<I", value)
    data = data[:4 + size]
    return Frame(frames.sdo_rx(ID), data)


def sdo_read(index, sub):
    return Frame(frames.sdo_rx(ID), bytes([0x40, index & 0xFF, index >> 8, sub, 0, 0, 0, 0]))


def test_bootup_frame():
    _, node = make()
    assert node.state == NmtState.BOOTUP
    assert node.boot(0.0) == [Frame(0x705, b"\x00")]
    assert node.state == NmtState.PRE_OPERATIONAL


def test_listen_ids():
    _, node = make()
    assert {0x000, 0x080, 0x605} <= node.listen_ids()


def test_mandatory_objects():
    od, _ = make()
    assert od.get(0x1018, 0) == 4
    assert od.get(0x1014, 0) == 0x85
    assert od.get(0x1005, 0) == 0x80
    assert od.get(0x1200, 1) == 0x605 and od.get(0x1200, 2) == 0x585
    assert od.get(0x1003, 0) == 0 and od.get(0x1003, 8) == 0
    assert od.sdo_read(0x1010, 1) == b"\x01\x00\x00\x00"
    assert od.get(0x1008, 0) == "SIM"


def test_heartbeat_period_follows_1017():
    od, node = make()
    node.boot(0.0)
    assert node.poll(5.0) == []
    node.handle(sdo_write(0x1017, 0, 100, 2), 10.0)
    assert node.poll(10.05) == []
    assert node.poll(10.1) == [Frame(0x705, b"\x7f")]
    assert node.poll(10.15) == []
    assert node.poll(10.2) == [Frame(0x705, b"\x7f")]
    node.handle(nmt(0x01), 10.21)
    assert node.poll(10.3) == [Frame(0x705, b"\x05")]
    node.handle(sdo_write(0x1017, 0, 0, 2), 10.31)
    assert node.poll(20.0) == []


def test_heartbeat_default_starts_after_boot():
    _, node = make(heartbeat_ms=1000)
    node.boot(0.0)
    assert node.poll(0.5) == []
    assert node.poll(1.0) == [Frame(0x705, b"\x7f")]


def test_nmt_transitions():
    _, node = make()
    node.boot(0.0)
    node.handle(nmt(0x01), 0.1)
    assert node.state == NmtState.OPERATIONAL
    node.handle(nmt(0x02), 0.2)
    assert node.state == NmtState.STOPPED
    assert node.handle(sdo_read(0x1000, 0), 0.3) == []
    node.handle(nmt(0x80), 0.4)
    assert node.state == NmtState.PRE_OPERATIONAL
    out = node.handle(sdo_read(0x1000, 0), 0.5)
    assert out and out[0].cob_id == 0x585 and out[0].data[0] == 0x43


def test_nmt_ignores_short_and_unknown():
    _, node = make()
    node.boot(0.0)
    assert node.handle(Frame(frames.NMT, b"\x01"), 0.1) == []
    assert node.handle(nmt(0x55), 0.1) == []
    assert node.state == NmtState.PRE_OPERATIONAL


def test_nmt_broadcast_and_unknown_node():
    _, node = make()
    node.boot(0.0)
    node.handle(nmt(0x01, ID + 1), 0.1)
    assert node.state == NmtState.PRE_OPERATIONAL
    node.handle(Frame(frames.NMT, bytes([0x01, 0])), 0.2)
    assert node.state == NmtState.OPERATIONAL


def test_stop_resets_sdo_transfer():
    od, node = make()
    node.boot(0.0)
    node.handle(Frame(frames.sdo_rx(ID), bytes([0x21, 0x08, 0x10, 0, 3, 0, 0, 0])), 0.1)
    node.handle(nmt(0x02), 0.2)
    assert node.sdo.state == "idle"
    assert node.poll(5.0) == []


def test_reset_node_restores_defaults():
    od, node = make()
    node.boot(0.0)
    node.handle(nmt(0x01), 0.1)
    od.set(0x2000, 0, 99)
    od.set(0x1017, 0, 50)
    out = node.handle(nmt(0x81), 1.0)
    assert out == [Frame(0x705, b"\x00")]
    assert node.state == NmtState.PRE_OPERATIONAL
    assert od.get(0x2000, 0) == 7 and od.get(0x1017, 0) == 0


def test_reset_communication_only_touches_comm_area():
    od, node = make()
    node.boot(0.0)
    od.set(0x2000, 0, 99)
    od.set(0x1017, 0, 50)
    assert node.handle(nmt(0x82), 1.0) == [Frame(0x705, b"\x00")]
    assert od.get(0x2000, 0) == 99 and od.get(0x1017, 0) == 0


def test_store_survives_reset_node_and_restore_discards():
    od, node = make()
    node.boot(0.0)
    od.set(0x2000, 0, 42)
    out = node.handle(sdo_write(0x1010, 1, SAVE), 0.1)
    assert out[0].data[0] == 0x60
    assert od.sdo_read(0x1010, 1) == b"\x01\x00\x00\x00"
    od.set(0x2000, 0, 43)
    node.handle(nmt(0x81), 1.0)
    assert od.get(0x2000, 0) == 42
    node.handle(sdo_write(0x1011, 1, LOAD), 1.1)
    node.handle(nmt(0x81), 2.0)
    assert od.get(0x2000, 0) == 7


@pytest.mark.parametrize("index", [0x1010, 0x1011])
def test_wrong_signature_aborts(index):
    od, node = make()
    node.boot(0.0)
    out = node.handle(sdo_write(index, 1, 0x12345678), 0.1)
    assert out[0].data[0] == 0x80
    assert struct.unpack_from("<I", out[0].data, 4)[0] == CANNOT_STORE
    od.set(0x2000, 0, 1)
    node.handle(nmt(0x81), 1.0)
    assert od.get(0x2000, 0) == 7


def test_sdo_timeout_abort_is_framed():
    _, node = make()
    node.boot(0.0)
    node.handle(Frame(frames.sdo_rx(ID), bytes([0x21, 0x08, 0x10, 0, 3, 0, 0, 0])), 0.1)
    out = node.poll(5.0)
    assert len(out) == 1 and out[0].cob_id == 0x585 and out[0].data[0] == 0x80


def test_heartbeat_disabled_and_enabled_via_od_set():
    od, node = make(heartbeat_ms=100)
    node.boot(0.0)
    assert node.poll(0.1) == [Frame(0x705, b"\x7f")]
    od.set(0x1017, 0, 0)
    assert node.poll(0.2) == []
    assert node.poll(0.3) == []
    od.set(0x1017, 0, 100)
    assert node.poll(1.0) == []
    assert node.poll(1.1) == [Frame(0x705, b"\x7f")]


def test_nmt_with_wrong_dlc_ignored():
    """NMT commands are exactly two bytes; longer frames are garbage."""
    _, node = make()
    node.boot(0.0)
    assert node.handle(Frame(frames.NMT, bytes([0x01, ID, 0xFF])), 0.1) == []
    assert node.state == NmtState.PRE_OPERATIONAL
