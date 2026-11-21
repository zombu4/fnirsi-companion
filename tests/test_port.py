"""Tests of link/port.py against a fake serial object that answers frames. No real port."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402

from fnirsi_companion.link import port  # noqa: E402
from fnirsi_companion.link.frames import (ANSWER, EVENT, REQUEST, STREAM, FrameParser,  # noqa: E402
                                          encode)
from fnirsi_companion.link.port import (AnswerTimeout, PortLost, SerialLink,  # noqa: E402
                                        StatusError)

WAIT = 5.0      # upper bound of every wait in these tests


class FakeSerial:
    """read / write / in_waiting / dtr / close like pyserial. Each request frame written is
    passed to handler(frame) -> list of (delay seconds, bytes) to send back."""

    def __init__(self, handler=None):
        self.handler = handler or self.default
        self.requests = []                  # frames written, in order
        self.dtr_history = []
        self.closed = False
        self.read_error = None
        self.write_error = None
        self._rx = bytearray()
        self._cond = threading.Condition()
        self._parser = FrameParser()

    # meter side
    @staticmethod
    def default(f):
        if f.cmd == 0x7F:
            return [(0, encode(f.seq, ANSWER, f.cmd, b"\x01"))]
        return [(0, encode(f.seq, ANSWER, f.cmd, b"\x00" + f.payload))]

    def push(self, data, delay=0.0):
        def put():
            with self._cond:
                self._rx += data
                self._cond.notify_all()
        if delay:
            threading.Timer(delay, put).start()
        else:
            put()

    # pyserial side
    @property
    def dtr(self):
        return self.dtr_history[-1] if self.dtr_history else False

    @dtr.setter
    def dtr(self, on):
        self.dtr_history.append(on)

    @property
    def in_waiting(self):
        if self.read_error:
            raise self.read_error
        with self._cond:
            return len(self._rx)

    def read(self, n):
        if self.read_error:
            raise self.read_error
        with self._cond:
            if not self._rx:
                self._cond.wait(0.02)
            data = bytes(self._rx[:n])
            del self._rx[:n]
            return data

    def write(self, data):
        if self.write_error:
            raise self.write_error
        for f in self._parser.feed(data, time.monotonic()):
            self.requests.append(f)
            for delay, out in self.handler(f) or ():
                self.push(out, delay)
        return len(data)

    def close(self):
        self.closed = True


def wait_until(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class LinkCase(unittest.TestCase):
    def make(self, handler=None):
        self.ser = FakeSerial(handler)
        self.ports = []

        def factory(name):
            self.ports.append(name)
            return self.ser
        link = SerialLink("PORT-X", serial_factory=factory)
        self.streams, self.events, self.losses = [], [], []
        link.stream.connect(self.streams.append, Qt.DirectConnection)
        link.event_frame.connect(self.events.append, Qt.DirectConnection)
        link.lost.connect(self.losses.append, Qt.DirectConnection)
        link.open()
        self.addCleanup(link.close)
        return link


class Basics(LinkCase):
    def test_open_sets_dtr_and_answers(self):
        link = self.make()
        self.assertEqual(self.ports, ["PORT-X"])
        self.assertEqual(self.ser.dtr_history, [True])
        self.assertTrue(link.dtr)
        self.assertEqual(link.request(0x01, b"\x01\x02\x03"), b"\x01\x02\x03")
        f = self.ser.requests[0]
        self.assertEqual((f.ftype, f.cmd, f.payload), (REQUEST, 0x01, b"\x01\x02\x03"))
        self.assertEqual(link.stats["frames_in"], 1)

    def test_status_error(self):
        link = self.make(lambda f: [(0, encode(f.seq, ANSWER, f.cmd, b"\x03\xAA\xBB"))])
        with self.assertRaises(StatusError) as cm:
            link.request(0x52, b"KO")
        self.assertEqual((cm.exception.cmd, cm.exception.status, cm.exception.data),
                         (0x52, 3, b"\xAA\xBB"))

    def test_answer_matching(self):
        # before the real answer: an answer with another seq, one with another cmd, one empty
        def handler(f):
            return [(0, encode(f.seq ^ 0x55, ANSWER, f.cmd, b"\x00bad-seq")),
                    (0, encode(f.seq, ANSWER, f.cmd ^ 1, b"\x00bad-cmd")),
                    (0, encode(f.seq, ANSWER, f.cmd, b"")),
                    (0, encode(f.seq, ANSWER, f.cmd, b"\x00good"))]
        link = self.make(handler)
        self.assertEqual(link.request(0x10), b"good")
        self.assertEqual(link.stats["stale"], 3)
        self.assertEqual(link.stats["frames_in"], 4)

    def test_sequence_numbers_advance(self):
        link = self.make()
        for _ in range(3):
            link.request(0x00)
        self.assertEqual([f.seq for f in self.ser.requests], [1, 2, 3])

    def test_bad_arguments(self):
        link = self.make()
        with self.assertRaises(ValueError):
            link.request_async(0x100)
        with self.assertRaises(ValueError):
            link.request_async(0x01, bytes(513))
        with self.assertRaises(ValueError):
            link.request_async(0x01, b"", 0)

    def test_not_open(self):
        link = SerialLink("PORT-X", serial_factory=lambda name: FakeSerial())
        with self.assertRaises(RuntimeError):
            link.request_async(0)

    def test_open_failure_is_port_lost(self):
        def factory(name):
            raise PermissionError(13, "Permission denied")
        link = SerialLink("PORT-X", serial_factory=factory)
        with self.assertRaises(PortLost) as cm:
            link.open()
        self.assertIn("Permission denied", str(cm.exception))

    def test_set_dtr(self):
        link = self.make()
        link.set_dtr(False)
        self.assertEqual(self.ser.dtr_history, [True, False])
        self.assertFalse(link.dtr)


class StreamsAndEvents(LinkCase):
    def test_streams_and_events_during_a_request(self):
        def handler(f):
            return [(0, encode(7, STREAM, 0x10, b"S" * 32)),
                    (0, encode(8, EVENT, 0x91, b"\x02\x01")),
                    (0, encode(f.seq, ANSWER, f.cmd, b"\x00ok"))]
        link = self.make(handler)
        self.assertEqual(link.request(0x11, b"\xe8\x03"), b"ok")
        self.assertTrue(wait_until(lambda: self.streams and self.events))
        self.assertEqual((self.streams[0].ftype, self.streams[0].cmd, self.streams[0].payload),
                         (STREAM, 0x10, b"S" * 32))
        self.assertEqual((self.events[0].ftype, self.events[0].cmd, self.events[0].seq),
                         (EVENT, 0x91, 8))

    def test_unsolicited_frames_without_request(self):
        link = self.make()
        self.ser.push(encode(1, STREAM, 0x22, bytes(64)) + b"\x00\x01" +
                      encode(2, EVENT, 0x94, b""))
        self.assertTrue(wait_until(lambda: len(self.streams) == 1 and len(self.events) == 1))
        self.assertEqual(link.stats["skipped"], 2)

    def test_crc_errors_counted(self):
        link = self.make()
        bad = bytearray(encode(1, STREAM, 0x22, b"x"))
        bad[-1] ^= 1
        self.ser.push(bytes(bad))
        self.assertTrue(wait_until(lambda: link.stats["crc_errors"] == 1))
        self.assertEqual(self.streams, [])


    def test_answer_behind_a_false_sync_byte_after_the_parser_limit(self):
        # a false 0xA5 (length 0x0100, never completed) before the answer: the link keeps feeding
        # the parser without new bytes, so the answer is taken once the limit has run out
        link = self.make(lambda f: [(0, bytes([0xA5, 0x00, 0x01, 0x07])
                                     + encode(f.seq, ANSWER, f.cmd, b"\x00ok"))])
        link._parser.limit = 0.1
        t0 = time.monotonic()
        self.assertEqual(link.request(0x05, b"", 2.0), b"ok")
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)
        self.assertEqual(link.stats["skipped"], 4)


class Timeouts(LinkCase):
    def test_timeout_then_ping_before_next_request(self):
        late = []

        def handler(f):
            if f.cmd == 0x20 and not late:
                late.append(f)
                return [(0.3, encode(f.seq, ANSWER, f.cmd, b"\x00late"))]  # after its limit
            if f.cmd == 0x01:
                # the late answer is still on its way: the PING answer comes after it
                return [(0.4, encode(f.seq, ANSWER, f.cmd, b"\x00" + f.payload))]
            return FakeSerial.default(f)
        link = self.make(handler)
        with self.assertRaises(AnswerTimeout):
            link.request(0x20, b"", timeout=0.1)
        self.assertEqual(link.request(0x00, b"next"), b"next")
        cmds = [(f.cmd, len(f.payload)) for f in self.ser.requests]
        self.assertEqual(cmds, [(0x20, 0), (0x01, 4), (0x00, 4)])
        self.assertEqual(link.stats["timeouts"], 1)
        self.assertEqual(link.stats["stale"], 1)            # the late answer was dropped

    def test_ping_with_wrong_data_is_repeated(self):
        state = {"pings": 0}

        def handler(f):
            if f.cmd == 0x20:
                return []
            if f.cmd == 0x01:
                state["pings"] += 1
                data = b"\x00" + (f.payload if state["pings"] > 1 else b"zzzz")
                return [(0, encode(f.seq, ANSWER, f.cmd, data))]
            return FakeSerial.default(f)
        link = self.make(handler)
        with self.assertRaises(AnswerTimeout):
            link.request(0x20, timeout=0.05)
        self.assertEqual(link.request(0x00, b"a"), b"a")
        self.assertEqual(state["pings"], 2)
        pings = [f.payload for f in self.ser.requests if f.cmd == 0x01]
        self.assertNotEqual(pings[0], pings[1])

    def test_unanswered_pings_lose_the_link(self):
        with mock.patch.object(port, "PING_TIMEOUT", 0.05):
            link = self.make(lambda f: [])
            with self.assertRaises(AnswerTimeout):
                link.request(0x00, timeout=0.05)
            fut = link.request_async(0x00)
            with self.assertRaises(PortLost):
                fut.result(WAIT)
        # the futures fail before `lost` is emitted (both in the worker's last step)
        self.assertTrue(wait_until(lambda: self.losses))
        self.assertEqual(len(self.losses), 1)
        self.assertIn("PING", self.losses[0])
        pings = [f for f in self.ser.requests if f.cmd == 0x01]
        self.assertEqual(len(pings), port.PING_ATTEMPTS)
        self.assertTrue(self.ser.closed)
        self.assertFalse(link.is_open)


class Loss(LinkCase):
    def test_read_error(self):
        link = self.make(lambda f: [])
        fut = link.request_async(0x00, timeout=WAIT)
        self.assertTrue(wait_until(lambda: self.ser.requests))
        self.ser.read_error = OSError(6, "Device not configured")
        with self.assertRaises(PortLost) as cm:
            fut.result(WAIT)
        self.assertIn("Device not configured", str(cm.exception))
        self.assertTrue(wait_until(lambda: self.losses))
        self.assertEqual(len(self.losses), 1)
        later = link.request_async(0x00)
        with self.assertRaises(PortLost):
            later.result(WAIT)
        with self.assertRaises(PortLost):
            link.set_dtr(True)

    def test_write_error(self):
        link = self.make()
        self.ser.write_error = OSError(5, "Input/output error")
        with self.assertRaises(PortLost):
            link.request(0x00)
        self.assertTrue(wait_until(lambda: self.losses))

    def test_mark_lost(self):
        link = self.make(lambda f: [])
        fut = link.request_async(0x00, timeout=WAIT)
        link.mark_lost("port left the system")
        with self.assertRaises(PortLost):
            fut.result(WAIT)
        self.assertTrue(wait_until(lambda: self.losses == ["port left the system"]))

    def test_close_fails_pending_without_lost_signal(self):
        link = self.make(lambda f: [])
        fut = link.request_async(0x00, timeout=WAIT)
        queued = link.request_async(0x10, timeout=WAIT)
        self.assertTrue(wait_until(lambda: self.ser.requests))
        link.close()
        for f in (fut, queued):
            with self.assertRaises(PortLost):
                f.result(WAIT)
        self.assertTrue(self.ser.closed)
        self.assertEqual(self.losses, [])
        link.close()                    # second close is harmless

    def test_request_on_worker_thread_refused(self):
        link = self.make()
        errors = []

        def on_stream(f):
            try:
                link.request(0x00)
            except RuntimeError as e:
                errors.append(e)
        link.stream.connect(on_stream, Qt.DirectConnection)
        self.ser.push(encode(1, STREAM, 0x10, b""))
        self.assertTrue(wait_until(lambda: errors))


class Exclusive(LinkCase):
    def test_other_threads_wait(self):
        link = self.make()
        entered, release = threading.Event(), threading.Event()
        results = []

        def job():
            with link.exclusive():
                entered.set()
                results.append(link.request(0x00, b"job1"))
                release.wait(WAIT)
                results.append(link.request(0x00, b"job2"))
        t = threading.Thread(target=job)
        t.start()
        self.assertTrue(entered.wait(WAIT))
        other = link.request_async(0x00, b"gui")
        time.sleep(0.15)
        self.assertFalse(other.done())                    # waits while the job holds the port
        release.set()
        t.join(WAIT)
        self.assertEqual(other.result(WAIT), b"gui")
        self.assertEqual(results, [b"job1", b"job2"])
        self.assertEqual([f.payload for f in self.ser.requests], [b"job1", b"job2", b"gui"])

    def test_reentrant_and_second_holder_waits(self):
        link = self.make()
        order = []
        inside = threading.Event()

        def first():
            with link.exclusive():
                with link.exclusive():
                    inside.set()
                    time.sleep(0.1)
                    order.append("first")

        def second():
            inside.wait(WAIT)
            with link.exclusive():
                order.append("second")
        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start()
        b.start()
        a.join(WAIT)
        b.join(WAIT)
        self.assertEqual(order, ["first", "second"])

    def test_cancelled_queued_request_is_not_sent(self):
        link = self.make()
        entered, release = threading.Event(), threading.Event()

        def job():
            with link.exclusive():
                entered.set()
                release.wait(WAIT)
        t = threading.Thread(target=job)
        t.start()
        self.assertTrue(entered.wait(WAIT))
        fut = link.request_async(0x00, b"cancel me")
        self.assertTrue(fut.cancel())
        release.set()
        t.join(WAIT)
        self.assertEqual(link.request(0x00, b"after"), b"after")
        self.assertEqual([f.payload for f in self.ser.requests], [b"after"])

    def test_exclusive_on_dead_link(self):
        link = self.make()
        link.close()
        with self.assertRaises(PortLost):
            with link.exclusive():
                pass


if __name__ == "__main__":
    unittest.main()
