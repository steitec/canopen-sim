# Source check

The values the simulator uses were checked by hand on 2026-11-18 against the CiA PAS
documents CiA 301 v4.2.0, CiA 401-1 v3.1.0 and CiA 406 v4.1.0 (downloaded free of charge
from CiA after registration; not included here) and against public drive manuals. This file
records the result in our own words and with values only; it contains no text, tables or
figures from those documents.

Result: the items below match, or were changed to match, before the 0.1.0 release. Changes
made because of this check are listed in the CHANGELOG under "Source check".

## CiA 301: communication

| # | Item | Result |
|---|---|---|
| 1 | Pre-defined connection set: NMT 0x000, SYNC 0x080, EMCY 0x080+id, TPDO1–4 0x180/0x280/0x380/0x480+id, RPDO1–4 0x200/0x300/0x400/0x500+id, SDO 0x580/0x600+id, heartbeat 0x700+id, node IDs 1–127 | matches |
| 2 | NMT command specifiers 0x01, 0x02, 0x80, 0x81, 0x82; node ID 0 addresses all nodes | matches |
| 3 | Heartbeat states 0x00 boot-up, 0x04 stopped, 0x05 operational, 0x7F pre-operational; 0x1017 in ms, 0 disables | matches |
| 4 | SDO command bytes (expedited, segmented, block incl. CRC) | matches; also cross-checked with the python `canopen` client |
| 5 | SDO abort code values | all values match. Changed: changing the COB-ID of a valid PDO and writing an unsupported transmission type now abort with 0x06090030 (was 0x06040043 / accepted). Reading an empty entry of 0x1003 now aborts with 0x08000024. 0x06010000 and 0x08000022 are defined but not used. |
| 6 | PDO transmission types 0, 1–240, 254, 255; 241–253 rejected | matches after the change above |
| 7 | PDO re-mapping procedure (invalidate, sub 0 = 0, entries, count, validate) and its abort codes | matches |
| 8 | EMCY 0x8210 (RPDO too short) and 0x8250 (RPDO timeout); a longer RPDO uses the first bytes | matches |
| 9 | EMCY frame layout, error register bits (0 generic, 1 current, 2 voltage, 3 temperature, 4 communication, 7 manufacturer), 0x1003 newest first and write 0 to clear (other values 0x06090030), 0x1015 in 100 µs, 0x0000 with the remaining register when one of several errors is gone | matches |
| 10 | 0x1000, 0x1001, 0x1018 present; 0x1017 present (mandatory without node guarding); 0x1010/0x1011 signatures "save"/"load", wrong signature 0x08000020 | matches |
| 11 | Vendor ID | CiA 301 treats 0 as an invalid vendor ID. The simulator has no assigned ID and keeps 0 on purpose; profiles can override it. Documented in the README. |

## CiA 401-1: generic I/O (generic-io)

| # | Item | Result |
|---|---|---|
| 12 | 0x6000 (U8 ro), 0x6200 (U8 rw), 0x6401 (I16 ro), 0x6411 (I16 rw); device type 0x000F0191 (DI, DO, AI, AO, pre-defined PDO mapping) | matches. Added: 0x6423 analog input global interrupt enable (BOOLEAN, default FALSE) gating change-triggered analog TPDOs, with EMCY 0x0080 on entering operational while disabled. CiA 401-2 removed from the sources (it covers joysticks). |
| 13 | EMCY 0x2320 short circuit, 0x3110 input voltage too high (over range) | matches; CiA 401-1 defines no wire-break code, so 0xFF01 (manufacturer-specific) stays |

## CiA 406: encoder (generic-encoder)

| # | Item | Result |
|---|---|---|
| 14 | 0x6000 (U16 rw, bit 0 code sequence), 0x6001, 0x6003, 0x6004 (U32 ro, TPDO), 0x6030:1 (I16 ro); device type 0x00020196 multi-turn | Changed: 0x6030 now in steps per second (was rpm), clamped to INTEGER16; added the class C1 objects 0x6500, 0x6501, 0x6502; device type 0x00010196 for single-turn; multi-turn position wraps at resolution × 65536. Modelled as class C1. |
| 15 | EMCY 0x5050 single-turn, 0x5051 multi-turn sensor failure | matches |

## Drive (generic-drive), against public manuals

Manuals used:
- Kollmorgen, AKD CANopen Communications Manual, edition Y, October 2020 (valid from firmware 1.20)
- SEW-EURODRIVE, Inverters of the MOVI-C Modular Automation System with CiA402 Device Profile, edition 07/2023 (online manual)
- Synapticon, SOMANET Software Reference Manual v4.1 (object dictionary and firmware error list)

| # | Item | Result |
|---|---|---|
| 16 | Objects 0x603F … 0x60FF; 0x6502 = 0x25 (bits 0 pp, 2 pv, 5 hm); device type 0x00020192 | matches (0x6502 bits in Kollmorgen and Synapticon, device type in SEW) |
| 17 | Statusword patterns 0x0000/0x0040 (mask 0x4F), 0x0021/0x0023/0x0027/0x0007 (mask 0x6F), 0x000F/0x0008 (mask 0x4F); controlword 0x06/0x07/0x0F, quick stop, fault reset on bit 7 rising edge | matches (SEW, Kollmorgen) |
| 18 | EMCY 0x4310 over temperature, 0x8611 following error, over current | 0x4310 and 0x8611 match (Kollmorgen, Synapticon). Changed: over current from 0x2310 to 0x2220 (Synapticon). |
| 19 | Manual editions in the profile sources | filled in (see above) |
