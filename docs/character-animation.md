# Character animation (0.6.0)

[Tool reference](tools.md) · [Animation tools](animation.md) · [README](../README.md)

| Tool | Parameters and behavior |
| --- | --- |
| `create_character_set` | New set `character`, control node `name`, 1–60 explicit `attributes` |
| `get_character_set` | Existing `character`, member `limit=200` (1–5000) |
| `create_animation_layer` | New `layer`, control node `name`, explicit `attributes`, `override=False` (additive) |
| `get_animation_layer` | Existing `layer`: weight, mute, solo, lock, mode, hierarchy, channels and curves |
| `edit_animation_layer` | Existing `layer`; optional `weight` (0–1), `mute`, `solo`, `lock` |
| `capture_pose` | `names` (1–200), optional common `attributes`; returns scalar values and scene units |
| `apply_pose` | `pose`: JSON object returned by capture; validates all channels before editing |
| `get_blend_shape_info` | Existing `blend_shape`, target `limit=200` (1–5000); sparse indices, aliases and weights |
| `set_blend_shape_weights` | Existing `blend_shape`, `weights` mapping index strings to values between 0 and 1 |
| `set_keyframes` (extended) | Optional `layer` to key an unlocked layer containing every requested channel |

## Control channels and layers

These are MCP tool calls; controls must already exist. `name` identifies the control,
while `character` and `layer` identify the new set/layer. Additional membership can be
managed in Maya; these creation tools accept one control per call.

```python
create_character_set(character="heroCharacter", name="|hand_CTRL",
                     attributes=["translateX", "translateY", "translateZ"])
get_character_set(character="heroCharacter")
create_animation_layer(layer="handPolish", name="|hand_CTRL",
                       attributes=["translateX", "translateY", "translateZ"], override=False)
get_animation_layer(layer="handPolish")
set_keyframes(name="|hand_CTRL", attributes=["translateY"], times=[24], value=2,
              layer="handPolish")
edit_animation_layer(layer="handPolish", weight=0.5, mute=False)
```

Character sets collect animation channels; these are Maya sets, not HumanIK definitions
or retargeting. Existing set/layer names are rejected. Explicit layer keying validates
membership because Maya otherwise ignores the requested layer for nonmember channels.
Use long attribute names when possible. Maya resolves explicit key values across layer
blending; the supplied value is not necessarily the raw layer-curve value.
Layer controls affect evaluation and do not create keys on layer weights.

## Poses

```python
capture_pose(names=["|hand_CTRL", "|elbow_CTRL"], attributes=["translateX", "translateY", "translateZ"])
# Pass the returned JSON object directly to apply_pose(pose=...).
```

Capture defaults to keyable scalar numeric channels, including custom scalar controls.
It supports at most 60 attributes per node and 2000 channels total. Values are local
channels rather than world-space transforms. Save the returned JSON yourself to retain
it between sessions; no internal pose library is maintained.

Application requires matching distance/angular/time units, existing node paths, matching
attribute types and settable channels. Locked, connected, incompatible or duplicate
channels are rejected before editing. It does not create keys; animated/layered channels
may be rejected, so use `set_keyframes` with captured values for those controls.
Poses do not map between differently named rigs.

## Facial targets

```python
get_blend_shape_info(blend_shape="face_BS")
set_blend_shape_weights(blend_shape="face_BS", weights={"0": 0.5, "7": 1.0})
set_keyframes(name="face_BS", attributes=["weight[7]"], times=[24], value=1)
```

Use indices returned by inspection; they can have gaps. Edits allow 1–200 existing
indices, reject locked/connected weights, and restrict values to 0–1. They do not create
targets, in-betweens or keys. Aliases identify targets, while edits require index strings.
Animate rig-driven facial weights through their controls.

All edits run on Maya's main thread in one undo chunk. Runtime failures can leave partial
changes; use `undo` to revert. Read-only mode retains layer/set inspection, pose capture,
and blend-shape inspection. Reload the plug-in and restart the MCP server/client after updating.

## References and tests

Based on Autodesk's [animLayer](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/animLayer.html),
[setKeyframe](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/setKeyframe.html),
[blendShape](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/blendShape.html),
and [character set](https://help.autodesk.com/cloudhelp/2022/ENU/Maya-Tech-Docs/CommandsPython/character.html) references.
The 2023 character page was unavailable; its command contract was checked against Autodesk's 2022 documentation.

```bash
python tests/test_character_animation.py
python tests/test_animation.py
python tests/test_bridge.py
```

Stub tests cover command arguments, layer membership/locks, pose validation, sparse targets,
undo cleanup, tool discovery and read-only mode. Live rig evaluation still needs Maya verification.
