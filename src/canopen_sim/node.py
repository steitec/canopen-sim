"""CANopen node protocol core: NMT, boot-up, heartbeat, SDO gating, store/restore.

Pure logic with an explicit ``now`` (seconds); no bus access and no threads.
"""
from __future__ import annotations

import copy
from enum import IntEnum

from . import frames
from .comm_objects import set_node_id
from .frames import Frame
from .od import CANNOT_STORE, ObjectDictionary, SdoAbort
from .sdo import SdoServer

SAVE_SIGNATURE = 0x65766173  # "save"
LOAD_SIGNATURE = 0x64616F6C  # "load"

NMT_START, NMT_STOP, NMT_PRE_OP = 0x01, 0x02, 0x80
NMT_RESET_NODE, NMT_RESET_COMM = 0x81, 0x82


class NmtState(IntEnum):
    BOOTUP = 0x00
    STOPPED = 0x04
    OPERATIONAL = 0x05
    PRE_OPERATIONAL = 0x7F


def _snapshot(od: ObjectDictionary) -> dict[tuple[int, int], object]:
    return {(v.index, v.sub): copy.deepcopy(v.value) for v in od.entries()}


class Node:
    def __init__(self, node_id: int, od: ObjectDictionary, name: str) -> None:
        self.node_id = node_id
        self.od = od
        self.name = name
        self.state = NmtState.BOOTUP
        self.sdo = SdoServer(od, node_id)
        if (0x1200, 1) in {(v.index, v.sub) for v in od.entries()}:
            set_node_id(od, node_id)
        self._defaults = _snapshot(od)
        self._stored: dict[tuple[int, int], object] | None = None
        self._hb_next: float | None = None
        self._hb_restart = False
        od.on_write(0x1010, 1, self._on_store)
        od.on_write(0x1011, 1, self._on_restore)
        od.on_write(0x1017, 0, lambda _value: setattr(self, "_hb_restart", True))

    # -- public API -------------------------------------------------------

    def listen_ids(self) -> set[int]:
        return {frames.NMT, frames.SYNC, frames.sdo_rx(self.node_id)}

    def boot(self, now: float) -> list[Frame]:
        """Enter PRE_OPERATIONAL and emit the boot-up frame."""
        self.sdo.reset()
        self.state = NmtState.PRE_OPERATIONAL
        self._restart_heartbeat(now)
        return [Frame(frames.heartbeat(self.node_id), bytes([NmtState.BOOTUP]))]

    def handle(self, frame: Frame, now: float) -> list[Frame]:
        if frame.cob_id == frames.NMT:
            return self._handle_nmt(frame.data, now)
        if frame.cob_id == frames.sdo_rx(self.node_id) and self._sdo_active():
            out = [Frame(frames.sdo_tx(self.node_id), d) for d in self.sdo.handle(frame.data, now)]
            if self._hb_restart:
                self._restart_heartbeat(now)
            return out
        return []

    def poll(self, now: float) -> list[Frame]:
        out: list[Frame] = []
        if self._sdo_active():
            out += [Frame(frames.sdo_tx(self.node_id), d) for d in self.sdo.poll(now)]
        if self._hb_next is not None and now >= self._hb_next - 1e-9:
            period = self.od.get(0x1017, 0) / 1000.0
            out.append(Frame(frames.heartbeat(self.node_id), bytes([self.state])))
            self._hb_next += period
            if self._hb_next <= now:  # fell far behind: do not burst
                self._hb_next = now + period
        return out

    # -- NMT --------------------------------------------------------------

    def _sdo_active(self) -> bool:
        return self.state in (NmtState.PRE_OPERATIONAL, NmtState.OPERATIONAL)

    def _handle_nmt(self, data: bytes, now: float) -> list[Frame]:
        if len(data) < 2 or data[1] not in (0, self.node_id):
            return []
        cmd = data[0]
        if cmd == NMT_START:
            self.state = NmtState.OPERATIONAL
        elif cmd == NMT_STOP:
            self.state = NmtState.STOPPED
            self.sdo.reset()
        elif cmd == NMT_PRE_OP:
            self.state = NmtState.PRE_OPERATIONAL
        elif cmd == NMT_RESET_NODE:
            self._restore(lambda key: True)
            return self.boot(now)
        elif cmd == NMT_RESET_COMM:
            self._restore(lambda key: 0x1000 <= key[0] <= 0x1FFF)
            return self.boot(now)
        return []

    def _restore(self, selected) -> None:
        source = self._stored if self._stored is not None else self._defaults
        for key, value in source.items():
            if selected(key):
                self.od.set(key[0], key[1], copy.deepcopy(value))

    # -- heartbeat --------------------------------------------------------

    def _restart_heartbeat(self, now: float) -> None:
        self._hb_restart = False
        try:
            period = self.od.get(0x1017, 0)
        except SdoAbort:
            period = 0
        self._hb_next = now + period / 1000.0 if period > 0 else None

    # -- store / restore --------------------------------------------------

    def _on_store(self, value: int) -> None:
        if value != SAVE_SIGNATURE:
            raise SdoAbort(CANNOT_STORE)
        self._stored = _snapshot(self.od)

    def _on_restore(self, value: int) -> None:
        if value != LOAD_SIGNATURE:
            raise SdoAbort(CANNOT_STORE)
        self._stored = None
