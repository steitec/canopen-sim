"""A bench runs one or more simulated nodes on a python-can bus.

Build one from a bundled profile (:meth:`Bench.single`), a bench YAML file or a
dict, then :meth:`Bench.start` it. Two threads serve the bus:

* the receive thread reads frames (``bus.recv(0.05)``) and hands each one to
  every node whose ``listen_ids()`` contain its COB-ID (NMT and SYNC reach all
  nodes), then sends the answers;
* the tick thread runs every 5 ms: it advances each model by the real elapsed
  monotonic time (capped at 0.1 s so a stall cannot explode the integration),
  polls the node (heartbeat, timers, delayed answers) and sends the frames.

Each node has its own lock, held while either thread touches it. An exception
inside a node is logged on the ``canopen_sim`` logger and that frame is
skipped; send errors are logged too. All sends share one bus-wide lock. Neither thread dies from bad input.

Bench YAML::

    bus: {interface: socketcan, channel: vcan0, bitrate: 500000}
    nodes:
      - {profile: generic-drive, id: 3, name: axis, heartbeat_ms: 500}
    control: {port: 47100}   # optional
"""
from __future__ import annotations

import io
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import can
import yaml

from .eds import export_eds
from .frames import Frame
from .node import Node
from .profile import ProfileError, build_node

log = logging.getLogger("canopen_sim")

TICK_INTERVAL = 0.005  # s
MAX_DT = 0.1  # s, cap for one model step
RECV_TIMEOUT = 0.05  # s
JOIN_TIMEOUT = 2.0  # s
_BENCH_KEYS = {"bus", "nodes", "control"}
_BUS_KEYS = {"interface", "channel", "bitrate"}


class BenchError(ValueError):
    """The bench could not be built, a node is unknown, or it could not start."""


def _default_channel(interface: str) -> str:
    if interface == "virtual":
        return f"canopen-sim-{uuid.uuid4().hex}"
    return "vcan0"


def _import_canopen() -> Any:
    try:
        import canopen
    except ImportError:
        raise RuntimeError("this needs the canopen package: pip install canopen") from None
    return canopen


