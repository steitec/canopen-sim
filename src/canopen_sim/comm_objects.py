"""Mandatory CANopen communication-profile objects."""
from __future__ import annotations

from dataclasses import dataclass, field

from .frames import emcy, rpdo, sdo_rx, sdo_tx, tpdo
from .od import (
    INCOMPATIBLE, NOT_MAPPABLE, PDO_LENGTH, DataType, ObjectDictionary, SdoAbort, Variable,
)

U8, U16, U32 = DataType.UNSIGNED8, DataType.UNSIGNED16, DataType.UNSIGNED32


class CommandVariable(Variable):
    """Variable whose value never changes: writes act as commands (via
    ``on_write`` hooks) and reads always return the initial value."""

    def __setattr__(self, key, val):
        if key == "value" and "value" in self.__dict__:
            return
        super().__setattr__(key, val)


def set_node_id(od: ObjectDictionary, node_id: int) -> None:
    """Fill the node-ID dependent defaults (EMCY COB-ID, SDO server COB-IDs)."""
    od.set(0x1014, 0, emcy(node_id))
    od.set(0x1200, 1, sdo_rx(node_id))
    od.set(0x1200, 2, sdo_tx(node_id))


def add_communication_objects(
    od: ObjectDictionary, *, device_type: int, vendor_id: int,
    product_code: int, revision: int, serial: int, device_name: str,
    heartbeat_ms: int = 1000,
) -> None:
    """Add the mandatory objects. The node-ID dependent COB-IDs (0x1014,
    0x1200) are placeholders until ``Node`` calls :func:`set_node_id`."""
    def add(index, sub, name, dtype, access, value, cls=Variable):
        od.add(cls(index, sub, name, dtype, access, value))

    add(0x1000, 0, "Device type", U32, "const", device_type)
    add(0x1001, 0, "Error register", U8, "ro", 0)
    add(0x1003, 0, "Pre-defined error field", U8, "rw", 0)
    for sub in range(1, 9):
        add(0x1003, sub, f"Standard error field {sub}", U32, "ro", 0)
    add(0x1005, 0, "COB-ID SYNC", U32, "rw", 0x80)
    add(0x1008, 0, "Manufacturer device name", DataType.VISIBLE_STRING, "const", device_name)
    add(0x1010, 0, "Store parameters: highest sub-index", U8, "const", 1)
    add(0x1010, 1, "Store all parameters", U32, "rw", 1, CommandVariable)
    add(0x1011, 0, "Restore defaults: highest sub-index", U8, "const", 1)
    add(0x1011, 1, "Restore all default parameters", U32, "rw", 1, CommandVariable)
    add(0x1014, 0, "COB-ID EMCY", U32, "rw", 0x80)
    add(0x1017, 0, "Producer heartbeat time", U16, "rw", heartbeat_ms)
    add(0x1018, 0, "Identity: highest sub-index", U8, "const", 4)
    add(0x1018, 1, "Vendor ID", U32, "ro", vendor_id)
    add(0x1018, 2, "Product code", U32, "ro", product_code)
    add(0x1018, 3, "Revision number", U32, "ro", revision)
    add(0x1018, 4, "Serial number", U32, "ro", serial)
    add(0x1200, 0, "SDO server: highest sub-index", U8, "const", 2)
    add(0x1200, 1, "COB-ID client to server", U32, "ro", 0x600)
    add(0x1200, 2, "COB-ID server to client", U32, "ro", 0x580)


@dataclass
class PdoSpec:
    number: int  # 0..3
    mapping: list[tuple[int, int, int]] = field(default_factory=list)  # (index, sub, bits)
    transmission_type: int = 255
    event_timer_ms: int = 0
    inhibit_100us: int = 0
    enabled: bool = True


INVALID = 0x80000000


def add_pdo_objects(
    od: ObjectDictionary, node_id: int, *, tpdos: list[PdoSpec], rpdos: list[PdoSpec],
) -> None:
    """Add PDO communication (0x1400/0x1800+n) and mapping (0x1600/0x1A00+n)
    objects. Call before ``Node(...)`` so reset restores them."""
    for specs, comm_base, map_base, cob_of, is_tx in (
        (rpdos, 0x1400, 0x1600, rpdo, False), (tpdos, 0x1800, 0x1A00, tpdo, True),
    ):
        for spec in specs:
            if not 0 <= spec.number <= 3:
                raise ValueError("PDO number must be 0..3")
            if len(spec.mapping) > 8:
                raise ValueError("at most 8 mapped objects")
            _add_pdo(od, comm_base + spec.number, map_base + spec.number,
                     cob_of(spec.number + 1, node_id), spec, is_tx)


def _add_pdo(od, comm, mapi, cob, spec, is_tx):
    def add(index, sub, name, dtype, access, value, **kw):
        od.add(Variable(index, sub, name, dtype, access, value, **kw))

    add(comm, 0, "Highest sub-index", U8, "const", 5)
    add(comm, 1, "COB-ID", U32, "rw", cob | (0 if spec.enabled else INVALID))
    add(comm, 2, "Transmission type", U8, "rw", spec.transmission_type)
    if is_tx:
        add(comm, 3, "Inhibit time", U16, "rw", spec.inhibit_100us)
    add(comm, 5, "Event timer", U16, "rw", spec.event_timer_ms)
    add(mapi, 0, "Number of mapped objects", U8, "rw", len(spec.mapping), high=8)
    for i in range(8):
        entry = 0
        if i < len(spec.mapping):
            index, sub, bits = spec.mapping[i]
            entry = index << 16 | sub << 8 | bits
        add(mapi, i + 1, f"Mapped object {i + 1}", U32, "rw", entry)

    def require_disabled():
        if not od.get(comm, 1) & INVALID:
            raise SdoAbort(INCOMPATIBLE)

    def on_cob(value):
        current = od.get(comm, 1)
        if not current & INVALID and value != current and not value & INVALID:
            raise SdoAbort(INCOMPATIBLE)

    def on_count(value):
        require_disabled()
        total = 0
        for sub in range(1, value + 1):
            entry = od.get(mapi, sub)
            var = od.entry(entry >> 16, (entry >> 8) & 0xFF)
            if not var.pdo_mappable:
                raise SdoAbort(NOT_MAPPABLE)
            total += entry & 0xFF
        if total > 64:
            raise SdoAbort(PDO_LENGTH)

    def on_entry(_value):
        require_disabled()
        if od.get(mapi, 0) != 0:
            raise SdoAbort(INCOMPATIBLE)

    od.on_write(comm, 1, on_cob)
    od.on_write(mapi, 0, on_count)
    for sub in range(1, 9):
        od.on_write(mapi, sub, on_entry)
