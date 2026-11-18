"""CANopen node protocol core: NMT, boot-up, heartbeat, SDO gating, store/restore.

Pure logic with an explicit ``now`` (seconds); no bus access and no threads.
"""
from __future__ import annotations

import copy
import struct
from enum import IntEnum

from . import frames
from .comm_objects import set_node_id
from .emcy import Emcy
from .faults import NodeFaults
from .frames import Frame
from .models.base import Model
from .od import CANNOT_STORE, ObjectDictionary, SdoAbort
from .pdo import PdoEngine
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
    def __init__(self, node_id: int, od: ObjectDictionary, name: str,
                 model: Model | None = None) -> None:
        self.node_id = node_id
        self.od = od
        self.name = name
        self.state = NmtState.BOOTUP
        self.sdo = SdoServer(od, node_id)
        if (0x1200, 1) in {(v.index, v.sub) for v in od.entries()}:
            set_node_id(od, node_id)
        if (0x1014, 0) in {(v.index, v.sub) for v in od.entries()}:
            od.set(0x1014, 0, frames.emcy(node_id))
        self._defaults = _snapshot(od)
        self.pdo = PdoEngine(self)
        self.emcy = Emcy(self)
        self.model = model
        if model is not None:
            model.bind(od, self.emcy)
        self._nf = NodeFaults(self)
        self._stored: dict[tuple[int, int], object] | None = None
        self._hb_next: float | None = None
        self._hb_restart = False
        od.on_write(0x1010, 1, self._on_store)
        od.on_write(0x1011, 1, self._on_restore)
        od.on_write(0x1017, 0, lambda _value: setattr(self, "_hb_restart", True))

    # -- public API -------------------------------------------------------

    def listen_ids(self) -> set[int]:
        return {frames.NMT, frames.SYNC, frames.sdo_rx(self.node_id)} | self.pdo.rpdo_cob_ids()

    def boot(self, now: float) -> list[Frame]:
        """Enter PRE_OPERATIONAL and emit the boot-up frame."""
        self.sdo.reset()
        self.state = NmtState.PRE_OPERATIONAL
        self._restart_heartbeat(now)
        self._nf.on_reset()
        if self._nf.muted():
            return []
        bootup = Frame(frames.heartbeat(self.node_id), bytes([NmtState.BOOTUP]))
        return [bootup, bootup] if self._nf.fs.take("double_bootup") is not None else [bootup]

    def handle(self, frame: Frame, now: float) -> list[Frame]:
        muted = self._nf.muted()
        if muted and frame.cob_id == frames.sdo_rx(self.node_id):
            return []  # requests are dropped without an answer
        out = self._dispatch(frame, now)
        return [] if muted else out

    def _dispatch(self, frame: Frame, now: float) -> list[Frame]:
        if frame.cob_id == frames.NMT:
            return self._handle_nmt(frame.data, now)
        if frame.cob_id == frames.sdo_rx(self.node_id) and self._sdo_active():
            out = [Frame(frames.sdo_tx(self.node_id), d) for d in self._nf.sdo_request(frame.data, now)]
            if self._hb_restart:
                self._restart_heartbeat(now)
            return out
        if frame.cob_id == frames.SYNC:
            return self.pdo.on_sync(now)
        if frame.cob_id in self.pdo.rpdo_cob_ids():
            return self.pdo.on_rpdo(frame, now)
        return []

    def emergency(self, code: int, register_bits: int, data: bytes = b"",
                  now: float | None = None) -> list[Frame]:
        """Raise an EMCY error (see :class:`canopen_sim.emcy.Emcy`)."""
        out = self.emcy.raise_error(code, register_bits, data, now)
        return [] if self._nf.muted() else out

    def tick(self, dt: float, now: float) -> list[Frame]:
        """Advance the behaviour model; returns its frames (none without a model)."""
        if self.model is None:
            return []
        out = self.model.tick(dt, now)
        return [] if self._nf.muted() else out

    def inject(self, kind: str, now: float | None = None, **params) -> list[Frame]:
        """Inject a fault (catalogue, model or ``emcy``); returns frames to send."""
        out = self._nf.inject(kind, now, params)
        return [] if self._nf.muted() else out

    def clear(self, kind: str | None = None, now: float | None = None) -> list[Frame]:
        """Clear one fault by name, or all faults when ``kind`` is None."""
        out = self._nf.clear(kind, now)
        return [] if self._nf.muted() else out

    def faults(self) -> list[str]:
        return self._nf.active()

    def _reset(self, now: float) -> list[Frame]:
        self._restore(lambda key: True)
        self.emcy.reset()
        return self._boot_with_faults(now)

    def _boot_with_faults(self, now: float) -> list[Frame]:
        """Boot-up first, then the EMCYs of faults a model still holds."""
        out = self.boot(now)
        if self.model is not None:
            extra = self.model.on_reset(now)
            if not self._nf.muted():
                out = out + extra
        return out

    def poll(self, now: float) -> list[Frame]:
        answers = self.sdo.poll(now) if self._sdo_active() else []
        due = self._nf.defer(answers, now)
        out: list[Frame] = [Frame(frames.sdo_tx(self.node_id), d) for d in due
                            if self._sdo_active()]  # not answered while STOPPED
        period = self.od.get(0x1017, 0) / 1000.0
        if period <= 0:
            self._hb_next = None  # 0x1017 disabled (also via od.set)
        elif self._hb_next is None:
            if self.state != NmtState.BOOTUP:  # enabled via od.set: start now
                self._hb_next = now + period
        elif now >= self._hb_next - 1e-9:
            if not self._nf.suppress_heartbeat():
                out.append(Frame(frames.heartbeat(self.node_id), bytes([self.state])))
            self._hb_next += period
            if self._hb_next <= now:  # fell far behind: do not burst
                self._hb_next = now + period
        out += self.emcy.poll(now)
        out += self.pdo.poll(now)
        return [] if self._nf.muted() else out

    # -- NMT --------------------------------------------------------------

    def _sdo_active(self) -> bool:
        return self.state in (NmtState.PRE_OPERATIONAL, NmtState.OPERATIONAL)

    def _handle_nmt(self, data: bytes, now: float) -> list[Frame]:
        if len(data) != 2 or data[1] not in (0, self.node_id):  # NMT DLC is 2
            return []
        cmd = data[0]
        if cmd == NMT_START:
            was = self.state
            self.state = NmtState.OPERATIONAL
            if was != NmtState.OPERATIONAL:
                out = self.pdo.on_operational(now)
                if self.model is not None:
                    out = out + self.model.on_operational(now)
                return out
        elif cmd == NMT_STOP:
            self.state = NmtState.STOPPED
            self.sdo.reset()
            self._nf.drop_queue()
        elif cmd == NMT_PRE_OP:
            self.state = NmtState.PRE_OPERATIONAL
        elif cmd == NMT_RESET_NODE:
            return self._reset(now)
        elif cmd == NMT_RESET_COMM:
            self._restore(lambda key: 0x1000 <= key[0] <= 0x1FFF)
            self.emcy.reset()
            return self._boot_with_faults(now)
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
