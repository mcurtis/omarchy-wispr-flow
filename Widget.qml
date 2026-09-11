import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Wispr Flow dictation indicator. Zero width while idle; slides in beside the
// built-in indicators when a dictation starts, showing a mic glyph and a
// scrolling level meter while listening and a pulsing hourglass while the
// transcription is processed. State comes from wispr-status.py (see there).
BarWidget {
  id: root
  moduleName: "mcurtis.wispr"

  property string dictationState: "idle"   // idle | starting | listening | processing
  property var history: []                  // most recent meter levels, newest last
  property bool restartPending: false

  readonly property bool shown: dictationState !== "idle"
  readonly property bool listening: dictationState === "listening"
  readonly property bool processing: dictationState === "processing"
  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color activeColor: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int barCount: Math.max(3, Math.min(12, parseInt(String(setting("bars", 7)), 10) || 7))
  readonly property string logPath: String(setting("logPath", "") || "")
  readonly property string scriptPath: Qt.resolvedUrl("wispr-status.py").toString().replace(/^file:\/\//, "")
  readonly property real meterHeight: Math.max(8, barSize - 12)
  readonly property string glyph: processing ? "󰔟" : "󰍬"
  readonly property string tooltip: listening ? "Wispr Flow: listening"
    : processing ? "Wispr Flow: transcribing"
    : dictationState === "starting" ? "Wispr Flow: starting" : "Wispr Flow"

  visible: true
  clip: true
  implicitWidth: shown ? content.implicitWidth + Style.space(12) : 0
  implicitHeight: barSize

  Behavior on implicitWidth {
    NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
  }

  function update(raw) {
    var text = String(raw || "").trim()
    if (text === "") return
    var data
    try {
      data = JSON.parse(text)
    } catch (error) {
      return
    }
    var next = String(data.state || "idle")
    if (next !== dictationState) {
      dictationState = next
      history = []
    }
    if (next === "listening" && typeof data.level === "number") {
      var copy = history.slice()
      copy.push(Math.max(0, Math.min(1, data.level)))
      while (copy.length > barCount) copy.shift()
      history = copy
    }
  }

  function restartStatus() {
    if (statusProcess.running) {
      restartPending = true
      statusProcess.signal(15)
    } else {
      statusProcess.running = true
    }
  }

  onLogPathChanged: restartStatus()

  // Plugin hot-reload destroys this item; take the status script down with it
  // (pdeathsig only covers the shell itself exiting).
  Component.onDestruction: {
    root.restartPending = false
    if (statusProcess.running) statusProcess.signal(15)
  }

  Process {
    id: statusProcess
    // Direct child of the shell so it dies with it instead of lingering.
    command: ["setpriv", "--pdeathsig", "TERM", "/usr/bin/python3", root.scriptPath]
    environment: root.logPath !== "" ? ({ WISPR_STATUS_LOG: root.logPath }) : ({})
    running: true
    stdout: SplitParser {
      onRead: function(data) { root.update(data) }
    }
    onExited: function(exitCode, exitStatus) {
      root.dictationState = "idle"
      root.history = []
      if (root.restartPending) {
        root.restartPending = false
        statusProcess.running = true
      } else {
        retryTimer.restart()
      }
    }
  }

  Timer {
    id: retryTimer
    interval: 3000
    onTriggered: if (!statusProcess.running) statusProcess.running = true
  }

  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.space(5)

    Text {
      id: glyphText
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.glyph
      color: root.listening ? root.activeColor : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.bar.iconFont

      Behavior on color { ColorAnimation { duration: 160 } }

      SequentialAnimation on opacity {
        running: root.shown && !root.listening
        loops: Animation.Infinite
        alwaysRunToEnd: true
        NumberAnimation { to: 0.35; duration: 500; easing.type: Easing.InOutSine }
        NumberAnimation { to: 1.0; duration: 500; easing.type: Easing.InOutSine }
      }
    }

    Row {
      id: meter
      anchors.verticalCenter: parent.verticalCenter
      spacing: 2
      visible: root.listening && !root.vertical
      height: root.meterHeight

      Repeater {
        model: root.barCount

        Rectangle {
          required property int index
          readonly property real level: root.history[index] !== undefined ? root.history[index] : 0
          anchors.verticalCenter: parent.verticalCenter
          width: 3
          radius: 1.5
          color: root.activeColor
          height: 2 + level * (root.meterHeight - 2)

          Behavior on height { NumberAnimation { duration: 70 } }
        }
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: if (root.bar) root.bar.run("wispr-flow")
    onEntered: if (root.bar) root.bar.showTooltip(root, root.tooltip)
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }
}
