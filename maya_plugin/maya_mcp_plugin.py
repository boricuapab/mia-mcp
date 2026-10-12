"""Maya MCP bridge plugin.

Runs a small localhost TCP server inside Maya. The external MCP server
(``maya-mcp``) connects to it and sends newline-delimited JSON requests::

    -> {"id": "...", "type": "get_scene_info", "params": {}, "token": "..."}
    <- {"id": "...", "status": "ok", "result": {...}}
    <- {"id": "...", "status": "error", "message": "..."}

All Maya work is marshalled onto Maya's main thread with
``maya.utils.executeInMainThreadWithResult``, so it is safe to call
``maya.cmds`` from the handlers below.

SECURITY: ``execute_python`` runs arbitrary code inside Maya with your user
permissions. The socket only binds to 127.0.0.1, and you can additionally set
``MAYA_MCP_TOKEN`` (in both Maya's and the MCP server's environment) to require
a shared secret. Do not run this on a machine holding data you can't afford to
lose.

Load with::

    import maya.cmds as cmds
    cmds.loadPlugin("/path/to/maya_mcp_plugin.py")

or copy the file into a folder on MAYA_PLUG_IN_PATH and enable it in the
Plug-in Manager (tick "Auto load" to start it with Maya).
"""

import ast
import base64
import contextlib
import glob
import hmac
import io
import json
import math
import os
import socket
import tempfile
import threading
import traceback

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel
import maya.utils

__version__ = "0.3.0"

HOST = "127.0.0.1"
DEFAULT_PORT = 9877
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_TEXT = 20000  # truncate stdout / repr output returned to the LLM

_DEFAULT_CAMERAS = {"|persp", "|top", "|front", "|side", "|back", "|left", "|bottom"}


