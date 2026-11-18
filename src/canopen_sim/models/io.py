"""Generic I/O module: digital and analog channels with loopback and faults."""
from __future__ import annotations

from typing import Any

from ..emcy import CURRENT, MANUFACTURER, VOLTAGE
from ..frames import Frame
from ..od import DataType, Variable
from ..signals import Signal
from .base import Model

I16_MAX = 32767
I16_MIN = -32768

WIRE_BREAK_CODE = 0xFF01  # no CiA 401 code for a broken wire: manufacturer-specific
SHORT_CIRCUIT_CODE = 0x2320
OVER_RANGE_CODE = 0x3110


def _clip(value: float) -> int:
    return max(I16_MIN, min(I16_MAX, int(round(value))))


class IoModel(Model):
    FAULTS = ("wire_break", "short_circuit", "over_range")

    def __init__(self, digital_inputs: int = 8, digital_outputs: int = 8,
                 analog_inputs: int = 2, analog_outputs: int = 2,
                 loopback: bool = True,
                 signals: dict[str, Signal] | None = None) -> None:
        super().__init__()
        self.n_di, self.n_do = digital_inputs, digital_outputs
        self.n_ai, self.n_ao = analog_inputs, analog_outputs
        self.loopback = loopback
        #: keys "di<n>" / "ai<n>" (1-based channel) drive that input
        self.signals = dict(signals or {})
        self._last_now = 0.0
        # (kind, "digital"|"analog", channel) -> EMCY code, kept in _active by name
        self._faults: dict[str, tuple[str, str, int]] = {}

    # -- objects ----------------------------------------------------------

    @staticmethod
    def _groups(bits: int) -> int:
        return (bits + 7) // 8

    def objects(self) -> list[Variable]:
        out: list[Variable] = []

        def array(index: int, name: str, dtype: DataType, access: str, count: int,
                  mappable: bool) -> None:
            if count == 0:
                return
            out.append(Variable(index, 0, "Highest sub-index", DataType.UNSIGNED8,
                                "const", count))
            for sub in range(1, count + 1):
                out.append(Variable(index, sub, f"{name} {sub}", dtype, access, 0,
                                    pdo_mappable=mappable))

        array(0x6000, "Read digital input 8-bit", DataType.UNSIGNED8, "ro",
              self._groups(self.n_di), True)
        array(0x6200, "Write digital output 8-bit", DataType.UNSIGNED8, "rw",
              self._groups(self.n_do), True)
        array(0x6401, "Read analog input 16-bit", DataType.INTEGER16, "ro",
              self.n_ai, True)
        array(0x6411, "Write analog output 16-bit", DataType.INTEGER16, "rw",
              self.n_ao, True)
        return out

    # -- behaviour --------------------------------------------------------

    def tick(self, dt: float, now: float) -> list[Frame]:
        self._last_now = now
        self._update(now)
        return []

    def _stuck(self, kind: str, domain: str, channel: int) -> bool:
        return any(f[0] == kind and f[1] == domain and f[2] == channel
                   for f in self._faults.values())

    def _update(self, now: float) -> None:
        od = self.od
        assert od is not None, "model not bound"
        # digital inputs
        for g in range(self._groups(self.n_di)):
            value = 0
            if self.loopback and g < self._groups(self.n_do):
                value = od.get(0x6200, g + 1)
            for bit in range(8):
                ch = g * 8 + bit + 1
                if ch > self.n_di:
                    value &= ~(1 << bit)
                    continue
                sig = self.signals.get(f"di{ch}")
                if sig is not None:
                    on = sig.value(now) >= 0.5
                    value = (value | (1 << bit)) if on else (value & ~(1 << bit))
                if self._stuck("short_circuit", "digital", ch):
                    value |= 1 << bit
                if self._stuck("wire_break", "digital", ch):
                    value &= ~(1 << bit)
            od.set(0x6000, g + 1, value)
        # analog inputs
        for ch in range(1, self.n_ai + 1):
            sig = self.signals.get(f"ai{ch}")
            if sig is not None:
                value = _clip(sig.value(now))
            elif self.loopback and ch <= self.n_ao:
                value = od.get(0x6411, ch)
            else:
                value = 0
            if self._stuck("short_circuit", "analog", ch) or \
                    self._stuck("over_range", "analog", ch):
                value = I16_MAX
            if self._stuck("wire_break", "analog", ch):
                value = 0
            od.set(0x6401, ch, value)

    # -- faults -----------------------------------------------------------

    def inject(self, kind: str, now: float | None = None, **params: Any) -> list[Frame]:
        self._check_kind(kind)
        domain = params.pop("type", "analog" if kind == "over_range" else "digital")
        channel = params.pop("channel", 1)
        if params:
            raise ValueError(f"unknown parameters {sorted(params)}; "
                             "use channel=<n> and type=digital|analog")
        if domain not in ("digital", "analog"):
            raise ValueError("type must be 'digital' or 'analog'")
        if kind == "over_range" and domain != "analog":
            raise ValueError("over_range applies to analog inputs only")
        limit = self.n_di if domain == "digital" else self.n_ai
        if not isinstance(channel, int) or isinstance(channel, bool) \
                or not 1 <= channel <= limit:
            raise ValueError(f"channel must be 1..{limit} for {domain} inputs")
        key = f"{kind}:{domain}:{channel}"
        self._faults[key] = (kind, domain, channel)
        code, bits = {
            "wire_break": (WIRE_BREAK_CODE, MANUFACTURER),
            "short_circuit": (SHORT_CIRCUIT_CODE, CURRENT),
            "over_range": (OVER_RANGE_CODE, VOLTAGE),
        }[kind]
        frames = self._raise(key, code, bits, now)
        self._update(self._last_now if now is None else now)
        return frames

    def clear_key(self, key: str, now: float | None = None) -> list[Frame]:
        return self._clear_keys_now([key] if key in self._faults else [], now)

    def clear(self, kind: str | None = None, now: float | None = None) -> list[Frame]:
        if kind is not None:
            self._check_kind(kind)
        keys = [k for k, f in self._faults.items() if kind is None or f[0] == kind]
        return self._clear_keys_now(keys, now)

    def _clear_keys_now(self, keys: list[str], now: float | None) -> list[Frame]:
        for k in keys:
            del self._faults[k]
        frames = self._clear_keys(keys, now)
        if self.od is not None:
            self._update(self._last_now if now is None else now)
        return frames

    def variables(self) -> dict[str, Any]:
        return {"loopback": self.loopback, "faults": self.faults()}
