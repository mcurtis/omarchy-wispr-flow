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


def spawn_helper(args, patch=None):
    """The helper as the shell runs it: isolated interpreter, closed environment.

    With `patch`, the module is imported and patched (Python source) before
    main() runs, so tests can shorten timers or point PW_RECORD at a stub
    without the helper growing a switch for it.
    """
    if patch is None:
        command = [sys.executable, "-I", "-S", HELPER]
    else:
        code = "import sys; sys.path.insert(0, %r); import wispr_flow_status as h; %s; h.main(sys.argv[1:])" % (
            HELPER_DIR, patch)
        command = [sys.executable, "-I", "-S", "-c", code]
    return subprocess.Popen(command + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={})


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

    def states(self):
        return [helper.parse_state(line) for line in self.follower.lines()]

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
        self.assertEqual(self.states(), ["starting", "listening"])

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
        self.assertEqual(self.states(), ["listening"])

    def test_rotation_follows_the_new_file(self):
        self.append(b"old session\n")
        self.follower.lines()
        os.rename(self.path, self.path + ".1")
        self.append(b"late write to the rotated file\n", self.path + ".1")
        self.append(status_line("idle"))
        self.assertEqual(self.states(), ["idle"])

    def test_deleted_file_reports_absent(self):
        self.append(b"line\n")
        self.follower.lines()
        os.unlink(self.path)
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)

    # Boundaries: what the log path may point at, and how much is read.

    def test_symlink_is_not_followed(self):
        real = os.path.join(self.dir.name, "real.log")
        self.append(status_line("listening"), real)
        os.symlink(real, self.path)
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)
        self.append(status_line("idle"), real)
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)

    def test_fifo_is_not_a_log(self):
        os.mkfifo(self.path)
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)

    def test_file_swapped_for_a_symlink_is_dropped(self):
        self.append(b"line\n")
        self.follower.lines()
        self.assertTrue(self.follower.present)
        os.rename(self.path, self.path + ".real")
        os.symlink(self.path + ".real", self.path)
        self.append(status_line("listening"), self.path + ".real")
        self.assertEqual(self.follower.lines(), [])
        self.assertFalse(self.follower.present)

    def test_reads_are_capped_and_a_backlog_is_flagged(self):
        self.follower.READ_LIMIT = 256
        self.follower.lines()
        self.append(status_line("listening") * 5)  # about 425 bytes
        first = self.follower.lines()
        self.assertTrue(self.follower.more)
        self.assertTrue(0 < len(first) < 5)
        rest = self.follower.lines()
        self.assertFalse(self.follower.more)
        self.assertEqual(len(first) + len(rest), 5)

    def test_overlong_partial_line_is_dropped_whole(self):
        self.follower.LINE_LIMIT = 100
        self.follower.lines()
        self.append(b"x" * 150)  # no newline yet
        self.assertEqual(self.follower.lines(), [])
        self.assertEqual(self.follower.buffer, b"")
        # The tail of that line must not surface as a line of its own.
        self.append(b"updateDictationStatus: listening\n" + status_line("processing"))
        self.assertEqual(self.states(), ["processing"])

    def test_overlong_complete_line_is_dropped(self):
        self.follower.LINE_LIMIT = 100
        self.follower.lines()
        self.append(b"y" * 150 + b"\n" + status_line("idle"))
        self.assertEqual(self.states(), ["idle"])

    def test_oversized_replacement_is_taken_from_the_end(self):
        self.follower.BACKLOG_LIMIT = 1024
        self.append(b"old\n")
        self.follower.lines()
        big = os.path.join(self.dir.name, "big.log")
        self.append(b"z" * 2000 + b"\n" + status_line("listening"), big)
        os.rename(big, self.path)
        self.assertEqual(self.follower.lines(), [])
        self.assertTrue(self.follower.present)
        self.append(status_line("idle"))
        self.assertEqual(self.states(), ["idle"])


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


class BoundaryTest(unittest.TestCase):
    """Fixed executables, a closed child environment, and libc without a search."""

    def test_executables_are_fixed_paths_never_searched_on_path(self):
        with tempfile.TemporaryDirectory() as fake:
            for name in ("pw-record", "setpriv"):
                stub = os.path.join(fake, name)
                with open(stub, "w") as handle:
                    handle.write("#!/bin/sh\n")
                os.chmod(stub, 0o755)
            saved = os.environ.get("PATH")
            os.environ["PATH"] = fake
            try:
                meter = helper.Meter(enabled=True)
            finally:
                if saved is None:
                    del os.environ["PATH"]
                else:
                    os.environ["PATH"] = saved
        self.assertTrue(helper.PW_RECORD.startswith("/usr/bin/"))
        self.assertTrue(helper.SETPRIV.startswith("/usr/bin/"))
        self.assertIn(meter.binary, (None, helper.PW_RECORD))
        self.assertIn(meter.setpriv, (None, helper.SETPRIV))
        self.assertIsNone(helper.executable(os.path.join(fake, "pw-record")))  # gone with the directory

    def test_child_environment_passes_only_the_pipewire_keys(self):
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update({"XDG_RUNTIME_DIR": "/run/user/1", "PATH": "/x", "LD_PRELOAD": "/y", "PIPEWIRE_REMOTE": "r"})
        try:
            self.assertEqual(helper.child_environment(), {"XDG_RUNTIME_DIR": "/run/user/1", "PIPEWIRE_REMOTE": "r"})
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_inotify_comes_from_the_interpreters_libc(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = helper.DirWatch(directory)
            try:
                self.assertIsNotNone(watch.fd)
                watch.ensure()
                self.assertTrue(watch.active)
            finally:
                watch.close()


class CaptureProtocolTest(unittest.TestCase):
    """`capture 1` / `capture 0` on stdin start and stop the meter."""

    PW_RECORD_STUB = """#!%s
import sys, time
while True:  # silence, in chunks the helper can frame
    sys.stdout.buffer.write(b"\\0" * 1600)
    sys.stdout.buffer.flush()
    time.sleep(0.05)
""" % sys.executable

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        stub = os.path.join(self.dir.name, "pw-record")
        with open(stub, "w") as handle:
            handle.write(self.PW_RECORD_STUB)
        os.chmod(stub, 0o755)
        self.proc = spawn_helper(["--log", os.path.join(self.dir.name, "launcher.log")],
                                 patch="h.PW_RECORD = %r" % stub)

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
        self.proc = None

    def tearDown(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdin.close()
            self.proc.stdout.close()
        self.dir.cleanup()

    def start(self, patch=None):
        self.proc = spawn_helper(["--log", self.path, "--no-meter"], patch=patch)

    def read(self, timeout=3.0):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        self.assertTrue(ready, "helper produced no output")
        return json.loads(self.proc.stdout.readline())

    def test_follows_a_log_that_appears_later(self):
        self.start()
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

    def test_heartbeat_repeats_the_record_while_nothing_changes(self):
        self.start(patch="h.HEARTBEAT = 0.3")
        first = self.read()
        started = time.monotonic()
        self.assertEqual(self.read(timeout=1.5), first)
        self.assertGreater(time.monotonic() - started, 0.2)

    def test_exits_when_stdin_closes(self):
        self.start()
        self.read()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(3), 0)

    def test_exits_cleanly_on_sigterm(self):
        self.start()
        self.read()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(3), 0)


if __name__ == "__main__":
    unittest.main()
