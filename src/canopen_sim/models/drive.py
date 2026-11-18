"""Generic servo drive: power state machine, profile velocity/position, homing.

The object set, the controlword commands and the statusword patterns follow
public manufacturer manuals (e.g. Kollmorgen AKD, SEW, Synapticon); no
member-only CiA document was used. Units are increments and increments/s.

Evaluation rules (deterministic, no randomness):

* Level-based controlword commands (shutdown, switch on, enable operation,
  disable voltage, quick stop) are evaluated immediately when 0x6040 is
  written (an ``on_write`` hook) and again every ``tick``.
* Edge-based bits (bit 4 new set-point / start homing, bit 7 fault reset) are
  evaluated in ``tick`` against the controlword seen by the previous tick.
* Not ready to switch on becomes Switch on disabled on the first tick;
  Fault reaction active lasts one tick before Fault; Quick stop active
  decelerates with 0x6084 and ends in Switch on disabled.
* Outside Operation enabled the drive free-runs: velocity ramps to zero with
  the deceleration 0x6084 (no load model), so a fault or shutdown never
  teleports the axis.
* The actual position lags the demand position by ``FOLLOWING_LAG`` seconds
  times the velocity; that lag is 0x60F4, so a small 0x6065 trips the
  following-error fault while moving.
"""
from __future__ import annotations

import math
from typing import Any

from ..emcy import CURRENT, MANUFACTURER, TEMPERATURE
from ..frames import Frame
from ..od import RANGE, DataType, SdoAbort, Variable
from .base import Model

OVER_TEMPERATURE_CODE = 0x4310
OVER_CURRENT_CODE = 0x2220
FOLLOWING_ERROR_CODE = 0x8611

MODE_PROFILE_POSITION = 1
MODE_PROFILE_VELOCITY = 3
MODE_HOMING = 6
SUPPORTED_MODES = (MODE_PROFILE_POSITION, MODE_PROFILE_VELOCITY, MODE_HOMING)
SUPPORTED_HOMING = (35, 37)  # both: current position is home
# 0x6502 supported drive modes: bit n-1 per mode n (PP bit 0, PV bit 2, homing bit 5)
SUPPORTED_MODES_BITS = sum(1 << (mode - 1) for mode in SUPPORTED_MODES)

NOT_READY, SWITCH_ON_DISABLED, READY, SWITCHED_ON = 0, 1, 2, 3
OPERATION_ENABLED, QUICK_STOP, FAULT_REACTION, FAULT = 4, 5, 6, 7
STATE_NAMES = ("not_ready_to_switch_on", "switch_on_disabled", "ready_to_switch_on",
               "switched_on", "operation_enabled", "quick_stop_active",
               "fault_reaction_active", "fault")
# statusword state pattern (bits 0-3, 5, 6) per state
_STATUS = (0x0000, 0x0040, 0x0021, 0x0023, 0x0027, 0x0007, 0x000F, 0x0008)

BIT_NEW_SETPOINT = 0x10
BIT_RELATIVE = 0x40
BIT_FAULT_RESET = 0x80
SW_REMOTE = 0x0200
SW_TARGET_REACHED = 0x0400
SW_SETPOINT_ACK = 0x1000  # PP: set-point acknowledge; homing: homing attained

FOLLOWING_LAG = 0.02  # s
VELOCITY_WINDOW = 10  # inc/s, target reached in profile velocity
SUBSTEP = 0.001  # s, internal integration step
_I32 = (-(1 << 31), (1 << 31) - 1)


