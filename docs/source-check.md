# Source check against the specifications and manuals

Ralf checks every item below by hand against the CiA PAS documents in
`~/Dokumente/CiA/` (CiA 301 v4.2.0, CiA 401-1 and 401-2 v3.1.0, CiA 406 v4.1.0) and the
manuals named in `src/canopen_sim/profiles/generic-drive.yaml`. Write results in your own
words and with values only: no text, tables or figures from the documents in this file or
anywhere in the repository. Every mismatch becomes a fix with a test before the release is
published. Line numbers refer to the 0.1.0 tag.

Each item: what the simulator does (file:line), then the fields to fill in.

## CiA 301: communication

### 1. Pre-defined connection set (COB-IDs)
Implemented: NMT 0x000, SYNC 0x080, EMCY 0x080+id, TPDO1..4 0x180/0x280/0x380/0x480+id,
RPDO1..4 0x200/0x300/0x400/0x500+id, SDO server to client 0x580+id, client to server
0x600+id, heartbeat 0x700+id; node IDs 1..127. `src/canopen_sim/frames.py:7-43`,
defaults of 0x1014 and 0x1200 in `comm_objects.py:26-28,52`.
- Checked: [ ]  Result:

### 2. NMT commands and states
Implemented: start 0x01, stop 0x02, pre-operational 0x80, reset node 0x81, reset
communication 0x82 (`node.py:24-25`, handled at `node.py:163-176`); state bytes boot-up
0x00, stopped 0x04, operational 0x05, pre-operational 0x7F (`node.py:28-32`).
- Checked: [ ]  Result:

### 3. Heartbeat and boot-up
Implemented: heartbeat producer time 0x1017 in milliseconds, 0 disables it
(`node.py:138-140,193`); boot-up message is a heartbeat frame with state 0x00 after reset.
- Checked: [ ]  Result:

### 4. SDO command bytes
Implemented, server side: initiate upload response 0x41 (size indicated) and 0x40 | n<<2 |
0x03 (expedited) (`sdo.py:131,137`); initiate download response 0x60 (`sdo.py:159`);
segment responses 0x00/0x20 with toggle bit 0x10 (`sdo.py:189,306`); abort 0x80
(`sdo.py:48`); block download response 0xA4 initiate, 0xA2 sub-block ack, 0xA1 end
(`sdo.py:225,253,266`); block upload initiate 0xC6, end 0xC1 | n<<2 with CRC-16/XMODEM,
start 0xA3 from the client (`sdo.py:22,28,298,322`). Command specifier dispatch at
`sdo.py:79-99`; timeout 1 s (`sdo.py:11,104`).
- Checked: [ ]  Result:

### 5. SDO abort codes used
Every code defined in `od.py:10-32` and where it is raised:

| code | name in the code | raised at |
|---|---|---|
| 0x05030000 | TOGGLE | `sdo.py:141,184` |
| 0x05040000 | TIMEOUT | `sdo.py:105` |
| 0x05040001 | BAD_COMMAND | `sdo.py:99,208,283` |
| 0x05040002 | BAD_BLOCK_SIZE | `sdo.py:289,316` |
| 0x05040003 | BAD_SEQUENCE | `sdo.py:314` |
| 0x05040004 | CRC_ERROR | `sdo.py:263` |
| 0x06010000 | UNSUPPORTED | defined at `od.py:16`, not raised anywhere |
| 0x06010001 | WRITE_ONLY | `od.py:155` |
| 0x06010002 | READ_ONLY | `od.py:161,176` |
| 0x06020000 | NO_OBJECT | `od.py:120` |
| 0x06040041 | NOT_MAPPABLE | `comm_objects.py:129` |
| 0x06040042 | PDO_LENGTH | `comm_objects.py:134,138,141` |
| 0x06040043 | INCOMPATIBLE | `comm_objects.py:115,120,146` |
| 0x06070010 | LENGTH_MISMATCH | `sdo.py:194,261` |
| 0x06070012 | TOO_LONG | `sdo.py:172,188,218,252,259`, `od.py:167` |
| 0x06070013 | TOO_SHORT | `od.py:165` |
| 0x06090011 | NO_SUBINDEX | `od.py:120` |
| 0x06090030 | RANGE | `models/drive.py:128,134` |
| 0x06090031 | TOO_HIGH | `od.py:182` |
| 0x06090032 | TOO_LOW | `od.py:184` |
| 0x08000000 | GENERAL | default of the `sdo_abort` fault, `faults.py:CATALOG` |
| 0x08000020 | CANNOT_STORE | `node.py:202,207` |
| 0x08000022 | DEVICE_STATE | defined at `od.py:32`, not raised anywhere |

For each: is the value right, and is it the right code for that situation?
- Checked: [ ]  Result:

### 6. PDO transmission types
Implemented: type 0 (synchronous, acyclic), 1 to 240 (every n-th SYNC), 254/255 (event,
with inhibit time in 100 us and event timer in ms); RPDO synchronous types hold data until
SYNC (`pdo.py:111-141`). Defaults in the profiles use 255, 1 and event timers.
- Checked: [ ]  Result:

### 7. PDO mapping procedure and communication parameters
Implemented: mapping is changed by writing 0 to sub-index 0, writing the entries, then
writing the count; errors answer 0x06040041, 0x06040042 and 0x06040043
(`comm_objects.py:100-146`). Mapping entry layout is index(16), sub-index(8),
length in bits(8) (`pdo.py:62-66`).
- Checked: [ ]  Result:

### 8. EMCY 0x8210 and 0x8250
Implemented: 0x8210 when an RPDO is shorter than its mapping, 0x8250 on RPDO timeout, both
with the communication bit of the error register (`pdo.py:16-17,150,166`).
- Checked: [ ]  Result:

### 9. EMCY frame layout and reset behaviour
Implemented: frame = code(16, little endian), error register(8), 5 bytes manufacturer data
(`emcy.py:37-46`). Clearing the last error sends code 0x0000 with register 0; clearing
one of several sends 0x0000 **with the remaining register** (`emcy.py:48-62`). Check this
against the EMCY state machine: should a partial clear send 0x0000 with the remaining
register, or something else? Also: error register bits (generic 0x01, current 0x02,
voltage 0x04, temperature 0x08, communication 0x10, manufacturer 0x80, `emcy.py:17-22`),
pre-defined error field 0x1003 (newest first, write 0 clears, `emcy.py:94-106`), inhibit
time 0x1015 in 100 us (`emcy.py:117`).
- Checked: [ ]  Result:

### 10. Mandatory and common objects 0x1000 to 0x1018
Implemented: 0x1000, 0x1001, 0x1003 (+ sub-indices), 0x1005 (0x80), 0x1008, 0x1010,
0x1011 (sub 1 only; signatures `save` 0x65766173 and `load` 0x64616F6C, `node.py:21-22`),
0x1014, 0x1015, 0x1017, 0x1018 (4 sub-indices) at `comm_objects.py:41-59`. Data types and
access (const/ro/rw) per entry. Is anything mandatory missing (0x1016, 0x1012, 0x1019)?
- Checked: [ ]  Result:

### 11. Vendor ID 0 for a simulator
Implemented: default 0 (`comm_objects.py:56`, profile identity `vendor_id: 0x00000000`).
Is 0 acceptable or reserved? Should a simulator use something else, and who assigns it?
- Checked: [ ]  Result:

## CiA 401: generic I/O (generic-io)

### 12. Object indices and data types
Implemented: 0x6000 read digital input 8-bit (U8, ro), 0x6200 write digital output 8-bit
(U8, rw), 0x6401 read analog input 16-bit (INTEGER16, ro), 0x6411 write analog output
16-bit (INTEGER16, rw), each as array with highest sub-index (`models/io.py:60-67`).
Device type 0x000F0191 (`profiles/generic-io.yaml:7`). Default PDO objects and loopback
behaviour (`profiles/generic-io.yaml:17-31`).
- Checked: [ ]  Result:

### 13. EMCY codes for I/O faults
Implemented: short circuit 0x2320 (current bit), over range 0x3110 (voltage bit), wire
break 0xFF01 (manufacturer-specific, no PAS code; `models/io.py:15-17,138-143`). Check
the first two against the PAS and confirm that the PAS names no wire-break code.
- Checked: [ ]  Result:

## CiA 406: encoder (generic-encoder)

### 14. Object indices, types and device type
Implemented: 0x6000 operating parameters (U16, bit 0 = counting direction), 0x6003 preset
(U32), 0x6004 position value (U32), 0x6030 speed value sub-index 1 (INTEGER16)
(`models/encoder.py:29-44`). Device type 0x00020196 (`profiles/generic-encoder.yaml:7`).
Also check the 4096 steps per revolution as a resolution/range convention.
- Checked: [ ]  Result:

### 15. EMCY codes 0x5050 and 0x5051
Implemented: single-turn failure 0x5050, multi-turn failure 0x5051, generic bit of the
error register (`models/encoder.py:11-12,94`).
- Checked: [ ]  Result:

## Drive (generic-drive), against the manuals

### 16. Drive objects and 0x6502
Implemented: 0x603F, 0x6040, 0x6041, 0x6060, 0x6061, 0x6064, 0x6065, 0x606C, 0x607A,
0x6081, 0x6083, 0x6084, 0x6098, 0x60F4, 0x60FF, 0x6502 (`models/drive.py:96-114`).
0x6502 reads 0x25 (profile position bit 0, profile velocity bit 2, homing bit 5;
`models/drive.py:43`). Device type 0x00020192 (`profiles/generic-drive.yaml:7`). Check
indices, types, units and the 0x6502 bit assignment in each manual.
- Checked: [ ]  Result:

### 17. Statusword patterns and the power state machine
Implemented: state pattern values per state 0x0000, 0x0040, 0x0021, 0x0023, 0x0027,
0x0007, 0x000F, 0x0008 (not ready, switch on disabled, ready, switched on, operation
enabled, quick stop, fault reaction, fault) plus remote bit 0x0200, target reached 0x0400,
set-point acknowledge / homing attained 0x1000 (`models/drive.py:51-59`). Controlword
edges: new set-point bit 4, relative bit 6, fault reset bit 7 (`models/drive.py:61-63`).
Check against each manual; note any differences between the three.
- Checked: [ ]  Result:

### 18. Drive EMCY codes
Implemented: over temperature 0x4310 (temperature bit), over current 0x2310 (current bit),
following error 0x8611 (manufacturer bit) (`models/drive.py:34-36,68-73`); also written to
0x603F.
- Checked: [ ]  Result:

### 19. Manual editions for `generic-drive.yaml` `sources`
`src/canopen_sim/profiles/generic-drive.yaml:37-39` currently says "edition: to be
confirmed" for all three. Fill in the exact edition (title, version, date) of each manual
that was used, and update the file and the README text.
- Kollmorgen AKD CANopen manual. Edition:
- SEW-EURODRIVE CANopen fieldbus manual. Edition:
- Synapticon SOMANET object dictionary. Edition:
- Checked: [ ]  Result:
