"""FastMCP server exposing Autodesk Maya to MCP clients (Claude, Codex, ...)."""

import base64
import json
import os

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image

from .connection import send_command

READ_ONLY = os.environ.get("MAYA_MCP_READONLY", "").lower() in ("1", "true", "yes")

INSTRUCTIONS = """\
Tools for inspecting and editing a live Autodesk Maya session.

Workflow tips:
- Start with get_scene_info, then get_scene_graph / list_objects to explore.
- Use get_object_info for details about one node (transform, mesh stats, materials, history).
- Prefer maya.cmds in execute_python. `cmds`, `mel` and `om` (maya.api.OpenMaya) are pre-imported
  and variables persist between calls. The last expression's value is returned, and print() output
  is captured.
- If unsure about a command's flags, call search_commands / get_command_help instead of guessing.
- Each execute_python call is a single undo chunk; call undo to revert a bad change.
- Use get_viewport_screenshot to check visual results.
"""

mcp = _Server("maya", instructions=INSTRUCTIONS)


def _fmt(result):
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_scene_info() -> str:
    """Summary of the open Maya scene: file, units, frame range, renderer, node counts, selection."""
    return _fmt(send_command("get_scene_info"))


@mcp.tool()
def get_scene_graph(root: str = "", max_depth: int = 3, limit: int = 500) -> str:
    """Outliner-style hierarchy of the scene (or of `root`), to `max_depth` levels."""
    return _fmt(send_command("get_scene_graph", {"root": root or None, "max_depth": max_depth, "limit": limit}))


@mcp.tool()
def list_objects(node_type: str = "transform", pattern: str = "*", limit: int = 200) -> str:
    """List nodes by Maya node type (e.g. transform, mesh, camera, shadingEngine, joint) and name pattern (supports * wildcards)."""
    return _fmt(send_command("list_objects", {"node_type": node_type, "pattern": pattern, "limit": limit}))


@mcp.tool()
def get_selection() -> str:
    """Return the currently selected nodes."""
    return _fmt(send_command("get_selection"))


@mcp.tool()
def get_object_info(name: str) -> str:
    """Detailed info for one node: transform values, hierarchy, mesh stats, materials, history, keyable attributes."""
    return _fmt(send_command("get_object_info", {"name": name}))


@mcp.tool()
def search_commands(query: str, limit: int = 50) -> str:
    """Search maya.cmds command names by substring (e.g. 'poly', 'constraint')."""
    return _fmt(send_command("search_commands", {"query": query, "limit": limit}))


@mcp.tool()
def get_command_help(command: str) -> str:
    """Return Maya's built-in help (flags, types) for a maya.cmds command, e.g. 'polyExtrudeFacet'."""
    return _fmt(send_command("get_command_help", {"command": command}))


@mcp.tool()
def get_viewport_screenshot(width: int = 960, height: int = 540) -> Image:
    """Capture the current Maya viewport as a PNG image (requires Maya running with its UI)."""
    result = send_command("get_viewport_screenshot", {"width": width, "height": height})
    return Image(data=base64.b64decode(result["data_base64"]), format="png")


if not READ_ONLY:

    @mcp.tool()
    def execute_python(code: str) -> str:
        """Run Python inside Maya's main thread and return captured stdout plus the value of the last expression.

        `cmds`, `mel` and `om` (maya.api.OpenMaya) are pre-imported; globals persist between calls.
        The whole call is one undo chunk. WARNING: runs with no sandbox; it can modify or delete anything
        your user account can.
        """
        return _fmt(send_command("execute_python", {"code": code}))

    @mcp.tool()
    def undo(steps: int = 1) -> str:
        """Undo the last `steps` operations in Maya (each execute_python call counts as one)."""
        return _fmt(send_command("undo", {"steps": steps}))


def main():
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
