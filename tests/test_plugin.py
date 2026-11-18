"""The pytest plugin's fixtures and ``bench.network()``."""
import can
import pytest

from canopen_sim.bench import Bench

from .helpers import wait_until

pytest_plugins = ["pytester"]


def test_canopen_bench_default_is_a_drive_on_node_1(canopen_bench):
    assert canopen_bench.running
    assert canopen_bench.interface == "virtual"
    node = canopen_bench.find()
    assert node.node_id == 1
    assert canopen_bench.profiles[node.name] == "generic-drive"


def test_canopen_bench_network_talks_to_the_bench(canopen_bench):
    network = canopen_bench.network()
    remote = canopen_bench.remote_node(network=network)
    assert remote.network is network
    assert remote.sdo.upload(0x1008, 0) == b"SIM-DRIVE-1"


def test_networks_are_disconnected_on_stop():
    bench = Bench.single("generic-io")
    bench.start()
    network = bench.network()
    assert network.bus is not None
    bench.stop()
    assert network.bus is None


def test_network_needs_a_running_bench():
    with pytest.raises(RuntimeError, match="start"):
        Bench.single("generic-io").network()


def test_network_without_canopen_has_an_install_hint(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "canopen":
            raise ImportError("no canopen")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with Bench.single("generic-io") as bench:
        with pytest.raises(RuntimeError, match="pip install canopen"):
            bench.network()


def test_factory_from_profile_and_dict(canopen_bench_factory):
    io_bench = canopen_bench_factory("generic-io")
    assert io_bench.running and io_bench.find().node_id == 1
    other = canopen_bench_factory({"nodes": [{"profile": "generic-encoder", "id": 5,
                                              "name": "enc"}]})
    assert other.find("enc").node_id == 5
    assert other.channel != io_bench.channel


def test_factory_stops_its_benches(pytester):
    pytester.makepyfile("""
        import pytest
        benches = []

        def test_start(canopen_bench_factory, canopen_bench):
            benches.append(canopen_bench_factory("generic-io"))
            benches.append(canopen_bench)

        def test_stopped():
            assert benches and not any(b.running for b in benches)
    """)
    pytester.runpytest("-W", "error").assert_outcomes(passed=2)


def _boot_ups(bus, timeout=0.3):
    seen = []
    while (msg := bus.recv(timeout)) is not None:
        if 0x701 <= msg.arbitration_id <= 0x77F and bytes(msg.data) == b"\x00":
            seen.append(msg.arbitration_id - 0x700)
    return seen


def test_two_benches_isolated(canopen_bench_factory):
    """Review focus 5: two benches with the same node ID on separate channels."""
    first = canopen_bench_factory("generic-io")
    second = canopen_bench_factory("generic-drive")
    assert first.channel != second.channel
    with can.Bus(interface="virtual", channel=first.channel) as bus_a, \
            can.Bus(interface="virtual", channel=second.channel) as bus_b:
        bus_a.send(can.Message(arbitration_id=0x000, data=[0x82, 0],
                               is_extended_id=False))   # reset communication, all nodes
        wait_until(lambda: _boot_ups(bus_a, 0.05) == [1])
        assert _boot_ups(bus_b) == []
        bus_b.send(can.Message(arbitration_id=0x000, data=[0x82, 1], is_extended_id=False))
        wait_until(lambda: _boot_ups(bus_b, 0.05) == [1])
        assert _boot_ups(bus_a) == []


@pytest.mark.parametrize("how", ["option", "ini"])
def test_canopen_bench_from_a_file(pytester, how):
    pytester.makefile(".yaml", axis="""
        bus: {interface: virtual, channel: shared-name}
        nodes:
          - {profile: generic-drive, id: 3, name: axis}
    """)
    pytester.makepyfile("""
        def test_axis(canopen_bench):
            assert canopen_bench.find("axis").node_id == 3
            assert canopen_bench.channel != "shared-name"   # fresh channel per test
    """)
    if how == "option":
        result = pytester.runpytest("-W", "error", "--canopen-bench", "axis.yaml")
    else:
        pytester.makeini("[pytest]\ncanopen_bench = axis.yaml\n")
        result = pytester.runpytest("-W", "error")
    result.assert_outcomes(passed=1)


def test_a_bad_bench_file_fails_the_test(pytester):
    pytester.makepyfile("""
        def test_x(canopen_bench):
            pass
    """)
    result = pytester.runpytest("--canopen-bench", "missing.yaml")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*canopen-sim bench*missing.yaml*"])
