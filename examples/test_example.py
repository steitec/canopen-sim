"""Commission a simulated drive with the canopen library, using the pytest plugin.

Run with:  pytest -v examples/test_example.py
(examples/pytest.ini points the ``canopen_bench`` fixture at axis.yaml; elsewhere
use ``--canopen-bench examples/axis.yaml`` or the ``canopen_bench`` ini key.)
"""
import time

import canopen


def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_drive_runs_faults_and_recovers(canopen_bench):
    drive = canopen_bench.remote_node("axis", cls=canopen.BaseNode402)
    drive.nmt.state = "PRE-OPERATIONAL"
    drive.setup_402_state_machine()            # reads the PDO layout via SDO
    drive.nmt.state = "OPERATIONAL"
    wait_until(lambda: drive.state == "SWITCH ON DISABLED")
    drive.state = "OPERATION ENABLED"
    drive.op_mode = "PROFILED VELOCITY"
    drive.rpdo_pointers[0x60FF].raw = 500      # target velocity in RPDO1
    drive.rpdo_pointers[0x60FF].pdo_parent.transmit()
    wait_until(lambda: drive.tpdo_values[0x606C] >= 400)    # velocity actual value

    canopen_bench.inject("over_temperature")
    wait_until(lambda: drive.state == "FAULT")
    assert [error.code for error in drive.emcy.active] == [0x4310]  # over temperature
    canopen_bench.clear()
    drive.state = "SWITCH ON DISABLED"         # fault reset
    drive.state = "OPERATION ENABLED"
    assert drive.state == "OPERATION ENABLED"
