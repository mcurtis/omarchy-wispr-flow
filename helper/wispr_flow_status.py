#!/usr/bin/env python3
"""Stream Wispr Flow dictation state as JSON lines for the Omarchy bar.

Follows the wispr-flow-linux launcher log (Electron stdout) for
`updateDictationStatus: <state>` lines and writes one JSON object per line:

    {"state": "idle|starting|listening|processing", "level": 0.0-1.0 | null,
     "log": true|false, "meter": true|false}

`state` is what the log says, `log` whether the log file is being followed,
`meter` whether a level meter can run. While the log says listening, or the
parent reports a Wispr capture stream on stdin (`capture 1` / `capture 0`),
the default microphone is sampled with pw-record and a level is emitted ~20
times a second. The parent owns the merge with PipeWire; this process only
reports. A record is repeated at least every HEARTBEAT seconds, so the parent
can tell a quiet helper from a stuck one.

Boundaries: stdlib only. Every executable is a fixed /usr/bin path and libc is
the running interpreter's own, so nothing is looked up through PATH or the
loader's search paths. The log is opened without following a symlink and is
followed only while it is a regular file owned by this user, and every read,
line and backlog has a fixed byte budget. The shell starts this file as
`setsid setpriv --pdeathsig TERM python3 -I -S` with a closed environment;
it also exits when its stdin closes, so it cannot outlive the shell.
"""
import argparse
import array
import ctypes
import json
import math
import os
import re
import select
import signal
import stat
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

# Fixed executables, never searched for on PATH.
PW_RECORD = "/usr/bin/pw-record"
SETPRIV = "/usr/bin/setpriv"
# All pw-record gets of our environment: enough to find the PipeWire socket.
CHILD_ENV_KEYS = ("XDG_RUNTIME_DIR", "PIPEWIRE_RUNTIME_DIR", "PIPEWIRE_REMOTE")

RATE = 16000
CHUNK_BYTES = RATE * 50 // 1000 * 2  # 50 ms of s16 mono => 20 Hz levels

# Recovery from an app that died mid-dictation: the log never gets its idle.
STATE_TIMEOUTS = {"starting": 15.0, "processing": 60.0, "listening": 15 * 60.0}

# inotify makes idle cost nothing; the slow poll only backs it up (and is the
# whole mechanism while the log directory does not exist yet).
POLL_WATCHED = 5.0
POLL_UNWATCHED = 1.0
# A record at least this often, changed or not: the parent's liveness signal.
HEARTBEAT = 5.0
# Commands from the parent are a few bytes; more than this is not the parent.
STDIN_LIMIT = 4096


def executable(path):
    """`path` when it names an executable file, else None. No PATH search."""
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def child_environment():
    """The closed environment a child gets: only CHILD_ENV_KEYS, when set."""
    return {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}


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
    """In-process `tail -F` with a fixed budget.

    The path is opened without following a final symlink, and is followed only
    while it names a regular file owned by this user; anything else counts as
    absent. Each poll reads at most READ_LIMIT bytes (`more` says a backlog is
    left), a line longer than LINE_LIMIT is dropped whole, and a replacement
    file larger than BACKLOG_LIMIT is picked up at its end, not replayed.

    The first open seeks to the end so an old session's last state is not
    replayed; a file that appears or is replaced later is read from the start.
    """

    OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
    READ_LIMIT = 64 * 1024
    LINE_LIMIT = 64 * 1024
    BACKLOG_LIMIT = 1024 * 1024

    def __init__(self, path):
        self.path = path
        self.fd = None
        self.identity = None  # (st_dev, st_ino) of the open file
        self.offset = 0  # bytes consumed from it
        self.buffer = b""  # partial trailing line
        self.skipping = False  # inside a line that went over LINE_LIMIT
        self.more = False  # the last read hit READ_LIMIT: poll again at once
        self.opened_once = False

    @property
    def present(self):
        return self.fd is not None

    @staticmethod
    def acceptable(st):
        return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()

    def _open(self):
        try:
            fd = os.open(self.path, self.OPEN_FLAGS)
        except OSError:  # absent, unreadable, or a symlink (ELOOP)
            return False
        st = os.fstat(fd)
        if not self.acceptable(st):
            os.close(fd)
            return False
        # An old session's tail is not replayed, and neither is an oversized
        # replacement, which is not a fresh session's log either.
        skip = not self.opened_once or st.st_size > self.BACKLOG_LIMIT
        self.fd = fd
        self.identity = (st.st_dev, st.st_ino)
        self.offset = os.lseek(fd, st.st_size if skip else 0, os.SEEK_SET)
        self.buffer = b""
        self.skipping = False
        self.opened_once = True
        return True

    def _close(self):
        if self.fd is not None:
            os.close(self.fd)
        self.fd = None
        self.identity = None
        self.buffer = b""
        self.skipping = False
        self.more = False

    def close(self):
        self._close()

    def _changed(self):
        """The path no longer names the open file, or the file was truncated."""
        try:
            st = os.lstat(self.path)
        except OSError:
            return True
        return (
            not self.acceptable(st)
            or (st.st_dev, st.st_ino) != self.identity
            or st.st_size < self.offset
        )

    def lines(self):
        """Complete lines appended since the last call, from at most READ_LIMIT bytes."""
        self.more = False
        if self.fd is None:
            if not self._open():
                self.opened_once = True  # a file created later is new content
                return []
        elif self._changed():
            self._close()
            if not self._open():
                return []
        try:
            data = os.read(self.fd, self.READ_LIMIT)
        except OSError:
            self._close()
            return []
        if not data:
            return []
        self.offset += len(data)
        self.more = len(data) == self.READ_LIMIT
        return self._split(data)

    def _split(self, data):
        if self.skipping:  # the rest of an overlong line goes too
            cut = data.find(b"\n")
            if cut < 0:
                return []
            data = data[cut + 1:]
            self.skipping = False
        parts = (self.buffer + data).split(b"\n")
        self.buffer = parts.pop()  # keep a partial trailing line for next time
        if len(self.buffer) > self.LINE_LIMIT:
            self.buffer = b""
            self.skipping = True
        return [part for part in parts if len(part) <= self.LINE_LIMIT]


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
        self.binary = executable(PW_RECORD) if enabled else None
        self.setpriv = executable(SETPRIV)
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
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_environment(),
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
        try:
            # The interpreter's own libc: no library search, no ldconfig or
            # compiler probing the way ctypes.util.find_library would do it.
            self.libc = ctypes.CDLL(None, use_errno=True)
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
    last_emit = 0.0

    def report(level=None, force=False):
        nonlocal last, last_emit
        snapshot = (status.state, follower.present, meter.available)
        if level is None and not force and snapshot == last:
            return
        last = snapshot
        last_emit = time.monotonic()
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
            timeout = min(timeout, max(0.0, last_emit + HEARTBEAT - time.monotonic()))
            if follower.more:
                timeout = 0.0  # a backlog is waiting; keep draining it
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
                    if len(stdin_buffer) > STDIN_LIMIT:
                        stdin_buffer = b""
                    for command in commands:
                        if command.strip() == b"capture 1":
                            capture_hint = True
                        elif command.strip() == b"capture 0":
                            capture_hint = False
            if watch.fd is not None and watch.fd in readable:
                watch.drain()

            status.feed(follower.lines())
            status.expire()
            report(force=time.monotonic() - last_emit >= HEARTBEAT)

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
