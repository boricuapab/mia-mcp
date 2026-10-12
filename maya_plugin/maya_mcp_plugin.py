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

__version__ = "0.6.0"

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


def _joint(name):
    node = _require_node(name)
    if cmds.nodeType(node) != "joint":
        raise CommandError("Expected a joint: " + node)
    return node


def handle_get_joint_info(params):
    node = _joint(params.get("name"))
    return {"name": node, "parent": cmds.listRelatives(node, parent=True, fullPath=True) or [],
            "children": cmds.listRelatives(node, children=True, type="joint", fullPath=True) or [],
            "world_position": cmds.xform(node, query=True, worldSpace=True, translation=True),
            "joint_orient": cmds.getAttr(node + ".jointOrient"),
            "rotation_order": cmds.getAttr(node + ".rotateOrder"),
            "radius": cmds.getAttr(node + ".radius")}


def handle_create_joint_chain(params):
    positions = params.get("positions")
    if not isinstance(positions, list) or not 1 <= len(positions) <= 200:
        raise CommandError("Provide 1–200 world-space positions")
    positions = [_vector(p) for p in positions]
    names = params.get("names") or []
    if not isinstance(names, list) or (names and (len(names) != len(positions) or any(not isinstance(n, str) or not n for n in names))):
        raise CommandError("names must contain one non-empty name per position")
    radius = _positive(params.get("radius", 1), "radius")
    parent = _joint(params["parent"]) if params.get("parent") else None
    previous_selection = cmds.ls(selection=True, long=True) or []
    joints = []
    with _edit_chunk("joint_chain"):
        try:
            cmds.select(clear=True)
            for i, position in enumerate(positions):
                # Explicit parenting avoids Maya joint()'s implicit selection parenting.
                node = cmds.createNode("joint", **({"name": names[i]} if names else {}),
                                       **({"parent": parent} if parent else {}))
                node = _require_node(node)
                cmds.xform(node, worldSpace=True, translation=position)
                cmds.setAttr(node + ".radius", radius)
                joints.append(node)
                parent = node
        finally:
            cmds.select(previous_selection, replace=True) if previous_selection else cmds.select(clear=True)
    return {"joints": joints}


def handle_create_constraint(params):
    kind = params.get("kind", "parent")
    commands = {"parent": "parentConstraint", "point": "pointConstraint", "orient": "orientConstraint",
                "scale": "scaleConstraint", "pole_vector": "poleVectorConstraint"}
    if kind not in commands:
        raise CommandError("kind must be parent, point, orient, scale or pole_vector")
    targets = _nodes({"names": params.get("targets")})
    driven = _require_node(params.get("driven"))
    if driven in targets:
        raise CommandError("Driven node cannot also be a target")
    if kind == "pole_vector":
        if cmds.nodeType(driven) != "ikHandle":
            raise CommandError("pole_vector requires an IK handle")
    elif not cmds.objectType(driven, isAType="transform"):
        raise CommandError("Driven node must be a transform")
    if any(not cmds.objectType(n, isAType="transform") for n in targets):
        raise CommandError("Targets must be transforms")
    options = {} if kind == "pole_vector" else {"maintainOffset": params.get("maintain_offset", True)}
    if params.get("name"):
        options["name"] = params["name"]
    with _edit_chunk("constraint"):
        result = getattr(cmds, commands[kind])(targets, driven, **options)
    return {"kind": kind, "constraints": result, "targets": targets, "driven": driven}


def handle_create_ik_handle(params):
    start = _joint(params.get("start_joint"))
    end = _joint(params.get("end_joint"))
    solver = params.get("solver", "ikRPsolver")
    if solver not in ("ikRPsolver", "ikSCsolver"):
        raise CommandError("solver must be ikRPsolver or ikSCsolver")
    descendants = cmds.listRelatives(start, allDescendents=True, fullPath=True, type="joint") or []
    if start == end or end not in descendants:
        raise CommandError("end_joint must be a descendant of start_joint")
    options = {"startJoint": start, "endEffector": end, "solver": solver}
    if params.get("name"):
        options["name"] = params["name"]
    with _edit_chunk("ik_handle"):
        result = cmds.ikHandle(**options)
    return {"handle": result[0], "effector": result[1], "solver": solver}


def _skin(params):
    node = _require_node(params.get("skin_cluster"))
    if cmds.nodeType(node) != "skinCluster":
        raise CommandError("Expected a skinCluster")
    # Import lazily so non-rigging commands do not need OpenMayaAnim.
    import maya.api.OpenMayaAnim as oma
    selection = om.MSelectionList()
    selection.add(node)
    return node, oma.MFnSkinCluster(selection.getDependNode(0))


def handle_get_skin_info(params):
    node, skin = _skin(params)
    return {"skin_cluster": node,
            "influences": [{"name": p.fullPathName(), "logical_index": skin.indexForInfluenceObject(p)}
                           for p in skin.influenceObjects()],
            "geometry": cmds.skinCluster(node, query=True, geometry=True) or [],
            "skinning_method": cmds.getAttr(node + ".skinningMethod"),
            "normalize_weights": cmds.getAttr(node + ".normalizeWeights")}


def handle_get_skin_weights(params):
    limit = _limit(params, 20)
    if limit > 200:
        raise CommandError("Skin weight limit must be 1–200 vertices")
    start = int(params.get("start", 0))
    if start < 0:
        raise CommandError("start must be non-negative")
    node, skin = _skin(params)
    # API geometry path ensures queries use the skinCluster's actual output geometry.
    if skin.numOutputConnections() != 1:
        raise CommandError("Skin weight queries require exactly one connected output geometry")
    path = skin.getPathAtIndex(skin.indexForOutputConnection(0))
    if cmds.nodeType(path.fullPathName()) != "mesh":
        raise CommandError("Skin weight queries currently support polygon meshes only")
    mesh = om.MFnMesh(path)
    ids = list(range(start, min(start + limit, mesh.numVertices)))
    influences = [p.fullPathName() for p in skin.influenceObjects()]
    if len(ids) * len(influences) > 20000:
        raise CommandError("Too many weights; reduce limit (maximum 20000 values)")
    vertices = []
    if ids:
        components = om.MFnSingleIndexedComponent()
        obj = components.create(om.MFn.kMeshVertComponent)
        components.addElements(ids)
        weights, count = skin.getWeights(path, obj)
        for offset, vertex in enumerate(ids):
            vertices.append({"index": vertex, "weights": list(weights[offset * count:(offset + 1) * count])})
    return {"skin_cluster": node, "geometry": path.fullPathName(), "total_vertices": mesh.numVertices,
            "influences": influences, "vertices": vertices}


def handle_bind_skin(params):
    mesh, _ = _mesh({"name": params.get("mesh")})
    joints = [_joint(n) for n in _nodes({"names": params.get("joints")})]
    if len(set(joints)) != len(joints):
        raise CommandError("Provide distinct joints")
    if any(cmds.nodeType(n) == "skinCluster" for n in (cmds.listHistory(mesh) or [])):
        raise CommandError("Mesh already has a skinCluster")
    maximum = params.get("max_influences", 4)
    method = params.get("skinning_method", 0)
    if not isinstance(maximum, int) or not 1 <= maximum <= 20 or method not in (0, 1, 2):
        raise CommandError("max_influences must be 1–20; skinning_method must be 0, 1 or 2")
    options = {"toSelectedBones": True, "maximumInfluences": maximum,
               "obeyMaxInfluences": True, "normalizeWeights": 1, "skinMethod": method}
    if params.get("name"):
        options["name"] = params["name"]
    with _edit_chunk("bind_skin"):
        result = cmds.skinCluster(joints, mesh, **options)
    return {"skin_cluster": result[0], "mesh": mesh, "joints": joints}


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommandError(label + " must be a finite number")
    return float(value)


def _time_range(params):
    start = _finite(params.get("start"), "start")
    end = _finite(params.get("end"), "end")
    if start > end:
        raise CommandError("start must not exceed end")
    return start, end


def _animation_plugs(params):
    node = _require_node(params.get("name"))
    attrs = params.get("attributes")
    if not isinstance(attrs, list) or not 1 <= len(attrs) <= 60:
        raise CommandError("Provide 1–60 explicit attributes")
    plugs = [_plug({"name": node, "attribute": attr}) for attr in attrs]
    if len(set(plugs)) != len(plugs):
        raise CommandError("Provide distinct attributes")
    return plugs


def handle_get_timeline(params):
    return {"current_time": cmds.currentTime(query=True), "time_unit": cmds.currentUnit(query=True, time=True),
            "playback_start": cmds.playbackOptions(query=True, minTime=True),
            "playback_end": cmds.playbackOptions(query=True, maxTime=True),
            "animation_start": cmds.playbackOptions(query=True, animationStartTime=True),
            "animation_end": cmds.playbackOptions(query=True, animationEndTime=True)}


def handle_set_timeline(params):
    current = _finite(params["current_time"], "current_time") if params.get("current_time") is not None else None
    options = {}
    if params.get("start") is not None or params.get("end") is not None:
        start, end = _time_range(params)
        options = {"minTime": start, "maxTime": end}
    if current is None and not options:
        raise CommandError("Provide current_time or both start and end")
    with _edit_chunk("timeline"):
        if options:
            cmds.playbackOptions(**options)
        if current is not None:
            cmds.currentTime(current, edit=True)
    return handle_get_timeline({})


def handle_set_keyframes(params):
    plugs = _animation_plugs(params)
    times = params.get("times")
    if not isinstance(times, list) or not 1 <= len(times) <= 200:
        raise CommandError("Provide 1–200 times")
    times = [_finite(t, "time") for t in times]
    options = {"time": times, "breakdown": params.get("breakdown", False)}
    if params.get("layer"):
        layer = _typed_node(params["layer"], "animLayer")
        if cmds.animLayer(layer, query=True, lock=True):
            raise CommandError("Animation layer is locked")
        members = {_canonical_plug(p) for p in (cmds.animLayer(layer, query=True, attribute=True) or [])}
        if any(_canonical_plug(p) not in members for p in plugs):
            raise CommandError("All keyed attributes must already belong to the requested layer")
        options["animLayer"] = layer
    if params.get("value") is not None:
        if len(plugs) != 1:
            raise CommandError("An explicit value requires exactly one attribute")
        options["value"] = _finite(params["value"], "value")
    for plug in plugs:
        if cmds.getAttr(plug, lock=True):
            raise CommandError("Attribute is locked: " + plug)
        if cmds.getAttr(plug, type=True) not in ("bool", "byte", "short", "long", "enum", "float", "double", "doubleLinear", "doubleAngle", "time"):
            raise CommandError("Only scalar numeric attributes can be keyed: " + plug)
    with _edit_chunk("set_keyframes"):
        count = cmds.setKeyframe(plugs, **options)
    return {"key_count": count, "plugs": plugs, "times": times}


def _anim_curve(params):
    node = _require_node(params.get("curve"))
    if not cmds.nodeType(node).startswith("animCurve"):
        raise CommandError("Expected an animation curve node")
    import maya.api.OpenMayaAnim as oma
    selection = om.MSelectionList()
    selection.add(node)
    return node, oma.MFnAnimCurve(selection.getDependNode(0))


def handle_get_animation_curve(params):
    limit = _limit(params)
    start = int(params.get("start", 0))
    if start < 0:
        raise CommandError("start must be non-negative")
    node, curve = _anim_curve(params)
    if curve.animCurveType in (2, 6):  # kAnimCurveTT / kAnimCurveUT: value() cannot read time outputs.
        raise CommandError("Time-output animation curves are not supported; use execute_python for TT/UT curves")
    keys = []
    for i in range(start, min(start + limit, curve.numKeys)):
        value = curve.input(i)
        keys.append({"index": i, "input": value.asUnits(om.MTime.uiUnit()) if curve.isTimeInput else value,
                     "value": curve.value(i), "in_tangent": curve.inTangentType(i),
                     "out_tangent": curve.outTangentType(i), "breakdown": curve.isBreakdown(i)})
    return {"curve": node, "curve_type": curve.animCurveType, "time_input": curve.isTimeInput,
            "total_keys": curve.numKeys, "weighted": curve.isWeighted,
            "pre_infinity": curve.preInfinityType, "post_infinity": curve.postInfinityType, "keys": keys,
            "value_units": "API internal units: radians for angles, centimeters for distances, seconds for time outputs"}


def handle_delete_keyframes(params):
    plugs = _animation_plugs(params)
    time = _time_range(params)
    with _edit_chunk("delete_keys"):
        cmds.cutKey(plugs, time=time, option="keys", clear=True, animation="objects")
    return {"plugs": plugs, "time_range": list(time)}


def handle_set_key_tangents(params):
    plugs = _animation_plugs(params)
    time = _time_range(params)
    incoming = params.get("in_tangent", "auto")
    outgoing = params.get("out_tangent", "auto")
    common = {"auto", "linear", "flat", "spline", "clamped", "plateau"}
    if incoming not in common or outgoing not in common | {"step", "stepnext"}:
        raise CommandError("Unsupported tangent type; step/stepnext are outgoing only")
    with _edit_chunk("key_tangents"):
        cmds.keyTangent(plugs, edit=True, time=time, inTangentType=incoming, outTangentType=outgoing, animation="objects")
    return {"plugs": plugs, "time_range": list(time), "in_tangent": incoming, "out_tangent": outgoing}


def handle_set_curve_infinity(params):
    plugs = _animation_plugs(params)
    pre, post = params.get("pre", "constant"), params.get("post", "constant")
    allowed = {"constant", "linear", "cycle", "cycleRelative", "oscillate"}
    if pre not in allowed or post not in allowed:
        raise CommandError("Infinity must be constant, linear, cycle, cycleRelative or oscillate")
    with _edit_chunk("infinity"):
        cmds.setInfinity(plugs, preInfinite=pre, postInfinite=post)
    return {"plugs": plugs, "pre": pre, "post": post}


def handle_bake_animation(params):
    plugs = _animation_plugs(params)
    time = _time_range(params)
    step = _positive(params.get("sample_by", 1), "sample_by")
    if ((time[1] - time[0]) / step + 1) * len(plugs) > 20000:
        raise CommandError("Bake exceeds 20000 channel samples; reduce range or increase sample_by")
    previous = cmds.currentTime(query=True)
    with _edit_chunk("bake_animation"):
        try:
            count = cmds.bakeResults(plugs, time=time, sampleBy=step, simulation=params.get("simulation", True),
                                     preserveOutsideKeys=True, sparseAnimCurveBake=False,
                                     disableImplicitControl=False, animation="objects")
        finally:
            cmds.currentTime(previous, edit=True)
    return {"channels_baked": count, "plugs": plugs, "time_range": list(time), "sample_by": step}


def _typed_node(name, kind):
    node = _require_node(name)
    if cmds.nodeType(node) != kind:
        raise CommandError("Expected a " + kind + " node")
    return node


def _canonical_plug(plug):
    # Layer membership may use short node names; resolve both sides before comparison.
    node, attribute = plug.split(".", 1)
    full = _require_node(node)
    return full + "." + cmds.attributeName(full + "." + attribute, long=True, leaf=False)


def handle_get_animation_layer(params):
    layer = _typed_node(params.get("layer"), "animLayer")
    result = {"layer": layer}
    for flag in ("weight", "mute", "solo", "lock", "override", "parent", "children", "attribute", "animCurves"):
        result[flag] = cmds.animLayer(layer, query=True, **{flag: True})
    return result


def handle_create_animation_layer(params):
    plugs = _animation_plugs(params)
    name = params.get("layer")
    if not isinstance(name, str) or not name or cmds.objExists(name):
        raise CommandError("Provide a new non-empty layer name")
    with _edit_chunk("create_animation_layer"):
        layer = cmds.animLayer(name, override=params.get("override", False))
        cmds.animLayer(layer, edit=True, attribute=plugs)
    return {"layer": layer, "attributes": plugs}


def handle_edit_animation_layer(params):
    layer = _typed_node(params.get("layer"), "animLayer")
    options = {}
    for flag in ("mute", "solo", "lock"):
        if params.get(flag) is not None:
            if not isinstance(params[flag], bool):
                raise CommandError(flag + " must be boolean")
            options[flag] = params[flag]
    if params.get("weight") is not None:
        weight = _finite(params["weight"], "weight")
        if not 0 <= weight <= 1:
            raise CommandError("weight must be between 0 and 1")
        options["weight"] = weight
    if not options:
        raise CommandError("Provide weight, mute, solo or lock")
    with _edit_chunk("edit_animation_layer"):
        cmds.animLayer(layer, edit=True, **options)
    return {"layer": layer, "applied": options}


_POSE_TYPES = {"bool", "byte", "short", "long", "enum", "float", "double", "doubleLinear", "doubleAngle", "time"}


def handle_capture_pose(params):
    nodes = _nodes(params)
    attributes = params.get("attributes")
    channels = []
    for node in nodes:
        attrs = attributes if attributes is not None else (cmds.listAttr(node, keyable=True, scalar=True) or [])
        if not isinstance(attrs, list) or len(attrs) > 60:
            raise CommandError("At most 60 attributes per node; specify attributes for larger controls")
        for attr in attrs:
            plug = _plug({"name": node, "attribute": attr})
            kind = cmds.getAttr(plug, type=True)
            if kind in _POSE_TYPES:
                channels.append({"name": node, "attribute": attr, "type": kind, "value": cmds.getAttr(plug)})
    if len(channels) > 2000:
        raise CommandError("Pose exceeds 2000 channels; reduce nodes/attributes")
    return {"linear_unit": cmds.currentUnit(query=True, linear=True),
            "angular_unit": cmds.currentUnit(query=True, angle=True),
            "time_unit": cmds.currentUnit(query=True, time=True), "channels": channels}


def handle_apply_pose(params):
    pose = params.get("pose")
    if not isinstance(pose, dict) or not isinstance(pose.get("channels"), list) or not 1 <= len(pose["channels"]) <= 2000:
        raise CommandError("Provide a captured pose with 1–2000 channels")
    for key, flag in (("linear_unit", "linear"), ("angular_unit", "angle"), ("time_unit", "time")):
        if pose.get(key) != cmds.currentUnit(query=True, **{flag: True}):
            raise CommandError("Pose units differ from scene units: " + key)
    validated, seen = [], set()
    for entry in pose["channels"]:
        if not isinstance(entry, dict):
            raise CommandError("Invalid pose channel")
        plug = _plug(entry)
        kind = cmds.getAttr(plug, type=True)
        if kind not in _POSE_TYPES or kind != entry.get("type") or not cmds.getAttr(plug, settable=True):
            raise CommandError("Pose channel is incompatible, locked or connected: " + plug)
        value = _finite(entry.get("value"), "pose value") if kind != "bool" else entry.get("value")
        if kind == "bool" and (not isinstance(value, (bool, int)) or value not in (0, 1)):
            raise CommandError("Invalid boolean pose value")
        if kind in ("byte", "short", "long", "enum") and int(value) != value:
            raise CommandError("Pose integer channel requires an integer")
        if plug in seen:
            raise CommandError("Duplicate pose channel: " + plug)
        seen.add(plug)
        validated.append((plug, value))
    with _edit_chunk("apply_pose"):
        for plug, value in validated:
            cmds.setAttr(plug, value)
    return {"applied_channels": len(validated)}


