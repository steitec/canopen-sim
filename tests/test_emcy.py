import struct

import pytest

from canopen_sim import frames
from canopen_sim.emcy import COMMUNICATION, CURRENT, GENERIC, MANUFACTURER, TEMPERATURE, VOLTAGE
from canopen_sim.frames import Frame
from canopen_sim.node import NmtState
from canopen_sim.od import NO_DATA, RANGE, SdoAbort
from .test_node import ID, make, nmt


def booted():
    od, node = make()
    node.boot(0.0)
    return od, node


def test_register_bits():
    assert (GENERIC, CURRENT, VOLTAGE, TEMPERATURE, COMMUNICATION, MANUFACTURER) == (
        0x01, 0x02, 0x04, 0x08, 0x10, 0x80)


def test_emcy_frame_layout():
    od, node = booted()
    out = node.emcy.raise_error(0x2310, CURRENT, b"\x01\x02", now=1.0)
    assert out == [Frame(frames.emcy(ID), struct.pack("<HB", 0x2310, 0x03) + b"\x01\x02\0\0\0")]
    assert od.get(0x1001, 0) == 0x03
    assert node.emcy.active() == [0x2310]
    assert node.emergency(0x3100, VOLTAGE)[0].data[:3] == struct.pack("<HB", 0x3100, 0x07)


def test_history_newest_first_max_8():
    od, node = booted()
    for i in range(10):
        node.emcy.raise_error(0x1000 + i, GENERIC, now=float(i))
    assert od.get(0x1003, 0) == 8
    assert [od.get(0x1003, s) for s in range(1, 9)] == [0x1009 - i for i in range(8)]


def test_clear_history_by_writing_zero():
    od, node = booted()
    node.emcy.raise_error(0x2310, CURRENT, now=0.0)
    node.emcy.raise_error(0x3100, VOLTAGE, now=0.0)
    od.sdo_write(0x1003, 0, b"\x00")
    assert od.get(0x1003, 0) == 0
    assert all(od.get(0x1003, s) == 0 for s in range(1, 9))
    assert od.get(0x1001, 0) == 0x07 and len(node.emcy.active()) == 2
    with pytest.raises(SdoAbort) as e:
        od.sdo_write(0x1003, 0, b"\x02")
    assert e.value.code == RANGE


def test_error_reset_frame_when_last_cleared():
    od, node = booted()
    node.emcy.raise_error(0x2310, CURRENT, now=0.0)
    node.emcy.raise_error(0x3100, VOLTAGE, now=0.0)
    # one of several cleared: code 0 with the remaining register
    assert node.emcy.clear(0x2310, now=1.0) == [
        Frame(frames.emcy(ID), struct.pack("<HB", 0, 0x05) + bytes(5))]
    assert od.get(0x1001, 0) == 0x05  # voltage + generic
    assert node.emcy.clear(now=2.0) == [Frame(frames.emcy(ID), bytes(8))]
    assert od.get(0x1001, 0) == 0 and node.emcy.active() == []
    assert node.emcy.clear(now=3.0) == []


def test_emcy_inhibit():
    od, node = booted()
    od.set(0x1015, 0, 100)  # 10 ms
    assert len(node.emcy.raise_error(0x1000, GENERIC, now=1.0)) == 1
    assert node.emcy.raise_error(0x2000, GENERIC, now=1.001) == []
    assert node.emcy.raise_error(0x3000, GENERIC, now=1.002) == []
    assert node.emcy.poll(1.005) == []
    out = node.emcy.poll(1.010)
    assert [struct.unpack("<H", f.data[:2])[0] for f in out] == [0x2000]
    assert [struct.unpack("<H", f.data[:2])[0] for f in node.poll(1.020)] == [0x3000]
    assert od.get(0x1015, 0) == 100


def test_not_sent_when_stopped_but_recorded():
    od, node = booted()
    node.handle(nmt(0x02), 1.0)
    assert node.state == NmtState.STOPPED
    assert node.emcy.raise_error(0x2310, CURRENT, now=1.0) == []
    assert od.get(0x1001, 0) == 0x03 and od.get(0x1003, 1) == 0x2310


def test_reset_clears_active_errors():
    od, node = booted()
    node.emcy.raise_error(0x2310, CURRENT, now=0.0)
    node.handle(nmt(0x82), 1.0)
    assert node.emcy.active() == [] and od.get(0x1001, 0) == 0


def test_emcy_cob_id_from_0x1014():
    od, node = booted()
    od.set(0x1014, 0, 0x0123)
    assert node.emcy.raise_error(0x2310, CURRENT, now=0.0)[0].cob_id == 0x123


def test_emcy_disabled_by_bit31():
    od, node = booted()
    od.set(0x1014, 0, 0x80000000 | frames.emcy(ID))
    assert node.emcy.raise_error(0x2310, CURRENT, now=0.0) == []
    assert node.emcy.clear(now=1.0) == []
    assert od.get(0x1003, 1) == 0x2310 and od.get(0x1001, 0) == 0


def test_history_entry_beyond_count_is_no_data():
    od, node = booted()
    for sub in range(1, 9):
        with pytest.raises(SdoAbort) as e:
            od.sdo_read(0x1003, sub)
        assert e.value.code == NO_DATA == 0x08000024
    node.emcy.raise_error(0x2310, CURRENT, now=0.0)
    assert od.sdo_read(0x1003, 1) == struct.pack("<I", 0x2310)
    with pytest.raises(SdoAbort) as e:
        od.sdo_read(0x1003, 2)
    assert e.value.code == NO_DATA
    assert od.sdo_read(0x1003, 0) == b"\x01"
