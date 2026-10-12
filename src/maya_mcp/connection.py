"""Client side of the Maya bridge: one short-lived TCP connection per request."""

import json
import os
import socket
import uuid

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TIMEOUT = 60.0


class MayaError(RuntimeError):
    """Raised when Maya is unreachable or returns an error."""


def send_command(command_type, params=None, *, host=None, port=None, timeout=None, token=None):
    host = host or os.environ.get("MAYA_MCP_HOST", DEFAULT_HOST)
    port = int(port or os.environ.get("MAYA_MCP_PORT", DEFAULT_PORT))
    timeout = float(timeout or os.environ.get("MAYA_MCP_TIMEOUT", DEFAULT_TIMEOUT))
    token = token if token is not None else os.environ.get("MAYA_MCP_TOKEN")

    request = {"id": uuid.uuid4().hex, "type": command_type, "params": params or {}}
    if token:
        request["token"] = token

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    raise MayaError("Maya closed the connection before replying")
                buffer += chunk
    except ConnectionRefusedError:
        raise MayaError(
            "Could not connect to Maya on {}:{}. Is Maya running with maya_mcp_plugin.py loaded?".format(host, port)
        )
    except socket.timeout:
        raise MayaError(
            "Timed out after {}s waiting for Maya (it may be busy with a long operation).".format(timeout)
        )
    except OSError as exc:
        raise MayaError("Socket error talking to Maya at {}:{}: {}".format(host, port, exc))

    response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
    if response.get("status") != "ok":
        raise MayaError(response.get("message", "Unknown error from Maya"))
    return response.get("result")
