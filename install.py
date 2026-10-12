"""Drag this file onto Maya's viewport to install the bridge and miamcp shelf."""

from pathlib import Path
import shutil


START_COMMAND = '''import maya.cmds as cmds
if "maya_mcp_plugin" not in (cmds.pluginInfo(query=True, listPlugins=True) or []):
    cmds.loadPlugin("maya_mcp_plugin.py")
else:
    print("[maya-mcp] Bridge plug-in is already loaded.")
'''


def install():
    import maya.cmds as cmds
    import maya.mel as mel

    root = Path(__file__).resolve().parent
    user_dir = Path(cmds.internalVar(userAppDir=True)) / cmds.about(version=True)
    plugin_dir = user_dir / "plug-ins"
    icon_dir = Path(cmds.internalVar(userPrefDir=True)) / "icons"
    shelf_dir = Path(cmds.internalVar(userShelfDir=True))
    for folder in (plugin_dir, icon_dir, shelf_dir):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "maya_plugin" / "maya_mcp_plugin.py", plugin_dir / "maya_mcp_plugin.py")
    icon = icon_dir / "mia_mcp_start_server_icon.png"
    shutil.copy2(root / "icons" / icon.name, icon)

    top = mel.eval("$tmp = $gShelfTopLevel")
    shelf = top + "|miamcp"
    if not cmds.shelfLayout(shelf, exists=True):
        shelf = cmds.shelfLayout("miamcp", parent=top)
    buttons = cmds.shelfLayout(shelf, query=True, childArray=True) or []
    button = next((b for b in buttons
                   if cmds.objectTypeUI(b) == "shelfButton"
                   and "maya_mcp_plugin" in (cmds.shelfButton(b, query=True, command=True) or "")), None)
    options = dict(label="Start Maya MCP", annotation="Start the Maya MCP bridge on localhost",
                   image=str(icon).replace("\\", "/"), image1=str(icon).replace("\\", "/"),
                   imageOverlayLabel="", sourceType="python", command=START_COMMAND,
                   style="iconOnly", width=35, height=34)
    if button:
        cmds.shelfButton(button, edit=True, **options)
    else:
        cmds.shelfButton(parent=shelf, **options)
    cmds.saveShelf(shelf, str(shelf_dir / "shelf_miamcp").replace("\\", "/"))
    cmds.tabLayout(top, edit=True, selectTab=shelf)
    print("[maya-mcp] Installed plug-in, icon, and miamcp shelf. Click Start Maya MCP to connect.")
    return shelf


def onMayaDroppedPythonFile(*args):
    install()


if __name__ == "__main__":
    install()
