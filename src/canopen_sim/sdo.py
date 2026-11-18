"""SDO server (CiA 301 expedited and segmented transfers), pure protocol core."""
from __future__ import annotations

import struct

from .od import (
    BAD_COMMAND, LENGTH_MISMATCH, TIMEOUT, TOGGLE, ObjectDictionary, SdoAbort,
)

SDO_TIMEOUT = 1.0

# transfer states
IDLE = "idle"
UPLOAD = "upload"      # segmented upload in progress
DOWNLOAD = "download"  # segmented download in progress

# client command specifiers (byte 0, bits 7-5)
CS_DOWNLOAD_SEGMENT = 0
CS_INITIATE_DOWNLOAD = 1
CS_INITIATE_UPLOAD = 2
CS_UPLOAD_SEGMENT = 3
CS_ABORT = 4


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
        self.last_activity = 0.0

    # -- public API -------------------------------------------------------

    def handle(self, data: bytes, now: float) -> list[bytes]:
        if len(data) < 1:
            return []
        data = bytes(data).ljust(8, b"\x00")[:8]
        cs = data[0] >> 5
        if cs == CS_ABORT:
            self.reset()
            return []
        try:
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
        payload = self.od.sdo_read(self.index, self.sub)
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
            n = (cmd >> 2) & 3 if cmd & 0x01 else 0
            self.od.sdo_write(self.index, self.sub, data[4:8 - n])
            self.reset()
            return ack
        self.state = DOWNLOAD
        self.buffer = b""
        self.toggle = 0
        self.size = struct.unpack_from("<I", data, 4)[0] if cmd & 0x01 else None
        self.last_activity = now
        return ack

    def _download_segment(self, data: bytes, now: float) -> bytes:
        cmd = data[0]
        if ((cmd >> 4) & 1) != self.toggle:
            raise SdoAbort(TOGGLE)
        n = (cmd >> 1) & 7
        self.buffer += data[1:8 - n]
        frame = bytes([0x20 | (self.toggle << 4)]) + bytes(7)
        if cmd & 0x01:  # last segment: commit
            payload = self.buffer
            if self.size is not None:
                if len(payload) < self.size:
                    raise SdoAbort(LENGTH_MISMATCH)
                payload = payload[:self.size]
            self.od.sdo_write(self.index, self.sub, payload)
            self.reset()
        else:
            self.toggle ^= 1
            self.last_activity = now
        return frame
