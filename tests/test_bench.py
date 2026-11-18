"""Bench over python-can's in-process ``virtual`` interface."""
import logging
import struct
import threading
import uuid

import can
import pytest

from canopen_sim.bench import Bench, BenchError
from tests.helpers import wait_until


@pytest.fixture
def channel():
    return f"test-{uuid.uuid4().hex}"


@pytest.fixture
def raw(channel):
    bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    yield bus
    bus.shutdown()


@pytest.fixture
def benches():
    started = []
    yield started
    for bench in started:
        bench.stop()


class Collector:
    """Collect frames from a raw bus while polling for a condition."""

    def __init__(self, bus):
        self.bus = bus
        self.frames = []

    def pump(self):
        while (msg := self.bus.recv(0)) is not None:
            self.frames.append((msg.arbitration_id, bytes(msg.data)))

    def wait_for(self, pred, timeout=2.0):
        found = []

        def check():
            self.pump()
            hits = [f for f in self.frames if pred(f)]
            if hits:
                found.append(hits[0])
                return True
            return False

        wait_until(check, timeout=timeout)
        return found[0]


def send(bus, cob, data):
    bus.send(can.Message(arbitration_id=cob, data=data, is_extended_id=False))


def sdo_upload(index, sub):
    return bytes([0x40]) + struct.pack("<HB", index, sub) + bytes(4)


def start(benches, bench, channel):
    benches.append(bench)
    bench.start(interface="virtual", channel=channel)
    return bench


def test_bench_bootup_and_sdo_roundtrip(benches, raw, channel):
    bench = start(benches, Bench.single("generic-drive", node_id=5), channel)
    assert bench.interface == "virtual" and bench.channel == channel
    rx = Collector(raw)
    assert rx.wait_for(lambda f: f[0] == 0x705) == (0x705, b"\x00")
    send(raw, 0x605, sdo_upload(0x1000, 0))
    answer = rx.wait_for(lambda f: f[0] == 0x585)
    assert answer[1][:4] == b"\x43\x00\x10\x00"
    assert struct.unpack("<I", answer[1][4:])[0] == 0x00020192


def test_heartbeat_and_ticking_model(benches, raw, channel):
    bench = Bench.single("generic-encoder", node_id=2)
    bench.nodes["generic-encoder"].od.set(0x1017, 0, 50)
    bench.nodes["generic-encoder"].model.speed_rpm = 600.0
    start(benches, bench, channel)
    rx = Collector(raw)
    rx.wait_for(lambda f: f == (0x702, b"\x7f"))  # heartbeat, pre-operational
    send(raw, 0x000, bytes([0x01, 2]))  # start
    rx.wait_for(lambda f: f == (0x702, b"\x05"))
    # TPDO1 carries a moving position value
    first = rx.wait_for(lambda f: f[0] == 0x182)
    rx.wait_for(lambda f: f[0] == 0x182 and f != first)
    assert bench.variables()["speed_rpm"] == 600.0


def test_receive_thread_survives_garbage(benches, raw, channel):
    bench = start(benches, Bench.single("generic-io", node_id=4), channel)
    rx = Collector(raw)
    rx.wait_for(lambda f: f[0] == 0x704)
    send(raw, 0x604, b"")  # DLC 0 SDO request
    send(raw, 0x000, bytes([0x01, 4, 0xFF]))  # 3-byte NMT
    send(raw, 0x7FF, b"\x01\x02")  # unknown COB-ID
    raw.send(can.Message(arbitration_id=0x604, is_extended_id=True, data=b"\x40"))
    raw.send(can.Message(arbitration_id=0x604, is_extended_id=False, is_remote_frame=True,
                         dlc=8))
    send(raw, 0x604, sdo_upload(0x1008, 0))
    answer = rx.wait_for(lambda f: f[0] == 0x584)
    assert answer[1][:4] == b"\x41\x08\x10\x00"  # segmented upload initiated
    assert bench.nodes["generic-io"].state == 0x7F  # 3-byte NMT ignored
    assert all(t.is_alive() for t in bench._threads)


def test_node_exception_is_logged_and_skipped(benches, raw, channel, caplog):
    bench = start(benches, Bench.single("generic-io", node_id=6), channel)
    rx = Collector(raw)
    rx.wait_for(lambda f: f[0] == 0x706)
    node = bench.nodes["generic-io"]
    original = node.handle
    calls = []

    def broken(frame, now):
        calls.append(frame)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return original(frame, now)

    node.handle = broken
    with caplog.at_level(logging.ERROR, logger="canopen_sim"):
        send(raw, 0x606, sdo_upload(0x1000, 0))
        wait_until(lambda: any("boom" in r.getMessage() or (r.exc_info and "boom" in
                                                            str(r.exc_info[1]))
                               for r in caplog.records))
        send(raw, 0x606, sdo_upload(0x1000, 0))
        rx.wait_for(lambda f: f[0] == 0x586)
    assert all(t.is_alive() for t in bench._threads)


def test_two_nodes_dispatch_and_inject(benches, raw, channel):
    bench = Bench.from_dict({
        "bus": {"interface": "virtual"},
        "nodes": [{"profile": "generic-drive", "id": 3, "name": "axis"},
                  {"profile": "generic-io", "id": 4, "name": "io"}]})
    start(benches, bench, channel)
    rx = Collector(raw)
    rx.wait_for(lambda f: f[0] == 0x703)
    rx.wait_for(lambda f: f[0] == 0x704)
    send(raw, 0x000, bytes([0x01, 0]))  # broadcast start
    wait_until(lambda: all(n.state == 0x05 for n in bench.nodes.values()))
    with pytest.raises(BenchError):
        bench.inject("over_temperature")  # two nodes: name one
    bench.inject("over_temperature", node="axis")
    emcy = rx.wait_for(lambda f: f[0] == 0x83)
    assert emcy[1][:2] == b"\x10\x43"
    assert "over_temperature" in bench.find(3).faults()
    assert bench.variables("axis")["nmt_state"] == "operational"
    text = bench.describe()
    assert "axis" in text and "io" in text and channel in text
    bench.clear()  # all nodes
    rx.wait_for(lambda f: f[0] == 0x83 and f[1][:2] == b"\x00\x00")
    assert bench.find("axis").faults() == []


def test_start_stop_idempotent_and_default_channel(raw):
    bench = Bench.single("generic-io", node_id=1)
    before = {t.ident for t in threading.enumerate()}
    try:
        bench.start()
        assert bench.interface == "virtual"
        assert bench.channel.startswith("canopen-sim-")
        with pytest.raises(BenchError):
            bench.start()
    finally:
        bench.stop()
    bench.stop()
    leftover = [t for t in threading.enumerate() if t.ident not in before]
    assert leftover == []
    other = Bench.single("generic-io", node_id=1)
    try:
        other.start()
        assert other.channel != bench.channel
    finally:
        other.stop()


def test_tick_dt_is_capped_after_a_stall(benches, channel):
    from canopen_sim import bench as bench_mod
    bench = start(benches, Bench.single("generic-drive", node_id=7), channel)
    node = bench.nodes["generic-drive"]
    original = node.tick
    dts = []

    def recording(dt, now):
        dts.append(dt)
        return original(dt, now)

    node.tick = recording
    wait_until(lambda: len(dts) >= 3)
    with bench._locks["generic-drive"]:  # stall the tick thread
        stalled_at = len(dts)
        threading.Event().wait(0.3)
    wait_until(lambda: len(dts) >= stalled_at + 3)
    assert max(dts) == bench_mod.MAX_DT
    assert all(0 < dt <= bench_mod.MAX_DT for dt in dts)
