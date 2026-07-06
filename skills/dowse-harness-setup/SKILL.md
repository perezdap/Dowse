---
name: dowse-harness-setup
description: Configure dowse MCP and CLI access across common coding harnesses. Use when wiring dowse into Pi, Cursor, Claude Code, Claude Desktop, VS Code/Copilot, Windsurf, or when checking whether .mcp.json is enough for a harness.
---

# dowse harness setup

Use this when a repo already uses `dowse` or the user asks to wire Dowse into one or more coding harnesses.

Rule of thumb: `dowse query` works anywhere an agent can run shell commands. MCP support is per harness. Do not assume every harness reads a repo-level `.mcp.json`.

## First checks

Run these from the repo root:

```powershell
dowse --help
dowse doctor --root .
dowse status --root .
```

If `status.stale` is `true`, refresh before testing MCP or query behavior:

```powershell
dowse index . --db .\.dowse_index
```

Smoke test the index:

```powershell
dowse query "main index and query orchestration" --db .\.dowse_index -n 3
```

For MCP, the `mcp` extra must be installed in the environment that provides the `dowse` command:

```powershell
pipx install "dowse-context[mcp,all-langs]"
# or, in a dev checkout:
pip install -e ".[dev,mcp]"
```

## Common server entry

Use this stdio server entry unless a harness needs a different wrapper:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", ".dowse_index"]
    }
  }
}
```

For global app configs, prefer an absolute database path so the server does not depend on launch directory:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", "C:\\path\\to\\repo\\.dowse_index"]
    }
  }
}
```

For repo-local configs, relative `".dowse_index"` is fine and is better for separate git worktrees.

## Harness matrix

| Harness | Config path | Root key | Notes |
|---|---|---|---|
| Pi with `pi-mcp-adapter` | `.mcp.json` | `mcpServers` | Add `"directTools": true`. Pi core does not include MCP. |
| Cursor | `.cursor/mcp.json` or `~/.cursor/mcp.json` | `mcpServers` | Project config belongs under `.cursor/`, not plain `.mcp.json`. |
| VS Code with Copilot | `.vscode/mcp.json` | `servers` | Root key differs. Also enable MCP in VS Code settings if needed. |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` | Use absolute paths. Restart Claude Desktop. |
| Claude Code | `claude mcp add` or `.claude/settings.json` | `mcpServers` | Prefer the CLI for current installs. Project file support depends on version. |
| Windsurf | `.windsurf/mcp.json` or `~/.windsurf/mcp.json` | `mcpServers` | Same basic shape as Cursor. |
| ChatGPT | app/connectors UI | n/a | Does not read local MCP JSON files. Usually needs a remote connector URL. |
| Unknown harness | check docs first | varies | Do not copy `.mcp.json` blindly. Confirm path and root key. |

## Pi setup

Use Dowse's Pi preset when available:

```powershell
dowse init . --harness pi --skip-index
```

Expected `.mcp.json`:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", ".dowse_index"],
      "directTools": true
    }
  }
}
```

Check that `pi-mcp-adapter` is installed. If missing, install it separately. Dowse only reports guidance, it does not install Pi or adapters.

After changing MCP config, restart the harness or reload the project. Most clients do not hot-reload MCP servers.

## Cursor setup

Create or merge `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", "${workspaceFolder}/.dowse_index"]
    }
  }
}
```

Then open Cursor settings and confirm the MCP server is green. If tools do not appear, enable the server/tools in Cursor and reload the project folder.

Optional freshness hook for Cursor only:

```powershell
dowse hook install
```

Do not add per-edit hooks. Use `index_status` then `index_codebase` when the index is stale.

## VS Code with Copilot setup

Create or merge `.vscode/mcp.json`. VS Code uses `"servers"`, not `"mcpServers"`:

```json
{
  "servers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", "${workspaceFolder}\\.dowse_index"]
    }
  }
}
```

If MCP tools do not show in Copilot Chat, check VS Code settings for MCP enablement and reload the window.

## Claude Desktop setup

Edit `%APPDATA%\Claude\claude_desktop_config.json` on Windows:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", "C:\\path\\to\\repo\\.dowse_index"]
    }
  }
}
```

Use absolute paths. Fully quit and reopen Claude Desktop after editing.

## Claude Code setup

Prefer CLI registration because it writes the current expected settings shape:

```powershell
claude mcp add --transport stdio dowse -- dowse serve --db C:\path\to\repo\.dowse_index
claude mcp list
```

For project-local config, use `.claude/settings.json` only after checking the installed Claude Code docs or `claude mcp --help`, because project `.mcp.json` behavior has changed across versions.

## Windsurf setup

Create or merge `.windsurf/mcp.json` for the project, or `~/.windsurf/mcp.json` globally:

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", ".dowse_index"]
    }
  }
}
```

Reload Windsurf after editing.

## Verification checklist

After configuring a harness:

1. `dowse doctor --root .` reports a healthy install, no locks, and a fresh or known-stale index.
2. `dowse query "where is query implemented" --db .\.dowse_index -n 3` returns JSON results.
3. The harness can see Dowse MCP tools. Expected tools are `query_context`, `index_codebase`, and `index_status`.
4. Call `index_status` first. If stale, call `index_codebase`. Then call `query_context`.
5. If the MCP server fails to start, run the exact configured command in a terminal from the same working directory.

## Troubleshooting

- `dowse` not found: use an absolute command path or install with `pipx install "dowse-context[mcp]"` so it is on PATH.
- MCP server starts but tools are missing: restart the client, then check the client's MCP logs.
- Relative db opens the wrong index: use an absolute `--db` path in global configs.
- Index lock errors: only one writer or `dowse serve` process can own the collection. Stop duplicate servers, then retry.
- VS Code config copied from Cursor fails: rename root `mcpServers` to `servers`.
- Plain `.mcp.json` ignored: move or copy the entry to that harness's documented config path.
- First query downloads or checks Hugging Face: expected unless MiniLM is already cached. Set `HF_TOKEN` only if rate limits matter.

## When editing repo docs or code

If changing Dowse itself, follow `AGENTS.md`: write a failing test first, make the smallest fix, then run `pytest -q`. Keep CLI stdout JSON-only.
