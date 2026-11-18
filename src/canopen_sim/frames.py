"""CAN frame value type and pre-defined connection set COB-IDs."""
from __future__ import annotations

from dataclasses import dataclass

NMT = 0x000
SYNC = 0x080


@dataclass(frozen=True)
class Frame:
    cob_id: int
    data: bytes


def emcy(node_id: int) -> int:
    return 0x080 + node_id


def tpdo(n: int, node_id: int) -> int:
    if not 1 <= n <= 4:
        raise ValueError("PDO number must be 1..4")
    return 0x080 + 0x100 * n + node_id


def rpdo(n: int, node_id: int) -> int:
    if not 1 <= n <= 4:
        raise ValueError("PDO number must be 1..4")
    return 0x100 + 0x100 * n + node_id


def sdo_tx(node_id: int) -> int:
    """Server to client."""
    return 0x580 + node_id


def sdo_rx(node_id: int) -> int:
    """Client to server."""
    return 0x600 + node_id


def heartbeat(node_id: int) -> int:
    return 0x700 + node_id
