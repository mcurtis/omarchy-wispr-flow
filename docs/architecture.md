# Architecture

The plugin is a service that owns the dictation state and a bar widget that
draws it. The service combines two independent signals, so either one failing
leaves the other working.

```mermaid
flowchart LR
  subgraph Wispr["Wispr Flow"]
    mic["capture stream<br/>Stream/Input/Audio<br/>binary = processName"]
    log["launcher.log<br/>updateDictationStatus: &lt;state&gt;"]
  end
  subgraph Plugin["omarchy-shell"]
    pw["PipeWire nodes<br/>(Quickshell, no process)"]
    helper["helper<br/>log follower + pw-record level"]
    svc["Service.qml<br/>merge rule + timeouts"]
    bar["BarWidget.qml"]
  end
  mic --> pw -->|listening| svc
  log --> helper -->|"JSON lines {state, level}"| svc
  svc -->|state, level, history| bar
```

## Merge rule

| PipeWire stream | Log state | Shown |
|---|---|---|
| present | anything | listening |
| absent | `initializing` | starting |
| absent | `listening` | listening |
| absent | `stopping`, `processing` | processing |
| absent | `idle`, `dismissed`, none | idle |

Listening wins from either side, because the PipeWire stream is the one signal
that cannot drift from what the microphone is actually doing. Starting and
processing exist only in the log, so without the helper the widget goes
straight from idle to listening and back.

A dictation that never reaches `idle`, because Wispr crashed or the log was
replaced mid-dictation, is ended by a timeout rather than holding the widget
open.

## The helper

The helper is a single Python file using only the standard library. The shell
starts it as a direct child under `setpriv --pdeathsig TERM`, so it dies with
the shell even if the shell is killed. It:

- follows the log like `tail -F`, reopening it when it is created, truncated or
  replaced, and buffering partial lines so a state line split across two writes
  is still read once;
- matches only `updateDictationStatus: <state>`, ignoring the multi-kilobyte
  JSON dumps that share the file;
- starts `pw-record` only while listening and computes an RMS level against an
  adaptive noise floor, emitting about 20 levels a second;
- stops `pw-record` on every exit path, including SIGTERM.

If Python, the log or `pw-record` is unavailable, the service reports a
`degraded` reason that the tooltip shows, and keeps showing listening from
PipeWire.
