"""Frame codec of the control channel, the same on the 2C53T and the LC1020E.

Frame on the wire (LC1020E PROTOCOL.md section 2, 2C53T PROTOCOL.md):

    sync 0xA5 | length u16 LE | seq u8 | type u8 | cmd u8 | payload 0..512 | crc u16 LE

length counts seq..payload (3..515). The CRC is CRC-16/CCITT-FALSE (polynomial 0x1021, initial
0xFFFF, no reflection, no final XOR) over length..payload. Types: 1 request, 2 answer, 3 stream,
4 event.

FrameParser follows the parser of tools/lc1020e.py of the LC1020E repository: it searches for
0xA5, rejects a length outside 3..515 by moving on one byte and checks the CRC (a wrong CRC is
counted and the search continues at the next byte). A frame whose length is valid but whose bytes
have not all arrived is waited for: a valid frame inside its payload (a FILE_READ of data that
holds protocol frames) must not replace it. Only when such a frame has stayed incomplete for
INCOMPLETE_LIMIT seconds (host time of the feeds) is its 0xA5 taken as a false sync byte (for
example when the port was opened in the middle of a frame) and skipped in favour of a complete
valid frame that follows it in the buffer. A false sync byte with more bytes arriving behind it
ends sooner by itself: once its length is complete, its CRC fails and the search moves on.

INCOMPLETE_LIMIT = 2.0 s is above every stated time in which a meter's sending of a started frame
can pause: the LC1020E sends from its main loop (SL 4.1 step 6) and one pass may take up to 1.5 s
(SL 4.3, a save inside the pass); the 2C53T's USB interrupt moves the tx ring into IN packets on
every 1 ms start of frame (2C53T src/usb_ctl.s:12), and its answers come within 500 ms (S2 2).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SYNC = 0xA5
REQUEST, ANSWER, STREAM, EVENT = 1, 2, 3, 4
LEN_MIN, LEN_MAX = 3, 515
MAX_PAYLOAD = LEN_MAX - 3
INCOMPLETE_LIMIT = 2.0          # seconds an incomplete frame is waited for (module docstring)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE of data; crc16(b"123456789") == 0x29B1."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode(seq: int, ftype: int, cmd: int, payload: bytes = b"") -> bytes:
    """One frame as bytes on the wire. seq and cmd are taken modulo 256; a payload longer than
    512 bytes raises ValueError."""
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload of %d bytes: at most %d" % (len(payload), MAX_PAYLOAD))
    body = struct.pack("<HBBB", len(payload) + 3, seq & 0xFF, ftype & 0xFF, cmd & 0xFF) + payload
    return bytes([SYNC]) + body + struct.pack("<H", crc16(body))


@dataclass(frozen=True)
class Frame:
    """One received frame. t is the host's time.monotonic() when its bytes were read."""
    seq: int
    ftype: int
    cmd: int
    payload: bytes
    t: float


class FrameParser:
    """Byte stream -> frames.

    crc_errors counts frames whose length was valid and complete but whose CRC did not match;
    skipped counts the bytes passed over while searching for a frame (including the sync byte of
    each CRC error). limit: INCOMPLETE_LIMIT, the seconds an incomplete frame is waited for."""

    def __init__(self, limit: float = INCOMPLETE_LIMIT) -> None:
        self._buf = bytearray()
        self.crc_errors = 0
        self.skipped = 0
        self.limit = limit
        self._head_t: float | None = None     # feed time at which the incomplete head was found

    @property
    def waiting(self) -> bool:
        """True while an incomplete frame is buffered (the link then feeds b"" with the time, so
        that the limit can run out without new bytes)."""
        return self._head_t is not None

    def _drop(self, n: int) -> None:
        del self._buf[:n]
        self._head_t = None

    def feed(self, data: bytes, t: float) -> list[Frame]:
        """Adds data (read at host time t; may be empty) and returns the frames completed by it,
        in order. Bytes of an incomplete frame stay buffered for the next call."""
        buf = self._buf
        buf += data
        out: list[Frame] = []
        while True:
            i = buf.find(SYNC)
            if i < 0:
                self.skipped += len(buf)
                self._drop(len(buf))
                break
            if i:
                self.skipped += i
                self._drop(i)
            if len(buf) < 3:
                break
            n = buf[1] | buf[2] << 8
            if not LEN_MIN <= n <= LEN_MAX:
                self.skipped += 1
                self._drop(1)
                continue
            if len(buf) < n + 5:
                if self._head_t is None:
                    self._head_t = t
                if t - self._head_t < self.limit:
                    break                    # still arriving: wait for the rest
                j = self._later_frame(1)
                if j < 0:
                    break                    # nothing complete behind it: keep waiting
                self.skipped += j
                self._drop(j)
                continue
            body = bytes(buf[1:3 + n])
            got = buf[3 + n] | buf[4 + n] << 8
            if crc16(body) != got:
                self.crc_errors += 1
                self.skipped += 1
                self._drop(1)
                continue
            out.append(Frame(seq=body[2], ftype=body[3], cmd=body[4], payload=body[5:], t=t))
            self._drop(n + 5)
        return out

    def _later_frame(self, start: int) -> int:
        """Index of the first 0xA5 at or after start that begins a complete frame with a valid
        length and CRC, or -1."""
        buf = self._buf
        j = buf.find(SYNC, start)
        while j >= 0:
            if j + 3 <= len(buf):
                n = buf[j + 1] | buf[j + 2] << 8
                if (LEN_MIN <= n <= LEN_MAX and j + n + 5 <= len(buf) and
                        crc16(bytes(buf[j + 1:j + 3 + n])) ==
                        (buf[j + 3 + n] | buf[j + 4 + n] << 8)):
                    return j
            j = buf.find(SYNC, j + 1)
        return -1
