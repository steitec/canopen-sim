"""Base class for behaviour models.

A model knows nothing of CANopen framing. It owns profile-area objects,
reads client-written values with ``od.get`` and publishes its own values
with ``od.set`` (which fires change listeners, so event-driven TPDOs
follow). Errors go out only through ``emcy.raise_error`` / ``emcy.clear``;
the EMCY frames are returned to the caller.
"""
from __future__ import annotations

from typing import Any

from ..emcy import Emcy
from ..frames import Frame
from ..od import ObjectDictionary, Variable


class Model:
    #: fault names this model knows
    FAULTS: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.od: ObjectDictionary | None = None
        self.emcy: Emcy | None = None
        # active faults: key -> EMCY code (or None for silent faults)
        self._active: dict[str, int | None] = {}
        self._bits: dict[str, int] = {}  # key -> error register bits

    def objects(self) -> list[Variable]:
        """Profile-area objects; add them to the OD before the Node is built."""
        return []

    def bind(self, od: ObjectDictionary, emcy: Emcy) -> None:
        self.od = od
        self.emcy = emcy

    def tick(self, dt: float, now: float) -> list[Frame]:
        return []

    def on_operational(self, now: float) -> list[Frame]:
        """The node entered NMT OPERATIONAL; return frames to send (no state change)."""
        return []

    def event_enabled(self, index: int, sub: int) -> bool:
        """False suppresses change-triggered TPDO sending for this object."""
        return True

    def inject(self, kind: str, now: float | None = None, **params: Any) -> list[Frame]:
        raise ValueError(self._unknown(kind))

    def clear(self, kind: str | None = None, now: float | None = None) -> list[Frame]:
        raise NotImplementedError

    def clear_key(self, key: str, now: float | None = None) -> list[Frame]:
        """Clear exactly the fault instance ``key`` as listed by :meth:`faults`."""
        return self.clear(key, now)

    def on_reset(self, now: float | None) -> list[Frame]:
        """The node's EMCY state was wiped by an NMT reset: re-raise every
        active fault so 0x1001/0x1003 and the EMCY frames agree again."""
        out: list[Frame] = []
        for key, code in list(self._active.items()):
            out += self._raise(key, code, self._bits.get(key, 0), now)
        return out

    def faults(self) -> list[str]:
        return list(self._active)

    def variables(self) -> dict[str, Any]:
        return {}

    # -- helpers for subclasses ------------------------------------------

    def _unknown(self, kind: str) -> str:
        return f"unknown fault {kind!r}; known faults: {', '.join(self.FAULTS)}"

    def _check_kind(self, kind: str) -> None:
        if kind not in self.FAULTS:
            raise ValueError(self._unknown(kind))

    def _raise(self, key: str, code: int | None, bits: int,
               now: float | None) -> list[Frame]:
        self._active[key] = code
        self._bits[key] = bits
        if code is None:
            return []
        assert self.emcy is not None, "model not bound"
        return self.emcy.raise_error(code, bits, now=now)

    def _clear_keys(self, keys: list[str], now: float | None) -> list[Frame]:
        """Drop faults; clear an EMCY code once no remaining fault uses it."""
        codes = [self._active.pop(k) for k in keys]
        for k in keys:
            self._bits.pop(k, None)
        out: list[Frame] = []
        for code in dict.fromkeys(c for c in codes if c is not None):
            if code not in self._active.values():
                assert self.emcy is not None, "model not bound"
                out += self.emcy.clear(code, now=now)
        return out
