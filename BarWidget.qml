import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Wispr Flow dictation indicator: a view over this plugin's service. Collapsed
// to nothing while idle; slides in beside the built-in indicators with a mic
// glyph and a scrolling level meter while listening, and a pulsing hourglass
// while the transcription is processed.
BarWidget {
  id: root

  // The host scopes serviceFor() to this plugin's own id, and the lookup is
  // reactive: the service can finish loading after the first bar is built.
  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null

  readonly property string dictationState: service ? service.state : "idle"
  readonly property bool shown: dictationState !== "idle"
  readonly property bool listening: dictationState === "listening"
  readonly property int barCount: Math.max(3, Math.min(12, parseInt(String(setting("bars", 7)), 10) || 7))
  readonly property bool meterWanted: setting("meter", true) !== false
  readonly property bool meterShown: listening && !vertical && meterWanted && !!service && service.meterAvailable
  readonly property var levels: {
    var recent = service ? service.history.slice(-barCount) : []
    while (recent.length < barCount) recent.unshift(0)
    return recent
  }

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color activeColor: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property real meterHeight: Math.max(Style.space(8), barSize - Style.space(12))
  readonly property real extent: (vertical ? content.implicitHeight : content.implicitWidth) + Style.space(12)
  readonly property string tooltip: {
    if (!service) return "Wispr Flow"
    var text = "Wispr Flow: " + service.statusText
    return service.degraded ? text + "\n" + service.degraded : text
  }

  // Latched while shown so the glyph does not flip back to the mic as the
  // widget slides out after transcribing.
  property string glyph: "󰍬"
  property real revealed: shown ? 1 : 0
  property bool hovered: false

  // The widget owns the settings UI, the service owns the work.
  Binding { target: root.service; property: "logPath"; value: String(root.setting("logPath", "") || ""); when: !!root.service }
  Binding { target: root.service; property: "processName"; value: String(root.setting("processName", "") || "wispr-flow"); when: !!root.service }
  Binding { target: root.service; property: "meterEnabled"; value: root.meterWanted; when: !!root.service }

  visible: revealed > 0
  clip: true
  opacity: revealed
  implicitWidth: vertical ? barSize : extent * revealed
  implicitHeight: vertical ? extent * revealed : barSize

  Behavior on revealed {
    NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
  }

  onDictationStateChanged: if (shown) glyph = dictationState === "processing" ? "󰔟" : "󰍬"
  onTooltipChanged: if (hovered && bar) bar.showTooltip(root, tooltip)

  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.space(5)

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.glyph
      color: root.listening ? root.activeColor : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.bar.iconFont

      Behavior on color {
        enabled: !root.bar || root.bar.foregroundAnimationEnabled
        ColorAnimation { duration: 160 }
      }

      SequentialAnimation on opacity {
        running: root.shown && !root.listening
        loops: Animation.Infinite
        alwaysRunToEnd: true
        NumberAnimation { to: 0.35; duration: 500; easing.type: Easing.InOutSine }
        NumberAnimation { to: 1.0; duration: 500; easing.type: Easing.InOutSine }
      }
    }

    Row {
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)
      visible: root.meterShown
      height: root.meterHeight

      Repeater {
        model: root.barCount

        Rectangle {
          required property int index
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(3)
          radius: width / 2
          color: root.activeColor
          height: Style.space(2) + root.levels[index] * (root.meterHeight - Style.space(2))

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
    onEntered: {
      root.hovered = true
      if (root.bar) root.bar.showTooltip(root, root.tooltip)
    }
    onExited: {
      root.hovered = false
      if (root.bar) root.bar.hideTooltip(root)
    }
  }
}
