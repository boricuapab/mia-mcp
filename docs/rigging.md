# Rigging tools (0.4.0)

[Tool reference](tools.md) · [README](../README.md)

| Tool | Parameters and behavior |
| --- | --- |
| `get_joint_info` | `name`: joint hierarchy, world position, joint orientation, rotation order and radius |
| `create_joint_chain` | `positions`: 1–200 world XYZ positions; optional `names`, existing joint `parent`, positive `radius` |
| `create_constraint` | `targets`, `driven`; `kind`: parent, point, orient, scale or pole_vector; `maintain_offset=True`; optional `name` |
| `create_ik_handle` | `start_joint`, descendant `end_joint`; `solver`: ikRPsolver (default) or ikSCsolver; optional `name` |
| `bind_skin` | `mesh`, explicit `joints`; `max_influences=4` (1–20), `skinning_method=0`, optional `name` |
| `get_skin_info` | `skin_cluster`: geometry, influence paths and logical indices, skinning and normalization modes |
| `get_skin_weights` | `skin_cluster`, zero-based `start=0`, `limit=20` (1–200); polygon vertex weights in returned influence order |

## Build and inspect a rig

These examples are MCP tool arguments. Use the actual names returned by creation tools:
Maya can rename nodes when requested names already exist.

```python
create_joint_chain(
    positions=[[0, 0, 0], [0, 5, 1], [0, 10, 0]],
    names=["shoulder_JNT", "elbow_JNT", "wrist_JNT"], radius=0.5)
get_joint_info(name="|shoulder_JNT|elbow_JNT")
create_ik_handle(start_joint="|shoulder_JNT",
                 end_joint="|shoulder_JNT|elbow_JNT|wrist_JNT", name="arm_IK")
create_constraint(targets=["|hand_CTRL"], driven="arm_IK", kind="point")
create_constraint(targets=["|elbow_CTRL"], driven="arm_IK", kind="pole_vector")
bind_skin(mesh="|body_GEO", joints=["|shoulder_JNT",
    "|shoulder_JNT|elbow_JNT", "|shoulder_JNT|elbow_JNT|wrist_JNT"], name="body_SKIN")
get_skin_info(skin_cluster="body_SKIN")
get_skin_weights(skin_cluster="body_SKIN", start=0, limit=20)
```

Controls and geometry must already exist. Joint chains use world positions and default
joint orientations; this tool does not automatically orient or mirror a skeleton.
Selection is restored after chain creation. Constraints accept at most 200 transform targets;
pole-vector constraints require an IK handle and ignore `maintain_offset`.
IK tools support existing joint hierarchies and RP/SC solvers, without spline IK.

Skin binding supports one unskinned polygon mesh with distinct explicit joints. It rejects
geometry with a skinCluster in its history. Methods are 0 (linear), 1 (dual quaternion),
and 2 (blended); weights are normalized interactively and the influence cap is enforced.
Weight inspection supports a connected polygon output and caps responses at 20000 weights.
Each vertex's weights follow the `influences` array order, not the sparse logical indices
reported by `get_skin_info`. This release reads weights; it does not edit them.

All edits run on Maya's main thread in one undo chunk per call. Failed edits can leave
partial changes; use `undo` to revert them. `MAYA_MCP_READONLY=1` exposes only the three
inspection tools. Reload the plug-in and restart the MCP server/client after updating.

## Implementation references and validation

Skin inspection uses Autodesk's [MFnSkinCluster API 2.0](https://help.autodesk.com/cloudhelp/2023/ENU/MAYA-API-REF/py_ref/class_open_maya_anim_1_1_m_fn_skin_cluster.html)
and [MFnGeometryFilter](https://help.autodesk.com/cloudhelp/2023/ENU/MAYA-API-REF/py_ref/class_open_maya_anim_1_1_m_fn_geometry_filter.html).
Edits use undoable commands: [skinCluster](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/skinCluster.html),
[ikHandle](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/ikHandle.html),
and [parentConstraint](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/parentConstraint.html).

```bash
python tests/test_rigging.py
python tests/test_bridge.py
```

Tests use Maya stubs to check command arguments, pagination, validation, undo cleanup,
tool registration and read-only behavior. Live deformation and IK results require Maya verification.
