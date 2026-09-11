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

  property string logState: "idle"
  property bool logPresent: false
  property bool helperMeter: false
  property bool helperReporting: false
  property bool logStale: false
  property real level: 0
  property var history: []

  property bool destroying: false
  property bool restartPending: false
  property int retryDelay: 1000

  readonly property var nodes: Pipewire.nodes ? Pipewire.nodes.values : []
  readonly property bool pipewireListening: {
    for (var i = 0; i < nodes.length; i++) {
      if (isWisprCapture(nodes[i])) return true
    }
    return false
  }
  // Once PipeWire has shown Wispr capturing, its absence is meaningful: a log
  // stuck on listening after the stream vanished means the app died mid-dictation.
  property bool pipewireConfirmed: false

  readonly property bool helperAvailable: helperProcess.running && helperReporting
  readonly property bool meterAvailable: helperAvailable && helperMeter && meterEnabled
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
      reasons.push("Status helper not running (needs python3 and setpriv): listening only, no meter")
    else if (!logPresent)
      reasons.push("Wispr log not found at " + resolvedLogPath + ": no starting or transcribing state")
    if (helperAvailable && meterEnabled && !helperMeter)
      reasons.push("pw-record unavailable: no level meter")
    return reasons.join("\n")
  }

  readonly property string helperPath: decodeURIComponent(
    Qt.resolvedUrl("helper/wispr_flow_status.py").toString().replace(/^file:\/\//, ""))
  readonly property var helperCommand: {
    var command = ["setpriv", "--pdeathsig", "TERM", "python3", helperPath, "--log", resolvedLogPath]
    if (!meterEnabled) command.push("--no-meter")
    return command
  }

  function isWisprCapture(node) {
    if (!node || !node.isStream || node.isSink || !node.ready || !node.properties) return false
    var props = node.properties
    return props["media.class"] === "Stream/Input/Audio"
      && props["application.process.binary"] === processName
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

  function restartHelper() {
    if (destroying) return
    retryTimer.stop()
    if (helperProcess.running) {
      restartPending = true
      helperProcess.signal(15)
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
  onLogStateChanged: {
    logStale = false
    updateStale()
  }
  // Settings arrive from the widget right after construction; the debounce
  // folds those into the first start instead of an immediate restart.
  onHelperCommandChanged: startTimer.restart()
  Component.onCompleted: startTimer.restart()

  // Hot reload and disable destroy this object; take the helper down with it
  // (pdeathsig only covers the whole shell exiting).
  Component.onDestruction: {
    destroying = true
    if (helperProcess.running) helperProcess.signal(15)
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

  Process {
    id: helperProcess
    command: root.helperCommand
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(data) { root.applyLine(data) }
    }
    onStarted: root.sendCaptureHint()
    onRunningChanged: {
      if (running) return
      root.helperReporting = false
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

  IpcHandler {
    target: "io.github.mcurtis.wispr-flow"

    function status(): string {
      return root.statusJson()
    }
  }
}
