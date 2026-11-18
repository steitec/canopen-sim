"""Rotary encoder: position integrated from a speed, with preset and faults."""
from __future__ import annotations

from typing import Any

from ..emcy import GENERIC
from ..frames import Frame
from ..od import DataType, Variable
from .base import Model

SINGLE_TURN_FAILURE = 0x5050
MULTI_TURN_FAILURE = 0x5051
_WRAP32 = 1 << 32


class EncoderModel(Model):
    FAULTS = ("sensor_error", "position_jump", "freeze")

    def __init__(self, resolution: int = 4096, multiturn: bool = True,
                 speed_rpm: float = 0.0) -> None:
        super().__init__()
        if resolution < 1:
            raise ValueError("resolution must be at least 1")
        self.resolution = resolution
        self.multiturn = multiturn
        self.speed_rpm = float(speed_rpm)
        self._pos = 0.0

    def objects(self) -> list[Variable]:
        return [
            Variable(0x6000, 0, "Operating parameters", DataType.UNSIGNED16, "rw", 0),
            Variable(0x6001, 0, "Measuring units per revolution", DataType.UNSIGNED32,
                     "rw", self.resolution, low=1),
            Variable(0x6003, 0, "Preset value", DataType.UNSIGNED32, "rw", 0),
            Variable(0x6004, 0, "Position value", DataType.UNSIGNED32, "ro", 0,
                     pdo_mappable=True),
            Variable(0x6030, 0, "Highest sub-index", DataType.UNSIGNED8, "const", 1),
            Variable(0x6030, 1, "Speed value", DataType.INTEGER16, "ro", 0,
                     pdo_mappable=True),
        ]

    def bind(self, od, emcy) -> None:
        super().bind(od, emcy)
        od.on_write(0x6003, 0, self._on_preset)

    # -- helpers ----------------------------------------------------------

    def _span(self) -> int:
        return _WRAP32 if self.multiturn else self._res()

    def _res(self) -> int:
        assert self.od is not None, "model not bound"
        return self.od.get(0x6001, 0)

    def _sign(self) -> int:
        assert self.od is not None, "model not bound"
        return -1 if self.od.get(0x6000, 0) & 1 else 1

    def _publish(self) -> None:
        assert self.od is not None
        self._pos %= self._span()
        self.od.set(0x6004, 0, int(self._pos))
        self.od.set(0x6030, 1, max(-32768, min(32767, round(self._sign() * self.speed_rpm))))

    def _on_preset(self, value: int) -> None:
        self._pos = float(value % self._span())
        self._publish()

    # -- behaviour --------------------------------------------------------

    def tick(self, dt: float, now: float) -> list[Frame]:
        frozen = any(k in self._active for k in ("sensor_error", "freeze"))
        if not frozen:
            self._pos += self._sign() * self.speed_rpm / 60.0 * dt * self._res()
        self._publish()
        return []

    def inject(self, kind: str, now: float | None = None, **params: Any) -> list[Frame]:
        self._check_kind(kind)
        if kind == "position_jump":
            delta = params.pop("delta", None)
            if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                raise ValueError("position_jump needs a numeric delta=<units>")
            if params:
                raise ValueError(f"unknown parameters {sorted(params)}")
            self._pos += delta
            self._publish()
            return []
        if params:
            raise ValueError(f"unknown parameters {sorted(params)}")
        if kind == "freeze":
            self._active["freeze"] = None
            return []
        code = MULTI_TURN_FAILURE if self.multiturn else SINGLE_TURN_FAILURE
        return self._raise("sensor_error", code, GENERIC, now)

    def clear(self, kind: str | None = None, now: float | None = None) -> list[Frame]:
        if kind == "position_jump":
            return []
        if kind is not None:
            self._check_kind(kind)
        keys = [k for k in self._active if kind is None or k == kind]
        return self._clear_keys(keys, now)

    def variables(self) -> dict[str, Any]:
        return {"speed_rpm": self.speed_rpm, "resolution": self.resolution,
                "multiturn": self.multiturn, "faults": self.faults()}