class CommandError(Exception):
    """Raised by handlers for errors that should be shown to the client."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text, limit=MAX_TEXT):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated {} chars]".format(len(text) - limit)


def _short(name):
    return name.rsplit("|", 1)[-1]


def _first_shape_type(node):
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True) or []
    return cmds.nodeType(shapes[0]) if shapes else None


def _require_node(name):
    if not name or not cmds.objExists(name):
        raise CommandError("Node not found: {!r}".format(name))
    matches = cmds.ls(name, long=True)
    if len(matches) > 1:
        raise CommandError(
            "Name {!r} is ambiguous; use a full path. Matches: {}".format(name, matches[:10])
        )
    return matches[0]


def _poly_count(mesh, **flag):
    try:
        value = cmds.polyEvaluate(mesh, **flag)
        return int(value) if isinstance(value, (int, float)) else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Command handlers (always executed on Maya's main thread)
# ---------------------------------------------------------------------------

def handle_ping(params):
    return {"pong": True, "plugin_version": __version__, "maya_version": cmds.about(version=True)}


def handle_get_scene_info(params):
    transforms = [t for t in (cmds.ls(type="transform", long=True) or []) if t not in _DEFAULT_CAMERAS]
    meshes = cmds.ls(type="mesh", long=True, noIntermediate=True) or []
    lights = cmds.ls(lights=True, long=True) or []
    cameras = [c for c in (cmds.ls(cameras=True, long=True) or [])
               if cmds.listRelatives(c, parent=True, fullPath=True)[0] not in _DEFAULT_CAMERAS]
    materials = cmds.ls(materials=True) or []
    try:
        renderer = cmds.getAttr("defaultRenderGlobals.currentRenderer")
    except Exception:
        renderer = None
    return {
        "scene_file": cmds.file(query=True, sceneName=True) or "untitled",
        "modified": bool(cmds.file(query=True, modified=True)),
        "maya_version": cmds.about(version=True),
        "linear_unit": cmds.currentUnit(query=True, linear=True),
        "time_unit": cmds.currentUnit(query=True, time=True),
        "up_axis": cmds.upAxis(query=True, axis=True),
        "current_frame": cmds.currentTime(query=True),
        "frame_range": [
            cmds.playbackOptions(query=True, minTime=True),
            cmds.playbackOptions(query=True, maxTime=True),
        ],
        "renderer": renderer,
        "counts": {
            "transforms": len(transforms),
            "meshes": len(meshes),
            "lights": len(lights),
            "cameras": len(cameras),
            "materials": len(materials),
            "total_faces": sum(_poly_count(m, face=True) for m in meshes),
        },
        "selection": cmds.ls(selection=True, long=True) or [],
        "top_level_nodes": [_short(n) for n in (cmds.ls(assemblies=True, long=True) or [])
                            if n not in _DEFAULT_CAMERAS][:100],
    }


def handle_list_objects(params):
    node_type = params.get("node_type") or "transform"
    pattern = params.get("pattern") or "*"
    limit = int(params.get("limit", 200))
    nodes = cmds.ls(pattern, type=node_type, long=True) or []
    nodes = [n for n in nodes if n not in _DEFAULT_CAMERAS]
    return {
        "total": len(nodes),
        "returned": min(len(nodes), limit),
        "nodes": [{"name": n, "type": cmds.nodeType(n), "shape_type": _first_shape_type(n)
                   if cmds.nodeType(n) == "transform" else None} for n in nodes[:limit]],
    }


def handle_get_selection(params):
    sel = cmds.ls(selection=True, long=True) or []
    return {"selection": [{"name": n, "type": cmds.nodeType(n)} for n in sel]}


def handle_get_object_info(params):
    node = _require_node(params.get("name"))
    info = {"name": node, "type": cmds.nodeType(node), "inherited_types": cmds.nodeType(node, inherited=True)}

    if cmds.objectType(node, isAType="transform"):
        parent = cmds.listRelatives(node, parent=True, fullPath=True)
        info["parent"] = parent[0] if parent else None
        info["children"] = cmds.listRelatives(node, children=True, fullPath=True, type="transform") or []
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True) or []
        info["shapes"] = [{"name": s, "type": cmds.nodeType(s)} for s in shapes]
        info["translate"] = list(cmds.getAttr(node + ".translate")[0])
        info["rotate"] = list(cmds.getAttr(node + ".rotate")[0])
        info["scale"] = list(cmds.getAttr(node + ".scale")[0])
        info["visibility"] = cmds.getAttr(node + ".visibility")
        try:
            info["world_bounding_box"] = cmds.exactWorldBoundingBox(node)  # xmin ymin zmin xmax ymax zmax
        except Exception:
            pass

        meshes = [s["name"] for s in info["shapes"] if s["type"] == "mesh"]
        if meshes:
            info["mesh"] = {
                "vertices": sum(_poly_count(m, vertex=True) for m in meshes),
                "edges": sum(_poly_count(m, edge=True) for m in meshes),
                "faces": sum(_poly_count(m, face=True) for m in meshes),
                "triangles": sum(_poly_count(m, triangle=True) for m in meshes),
            }
        engines = []
        for s in shapes:
            engines += cmds.listConnections(s, type="shadingEngine") or []
        engines = sorted(set(engines))
        materials = []
        for sg in engines:
            materials += cmds.listConnections(sg + ".surfaceShader") or []
        info["shading_engines"] = engines
        info["materials"] = sorted(set(materials))

    history = cmds.listHistory(node, pruneDagObjects=True) or []
    info["history"] = [{"name": h, "type": cmds.nodeType(h)} for h in history if h != node][:50]

    attrs = {}
    for attr in (cmds.listAttr(node, keyable=True) or [])[:60]:
        try:
            value = cmds.getAttr("{}.{}".format(node, attr))
            attrs[attr] = value if isinstance(value, (int, float, str, bool)) else str(value)
        except Exception:
            continue
    info["keyable_attributes"] = attrs
    return info


def handle_get_scene_graph(params):
    root = params.get("root")
    max_depth = int(params.get("max_depth", 3))
    budget = [int(params.get("limit", 500))]

    def build(node, depth):
        budget[0] -= 1
        entry = {"name": _short(node), "path": node, "shape": _first_shape_type(node)}
        children = cmds.listRelatives(node, children=True, fullPath=True, type="transform") or []
        if children:
            if depth >= max_depth:
                entry["children_hidden"] = len(children)
            else:
                entry["children"] = []
                for child in children:
                    if budget[0] <= 0:
                        entry["truncated"] = True
                        break
                    entry["children"].append(build(child, depth + 1))
        return entry

    if root:
        roots = [_require_node(root)]
    else:
        roots = [n for n in (cmds.ls(assemblies=True, long=True) or []) if n not in _DEFAULT_CAMERAS]
    return {"graph": [build(r, 1) for r in roots if budget[0] > 0]}


_namespace = {"__name__": "__maya_mcp__"}


def handle_execute_python(params):
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CommandError("'code' must be a non-empty string")

    _namespace.update({"cmds": cmds, "mel": mel, "om": om})
    stdout = io.StringIO()
    result_repr = None
    cmds.undoInfo(openChunk=True, chunkName="maya_mcp")
    try:
        tree = ast.parse(code, mode="exec")
        last_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_expr = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            exec(compile(tree, "<maya_mcp>", "exec"), _namespace)
            if last_expr is not None:
                value = eval(compile(last_expr, "<maya_mcp>", "eval"), _namespace)
                if value is not None:
                    result_repr = repr(value)
    except Exception:
        raise CommandError(
            "{}\n{}".format(_truncate(stdout.getvalue()), traceback.format_exc(limit=-3)).strip()
        )
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"stdout": _truncate(stdout.getvalue()),
            "result": _truncate(result_repr) if result_repr is not None else None}


def handle_undo(params):
    steps = max(1, min(int(params.get("steps", 1)), 20))
    for _ in range(steps):
        cmds.undo()
    return {"undone": steps}


def handle_search_commands(params):
    query = (params.get("query") or "").lower()
    if not query:
        raise CommandError("'query' is required")
    limit = int(params.get("limit", 50))
    names = sorted(n for n in dir(cmds) if query in n.lower() and not n.startswith("_"))
    return {"total": len(names), "commands": names[:limit]}


def handle_get_command_help(params):
    command = params.get("command")
    if not command or not hasattr(cmds, command):
        raise CommandError("Unknown maya.cmds command: {!r}".format(command))
    try:
        text = cmds.help(command, language="python")
    except Exception:
        text = cmds.help(command)
    return {"command": command, "help": _truncate(str(text))}


def handle_get_viewport_screenshot(params):
    width = max(64, min(int(params.get("width", 960)), 4096))
    height = max(64, min(int(params.get("height", 540)), 4096))
    tmp_dir = tempfile.mkdtemp(prefix="maya_mcp_")
    base = os.path.join(tmp_dir, "shot")
    try:
        cmds.playblast(
            completeFilename=base + ".png", format="image", compression="png",
            frame=[cmds.currentTime(query=True)], widthHeight=[width, height],
            percent=100, viewer=False, showOrnaments=False, forceOverwrite=True, offScreen=True,
        )
    except Exception as exc:
        raise CommandError("Viewport capture failed (needs Maya running with UI): {}".format(exc))
    files = sorted(glob.glob(base + "*.png"))
    if not files:
        raise CommandError("Viewport capture produced no image")
    try:
        with open(files[0], "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    finally:
        for f in files:
            with contextlib.suppress(OSError):
                os.remove(f)
        with contextlib.suppress(OSError):
            os.rmdir(tmp_dir)
    return {"format": "png", "width": width, "height": height, "data_base64": data}


def _limit(params, default=200):
    value = int(params.get("limit", default))
    if not 1 <= value <= 5000:
        raise CommandError("limit must be between 1 and 5000")
    return value


def _mesh(params):
    node = _require_node(params.get("name"))
    if cmds.nodeType(node) != "mesh":
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True, type="mesh") or []
        if len(shapes) != 1:
            raise CommandError("Specify a mesh shape or a transform with exactly one mesh shape")
        node = shapes[0]
    selection = om.MSelectionList()
    selection.add(node)
    return node, om.MFnMesh(selection.getDagPath(0))


def _space(params):
    space = params.get("space", "world")
    if space not in ("world", "object"):
        raise CommandError("space must be world or object")
    return om.MSpace.kWorld if space == "world" else om.MSpace.kObject


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise CommandError("Expected three finite numbers")
    try:
        values = [float(v) for v in value]
    except (TypeError, ValueError):
        raise CommandError("Expected three finite numbers")
    if not all(math.isfinite(v) for v in values):
        raise CommandError("Expected three finite numbers")
    return values


def handle_get_mesh_info(params):
    node, mesh = _mesh(params)
    return {"name": node, "vertices": mesh.numVertices, "edges": mesh.numEdges,
            "faces": mesh.numPolygons,
            "uv_sets": [{"name": name, "uv_count": mesh.numUVs(name)} for name in mesh.getUVSetNames()],
            "color_sets": list(mesh.getColorSetNames())}


def handle_get_mesh_vertices(params):
    limit = _limit(params)
    start = int(params.get("start", 0))
    if start < 0:
        raise CommandError("start must be non-negative")
    space = _space(params)
    node, mesh = _mesh(params)
    points = mesh.getPoints(space)
    return {"name": node, "space": params.get("space", "world"), "total": len(points),
            "start": start, "vertices": [{"index": i, "position": [points[i].x, points[i].y, points[i].z]}
                                         for i in range(start, min(start + limit, len(points)))]}


def handle_get_closest_point(params):
    point = _vector(params.get("point"))
    space = _space(params)
    node, mesh = _mesh(params)
    position, face = mesh.getClosestPoint(om.MPoint(*point), space)
    return {"name": node, "space": params.get("space", "world"),
            "position": [position.x, position.y, position.z], "face_index": face}


def handle_list_attributes(params):
    node = _require_node(params.get("name"))
    limit = _limit(params)
    options = {"keyable": True} if params.get("keyable_only", False) else {}
    attrs = cmds.listAttr(node, **options) or []
    entries = []
    for attr in attrs[:limit]:
        plug = node + "." + attr
        try:
            entries.append({"name": attr, "type": cmds.getAttr(plug, type=True),
                            "locked": cmds.getAttr(plug, lock=True),
                            "settable": cmds.getAttr(plug, settable=True)})
        except Exception as exc:
            entries.append({"name": attr, "error": str(exc)})
    return {"name": node, "total": len(attrs), "attributes": entries}


def handle_get_connections(params):
    node = _require_node(params.get("name"))
    direction = params.get("direction", "both")
    if direction not in ("incoming", "outgoing", "both"):
        raise CommandError("direction must be incoming, outgoing or both")
    limit = _limit(params)
    result = {}
    for key, source in (("incoming", True), ("outgoing", False)):
        if direction not in (key, "both"):
            continue
        pairs = cmds.listConnections(node, source=source, destination=not source,
                                     plugs=True, connections=True) or []
        result[key] = {"total": len(pairs) // 2,
                       "connections": [{"source": pairs[i + 1] if source else pairs[i],
                                        "destination": pairs[i] if source else pairs[i + 1]}
                                       for i in range(0, min(len(pairs), limit * 2), 2)]}
    return {"name": node, **result}


def handle_get_api_help(params):
    name = params.get("class_name", "")
    if not isinstance(name, str) or name.startswith("_") or not hasattr(om, name):
        raise CommandError("Unknown maya.api.OpenMaya class")
    cls = getattr(om, name)
    member = params.get("member", "")
    if member and (member.startswith("_") or not hasattr(cls, member)):
        raise CommandError("Unknown public API member")
    target = getattr(cls, member) if member else cls
    return {"class_name": name, "member": member, "help": _truncate(target.__doc__ or "No inline documentation"),
            "members": [n for n in dir(cls) if not n.startswith("_")] if not member else []}


def handle_set_transform(params):
    node = _require_node(params.get("name"))
    if not cmds.objectType(node, isAType="transform"):
        raise CommandError("Expected a transform node")
    _space(params)
    options = {key: _vector(params[key]) for key in ("translation", "rotation", "scale")
               if params.get(key) is not None}
    if not options:
        raise CommandError("Provide translation, rotation or scale")
    cmds.undoInfo(openChunk=True, chunkName="maya_mcp_set_transform")
    try:
        cmds.xform(node, worldSpace=params.get("space", "world") == "world", **options)
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"name": node, "space": params.get("space", "world"), "applied": options}


@contextlib.contextmanager
def _edit_chunk(label):
    cmds.undoInfo(openChunk=True, chunkName="maya_mcp_" + label)
    try:
        yield
    finally:
        cmds.undoInfo(closeChunk=True)


def _nodes(params):
    names = params.get("names")
    if not isinstance(names, list) or not 1 <= len(names) <= 200:
        raise CommandError("names must contain 1–200 explicit node names")
    return [_require_node(n) for n in names]


def _positive(value, label):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise CommandError(label + " must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise CommandError(label + " must be a positive finite number")
    return value


def handle_create_object(params):
    kind = params.get("kind", "cube")
    factories = {"cube": "polyCube", "sphere": "polySphere", "cylinder": "polyCylinder",
                 "cone": "polyCone", "plane": "polyPlane", "torus": "polyTorus",
                 "camera": "camera", "point_light": "pointLight", "directional_light": "directionalLight",
                 "spot_light": "spotLight", "area_light": "areaLight"}
    if kind not in factories:
        raise CommandError("Unsupported object kind. Choose: " + ", ".join(sorted(factories)))
    size = _positive(params.get("size", 1), "size")
    position = _vector(params.get("position", [0, 0, 0]))
    name = params.get("name", "")
    with _edit_chunk("create_object"):
        result = getattr(cmds, factories[kind])(**({"name": name} if name else {}))
        node = result[0] if isinstance(result, (list, tuple)) else result
        if cmds.nodeType(node) != "transform":
            node = cmds.listRelatives(node, parent=True, fullPath=True)[0]
        cmds.xform(node, worldSpace=True, translation=position)
        if kind not in ("camera", "point_light", "directional_light", "spot_light", "area_light"):
            cmds.xform(node, scale=[size] * 3)
        return {"name": _require_node(node), "kind": kind}


def _plug(params):
    node = _require_node(params.get("name"))
    attr = params.get("attribute")
    if not isinstance(attr, str) or not attr or "." in attr:
        raise CommandError("attribute must be a single attribute name (array indices allowed)")
    plug = node + "." + attr
    if not cmds.objExists(plug):
        raise CommandError("Attribute not found: " + plug)
    return plug


def handle_get_attribute(params):
    plug = _plug(params)
    return {"plug": plug, "type": cmds.getAttr(plug, type=True), "value": cmds.getAttr(plug)}


def handle_set_attribute(params):
    plug = _plug(params)
    kind = cmds.getAttr(plug, type=True)
    value = params.get("value")
    if not cmds.getAttr(plug, settable=True):
        raise CommandError("Attribute is locked or connected: " + plug)
    if kind == "string":
        if not isinstance(value, str):
            raise CommandError("Expected a string value")
        args, options = [value], {"type": "string"}
    elif kind in ("double3", "float3"):
        args, options = _vector(value), {"type": kind}
    elif kind in ("bool", "byte", "char", "short", "long", "enum", "float", "double", "doubleLinear", "doubleAngle", "time"):
        if not isinstance(value, (int, float, bool)) or not math.isfinite(value):
            raise CommandError("Expected a finite numeric value")
        if kind in ("bool", "byte", "char", "short", "long", "enum") and int(value) != value:
            raise CommandError("Expected an integer value")
        if kind == "bool" and value not in (0, 1):
            raise CommandError("Boolean value must be true/false or 0/1")
        args, options = [value], {}
    else:
        raise CommandError("Unsupported attribute type: " + kind)
    with _edit_chunk("set_attribute"):
        cmds.setAttr(plug, *args, **options)
    return handle_get_attribute(params)


def handle_select_objects(params):
    mode = params.get("mode", "replace")
    if mode not in ("replace", "add", "clear"):
        raise CommandError("mode must be replace, add or clear")
    nodes = [] if mode == "clear" else _nodes(params)
    with _edit_chunk("select"):
        if mode == "clear":
            cmds.select(clear=True)
        else:
            cmds.select(nodes, **{mode: True})
    return {"selection": cmds.ls(selection=True, long=True) or []}


def handle_frame_objects(params):
    nodes = _nodes(params)
    panel = params.get("panel") or cmds.getPanel(withFocus=True)
    if panel not in (cmds.getPanel(type="modelPanel") or []):
        raise CommandError("Specify an existing modelPanel (Maya UI required)")
    camera = cmds.modelPanel(panel, query=True, camera=True)
    with _edit_chunk("frame_objects"):
        cmds.viewFit(camera, nodes, animate=False)
    return {"panel": panel, "camera": camera, "framed": nodes}


def handle_create_curve(params):
    points = params.get("points")
    degree = params.get("degree", 1)
    if degree not in (1, 2, 3) or not isinstance(points, list) or not degree + 1 <= len(points) <= 5000:
        raise CommandError("degree must be 1–3; provide degree+1 to 5000 XYZ points")
    points = [_vector(p) for p in points]
    options = {"point": points, "degree": degree}
    if params.get("name"):
        options["name"] = params["name"]
    with _edit_chunk("create_curve"):
        node = cmds.curve(**options)
    return {"name": _require_node(node), "degree": degree, "point_count": len(points)}


def handle_create_material(params):
    kind = params.get("shader_type", "lambert")
    if kind not in ("lambert", "blinn", "phong"):
        raise CommandError("shader_type must be lambert, blinn or phong")
    color = _vector(params.get("color", [0.5, 0.5, 0.5]))
    if any(v < 0 or v > 1 for v in color):
        raise CommandError("color channels must be between 0 and 1")
    nodes = _nodes(params) if params.get("names") else []
    options = {"asShader": True}
    if params.get("name"):
        options["name"] = params["name"]
    with _edit_chunk("create_material"):
        material = cmds.shadingNode(kind, **options)
        engine = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=material + "SG")
        cmds.setAttr(material + ".color", *color, type="double3")
        cmds.connectAttr(material + ".outColor", engine + ".surfaceShader")
        if nodes:
            cmds.sets(nodes, edit=True, forceElement=engine)
    return {"material": material, "shading_engine": engine, "assigned": nodes}


def handle_organize_objects(params):
    nodes = _nodes(params)
    if len(set(nodes)) != len(nodes) or any(a != b and b.startswith(a + "|") for a in nodes for b in nodes):
        raise CommandError("Use distinct transforms without ancestor/descendant pairs")
    action = params.get("action", "group")
    if action not in ("group", "parent", "unparent"):
        raise CommandError("action must be group, parent or unparent")
    for node in nodes:
        if not cmds.objectType(node, isAType="transform"):
            raise CommandError("Organization requires transform nodes")
    parent = _require_node(params.get("parent")) if action == "parent" else None
    if parent and not cmds.objectType(parent, isAType="transform"):
        raise CommandError("Parent must be a transform")
    if parent and any(parent == n or parent.startswith(n + "|") for n in nodes):
        raise CommandError("Cannot parent a node to itself or its descendant")
    with _edit_chunk("organize"):
        if action == "group":
            result = cmds.group(nodes, **({"name": params["name"]} if params.get("name") else {}))
        elif action == "parent":
            result = cmds.parent(nodes, parent, absolute=True)
        else:
            result = cmds.parent(nodes, world=True, absolute=True)
    return {"action": action, "result": result}


def handle_mesh_operation(params):
    nodes = _nodes(params)
    operation = params.get("operation")
    if operation not in ("smooth", "triangulate", "combine"):
        raise CommandError("operation must be smooth, triangulate or combine")
    for node in nodes:
        _mesh({"name": node})
    if operation == "combine" and len(nodes) < 2:
        raise CommandError("combine requires at least two meshes")
    divisions = params.get("divisions", 1)
    if not isinstance(divisions, int) or not 1 <= divisions <= 3:
        raise CommandError("divisions must be an integer from 1 to 3")
    with _edit_chunk("mesh_operation"):
        if operation == "smooth":
            result = cmds.polySmooth(nodes, divisions=divisions, constructionHistory=True)
        elif operation == "triangulate":
            result = cmds.polyTriangulate(nodes, constructionHistory=True)
        else:
            result = cmds.polyUnite(nodes, constructionHistory=True)
    return {"operation": operation, "result": result}


HANDLERS = {
    "create_object": handle_create_object,
    "get_attribute": handle_get_attribute,
    "set_attribute": handle_set_attribute,
    "select_objects": handle_select_objects,
    "frame_objects": handle_frame_objects,
    "create_curve": handle_create_curve,
    "create_material": handle_create_material,
    "organize_objects": handle_organize_objects,
    "mesh_operation": handle_mesh_operation,
    "get_mesh_info": handle_get_mesh_info,
    "get_mesh_vertices": handle_get_mesh_vertices,
    "get_closest_point": handle_get_closest_point,
    "list_attributes": handle_list_attributes,
    "get_connections": handle_get_connections,
    "get_api_help": handle_get_api_help,
    "set_transform": handle_set_transform,
    "ping": handle_ping,
    "get_scene_info": handle_get_scene_info,
    "list_objects": handle_list_objects,
    "get_selection": handle_get_selection,
    "get_object_info": handle_get_object_info,
    "get_scene_graph": handle_get_scene_graph,
    "execute_python": handle_execute_python,
    "undo": handle_undo,
    "search_commands": handle_search_commands,
    "get_command_help": handle_get_command_help,
    "get_viewport_screenshot": handle_get_viewport_screenshot,
}


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

def _run_on_main_thread(func, params):
    return maya.utils.executeInMainThreadWithResult(func, params)


class BridgeServer(object):
    def __init__(self, host=HOST, port=DEFAULT_PORT, token=None):
        self.host, self.port, self.token = host, port, token
        self._sock = None
        self._thread = None
        self._running = threading.Event()

    @property
    def running(self):
        return self._running.is_set()

    def start(self):
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(5)
        sock.settimeout(1.0)
        self._sock = sock
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop, name="maya-mcp-accept", daemon=True)
        self._thread.start()
        om.MGlobal.displayInfo("[maya-mcp] listening on {}:{}".format(self.host, self.port))

    def stop(self):
        self._running.clear()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        om.MGlobal.displayInfo("[maya-mcp] stopped")

    def _accept_loop(self):
        while self._running.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    def _client_loop(self, client):
        buffer = b""
        with client:
            client.settimeout(None)
            while self._running.is_set():
                try:
                    chunk = client.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_REQUEST_BYTES:
                    return
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self.dispatch(line)
                    try:
                        client.sendall(json.dumps(response).encode("utf-8") + b"\n")
                    except OSError:
                        return

    def dispatch(self, raw):
        request_id = None
        try:
            request = json.loads(raw.decode("utf-8"))
            request_id = request.get("id")
            if self.token and not hmac.compare_digest(str(request.get("token") or ""), self.token):
                return {"id": request_id, "status": "error", "message": "Unauthorized: bad or missing MAYA_MCP_TOKEN"}
            handler = HANDLERS.get(request.get("type"))
            if handler is None:
                return {"id": request_id, "status": "error",
                        "message": "Unknown command {!r}. Available: {}".format(request.get("type"), sorted(HANDLERS))}
            result = _run_on_main_thread(handler, request.get("params") or {})
            return {"id": request_id, "status": "ok", "result": result}
        except CommandError as exc:
            return {"id": request_id, "status": "error", "message": str(exc)}
        except Exception:
            return {"id": request_id, "status": "error", "message": traceback.format_exc(limit=-3)}


_server = None


def start(port=None):
    """Start the bridge (also called automatically when the plug-in loads)."""
    global _server
    if _server is None or not _server.running:
        _server = BridgeServer(
            port=int(port or os.environ.get("MAYA_MCP_PORT", DEFAULT_PORT)),
            token=os.environ.get("MAYA_MCP_TOKEN") or None,
        )
        _server.start()
    return _server


def stop():
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def initializePlugin(plugin):  # noqa: N802 - Maya naming
    start()


def uninitializePlugin(plugin):  # noqa: N802 - Maya naming
    stop()
