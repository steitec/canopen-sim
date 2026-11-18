"""The success scenario on a real (virtual) SocketCAN interface.

Needs Linux with ``vcan0`` up (CI sets it up; locally: ``sudo modprobe vcan;
sudo ip link add vcan0 type vcan; sudo ip link set up vcan0``). Skipped otherwise.
Run only these with ``pytest -m vcan``.
"""
import socket
import sys

import pytest

from canopen_sim.bench import Bench

from .helpers import commission_fault_and_recover


def _has_vcan0() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        socket.if_nametoindex("vcan0")
    except OSError:
        return False
    return True


pytestmark = [
    pytest.mark.vcan,
    pytest.mark.skipif(not _has_vcan0(), reason="needs Linux with a vcan0 interface"),
]


def test_commissioning_on_socketcan_vcan0():
    bench = Bench.single("generic-drive", node_id=1, name="axis")
    bench.start(interface="socketcan", channel="vcan0")
    try:
        commission_fault_and_recover(bench)
    finally:
        bench.stop()