class Bench:
    def __init__(self, nodes: Iterable[Node], *, profiles: dict[str, str] | None = None,
                 interface: str = "virtual", channel: str | None = None,
                 bitrate: int | None = None, control_port: int | None = None) -> None:
        self.nodes: dict[str, Node] = {}
        ids: dict[int, str] = {}
        for node in nodes:
            if node.name in self.nodes:
                raise BenchError(f"node name {node.name!r} used twice; give each node "
                                 "its own name")
            if node.node_id in ids:
                raise BenchError(f"node ID {node.node_id} used by {ids[node.node_id]!r} "
                                 f"and {node.name!r}")
            self.nodes[node.name] = node
            ids[node.node_id] = node.name
        if not self.nodes:
            raise BenchError("a bench needs at least one node")
        #: profile each node was built from (name of a bundled profile or a path)
        self.profiles: dict[str, str] = dict(profiles or {})
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        #: control port from the YAML or start(); 0 = ephemeral, the real port is
        #: recorded here once the control server runs. None = no control channel.
        self.control_port = control_port
        #: the control channel has no authentication: keep it on loopback.
        self.control_host = "127.0.0.1"
        self._control: Any = None
        self._locks = {name: threading.Lock() for name in self.nodes}
        self._bus: can.BusABC | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # python-can does not promise thread-safe sends for every backend: the
        # receive thread, the tick thread and callers all send through this lock
        # (recv stays outside it).
        self._send_lock = threading.Lock()
        self._networks: list[Any] = []  # canopen.Network objects from network()

    # -- construction -----------------------------------------------------

    @classmethod
    def single(cls, profile: str | Path | dict, node_id: int = 1,
               name: str | None = None) -> "Bench":
        try:
            node = build_node(profile, node_id=node_id, name=name)
        except ProfileError as error:
            raise BenchError(str(error)) from None
        label = profile if isinstance(profile, (str, Path)) else node.name
        return cls([node], profiles={node.name: str(label)})

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Bench":
        path = Path(path)
        try:
            with path.open(encoding="utf-8") as handle:
                spec = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as error:
            raise BenchError(f"cannot read bench {path}: {error}") from None
        return cls.from_dict(spec, base_dir=path.parent)

    @classmethod
    def from_dict(cls, spec: Any, base_dir: Path | None = None) -> "Bench":
        """Build from a bench mapping; relative profile paths resolve against
        ``base_dir`` (the YAML file's folder) when that file exists."""
        if not isinstance(spec, dict):
            raise BenchError("a bench must be a mapping with bus and nodes")
        unknown = set(spec) - _BENCH_KEYS
        if unknown:
            raise BenchError(f"unknown bench key(s) {', '.join(sorted(unknown))}")
        bus = spec.get("bus") or {}
        if not isinstance(bus, dict) or set(bus) - _BUS_KEYS:
            raise BenchError(f"bus must be a mapping with {', '.join(sorted(_BUS_KEYS))}")
        control = spec.get("control") or {}
        if not isinstance(control, dict) or set(control) - {"port"}:
            raise BenchError("control must be a mapping with port")
        entries = spec.get("nodes")
        if not isinstance(entries, list) or not entries:
            raise BenchError("nodes must be a non-empty list")
        nodes, profiles = [], {}
        for entry in entries:
            if not isinstance(entry, dict) or "profile" not in entry or "id" not in entry:
                raise BenchError("each node needs a profile and an id")
            overrides = {k: v for k, v in entry.items() if k not in ("id", "name")}
            profile = overrides["profile"]
            if base_dir is not None and isinstance(profile, str) \
                    and (base_dir / profile).is_file():
                overrides["profile"] = str(base_dir / profile)
            try:
                node = build_node(overrides, node_id=entry["id"], name=entry.get("name"))
            except ProfileError as error:
                raise BenchError(f"node {entry.get('name', entry['id'])!r}: {error}") \
                    from None
            nodes.append(node)
            profiles[node.name] = str(profile)
        return cls(nodes, profiles=profiles,
                   interface=str(bus.get("interface", "virtual")),
                   channel=None if bus.get("channel") is None else str(bus["channel"]),
                   bitrate=bus.get("bitrate"), control_port=control.get("port"))

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._bus is not None

    def start(self, *, interface: str | None = None, channel: str | None = None,
              control_port: int | None = None) -> None:
        """Open the bus, start the threads, then boot every node.

        ``interface`` defaults to the bench's own (from the YAML, else
        ``virtual``). ``channel`` defaults to the YAML channel when the
        interface is the YAML one, else ``vcan0`` for socketcan and a unique
        ``canopen-sim-<uuid>`` for virtual. ``control_port`` (0 = any free
        port) starts the JSON control channel on loopback; the port in use is
        then in ``bench.control_port``."""
        if self.running:
            raise BenchError("bench already started")
        configured = self.interface
        interface = interface or configured
        if channel is None:
            channel = self.channel if interface == configured and self.channel else \
                _default_channel(interface)
        kwargs: dict[str, Any] = {}
        if self.bitrate is not None and interface not in ("virtual", "socketcan"):
            kwargs["bitrate"] = self.bitrate
        try:
            bus = can.Bus(interface=interface, channel=channel,
                          receive_own_messages=False, **kwargs)
        except (can.CanError, OSError, ValueError, ImportError) as error:
            raise BenchError(f"cannot open {interface} bus {channel!r}: {error}") from None
        self.interface, self.channel = interface, channel
        if control_port is not None:
            self.control_port = control_port
        self._bus = bus
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._receive_loop, name="canopen-sim-rx", daemon=True),
            threading.Thread(target=self._tick_loop, name="canopen-sim-tick", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        now = time.monotonic()
        for name, node in self.nodes.items():
            self._run(name, lambda n=node: n.boot(now))
        if self.control_port is not None:
            from .control import ControlServer
            control = ControlServer(self, self.control_host, self.control_port)
            try:
                self.control_port = control.start()
            except OSError as error:
                self.stop()
                raise BenchError(f"cannot open control port {self.control_port}: "
                                 f"{error}") from None
            self._control = control

    def stop(self) -> None:
        """Stop the threads and shut the bus down; safe to call twice."""
        bus = self._bus
        if bus is None:
            return
        control, self._control = self._control, None
        if control is not None:
            control.stop()
        networks, self._networks = self._networks, []
        for network in networks:
            try:
                network.disconnect()
            except Exception:  # noqa: BLE001 - a client error must not mask stop()
                log.exception("network disconnect failed")
        self._stop.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(JOIN_TIMEOUT)
                if thread.is_alive():
                    log.warning("thread %s did not stop within %.1f s", thread.name,
                                JOIN_TIMEOUT)
        self._threads = []
        self._bus = None
        try:
            bus.shutdown()
        except Exception:  # noqa: BLE001 - a failing shutdown must not mask stop()
            log.exception("bus shutdown failed")

    def __enter__(self) -> "Bench":
        if not self.running:
            self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- threads ----------------------------------------------------------

    def _send(self, frames: list[Frame]) -> None:
        bus = self._bus
        if bus is None:
            return
        for frame in frames:
            try:
                with self._send_lock:
                    bus.send(can.Message(arbitration_id=frame.cob_id, data=frame.data,
                                         is_extended_id=False), timeout=RECV_TIMEOUT)
            except (can.CanError, OSError, ValueError) as error:
                log.error("cannot send COB-ID 0x%03X: %s", frame.cob_id, error)

    def _run(self, name: str, step) -> None:
        """Run ``step()`` under the node lock and send its frames; log errors."""
        with self._locks[name]:
            try:
                frames = step()
            except Exception:  # noqa: BLE001 - one bad frame or step must not stop the bench
                log.exception("node %r failed; frame or step skipped", name)
                return
            self._send(frames)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            bus = self._bus
            if bus is None:
                return
            try:
                msg = bus.recv(RECV_TIMEOUT)
            except Exception as error:  # noqa: BLE001 - keep serving after bus errors
                if self._stop.is_set():
                    return
                log.error("receive failed: %s", error)
                self._stop.wait(RECV_TIMEOUT)
                continue
            if msg is None or msg.is_extended_id or msg.is_remote_frame or msg.is_error_frame:
                continue
            frame = Frame(msg.arbitration_id, bytes(msg.data))
            now = time.monotonic()
            for name, node in self.nodes.items():
                self._run(name, lambda n=node: n.handle(frame, now)
                          if frame.cob_id in n.listen_ids() else [])

    def _tick_loop(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(TICK_INTERVAL):
            now = time.monotonic()
            dt = min(now - last, MAX_DT)
            last = now
            for name, node in self.nodes.items():
                self._run(name, lambda n=node: n.tick(dt, now))
                self._run(name, lambda n=node: n.poll(now))

    # -- canopen client ---------------------------------------------------

    def network(self) -> Any:
        """A ``canopen.Network`` connected to the bench's bus (needs the
        ``canopen`` package). It is disconnected when the bench stops."""
        canopen = _import_canopen()
        if not self.running:
            raise RuntimeError("start the bench before bench.network()")
        kwargs: dict[str, Any] = {}
        if self.bitrate is not None and self.interface not in ("virtual", "socketcan"):
            kwargs["bitrate"] = self.bitrate
        network = canopen.Network()
        network.NOTIFIER_CYCLE = 0.1  # faster disconnect than the 1 s default
        network.connect(interface=self.interface, channel=self.channel, **kwargs)
        self._networks.append(network)
        return network

    def remote_node(self, node: Any = None, *, cls: Any = None, network: Any = None) -> Any:
        """A canopen node (``cls``, default ``canopen.RemoteNode``; e.g.
        ``canopen.BaseNode402``) for one bench node, built from its exported EDS
        and added to ``network`` (default: the bench's first network, created
        on demand)."""
        canopen = _import_canopen()
        target = self.find(node)
        stream = io.StringIO(export_eds(target))
        stream.name = f"{target.name}.eds"  # canopen picks the format from the name
        od = canopen.import_od(stream, target.node_id)
        if network is None:
            network = self._networks[0] if self._networks else self.network()
        return network.add_node((cls or canopen.RemoteNode)(target.node_id, od))

    # -- access -----------------------------------------------------------

    def find(self, name_or_id: Any = None) -> Node:
        """A node by name or ID; ``None`` is allowed when the bench has one node."""
        if name_or_id is None:
            if len(self.nodes) == 1:
                return next(iter(self.nodes.values()))
            raise BenchError(f"node required: {', '.join(self.nodes)}")
        if isinstance(name_or_id, str) and name_or_id in self.nodes:
            return self.nodes[name_or_id]
        wanted = name_or_id
        if isinstance(wanted, str) and wanted.strip().isdigit():
            wanted = int(wanted)
        if isinstance(wanted, int) and not isinstance(wanted, bool):
            for node in self.nodes.values():
                if node.node_id == wanted:
                    return node
        raise BenchError(f"no node {name_or_id!r}; nodes: " + ", ".join(
            f"{n.name} (ID {n.node_id})" for n in self.nodes.values()))

    def _apply(self, node: Node, action) -> None:
        with self._locks[node.name]:
            frames = action(node)
            self._send(frames)

    def inject(self, kind: str, node: Any = None, **params: Any) -> None:
        """Inject a fault into one node (see :meth:`Node.inject`); bad kinds or
        parameters raise ``ValueError``."""
        target = self.find(node)
        self._apply(target, lambda n: n.inject(kind, time.monotonic(), **params))

    def clear(self, kind: str | None = None, node: Any = None) -> None:
        """Clear a fault (or all) in one node; with several nodes and no node
        named, ``clear()`` clears every fault on every node."""
        if node is None and kind is None:
            targets = list(self.nodes.values())
        else:
            targets = [self.find(node)]
        for target in targets:
            self._apply(target, lambda n: n.clear(kind, time.monotonic()))

    def variables(self, node: Any = None) -> dict[str, Any]:
        """Model variables of one node plus its ``nmt_state``."""
        target = self.find(node)
        with self._locks[target.name]:
            values = dict(target.model.variables()) if target.model is not None else {}
            values["nmt_state"] = target.state.name.lower()
        return values

    def status(self) -> dict[str, dict[str, Any]]:
        """Per node: id, profile, NMT state, active faults and EMCY codes (hex),
        read under the node lock."""
        out: dict[str, dict[str, Any]] = {}
        for name, node in self.nodes.items():
            with self._locks[name]:
                out[name] = {"id": node.node_id, "profile": self.profiles.get(name),
                             "nmt_state": node.state.name.lower(),
                             "faults": node.faults(),
                             "errors": [f"0x{code:04X}" for code in node.emcy.active()]}
        return out

    def describe(self) -> str:
        where = f"{self.interface} {self.channel or '(channel on start)'}"
        lines = [f"bus: {where}{'' if self.running else ' (not started)'}"]
        for name, node in self.nodes.items():
            with self._locks[name]:
                faults = node.faults()
                state = node.state.name.lower()
            profile = self.profiles.get(name, "?")
            lines.append(f"  {name}: node {node.node_id}, {profile}, {state}, "
                         f"faults: {', '.join(faults) or 'none'}")
        return "\n".join(lines)
