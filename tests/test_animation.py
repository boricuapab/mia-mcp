"""Animation contracts using Maya stubs, without editing a live scene."""
from types import SimpleNamespace
from unittest.mock import patch
from test_bridge import plugin, fake_cmds, om, check
from test_modeling import rejected


def test_animation():
    with patch.object(fake_cmds, "currentTime", create=True, return_value=12) as time, \
         patch.object(fake_cmds, "currentUnit", create=True, return_value="film"), \
         patch.object(fake_cmds, "playbackOptions", create=True, return_value=24) as playback:
        plugin.handle_set_timeline({"current_time": 5, "start": 1, "end": 48})
        check("timeline range and time forwarded", playback.call_args_list[0].kwargs == {"minTime": 1., "maxTime": 48.} and time.call_args_list[0].args == (5.,))
        rejected(plugin.handle_set_timeline, {"start": 5, "end": 1})
        rejected(plugin.handle_set_timeline, {"start": 1})
        rejected(plugin.handle_set_timeline, {"current_time": float("nan")})
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.tx"]), \
         patch.object(fake_cmds, "getAttr", create=True, side_effect=lambda p, **kw: "doubleLinear" if kw.get("type") else False), \
         patch.object(fake_cmds, "setKeyframe", create=True, return_value=2) as key:
        r = plugin.handle_set_keyframes({"times": [1, 24], "value": 10})
        check("keyframe explicit times/value", r["key_count"] == 2 and key.call_args.kwargs["value"] == 10.)
        rejected(plugin.handle_set_keyframes, {"times": []})
        with patch.object(fake_cmds, "getAttr", return_value=True):
            rejected(plugin.handle_set_keyframes, {"times": [1]})
        with patch.object(plugin, "_animation_plugs", return_value=["a.tx", "a.ty"]):
            rejected(plugin.handle_set_keyframes, {"times": [1], "value": 1})
    params = {"start": 1, "end": 24}
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.tx"]), \
         patch.object(fake_cmds, "cutKey", create=True) as cut, \
         patch.object(fake_cmds, "keyTangent", create=True) as tangent, \
         patch.object(fake_cmds, "setInfinity", create=True) as infinity:
        plugin.handle_delete_keyframes(params)
        check("delete avoids clipboard and selection", cut.call_args.kwargs["clear"] and cut.call_args.kwargs["animation"] == "objects")
        plugin.handle_set_key_tangents({**params, "out_tangent": "step"})
        check("step outgoing tangent", tangent.call_args.kwargs["outTangentType"] == "step")
        rejected(plugin.handle_set_key_tangents, {**params, "in_tangent": "step"})
        plugin.handle_set_curve_infinity({"pre": "cycle", "post": "oscillate"})
        check("curve infinity", infinity.call_args.kwargs == {"preInfinite": "cycle", "postInfinite": "oscillate"})
        rejected(plugin.handle_set_curve_infinity, {"post": "invalid"})
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.tx"]), \
         patch.object(fake_cmds, "currentTime", create=True, return_value=12) as time, \
         patch.object(fake_cmds, "bakeResults", create=True, return_value=1) as bake:
        plugin.handle_bake_animation(params)
        check("bake preserves outside keys and controls", bake.call_args.kwargs["preserveOutsideKeys"] and not bake.call_args.kwargs["disableImplicitControl"])
        check("bake restores time", time.call_args.args == (12,) and time.call_args.kwargs == {"edit": True})
        rejected(plugin.handle_bake_animation, {"start": 0, "end": 30000})
        rejected(plugin.handle_bake_animation, {**params, "sample_by": 0})
        bake.side_effect = RuntimeError("simulation failed")
        try: plugin.handle_bake_animation(params)
        except RuntimeError: pass
        check("bake failure restores time and undo", time.call_args.args == (12,) and fake_cmds.undo_open == 0)
    class Curve:
        numKeys, animCurveType, isTimeInput, isWeighted = 3, 1, True, False
        preInfinityType, postInfinityType = 0, 3
        def input(self, index): return SimpleNamespace(asUnits=lambda unit: [1, 12, 24][index])
        def value(self, index): return [0, 5, 10][index]
        def inTangentType(self, index): return 11
        def outTangentType(self, index): return 2
        def isBreakdown(self, index): return index == 1
    curve = Curve()
    with patch.object(plugin, "_anim_curve", return_value=("anim1", curve)), \
         patch.object(om, "MTime", create=True, new=SimpleNamespace(uiUnit=lambda: 6)):
        r = plugin.handle_get_animation_curve({"start": 1, "limit": 1})
        check("API curve pagination and time conversion", len(r["keys"]) == 1 and r["keys"][0]["input"] == 12 and r["keys"][0]["breakdown"])
        check("empty curve page", plugin.handle_get_animation_curve({"start": 3})["keys"] == [])
        curve.animCurveType = 2
        rejected(plugin.handle_get_animation_curve, {})
    check("animation undo chunks balanced", fake_cmds.undo_open == 0)


if __name__ == "__main__":
    test_animation()
    print("ALL ANIMATION OK")