def handle_get_blend_shape_info(params):
    node = _typed_node(params.get("blend_shape"), "blendShape")
    limit = _limit(params)
    indices = cmds.getAttr(node + ".weight", multiIndices=True) or []
    aliases = cmds.aliasAttr(node, query=True) or []
    mapping = {aliases[i + 1]: aliases[i] for i in range(0, len(aliases), 2)}
    return {"blend_shape": node, "total_targets": len(indices),
            "geometry": cmds.blendShape(node, query=True, geometry=True) or [],
            "targets": [{"index": i, "alias": mapping.get("weight[{}]".format(i)),
                         "weight": cmds.getAttr(node + ".weight[{}]".format(i))} for i in indices[:limit]]}


def handle_set_blend_shape_weights(params):
    node = _typed_node(params.get("blend_shape"), "blendShape")
    weights = params.get("weights")
    if not isinstance(weights, dict) or not 1 <= len(weights) <= 200:
        raise CommandError("Provide 1–200 weights keyed by numeric target index")
    indices = set(cmds.getAttr(node + ".weight", multiIndices=True) or [])
    validated = []
    for key, value in weights.items():
        if not isinstance(key, str) or not key.isdecimal() or int(key) not in indices:
            raise CommandError("Unknown blend shape target index: " + str(key))
        value = _finite(value, "weight")
        if not 0 <= value <= 1:
            raise CommandError("Blend shape weight must be between 0 and 1")
        plug = node + ".weight[{}]".format(int(key))
        if not cmds.getAttr(plug, settable=True):
            raise CommandError("Target weight is locked or connected: " + plug)
        if any(p == plug for p, _ in validated):
            raise CommandError("Duplicate target index")
        validated.append((plug, value))
    with _edit_chunk("blend_shape_weights"):
        for plug, value in validated:
            cmds.setAttr(plug, value)
    return {"blend_shape": node, "weights": weights}


def handle_create_character_set(params):
    plugs = _animation_plugs(params)
    name = params.get("character")
    if not isinstance(name, str) or not name or cmds.objExists(name):
        raise CommandError("Provide a new non-empty character set name")
    with _edit_chunk("character_set"):
        result = cmds.character(plugs, name=name)
    return {"character": result, "attributes": plugs}


def handle_get_character_set(params):
    node = _typed_node(params.get("character"), "character")
    members = cmds.character(node, query=True) or []
    return {"character": node, "total_members": len(members), "members": members[:_limit(params)]}


HANDLERS = {
    "get_animation_layer": handle_get_animation_layer,
    "create_animation_layer": handle_create_animation_layer,
    "edit_animation_layer": handle_edit_animation_layer,
    "capture_pose": handle_capture_pose,
    "apply_pose": handle_apply_pose,
    "get_blend_shape_info": handle_get_blend_shape_info,
    "set_blend_shape_weights": handle_set_blend_shape_weights,
    "create_character_set": handle_create_character_set,
    "get_character_set": handle_get_character_set,
    "get_timeline": handle_get_timeline,
    "set_timeline": handle_set_timeline,
    "set_keyframes": handle_set_keyframes,
    "get_animation_curve": handle_get_animation_curve,
    "delete_keyframes": handle_delete_keyframes,
    "set_key_tangents": handle_set_key_tangents,
    "set_curve_infinity": handle_set_curve_infinity,
    "bake_animation": handle_bake_animation,
    "get_joint_info": handle_get_joint_info,
    "create_joint_chain": handle_create_joint_chain,
    "create_constraint": handle_create_constraint,
    "create_ik_handle": handle_create_ik_handle,
    "get_skin_info": handle_get_skin_info,
    "get_skin_weights": handle_get_skin_weights,
    "bind_skin": handle_bind_skin,
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
