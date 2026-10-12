"""Standalone command-contract tests; no Maya installation required."""
from unittest.mock import patch

from test_bridge import plugin, fake_cmds, check


def rejected(handler, params):
    try:
        handler(params)
    except plugin.CommandError:
        return
    raise AssertionError("Expected CommandError")


def test_modeling():
    with patch.object(fake_cmds, "polyCube", return_value=["|pCube1", "polyCube1"]) as create:
        result = plugin.handle_create_object({"kind": "cube", "size": 2, "position": [1, 2, 3]})
        check("primitive creation", result["name"] == "|pCube1" and create.call_count == 1)
        check("primitive scale", fake_cmds.last_transform[1] == {"scale": [2., 2., 2.]})
    rejected(plugin.handle_create_object, {"kind": "unsupported"})
    rejected(plugin.handle_create_object, {"size": -1})
    with patch.object(fake_cmds, "curve", create=True, return_value="|pCube1") as curve:
        plugin.handle_create_curve({"points": [[0, 0, 0], [1, 2, 3]], "degree": 1})
        check("curve control points", curve.call_args.kwargs["point"] == [[0., 0., 0.], [1., 2., 3.]])
    rejected(plugin.handle_create_curve, {"points": [[0, 0, 0]], "degree": 3})
    with patch.object(fake_cmds, "select", create=True) as select:
        plugin.handle_select_objects({"names": ["|pCube1"], "mode": "add"})
        check("explicit additive selection", select.call_args.kwargs == {"add": True})
        plugin.handle_select_objects({"mode": "clear"})
        check("clear selection", select.call_args.kwargs == {"clear": True})
    with patch.object(fake_cmds, "getPanel", create=True, side_effect=["modelPanel4", ["modelPanel4"]]), \
         patch.object(fake_cmds, "modelPanel", create=True, return_value="persp"), \
         patch.object(fake_cmds, "viewFit", create=True) as fit:
        plugin.handle_frame_objects({"names": ["|pCube1"]})
        check("viewport explicit target", fit.call_args.args == ("persp", ["|pCube1"]))
    with patch.object(fake_cmds, "group", create=True, return_value="group1") as group:
        plugin.handle_organize_objects({"names": ["|pCube1"], "name": "props"})
        check("named group", group.call_args.kwargs == {"name": "props"})
    rejected(plugin.handle_organize_objects, {"names": ["|pCube1"], "action": "parent", "parent": "|pCube1"})
    with patch.object(fake_cmds, "parent", create=True, return_value=["pCube1"]) as parent:
        plugin.handle_organize_objects({"names": ["|pCube1"], "action": "parent", "parent": "|pSphere1"})
        check("parent preserves placement", parent.call_args.kwargs == {"absolute": True})
    with patch.object(plugin, "_mesh", return_value=("mesh", None)), \
         patch.object(fake_cmds, "polySmooth", create=True, return_value="smooth1") as smooth:
        plugin.handle_mesh_operation({"names": ["|pCube1"], "operation": "smooth", "divisions": 2})
        check("smooth keeps history", smooth.call_args.kwargs == {"divisions": 2, "constructionHistory": True})
        rejected(plugin.handle_mesh_operation, {"names": ["|pCube1"], "operation": "combine"})
        rejected(plugin.handle_mesh_operation, {"names": ["|pCube1"], "operation": "smooth", "divisions": 4})
    with patch.object(fake_cmds, "shadingNode", create=True, return_value="paint"), \
         patch.object(fake_cmds, "sets", create=True, return_value="paintSG") as sets, \
         patch.object(fake_cmds, "setAttr", create=True), \
         patch.object(fake_cmds, "connectAttr", create=True) as connect:
        r = plugin.handle_create_material({"names": ["|pCube1"], "color": [1, 0, 0]})
        check("shader wired and assigned", r["shading_engine"] == "paintSG" and connect.call_args.args == ("paint.outColor", "paintSG.surfaceShader") and sets.call_args.kwargs["forceElement"] == "paintSG")
    rejected(plugin.handle_create_material, {"color": [2, 0, 0]})
    with patch.object(plugin, "_plug", return_value="|pCube1.visibility"), \
         patch.object(fake_cmds, "getAttr", create=True, side_effect=lambda p, **kw: "bool" if kw.get("type") else True), \
         patch.object(fake_cmds, "setAttr", create=True) as setter:
        plugin.handle_set_attribute({"value": False})
        check("boolean attribute", setter.call_args.args == ("|pCube1.visibility", False))
        rejected(plugin.handle_set_attribute, {"value": 2})
    with patch.object(fake_cmds, "curve", create=True, side_effect=RuntimeError("failure")):
        try:
            plugin.handle_create_curve({"points": [[0, 0, 0], [1, 0, 0]]})
        except RuntimeError:
            pass
    check("editing chunks balanced including failure", fake_cmds.undo_open == 0)


if __name__ == "__main__":
    test_modeling()
    print("ALL MODELING OK")
