"""End-to-end test of the bridge protocol with a stubbed `maya` package.

Run:  python tests/test_bridge.py
Requires `mcp` to be installed (pip install -e .).
"""

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---- fake maya ------------------------------------------------------------

class FakeCmds(types.ModuleType):
    def __init__(self):
        super().__init__("maya.cmds")
        self.undo_open = 0
        self.nodes = {"|pCube1": "transform", "|pSphere1": "transform", "|persp": "transform"}

    def about(self, version=False):
        return "2026"

    def ls(self, *args, **kw):
        if args and args[0] in self.nodes:
            return [args[0]]
        if kw.get("type") == "transform" or args == ("*",) and kw.get("type") == "transform":
            return list(self.nodes)
        return []

    def nodeType(self, n, **kw):
        return self.nodes.get(n, "transform")

    def listRelatives(self, *a, **kw):
        return []

    def undoInfo(self, **kw):
        self.undo_open += 1 if kw.get("openChunk") else -1 if kw.get("closeChunk") else 0

    def polyCube(self):
        return "pCube1"

    def undo(self):
        pass

    def objExists(self, name):
        return name in self.nodes

    def objectType(self, name, **kw):
        return self.nodes.get(name) == "transform"

    def xform(self, name, **kw):
        self.last_transform = (name, kw)


fake_cmds = FakeCmds()
# real maya.cmds exposes commands in the module __dict__ (which is what dir() lists)
for _name in ("about", "ls", "nodeType", "listRelatives", "undoInfo", "polyCube", "undo"):
    setattr(fake_cmds, _name, getattr(fake_cmds, _name))
maya = types.ModuleType("maya")
maya_api = types.ModuleType("maya.api")
om = types.ModuleType("maya.api.OpenMaya")
om.MGlobal = types.SimpleNamespace(displayInfo=lambda m: None)
maya_mel = types.ModuleType("maya.mel")
maya_utils = types.ModuleType("maya.utils")
maya_utils.executeInMainThreadWithResult = lambda fn, *a: fn(*a)
maya.cmds, maya.mel, maya.utils, maya.api = fake_cmds, maya_mel, maya_utils, maya_api
maya_api.OpenMaya = om
sys.modules.update({
    "maya": maya, "maya.cmds": fake_cmds, "maya.mel": maya_mel, "maya.utils": maya_utils,
    "maya.api": maya_api, "maya.api.OpenMaya": om,
})

