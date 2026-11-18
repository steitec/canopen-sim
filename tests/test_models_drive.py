import struct

import pytest

from canopen_sim.comm_objects import add_communication_objects
from canopen_sim.models.drive import DriveModel
from canopen_sim.node import Node
from canopen_sim.od import RANGE, ObjectDictionary, SdoAbort


def build(model):
    od = ObjectDictionary()
    add_communication_objects(
        od, device_type=0x00020192, vendor_id=0, product_code=1, revision=2,
        serial=3, device_name="DRV")
    for v in model.objects():
        od.add(v)
    node = Node(1, od, "drv")
    model.bind(od, node.emcy)
    node.boot(0.0)
    return od, node


class Rig:
    """Drives a model with an explicit clock."""

    def __init__(self, **kw):
        self.m = DriveModel(**kw)
        self.od, self.node = build(self.m)
        self.t = 0.0
        self.tick(0.0)

    def tick(self, dt=0.01):
        self.t += dt
        return self.m.tick(dt, self.t)

    def run(self, seconds, dt=0.01):
        for _ in range(round(seconds / dt)):
            self.tick(dt)

    def w(self, index, sub, fmt, value):
        self.od.sdo_write(index, sub, struct.pack(fmt, value))

    def cw(self, value):
        self.w(0x6040, 0, "<H", value)

    def sw(self):
        return self.od.get(0x6041, 0)

    def enable(self, mode=3):
        self.w(0x6060, 0, "<b", mode)
        for c in (0x06, 0x07, 0x0F):
            self.cw(c)
            self.tick()
        assert self.sw() & 0x6F == 0x27


def test_objects():
    r = Rig()
    for idx in (0x603F, 0x6040, 0x6041, 0x6060, 0x6061, 0x6064, 0x606C, 0x607A, 0x60FF):
        assert r.od.entry(idx, 0).pdo_mappable
    for idx in (0x6065, 0x6081, 0x6083, 0x6084, 0x6098, 0x60F4):
        assert not r.od.entry(idx, 0).pdo_mappable
    assert r.od.entry(0x6041, 0).access == "ro"
    assert r.od.entry(0x6040, 0).access == "rw"
    assert r.od.get(0x6065, 0) == 1000


def test_power_up_to_switch_on_disabled():
    m = DriveModel()
    od, _ = build(m)
    assert od.get(0x6041, 0) & 0x4F == 0x00  # not ready to switch on
    m.tick(0.01, 0.01)
    assert od.get(0x6041, 0) & 0x6F == 0x40
    assert od.get(0x6041, 0) & 0x200  # remote


def test_enable_sequence_statuswords():
    r = Rig()
    r.cw(0x06)
    assert r.sw() & 0x6F == 0x21  # evaluated at once on write
    r.cw(0x07)
    assert r.sw() & 0x6F == 0x23
    r.cw(0x0F)
    assert r.sw() & 0x6F == 0x27
    r.cw(0x00)  # disable voltage
    assert r.sw() & 0x6F == 0x40


def test_wrong_command_order_ignored():
    r = Rig()
    r.cw(0x0F)  # enable from switch on disabled: no transition
    r.tick()
    assert r.sw() & 0x6F == 0x40


def test_profile_velocity_ramps():
    r = Rig()
    r.enable(3)
    r.w(0x6083, 0, "<I", 1000)
    r.w(0x60FF, 0, "<i", 1000)
    r.run(0.5)
    assert r.od.get(0x606C, 0) == pytest.approx(500, abs=15)
    assert not r.sw() & 0x400
    r.run(0.6)
    assert r.od.get(0x606C, 0) == 1000
    assert r.sw() & 0x400
    assert r.od.get(0x6064, 0) > 400
    r.w(0x60FF, 0, "<i", 9000)  # clamped at max_velocity
    r.run(3.0)
    assert r.od.get(0x606C, 0) == 3000
    r.w(0x60FF, 0, "<i", -500)
    r.run(5.0)
    assert r.od.get(0x606C, 0) == -500


def test_profile_position_reaches_target_and_sets_bit10():
    r = Rig()
    r.enable(1)
    r.w(0x607A, 0, "<i", 1000)
    r.w(0x6081, 0, "<I", 500)
    r.w(0x6083, 0, "<I", 1000)
    r.w(0x6084, 0, "<I", 1000)
    assert r.sw() & 0x400  # nothing to do yet
    r.cw(0x1F)
    r.tick()
    assert r.sw() & 0x1000 and not r.sw() & 0x400  # set-point acknowledged
    assert r.od.get(0x6061, 0) == 1
    r.run(0.5)
    assert 0 < r.od.get(0x6064, 0) < 1000
    r.run(4.0)
    assert r.od.get(0x6064, 0) == 1000
    assert r.od.get(0x606C, 0) == 0
    assert r.sw() & 0x400
    r.cw(0x0F)
    r.tick()
    assert not r.sw() & 0x1000


