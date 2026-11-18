"""pytest plugin: a fresh simulated CANopen bench for every test.

Fixtures:

- ``canopen_bench`` -- the bench from ``--canopen-bench FILE`` or the ini key
  ``canopen_bench`` (relative to the rootdir); without either, one
  ``generic-drive`` on node 1. ``canopen_bench.network()`` returns a connected
  ``canopen.Network``, ``canopen_bench.remote_node(node, cls=...)`` a canopen
  node built from the exported EDS (both need the ``canopen`` package).
- ``canopen_bench_factory`` -- ``factory(source, **start_options)`` for more
  benches: a bench file, a dict, or a bundled profile name. Start options are
  those of :meth:`Bench.start`.

A bench on the ``virtual`` interface always gets a fresh, unique channel (even
if its file names one), so tests never hear each other; pass ``channel=`` to
the factory to choose one. A ``socketcan`` bench keeps its channel. Every
bench is stopped at teardown, which also disconnects its networks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .bench import Bench, BenchError, _default_channel
from .profile import bundled_profiles

DEFAULT_PROFILE = "generic-drive"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("canopen-sim")
    group.addoption("--canopen-bench", metavar="FILE",
                    help="bench file (YAML) for the canopen_bench fixture")
    parser.addini("canopen_bench", "bench file (YAML) for the canopen_bench fixture, "
                  "relative to the rootdir")


class BenchPool:
    def __init__(self, root: Path | None):
        self.root = root
        self.benches: list[Bench] = []

    def start(self, source: str | Path | dict, **options: Any) -> Bench:
        message = None
        try:
            if isinstance(source, dict):
                bench = Bench.from_dict(source)
            elif isinstance(source, str) and source in bundled_profiles():
                bench = Bench.single(source)
            else:
                path = Path(source)
                if self.root is not None and not path.is_absolute():
                    path = self.root / path
                bench = Bench.from_yaml(path)
            if (options.get("interface") or bench.interface) == "virtual":
                options.setdefault("channel", _default_channel("virtual"))
            bench.start(**options)
        except (BenchError, OSError) as error:
            message = f"canopen-sim bench: {error}"
        if message is not None:
            pytest.fail(message, pytrace=False)
        self.benches.append(bench)
        return bench

    def stop_all(self) -> None:
        failure = None
        for bench in self.benches:
            try:
                bench.stop()
            except Exception as error:  # keep stopping the others
                failure = failure or error
        self.benches.clear()
        if failure is not None:
            raise failure


def _configured(config: pytest.Config) -> Path | str:
    option = config.getoption("--canopen-bench")
    if option:
        return config.invocation_params.dir / option
    ini = config.getini("canopen_bench")
    if ini:
        return config.rootpath / ini
    return DEFAULT_PROFILE


@pytest.fixture
def canopen_bench_factory(request: pytest.FixtureRequest):
    pool = BenchPool(request.config.rootpath)
    yield pool.start
    pool.stop_all()


@pytest.fixture
def canopen_bench(request: pytest.FixtureRequest):
    pool = BenchPool(None)
    yield pool.start(_configured(request.config))
    pool.stop_all()
