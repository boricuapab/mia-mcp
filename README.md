# maya-mcp

An MCP server for **Autodesk Maya**, modelled on the [Blender MCP server](https://www.blender.org/lab/mcp-server/). It lets Claude, Codex, or any other MCP client inspect your scene, answer questions about it, and edit it through Maya's Python API.

> **Security warning.** `execute_python` runs LLM-generated code inside Maya with your user's permissions, with no sandbox. It can delete files or send data anywhere. Use a VM or a machine without sensitive data, or set `MAYA_MCP_READONLY=1` (see below) when you only want inspection.

## How it works

```
 MCP client (Claude / Codex)  <--stdio-->  maya-mcp server  <--TCP 127.0.0.1:8675-->  Maya plug-in
                                           (src/maya_mcp)                              (maya_plugin/maya_mcp_plugin.py)
```

Same shape as the Blender setup (add-on + MCP server + client):

1. **Maya plug-in** (`maya_plugin/maya_mcp_plugin.py`) runs a localhost socket server inside Maya. Every command is marshalled onto Maya's main thread, so `maya.cmds` is safe to call.
2. **MCP server** (`maya-mcp`) speaks MCP over stdio and forwards tool calls to the plug-in.
3. **Client** is Claude Code, Claude Desktop, Codex CLI, or anything that supports MCP.

## Requirements

- Maya 2022 or newer (Python 3)
- Python 3.10+ for the MCP server (`mcp` SDK v1 or v2 both work)
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`

## 1. Install the Maya plug-in

Pick one:

- **Plug-in + custom shelf (recommended):** drag `install.py` from this checkout onto
  Maya's viewport. It installs the plug-in, the supplied start-server icon, and a
  persistent **miamcp** shelf. Click **Start Maya MCP** to start the bridge. Running
  the installer again updates the existing server button without removing other
  shelf buttons. The button uses Maya's plug-in search path, so no user-specific
  paths need editing. Keep `install.py`, `maya_plugin/`, and `icons/` together.
  The **mia-mcp** menu provides **Start Server**, **Server Log and Stats**, and
  **Load / Auto load Preferences**. The shelf and menu use the same bridge.
  The status window refreshes every second with endpoint, uptime, request/error
  counts, request duration, and the most recent 200 request log entries.
  In Maya's Plug-in Manager, find `maya_mcp_plugin.py` and use **Loaded** and
  **Auto load** to control loading and startup. To update an already-loaded
  plug-in after reinstalling, unload and reload it in Plug-in Manager.
  Alternatively, run in Maya's Python Script Editor:
  ```python
  import runpy
  runpy.run_path("/absolute/path/to/mia-mcp/install.py", run_name="__main__")
  ```
- **Load once:** in Maya's Script Editor (Python tab):
  ```python
  import maya.cmds as cmds
  cmds.loadPlugin("/absolute/path/to/maya-mcp/maya_plugin/maya_mcp_plugin.py")
  ```
- **Permanent:** copy `maya_mcp_plugin.py` into a folder on `MAYA_PLUG_IN_PATH` (e.g. `~/maya/<version>/plug-ins/`), then enable it in **Windows > Settings/Preferences > Plug-in Manager** and tick **Auto load**.

The Script Editor prints `[maya-mcp] listening on 127.0.0.1:8675`. Unloading the plug-in stops the server.

## 2. Install the MCP server

```bash
cd maya-mcp
pip install -e .          # or run it ad hoc with: uvx --from /absolute/path/to/maya-mcp maya-mcp
```

## 3. Connect your client

Replace `/absolute/path/to/maya-mcp` with your checkout path.

**Claude Code**

```bash
claude mcp add maya -- uvx --from /absolute/path/to/maya-mcp maya-mcp
```

**Claude Desktop**: add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maya": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/maya-mcp", "maya-mcp"]
    }
  }
}
```

**Codex CLI**: either

```bash
codex mcp add maya -- uvx --from /absolute/path/to/maya-mcp maya-mcp
```

or add to `~/.codex/config.toml`:

```toml
[mcp_servers.maya]
command = "uvx"
args = ["--from", "/absolute/path/to/maya-mcp", "maya-mcp"]
```

If you used `pip install -e .` instead of `uvx`, use `command = "maya-mcp"` with no args.

Start Maya (plug-in loaded) first, then your client.

## Documentation

See the [tool reference](docs/tools.md) for available functions, supported parameters, examples, and limits.

## Configuration (environment variables)

Set these in the MCP client's `env` for the server, and (for `MAYA_MCP_PORT` / `MAYA_MCP_TOKEN`) in Maya's environment before it starts.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAYA_MCP_PORT` | `8675` | Bridge port |
| `MAYA_MCP_HOST` | `127.0.0.1` | Where the server looks for Maya (the plug-in only ever binds to localhost) |
| `MAYA_MCP_TOKEN` | unset | Shared secret; if set in Maya, requests without it are rejected |
| `MAYA_MCP_TIMEOUT` | `60` | Seconds to wait for Maya to answer |
| `MAYA_MCP_READONLY` | unset | `1` hides `execute_python` and `undo`, leaving inspection tools only |

Example with a token in Codex:

```toml
[mcp_servers.maya]
command = "uvx"
args = ["--from", "/absolute/path/to/maya-mcp", "maya-mcp"]
env = { MAYA_MCP_TOKEN = "change-me" }
```

## Example prompts

- "Analyze the scene and list the objects with the highest face count that are smallest on screen."
- "Which meshes use the material `lambert3`?"
- "Rename everything to follow `GRP_/GEO_/LGT_` naming and report what changed."
- "Find meshes with non-frozen transforms or non-uniform scale."
- "My skinned mesh isn't deforming with its skeleton. Inspect the history and tell me why."
- "Check this checklist: all meshes have materials, no n-gons, no duplicate names."

Results depend heavily on the model you use. Review what it did and use `undo` if a change is wrong.

## Troubleshooting

- **"Could not connect to Maya"**: Maya isn't running, the plug-in isn't loaded, or the ports differ.
- **Timeouts**: Maya is busy (render, long script). Raise `MAYA_MCP_TIMEOUT`.
- **Screenshot fails**: needs an interactive Maya session; it does not work in `mayapy`/batch mode.
- **Port already in use**: set a different `MAYA_MCP_PORT` for both Maya and the server.

## Development

```bash
pip install -e .
python tests/test_bridge.py   # runs the plug-in against a stubbed `maya` module and drives the MCP server over stdio
```

The tests don't need Maya. They exercise the protocol, auth token, error handling and MCP tool listing, but not real `maya.cmds` behavior, so it's worth trying each tool once in a real Maya session.
