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
- Use create_object/create_curve/create_material for scene building; organize_objects for hierarchy.
- Prefer set_attribute/set_transform and mesh_operation for supported edits; each is undoable.
- Use select_objects to manage selection and frame_objects to fit a viewport camera.
- Use get_joint_info/get_skin_info/get_skin_weights to inspect rigs; create_joint_chain,
  create_constraint, create_ik_handle and bind_skin for undoable rig construction.
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


@mcp.tool()
def get_mesh_info(name: str) -> str:
    """Mesh topology counts, UV sets and color sets via OpenMaya API 2.0. Accepts a mesh shape or single-mesh transform."""
    return _fmt(send_command("get_mesh_info", {"name": name}))


@mcp.tool()
def get_mesh_vertices(name: str, space: str = "world", start: int = 0, limit: int = 200) -> str:
    """Read indexed mesh vertex positions. space is world/object; paginate with start and limit (1–5000)."""
    return _fmt(send_command("get_mesh_vertices", {"name": name, "space": space, "start": start, "limit": limit}))


@mcp.tool()
def get_closest_point(name: str, point: list[float], space: str = "world") -> str:
    """Find the nearest mesh surface point and face index to [x,y,z] in world/object space."""
    return _fmt(send_command("get_closest_point", {"name": name, "point": point, "space": space}))


@mcp.tool()
def list_attributes(name: str, keyable_only: bool = False, limit: int = 200) -> str:
    """Inspect attribute types, lock and settable state. limit is 1–5000."""
    return _fmt(send_command("list_attributes", {"name": name, "keyable_only": keyable_only, "limit": limit}))


@mcp.tool()
def get_connections(name: str, direction: str = "both", limit: int = 200) -> str:
    """Inspect dependency graph plug connections. direction is incoming/outgoing/both; limit per direction is 1–5000."""
    return _fmt(send_command("get_connections", {"name": name, "direction": direction, "limit": limit}))


@mcp.tool()
def get_api_help(class_name: str, member: str = "") -> str:
    """Inspect installed maya.api.OpenMaya documentation and public members, e.g. MFnMesh/getClosestPoint."""
    return _fmt(send_command("get_api_help", {"class_name": class_name, "member": member}))


@mcp.tool()
def get_attribute(name: str, attribute: str) -> str:
    """Read one attribute's value and Maya type; use an explicit node name."""
    return _fmt(send_command("get_attribute", {"name": name, "attribute": attribute}))


@mcp.tool()
def get_joint_info(name: str) -> str:
    """Inspect joint hierarchy, world position, orientation, rotation order and radius."""
    return _fmt(send_command("get_joint_info", {"name": name}))


@mcp.tool()
def get_skin_info(skin_cluster: str) -> str:
    """Inspect skin geometry, influence full paths/logical indices, skinning and normalization modes."""
    return _fmt(send_command("get_skin_info", {"skin_cluster": skin_cluster}))


@mcp.tool()
def get_skin_weights(skin_cluster: str, start: int = 0, limit: int = 20) -> str:
    """Read polygon vertex weights via API 2.0 in returned influence order. limit 1–200; at most 20000 weight values."""
    return _fmt(send_command("get_skin_weights", {"skin_cluster": skin_cluster, "start": start, "limit": limit}))


