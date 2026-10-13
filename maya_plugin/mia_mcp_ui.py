"""Shared menu, shelf entry point, and live bridge status panel for Maya."""
import time
import maya.cmds as cmds
import maya.mel as mel

_start = None
_get_server = None
_dialog = None
MENU = "miaMCPMenu"


def bind(start, get_server):
    global _start, _get_server
    _start, _get_server = start, get_server
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)
    main = mel.eval("$tmp = $gMainWindow")
    cmds.menu(MENU, label="mia-mcp", parent=main, tearOff=True)
    cmds.menuItem(label="Start Server", parent=MENU, command=lambda *_: start_server())
    cmds.menuItem(label="Server Log and Stats...", parent=MENU, command=lambda *_: show_status())
    cmds.menuItem(divider=True, parent=MENU)
    cmds.menuItem(label="Auto load at startup", parent=MENU,
                  checkBox=cmds.pluginInfo("maya_mcp_plugin", query=True, autoload=True),
                  command=lambda value: set_autoload(value))
    cmds.menuItem(label="Load / Auto load Preferences...", parent=MENU,
                  command=lambda *_: mel.eval("PluginManager"))


def set_autoload(value):
    cmds.pluginInfo("maya_mcp_plugin", edit=True, autoload=bool(value))
    cmds.pluginInfo(savePluginPrefs=True)


def start_server():
    if _start is None:
        cmds.loadPlugin("maya_mcp_plugin.py", quiet=True)
    if _start is None:
        raise RuntimeError("Reload maya_mcp_plugin.py to initialize the mia-mcp menu.")
    _start()
    show_status()


def show_status():
    global _dialog
    from PySide2 import QtCore, QtWidgets
    from shiboken2 import wrapInstance
    from maya import OpenMayaUI

    if _dialog is None:
        parent = wrapInstance(int(OpenMayaUI.MQtUtil.mainWindow()), QtWidgets.QWidget)
        _dialog = QtWidgets.QDialog(parent)
        _dialog.setWindowTitle("mia-mcp — Server Log and Stats")
        _dialog.resize(620, 420)
        layout = QtWidgets.QVBoxLayout(_dialog)
        summary = QtWidgets.QLabel()
        layout.addWidget(summary)
        button = QtWidgets.QPushButton("Start Server")
        button.clicked.connect(lambda: start_server())
        layout.addWidget(button)
        log = QtWidgets.QPlainTextEdit()
        log.setReadOnly(True)
        layout.addWidget(log)

        def refresh():
            server = _get_server() if _get_server else None
            if server is None:
                summary.setText("Server stopped")
                return
            with server.stats_lock:
                summary.setText("Status: {}  |  {}:{}\nUptime: {} s  |  Requests: {}  |  Errors: {}  |  Last request: {:.1f} ms".format(
                    "Running" if server.running else "Stopped", server.host, server.port,
                    int(time.time() - server.started_at) if server.running and server.started_at else 0,
                    server.requests, server.errors, server.last_ms))
                content = "\n".join(server.log)
            if log.toPlainText() != content:
                log.setPlainText(content)
                log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())
            button.setEnabled(not server.running)

        timer = QtCore.QTimer(_dialog)
        timer.timeout.connect(refresh)
        timer.start(1000)
        _dialog._timer = timer
        refresh()
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()


def cleanup():
    global _dialog, _start, _get_server
    if _dialog is not None:
        _dialog._timer.stop()
        _dialog.close()
        _dialog.deleteLater()
        _dialog = None
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU)
    _start = _get_server = None
