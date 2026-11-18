"""Fault injection for a node.

:class:`FaultSet` holds the catalogue faults a node currently carries (same API as
enip-sim's). A fault with a finite ``count`` fires that many times, otherwise it stays
until cleared. :class:`NodeFaults` applies them to a node: it filters SDO traffic and
TPDO frames, owns the queue of delayed SDO answers and routes ``inject``/``clear`` to the
catalogue, the behaviour model and the EMCY producer. Pure logic with an explicit ``now``.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .frames import Frame
from .sdo import CS_ABORT, CS_BLOCK_DOWNLOAD, CS_BLOCK_UPLOAD, DOWNLOAD, UPLOAD, abort_frame

if TYPE_CHECKING:
    from .node import Node

CATALOG: dict[str, dict[str, Any]] = {
    # node
    "mute": {},                                  # the node sends nothing
    "no_heartbeat": {},                          # heartbeat frames are suppressed
    "restart": {},                               # one-shot: reset the node now
    "double_bootup": {},                         # one-shot: the next boot-up goes out twice
    # SDO
    "sdo_abort": {"code": 0x08000000, "index": None},   # requests answered with this abort
    "sdo_delay": {"seconds": 0.5},               # answers come late
    "sdo_drop": {},                              # a request gets no answer, state unchanged
    "sdo_bad_toggle": {},                        # a segment answer has the toggle inverted
    # PDO
    "pdo_dropout": {"every": 2, "pdo": None},    # every n-th TPDO frame is lost
    "pdo_bad_length": {"length": None, "pdo": None},  # TPDO frames are truncated
    "pdo_freeze": {"pdo": None},                 # TPDO keeps the data of the injection moment
    # application
    "emcy": {"code": 0xFF00, "register": 0x80, "data": ""},
}
_DEFAULT_COUNT = {"sdo_drop": 1, "sdo_bad_toggle": 1, "double_bootup": 1}


class FaultError(ValueError):
    """An unknown fault kind or a bad parameter."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_int(name: str, value: Any, low: int, high: int, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not _is_int(value) or not low <= value <= high:
        raise FaultError(f"{name} must be an integer in {low}..{high}")


def known_names(model: Any = None) -> list[str]:
    return sorted(set(CATALOG) | set(getattr(model, "FAULTS", ())))


class FaultSet:
    def __init__(self) -> None:
        self._faults: dict[str, dict[str, Any]] = {}

    @staticmethod
    def check(kind: str, count: int | None, params: dict[str, Any],
              known: list[str] | None = None) -> tuple[dict[str, Any], int | None]:
        defaults = CATALOG.get(kind)
        if defaults is None:
            raise FaultError(f"unknown fault {kind!r}; known: {', '.join(known or known_names())}")
        unknown = set(params) - set(defaults)
        if unknown:
            raise FaultError(f"{kind}: unknown parameter(s) {', '.join(sorted(unknown))}")
        full = {**defaults, **params}
        if kind == "sdo_abort":
            _check_int("sdo_abort code", full["code"], 0, 0xFFFFFFFF)
            _check_int("sdo_abort index", full["index"], 0, 0xFFFF, optional=True)
        elif kind == "sdo_delay":
            s = full["seconds"]
            if isinstance(s, bool) or not isinstance(s, (int, float)) \
                    or not math.isfinite(s) or s < 0:
                raise FaultError("sdo_delay seconds must be a non-negative number")
        elif kind == "pdo_dropout":
            _check_int("pdo_dropout every", full["every"], 2, 1 << 30)
        elif kind == "pdo_bad_length":
            _check_int("pdo_bad_length length", full["length"], 0, 8, optional=True)
        elif kind == "emcy":
            _check_int("emcy code", full["code"], 0, 0xFFFF)
            _check_int("emcy register", full["register"], 0, 0xFF)
            try:
                data = bytes.fromhex(full["data"])
            except (TypeError, ValueError):
                raise FaultError("emcy data must be a hex string") from None
            if len(data) > 5:
                raise FaultError("emcy data must be at most 5 bytes")
        if "pdo" in full:
            _check_int(f"{kind} pdo", full["pdo"], 1, 4, optional=True)
        remaining = count if count is not None else _DEFAULT_COUNT.get(kind)
        if remaining is not None and (not _is_int(remaining) or remaining <= 0):
            raise FaultError("count must be a positive integer")
        return full, remaining

    def inject(self, kind: str, *, count: int | None = None, **params: Any) -> None:
        full, remaining = self.check(kind, count, params)
        self._faults[kind] = {"params": full, "count": remaining}

    def peek(self, kind: str) -> dict[str, Any] | None:
        entry = self._faults.get(kind)
        return dict(entry["params"]) if entry else None

    def take(self, kind: str) -> dict[str, Any] | None:
        entry = self._faults.get(kind)
        if entry is None:
            return None
        if entry["count"] is not None:
            entry["count"] -= 1
            if entry["count"] <= 0:
                del self._faults[kind]
        return dict(entry["params"])

    def clear(self, kind: str | None = None) -> None:
        if kind is None:
            self._faults.clear()
        else:
            self._faults.pop(kind, None)

    def active(self) -> list[str]:
        return sorted(self._faults)


