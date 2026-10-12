"""Character animation command contracts with Maya stubs."""
from unittest.mock import patch
from test_bridge import plugin, fake_cmds, check
from test_modeling import rejected


def test_character_animation():
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.translateX"]), \
         patch.object(fake_cmds, "animLayer", create=True, return_value="bodyLayer") as layer:
        plugin.handle_create_animation_layer({"layer": "bodyLayer", "override": True})
        check("layer explicit membership", layer.call_args.kwargs == {"edit": True, "attribute": ["|ctrl.translateX"]})
        rejected(plugin.handle_create_animation_layer, {"layer": "|pCube1"})
    with patch.object(plugin, "_typed_node", return_value="bodyLayer"), \
         patch.object(fake_cmds, "animLayer", create=True) as layer:
        plugin.handle_edit_animation_layer({"layer": "bodyLayer", "weight": 0.5, "mute": True})
        check("layer weight/mute", layer.call_args.kwargs == {"edit": True, "mute": True, "weight": 0.5})
        rejected(plugin.handle_edit_animation_layer, {"weight": 2})
        rejected(plugin.handle_edit_animation_layer, {})
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.translateX"]), \
         patch.object(plugin, "_typed_node", return_value="bodyLayer"), \
         patch.object(plugin, "_canonical_plug", side_effect=lambda p: p.replace("ctrl.tx", "ctrl.translateX")), \
         patch.object(fake_cmds, "getAttr", create=True, side_effect=lambda p, **kw: "doubleLinear" if kw.get("type") else False), \
         patch.object(fake_cmds, "animLayer", create=True, side_effect=lambda n, **kw: False if kw.get("lock") else ["|ctrl.tx"]), \
         patch.object(fake_cmds, "setKeyframe", create=True, return_value=1) as key:
        plugin.handle_set_keyframes({"times": [1], "layer": "bodyLayer"})
        check("key explicit layer", key.call_args.kwargs["animLayer"] == "bodyLayer")
        with patch.object(fake_cmds, "animLayer", return_value=[]):
            rejected(plugin.handle_set_keyframes, {"times": [1], "layer": "bodyLayer"})
        with patch.object(fake_cmds, "animLayer", return_value=True):
            rejected(plugin.handle_set_keyframes, {"times": [1], "layer": "bodyLayer"})
    units = lambda **kw: "cm" if kw.get("linear") else "deg" if kw.get("angle") else "film"
    with patch.object(plugin, "_nodes", return_value=["|ctrl"]), \
         patch.object(plugin, "_plug", side_effect=lambda p: p["name"] + "." + p["attribute"]), \
         patch.object(fake_cmds, "listAttr", create=True, return_value=["translateX"]), \
         patch.object(fake_cmds, "currentUnit", create=True, side_effect=units), \
         patch.object(fake_cmds, "getAttr", create=True, side_effect=lambda p, **kw: "doubleLinear" if kw.get("type") else True if kw.get("settable") else 5), \
         patch.object(fake_cmds, "setAttr", create=True) as setter:
        pose = plugin.handle_capture_pose({})
        check("pose scalar value and units", pose["channels"][0]["value"] == 5 and pose["angular_unit"] == "deg")
        plugin.handle_apply_pose({"pose": pose})
        check("pose apply", setter.call_args.args == ("|ctrl.translateX", 5.))
        rejected(plugin.handle_apply_pose, {"pose": {**pose, "linear_unit": "m"}})
        rejected(plugin.handle_apply_pose, {"pose": {**pose, "channels": pose["channels"] * 2}})
        with patch.object(fake_cmds, "getAttr", side_effect=lambda p, **kw: "doubleLinear" if kw.get("type") else False):
            rejected(plugin.handle_apply_pose, {"pose": pose})
    with patch.object(plugin, "_typed_node", return_value="faceBS"), \
         patch.object(fake_cmds, "getAttr", create=True, side_effect=lambda p, **kw: [0, 7] if kw.get("multiIndices") else True if kw.get("settable") else 0.25), \
         patch.object(fake_cmds, "aliasAttr", create=True, return_value=["smile", "weight[7]"]), \
         patch.object(fake_cmds, "blendShape", create=True, return_value=["headShape"]), \
         patch.object(fake_cmds, "setAttr", create=True) as setter:
        r = plugin.handle_get_blend_shape_info({})
        check("sparse blend shape aliases", r["targets"][1]["index"] == 7 and r["targets"][1]["alias"] == "smile")
        plugin.handle_set_blend_shape_weights({"weights": {"7": 0.75}})
        check("blend shape explicit weight", setter.call_args.args == ("faceBS.weight[7]", 0.75))
        rejected(plugin.handle_set_blend_shape_weights, {"weights": {"1": 0.5}})
        rejected(plugin.handle_set_blend_shape_weights, {"weights": {"7": 2}})
        with patch.object(fake_cmds, "setAttr", side_effect=RuntimeError("failed")):
            try: plugin.handle_set_blend_shape_weights({"weights": {"7": 0.5}})
            except RuntimeError: pass
    with patch.object(plugin, "_animation_plugs", return_value=["|ctrl.tx"]), \
         patch.object(plugin, "_typed_node", return_value="hero"), \
         patch.object(fake_cmds, "character", create=True, return_value="hero") as character:
        plugin.handle_create_character_set({"character": "hero"})
        check("character explicit attributes", character.call_args.args == (["|ctrl.tx"],))
        character.return_value = ["|ctrl.tx", "|ctrl.ty"]
        check("character member limit", plugin.handle_get_character_set({"limit": 1})["members"] == ["|ctrl.tx"])
    check("character edits balance undo chunks", fake_cmds.undo_open == 0)


if __name__ == "__main__":
    test_character_animation()
    print("ALL CHARACTER ANIMATION OK")