def test_relative_move():
    r = Rig()
    r.enable(1)
    r.w(0x607A, 0, "<i", 1000)
    r.cw(0x1F)
    r.run(5.0)
    assert r.od.get(0x6064, 0) == 1000
    r.cw(0x0F)
    r.tick()
    r.w(0x607A, 0, "<i", -300)
    r.cw(0x5F)  # bit 6 relative
    r.run(5.0)
    assert r.od.get(0x6064, 0) == 700
    assert r.sw() & 0x400


def test_homing_method_35():
    r = Rig()
    r.enable(1)
    r.w(0x607A, 0, "<i", 250)
    r.cw(0x1F)
    r.run(3.0)
    assert r.od.get(0x6064, 0) == 250
    r.cw(0x0F)
    r.w(0x6060, 0, "<b", 6)
    r.w(0x6098, 0, "<b", 35)
    r.tick()
    assert r.od.get(0x6061, 0) == 6
    assert not r.sw() & 0x1000
    r.cw(0x1F)
    r.tick()
    assert r.od.get(0x6064, 0) == 0
    assert r.sw() & 0x1000 and r.sw() & 0x400
    r.w(0x6098, 0, "<b", 37)
    r.cw(0x0F)
    r.tick()
    r.cw(0x1F)
    r.tick()
    assert r.od.get(0x6064, 0) == 0


def test_unsupported_mode_aborts():
    r = Rig()
    with pytest.raises(SdoAbort) as e:
        r.w(0x6060, 0, "<b", 8)
    assert e.value.code == RANGE
    assert r.od.get(0x6060, 0) == 3
    with pytest.raises(SdoAbort) as e:
        r.w(0x6098, 0, "<b", 1)
    assert e.value.code == RANGE
    assert r.od.get(0x6098, 0) == 35


def test_quick_stop_decelerates_then_disabled():
    r = Rig()
    r.enable(3)
    r.w(0x6083, 0, "<I", 5000)
    r.w(0x6084, 0, "<I", 1000)
    r.w(0x60FF, 0, "<i", 1000)
    r.run(1.0)
    assert r.od.get(0x606C, 0) == 1000
    r.cw(0x02)  # quick stop (bit 2 = 0)
    assert r.sw() & 0x6F == 0x07
    r.run(0.5)
    assert r.sw() & 0x6F == 0x07
    assert 0 < r.od.get(0x606C, 0) < 1000
    r.run(1.0)
    assert r.od.get(0x606C, 0) == 0
    assert r.sw() & 0x6F == 0x40


def test_quick_stop_from_switched_on_goes_disabled():
    r = Rig()
    r.cw(0x06)
    r.cw(0x07)
    r.cw(0x02)
    assert r.sw() & 0x6F == 0x40


def test_over_temperature_fault_and_reset():
    r = Rig()
    r.enable(3)
    frames = r.m.inject("over_temperature", now=r.t)
    assert frames and frames[0].data[:2] == struct.pack("<H", 0x4310)
    assert r.sw() & 0x4F == 0x0F  # fault reaction active (transient)
    assert r.od.get(0x603F, 0) == 0x4310
    assert r.m.faults() == ["over_temperature"]
    r.tick()
    assert r.sw() & 0x4F == 0x08
    r.cw(0x80)  # reset while the fault is still there
    r.tick()
    assert r.sw() & 0x4F == 0x08
    r.cw(0x00)
    r.tick()
    r.m.clear("over_temperature", now=r.t)
    r.tick()
    assert r.sw() & 0x4F == 0x08  # clearing alone does not leave Fault
    r.cw(0x80)
    r.tick()
    assert r.sw() & 0x6F == 0x40
    assert r.od.get(0x603F, 0) == 0
    assert r.m.faults() == []


def test_over_current_and_register_bits():
    r = Rig()
    r.m.inject("over_current", now=r.t)
    assert r.od.get(0x603F, 0) == 0x2220
    assert r.od.get(0x1001, 0) & 0x02
    r.m.clear(now=r.t)
    assert r.od.get(0x1001, 0) == 0


