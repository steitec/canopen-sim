"""Shared test helpers."""
import time


def wait_until(pred, timeout=2.0, interval=0.01):
    """Poll ``pred`` until it returns truthy; raise AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)


def commission_fault_and_recover(bench, node_name="axis"):
    """The success scenario: commission a drive with the ``canopen`` library's
    ``BaseNode402``, run it, inject over_temperature, clear, reset the fault."""
    import canopen

    node = bench.remote_node(node_name, cls=canopen.BaseNode402)
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

    bench.inject("over_temperature")
    wait_until(lambda: node.state == "FAULT")
    assert node.is_faulted()
    bench.clear()
    node.state = "SWITCH ON DISABLED"              # fault reset (controlword bit 7)
    node.state = "OPERATION ENABLED"
    assert node.state == "OPERATION ENABLED"
    assert bench.variables(node_name)["state"] == "operation_enabled"
