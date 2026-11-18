"""Mandatory CANopen communication-profile objects."""
from __future__ import annotations

from .frames import emcy, sdo_rx, sdo_tx
from .od import DataType, ObjectDictionary, Variable

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
