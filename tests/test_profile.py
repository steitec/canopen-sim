"""Profile loader and node builder."""
import pytest

from canopen_sim.bench import Bench, BenchError
from canopen_sim.frames import Frame
from canopen_sim.models.drive import DriveModel
from canopen_sim.models.encoder import EncoderModel
from canopen_sim.models.io import IoModel
from canopen_sim.node import Node
from canopen_sim.od import DataType, encode
from canopen_sim.profile import ProfileError, build_node, bundled_profiles, load_profile

EXPECTED = {
    "generic-io": (0x000F0191, IoModel),
    "generic-encoder": (0x00020196, EncoderModel),
    "generic-drive": (0x00020192, DriveModel),
}


def test_bundled_profiles_build():
    assert bundled_profiles() == sorted(EXPECTED)
    for name, (device_type, model_cls) in EXPECTED.items():
        node = build_node(name, node_id=7)
        assert isinstance(node, Node)
        assert node.node_id == 7 and node.name == name
        assert isinstance(node.model, model_cls)
        assert node.od.get(0x1000, 0) == device_type
        assert node.od.get(0x1018, 0) == 4
        assert node.od.get(0x1018, 1) == 0  # default vendor ID
        assert node.od.get(0x1008, 0).startswith("SIM-")
        assert node.od.get(0x1200, 1) == 0x607
        spec = load_profile(name)
        assert spec["sources"] and spec["limits"]
        # every profile maps at least one TPDO and its COB-ID is valid
        assert node.od.get(0x1800, 1) == 0x187
        assert node.od.get(0x1A00, 0) >= 1


def test_bundled_default_mappings_follow_pdo_rules():
    """The SDO mapping rules accept every default mapping: disable, clear the
    count and write it back (that write validates all entries)."""
    for name in EXPECTED:
        node = build_node(name, node_id=1)
        keys = {(v.index, v.sub) for v in node.od.entries()}
        checked = 0
        for comm, mapping in ((0x1400, 0x1600), (0x1800, 0x1A00)):
            for n in range(4):
                if (mapping + n, 0) not in keys:
                    continue
                count = node.od.get(mapping + n, 0)
                cob = node.od.get(comm + n, 1)
                node.od.sdo_write(comm + n, 1, encode(DataType.UNSIGNED32, cob | 0x80000000))
                node.od.sdo_write(mapping + n, 0, b"\x00")
                node.od.sdo_write(mapping + n, 0, bytes([count]))
                checked += 1
        assert checked >= 2, name


def test_drive_pdo_layout():
    node = build_node("generic-drive", node_id=3)
    assert node.od.get(0x1A00, 1) == 0x60410010
    assert node.od.get(0x1A00, 2) == 0x606C0020
    assert node.od.get(0x1600, 1) == 0x60400010
    assert node.od.get(0x1600, 2) == 0x60FF0020
    # RPDO1 drives the controlword in OPERATIONAL
    node.boot(0.0)
    node.handle(Frame(0x000, bytes([1, 3])), 0.0)
    node.handle(Frame(0x203, bytes([0x06, 0x00, 0x10, 0, 0, 0])), 0.01)
    assert node.od.get(0x6040, 0) == 0x06
    assert node.od.get(0x60FF, 0) == 16


def test_profile_override_and_extra_object():
    node = build_node({
        "profile": "generic-io",
        "identity": {"serial": 42, "vendor_id": 0x1234},
        "heartbeat_ms": 250,
        "model": {"digital_inputs": 16},
        "objects": [
            {"index": 0x2000, "sub": 0, "name": "Filter time", "type": "UNSIGNED16",
             "access": "rw", "default": 10, "low": 1, "high": 1000, "pdo": True},
            {"index": 0x2001, "sub": 0, "name": "Label", "type": "VISIBLE_STRING",
             "access": "rw", "default": "bench"},
        ],
    }, node_id=9, name="io9")
    assert node.name == "io9"
    assert node.od.get(0x1018, 4) == 42
    assert node.od.get(0x1018, 1) == 0x1234
    assert node.od.get(0x1018, 2) == load_profile("generic-io")["identity"]["product_code"]
    assert node.od.get(0x1017, 0) == 250
    assert node.od.get(0x6000, 0) == 2  # 16 inputs -> two 8-bit groups
    var = node.od.entry(0x2000, 0)
    assert (var.dtype, var.access, var.value, var.low, var.high, var.pdo_mappable) == \
        (DataType.UNSIGNED16, "rw", 10, 1, 1000, True)
    assert node.od.get(0x2001, 0) == "bench"


