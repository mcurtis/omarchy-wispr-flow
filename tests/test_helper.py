import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from array import array

HELPER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helper")
HELPER = os.path.join(HELPER_DIR, "wispr_flow_status.py")
sys.path.insert(0, HELPER_DIR)

import wispr_flow_status as helper  # noqa: E402

LISTENING = b"16:33:33.175 \xe2\x80\xba updateDictationStatus: listening { customAttributes: { uuid: 'x' } }"


def status_line(state):
    return ("12:00:00.000 › updateDictationStatus: %s { customAttributes: { uuid: '' } }\n" % state).encode()


class StateMappingTest(unittest.TestCase):
    def test_push_to_talk_cycle(self):
        states = [helper.parse_state(status_line(s)) for s in
                  ("initializing", "listening", "stopping", "processing", "idle")]
        self.assertEqual(states, ["starting", "listening", "processing", "processing", "idle"])

    def test_real_log_line(self):
        self.assertEqual(helper.parse_state(LISTENING), "listening")

    def test_other_states_are_idle(self):
        for state in ("dismissed", "testing", "something_new"):
            self.assertEqual(helper.parse_state(status_line(state)), "idle")

    def test_non_status_lines_are_ignored(self):
        self.assertIsNone(helper.parse_state(b"16:33:50 \xe2\x80\xba Ranked mic resolution: selected device"))
        self.assertIsNone(helper.parse_state(b'{"dictationStatus": "listening", "x": ' + b"1," * 100000 + b"}"))


class LogFollowerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "launcher.log")
        self.follower = helper.LogFollower(self.path)

    def tearDown(self):
        self.follower.close()
        self.dir.cleanup()

    def append(self, data, path=None):
        with open(path or self.path, "ab") as handle:
            handle.write(data)

    def test_existing_content_is_not_replayed(self):
        self.append(status_line("listening"))
        self.assertEqual(self.follower.lines(), [])
        self.assertTrue(self.follower.present)
        self.append(status_line("idle"))
        self.assertEqual(self.follower.lines(), [status_line("idle").rstrip(b"\n")])

    def test_absent_file_then_created_is_read_from_start(self):
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)
        self.append(status_line("initializing") + status_line("listening"))
        self.assertEqual([helper.parse_state(l) for l in self.follower.lines()], ["starting", "listening"])

    def test_partial_lines_are_held_until_complete(self):
        self.follower.lines()
        line = status_line("processing")
        self.append(line[:20])
        self.assertEqual(self.follower.lines(), [])
        self.append(line[20:])
        self.assertEqual(self.follower.lines(), [line.rstrip(b"\n")])

    def test_truncation_restarts_from_the_top(self):
        self.append(b"x" * 4096 + b"\n")
        self.follower.lines()
        with open(self.path, "wb") as handle:
            handle.write(status_line("listening"))
        self.assertEqual([helper.parse_state(l) for l in self.follower.lines()], ["listening"])

    def test_rotation_follows_the_new_file(self):
        self.append(b"old session\n")
        self.follower.lines()
        os.rename(self.path, self.path + ".1")
        self.append(b"late write to the rotated file\n", self.path + ".1")
        self.append(status_line("idle"))
        self.assertEqual([helper.parse_state(l) for l in self.follower.lines()], ["idle"])

    def test_deleted_file_reports_absent(self):
        self.append(b"line\n")
        self.follower.lines()
        os.unlink(self.path)
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.status = helper.Status(clock=lambda: self.now)

    def test_feed_reports_changes_only(self):
        self.assertTrue(self.status.feed([status_line("listening")]))
        self.assertFalse(self.status.feed([status_line("listening"), b"noise"]))
        self.assertEqual(self.status.state, "listening")

    def test_stuck_processing_expires_to_idle(self):
        self.status.feed([status_line("processing")])
        self.now = helper.STATE_TIMEOUTS["processing"] - 1
        self.assertFalse(self.status.expire())
        self.assertAlmostEqual(self.status.remaining(), 1)
        self.now += 2
        self.assertTrue(self.status.expire())
        self.assertEqual(self.status.state, "idle")
        self.assertIsNone(self.status.remaining())


class LevelTest(unittest.TestCase):
    @staticmethod
    def tone(amplitude, count=800):
        return array("h", [amplitude if i % 2 else -amplitude for i in range(count)]).tobytes()

    def test_rms_db(self):
        self.assertAlmostEqual(helper.rms_db(self.tone(32767)), 0.0, places=2)
        self.assertAlmostEqual(helper.rms_db(self.tone(3277)), -20.0, places=1)
        self.assertEqual(helper.rms_db(self.tone(0)), 20 * -6)  # clamped at 1e-6
        self.assertEqual(helper.rms_db(b""), -120.0)

    def test_background_reads_zero_and_speech_rises(self):
        floor = helper.NoiseFloor()
        for _ in range(40):
            self.assertEqual(floor.scale(-60.0), 0.0)
        self.assertAlmostEqual(floor.scale(-46.0), 0.5, places=1)
        self.assertEqual(floor.scale(0.0), 1.0)

    def test_floor_drops_instantly_and_rises_slowly(self):
        floor = helper.NoiseFloor()
        floor.scale(-30.0)
        floor.scale(-70.0)
        self.assertEqual(floor.floor, -70.0)
        floor.scale(-30.0)
        self.assertAlmostEqual(floor.floor, -70.0 + helper.NoiseFloor.RISE_DB)


