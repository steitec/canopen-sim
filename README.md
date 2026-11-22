<a href="https://steitec.net"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-dark.svg">
  <img align="right" height="32" alt="steitec" src="https://raw.githubusercontent.com/steitec/.github/main/brand/steitec-logo-on-light.svg">
</picture></a>

# canopen-sim

[![CI](https://github.com/steitec/canopen-sim/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/steitec/canopen-sim/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/canopen-sim?v=0.1.1)](https://pypi.org/project/canopen-sim/)
[![Python](https://img.shields.io/pypi/pyversions/canopen-sim?v=0.1.1)](https://pypi.org/project/canopen-sim/)
[![License](https://img.shields.io/pypi/l/canopen-sim?v=0.1.1)](https://github.com/steitec/canopen-sim/blob/main/LICENSE)

Simulated **CANopen** devices that **behave**: a servo drive that walks through its power
state machine, ramps to a target velocity and can be put into a fault by injecting an over-temperature fault, which you clear
on the control channel and then acknowledge with a fault reset; an I/O module whose inputs follow its outputs or a signal you define; a
multi-turn encoder that counts. They run on a virtual CAN bus (any platform) or on a real
SocketCAN interface such as `vcan0` (Linux), and speak NMT with heartbeat, SDO
(expedited, segmented and block), PDOs (event, timer and SYNC driven, with mapping), SYNC
and EMCY. So you can develop and test a CANopen master, a gateway or your own
commissioning scripts without a drive on the bench, also in CI.

The test suite cross-checks the simulator against the independent
[canopen](https://github.com/christiansandberg/canopen) Python library, including a full
drive commissioning with its `BaseNode402` (enable, run a velocity, fault, reset, run again).

```console
$ canopen-sim serve --profile generic-drive --id 3 --interface socketcan --channel vcan0 &
generic-drive  id 3  generic-drive  vcan0
control 127.0.0.1:47100
$ canopen-sim state
node              id  profile           nmt_state        faults                  errors
generic-drive      3  generic-drive     pre_operational  -                       -
$ canopen-sim inject over_temperature     # the drive goes to FAULT and sends EMCY 0x4310
$ canopen-sim clear
```

## Install

Requires Python 3.10 or newer. Runtime dependencies: `python-can` and `PyYAML`.

```bash
pip install canopen-sim
```

From source, with the test dependencies (including `canopen` for the cross-checks):

```bash
git clone https://github.com/steitec/canopen-sim
cd canopen-sim
pip install -e '.[dev]'
pytest
```

## Quick start

On Linux, set up a virtual SocketCAN interface once per boot:

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

Then serve a device on it and watch the traffic with `candump` from `can-utils`:

```bash
canopen-sim serve --profile generic-drive --id 3 --interface socketcan --channel vcan0
candump vcan0                                   # in a second terminal
```

Without `--interface`, `serve` runs on python-can's `virtual` bus, which exists only
inside one process: `candump`, another process or another tool cannot see it (`serve`
prints a note saying so). It is what the pytest fixture and `Bench` in Python use. To
reach a node from other processes you need `--interface socketcan`, on Linux. On Windows
and macOS only the virtual bus is available. A bench with several devices
(or a fixed bus) is a YAML file:

```yaml
# bench.yaml
bus: {interface: socketcan, channel: vcan0}
nodes:
  - {profile: generic-drive, id: 3, name: axis, heartbeat_ms: 500}
  - {profile: generic-io, id: 5, name: io}
control: {port: 47100}          # optional; 127.0.0.1 only
```

```bash
canopen-sim serve --bench bench.yaml
```

The control channel (default port 47100) is for fault injection and has **no
authentication**. It binds to the loopback interface only; keep it that way.

## Test a master with pytest

The package installs a pytest plugin. The `canopen_bench` fixture starts a bench for each
test on its own virtual channel; `remote_node()` gives you a node of the `canopen` library
built from the exported EDS:

```python
import canopen


def test_drive_runs_faults_and_recovers(canopen_bench):
    drive = canopen_bench.remote_node("axis", cls=canopen.BaseNode402)
    drive.nmt.state = "PRE-OPERATIONAL"
    drive.setup_402_state_machine()            # reads the PDO layout via SDO
    drive.nmt.state = "OPERATIONAL"
    ...
    canopen_bench.inject("over_temperature")
    ...                                        # wait for drive.state == "FAULT"
    assert [error.code for error in drive.emcy.active] == [0x4310]
    canopen_bench.clear()
    drive.state = "SWITCH ON DISABLED"         # fault reset
```

The complete, runnable version is [`examples/test_example.py`](examples/test_example.py).
Point the fixture at your own bench with `--canopen-bench FILE` or the ini key
`canopen_bench`; without either you get one `generic-drive` on node 1.
`canopen_bench_factory` starts more benches from a file, a dict or a profile name. The
`canopen` package is needed only for `remote_node()` and `network()`.

## Faults

Inject a fault at runtime with `canopen-sim inject KIND [key=value ...] [--node NAME]`,
over the control channel, or with `bench.inject(...)` in Python. `clear [KIND]` removes
one fault or all. Parameters go as `key=value`; `count=N` limits a catalogue fault to N
firings.

**Catalogue (every node):**

| fault | parameters | effect |
|---|---|---|
| `mute` | | the node sends nothing at all |
| `no_heartbeat` | | heartbeat frames are suppressed |
| `restart` | | one-shot: the node resets now (boot-up message follows) |
| `double_bootup` | | one-shot: the next boot-up message goes out twice |
| `sdo_abort` | `code` (default 0x08000000), `index` | requests (to that index) are answered with this SDO abort code |
| `sdo_delay` | `seconds` (default 0.5) | SDO answers come late |
| `sdo_drop` | | a request gets no answer, the server state is unchanged |
| `sdo_bad_toggle` | | a segment answer has the toggle bit inverted |
| `pdo_dropout` | `every` (default 2), `pdo` | every n-th TPDO frame is lost |
| `pdo_bad_length` | `length`, `pdo` | TPDO frames are truncated |
| `pdo_freeze` | `pdo` | the TPDO keeps the data of the moment of injection |
| `emcy` | `code` (default 0xFF00), `register` (default 0x80), `data` (hex, up to 5 bytes) | the node sends this EMCY and keeps it active until cleared |

`pdo` selects TPDO 1 to 4; without it the fault applies to all.

**Drive (`generic-drive`):**

| fault | EMCY code | error register bit | effect |
|---|---|---|---|
| `over_temperature` | 0x4310 | temperature | enters the fault reaction, then FAULT; error code 0x603F set |
| `over_current` | 0x2220 | current | the same |
| `following_error` | 0x8611 | manufacturer-specific | the same; also raised by the model when the following error exceeds 0x6065 |

`clear` removes the cause and sends EMCY 0x0000; the drive leaves FAULT on the next
rising edge of controlword bit 7, like a real one.

**I/O module (`generic-io`):** parameters `channel=N` (1-based, default 1) and
`type=digital|analog`.

| fault | EMCY code | effect |
|---|---|---|
| `wire_break` | 0xFF01 (manufacturer-specific) | the input reads 0 |
| `short_circuit` | 0x2320 | a digital input reads 1, an analog input full scale |
| `over_range` | 0x3110 | an analog input reads full scale (`type=analog` only) |

**Encoder (`generic-encoder`):**

| fault | parameters | effect |
|---|---|---|
| `sensor_error` | | the position stops; EMCY 0x5051 (multi-turn) or 0x5050 (single-turn) |
| `position_jump` | `delta=<units>` | the position jumps once; no EMCY |
| `freeze` | | the position stops; no EMCY |

## Profiles

```console
$ canopen-sim list
$ canopen-sim map generic-drive          # objects, default PDO mappings, sources and limits
$ canopen-sim export-eds generic-drive --id 3 -o drive.eds
```

| profile | what it is | device type | default PDOs |
|---|---|---|---|
| `generic-drive` | servo drive: power state machine, profile velocity, profile position, homing (methods 35 and 37), increments and increments/s; object set as documented in public manufacturer manuals | 0x00020192 | TPDO1 statusword + velocity actual, TPDO2 statusword + position actual; RPDO1 controlword + target velocity, RPDO2 controlword + target position |
| `generic-io` | 8 digital inputs, 8 digital outputs, 2 analog inputs, 2 analog outputs; outputs loop back to the inputs unless a signal or a fault drives them | 0x000F0191 | TPDO1 digital inputs, TPDO2 analog inputs; RPDO1 digital outputs, RPDO2 analog outputs |
| `generic-encoder` | multi-turn rotary encoder, 4096 steps per revolution, position integrated from a set speed; preset and counting direction | 0x00020196 | TPDO1 position (timer 100 ms and on change), TPDO2 position + speed (every SYNC) |

Each profile names what it follows in `sources` and states what it does not do in
`limits` (shown by `canopen-sim map`):

- **generic-io** follows CiA 401-1 v3.1.0 (PAS). Limits: only 8-bit digital and
  16-bit analog objects, no polarity, filter or interrupt-mask objects (except the analog
  global interrupt enable below); wire break uses a manufacturer-specific EMCY code because
  CiA 401 defines none. Analog input interrupt: object 0x6423 (BOOLEAN, rw, PDO-mappable)
  defaults to FALSE, so changes of the analog inputs do not trigger event-driven TPDOs
  until a master sets it TRUE (digital inputs are not affected; event timer and SYNC
  transmissions still work). When the node enters OPERATIONAL while 0x6423 is FALSE it
  sends the warning EMCY 0x0080 (error register generic bit) and stays OPERATIONAL; the
  warning is cleared (EMCY 0x0000) on the next model tick after 0x6423 is set TRUE. The
  event TPDOs sent once on entering OPERATIONAL include the analog values. An NMT reset
  forgets the warning.
- **generic-encoder** follows CiA 406 v4.1.0 (PAS) and is modelled as class C1: operating
  parameters, resolution, preset, position and speed plus the C1 objects 0x6500 (operating
  status, mirrors the code-sequence bit), 0x6501 (single-turn resolution) and 0x6502
  (distinguishable revolutions: 65536 for multi-turn, 1 for single-turn, a fixed simulator
  value; the position wraps at resolution times 65536, capped at 2^32). No alarms or
  warnings objects, no total measuring range (0x6002), no cyclic timer (0x6200), no cams or diagnostics; no
  acceleration, jitter or quantisation noise. The speed value 0x6030:1 is in steps per
  second (the configured rpm times the resolution divided by 60) and is clamped to
  INTEGER16, so fast or high-resolution settings saturate at -32768..32767. The device type
  is derived from the model (0x00020196 multi-turn, 0x00010196 single-turn) unless the
  profile sets `device_type` explicitly.
- **generic-drive** follows the CANopen manuals of Kollmorgen (AKD), SEW-EURODRIVE and
  Synapticon (SOMANET) as published, **not CiA 402 itself** (editions listed in
  `docs/source-check.md`). Limits: no load or motor model, the following error is a fixed
  lag (20 ms times the velocity), no halt bit (controlword bit 8), no cyclic synchronous
  modes, only homing methods 35 and 37; temperature and current faults are injected, not
  computed.

Every device has the mandatory communication objects (0x1000 to 0x1018, including
store/restore with the `save`/`load` signatures, in memory only) and the vendor ID is 0
unless a profile overrides it. Vendor ID 0 means no assigned CANopen vendor ID, which
CiA 301 treats as invalid; set your own in a profile override if your master needs one.

Describe your own device in a YAML profile (see `src/canopen_sim/profiles/` for the
format) and pass it to `--profile` or a bench.

## CLI reference

| command | what it does |
|---|---|
| `canopen-sim list` | list the bundled profiles |
| `canopen-sim map PROFILE [--id N]` | objects, default PDO mappings, sources and limits of a profile or YAML file |
| `canopen-sim export-eds PROFILE [--id N] [-o FILE]` | write the profile's EDS file |
| `canopen-sim serve (--bench FILE \| --profile NAME [--id N]) [--interface socketcan\|virtual] [--channel NAME] [--control-port N]` | run a bench until SIGINT/SIGTERM |
| `canopen-sim inject KIND [key=value ...] [--node NODE]` | inject a fault (see [Faults](#faults)) |
| `canopen-sim clear [KIND] [--node NODE]` | clear a fault, or all |
| `canopen-sim state [--json] [--node NODE]` | nodes, NMT states, faults and EMCY codes |

`inject`, `clear` and `state` also take `--control-host` and `--control-port`
(default 127.0.0.1:47100). `--node` is needed when the bench has several nodes. The
control protocol is one JSON object per line over TCP (`inject`, `clear`, `get`, `list`,
plus a `node` key); see `src/canopen_sim/control.py`.

## Honest limits

- **Not certified.** canopen-sim is not certified by CiA and has not passed a
  conformance test.
- **Not tested against real devices.** It was cross-checked against the `canopen` Python
  library only, and checked by hand against public specifications and manuals.
- **A virtual CAN bus is not a CAN bus.** `vcan` has no arbitration, no bit errors and no
  real bus-off; the virtual python-can bus does not either. Timing is not real-time: frames
  are produced by Python threads every few milliseconds.
- **The drive follows manufacturer manuals, not CiA 402.** Its object set and
  statusword behaviour come from public manuals of real drives; where they differ from
  the standard, the manuals won.
- Nodes inside one bench do not receive each other's frames: the bench has one bus handle
  and no internal loopback. Use a separate bus handle (`bench.network()`, another process on socketcan, or a real master) to talk to a node.
- The drive's following error is a fixed-lag model, and the halt bit (controlword bit 8)
  is not implemented. Statusword bit 4 (voltage enabled) is not reported.
- The I/O wire-break fault uses the manufacturer-specific EMCY code 0xFF01; CiA 401
  defines none.
- EDS export is checked by importing it with the `canopen` Python library, not against
  CiA 306.
- The control channel has no authentication. It binds to the loopback interface; do not
  expose it.
- J1939, CANopen FD, LSS, node guarding and CiA 302 (network management) are out of scope.

## Trademarks and licence

CANopen® and CiA® are registered trademarks of CAN in Automation (CiA) e.V. This project is not certified by or affiliated with CiA.

MIT, see [LICENSE](LICENSE). `python-can` is licensed LGPL-3.0 and stays a separate package
that canopen-sim imports; it is not bundled or modified here.
