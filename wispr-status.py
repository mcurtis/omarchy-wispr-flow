#!/usr/bin/env python3
"""Stream Wispr Flow dictation state as JSON lines for the Omarchy bar widget.

Follows the wispr-flow-linux launcher log (Electron stdout) for
`updateDictationStatus: <state>` lines and prints one JSON object per line:

    {"state": "idle|starting|listening|processing", "level": 0.0-1.0}

While listening it also captures the default microphone with pw-record and
emits a smoothed level ~20 times a second, so the bar can draw a live meter.
Runs as the bar's direct child (setpriv --pdeathsig TERM) so it dies with it.
"""
import array
import json
import math
import os
import re
import signal
import subprocess
import sys
import time

LOG_PATH = os.environ.get("WISPR_STATUS_LOG") or os.path.expanduser(
    "~/.cache/wispr-flow/launcher.log"
)
STATE_RE = re.compile(rb"updateDictationStatus: ([a-z_]+)")
STATE_MAP = {
    "initializing": "starting",
    "listening": "listening",
    "stopping": "processing",
    "processing": "processing",
}
RATE = 16000
CHUNK_BYTES = RATE * 50 // 1000 * 2  # 50 ms of s16 mono
LISTEN_TIMEOUT = 15 * 60  # Wispr caps dictations well below this
PROCESS_TIMEOUT = 90  # transcription stuck (app died mid-dictation)


def emit(state, level=0.0):
    sys.stdout.write(json.dumps({"state": state, "level": round(level, 3)}) + "\n")
    sys.stdout.flush()


class LogFollower:
    """tail -F in-process: survives the file being absent, truncated, or rotated."""

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.inode = None
        self.buffer = b""

    def _open(self, seek_end):
        try:
            handle = open(self.path, "rb")
        except OSError:
            return False
        self.handle = handle
        self.inode = os.fstat(handle.fileno()).st_ino
        self.buffer = b""
        if seek_end:
            handle.seek(0, os.SEEK_END)
        return True

    def _close(self):
        if self.handle:
            self.handle.close()
        self.handle = None

    def lines(self):
        if self.handle is None and not self._open(seek_end=True):
            return []
        try:
            stat = os.stat(self.path)
        except OSError:
            self._close()
            return []
        if stat.st_ino != self.inode or stat.st_size < self.handle.tell():
            self._close()
            if not self._open(seek_end=False):
                return []
        data = self.handle.read()
        if not data:
            return []
        self.buffer += data
        parts = self.buffer.split(b"\n")
        self.buffer = parts.pop()
        return parts


class Meter:
    """Default-source level via pw-record, with an adaptive noise floor."""

    def __init__(self):
        self.proc = None
        self.pending = b""
        self.floor = None

    def start(self):
        if self.proc:
            return
        try:
            self.proc = subprocess.Popen(
                ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            os.set_blocking(self.proc.stdout.fileno(), False)
        except OSError:
            self.proc = None
        self.pending = b""
        self.floor = None

    def stop(self):
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def level(self):
        if not self.proc:
            return None
        try:
            data = self.proc.stdout.read()
        except BlockingIOError:
            data = None
        if data:
            self.pending += data
        if len(self.pending) < CHUNK_BYTES:
            return None
        usable = len(self.pending) // CHUNK_BYTES * CHUNK_BYTES
        chunk = self.pending[usable - CHUNK_BYTES : usable]  # newest full chunk
        self.pending = self.pending[usable:]
        samples = array.array("h", chunk)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
        db = 20 * math.log10(max(rms, 1e-6))
        if self.floor is None:
            self.floor = db
        self.floor = min(db, self.floor + 0.15)  # rises slowly, drops instantly
        return max(0.0, min(1.0, (db - self.floor - 3.0) / 22.0))


def main():
    follower = LogFollower(LOG_PATH)
    meter = Meter()
    state = "idle"
    since = time.monotonic()

    def shutdown(*_):
        meter.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    emit(state)

    while True:
        for line in follower.lines():
            match = STATE_RE.search(line)
            if not match:
                continue
            new_state = STATE_MAP.get(match.group(1).decode(), "idle")
            if new_state == state:
                continue
            state = new_state
            since = time.monotonic()
            if state == "listening":
                meter.start()
            else:
                meter.stop()
            emit(state)

        elapsed = time.monotonic() - since
        if (state == "listening" and elapsed > LISTEN_TIMEOUT) or (
            state in ("starting", "processing") and elapsed > PROCESS_TIMEOUT
        ):
            state = "idle"
            meter.stop()
            emit(state)

        if state == "listening":
            level = meter.level()
            if level is not None:
                emit(state, level)
            time.sleep(0.03)
        else:
            time.sleep(0.2)


if __name__ == "__main__":
    main()
