import QtQuick
import Quickshell.Io

// No UI. Enabling the plugin starts an already-configured integration; setup
// remains explicit. systemd owns the processes across shell reloads and exit.
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
