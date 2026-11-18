"""SDO server (CiA 301 expedited, segmented and block transfers), pure protocol core."""
from __future__ import annotations

import struct

from .od import (
    BAD_BLOCK_SIZE, BAD_COMMAND, BAD_SEQUENCE, CRC_ERROR, LENGTH_MISMATCH, TIMEOUT, TOGGLE, TOO_LONG, DataType,
    ObjectDictionary, SdoAbort, encode,
)

SDO_TIMEOUT = 1.0
MAX_VARIABLE_LENGTH = 65536  # cap for segmented download of strings

# transfer states
IDLE = "idle"
UPLOAD = "upload"      # segmented upload in progress
DOWNLOAD = "download"  # segmented download in progress

BLOCK_SIZE = 127       # segments per sub-block the server offers/accepts
BLOCK_DOWNLOAD = "block_download"          # receiving sub-block segments
BLOCK_DOWNLOAD_END = "block_download_end"  # waiting for the client end frame
BLOCK_UPLOAD_START = "block_upload_start"  # waiting for the client start (0xA3)
BLOCK_UPLOAD_ACK = "block_upload_ack"      # sub-block sent, waiting for ack
BLOCK_UPLOAD_END = "block_upload_end"      # end sent, waiting for confirmation


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0, no reflection)."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# client command specifiers (byte 0, bits 7-5)
CS_DOWNLOAD_SEGMENT = 0
CS_INITIATE_DOWNLOAD = 1
CS_INITIATE_UPLOAD = 2
CS_UPLOAD_SEGMENT = 3
CS_ABORT = 4
CS_BLOCK_UPLOAD = 5
CS_BLOCK_DOWNLOAD = 6


def abort_frame(index: int, sub: int, code: int) -> bytes:
    return struct.pack("<BHBI", 0x80, index, sub, code)


