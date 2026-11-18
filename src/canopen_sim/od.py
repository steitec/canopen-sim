"""Object dictionary, CANopen data types and SDO abort codes."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Iterable

# SDO abort codes
TOGGLE = 0x05030000
TIMEOUT = 0x05040000
BAD_COMMAND = 0x05040001
BAD_BLOCK_SIZE = 0x05040002
BAD_SEQUENCE = 0x05040003
CRC_ERROR = 0x05040004
UNSUPPORTED = 0x06010000
WRITE_ONLY = 0x06010001
READ_ONLY = 0x06010002
NO_OBJECT = 0x06020000
NOT_MAPPABLE = 0x06040041
PDO_LENGTH = 0x06040042
INCOMPATIBLE = 0x06040043
LENGTH_MISMATCH = 0x06070010
TOO_LONG = 0x06070012
TOO_SHORT = 0x06070013
NO_SUBINDEX = 0x06090011
RANGE = 0x06090030
TOO_HIGH = 0x06090031
TOO_LOW = 0x06090032
GENERAL = 0x08000000
CANNOT_STORE = 0x08000020
DEVICE_STATE = 0x08000022


class SdoAbort(Exception):
    """Raised to abort an SDO transfer with a 32-bit abort code."""

    def __init__(self, code: int):
        super().__init__(f"SDO abort 0x{code:08X}")
        self.code = code


class DataType(IntEnum):
    BOOLEAN = 1
    INTEGER8 = 2
    INTEGER16 = 3
    INTEGER32 = 4
    UNSIGNED8 = 5
    UNSIGNED16 = 6
    UNSIGNED32 = 7
    REAL32 = 8
    VISIBLE_STRING = 9
    OCTET_STRING = 0x0A
    INTEGER64 = 0x15
    UNSIGNED64 = 0x1B


_STRUCT = {
    DataType.BOOLEAN: "<B",
    DataType.INTEGER8: "<b",
    DataType.INTEGER16: "<h",
    DataType.INTEGER32: "<i",
    DataType.UNSIGNED8: "<B",
    DataType.UNSIGNED16: "<H",
    DataType.UNSIGNED32: "<I",
    DataType.REAL32: "<f",
    DataType.INTEGER64: "<q",
    DataType.UNSIGNED64: "<Q",
}


def encode(dtype: DataType, value: Any) -> bytes:
    """Encode a Python value to its little-endian wire form."""
    if dtype == DataType.VISIBLE_STRING:
        return value.encode("ascii") if isinstance(value, str) else bytes(value)
    if dtype == DataType.OCTET_STRING:
        return bytes(value)
    if dtype == DataType.BOOLEAN:
        value = 1 if value else 0
    return struct.pack(_STRUCT[dtype], value)


def decode(dtype: DataType, data: bytes) -> Any:
    """Decode wire bytes to a Python value (strings may be any length)."""
    if dtype == DataType.VISIBLE_STRING:
        return bytes(data).decode("ascii", errors="replace")
    if dtype == DataType.OCTET_STRING:
        return bytes(data)
    value = struct.unpack(_STRUCT[dtype], data)[0]
    return bool(value) if dtype == DataType.BOOLEAN else value


@dataclass
class Variable:
    index: int
    sub: int
    name: str
    dtype: DataType
    access: str  # "ro" | "wo" | "rw" | "const"
    value: Any
    low: float | None = None
    high: float | None = None
    pdo_mappable: bool = False


class ObjectDictionary:
    def __init__(self) -> None:
        self._vars: dict[tuple[int, int], Variable] = {}
        self._indexes: set[int] = set()
        self._hooks: dict[tuple[int, int], list[Callable[[Any], None]]] = {}
        self._listeners: list[Callable[[int, int], None]] = []

    def add(self, var: Variable) -> None:
        self._vars[(var.index, var.sub)] = var
        self._indexes.add(var.index)

    def entry(self, index: int, sub: int) -> Variable:
        var = self._vars.get((index, sub))
        if var is None:
            raise SdoAbort(NO_SUBINDEX if index in self._indexes else NO_OBJECT)
        return var

    def entries(self) -> Iterable[Variable]:
        return self._vars.values()

    def get(self, index: int, sub: int) -> Any:
        return self.entry(index, sub).value

    def set(self, index: int, sub: int, value: Any) -> None:
        """Internal write: no access, range or callback handling."""
        var = self.entry(index, sub)
        changed = var.value != value
        var.value = value
        if changed:
            self._notify(index, sub)

    def on_change(self, fn: Callable[[int, int], None]) -> None:
        """Register ``fn(index, sub)`` called after a stored value changed
        (via ``set``, a successful ``sdo_write`` or ``pdo_write``)."""
        self._listeners.append(fn)

    def _notify(self, index: int, sub: int) -> None:
        for fn in self._listeners:
            fn(index, sub)

    def on_write(self, index: int, sub: int, fn: Callable[[Any], None]) -> None:
        """Register a callback run before a write is committed; it may raise
        SdoAbort to veto the write (the old value then stays)."""
        self.entry(index, sub)
        self._hooks.setdefault((index, sub), []).append(fn)

    def sdo_read(self, index: int, sub: int) -> bytes:
        var = self.entry(index, sub)
        if var.access == "wo":
            raise SdoAbort(WRITE_ONLY)
        return encode(var.dtype, var.value)

    def sdo_write(self, index: int, sub: int, data: bytes) -> None:
        var = self.entry(index, sub)
        if var.access in ("ro", "const"):
            raise SdoAbort(READ_ONLY)
        if var.dtype in _STRUCT:
            size = struct.calcsize(_STRUCT[var.dtype])
            if len(data) < size:
                raise SdoAbort(TOO_SHORT)
            if len(data) > size:
                raise SdoAbort(TOO_LONG)
        self._commit(var, decode(var.dtype, data))

    def pdo_write(self, index: int, sub: int, value: Any) -> None:
        """RPDO write path: no access check (objects may be "wo"), but range
        checks, hooks and change notification still apply. Raises SdoAbort
        on a veto or out-of-range value; the caller ignores it."""
        var = self.entry(index, sub)
        if var.access in ("ro", "const"):
            raise SdoAbort(READ_ONLY)
        self._commit(var, value)

    def _commit(self, var: Variable, value: Any) -> None:
        if var.dtype not in (DataType.VISIBLE_STRING, DataType.OCTET_STRING):
            if var.high is not None and value > var.high:
                raise SdoAbort(TOO_HIGH)
            if var.low is not None and value < var.low:
                raise SdoAbort(TOO_LOW)
        for fn in self._hooks.get((var.index, var.sub), ()):
            fn(value)
        changed = var.value != value
        var.value = value
        if changed:
            self._notify(var.index, var.sub)
