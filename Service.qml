import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire

// Wispr Flow dictation state, owned once per shell and shared by every bar
// instance. Two signals are merged:
//   - PipeWire: Wispr's capture stream exists exactly while it listens. Native
//     and log-format independent, so the widget works with no helper at all.
//   - helper/wispr_flow_status.py: follows the launcher log for the starting and
//     processing states and samples the microphone for the level meter.
//
// The helper is the only process this plugin starts, and it is confined: fixed
// /usr/bin executables (no PATH lookup), an isolated interpreter (-I -S) in a
// closed environment, its own process group so the whole tree is signalled
// together, a byte budget on what it may send, and a deadline by which it must
// have said something. The README's "The helper's boundaries" has the summary.
//
// Not keepLoaded: nothing here must survive a plugin hot-reload, and a kept
// instance would keep running old code (and an old helper) until a shell
// restart. The host destroys and recreates this object on reload instead.
Scope {
  id: root

  // Injected by the host: a facade scoped to this plugin.
  property var shell: null
  property var manifest: null

  // Services receive no settings of their own; the bar widget pushes its
  // entry's settings here (see BarWidget.qml).
  property string logPath: ""
  property string processName: "wispr-flow"
  property bool meterEnabled: true

  readonly property int historySize: 12 // the widget's maximum bar count
  readonly property int staleGraceMs: 3000
  // The helper repeats a record at least every 5 s (HEARTBEAT in the helper).
  // Silence past this deadline means it is stuck or replaced: it is killed.
  readonly property int helperDeadlineMs: 30000
  // A real record is under 100 characters; anything near this is not the helper.
  readonly property int helperLineBudget: 4096
  // TERM goes to the helper's process group; KILL follows for whatever is left.
  readonly property int killGraceMs: 2000

  property string logState: "idle"
  property bool logPresent: false
  property bool helperMeter: false
  property bool helperReporting: false
  property bool logStale: false
  property string helperLogPath: "" // the log the running helper follows
  property real level: 0
  property var history: []

  property bool destroying: false
  property bool restartPending: false
  property bool abandoning: false // the running helper is being taken down for misbehaving
  property int retryDelay: 1000
  property string pending: "" // helper output since the last newline

  readonly property var nodes: Pipewire.nodes ? Pipewire.nodes.values : []
  // A node's `properties` stay empty until something binds it, so every capture
  // stream is tracked; only then can application.process.binary be read.
  readonly property var captureStreams: {
    var list = []
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i]
      if (node && node.isStream && !node.isSink) list.push(node)
    }
    return list
  }
  readonly property bool pipewireListening: {
    for (var i = 0; i < captureStreams.length; i++) {
      if (isWisprCapture(captureStreams[i])) return true
    }
    return false
  }
  // Once PipeWire has shown Wispr capturing, its absence is meaningful: a log
  // stuck on listening after the stream vanished means the app died mid-dictation.
  property bool pipewireConfirmed: false

  readonly property bool helperAvailable: helperProcess.running && helperReporting
  readonly property bool meterAvailable: helperAvailable && helperMeter && meterEnabled
  // Merge rule:
  //   listening   - PipeWire shows Wispr's capture stream, OR the log says
  //                 listening and has not gone stale (stale = 3 s after a
  //                 previously confirmed stream disappeared).
  //   starting /
  //   processing  - from the log only, and only while the helper is reporting.
  //                 If the helper dies mid-transcription the widget collapses to
  //                 idle rather than holding a state nothing is updating.
  //   idle        - everything else.
  readonly property string state: {
    if (pipewireListening || (logState === "listening" && !logStale)) return "listening"
    if (helperAvailable && (logState === "starting" || logState === "processing")) return logState
    return "idle"
  }
  readonly property string statusText: ({
    idle: "Idle",
    starting: "Starting",
    listening: "Listening",
    processing: "Transcribing"
  })[state]
  readonly property string resolvedLogPath: logPath !== ""
    ? logPath.replace(/^~(?=\/|$)/, Quickshell.env("HOME"))
    : Quickshell.env("HOME") + "/.cache/wispr-flow/launcher.log"
  readonly property string degraded: {
    var reasons = []
    if (!helperAvailable)
      reasons.push("Status helper not running (needs /usr/bin/python3 and util-linux): listening only, no meter")
    else if (!logPresent)
      reasons.push("Wispr log not found at " + resolvedLogPath + ": no starting or transcribing state")
    if (helperAvailable && meterEnabled && !helperMeter)
      reasons.push("pw-record unavailable: no level meter")
    return reasons.join("\n")
  }

  readonly property string helperPath: decodeURIComponent(
    Qt.resolvedUrl("helper/wispr_flow_status.py").toString().replace(/^file:\/\//, ""))
  // Every executable by absolute path; nothing is resolved through PATH.
  readonly property var helperCommand: {
    var command = [
      "/usr/bin/setsid",                          // its own session and process group
      "/usr/bin/setpriv", "--pdeathsig", "TERM",  // dies with the shell
      "/usr/bin/python3", "-I", "-S",             // no PYTHON* variables, no site or user packages
      helperPath, "--log", resolvedLogPath
    ]
    if (!meterEnabled) command.push("--no-meter")
    return command
  }
  // The helper's whole environment, on top of clearEnvironment. A null entry
  // passes the shell's value through only when the shell has one; these are
  // what pw-record needs to find the PipeWire socket, and nothing else.
  readonly property var helperEnvironment: ({
    "XDG_RUNTIME_DIR": null,
    "PIPEWIRE_RUNTIME_DIR": null,
    "PIPEWIRE_REMOTE": null
  })

  function isWisprCapture(node) {
    if (!node.ready || !node.properties) return false
    var props = node.properties
    return props["media.class"] === "Stream/Input/Audio"
      && props["application.process.binary"] === processName
  }

  // Line assembly with a budget, in place of SplitParser's own unbounded
  // buffering: a helper that sends an overlong line, or keeps sending bytes
  // without a newline, is not our helper and is taken down.
  function acceptChunk(chunk) {
    if (abandoning) return
    var parts = (pending + chunk).split("\n")
    pending = parts.pop()
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].length > helperLineBudget) {
        abandonHelper("a line over the output budget")
        return
      }
      applyLine(parts[i])
    }
    if (pending.length > helperLineBudget) {
      abandonHelper("output over the line budget without a newline")
      return
    }
    if (parts.length > 0) watchdog.restart()
  }

  function applyLine(raw) {
    var data
    try {
      data = JSON.parse(String(raw || ""))
    } catch (error) {
      return
    }
    helperReporting = true
    retryDelay = 1000
    logPresent = data.log === true
    helperMeter = data.meter === true
    var next = String(data.state || "idle")
    if (next !== logState) logState = next
    if (typeof data.level === "number" && state === "listening") pushLevel(data.level)
  }

  function pushLevel(value) {
    level = Math.max(0, Math.min(1, value))
    var copy = history.slice()
    copy.push(level)
    while (copy.length > historySize) copy.shift()
    history = copy
  }

  function sendCaptureHint() {
    if (helperProcess.running) helperProcess.write(pipewireListening ? "capture 1\n" : "capture 0\n")
  }

  // Process.signal reaches the direct child only. kill(1) with the negated pid
  // reaches the process group setsid gave the helper, a pw-record it left
  // behind included. The pid check keeps a stale or null id from ever turning
  // into a signal to our own group.
  function signalHelperGroup(name) {
    var pid = Number(helperProcess.processId)
    if (!helperProcess.running || !(pid > 1)) return
    Quickshell.execDetached(["/usr/bin/kill", "-s", name, "--", "-" + pid])
  }

  function terminateHelper() {
    if (!helperProcess.running) return
    signalHelperGroup("TERM")
    killTimer.restart()
  }

  function abandonHelper(reason) {
    console.warn("wispr-flow: helper sent " + reason + "; terminating its process group")
    abandoning = true
    pending = ""
    terminateHelper()
  }

  function restartHelper() {
    if (destroying) return
    retryTimer.stop()
    if (helperProcess.running) {
      restartPending = true
      terminateHelper()
    } else {
      helperProcess.running = true
    }
  }

  function statusJson() {
    return JSON.stringify({
      state: state,
      statusText: statusText,
      level: level,
      history: history,
      logState: logState,
      pipewireListening: pipewireListening,
      helperAvailable: helperAvailable,
      meterAvailable: meterAvailable,
      logPresent: logPresent,
      degraded: degraded,
      logPath: resolvedLogPath,
      helperLogPath: helperLogPath,
      helperPid: Number(helperProcess.processId) || 0,
      processName: processName,
      meterEnabled: meterEnabled
    })
  }

  onStateChanged: {
    level = 0
    history = []
  }
  onPipewireListeningChanged: {
    if (pipewireListening) pipewireConfirmed = true
    sendCaptureHint()
    updateStale()
  }
  // A renamed build (or a typo in the setting) must not leave the stale rule
  // armed against a stream that can no longer match.
  onProcessNameChanged: pipewireConfirmed = false
  onLogStateChanged: {
    logStale = false
    updateStale()
  }
  // Settings arrive from the widget right after construction; the debounce
  // folds those into the first start instead of an immediate restart.
  onHelperCommandChanged: startTimer.restart()
  Component.onCompleted: startTimer.restart()

  // Hot reload and disable destroy this object; take the helper's group down
  // with it (pdeathsig only covers the whole shell exiting). The process
  // object's own destructor kills the leader if it has not gone by then.
  Component.onDestruction: {
    destroying = true
    signalHelperGroup("TERM")
  }

  function updateStale() {
    if (logState === "listening" && pipewireConfirmed && !pipewireListening) staleTimer.restart()
    else staleTimer.stop()
  }

  Timer {
    id: staleTimer
    interval: root.staleGraceMs
    onTriggered: root.logStale = true
  }

  Timer {
    id: startTimer
    interval: 150
    onTriggered: root.restartHelper()
  }

  Timer {
    id: retryTimer
    interval: root.retryDelay
    onTriggered: {
      root.retryDelay = Math.min(30000, root.retryDelay * 2)
      if (!helperProcess.running) helperProcess.running = true
    }
  }

  // Restarted by every complete line; the helper's heartbeat keeps it fed.
  Timer {
    id: watchdog
    interval: root.helperDeadlineMs
    onTriggered: root.abandonHelper("nothing for " + interval + " ms")
  }

  Timer {
    id: killTimer
    interval: root.killGraceMs
    onTriggered: root.signalHelperGroup("KILL")
  }

  Process {
    id: helperProcess
    command: root.helperCommand
    clearEnvironment: true
    environment: root.helperEnvironment
    stdinEnabled: true
    // No split marker: chunks arrive as read and acceptChunk() assembles the
    // lines under a budget, which the parser's newline mode cannot enforce.
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.acceptChunk(data) }
    }
    onStarted: {
      root.pending = ""
      root.abandoning = false
      root.helperLogPath = root.resolvedLogPath
      root.sendCaptureHint()
      watchdog.restart()
    }
    onRunningChanged: {
      if (running) return
      watchdog.stop()
      killTimer.stop()
      root.pending = ""
      root.abandoning = false
      root.helperReporting = false
      root.helperLogPath = ""
      root.logState = "idle"
      if (root.destroying) return
      if (root.restartPending) {
        root.restartPending = false
        running = true
      } else {
        retryTimer.restart()
      }
    }
  }

  PwObjectTracker { objects: root.captureStreams }

  IpcHandler {
    target: "io.github.mcurtis.wispr-flow"

    function status(): string {
      return root.statusJson()
    }
  }
}
