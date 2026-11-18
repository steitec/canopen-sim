"""EMCY producer: error register (0x1001), error history (0x1003), inhibit (0x1015).

Pure logic with an explicit ``now``; frames are returned, never sent here.
"""
from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from . import frames
from .frames import Frame
from .od import RANGE, SdoAbort

if TYPE_CHECKING:
    from .node import Node

GENERIC = 0x01
CURRENT = 0x02
VOLTAGE = 0x04
TEMPERATURE = 0x08
COMMUNICATION = 0x10
MANUFACTURER = 0x80

HISTORY_SIZE = 8


class Emcy:
    def __init__(self, node: "Node") -> None:
        self.node = node
        self._active: list[tuple[int, int]] = []  # (code, register bits), oldest first
        self._queue: list[Frame] = []
        self._last_sent: float | None = None
        node.od.on_write(0x1003, 0, self._on_history_write)

    # -- public API -------------------------------------------------------

    def raise_error(self, code: int, register_bits: int, data: bytes = b"",
                    now: float | None = None) -> list[Frame]:
        """Record an error and return the EMCY frames that may go out now."""
        code &= 0xFFFF
        self._active = [e for e in self._active if e[0] != code]
        self._active.append((code, register_bits & 0xFF))
        register = self._update_register()
        self._push_history(code)
        payload = struct.pack("<HB", code, register) + bytes(data[:5]).ljust(5, b"\0")
        return self._send(Frame(frames.emcy(self.node.node_id), payload), now)

    def clear(self, code: int | None = None, now: float | None = None) -> list[Frame]:
        """Clear one error (or all). The reset frame goes out when the last
        active error is gone; clearing one of several only updates 0x1001."""
        if not self._active:
            return []
        if code is None:
            self._active = []
        else:
            remaining = [e for e in self._active if e[0] != code & 0xFFFF]
            if len(remaining) == len(self._active):
                return []
            self._active = remaining
        self._update_register()
        if self._active:
            return []
        return self._send(Frame(frames.emcy(self.node.node_id), bytes(8)), now)

    def active(self) -> list[int]:
        return [code for code, _ in self._active]

    def poll(self, now: float) -> list[Frame]:
        """Release queued frames whose inhibit time has passed, in order."""
        out: list[Frame] = []
        while self._queue and self._may_send(now):
            out.append(self._queue.pop(0))
            self._last_sent = now
        return out

    def reset(self) -> None:
        """Node reset: forget active errors and queued frames (history is
        restored by the node's normal reset rules)."""
        self._active = []
        self._queue = []
        self._last_sent = None
        self.node.od.set(0x1001, 0, 0)

    # -- internals --------------------------------------------------------

    def _update_register(self) -> int:
        register = 0
        for _, bits in self._active:
            register |= bits
        if self._active:
            register |= GENERIC
        self.node.od.set(0x1001, 0, register)
        return register

    def _push_history(self, code: int) -> None:
        od = self.node.od
        entries = [code] + [od.get(0x1003, s) for s in range(1, od.get(0x1003, 0) + 1)]
        entries = entries[:HISTORY_SIZE]
        for sub in range(1, HISTORY_SIZE + 1):
            od.set(0x1003, sub, entries[sub - 1] if sub <= len(entries) else 0)
        od.set(0x1003, 0, len(entries))

    def _on_history_write(self, value: int) -> None:
        if value != 0:
            raise SdoAbort(RANGE)
        for sub in range(1, HISTORY_SIZE + 1):
            self.node.od.set(0x1003, sub, 0)

    def _can_transmit(self) -> bool:
        from .node import NmtState
        return self.node.state in (NmtState.PRE_OPERATIONAL, NmtState.OPERATIONAL)

    def _may_send(self, now: float | None) -> bool:
        if not self._can_transmit():
            return False
        if now is None or self._last_sent is None:
            return True
        inhibit = self.node.od.get(0x1015, 0) * 1e-4
        return now >= self._last_sent + inhibit - 1e-9

    def _send(self, frame: Frame, now: float | None) -> list[Frame]:
        if not self._can_transmit():
            return []
        if self._queue or not self._may_send(now):
            self._queue.append(frame)
            return []
        if now is not None:
            self._last_sent = now
        return [frame]
