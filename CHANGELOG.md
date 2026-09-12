# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-09-12

Hardening from the marketplace security review; no change to what the widget
shows.

### Security

- Every executable the plugin starts is a fixed `/usr/bin` path: `setsid`,
  `setpriv`, `python3` and `pw-record` are no longer looked up on `PATH`, and
  clicking the widget runs `/usr/bin/wispr-flow` directly instead of through
  a shell.
- The helper runs under `python3 -I -S` with a closed environment (only
  `XDG_RUNTIME_DIR` and the PipeWire socket variables pass through, to
  `pw-record` as well), and finds inotify in the interpreter's own libc
  instead of searching for a library.
- The launcher log is opened without following a symlink and is followed only
  while it is a regular file owned by the user, with fixed budgets for each
  read (64 KiB), each line (64 KiB) and a replaced file's backlog (1 MiB).
- The helper repeats its record at least every 5 seconds; the service
  assembles its output under a 4 KiB line budget and kills a helper that
  exceeds it or goes silent for 30 seconds.
- The helper leads its own process group (`setsid`), and every stop, restart
  and reload signals the whole group, TERM then KILL after two seconds.

### Added

- `helperPid` in the IPC `status` output.

## [0.1.0] - 2026-09-11

### Added

- Bar widget that stays hidden while Wispr Flow is idle and slides in next to
  the built-in indicators when a dictation starts.
- Listening state with a mic glyph and a scrolling level meter, and a pulsing
  hourglass while the transcription is processed.
- Service that detects listening natively from Wispr's PipeWire capture stream,
  so the widget works with no helper process at all.
- Optional log-follower helper (Python, standard library only) that adds the
  starting and processing states and the level meter.
- Degraded mode with the reason in the tooltip when the helper or `pw-record`
  is unavailable.
- Settings: `bars`, `meter`, `logPath`, `processName`.
- IPC `status` command that prints the current state as JSON.
- `scripts/simulate.sh` to walk the states without Wispr Flow, and a
  `unittest` suite for the helper.
