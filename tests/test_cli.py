"""Command line: list, map, export-eds, control commands and serve."""
import re
import sys
import threading

import pytest

from canopen_sim.bench import Bench
from canopen_sim.cli import main
from canopen_sim.profile import bundled_profiles
from tests.helpers import wait_until


@pytest.fixture
def bench():
    bench = Bench.single("generic-drive", node_id=3)
    bench.start(interface="virtual", control_port=0)
    yield bench
    bench.stop()


def test_list_names_three_profiles(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    names = bundled_profiles()
    assert len(names) == 3
    for name in names:
        assert name in out


def test_map_shows_sources_and_limits(capsys):
    assert main(["map", "generic-drive"]) == 0
    out = capsys.readouterr().out
    assert "0x6040:0" in out
    assert "TPDO1" in out and "RPDO1" in out
    assert "0x6041:0/16" in out
    assert "\nSources\n" in out and "\nLimits\n" in out
    assert "not certified by CiA" in out


def test_map_unknown_profile_exit_2(capsys):
    assert main(["map", "nope"]) == 2
    assert "unknown profile" in capsys.readouterr().err


def test_export_eds_to_file(tmp_path, capsys):
    target = tmp_path / "drive.eds"
    assert main(["export-eds", "generic-drive", "-o", str(target), "--id", "5"]) == 0
    assert "[FileInfo]" in target.read_text(encoding="utf-8")
    assert main(["export-eds", "generic-drive"]) == 0
    assert "[FileInfo]" in capsys.readouterr().out


def test_inject_against_running_bench(bench, capsys):
    port = str(bench.control_port)
    assert main(["inject", "over_temperature", "--control-port", port]) == 0
    assert "over_temperature" in bench.status()["generic-drive"]["faults"]
    assert main(["state", "--control-port", port]) == 0
    out = capsys.readouterr().out
    assert "generic-drive" in out and "over_temperature" in out
    assert main(["clear", "--control-port", port]) == 0
    assert bench.status()["generic-drive"]["faults"] == []


def test_inject_parses_values(bench):
    port = str(bench.control_port)
    assert main(["inject", "bogus", "--control-port", port]) == 1
    assert main(["inject", "over_temperature", "x=0x10", "y=1.5", "z=true",
                 "--control-port", port]) == 1  # unknown parameters: answered ok:false


def test_error_answer_exit_1_on_stderr(bench, capsys):
    assert main(["inject", "bogus", "--control-port", str(bench.control_port)]) == 1
    assert capsys.readouterr().err.strip()


def test_state_json(bench, capsys):
    assert main(["state", "--json", "--control-port", str(bench.control_port)]) == 0
    assert '"nmt_state"' in capsys.readouterr().out


def test_unreachable_control_exit_3(capsys):
    assert main(["state", "--control-port", "1"]) == 3
    assert "control" in capsys.readouterr().err


def test_serve_rejects_bad_bench_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("nodes: 5\n", encoding="utf-8")
    assert main(["serve", "--bench", str(bad)]) == 2
    assert capsys.readouterr().err
    assert main(["serve"]) == 2
    assert main(["serve", "--profile", "nope"]) == 2


def test_serve_prints_nodes_and_stops(capsys):
    stop = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(main(
        ["serve", "--profile", "generic-drive", "--id", "3", "--interface", "virtual",
         "--control-port", "0"], stop=stop)))
    thread.start()
    try:
        out = []

        def ready():
            out.append(capsys.readouterr().out)
            return "control 127.0.0.1:" in "".join(out)

        wait_until(ready, timeout=5.0)
        text = "".join(out)
        assert re.search(r"generic-drive\s+id 3\s+generic-drive\s+\S+", text)
        port = int(re.search(r"control 127\.0\.0\.1:(\d+)", text).group(1))
        assert port > 0
        assert main(["state", "--control-port", str(port)]) == 0
    finally:
        stop.set()
        thread.join(5)
    assert result == [0]


@pytest.mark.parametrize("params", [["foo"], ["x=nan"], ["x=inf"]])
def test_malformed_inject_params_exit_2(params, capsys):
    assert main(["inject", "over_temperature", *params, "--control-port", "1"]) == 2
    assert capsys.readouterr().err.strip()


def test_map_runs_as_module():
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "canopen_sim", "map", "generic-drive"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="the `| head` pipeline needs a POSIX shell")
def test_broken_pipe_is_silent():
    import subprocess
    shell = subprocess.run(
        f"{sys.executable} -m canopen_sim map generic-drive | head -1 >/dev/null",
        shell=True, stderr=subprocess.PIPE)
    assert shell.stderr == b""


def test_serve_on_virtual_bus_prints_a_hint_to_stderr(capsys):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: main(
        ["serve", "--profile", "generic-drive", "--interface", "virtual",
         "--control-port", "0"], stop=stop))
    thread.start()
    try:
        err = []
        wait_until(lambda: err.append(capsys.readouterr().err) or "note:" in "".join(err))
        assert "virtual bus is only reachable from this process" in "".join(err)
        assert "--interface socketcan --channel vcan0" in "".join(err)
    finally:
        stop.set()
        thread.join(5)
