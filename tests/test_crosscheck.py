"""Cross-check against an independent client: the ``canopen`` Python library.

Each test runs a bench on a fresh virtual channel and talks to it through
``bench.network()`` (a connected ``canopen.Network``) and the node's exported
EDS. The success criterion of the project is the drive commissioning test.
"""
import canopen
import pytest

from canopen_sim.bench import Bench

from .helpers import wait_until

BLOB = 0x2100
BLOB_SIZE = 300


def _drive_with_blob():
    return Bench.from_dict({"nodes": [{
        "profile": "generic-drive", "id": 1, "name": "axis",
        "objects": [{"index": BLOB, "name": "Blob", "type": "OCTET_STRING",
                     "access": "rw", "default": "00" * BLOB_SIZE}]}]})


@pytest.fixture
def drive():
    with Bench.single("generic-drive", node_id=1, name="axis") as bench:
        yield bench


def test_canopen_sdo_expedited_segmented_block():
    with _drive_with_blob() as bench:
        node = bench.remote_node("axis")
        assert node.sdo.upload(0x1000, 0) == (0x00020192).to_bytes(4, "little")  # expedited
        assert node.sdo.upload(0x1008, 0) == b"SIM-DRIVE-1"                       # segmented
        payload = bytes(range(256)) + bytes(range(BLOB_SIZE - 256))
        with node.sdo.open(BLOB, 0, "wb", size=BLOB_SIZE, block_transfer=True) as stream:
            stream.write(payload)
        assert bench.find("axis").od.get(BLOB, 0) == payload
        with node.sdo.open(BLOB, 0, "rb", block_transfer=True) as stream:
            assert stream.read() == payload


def test_canopen_nmt_and_heartbeat(drive):
    node = drive.remote_node("axis")
    node.sdo[0x1017].raw = 50                      # faster heartbeat for the test
    node.nmt.state = "RESET COMMUNICATION"
    node.nmt.wait_for_bootup(timeout=2.0)
    node.sdo[0x1017].raw = 50                      # reset communication restored it
    assert node.nmt.wait_for_heartbeat(timeout=2.0) == "PRE-OPERATIONAL"
    node.nmt.state = "OPERATIONAL"
    wait_until(lambda: node.nmt.wait_for_heartbeat(timeout=1.0) == "OPERATIONAL")
    assert drive.variables("axis")["nmt_state"] == "operational"


def test_canopen_pdo_read(drive):
    node = drive.remote_node("axis")
    node.tpdo.read()
    assert node.tpdo[1].cob_id == 0x181
    assert [v.index for v in node.tpdo[1].map] == [0x6041, 0x606C]
    node.nmt.state = "OPERATIONAL"
    assert node.tpdo[1].wait_for_reception(timeout=2.0) is not None
    assert node.tpdo[1]["Statusword"].raw & 0x4F == 0x40      # switch on disabled


def test_canopen_emcy(drive):
    node = drive.remote_node("axis")
    # emcy.wait() only sees EMCYs that arrive after it is called, which races
    # with inject(); the consumer's log and active list do not.
    drive.inject("over_temperature")
    wait_until(lambda: [e.code for e in node.emcy.active] == [0x4310])
    assert node.emcy.active[0].register & 0x08                # temperature bit
    drive.clear()
    wait_until(lambda: node.emcy.log[-1].code == 0x0000)      # error reset EMCY
    assert node.emcy.active == []


def test_drive_commissioning_with_basenode402(drive):
    """Success criterion: commission, run, fault, acknowledge, run again."""
    node = drive.remote_node("axis", cls=canopen.BaseNode402)
    node.nmt.state = "PRE-OPERATIONAL"             # the client checks its NMT view first
    node.setup_402_state_machine()
    node.nmt.state = "OPERATIONAL"
    wait_until(lambda: node.state == "SWITCH ON DISABLED")
    node.state = "OPERATION ENABLED"
    node.op_mode = "PROFILED VELOCITY"
    assert node.op_mode == "PROFILED VELOCITY"
    node.rpdo_pointers[0x60FF].raw = 500           # target velocity, sent with RPDO1
    node.rpdo_pointers[0x60FF].pdo_parent.transmit()
    wait_until(lambda: node.tpdo_values[0x606C] >= 400)

    drive.inject("over_temperature")
    wait_until(lambda: node.state == "FAULT")
    assert node.is_faulted()
    drive.clear()
    node.state = "SWITCH ON DISABLED"              # fault reset (controlword bit 7)
    node.state = "OPERATION ENABLED"
    assert node.state == "OPERATION ENABLED"
    assert drive.variables("axis")["state"] == "operation_enabled"
