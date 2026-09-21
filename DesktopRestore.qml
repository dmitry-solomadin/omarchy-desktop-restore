import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.dmitry-solomadin.desktop-restore"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property string helper: decodeURIComponent(Qt.resolvedUrl("bin/desktop-restore").toString().replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property var checkpoint: ({ windows: [] })
  property string error: ""
  property string result: ""

  function refresh() { if (!reader.running) reader.running = true }
  function restore() {
    if (restorer.running) return
    error = ""
    result = ""
    restorer.running = true
  }
  function dateLabel(seconds) { return seconds ? new Date(seconds * 1000).toLocaleString() : "No checkpoint yet" }

  Component.onCompleted: refresh()
  onOpenedChanged: { if (opened) refresh() }
  Timer { interval: 5000; running: root.opened; repeat: true; onTriggered: root.refresh() }
  Process {
    id: reader
    command: [root.helper, "status", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.checkpoint = JSON.parse(text) }
        catch (e) { root.error = "Could not read the checkpoint. Check the CLI status." }
      }
    }
  }
  Process {
    id: restorer
    command: [root.helper, "restore"]
    stdout: StdioCollector { onStreamFinished: root.result = text.trim() }
    stderr: StdioCollector { onStreamFinished: { if (text.trim()) root.error = text.trim() } }
    onExited: function(code, status) {
      if (code !== 0 && root.error === "") root.error = "Some windows could not be restored. See the result below."
      root.refresh()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰑓"
    tooltipText: "Desktop Restore · " + root.checkpoint.windows.length + " saved windows"
    onPressed: function(mouseButton) { if (mouseButton === Qt.LeftButton) root.toggle() }
  }

  KeyboardPanel {
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.opened
    focusTarget: content
    contentWidth: fittedContentWidth(Style.space(440))
    contentHeight: cappedContentHeight(Style.space(480))
    ColumnLayout {
      id: content
      anchors.fill: parent
      spacing: Style.space(10)
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        else if (event.key === Qt.Key_R && !event.isAutoRepeat) { root.restore(); event.accepted = true }
      }
      Text {
        text: "Desktop Restore"
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        color: root.foreground
      }
      Text {
        Layout.fillWidth: true
        text: root.checkpoint.installed
          ? "Restore checkpoint: " + root.dateLabel(root.checkpoint.saved)
          : "Finish setup in a terminal:\n" + root.helper + " install"
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        color: root.foreground
      }
      Rectangle {
        Layout.fillWidth: true
        implicitHeight: Style.space(38)
        color: Qt.alpha(root.foreground, restoreArea.containsMouse ? 0.15 : 0.07)
        border.color: Qt.alpha(root.foreground, 0.2)
        opacity: restoreArea.enabled ? 1 : 0.5
        Text {
          anchors.centerIn: parent
          text: restorer.running ? "Restoring…" : "Restore missing windows"
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          color: root.foreground
        }
        MouseArea {
          id: restoreArea
          anchors.fill: parent
          hoverEnabled: true
          enabled: !restorer.running && root.checkpoint.windows.length > 0
          onClicked: root.restore()
        }
      }
      ListView {
        id: list
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        spacing: Style.space(8)
        model: root.checkpoint.windows
        ScrollBar.vertical: ScrollBar {}
        delegate: Text {
          required property var modelData
          width: list.width
          text: "Workspace " + modelData.workspace + " · " + modelData.kind + "\n" + modelData.title
            + (modelData.error ? "\n" + modelData.error : "")
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          color: root.foreground
        }
      }
      Text {
        Layout.fillWidth: true
        Layout.maximumHeight: Style.space(100)
        clip: true
        text: root.error || root.result || "Super+Shift+R to restore · shutdown saves silently"
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        font.family: root.fontFamily
        font.pixelSize: Style.font.body * 0.85
        color: Qt.alpha(root.foreground, 0.7)
      }
    }
  }
}