spec = importlib.util.spec_from_file_location("maya_mcp_plugin", ROOT / "maya_plugin" / "maya_mcp_plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

from maya_mcp.connection import MayaError, send_command  # noqa: E402

PORT = 19877
os.environ["MAYA_MCP_PORT"] = str(PORT)


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def test_protocol():
    server = plugin.BridgeServer(port=PORT)
    server.start()
    try:
        check("ping", send_command("ping")["pong"] is True)

        r = send_command("execute_python", {"code": "x = 21\nprint('hi')\nx * 2"})
        check("execute_python stdout + last expr", r["stdout"].strip() == "hi" and r["result"] == "42")

        r = send_command("execute_python", {"code": "x + 1"})
        check("namespace persists between calls", r["result"] == "22")

        r = send_command("execute_python", {"code": "cmds.polyCube()"})
        check("cmds available in namespace", r["result"] == "'pCube1'")
        check("undo chunk balanced", fake_cmds.undo_open == 0)

        try:
            send_command("execute_python", {"code": "print('before')\n1/0"})
            check("python error raises", False)
        except MayaError as e:
            check("python error returns traceback + stdout", "ZeroDivisionError" in str(e) and "before" in str(e))
        check("undo chunk closed after error", fake_cmds.undo_open == 0)

        r = send_command("list_objects", {"limit": 10})
        check("list_objects excludes default cameras", r["total"] == 2)

        r = send_command("search_commands", {"query": "polycube"})
        check("search_commands", r["commands"] == ["polyCube"])

        try:
            send_command("nope")
            check("unknown command raises", False)
        except MayaError as e:
            check("unknown command lists available", "Available" in str(e))
    finally:
        server.stop()


def test_token():
    server = plugin.BridgeServer(port=PORT, token="s3cret")
    server.start()
    try:
        try:
            send_command("ping", token="")
            check("missing token rejected", False)
        except MayaError as e:
            check("missing token rejected", "Unauthorized" in str(e))
        check("correct token accepted", send_command("ping", token="s3cret")["pong"])
    finally:
        server.stop()


def test_connection_refused():
    try:
        send_command("ping", port=PORT + 1)
        check("refused -> helpful error", False)
    except MayaError as e:
        check("refused -> helpful error", "Is Maya running" in str(e))


def is_err(res):
    # mcp 1.x: isError, mcp 2.x: is_error
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


def test_extended_tools():
    point = lambda x, y, z: types.SimpleNamespace(x=x, y=y, z=z)
    class Mesh:
        numVertices, numEdges, numPolygons = 3, 3, 1
        def getPoints(self, space):
            self.space = space
            return [point(0, 0, 0), point(1, 0, 0), point(0, 1, 0)]
        def getClosestPoint(self, value, space):
            return point(value.x, value.y, 0), 0
        def getUVSetNames(self):
            return ["map1"]
        def numUVs(self, name):
            return 3
        def getColorSetNames(self):
            return []
    mesh = Mesh()
    original = plugin._mesh
    plugin._mesh = lambda params: ("|meshShape", mesh)
    om.MSpace = types.SimpleNamespace(kWorld=4, kObject=2)
    om.MPoint = point
    try:
        r = plugin.handle_get_mesh_vertices({"start": 1, "limit": 1, "space": "object"})
        check("vertex pagination and object space", r["vertices"] == [{"index": 1, "position": [1, 0, 0]}] and mesh.space == 2)
        check("mesh UV summary", plugin.handle_get_mesh_info({})["uv_sets"] == [{"name": "map1", "uv_count": 3}])
        check("closest surface point", plugin.handle_get_closest_point({"point": [1, 2, 3]})["position"] == [1, 2, 0])
        for handler, params in ((plugin.handle_get_mesh_vertices, {"limit": 0}),
                                (plugin.handle_get_mesh_vertices, {"start": -1}),
                                (plugin.handle_get_closest_point, {"point": [1, 2, float("nan")]}),
                                (plugin.handle_get_mesh_vertices, {"space": "invalid"})):
            try:
                handler(params)
                check("invalid arguments rejected", False)
            except plugin.CommandError:
                pass
        check("invalid mesh arguments rejected", True)
    finally:
        plugin._mesh = original
    plugin.handle_set_transform({"name": "|pCube1", "translation": [1, 2, 3], "space": "object"})
    check("transform space and undo chunk", fake_cmds.last_transform[1] == {"worldSpace": False, "translation": [1., 2., 3.]} and fake_cmds.undo_open == 0)
    original_xform = fake_cmds.xform
    def fail(*args, **kwargs):
        raise RuntimeError("locked transform")
    fake_cmds.xform = fail
    try:
        plugin.handle_set_transform({"name": "|pCube1", "scale": [2, 2, 2]})
    except RuntimeError:
        check("transform failure closes undo chunk", fake_cmds.undo_open == 0)
    finally:
        fake_cmds.xform = original_xform


async def test_mcp_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = plugin.BridgeServer(port=PORT)
    server.start()
    try:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "maya_mcp.server"],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])), "MAYA_MCP_PORT": str(PORT)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                check("MCP lists tools", {"get_scene_info", "execute_python", "undo", "get_viewport_screenshot", "get_mesh_info", "get_mesh_vertices", "get_closest_point", "list_attributes", "get_connections", "get_api_help", "set_transform"} <= tools)
                res = await session.call_tool("execute_python", {"code": "1 + 1"})
                check("MCP execute_python round trip", not is_err(res) and '"2"' in res.content[0].text)
                res = await session.call_tool("get_object_info", {"name": "does_not_exist"})
                check("MCP surfaces errors as isError", is_err(res))

        # read-only mode hides mutating tools
        params.env["MAYA_MCP_READONLY"] = "1"
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                check("read-only hides all editing tools", not {"execute_python", "undo", "set_transform"} & tools)
                check("read-only retains mesh inspection", {"get_mesh_info", "get_closest_point"} <= tools)
    finally:
        server.stop()


if __name__ == "__main__":
    test_extended_tools()
    test_protocol()
    test_token()
    test_connection_refused()
    asyncio.run(test_mcp_stdio())
    print("ALL OK")