class SdoServer:
    def __init__(self, od: ObjectDictionary, node_id: int) -> None:
        self.od = od
        self.node_id = node_id
        self.reset()

    def reset(self) -> None:
        self.state = IDLE
        self.index = 0
        self.sub = 0
        self.toggle = 0
        self.buffer = b""      # upload: data to send; download: data received
        self.pos = 0           # upload: bytes already sent
        self.size: int | None = None  # download: size announced by the client
        self.limit = 0         # download: maximum accepted length
        self.crc_ok = False    # block download: client uses CRC
        self.pending: list[bytes] = []  # block download: segments of the current sub-block
        self.discarding = False  # block download: dropping segments after a sequence error
        self.blksize = 0       # block transfer: segments per sub-block
        self.sent = 0          # block upload: segments in the last sub-block
        self.last_activity = 0.0

    # -- public API -------------------------------------------------------

    def handle(self, data: bytes, now: float) -> list[bytes]:
        if len(data) < 1:
            return []
        data = bytes(data).ljust(8, b"\x00")[:8]
        cs = data[0] >> 5
        in_blocks = self.state == BLOCK_DOWNLOAD  # data[0] is c<<7 | seqno here
        if cs == CS_ABORT and (not in_blocks or data[0] == 0x80):
            self.reset()
            return []
        try:
            if in_blocks:
                return self._block_download_segment(data, now)
            if cs == CS_BLOCK_DOWNLOAD:
                return self._block_download_cmd(data, now)
            if cs == CS_BLOCK_UPLOAD:
                return self._block_upload_cmd(data, now)
            if cs == CS_INITIATE_UPLOAD:
                return [self._initiate_upload(data, now)]
            if cs == CS_INITIATE_DOWNLOAD:
                return [self._initiate_download(data, now)]
            if cs == CS_UPLOAD_SEGMENT and self.state == UPLOAD:
                return [self._upload_segment(data, now)]
            if cs == CS_DOWNLOAD_SEGMENT and self.state == DOWNLOAD:
                return [self._download_segment(data, now)]
            raise SdoAbort(BAD_COMMAND)
        except SdoAbort as exc:
            return [self._abort(exc.code)]

    def poll(self, now: float) -> list[bytes]:
        if self.state != IDLE and now - self.last_activity > SDO_TIMEOUT:
            return [self._abort(TIMEOUT)]
        return []

    # -- helpers ----------------------------------------------------------

    def _abort(self, code: int) -> bytes:
        frame = abort_frame(self.index, self.sub, code)
        self.reset()
        return frame

    def _begin(self, data: bytes) -> None:
        self.reset()
        self.index = data[1] | (data[2] << 8)
        self.sub = data[3]

    # -- upload -----------------------------------------------------------

    def _initiate_upload(self, data: bytes, now: float) -> bytes:
        self._begin(data)
        return self._upload_reply(data, self.od.sdo_read(self.index, self.sub), now)

    def _upload_reply(self, data: bytes, payload: bytes, now: float) -> bytes:
        head = bytes([0, data[1], data[2], data[3]])
        if len(payload) <= 4:
            n = 4 - len(payload)
            self.reset()
            return bytes([0x40 | (n << 2) | 0x02 | 0x01]) + head[1:] + payload.ljust(4, b"\x00")
        self.state = UPLOAD
        self.buffer = payload
        self.pos = 0
        self.toggle = 0
        self.last_activity = now
        return bytes([0x41]) + head[1:] + struct.pack("<I", len(payload))

    def _upload_segment(self, data: bytes, now: float) -> bytes:
        if ((data[0] >> 4) & 1) != self.toggle:
            raise SdoAbort(TOGGLE)
        chunk = self.buffer[self.pos:self.pos + 7]
        self.pos += len(chunk)
        last = self.pos >= len(self.buffer)
        n = 7 - len(chunk)
        frame = bytes([(self.toggle << 4) | (n << 1) | (1 if last else 0)]) + chunk.ljust(7, b"\x00")
        if last:
            self.reset()
        else:
            self.toggle ^= 1
            self.last_activity = now
        return frame

    # -- download ---------------------------------------------------------

    def _initiate_download(self, data: bytes, now: float) -> bytes:
        self._begin(data)
        cmd = data[0]
        ack = bytes([0x60, data[1], data[2], data[3], 0, 0, 0, 0])
        if cmd & 0x02:  # expedited
            if cmd & 0x01:
                payload = data[4:8 - ((cmd >> 2) & 3)]
            else:  # no size indicator: a fixed-size type tells the length itself
                var = self.od.entry(self.index, self.sub)
                if var.dtype in (DataType.VISIBLE_STRING, DataType.OCTET_STRING):
                    payload = data[4:8]
                else:
                    payload = data[4:4 + len(encode(var.dtype, var.value))]
            self.od.sdo_write(self.index, self.sub, payload)
            self.reset()
            return ack
        var = self.od.entry(self.index, self.sub)
        if var.dtype in (DataType.VISIBLE_STRING, DataType.OCTET_STRING):
            limit = MAX_VARIABLE_LENGTH
        else:
            limit = len(encode(var.dtype, var.value))
        size = struct.unpack_from("<I", data, 4)[0] if cmd & 0x01 else None
        if size is not None and size > limit:
            raise SdoAbort(TOO_LONG)
        self.state = DOWNLOAD
        self.buffer = b""
        self.toggle = 0
        self.size = size
        self.limit = limit
        self.last_activity = now
        return ack

    def _download_segment(self, data: bytes, now: float) -> bytes:
        cmd = data[0]
        if ((cmd >> 4) & 1) != self.toggle:
            raise SdoAbort(TOGGLE)
        n = (cmd >> 1) & 7
        self.buffer += data[1:8 - n]
        if len(self.buffer) > (self.size if self.size is not None else self.limit):
            raise SdoAbort(TOO_LONG)
        frame = bytes([0x20 | (self.toggle << 4)]) + bytes(7)
        if cmd & 0x01:  # last segment: commit
            payload = self.buffer
            if self.size is not None:
                if len(payload) < self.size:
                    raise SdoAbort(LENGTH_MISMATCH)
            self.od.sdo_write(self.index, self.sub, payload)
            self.reset()
        else:
            self.toggle ^= 1
            self.last_activity = now
        return frame

    # -- block download ---------------------------------------------------

    def _block_download_cmd(self, data: bytes, now: float) -> list[bytes]:
        cmd = data[0]
        if cmd & 0x01:
            if self.state != BLOCK_DOWNLOAD_END:
                raise SdoAbort(BAD_COMMAND)
            return [self._block_download_end(data)]
        self._begin(data)
        var = self.od.entry(self.index, self.sub)
        if var.dtype in (DataType.VISIBLE_STRING, DataType.OCTET_STRING):
            limit = MAX_VARIABLE_LENGTH
        else:
            limit = len(encode(var.dtype, var.value))
        size = struct.unpack_from("<I", data, 4)[0] if cmd & 0x02 else None
        if size is not None and size > limit:
            raise SdoAbort(TOO_LONG)
        self.state = BLOCK_DOWNLOAD
        self.size = size
        self.limit = limit
        self.crc_ok = bool(cmd & 0x04)
        self.blksize = BLOCK_SIZE
        self.last_activity = now
        return [bytes([0xA4, data[1], data[2], data[3], BLOCK_SIZE, 0, 0, 0])]

    def _block_download_segment(self, data: bytes, now: float) -> list[bytes]:
        self.last_activity = now
        last = bool(data[0] & 0x80)
        seq = data[0] & 0x7F
        if self.discarding:
            if seq != 1:
                return []
            self.discarding = False
        if seq != len(self.pending) + 1:
            frame = self._block_ack(len(self.pending))
            self.discarding = True
            return [frame]
        self.pending.append(data[1:8])
        if seq == self.blksize or last:
            frame = self._block_ack(seq)
            if last:
                self.state = BLOCK_DOWNLOAD_END
            return [frame]
        return []

    def _block_ack(self, ackseq: int) -> bytes:
        """Commit the good segments of the sub-block and build the ack."""
        self.buffer += b"".join(self.pending[:ackseq])
        self.pending = []
        if len(self.buffer) > (self.size if self.size is not None else self.limit) + 6:
            raise SdoAbort(TOO_LONG)
        return bytes([0xA2, ackseq, self.blksize]) + bytes(5)

    def _block_download_end(self, data: bytes) -> bytes:
        n = (data[0] >> 2) & 7
        payload = self.buffer[:len(self.buffer) - n] if n else self.buffer
        if len(payload) > (self.size if self.size is not None else self.limit):
            raise SdoAbort(TOO_LONG)
        if self.size is not None and len(payload) != self.size:
            raise SdoAbort(LENGTH_MISMATCH)
        if self.crc_ok and crc16(payload) != struct.unpack_from("<H", data, 1)[0]:
            raise SdoAbort(CRC_ERROR)
        self.od.sdo_write(self.index, self.sub, payload)
        self.reset()
        return bytes([0xA1]) + bytes(7)

    # -- block upload -----------------------------------------------------

    def _block_upload_cmd(self, data: bytes, now: float) -> list[bytes]:
        sub = data[0] & 0x03
        if sub == 0:
            return [self._block_upload_initiate(data, now)]
        if sub == 3 and self.state == BLOCK_UPLOAD_START:
            self.last_activity = now
            return self._send_sub_block()
        if sub == 2 and self.state == BLOCK_UPLOAD_ACK:
            self.last_activity = now
            return self._block_upload_ack(data)
        if sub == 1 and self.state == BLOCK_UPLOAD_END:
            self.reset()
            return []
        raise SdoAbort(BAD_COMMAND)

    def _block_upload_initiate(self, data: bytes, now: float) -> bytes:
        self._begin(data)
        blksize, pst = data[4], data[5]
        if blksize == 0 or blksize > BLOCK_SIZE:
            raise SdoAbort(BAD_BLOCK_SIZE)
        payload = self.od.sdo_read(self.index, self.sub)
        if pst > 0 and len(payload) <= pst:
            return self._upload_reply(data, payload, now)  # protocol switch
        self.state = BLOCK_UPLOAD_START
        self.buffer = payload
        self.pos = 0
        self.blksize = blksize
        self.last_activity = now
        return bytes([0xC6, data[1], data[2], data[3]]) + struct.pack("<I", len(payload))

    def _send_sub_block(self) -> list[bytes]:
        chunks = [self.buffer[i:i + 7] for i in range(self.pos, len(self.buffer), 7)]
        chunks = chunks[:self.blksize] or [b""]
        out = []
        for i, chunk in enumerate(chunks, 1):
            last = i == len(chunks) and self.pos + 7 * i >= len(self.buffer)
            out.append(bytes([(0x80 if last else 0) | i]) + chunk.ljust(7, b"\x00"))
        self.sent = len(chunks)
        self.state = BLOCK_UPLOAD_ACK
        return out

    def _block_upload_ack(self, data: bytes) -> list[bytes]:
        ackseq, blksize = data[1], data[2]
        if ackseq > self.sent:
            raise SdoAbort(BAD_SEQUENCE)
        if blksize == 0 or blksize > BLOCK_SIZE:
            raise SdoAbort(BAD_BLOCK_SIZE)
        self.pos = min(self.pos + 7 * ackseq, len(self.buffer))
        self.blksize = blksize
        if ackseq == self.sent and self.pos >= len(self.buffer):
            n = (7 - len(self.buffer) % 7) % 7 if self.buffer else 7
            self.state = BLOCK_UPLOAD_END
            return [bytes([0xC1 | (n << 2)]) + struct.pack("<H", crc16(self.buffer)) + bytes(5)]
        return self._send_sub_block()
