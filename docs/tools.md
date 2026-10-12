# Tool reference

[Back to README](../README.md)

See [rigging tools](rigging.md) for joints, constraints, inverse kinematics, and skinning.
See [animation tools](animation.md) for timelines, keyframes, curve inspection, tangents, looping, and baking.
See [character animation](character-animation.md) for character sets, layers, poses, and facial target weights.

## Inspection, API, and execution tools

| Tool | What it does |
| --- | --- |
| `get_scene_info` | File, units, frame range, renderer, node counts, total faces, selection |
| `get_scene_graph` | Outliner-style hierarchy, optionally from a root, depth-limited |
| `list_objects` | List nodes by type (`transform`, `mesh`, `camera`, `shadingEngine`, `joint`, ...) and wildcard pattern |
| `get_selection` | Currently selected nodes |
| `get_object_info` | One node in detail: transform, bounds, mesh stats, materials, history, keyable attributes |
| `search_commands` | Search `maya.cmds` command names |
| `get_command_help` | Built-in help/flags for a `maya.cmds` command |
| `get_viewport_screenshot` | PNG of the current viewport (needs Maya with UI) |
| `execute_python` | Run Python in Maya. `cmds`, `mel`, `om` pre-imported, globals persist, last expression is returned, stdout captured, one undo chunk per call |
| `undo` | Undo the last N operations |
| `get_mesh_info` | API 2.0 topology counts, UV set counts and color sets |
| `get_mesh_vertices` | Indexed vertex positions in world/object space, paginated with `start` and `limit` |
| `get_closest_point` | Nearest surface point and face index for an XYZ point |
| `list_attributes` | Attribute types, lock and settable state; optional keyable filter |
| `get_connections` | Incoming/outgoing dependency graph connections with source and destination plugs |
| `get_api_help` | Installed OpenMaya class/member documentation and public member discovery |
| `set_transform` | Absolute translation, rotation and scale in world/object space in one undo chunk |

### API tools (0.2.0)

Mesh tools accept a mesh shape path or a transform with exactly one non-intermediate mesh shape.
Use a full DAG path when names are ambiguous. Vertex and connection limits must be 1–5000;
vertex pagination starts at zero. Coordinates use Maya's scene units. Transform rotation uses
Maya's current angular unit. An edit that fails may have partially applied; use `undo` to revert it.

Examples:

- `get_mesh_vertices(name="|pCube1", space="object", start=0, limit=8)`
- `get_closest_point(name="|pCube1", point=[0, 5, 0], space="world")`
- `get_connections(name="lambert1", direction="outgoing")`
- `get_api_help(class_name="MFnMesh", member="getClosestPoint")`
- `set_transform(name="|pCube1", translation=[0, 2, 0], space="world")`

Based on Autodesk's [Maya 2023 Python API 2.0 MFnMesh reference](https://help.autodesk.com/cloudhelp/2023/ENU/MAYA-API-REF/py_ref/class_open_maya_1_1_m_fn_mesh.html)
and [xform command reference](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/xform.html).
All API calls run on Maya's main thread. Geometry inspection uses API 2.0;
transform edits use `maya.cmds.xform` to preserve Maya undo support.

After updating, unload and reload the Maya plug-in (or restart Maya), then restart your MCP
client/server to discover the new tools. Read-only mode also hides `set_transform`.

## Scene building tools (0.3.0)

| Tool | Supported operations |
| --- | --- |
| `create_object` | Cube, sphere, cylinder, cone, plane, torus, camera, point/directional/spot/area lights |
| `get_attribute` / `set_attribute` | Read attribute values; edit numeric, boolean, string or float3/double3 values |
| `select_objects` | Replace, add or clear selection |
| `frame_objects` | Fit a specified or focused model-panel camera to nodes |
| `create_curve` | Open NURBS curves from XYZ control points, degree 1–3 |
| `create_material` | Lambert, Blinn or Phong shaders, shading-engine wiring and optional assignment |
| `organize_objects` | Group, parent or unparent transforms while preserving world placement |
| `mesh_operation` | Smooth, triangulate or combine polygon meshes with construction history |

Examples:

```python
create_object(kind="cube", name="prop", size=2, position=[0, 1, 0])
create_material(shader_type="blinn", name="redPaint", color=[0.8, 0.1, 0.1], names=["|prop"])
set_attribute(name="|prop", attribute="visibility", value=True)
create_curve(name="path", degree=1, points=[[0, 0, 0], [2, 1, 0], [4, 0, 0]])
organize_objects(names=["|prop", "|path"], action="group", name="props")
frame_objects(names=["|props"], panel="modelPanel4")
```

Node lists are limited to 200 explicit names; use full paths for ambiguous names.
`size` is a positive uniform transform scale for default primitives, not a dimension in scene units.
Curves accept up to 5000 control points. Material assignment replaces existing shading membership.
Mesh operations act on whole meshes, with smoothing limited to 1–3 divisions.
Grouping/parenting requires distinct transforms without ancestor/descendant pairs.
Editing and viewport framing use undo chunks; a failed operation can leave partial changes,
which can be reverted with `undo`. Only `get_attribute` from this set is exposed in read-only mode.

The bridge and dependencies remain standalone. Reload the Maya plug-in and restart the MCP
server/client after updating. Automated tests use Maya stubs and do not verify rendered results.

```bash
python tests/test_modeling.py
python tests/test_bridge.py
```

