import json
import os
import select
import signal
import subprocess
import sys
import tempfile
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
