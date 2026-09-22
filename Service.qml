import QtQuick
import Quickshell.Io

// No UI. Enabling installs or refreshes integration once per code revision.
// Ordinary shell reloads only start services that are not already running.
Item {
  id: root
  property var shell: null
  readonly property string setup: decodeURIComponent(Qt.resolvedUrl("lib/setup.py").toString().replace(/^file:\/\//, ""))

  Process {
    command: ["python3", root.setup, "start"]
    running: true
    stderr: StdioCollector {
      onStreamFinished: if (text.trim()) console.warn("Desktop Restore: " + text.trim())
    }
  }
}
