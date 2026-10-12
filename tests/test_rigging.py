"""Rigging command and API contracts with Maya stubs."""
from types import SimpleNamespace
from unittest.mock import patch
from test_bridge import plugin, fake_cmds, om, check
from test_modeling import rejected


def test_rigging():
    with patch.object(plugin, "_require_node", side_effect=lambda n: n), \
         patch.object(fake_cmds, "createNode", create=True, side_effect=["root", "root|tip"]) as create, \
         patch.object(fake_cmds, "select", create=True) as select, \
         patch.object(fake_cmds, "setAttr", create=True):
        r = plugin.handle_create_joint_chain({"positions": [[0, 0, 0], [0, 5, 0]], "names": ["root", "tip"]})
        check("chain explicit parenting", r["joints"] == ["root", "root|tip"] and create.call_args.kwargs["parent"] == "root")
        check("chain selection restored", select.call_args.kwargs == {"clear": True})
        rejected(plugin.handle_create_joint_chain, {"positions": [[0, 0, 0]], "names": ["a", "b"]})
    with patch.object(fake_cmds, "parentConstraint", create=True, return_value=["constraint1"]) as command:
        plugin.handle_create_constraint({"targets": ["|pCube1"], "driven": "|pSphere1"})
        check("constraint maintains offset", command.call_args.kwargs == {"maintainOffset": True})
        rejected(plugin.handle_create_constraint, {"targets": ["|pCube1"], "driven": "|pCube1"})
        rejected(plugin.handle_create_constraint, {"targets": ["|pCube1"], "driven": "|pSphere1", "kind": "pole_vector"})
    with patch.object(plugin, "_joint", side_effect=lambda n: n), \
         patch.object(fake_cmds, "listRelatives", return_value=["tip"]), \
         patch.object(fake_cmds, "ikHandle", create=True, return_value=["ik1", "eff1"]) as ik:
        r = plugin.handle_create_ik_handle({"start_joint": "root", "end_joint": "tip"})
        check("IK chain and solver", r["effector"] == "eff1" and ik.call_args.kwargs["solver"] == "ikRPsolver")
        rejected(plugin.handle_create_ik_handle, {"start_joint": "root", "end_joint": "other"})
        rejected(plugin.handle_create_ik_handle, {"start_joint": "root", "end_joint": "tip", "solver": "invalid"})
    with patch.object(plugin, "_mesh", return_value=("meshShape", None)), \
         patch.object(plugin, "_joint", side_effect=lambda n: n), \
         patch.object(fake_cmds, "listHistory", create=True, return_value=[]), \
         patch.object(fake_cmds, "skinCluster", create=True, return_value=["skin1"]) as bind:
        r = plugin.handle_bind_skin({"mesh": "mesh", "joints": ["|pCube1"], "skinning_method": 1})
        check("bind uses documented skinMethod", r["skin_cluster"] == "skin1" and bind.call_args.kwargs["skinMethod"] == 1)
        rejected(plugin.handle_bind_skin, {"mesh": "mesh", "joints": ["|pCube1"], "max_influences": 0})
        with patch.object(fake_cmds, "listHistory", return_value=["skin1"]), patch.object(fake_cmds, "nodeType", return_value="skinCluster"):
            rejected(plugin.handle_bind_skin, {"mesh": "mesh", "joints": ["|pCube1"]})
    class Skin:
        def numOutputConnections(self): return 1
        def indexForOutputConnection(self, index): return 7
        def getPathAtIndex(self, index):
            assert index == 7
            return SimpleNamespace(fullPathName=lambda: "|meshShape", apiType=lambda: 1)
        def influenceObjects(self):
            return [SimpleNamespace(fullPathName=lambda: "|root"), SimpleNamespace(fullPathName=lambda: "|tip")]
        def indexForInfluenceObject(self, path): return 4 if path.fullPathName() == "|root" else 9
        def getWeights(self, path, component): return [0.25, 0.75, 0.5, 0.5], 2
    class Component:
        def create(self, kind): return self
        def addElements(self, elements): assert elements == [1, 2]
    with patch.object(plugin, "_skin", return_value=("skin1", Skin())), \
         patch.object(om, "MFnMesh", create=True, return_value=SimpleNamespace(numVertices=3)), \
         patch.object(om, "MFnSingleIndexedComponent", create=True, side_effect=Component), \
         patch.object(om, "MFn", create=True, new=SimpleNamespace(kMeshVertComponent=1)), \
         patch.object(fake_cmds, "skinCluster", create=True, return_value=["meshShape"]), \
         patch.object(fake_cmds, "nodeType", return_value="mesh"), \
         patch.object(fake_cmds, "getAttr", create=True, return_value=1):
        r = plugin.handle_get_skin_weights({"start": 1, "limit": 2})
        check("weight pagination and influence order", r["vertices"] == [{"index": 1, "weights": [0.25, 0.75]}, {"index": 2, "weights": [0.5, 0.5]}] and r["influences"] == ["|root", "|tip"])
        check("empty weight page", plugin.handle_get_skin_weights({"start": 3})["vertices"] == [])
        check("sparse logical influence indices", plugin.handle_get_skin_info({})["influences"][1]["logical_index"] == 9)
        rejected(plugin.handle_get_skin_weights, {"limit": 201})
    with patch.object(fake_cmds, "createNode", create=True, side_effect=RuntimeError("failed")), patch.object(fake_cmds, "select", create=True):
        try: plugin.handle_create_joint_chain({"positions": [[0, 0, 0]]})
        except RuntimeError: pass
    check("rigging failure closes undo chunk", fake_cmds.undo_open == 0)


if __name__ == "__main__":
    test_rigging()
    print("ALL RIGGING OK")