class NodeFaults:
    def __init__(self, node: Node) -> None:
        self.node = node
        self.fs = FaultSet()
        self._queue: list[tuple[float, bytes]] = []   # delayed SDO answers (due, data)
        self._frozen: dict[int, bytes] = {}           # TPDO number -> frozen data
        self._sent: dict[int, int] = {}               # TPDO number (0 = all) -> frames seen

    # -- inject / clear ---------------------------------------------------

    def _model_has(self, kind: str) -> bool:
        model = self.node.model
        return model is not None and kind in model.FAULTS

    def inject(self, kind: str, now: float | None, params: dict[str, Any]) -> list[Frame]:
        params = dict(params)
        if self._model_has(kind):
            if "count" in params:
                raise FaultError("count is only supported for catalogue faults")
            try:
                return self.node.model.inject(kind, now, **params)
            except ValueError as exc:
                raise FaultError(f"{kind}: {exc}") from exc
        count = params.pop("count", None)
        full, remaining = self.fs.check(kind, count, params, known_names(self.node.model))
        if kind == "restart":
            return self.node._reset(0.0 if now is None else now)
        out: list[Frame] = []
        if kind == "emcy":
            old = self.fs.peek("emcy")
            if old and old["code"] != full["code"]:
                out += self.node.emcy.clear(old["code"], now)
            out += self.node.emcy.raise_error(
                full["code"], full["register"], bytes.fromhex(full["data"]), now)
        elif kind == "pdo_freeze":
            self._freeze(full["pdo"])
        self.fs.inject(kind, count=count, **params)
        return out

    def clear(self, kind: str | None, now: float | None) -> list[Frame]:
        out: list[Frame] = []
        if kind is not None and self._model_has(kind):
            return self.node.model.clear(kind, now)
        if kind is not None and kind not in CATALOG:
            raise FaultError(
                f"unknown fault {kind!r}; known: {', '.join(known_names(self.node.model))}")
        if kind in (None, "emcy"):
            out += self.clear_emcy(now)
        if kind is None and self.node.model is not None:
            out += self.node.model.clear(None, now)
        if kind in (None, "pdo_freeze"):
            self._frozen.clear()
        if kind in (None, "pdo_dropout"):
            self._sent.clear()
        self.fs.clear(kind)
        return out

    def clear_emcy(self, now: float | None) -> list[Frame]:
        entry = self.fs.peek("emcy")
        if entry is None:
            return []
        self.fs.clear("emcy")
        return self.node.emcy.clear(entry["code"], now)

    def active(self) -> list[str]:
        model = self.node.model
        return self.fs.active() + (model.faults() if model is not None else [])

    def on_reset(self) -> None:
        """Node reset: the node forgets its injected EMCY and queued answers."""
        self.fs.clear("emcy")
        self._queue.clear()

    def muted(self) -> bool:
        return self.fs.peek("mute") is not None

    def suppress_heartbeat(self) -> bool:
        return self.fs.take("no_heartbeat") is not None

    # -- SDO --------------------------------------------------------------

    def _target(self, data: bytes) -> tuple[int, int]:
        """Index/sub a request refers to (segments refer to the running transfer)."""
        sdo = self.node.sdo
        cs = data[0] >> 5
        initiate = cs in (1, 2) or (cs == CS_BLOCK_UPLOAD and data[0] & 3 == 0) \
            or (cs == CS_BLOCK_DOWNLOAD and not data[0] & 1)
        if initiate:
            return data[1] | (data[2] << 8), data[3]
        return sdo.index, sdo.sub

    def sdo_request(self, data: bytes, now: float) -> list[bytes]:
        fs, sdo = self.fs, self.node.sdo
        if len(data) < 1 or data[0] >> 5 == CS_ABORT:
            return sdo.handle(data, now)
        data = bytes(data).ljust(8, b"\x00")[:8]
        if fs.take("sdo_drop") is not None:
            return []
        params = fs.peek("sdo_abort")
        if params is not None:
            index, sub = self._target(data)
            if params["index"] is None or params["index"] == index:
                fs.take("sdo_abort")
                return self.defer([abort_frame(index, sub, params["code"])], now)
        cs = data[0] >> 5
        segment = (cs == 3 and sdo.state == UPLOAD) or (cs == 0 and sdo.state == DOWNLOAD)
        answers = sdo.handle(data, now)
        if segment and answers and answers[0][0] != 0x80 \
                and fs.take("sdo_bad_toggle") is not None:
            answers = [bytes([answers[0][0] ^ 0x10]) + answers[0][1:]] + answers[1:]
        return self.defer(answers, now)

    def defer(self, answers: list[bytes], now: float) -> list[bytes]:
        """Queue answers behind earlier delayed ones; return those due now."""
        if answers and (self.fs.peek("sdo_delay") is not None or self._queue):
            delay = self.fs.take("sdo_delay")
            due = now + (delay["seconds"] if delay else 0.0)
            if self._queue:
                due = max(due, self._queue[-1][0])
            self._queue += [(due, a) for a in answers]
        elif answers:
            return answers
        out: list[bytes] = []
        while self._queue and self._queue[0][0] <= now + 1e-9:
            out.append(self._queue.pop(0)[1])
        return out

    # -- PDO --------------------------------------------------------------

    def _freeze(self, pdo: int | None) -> None:
        for t in self.node.pdo.tpdos:
            number = t.comm - 0x1800 + 1
            frame = self.node.pdo._build(t)
            if frame is not None and pdo in (None, number):
                self._frozen[number] = frame.data

    def tpdo(self, number: int, frame: Frame) -> Frame | None:
        """Apply PDO faults to a TPDO frame about to go out; None = lost."""
        fs = self.fs
        data = frame.data
        freeze = fs.peek("pdo_freeze")
        if freeze and freeze["pdo"] in (None, number):
            data = self._frozen.setdefault(number, data)
        bad = fs.peek("pdo_bad_length")
        if bad and bad["pdo"] in (None, number):
            fs.take("pdo_bad_length")
            length = bad["length"] if bad["length"] is not None else max(len(data) - 1, 0)
            data = data[:length]
        drop = fs.peek("pdo_dropout")
        if drop and drop["pdo"] in (None, number):
            key = 0 if drop["pdo"] is None else number
            self._sent[key] = self._sent.get(key, 0) + 1
            if self._sent[key] % drop["every"] == 0:
                fs.take("pdo_dropout")
                return None
        return Frame(frame.cob_id, data)