if not READ_ONLY:

    @mcp.tool()
    def create_joint_chain(positions: list[list[float]], names: list[str] = [], parent: str = "", radius: float = 1) -> str:
        """Create 1–200 joints at world XYZ positions, optionally under a joint. Restores selection; orientation remains at defaults."""
        return _fmt(send_command("create_joint_chain", {"positions": positions, "names": names, "parent": parent, "radius": radius}))

    @mcp.tool()
    def create_constraint(targets: list[str], driven: str, kind: str = "parent", maintain_offset: bool = True, name: str = "") -> str:
        """Create parent/point/orient/scale/pole_vector constraints. Pole vector requires IK handle and ignores maintain_offset."""
        return _fmt(send_command("create_constraint", {"targets": targets, "driven": driven, "kind": kind, "maintain_offset": maintain_offset, "name": name}))

    @mcp.tool()
    def create_ik_handle(start_joint: str, end_joint: str, solver: str = "ikRPsolver", name: str = "") -> str:
        """Create RP or SC inverse kinematics. End joint must descend from start joint."""
        return _fmt(send_command("create_ik_handle", {"start_joint": start_joint, "end_joint": end_joint, "solver": solver, "name": name}))

    @mcp.tool()
    def bind_skin(mesh: str, joints: list[str], max_influences: int = 4, skinning_method: int = 0, name: str = "") -> str:
        """Bind an unskinned polygon mesh to explicit joints. Method 0 linear/1 dual quaternion/2 blended; max influences 1–20."""
        return _fmt(send_command("bind_skin", {"mesh": mesh, "joints": joints, "max_influences": max_influences, "skinning_method": skinning_method, "name": name}))

    @mcp.tool()
    def create_object(kind: str = "cube", name: str = "", size: float = 1,
                      position: list[float] = [0, 0, 0]) -> str:
        """Create cube/sphere/cylinder/cone/plane/torus/camera or point_light/directional_light/spot_light/area_light. size scales primitives only."""
        return _fmt(send_command("create_object", {"kind": kind, "name": name, "size": size, "position": position}))

    @mcp.tool()
    def set_attribute(name: str, attribute: str, value: str | float | bool | list[float]) -> str:
        """Set a string, numeric, boolean or three-component attribute in one undo chunk. Locked/connected attributes are rejected."""
        return _fmt(send_command("set_attribute", {"name": name, "attribute": attribute, "value": value}))

    @mcp.tool()
    def select_objects(names: list[str] = [], mode: str = "replace") -> str:
        """Replace/add explicit node selection, or clear it with mode=clear. Up to 200 nodes."""
        return _fmt(send_command("select_objects", {"names": names, "mode": mode}))

    @mcp.tool()
    def frame_objects(names: list[str], panel: str = "") -> str:
        """Fit a model-panel camera to explicit nodes without changing selection. Defaults to focused panel; requires Maya UI."""
        return _fmt(send_command("frame_objects", {"names": names, "panel": panel}))

    @mcp.tool()
    def create_curve(points: list[list[float]], degree: int = 1, name: str = "") -> str:
        """Create an open NURBS curve from degree+1 to 5000 XYZ control points. degree is 1, 2 or 3."""
        return _fmt(send_command("create_curve", {"points": points, "degree": degree, "name": name}))

    @mcp.tool()
    def create_material(shader_type: str = "lambert", name: str = "",
                        color: list[float] = [0.5, 0.5, 0.5], names: list[str] = []) -> str:
        """Create lambert/blinn/phong shader and shading engine; optionally assign to nodes. RGB channels are 0–1; assignment replaces existing materials."""
        return _fmt(send_command("create_material", {"shader_type": shader_type, "name": name, "color": color, "names": names}))

    @mcp.tool()
    def organize_objects(names: list[str], action: str = "group", name: str = "", parent: str = "") -> str:
        """Group, parent or unparent transforms while preserving world placement. parent is required for action=parent."""
        return _fmt(send_command("organize_objects", {"names": names, "action": action, "name": name, "parent": parent}))

    @mcp.tool()
    def mesh_operation(names: list[str], operation: str, divisions: int = 1) -> str:
        """Smooth (1–3 divisions), triangulate or combine meshes with construction history in one undo chunk."""
        return _fmt(send_command("mesh_operation", {"names": names, "operation": operation, "divisions": divisions}))

    @mcp.tool()
    def set_transform(name: str, translation: list[float] | None = None,
                      rotation: list[float] | None = None, scale: list[float] | None = None,
                      space: str = "world") -> str:
        """Set absolute transform vectors in world/object space in one undo chunk. Rotation uses Maya's current angular unit."""
        return _fmt(send_command("set_transform", {"name": name, "translation": translation,
                    "rotation": rotation, "scale": scale, "space": space}))

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
