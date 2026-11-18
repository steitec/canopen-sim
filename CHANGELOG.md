# Changelog

## 0.1.0 — 2026-11-18

First release.

- CANopen device simulation on python-can: `virtual` bus on every platform, SocketCAN
  (`vcan0`) on Linux.
- NMT with heartbeat and boot-up, SDO server (expedited, segmented, block), PDOs
  (event, timer and SYNC driven, with mapping), SYNC, EMCY with error register and
  history, store/restore in memory.
- Three bundled profiles: `generic-drive` (power state machine, profile velocity and
  position, homing), `generic-io` (digital and analog I/O), `generic-encoder` (multi-turn).
  Each lists its `sources` and `limits`; custom devices from YAML profiles and benches.
- Fault injection: node, SDO, PDO and EMCY catalogue plus drive, I/O and encoder faults,
  switchable at runtime over a loopback JSON control channel.
- CLI (`list`, `map`, `export-eds`, `serve`, `inject`, `clear`, `state`), EDS export (checked by
  importing it with `canopen`), pytest plugin with `canopen_bench` fixtures.
- Cross-checked against the `canopen` library, including drive commissioning with
  `BaseNode402`; CI on Linux, Windows and macOS plus a `vcan0` job.
