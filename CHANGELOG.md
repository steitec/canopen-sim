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
- Source check against the CiA PAS documents:
  - PDO COB-ID changes while valid and reserved transmission types (241 to 253) abort with
    0x06090030.
  - 0x1003 entries beyond the recorded error count abort with 0x08000024.
  - generic-io: analog input global interrupt enable 0x6423 (default FALSE) gates
    change-triggered analog TPDOs; entering OPERATIONAL while FALSE sends EMCY 0x0080,
    cleared when 0x6423 is set TRUE; `sources` lists CiA 401-1 only.
  - generic-encoder: speed value in steps per second (clamped to INTEGER16), class C1
    objects 0x6500, 0x6501 and 0x6502, device type derived from multi-turn or single-turn.
  - generic-drive: `over_current` uses EMCY / 0x603F code 0x2220 (device internal continuous
    over current, as in the public Synapticon SOMANET error list) instead of 0x2310.
  - Vendor ID 0 is documented as "no assigned vendor ID" in the README and the profiles.
- Cross-checked against the `canopen` library, including drive commissioning with
  `BaseNode402`; CI on Linux, Windows and macOS plus a `vcan0` job.