class DriveModel(Model):
    FAULTS = ("over_temperature", "over_current", "following_error")
    _FAULT_INFO = {
        "over_temperature": (OVER_TEMPERATURE_CODE, TEMPERATURE),
        "over_current": (OVER_CURRENT_CODE, CURRENT),
        "following_error": (FOLLOWING_ERROR_CODE, MANUFACTURER),
    }

    def __init__(self, max_velocity: int = 3000, acceleration: int = 1000,
                 deceleration: int = 1000, following_error_window: int = 1000,
                 temperature_limit: float = 80.0) -> None:
        super().__init__()
        self.max_velocity = max_velocity
        self.acceleration = acceleration
        self.deceleration = deceleration
        self.following_error_window = following_error_window
        self.temperature_limit = temperature_limit
        self._state = NOT_READY
        self._pos = 0.0  # demand position
        self._vel = 0.0
        self._prev_cw = 0
        self._move_target: float | None = None  # active PP move
        self._ack = False  # PP set-point acknowledge / homing attained
        self._self_raised: set[str] = set()

    def objects(self) -> list[Variable]:
        U16, U32, I8, I32 = (DataType.UNSIGNED16, DataType.UNSIGNED32,
                             DataType.INTEGER8, DataType.INTEGER32)
        return [
            Variable(0x603F, 0, "Error code", U16, "ro", 0, pdo_mappable=True),
            Variable(0x6040, 0, "Controlword", U16, "rw", 0, pdo_mappable=True),
            Variable(0x6041, 0, "Statusword", U16, "ro", 0, pdo_mappable=True),
            Variable(0x6060, 0, "Modes of operation", I8, "rw",
                     MODE_PROFILE_VELOCITY, pdo_mappable=True),
            Variable(0x6061, 0, "Modes of operation display", I8, "ro",
                     MODE_PROFILE_VELOCITY, pdo_mappable=True),
            Variable(0x6064, 0, "Position actual value", I32, "ro", 0, pdo_mappable=True),
            Variable(0x6065, 0, "Following error window", U32, "rw",
                     self.following_error_window),
            Variable(0x606C, 0, "Velocity actual value", I32, "ro", 0, pdo_mappable=True),
            Variable(0x607A, 0, "Target position", I32, "rw", 0, pdo_mappable=True),
            Variable(0x6081, 0, "Profile velocity", U32, "rw", self.max_velocity),
            Variable(0x6083, 0, "Profile acceleration", U32, "rw", self.acceleration),
            Variable(0x6084, 0, "Profile deceleration", U32, "rw", self.deceleration),
            Variable(0x6098, 0, "Homing method", I8, "rw", SUPPORTED_HOMING[0]),
            Variable(0x60F4, 0, "Following error actual value", I32, "ro", 0),
            Variable(0x60FF, 0, "Target velocity", I32, "rw", 0, pdo_mappable=True),
            Variable(0x6502, 0, "Supported drive modes", U32, "ro", SUPPORTED_MODES_BITS),
        ]

    def bind(self, od, emcy) -> None:
        super().bind(od, emcy)
        od.on_write(0x6040, 0, self._on_controlword)
        od.on_write(0x6060, 0, self._on_mode)
        od.on_write(0x6098, 0, self._on_homing_method)
        self._publish()

    # -- write hooks ------------------------------------------------------

    def _on_mode(self, value: int) -> None:
        if value not in SUPPORTED_MODES:
            raise SdoAbort(RANGE)
        self._move_target = None
        self._ack = False

    def _on_homing_method(self, value: int) -> None:
        if value not in SUPPORTED_HOMING:
            raise SdoAbort(RANGE)

    def _on_controlword(self, value: int) -> None:
        self._apply_controlword(value)
        self._publish()

    # -- state machine ----------------------------------------------------

    def _set_state(self, state: int) -> None:
        if state != OPERATION_ENABLED:
            self._move_target = None
            self._ack = False
        self._state = state

    def _apply_controlword(self, cw: int) -> None:
        s = self._state
        if s in (NOT_READY, FAULT_REACTION, FAULT):
            return
        if not cw & 0x02:  # disable voltage
            if s != SWITCH_ON_DISABLED:
                self._set_state(SWITCH_ON_DISABLED)
        elif not cw & 0x04:  # quick stop
            if s == OPERATION_ENABLED:
                self._set_state(QUICK_STOP)
            elif s in (READY, SWITCHED_ON):
                self._set_state(SWITCH_ON_DISABLED)
        elif cw & 0x0F == 0x06:  # shutdown
            if s in (SWITCH_ON_DISABLED, SWITCHED_ON, OPERATION_ENABLED):
                self._set_state(READY)
        elif cw & 0x0F == 0x07:  # switch on
            if s in (READY, OPERATION_ENABLED):
                self._set_state(SWITCHED_ON)
        elif cw & 0x0F == 0x0F:  # enable operation
            if s == READY:  # switch on + enable operation in one step
                self._set_state(SWITCHED_ON)
                s = SWITCHED_ON
            if s == SWITCHED_ON:
                self._set_state(OPERATION_ENABLED)

    def _enter_fault(self, code: int) -> None:
        assert self.od is not None
        self.od.set(0x603F, 0, code)
        if self._state != FAULT:
            self._set_state(FAULT_REACTION)

    def _statusword(self) -> int:
        assert self.od is not None
        sw = _STATUS[self._state] | SW_REMOTE
        if self._state != OPERATION_ENABLED:
            return sw
        mode = self.od.get(0x6060, 0)
        if mode == MODE_PROFILE_VELOCITY:
            target = self._velocity_target()
            if abs(self._vel - target) <= VELOCITY_WINDOW:
                sw |= SW_TARGET_REACHED
        elif self._move_target is None and self._vel == 0:
            sw |= SW_TARGET_REACHED
        if mode != MODE_PROFILE_VELOCITY and self._ack:
            sw |= SW_SETPOINT_ACK
        return sw

    # -- motion -----------------------------------------------------------

    def _velocity_target(self) -> float:
        assert self.od is not None
        limit = self.max_velocity
        return float(max(-limit, min(limit, self.od.get(0x60FF, 0))))

    def _step(self, h: float) -> None:
        """One integration step of length ``h`` seconds."""
        assert self.od is not None
        acc = float(self.od.get(0x6083, 0))
        dec = float(self.od.get(0x6084, 0))
        mode = self.od.get(0x6060, 0)
        desired = 0.0
        if self._state == OPERATION_ENABLED:
            if mode == MODE_PROFILE_VELOCITY:
                desired = self._velocity_target()
            elif mode == MODE_PROFILE_POSITION and self._move_target is not None:
                remaining = self._move_target - self._pos
                vmax = min(float(self.od.get(0x6081, 0)), float(self.max_velocity))
                speed = min(vmax, math.sqrt(2.0 * dec * abs(remaining)))
                desired = math.copysign(speed, remaining)
        rate = acc if (desired * self._vel > 0 and abs(desired) > abs(self._vel)) \
            or (self._vel == 0 and desired != 0) else dec
        delta = desired - self._vel
        self._vel += max(-rate * h, min(rate * h, delta))
        self._vel = max(-self.max_velocity, min(self.max_velocity, self._vel))
        before = self._pos
        self._pos += self._vel * h
        target = self._move_target
        if target is not None:
            crossed = (before - target) * (self._pos - target) <= 0 and self._vel != 0
            if crossed or abs(target - self._pos) <= 0.5:
                self._pos = target
                self._vel = 0.0
                self._move_target = None

    def _integrate(self, dt: float) -> None:
        steps = max(1, math.ceil(dt / SUBSTEP - 1e-9)) if dt > 0 else 0
        for _ in range(steps):
            self._step(dt / steps)

    def _start_setpoint(self, cw: int) -> None:
        assert self.od is not None
        mode = self.od.get(0x6060, 0)
        if mode == MODE_PROFILE_POSITION:
            target = float(self.od.get(0x607A, 0))
            self._move_target = self._pos + target if cw & BIT_RELATIVE else target
            self._ack = True
        elif mode == MODE_HOMING:
            self._pos = 0.0
            self._vel = 0.0
            self._ack = True  # homing attained

    def _lag(self) -> int:
        return round(self._vel * FOLLOWING_LAG)

    def _publish(self) -> None:
        assert self.od is not None
        od = self.od
        err = self._lag()
        actual = max(_I32[0], min(_I32[1], round(self._pos) - err))
        od.set(0x6064, 0, actual)
        od.set(0x606C, 0, round(self._vel))
        od.set(0x60F4, 0, err)
        od.set(0x6061, 0, od.get(0x6060, 0))
        od.set(0x6041, 0, self._statusword())

    # -- behaviour --------------------------------------------------------

    def _add_fault(self, key: str, now: float | None, auto: bool = False) -> list[Frame]:
        code, bits = self._FAULT_INFO[key]
        if auto:
            self._self_raised.add(key)
        else:
            self._self_raised.discard(key)
        frames = self._raise(key, code, bits, now)
        self._enter_fault(code)
        return frames

    def tick(self, dt: float, now: float) -> list[Frame]:
        assert self.od is not None
        out: list[Frame] = []
        cw = self.od.get(0x6040, 0)
        if self._state == NOT_READY:
            self._state = SWITCH_ON_DISABLED
        elif self._state == FAULT_REACTION:
            self._state = FAULT
        elif self._state == FAULT:
            if cw & BIT_FAULT_RESET and not self._prev_cw & BIT_FAULT_RESET:
                out += self._try_reset(now)
        else:
            if self._state == OPERATION_ENABLED:
                if cw & BIT_NEW_SETPOINT and not self._prev_cw & BIT_NEW_SETPOINT:
                    self._start_setpoint(cw)
                elif not cw & BIT_NEW_SETPOINT and \
                        self.od.get(0x6060, 0) == MODE_PROFILE_POSITION:
                    self._ack = False
            self._apply_controlword(cw)
        self._integrate(dt)
        if self._state == QUICK_STOP and self._vel == 0:
            self._set_state(SWITCH_ON_DISABLED)
        if (self._state == OPERATION_ENABLED
                and self.od.get(0x6060, 0) == MODE_PROFILE_POSITION
                and abs(self._lag()) > self.od.get(0x6065, 0)):
            out += self._add_fault("following_error", now, auto=True)
        self._prev_cw = cw
        self._publish()
        return out

    def on_reset(self, now: float | None) -> list[Frame]:
        assert self.od is not None
        out = super().on_reset(now)
        if self._active:  # the drive stays in Fault; restore the error code
            last = next(reversed(self._active))
            self.od.set(0x603F, 0, self._FAULT_INFO[last][0])
        return out

    def _try_reset(self, now: float) -> list[Frame]:
        assert self.od is not None
        out = self._clear_keys(sorted(self._self_raised & set(self._active)), now)
        self._self_raised.clear()
        if not self._active:
            self.od.set(0x603F, 0, 0)
            self._set_state(SWITCH_ON_DISABLED)
        return out

    def inject(self, kind: str, now: float | None = None, **params: Any) -> list[Frame]:
        self._check_kind(kind)
        if params:
            raise ValueError(f"unknown parameters {sorted(params)}")
        frames = self._add_fault(kind, now)
        self._publish()
        return frames

    def clear(self, kind: str | None = None, now: float | None = None) -> list[Frame]:
        if kind is not None:
            self._check_kind(kind)
        keys = [k for k in self._active if kind is None or k == kind]
        self._self_raised.difference_update(keys)
        return self._clear_keys(keys, now)

    def variables(self) -> dict[str, Any]:
        return {"state": STATE_NAMES[self._state], "position": round(self._pos),
                "velocity": round(self._vel), "max_velocity": self.max_velocity,
                "temperature_limit": self.temperature_limit,
                "faults": self.faults()}
