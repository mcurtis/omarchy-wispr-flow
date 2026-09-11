#!/usr/bin/env python3
"""Stream Wispr Flow dictation state as JSON lines for the Omarchy bar.

Follows the wispr-flow-linux launcher log (Electron stdout) for
`updateDictationStatus: <state>` lines and writes one JSON object per line:

    {"state": "idle|starting|listening|processing", "level": 0.0-1.0 | null,
     "log": true|false, "meter": true|false}

`state` is what the log says, `log` whether the log file exists, `meter`
whether a level meter can run. While the log says listening, or the parent
reports a Wispr capture stream on stdin (`capture 1` / `capture 0`), the
default microphone is sampled with pw-record and a level is emitted ~20 times
a second. The parent owns the merge with PipeWire; this process only reports.

Stdlib only. Runs as the shell's direct child under `setpriv --pdeathsig TERM`
and also exits when its stdin closes, so it cannot outlive the shell.
"""
import argparse
import array
import ctypes
import ctypes.util
import json
import math
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

DEFAULT_LOG = "~/.cache/wispr-flow/launcher.log"
STATE_RE = re.compile(rb"updateDictationStatus: ([a-z_]+)")
STATE_MAP = {
    "initializing": "starting",
    "listening": "listening",
    "stopping": "processing",
    "processing": "processing",
}

RATE = 16000
CHUNK_BYTES = RATE * 50 // 1000 * 2  # 50 ms of s16 mono => 20 Hz levels

# Recovery from an app that died mid-dictation: the log never gets its idle.
STATE_TIMEOUTS = {"starting": 15.0, "processing": 60.0, "listening": 15 * 60.0}

# inotify makes idle cost nothing; the slow poll only backs it up (and is the
# whole mechanism while the log directory does not exist yet).
POLL_WATCHED = 5.0
POLL_UNWATCHED = 1.0


def map_state(raw):
    """Wispr's status word -> the four states the bar distinguishes."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    return STATE_MAP.get(raw, "idle")


def parse_state(line):
    """Mapped state for a log line, or None when the line is not a status line."""
    match = STATE_RE.search(line)
    return map_state(match.group(1)) if match else None


class LogFollower:
    """In-process `tail -F`: survives the file being absent, truncated or replaced.

    The first open seeks to the end so an old session's last state is not
    replayed; a file that appears or is replaced later is read from the start.
    """

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.inode = None
        self.buffer = b""
        self.opened_once = False

    @property
    def present(self):
        return self.handle is not None

    def _open(self):
        try:
            handle = open(self.path, "rb")
        except OSError:
            return False
        self.handle = handle
        self.inode = os.fstat(handle.fileno()).st_ino
        self.buffer = b""
        if not self.opened_once:
            handle.seek(0, os.SEEK_END)
        self.opened_once = True
        return True

    def _close(self):
        if self.handle:
            self.handle.close()
        self.handle = None
        self.buffer = b""

    def close(self):
        self._close()

    def lines(self):
        """Complete lines appended since the last call."""
        if self.handle is None:
            if not self._open():
                self.opened_once = True  # a file created later is new content
                return []
        try:
            stat = os.stat(self.path)
        except OSError:
            self._close()
            return []
        if stat.st_ino != self.inode or stat.st_size < self.handle.tell():
            self._close()
            if not self._open():
                return []
        data = self.handle.read()
        if not data:
            return []
        self.buffer += data
        parts = self.buffer.split(b"\n")
        self.buffer = parts.pop()  # keep a partial trailing line for next time
        return parts


class NoiseFloor:
    """Adaptive floor in dBFS: drops instantly to quiet input, creeps up slowly.

    A room's background is whatever the quietest recent chunks are, so speech
    stands out on any microphone gain without a calibration setting.
    """

    RISE_DB = 0.15  # per chunk; ~3 dB/s at 20 Hz
    HEADROOM_DB = 3.0
    RANGE_DB = 22.0

    def __init__(self):
        self.floor = None

    def reset(self):
        self.floor = None

    def scale(self, db):
        if self.floor is None:
            self.floor = db
        self.floor = min(db, self.floor + self.RISE_DB)
        return max(0.0, min(1.0, (db - self.floor - self.HEADROOM_DB) / self.RANGE_DB))


def rms_db(chunk):
    """RMS of little-endian s16 samples in dBFS (clamped at -120)."""
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    return 20 * math.log10(max(rms, 1e-6))


class Meter:
    """Default-source capture through pw-record; one level per full chunk."""

    def __init__(self, enabled):
        self.binary = shutil.which("pw-record") if enabled else None
        self.setpriv = shutil.which("setpriv")
        self.proc = None
        self.failed = False  # pw-record quit on its own during this capture
        self.pending = b""
        self.floor = NoiseFloor()

    @property
    def available(self):
        return self.binary is not None and not self.failed

    @property
    def fd(self):
        return self.proc.stdout.fileno() if self.proc else None

    def start(self):
        if self.proc or not self.available:
            return
        command = [self.binary, "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"]
        if self.setpriv:
            # pw-record must die with us even if we are SIGKILLed.
            command = [self.setpriv, "--pdeathsig", "TERM"] + command
        try:
            self.proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError:
            self.proc = None
            self.binary = None
            return
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.pending = b""
        self.floor.reset()

    def stop(self):
        if not self.proc:
            return
        proc, self.proc = self.proc, None
        proc.terminate()
        try:
            proc.wait(1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        self.pending = b""

    def read_levels(self):
        """Levels for every complete chunk read; stops the meter on EOF."""
        if not self.proc:
            return []
        try:
            data = self.proc.stdout.read()
        except BlockingIOError:
            return []
        if data is None:
            return []
        if data == b"":  # EOF: pw-record quit (no PipeWire, no source, ...)
            self.stop()
            self.failed = True  # do not respawn it in a tight loop
            return []
        self.pending += data
        levels = []
        while len(self.pending) >= CHUNK_BYTES:
            chunk, self.pending = self.pending[:CHUNK_BYTES], self.pending[CHUNK_BYTES:]
            levels.append(self.floor.scale(rms_db(chunk)))
        return levels


class DirWatch:
    """inotify on the log's directory through libc; inert where unavailable."""

    MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
    # IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO |
    # IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
    IN_IGNORED = 0x8000
    IN_NONBLOCK = 0o4000
    IN_CLOEXEC = 0o2000000

    def __init__(self, directory):
        self.directory = directory
        self.fd = None
        self.wd = None
        self.libc = None
        name = ctypes.util.find_library("c")
        if not name:
            return
        try:
            self.libc = ctypes.CDLL(name, use_errno=True)
            fd = self.libc.inotify_init1(self.IN_NONBLOCK | self.IN_CLOEXEC)
        except (OSError, AttributeError):
            self.libc = None
            return
        self.fd = fd if fd >= 0 else None

    @property
    def active(self):
        return self.wd is not None

    def ensure(self):
        """(Re)attach the watch once the directory exists."""
        if self.fd is None or self.wd is not None:
            return
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(self.directory), self.MASK)
        self.wd = wd if wd >= 0 else None

    def drain(self):
        while self.fd is not None:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                return
            offset = 0
            while offset + 16 <= len(data):
                _, mask, _, length = struct.unpack_from("iIII", data, offset)
                if mask & self.IN_IGNORED:
                    self.wd = None  # directory went away; ensure() re-adds it
                offset += 16 + length

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
        self.fd = None