class FakeStdout:
    """Just enough of pw-record's non-blocking stdout for Meter.read_levels."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.closed = False
        self._r, self._w = os.pipe()

    def read(self):
        return self.reads.pop(0) if self.reads else None

    def fileno(self):
        return self._r

    def close(self):
        self.closed = True
        os.close(self._r)
        os.close(self._w)


class FakeProc:
    def __init__(self, reads):
        self.stdout = FakeStdout(reads)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class MeterTest(unittest.TestCase):
    """Chunk framing and the EOF path, without a real pw-record."""

    def meter(self, reads):
        meter = helper.Meter(enabled=False)  # no pw-record lookup
        meter.binary = "pw-record"
        meter.proc = FakeProc(reads)
        return meter

    def test_levels_are_framed_on_chunk_boundaries(self):
        chunk = LevelTest.tone(3277, helper.CHUNK_BYTES // 2)
        half = chunk[: helper.CHUNK_BYTES // 2]
        meter = self.meter([chunk + half, half])
        first = meter.read_levels()
        self.assertEqual(len(first), 1)  # 1.5 chunks in: one level, half held
        self.assertEqual(len(meter.pending), helper.CHUNK_BYTES // 2)
        self.assertEqual(len(meter.read_levels()), 1)  # the held half completes
        self.assertEqual(meter.pending, b"")

    def test_no_data_yet_yields_nothing(self):
        meter = self.meter([None])
        self.assertEqual(meter.read_levels(), [])
        self.assertIsNotNone(meter.proc)

    def test_eof_marks_the_meter_failed_and_stops_respawning(self):
        meter = self.meter([b""])
        proc = meter.proc
        self.assertEqual(meter.read_levels(), [])
        self.assertTrue(proc.terminated)
        self.assertTrue(meter.failed)
        self.assertFalse(meter.available)
        meter.start()  # a dead pw-record must not be restarted in a loop
        self.assertIsNone(meter.proc)
        self.assertEqual(meter.read_levels(), [])


class CaptureProtocolTest(unittest.TestCase):
    """`capture 1` / `capture 0` on stdin start and stop the meter."""

    PW_RECORD_STUB = r"""#!/usr/bin/env python3
import sys, time
while True:  # silence, in chunks the helper can frame
    sys.stdout.buffer.write(b"\0" * 1600)
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        stub = os.path.join(self.dir.name, "pw-record")
        with open(stub, "w") as handle:
            handle.write(self.PW_RECORD_STUB)
        os.chmod(stub, 0o755)
        env = dict(os.environ, PATH=self.dir.name + os.pathsep + os.environ["PATH"])
        self.proc = subprocess.Popen(
            [sys.executable, HELPER, "--log", os.path.join(self.dir.name, "launcher.log")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.dir.cleanup()

    def records(self, seconds):
        out, deadline = [], time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if ready:
                out.append(json.loads(self.proc.stdout.readline()))

    def send(self, command):
        self.proc.stdin.write(command)
        self.proc.stdin.flush()

    def test_capture_hint_starts_and_stops_the_meter(self):
        ready, _, _ = select.select([self.proc.stdout], [], [], 3)
        self.assertTrue(ready, "helper produced no output")
        first = json.loads(self.proc.stdout.readline())
        self.assertEqual((first["state"], first["meter"]), ("idle", True))
        self.assertIsNone(first["level"])

        self.send(b"capture 1\n")
        levels = [r["level"] for r in self.records(1.5) if r["level"] is not None]
        self.assertTrue(levels, "capture 1 produced no levels")
        self.assertEqual(set(levels), {0.0})  # the stub records silence

        self.send(b"capture 0\n")
        self.records(0.5)  # drain whatever was already in flight
        self.assertEqual([r for r in self.records(1) if r["level"] is not None], [])


class HelperProcessTest(unittest.TestCase):
    """The helper as the shell runs it: JSON on stdout, commands on stdin."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "wispr", "launcher.log")
        self.proc = subprocess.Popen(
            [sys.executable, HELPER, "--log", self.path, "--no-meter"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.dir.cleanup()

    def read(self, timeout=3.0):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        self.assertTrue(ready, "helper produced no output")
        return json.loads(self.proc.stdout.readline())

    def test_follows_a_log_that_appears_later(self):
        self.assertEqual(self.read(), {"state": "idle", "level": None, "log": False, "meter": False})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "ab") as handle:
            handle.write(status_line("initializing"))
            handle.flush()
            self.assertEqual(self.read()["state"], "starting")
            handle.write(status_line("listening"))
            handle.flush()
            record = self.read()
            self.assertEqual((record["state"], record["log"]), ("listening", True))

    def test_exits_when_stdin_closes(self):
        self.read()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(3), 0)

    def test_exits_cleanly_on_sigterm(self):
        self.read()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(3), 0)


if __name__ == "__main__":
    unittest.main()
