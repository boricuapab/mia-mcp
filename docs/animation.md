# Animation tools (0.5.0)

[Tool reference](tools.md) · [README](../README.md)

| Tool | Parameters and behavior |
| --- | --- |
| `get_timeline` | Current frame, scene time unit, playback and animation ranges |
| `set_timeline` | Optional `current_time`; both `start` and `end` to change playback range |
| `set_keyframes` | `name`, explicit `attributes`, `times`; optional single-channel `value`, `breakdown=False` |
| `get_animation_curve` | Explicit animCurve `curve`, key-index `start=0`, `limit=200` (1–5000) |
| `delete_keyframes` | `name`, explicit `attributes`, inclusive `start`/`end` frame range; leaves key clipboard unchanged |
| `set_key_tangents` | `name`, `attributes`, frame `start`/`end`, `in_tangent="auto"`, `out_tangent="auto"` |
| `set_curve_infinity` | `name`, `attributes`, `pre="constant"`, `post="constant"` |
| `bake_animation` | `name`, `attributes`, frame `start`/`end`, `sample_by=1`, `simulation=True` |

## Examples

These are MCP tool calls. The named control must already exist:

```python
get_timeline()
set_timeline(current_time=1, start=1, end=48)
set_keyframes(name="|hand_CTRL", attributes=["translateY"], times=[1], value=0)
set_keyframes(name="|hand_CTRL", attributes=["translateY"], times=[24], value=5)
set_keyframes(name="|hand_CTRL", attributes=["translateY"], times=[48], value=0)
set_key_tangents(name="|hand_CTRL", attributes=["translateY"], start=1, end=48,
                 in_tangent="auto", out_tangent="auto")
set_curve_infinity(name="|hand_CTRL", attributes=["translateY"], post="cycle")
get_connections(name="|hand_CTRL", direction="incoming")
# Use the actual animCurve node found in the connections:
get_animation_curve(curve="hand_CTRL_translateY", start=0, limit=20)
bake_animation(name="|hand_CTRL", attributes=["translateX", "translateY", "translateZ"],
               start=1, end=48, sample_by=1)
delete_keyframes(name="|hand_CTRL", attributes=["translateY"], start=24, end=24)
```

## Units, scope, and limits

Frame inputs use Maya's current scene time unit and may be fractional. Timeline edits
change playback bounds, leaving the animation range and frame rate unchanged.
Current-frame changes follow Maya's own undo behavior and are not guaranteed to be undone.

Animation edits require 1–60 distinct explicit attributes on one node. Key creation
accepts 1–200 times and scalar numeric channels. Locked channels are rejected. An explicit
value is allowed for one channel only; without it, Maya keys the current channel value
at each requested time. Existing keys at those times can be replaced. Attribute values
use Maya command units, such as the scene distance and angular units.

Tangents support auto, linear, flat, spline, clamped and plateau; outgoing tangents also
support step and stepnext. Infinity modes are constant, linear, cycle, cycleRelative
and oscillate. These edit existing animated channels; they do not create keys by themselves.
Range edits target objects explicitly rather than selected keys. Layered range edits follow
Maya's command behavior. `set_keyframes` now accepts an optional `layer` parameter;
see [character animation](character-animation.md) for membership checks and layer controls.

Curve inspection uses API 2.0. Time inputs are converted to scene frame units; unitless
inputs remain numbers. Output values use API internal units (radians for angles,
centimeters for distances). Time-output TT/UT curves are rejected because `MFnAnimCurve.value`
does not support them. Tangent, curve-type and infinity fields are Autodesk enum integers.

Baking replaces animation within the requested range, retains implicit controls and
requests preservation of outside keys. Maya only guarantees outside-key preservation
for supported curve/pairBlend setups; other upstream networks may behave differently.
Bakes are capped at 20000 channel samples and restore the original frame even on failure.
All edits use one undo chunk per call; errors can leave partial changes, which can be
reverted with `undo` where Maya supports it.

Read-only mode exposes `get_timeline` and `get_animation_curve` only. Reload the Maya
plug-in and restart the MCP server/client after updating.

## References and testing

Based on Autodesk's [MFnAnimCurve Python API 2.0 reference](https://help.autodesk.com/cloudhelp/2023/ENU/MAYA-API-REF/py_ref/class_open_maya_anim_1_1_m_fn_anim_curve.html),
[setKeyframe](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/setKeyframe.html),
[keyTangent](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/keyTangent.html),
[setInfinity](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/setInfinity.html),
and [bakeResults](https://help.autodesk.com/cloudhelp/2023/ENU/Maya-Tech-Docs/CommandsPython/bakeResults.html).

```bash
python tests/test_animation.py
python tests/test_bridge.py
```

Tests use Maya stubs for command arguments, validation, curve pagination, time restoration,
undo cleanup, tool registration and read-only mode. Live animation evaluation still needs Maya verification.
