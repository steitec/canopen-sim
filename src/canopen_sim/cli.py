"""Command line for canopen-sim.

    canopen-sim list                                  # the bundled device profiles
    canopen-sim map generic-drive                     # objects, default PDOs, sources, limits
    canopen-sim serve --profile generic-drive --id 3  # one node on a virtual bus
    canopen-sim serve --bench bench.yaml --interface socketcan --channel vcan0
    canopen-sim inject over_temperature               # fault injection (control channel)
    canopen-sim clear                                 # clear all faults
    canopen-sim state                                 # nodes, NMT states, faults, EMCY codes
    canopen-sim export-eds generic-drive -o drive.eds --id 3

Exit codes: 0 ok, 1 the control channel answered ok:false, 2 usage, bench or profile
error, 3 control port unreachable.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .bench import Bench, BenchError
from .control import CONTROL_PORT, send_control
from .eds import export_eds
from .profile import ProfileError, build_node, bundled_profiles, resolve_profile

PROG = "canopen-sim"
_INVALID = 0x80000000


# --- helpers ------------------------------------------------------------------------

def _coerce(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    for convert in (lambda v: int(v, 0), float):
        try:
            return convert(value)
        except ValueError:
            continue
    return value


def _params(pairs: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"{PROG}: parameter {pair!r} must be name=value")
        name, value = pair.split("=", 1)
        params[name] = _coerce(value)
    return params


def _error(message: object) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def _first_line(text: object) -> str:
    return " ".join(str(text or "").split())


# --- commands -----------------------------------------------------------------------

def _cmd_list(args) -> int:
    for name in bundled_profiles():
        spec = resolve_profile(name)
        print(f"{name:20}{_first_line(spec.get('description'))}")
    return 0


def _default_text(var) -> str:
    value = var.value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex() or "-"
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9:
        return hex(value) if value >= 0 else str(value)
    return str(value)


def _pdo_lines(node) -> list[str]:
    lines = []
    for label, pdos in (("TPDO", node.pdo.tpdos), ("RPDO", node.pdo.rpdos)):
        for pdo in pdos:
            number = (pdo.comm & 0x1FF) + 1
            raw = node.od.get(pdo.comm, 1)
            cob = "invalid" if raw & _INVALID else f"0x{raw & 0x7FF:03X}"
            kind = node.od.get(pdo.comm, 2)
            mapped = []
            for sub in range(1, node.od.get(pdo.mapping, 0) + 1):
                entry = node.od.get(pdo.mapping, sub)
                mapped.append(f"0x{entry >> 16:04X}:{(entry >> 8) & 0xFF}/{entry & 0xFF}")
            lines.append(f"  {label}{number}  COB-ID {cob}  type {kind}  "
                         f"{', '.join(mapped) or '(nothing mapped)'}")
    return lines


def _cmd_map(args) -> int:
    try:
        spec = resolve_profile(args.profile)
        node = build_node(args.profile, node_id=args.id)
    except ProfileError as error:
        _error(error)
        return 2
    print(f"{spec.get('name', args.profile)}: {_first_line(spec.get('description'))}")
    print(f"\nObjects (node ID {args.id})")
    print(f"  {'index:sub':11}{'name':34}{'type':14}{'access':7}{'default':14}pdo")
    for var in sorted(node.od.entries(), key=lambda v: (v.index, v.sub)):
        print(f"  {f'0x{var.index:04X}:{var.sub}':11}{var.name[:32]:34}{var.dtype.name:14}"
              f"{var.access:7}{_default_text(var):14}{'pdo' if var.pdo_mappable else '-'}")
    pdos = _pdo_lines(node)
    if pdos:
        print("\nDefault PDO mappings")
        for line in pdos:
            print(line)
    for title, lines in (("Sources", spec.get("sources")), ("Limits", spec.get("limits"))):
        if lines:
            print(f"\n{title}")
            for line in lines:
                print(f"  - {line}")
    return 0


def _cmd_export_eds(args) -> int:
    try:
        node = build_node(args.profile, node_id=args.id)
    except ProfileError as error:
        _error(error)
        return 2
    if args.output:
        try:
            export_eds(node, Path(args.output))
        except OSError as error:
            _error(error)
            return 2
    else:
        sys.stdout.write(export_eds(node))
    return 0


def _cmd_serve(args, stop: threading.Event | None = None) -> int:
    if bool(args.bench) == bool(args.profile):
        _error("serve needs exactly one of --bench FILE or --profile NAME")
        return 2
    try:
        bench = (Bench.from_yaml(args.bench) if args.bench
                 else Bench.single(args.profile, node_id=args.id))
        bench.start(interface=args.interface, channel=args.channel,
                    control_port=args.control_port)
    except BenchError as error:
        _error(error)
        return 2
    stop = stop or threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):  # not in the main thread / not on this platform
            pass
    try:
        for name, node in bench.nodes.items():
            print(f"{name}  id {node.node_id}  {bench.profiles.get(name, '?')}  "
                  f"{bench.channel}", flush=True)
        if bench.control_port is not None:
            print(f"control {bench.control_host}:{bench.control_port}", flush=True)
        while not stop.wait(0.5):
            pass
    finally:
        bench.stop()
    return 0


def _control_call(args, request: dict) -> dict | int:
    if args.node is not None:
        request["node"] = args.node
    try:
        answer = send_control(request, host=args.control_host, port=args.control_port)
    except (OSError, ValueError) as error:
        _error(f"control channel {args.control_host}:{args.control_port}: {error}")
        return 3
    if not answer.get("ok"):
        _error(answer.get("error", "request failed"))
        return 1
    return answer


def _cmd_inject(args) -> int:
    answer = _control_call(args, {"inject": args.kind, "params": _params(args.params)})
    return answer if isinstance(answer, int) else 0


def _cmd_clear(args) -> int:
    answer = _control_call(args, {"clear": args.kind if args.kind else True})
    return answer if isinstance(answer, int) else 0


def _cmd_state(args) -> int:
    answer = _control_call(args, {"list": True})
    if isinstance(answer, int):
        return answer
    nodes = answer["nodes"]
    if args.json:
        print(json.dumps(nodes, indent=2))
        return 0
    print(f"{'node':16}{'id':>4}  {'profile':18}{'nmt_state':17}{'faults':24}errors")
    for name, info in nodes.items():
        print(f"{name:16}{info['id']:>4}  {str(info['profile']):18}{info['nmt_state']:17}"
              f"{', '.join(info['faults']) or '-':24}{', '.join(info['errors']) or '-'}")
    return 0


# --- parser -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Simulated CANopen devices.")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_control(p):
        p.add_argument("--control-host", default="127.0.0.1")
        p.add_argument("--control-port", type=int, default=CONTROL_PORT)
        p.add_argument("--node", help="node name or ID (needed when the bench has several)")

    p = sub.add_parser("list", help="list the bundled profiles")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("map", help="objects, default PDO mappings, sources and limits")
    p.add_argument("profile", help="bundled profile name or YAML file")
    p.add_argument("--id", type=int, default=1, help="node ID (default 1)")
    p.set_defaults(func=_cmd_map)

    p = sub.add_parser("export-eds", help="write a profile's EDS file")
    p.add_argument("profile", help="bundled profile name or YAML file")
    p.add_argument("-o", "--output", help="output file (default: standard output)")
    p.add_argument("--id", type=int, default=1, help="node ID (default 1)")
    p.set_defaults(func=_cmd_export_eds)

    p = sub.add_parser("serve", help="run a bench and serve CANopen nodes")
    p.add_argument("--bench", help="bench YAML file")
    p.add_argument("--profile", help="bundled profile name or YAML file (one node)")
    p.add_argument("--id", type=int, default=1, help="node ID with --profile (default 1)")
    p.add_argument("--interface", choices=["socketcan", "virtual"],
                   help="python-can interface (default: the bench's own, else virtual)")
    p.add_argument("--channel", help="bus channel (default vcan0 for socketcan)")
    p.add_argument("--control-port", type=int, default=CONTROL_PORT,
                   help=f"control channel port on 127.0.0.1 (default {CONTROL_PORT}; "
                        "0 = any free port)")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("inject", help="inject a fault via the control channel")
    p.add_argument("kind")
    p.add_argument("params", nargs="*", metavar="key=value")
    add_control(p)
    p.set_defaults(func=_cmd_inject)

    p = sub.add_parser("clear", help="clear a fault (or all)")
    p.add_argument("kind", nargs="?")
    add_control(p)
    p.set_defaults(func=_cmd_clear)

    p = sub.add_parser("state", help="nodes, NMT states, faults and EMCY codes")
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    add_control(p)
    p.set_defaults(func=_cmd_state)

    return parser


def main(argv: list[str] | None = None, stop: threading.Event | None = None) -> int:
    """Run the CLI. ``stop`` (tests) ends ``serve`` like SIGINT does."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is _cmd_serve:
        return _cmd_serve(args, stop)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
