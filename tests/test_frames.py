"""Tests of link/frames.py: CRC check value, the worked examples of the LC1020E PROTOCOL.md
section 15, and the parser's resynchronisation rules."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fnirsi_companion.link.frames import (ANSWER, EVENT, INCOMPLETE_LIMIT, MAX_PAYLOAD,  # noqa: E402
                                          REQUEST, Frame, FrameParser, crc16, encode)

# (seq, type, cmd, payload, bytes on the wire): LC1020E PROTOCOL.md section 15
EXAMPLES = [
    (0x01, REQUEST, 0x00, b"", "a5 03 00 01 01 00 df fb"),
    (0x02, REQUEST, 0x01, bytes([1, 2, 3]), "a5 06 00 02 01 01 01 02 03 61 87"),
    (0x02, ANSWER, 0x01, bytes([0, 1, 2, 3]), "a5 07 00 02 02 01 00 01 02 03 1a 89"),
    (0x03, REQUEST, 0x10, b"", "a5 03 00 03 01 10 8e 87"),
    (0x04, REQUEST, 0x13, bytes([0x0A, 0, 0]), "a5 06 00 04 01 13 0a 00 00 bf 5a"),
    (0x05, REQUEST, 0x13, bytes([0x03, 0xDC, 0x05]), "a5 06 00 05 01 13 03 dc 05 61 81"),
    (0x06, REQUEST, 0x11, bytes([0xE8, 0x03]), "a5 05 00 06 01 11 e8 03 73 ee"),
    (0x07, REQUEST, 0x23, bytes([0, 0]), "a5 05 00 07 01 23 00 00 9f 46"),
    (0x08, REQUEST, 0x52, b"KO", "a5 05 00 08 01 52 4b 4f 43 a4"),
    (0x09, REQUEST, 0x53, bytes([0, 0, 0, 0, 1]), "a5 08 00 09 01 53 00 00 00 00 01 4f e4"),
    (0x0A, REQUEST, 0x27, bytes([0, 0x4B, 0x4F]), "a5 06 00 0a 01 27 00 4b 4f d8 93"),
    (0x0B, REQUEST, 0x05, bytes([0x46, 0x4F]), "a5 05 00 0b 01 05 46 4f 93 e7"),
    (0x0C, REQUEST, 0x60, bytes([0xFF]), "a5 04 00 0c 01 60 ff 69 65"),
    (0x0D, ANSWER, 0x7F, bytes([1]), "a5 04 00 0d 02 7f 01 11 57"),
    (0x00, EVENT, 0x91, bytes([2, 1]), "a5 05 00 00 04 91 02 01 d2 7b"),
]


def wire(hexstr):
    return bytes.fromhex(hexstr.replace(" ", ""))


class Crc(unittest.TestCase):
    def test_check_value(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_first_example(self):
        # PROTOCOL.md 15: CRC 0xFBDF over 03 00 01 01 00
        self.assertEqual(crc16(bytes([3, 0, 1, 1, 0])), 0xFBDF)


class Encode(unittest.TestCase):
    def test_worked_examples(self):
        for seq, ftype, cmd, payload, hexstr in EXAMPLES:
            with self.subTest(hexstr):
                self.assertEqual(encode(seq, ftype, cmd, payload), wire(hexstr))

    def test_payload_limit(self):
        f = encode(1, REQUEST, 1, bytes(MAX_PAYLOAD))
        self.assertEqual(len(f), MAX_PAYLOAD + 8)
        self.assertEqual(f[1] | f[2] << 8, 515)
        with self.assertRaises(ValueError):
            encode(1, REQUEST, 1, bytes(MAX_PAYLOAD + 1))

    def test_seq_and_cmd_modulo_256(self):
        self.assertEqual(encode(0x101, REQUEST, 0x100), encode(1, REQUEST, 0))


class Parser(unittest.TestCase):
    def frames_of_examples(self, t):
        return [Frame(seq, ftype, cmd, payload, t) for seq, ftype, cmd, payload, _ in EXAMPLES]

    def test_all_examples_in_one_feed(self):
        p = FrameParser()
        data = b"".join(wire(e[4]) for e in EXAMPLES)
        self.assertEqual(p.feed(data, 5.0), self.frames_of_examples(5.0))
        self.assertEqual((p.crc_errors, p.skipped), (0, 0))

    def test_byte_by_byte(self):
        p = FrameParser()
        out = []
        for i, b in enumerate(b"".join(wire(e[4]) for e in EXAMPLES)):
            out += p.feed(bytes([b]), float(i))
        self.assertEqual([(f.seq, f.ftype, f.cmd, f.payload) for f in out],
                         [(e[0], e[1], e[2], e[3]) for e in EXAMPLES])
        # t is the time of the feed that completed the frame
        self.assertEqual(out[0].t, float(len(wire(EXAMPLES[0][4])) - 1))

    def test_garbage_before_and_between(self):
        p = FrameParser()
        a, b = wire(EXAMPLES[0][4]), wire(EXAMPLES[2][4])
        got = p.feed(b"\x00\x11\x22" + a + b"\xff\xfe" + b, 0.0)
        self.assertEqual(len(got), 2)
        self.assertEqual(p.skipped, 5)
        self.assertEqual(p.crc_errors, 0)

    def test_crc_error_counted_and_resync(self):
        p = FrameParser()
        bad = bytearray(wire(EXAMPLES[1][4]))
        bad[-1] ^= 0xFF
        good = wire(EXAMPLES[3][4])
        got = p.feed(bytes(bad) + good, 0.0)
        self.assertEqual([(f.seq, f.cmd) for f in got], [(0x03, 0x10)])
        self.assertEqual(p.crc_errors, 1)
        self.assertEqual(p.skipped, len(bad))

    def test_length_outside_range(self):
        for n in (0, 1, 2, 516, 0xFFFF):
            with self.subTest(n=n):
                p = FrameParser()
                got = p.feed(bytes([0xA5, n & 0xFF, n >> 8]) + wire(EXAMPLES[0][4]), 0.0)
                self.assertEqual(len(got), 1)
                self.assertEqual(p.crc_errors, 0)
                self.assertEqual(p.skipped, 3)

    def test_incomplete_false_sync_skipped_when_valid_frame_follows(self):
        p = FrameParser()
        # 0xA5 with length 0x0100: incomplete; a complete valid frame follows in the buffer. It is
        # waited for until it has stayed incomplete for INCOMPLETE_LIMIT, then skipped.
        got = p.feed(bytes([0xA5, 0x00, 0x01, 0x07]) + wire(EXAMPLES[14][4]), 1.0)
        self.assertEqual(got, [])
        self.assertTrue(p.waiting)
        self.assertEqual(p.feed(b"", 1.0 + INCOMPLETE_LIMIT - 0.01), [])
        got = p.feed(b"", 1.0 + INCOMPLETE_LIMIT)
        self.assertEqual([(f.ftype, f.cmd, f.payload) for f in got], [(EVENT, 0x91, b"\x02\x01")])
        self.assertEqual(p.skipped, 4)
        self.assertFalse(p.waiting)

    def test_long_answer_with_a_frame_inside_is_not_replaced(self):
        # the review's case (review_r1 finding 5): a FILE_READ answer whose data holds a complete
        # valid frame with the same seq and cmd, fed in 64-byte USB pieces
        inner = encode(7, ANSWER, 0x71, b"\x00" + b"FAKE")
        data = b"\x00" + b"\x10\x00\x00\x00" + b"x" * 20 + inner + b"y" * 400
        outer = encode(7, ANSWER, 0x71, data)
        p = FrameParser()
        got = []
        for k in range(0, len(outer), 64):
            got += p.feed(outer[k:k + 64], 0.0)
        self.assertEqual(got, [Frame(7, ANSWER, 0x71, data, 0.0)])
        self.assertEqual((p.skipped, p.crc_errors), (0, 0))

    def test_stale_incomplete_frame_without_a_later_frame_keeps_waiting(self):
        p = FrameParser()
        w = wire(EXAMPLES[2][4])
        self.assertEqual(p.feed(w[:6], 0.0), [])
        self.assertEqual(p.feed(b"", 10 * INCOMPLETE_LIMIT), [])      # nothing to take instead
        got = p.feed(w[6:], 11 * INCOMPLETE_LIMIT)
        self.assertEqual([(f.seq, f.cmd) for f in got], [(0x02, 0x01)])
        self.assertEqual(p.skipped, 0)

    def test_limit_counts_from_the_first_feed_of_the_incomplete_frame(self):
        p = FrameParser(limit=1.0)
        w = wire(EXAMPLES[0][4])
        self.assertEqual(p.feed(bytes([0xA5, 0x00, 0x01]), 0.0), [])
        self.assertEqual(p.feed(w, 0.9), [])                   # 0.9 s old: still waited for
        self.assertEqual(len(p.feed(b"", 1.0)), 1)              # 1.0 s: skipped for the frame

    def test_incomplete_frame_waits(self):
        p = FrameParser()
        w = wire(EXAMPLES[2][4])
        self.assertEqual(p.feed(w[:6], 0.0), [])
        got = p.feed(w[6:], 1.0)
        self.assertEqual(got, [Frame(0x02, ANSWER, 0x01, bytes([0, 1, 2, 3]), 1.0)])
        self.assertEqual(p.skipped, 0)

    def test_max_payload_frame(self):
        p = FrameParser()
        payload = bytes(range(256)) * 2
        got = p.feed(encode(9, ANSWER, 0x60, payload), 0.0)
        self.assertEqual(got, [Frame(9, ANSWER, 0x60, payload, 0.0)])

    def test_garbage_without_sync_is_dropped(self):
        p = FrameParser()
        self.assertEqual(p.feed(b"\x00" * 10, 0.0), [])
        self.assertEqual(p.skipped, 10)


if __name__ == "__main__":
    unittest.main()
