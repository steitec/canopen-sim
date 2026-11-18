"""The JSON control channel of a running bench."""
import json
import socket
import uuid

import pytest

from canopen_sim.bench import Bench
from canopen_sim.control import CONTROL_PORT, send_control


@pytest.fixture
def started():
    benches = []

    def make(*specs):
        nodes = [{"profile": "generic-drive", "id": i, "name": n} for n, i in specs]
        bench = Bench.from_dict({"nodes": nodes})
        bench.start(channel=f"test-{uuid.uuid4().hex}", control_port=0)
        benches.append(bench)
        return bench

    yield make
    for bench in benches:
        bench.stop()


def _call(bench, request):
    return send_control(request, port=bench.control_port)


def test_default_port():
    assert CONTROL_PORT == 47100


def test_inject_and_list(started):
    bench = started(("axis", 3))
    assert bench.control_port != 0
    assert _call(bench, {"inject": "over_temperature", "node": "axis"})["ok"] is True
    node = _call(bench, {"list": True})["nodes"]["axis"]
    assert node["id"] == 3
    assert node["profile"] == "generic-drive"
    assert node["nmt_state"] in {"operational", "pre_operational", "bootup", "stopped"}
    assert "over_temperature" in node["faults"]
    assert node["errors"] == ["0x4310"]
    values = _call(bench, {"get": True})["values"]
    assert "nmt_state" in values
    only = _call(bench, {"get": ["nmt_state"], "node": 3})["values"]
    assert list(only) == ["nmt_state"]
    bad = _call(bench, {"get": ["nope"]})
    assert bad["ok"] is False and "nmt_state" in bad["error"]
    assert _call(bench, {"clear": True})["ok"] is True
    assert _call(bench, {"list": True})["nodes"]["axis"]["faults"] == []


def test_node_required_with_two_nodes(started):
    bench = started(("axis", 3), ("lift", 4))
    answer = _call(bench, {"inject": "over_temperature"})
    assert answer["ok"] is False
    assert "node required: axis, lift" in answer["error"]
    assert _call(bench, {"inject": "over_temperature", "node": 4})["ok"] is True
    assert _call(bench, {"inject": "explode", "node": "axis"})["ok"] is False
    assert _call(bench, {"inject": "x", "params": [], "node": "axis"})["ok"] is False


def test_bad_json_keeps_connection(started):
    bench = started(("axis", 3))
    with socket.create_connection(("127.0.0.1", bench.control_port), timeout=2) as sock, \
            sock.makefile("rwb") as stream:
        stream.write(b"not json\n")
        stream.flush()
        assert json.loads(stream.readline())["ok"] is False
        stream.write(b'{"list": true}\n')
        stream.flush()
        assert json.loads(stream.readline())["ok"] is True


def test_line_too_long(started):
    bench = started(("axis", 3))
    with socket.create_connection(("127.0.0.1", bench.control_port), timeout=2) as sock, \
            sock.makefile("rwb") as stream:
        sock.sendall(b"x" * 70000 + b"\n")
        answer = json.loads(stream.readline())
        assert answer["ok"] is False and "too long" in answer["error"]
        try:
            assert stream.readline() == b""
        except ConnectionResetError:    # a platform may still reset after the answer
            pass


def test_stop_closes_server(started):
    bench = started(("axis", 3))
    port = bench.control_port
    bench.stop()
    with pytest.raises(OSError):
        send_control({"list": True}, port=port, timeout=0.5)


def test_unserialisable_values_are_sanitised(started):
    import enum

    class Mode(enum.IntEnum):
        RUN = 1

    bench = started(("axis", 3))
    node = bench.nodes["axis"]
    node.model.variables = lambda: {"a": float("nan"), "b": float("inf"), "c": b"\x01\xff",
                                    "d": Mode.RUN, "e": 1.5}
    answer = _call(bench, {"get": ["a", "b", "c", "d", "e"], "node": "axis"})
    assert answer["values"] == {"a": None, "b": None, "c": "01ff", "d": "RUN", "e": 1.5}


def test_unserialisable_object_keeps_connection(started):
    bench = started(("axis", 3))
    bench.nodes["axis"].model.variables = lambda: {"x": object()}
    with socket.create_connection(("127.0.0.1", bench.control_port), timeout=2) as sock, \
            sock.makefile("rwb") as stream:
        stream.write(b'{"get": true}\n')
        stream.flush()
        assert json.loads(stream.readline())["ok"] is False
        stream.write(b'{"list": true}\n')
        stream.flush()
        assert json.loads(stream.readline())["ok"] is True