def test_following_error_triggers_itself():
    r = Rig()
    r.enable(1)
    r.w(0x6065, 0, "<I", 10)
    r.w(0x607A, 0, "<i", 5000)
    r.w(0x6081, 0, "<I", 2000)
    r.cw(0x1F)
    r.run(1.0)
    assert r.od.get(0x603F, 0) == 0x8611
    assert r.sw() & 0x4F == 0x08
    assert "following_error" in r.m.faults()
    assert r.od.get(0x606C, 0) < 2000  # stopped by the fault reaction
    # a reset clears the self-raised fault once the drive stands still
    r.run(3.0)
    r.cw(0x00)
    r.tick()
    r.cw(0x80)
    r.tick()
    assert r.sw() & 0x6F == 0x40
    assert r.m.faults() == []


def test_no_following_error_in_velocity_mode():
    r = Rig()
    r.w(0x6065, 0, "<I", 1)
    r.enable(3)
    r.w(0x60FF, 0, "<i", 2000)
    r.run(2.0)
    assert r.sw() & 0x6F == 0x27


def test_unknown_fault():
    r = Rig()
    with pytest.raises(ValueError, match="known faults"):
        r.m.inject("nope")
    with pytest.raises(ValueError, match="known faults"):
        r.m.clear("nope")


def test_consistent_with_canopen_state402_masks():
    """Independent reference: the canopen client's controlword/statusword values."""
    State402 = pytest.importorskip("canopen.profiles.p402").State402
    r = Rig()

    def state():
        sw = r.sw()
        return [n for n, (mask, bits) in State402.SW_MASK.items() if sw & mask == bits]

    assert state() == ["SWITCH ON DISABLED"]
    for cmd, name in ((State402.CW_SHUTDOWN, "READY TO SWITCH ON"),
                      (State402.CW_SWITCH_ON, "SWITCHED ON"),
                      (State402.CW_OPERATION_ENABLED, "OPERATION ENABLED"),
                      (State402.CW_QUICK_STOP, "QUICK STOP ACTIVE"),
                      (State402.CW_DISABLE_VOLTAGE, "SWITCH ON DISABLED")):
        r.cw(cmd)  # the write hook evaluates at once; no tick in between
        assert state() == [name], (hex(cmd), name)
    r.m.inject("over_current", now=r.t)
    assert state() == ["FAULT REACTION ACTIVE"]
    r.tick()
    assert state() == ["FAULT"]
    r.m.clear("over_current", now=r.t)
    r.cw(State402.CW_SWITCH_ON_DISABLED)
    r.tick()
    assert state() == ["SWITCH ON DISABLED"]


def test_supported_drive_modes_object():
    """Regression (canopen cross-check): BaseNode402.op_mode reads 0x6502 before
    switching; without it the client raised KeyError. Bits: PP 0, PV 2, homing 5."""
    r = Rig()
    var = r.od.entry(0x6502, 0)
    assert var.access == "ro" and not var.pdo_mappable
    assert r.od.get(0x6502, 0) == 0x25
    OperationMode = pytest.importorskip("canopen.profiles.p402").OperationMode
    for mode in ("PROFILED POSITION", "PROFILED VELOCITY", "HOMING"):
        bits = OperationMode.SUPPORTED[mode]
        assert r.od.get(0x6502, 0) & bits == bits
    assert not r.od.get(0x6502, 0) & OperationMode.SUPPORTED["PROFILED TORQUE"]


def test_switch_on_and_enable_from_ready():
    r = Rig()
    r.cw(0x06)
    r.tick()
    r.cw(0x0F)
    r.tick()
    assert r.sw() & 0x6F == 0x27


@pytest.mark.parametrize("cmd", [0x81, 0x82])
def test_fault_survives_nmt_reset_consistently(cmd):
    from canopen_sim.frames import Frame
    r = Rig()
    r.node.model = r.m
    r.enable(3)
    r.m.inject("over_temperature", now=r.t)
    r.tick()
    r.tick()
    out = r.node.handle(Frame(0x000, bytes([cmd, 1])), r.t)
    assert out[0].cob_id == 0x701 and out[0].data == b"\x00"
    assert [f.cob_id for f in out[1:]] == [0x081]
    assert out[1].data[:2] == struct.pack("<H", 0x4310)
    r.tick()
    assert r.sw() & 0x4F == 0x08  # still Fault
    assert r.od.get(0x603F, 0) == 0x4310
    assert r.node.emcy.active() == [0x4310]
    assert r.od.get(0x1001, 0) & 0x09 == 0x09
    cleared = r.m.clear("over_temperature", now=r.t + 1)
    assert cleared and cleared[0].data[:2] == b"\x00\x00"
