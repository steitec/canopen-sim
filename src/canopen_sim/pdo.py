"""PDO engine: TPDO transmission types, SYNC handling, RPDO reception and
deadline monitoring. Pure logic with an explicit ``now`` (seconds)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .frames import Frame
from .od import DataType, SdoAbort, decode, encode

if TYPE_CHECKING:
    from .node import Node

INVALID = 0x80000000
EMCY_RPDO_LENGTH = 0x8210
EMCY_RPDO_TIMEOUT = 0x8250
REG_COMMUNICATION = 0x10
_SIGNED = (DataType.INTEGER8, DataType.INTEGER16, DataType.INTEGER32, DataType.INTEGER64)
_EVENT_TYPES = (254, 255)


@dataclass
class _Tpdo:
    comm: int
    mapping: int
    pending: bool = False
    sync_count: int = 0
    last_tx: float | None = None


@dataclass
class _Rpdo:
    comm: int
    mapping: int
    last_rx: float | None = None
    deadline_hit: bool = False
    held: bytes | None = None  # synchronous RPDO data waiting for SYNC


class PdoEngine:
    def __init__(self, node: Node) -> None:
        self.node = node
        self.od = node.od
        indexes = {v.index for v in self.od.entries()}
        self.tpdos = [_Tpdo(0x1800 + n, 0x1A00 + n) for n in range(512)
                      if 0x1800 + n in indexes and 0x1A00 + n in indexes]
        self.rpdos = [_Rpdo(0x1400 + n, 0x1600 + n) for n in range(512)
                      if 0x1400 + n in indexes and 0x1600 + n in indexes]
        self.od.on_change(self.value_changed)

    # -- helpers ----------------------------------------------------------

    def _active(self) -> bool:
        from .node import NmtState
        return self.node.state == NmtState.OPERATIONAL

    def _cob(self, comm: int) -> int | None:
        raw = self.od.get(comm, 1)
        return None if raw & INVALID else raw & 0x7FF

    def _entries(self, mapping: int) -> list[tuple[int, int, int]]:
        out = []
        for sub in range(1, self.od.get(mapping, 0) + 1):
            raw = self.od.get(mapping, sub)
            out.append((raw >> 16, (raw >> 8) & 0xFF, raw & 0xFF))
        return out

    def rpdo_cob_ids(self) -> set[int]:
        return {c for r in self.rpdos if (c := self._cob(r.comm)) is not None}

    # -- TPDO -------------------------------------------------------------

    def value_changed(self, index: int, sub: int) -> None:
        for t in self.tpdos:
            if self.od.get(t.comm, 2) in (0, *_EVENT_TYPES) and any(
                    e[:2] == (index, sub) for e in self._entries(t.mapping)):
                t.pending = True

    def _build(self, t: _Tpdo) -> Frame | None:
        cob = self._cob(t.comm)
        if cob is None:
            return None
        acc = pos = 0
        for index, sub, bits in self._entries(t.mapping):
            var = self.od.entry(index, sub)
            raw = int.from_bytes(encode(var.dtype, var.value), "little")
            acc |= (raw & ((1 << bits) - 1)) << pos
            pos += bits
        return Frame(cob, acc.to_bytes((pos + 7) // 8, "little"))

    def _send(self, t: _Tpdo, now: float) -> list[Frame]:
        t.pending = False
        t.last_tx = now
        frame = self._build(t)
        if frame is not None:
            frame = self.node._nf.tpdo(t.comm - 0x1800 + 1, frame)
        return [frame] if frame else []

    def on_operational(self, now: float) -> list[Frame]:
        """Called when entering OPERATIONAL: fresh timers, event TPDOs once."""
        out: list[Frame] = []
        for t in self.tpdos:
            t.pending, t.sync_count, t.last_tx = False, 0, now
            if self.od.get(t.comm, 2) in _EVENT_TYPES:
                out += self._send(t, now)
        for r in self.rpdos:
            r.last_rx, r.deadline_hit, r.held = now, False, None
        return out

    def on_sync(self, now: float) -> list[Frame]:
        if not self._active():
            return []
        out: list[Frame] = []
        for r in self.rpdos:
            if r.held is not None:
                data, r.held = r.held, None
                self._apply(r, data)
        for t in self.tpdos:
            ttype = self.od.get(t.comm, 2)
            if ttype == 0:
                if t.pending:
                    out += self._send(t, now)
            elif 1 <= ttype <= 240:
                t.sync_count += 1
                if t.sync_count >= ttype:
                    t.sync_count = 0
                    out += self._send(t, now)
        return out

    def poll(self, now: float) -> list[Frame]:
        if not self._active():
            return []
        out: list[Frame] = []
        for t in self.tpdos:
            if self.od.get(t.comm, 2) not in _EVENT_TYPES:
                continue
            last = t.last_tx if t.last_tx is not None else now
            inhibit = self.od.get(t.comm, 3) / 10000.0
            timer = self.od.get(t.comm, 5) / 1000.0
            if t.pending and now >= last + inhibit - 1e-9:
                out += self._send(t, now)
            elif timer > 0 and now >= last + timer - 1e-9:
                out += self._send(t, now)
        for r in self.rpdos:
            timer = self.od.get(r.comm, 5) / 1000.0
            if (timer > 0 and r.last_rx is not None and not r.deadline_hit
                    and now >= r.last_rx + timer - 1e-9):
                r.deadline_hit = True
                out += self.node.emergency(EMCY_RPDO_TIMEOUT, REG_COMMUNICATION, now=now)
        return out

    # -- RPDO -------------------------------------------------------------

    def on_rpdo(self, frame: Frame, now: float) -> list[Frame]:
        if not self._active():
            return []
        out: list[Frame] = []
        for r in self.rpdos:
            if self._cob(r.comm) != frame.cob_id:
                continue
            r.last_rx, r.deadline_hit = now, False
            entries = self._entries(r.mapping)
            need = (sum(e[2] for e in entries) + 7) // 8
            if len(frame.data) < need:
                out += self.node.emergency(EMCY_RPDO_LENGTH, REG_COMMUNICATION, now=now)
            elif self.od.get(r.comm, 2) <= 240:
                r.held = bytes(frame.data)
            else:
                self._apply(r, bytes(frame.data))
        return out

    def _apply(self, r: _Rpdo, data: bytes) -> None:
        acc = int.from_bytes(data, "little")
        pos = 0
        for index, sub, bits in self._entries(r.mapping):
            raw = (acc >> pos) & ((1 << bits) - 1)
            pos += bits
            try:
                var = self.od.entry(index, sub)
                self.od.pdo_write(index, sub, _convert(var.dtype, raw, bits))
            except (SdoAbort, ValueError, TypeError, OverflowError, struct.error):
                continue  # vetoed, out of range or malformed mapping: ignored


def _convert(dtype: DataType, raw: int, bits: int):
    if dtype in (DataType.VISIBLE_STRING, DataType.OCTET_STRING):
        return decode(dtype, raw.to_bytes((bits + 7) // 8, "little"))
    size = len(encode(dtype, 0))
    if dtype in _SIGNED and bits < size * 8 and raw >> (bits - 1):
        raw -= 1 << bits
    raw &= (1 << (size * 8)) - 1
    return decode(dtype, raw.to_bytes(size, "little"))
