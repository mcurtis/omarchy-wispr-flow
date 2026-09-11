# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