class Status:
    """Log-derived state with recovery from states the app never left."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = "idle"
        self.since = clock()

    def feed(self, lines):
        changed = False
        for line in lines:
            state = parse_state(line)
            if state is not None and state != self.state:
                self.state = state
                self.since = self.clock()
                changed = True
        return changed

    def expire(self):
        limit = STATE_TIMEOUTS.get(self.state)
        if limit is not None and self.clock() - self.since > limit:
            self.state = "idle"
            self.since = self.clock()
            return True
        return False

    def remaining(self):
        limit = STATE_TIMEOUTS.get(self.state)
        return None if limit is None else max(0.0, limit - (self.clock() - self.since))


def emit(out, state, level, log, meter):
    record = {"state": state, "level": None if level is None else round(level, 3), "log": log, "meter": meter}
    out.write(json.dumps(record) + "\n")
    out.flush()


def run(log_path, meter_enabled, stdin=sys.stdin.buffer, out=sys.stdout):
    follower = LogFollower(log_path)
    watch = DirWatch(os.path.dirname(log_path) or ".")
    meter = Meter(meter_enabled)
    status = Status()
    capture_hint = False
    stdin_fd = stdin.fileno()
    os.set_blocking(stdin_fd, False)
    stdin_buffer = b""

    follower.lines()
    last = None

    def report(level=None):
        nonlocal last
        snapshot = (status.state, follower.present, meter.available)
        if level is None and snapshot == last:
            return
        last = snapshot
        emit(out, status.state, level, follower.present, meter.available)

    report()
    try:
        while True:
            watch.ensure()
            if status.state == "listening" or capture_hint:
                meter.start()
            else:
                meter.stop()
                meter.failed = False

            fds = [stdin_fd]
            if watch.fd is not None and watch.active:
                fds.append(watch.fd)
            if meter.fd is not None:
                fds.append(meter.fd)
            timeout = POLL_WATCHED if watch.active else POLL_UNWATCHED
            remaining = status.remaining()
            if remaining is not None:
                timeout = min(timeout, remaining + 0.05)
            readable, _, _ = select.select(fds, [], [], timeout)

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 4096)
                except BlockingIOError:
                    data = None
                if data == b"":
                    return  # parent closed our stdin: it is gone or replacing us
                if data:
                    stdin_buffer += data
                    *commands, stdin_buffer = stdin_buffer.split(b"\n")
                    for command in commands:
                        if command.strip() == b"capture 1":
                            capture_hint = True
                        elif command.strip() == b"capture 0":
                            capture_hint = False
            if watch.fd is not None and watch.fd in readable:
                watch.drain()

            status.feed(follower.lines())
            status.expire()
            report()

            if meter.fd is not None and meter.fd in readable:
                for level in meter.read_levels():
                    report(level)
    finally:
        meter.stop()
        follower.close()
        watch.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", default="", help="launcher log path (default %s)" % DEFAULT_LOG)
    parser.add_argument("--no-meter", action="store_true", help="never start pw-record")
    args = parser.parse_args(argv)
    log_path = os.path.expanduser(args.log or DEFAULT_LOG)

    def terminate(*_):
        raise SystemExit(0)  # unwinds through run()'s finally, stopping pw-record

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        run(log_path, not args.no_meter)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