@pytest.mark.parametrize("mapping", [
    "0x6041/16", "0x6041:0", "x6041:0/16", "0x6041:0/0", "0x6041:0/65", "",
    "0x9999:0/16",  # no such object
    "0x6065:0/32",  # exists but not mappable
    "0x6041:0/32",  # wider than UNSIGNED16
])
def test_bad_mapping_string_rejected(mapping):
    with pytest.raises(ProfileError):
        build_node({"profile": "generic-drive",
                    "tpdos": [{"number": 0, "mapping": [mapping]}]}, node_id=1)


def test_bad_profiles_rejected():
    cases = [
        "no-such-profile",
        {"profile": "generic-io", "model": {"type": "pump"}},
        {"profile": "generic-io", "model": {"bogus": 1}},
        {"profile": "generic-io", "objects": [{"index": 0x6000, "sub": 9, "name": "x",
                                               "type": "UNSIGNED8", "access": "rw"}]},
        {"profile": "generic-io", "objects": [{"index": 0x2000, "sub": 0, "name": "x",
                                               "type": "FLOAT", "access": "rw"}]},
        {"profile": "generic-io", "objects": [{"index": 0x2000, "sub": 0, "name": "x",
                                               "type": "UNSIGNED8", "access": "rw",
                                               "default": 300}]},
        {"profile": "generic-drive",  # 9 x 16 bits > 64 bits and > 8 entries
         "tpdos": [{"number": 0, "mapping": ["0x6041:0/16"] * 9}]},
        {"profile": "generic-drive",  # RPDO must map writable objects
         "rpdos": [{"number": 0, "mapping": ["0x6041:0/16"]}]},
        {"profile": "generic-drive", "tpdos": [{"number": 4, "mapping": []}]},
        {"profile": "generic-drive", "unknown_key": 1},
    ]
    for case in cases:
        with pytest.raises(ProfileError):
            build_node(case, node_id=1)
    for bad_id in (0, 128, "3"):
        with pytest.raises(ProfileError):
            build_node("generic-io", node_id=bad_id)


def test_duplicate_node_id_rejected():
    spec = {"bus": {"interface": "virtual"},
            "nodes": [{"profile": "generic-drive", "id": 3, "name": "axis"},
                      {"profile": "generic-io", "id": 3, "name": "io"}]}
    with pytest.raises(BenchError, match="node ID 3"):
        Bench.from_dict(spec)
    spec["nodes"][1]["id"] = 4
    spec["nodes"][1]["name"] = "axis"
    with pytest.raises(BenchError, match="name"):
        Bench.from_dict(spec)


def test_bench_from_yaml(tmp_path):
    path = tmp_path / "bench.yaml"
    path.write_text(
        "bus: {interface: socketcan, channel: vcan1, bitrate: 250000}\n"
        "nodes:\n"
        "  - {profile: generic-drive, id: 3, name: axis, heartbeat_ms: 500}\n"
        "  - {profile: generic-io, id: 4}\n"
        "control: {port: 47101}\n", encoding="utf-8")
    bench = Bench.from_yaml(path)
    assert bench.interface == "socketcan" and bench.channel == "vcan1"
    assert bench.bitrate == 250000 and bench.control_port == 47101
    assert list(bench.nodes) == ["axis", "generic-io"]
    assert bench.find(3) is bench.nodes["axis"] and bench.find("axis").node_id == 3
    assert bench.find(4).name == "generic-io"
    assert bench.nodes["axis"].od.get(0x1017, 0) == 500
    assert bench.profiles == {"axis": "generic-drive", "generic-io": "generic-io"}
    with pytest.raises(BenchError):
        bench.find("nope")
    with pytest.raises(BenchError):
        Bench.from_yaml(tmp_path / "missing.yaml")


def test_io_signals_from_profile():
    node = build_node({"profile": "generic-io", "model": {"signals": {
        "ai1": [{"type": "constant", "value": 100}, {"type": "sine", "offset": 0,
                                                     "amplitude": 0, "period": 1}],
        "di2": {"sources": [{"type": "constant", "value": 1}], "seed": 3}}}}, node_id=1)
    node.tick(0.005, 1.0)
    assert node.od.get(0x6401, 1) == 100
    assert node.od.get(0x6000, 1) & 0x02
    for bad in ({"ai1": [{"type": "square", "value": 1}]},
                {"ai1": [{"type": "ramp", "start": 0, "stop": 1}]},
                {"ai1": [{"type": "sine", "offset": 0, "amplitude": 1, "period": 0}]},
                {"ai1": []}):
        with pytest.raises(ProfileError):
            build_node({"profile": "generic-io", "model": {"signals": bad}}, node_id=1)
