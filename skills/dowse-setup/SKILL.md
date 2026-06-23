---
name: dowse-setup
description: Install, configure, and onboard the dowse local code Context Engine (tree-sitter + zvec + sentence-transformers) for a workspace. Use when user says "set up dowse", "install dowse", "configure dowse MCP", "index this repo with dowse", "wire dowse into Claude/Cursor/Copilot", or mentions dowse and needs install/index/serve steps.
---

# dowse setup

`dowse` is a local code Context Engine: tree-sitter extracts function/class
symbols, sentence-transformers embeds them, zvec stores vectors, and a hybrid
(dense + lexical re-rank) query returns ranked JSON snippets. Three surfaces:
`dowse index`, `dowse query`, `dowse serve` (MCP stdio). stdout is JSON only;
progress goes to stderr. Windows-first; CPython 3.12; runs fully offline after
the first model download (~80 MB MiniLM).

## Quick start (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"          # or: pip install dowse
dowse --help                      # sanity check
dowse index .\path\to\repo --db .\.dowse_index
dowse query "how does auth work" --db .\.dowse_index
```

## Workflows

### 1. Install

1. Confirm CPython 3.10+ (3.12 verified). zvec ships prebuilt Windows x64
   wheels — no compiler.
2. Create + activate venv.
3. `pip install -e ".[dev]"` for the repo, or `pip install dowse` for end use.
4. Optional language extras (only if the target repo uses them):
   `pip install "dowse[all-langs]"` — or pick: `[go]`, `[rust]`, `[bash]`,
   `[javascript]`, `[typescript]`.
5. Optional MCP extra: `pip install "dowse[mcp]"` — only if wiring `dowse serve`.
6. Verify: `dowse --help` and `pytest -q`.

**Do NOT install `tree-sitter-language-pack`** — it fetches grammars at
runtime and breaks offline use. Per-language wheels are self-contained.

### 2. Index

```powershell
dowse init .\repo --db .\.dowse_index         # one-command bootstrap: MCP config + gitignore + index
dowse index .\repo --db .\.dowse_index           # incremental, idempotent
dowse index .\repo --db .\.dowse_index --reset    # clean rebuild
dowse index .\packages --db .\.dowse_index --definitions   # YAML/Markdown sections
```

`dowse init` is the fastest path from fresh clone to working MCP: it writes or
merges `.mcp.json` with a `dowse` server entry, adds `.dowse_index/` to
`.gitignore`, reports missing grammar extras, and runs the first index — all in
one step. Use `--skip-index` for config-only runs. Re-running `init` is
idempotent (no duplicates, no clobbered MCP servers).

- Idempotent reconcile: re-running on an unchanged tree is a no-op; editing
  a file updates only changed symbols. Doc id = `sha1(file_path::symbol_name::kind)`.
- One `--db` per codebase. Paths in the index are POSIX-relative to the root.
- `.dowse_index` is a zvec collection directory (DB files, not a single file).
  It's in `.gitignore` — don't commit it.
- Missing grammars are skipped with a report like
  `skipped 12 .go files (go) - pip install "dowse[go]"` — never silent.
- First run downloads MiniLM once; subsequent runs are offline.

### 3. Query

```powershell
dowse query "how are auth tokens generated" --db .\.dowse_index
dowse query "RuntimeError: pool exhausted" --kind function -n 5 --db .\.dowse_index
dowse query "retry with backoff" --db .\.dowse_index | jq -r '.results[] | "\(.file_path):\(.start_line)"'
```

- Hybrid: `final = 0.7·dense + 0.3·lexical`. Pasting raw error messages works
  well because the lexical pass matches the literal symbol name.
- Filters: `--kind function|class|section`, `--lang python|powershell|yaml`,
  or `--filter "kind = 'function' AND file_path LIKE 'src/%'"` (zvec SQL;
  `==` is a syntax error — use `=`).
- Output `score` is similarity (higher = better); internally zvec returns a
  distance and dowse converts as `1 - distance`.
- **Use the same `--model` for query as for index.**

### 4. MCP server (for coding harnesses)

```powershell
pip install "dowse[mcp]"
dowse serve --db .\.dowse_index
```

Two tools: `query_context` (semantic recall — describe behaviour or paste an
error; complements grep/glob) and `index_codebase` (build/refresh, idempotent).

Register with Claude Desktop / Claude Code
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "dowse": {
      "command": "dowse",
      "args": ["serve", "--db", "C:\\path\\to\\.dowse_index"]
    }
  }
}
```

Use a **stable absolute `--db` path** so the server always opens the same
collection regardless of launch cwd.

### 4a. Multiple agents / git worktrees

Lock identity is the **resolved `--db` path**, not the repo. Two strategies:

- **Per-worktree index (recommended for parallel agents).** Each worktree uses
  a relative `--db .\.dowse_index`, which resolves to a different absolute path
  per worktree. Separate collection, separate `<db>.serve.lock` → **zero
  contention**: every agent can index, query, and serve at once. Cost: each
  worktree builds its own index (the ~80 MB model is shared via the HF cache;
  only per-symbol embedding repeats). Upside: the index always matches that
  worktree's actual code. `.dowse_index/` and `*.serve.lock` are git-ignored,
  so they won't pollute status — but clean up worktrees to reclaim disk.
- **Shared index (one checkout, many agents).** All agents point at the same
  absolute `--db`. This is the only case where the read/write rules and the
  server lock matter: many concurrent readers (`query`/`status`), one writer
  (`index`), and a single `dowse serve`. Prefer one long-lived shared server.

### 5. Hooks (optional, SessionStart only)

A SessionStart hook keeps the index fresh per session:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "dowse index . --db .\\.dowse_index 2>nul"
      }]
    }]
  }
}
```

**Avoid PostToolUse / per-edit hooks** — `run_index` re-walks and re-parses
the whole tree every call (only the zvec upsert is incremental). For
in-session freshness, instruct the agent to call the `index_codebase` MCP
tool after substantial edits — that keeps all DB access in the server
process. `dowse query` and `dowse status` use read-only zvec opens, so
separate agents can query one `.dowse_index` concurrently. Writers still
exclude readers: `index` / `index_codebase` can contend with queries or another
server, but dowse reports that as a concise stderr error. `dowse serve` also
holds a dedicated `<db>.serve.lock` for its lifetime (preventing a second server
for the same index) and performs an active-writer preflight before startup.

## Gotchas

- `stdout` is JSON only; never print banners/progress there. Pipe to `jq`.
- zvec cosine `query()` returns a **distance** (lower = closer); dowse
  converts to similarity as `1 - score`.
- `Store.sync_file()` does idempotent per-file reconcile — don't hand-roll
  insert/delete.
- Escape string literals in zvec SQL filters via single-quote doubling
  (`'it''s'`); `==` is a syntax error.
- Lazy-loaded heavy deps (torch, sentence-transformers) — `--help` and
  extraction stay fast.
- Optional grammars degrade gracefully: missing wheel → skip, not crash.

## Reference

- Repo layout, indexing model, coding standards: `AGENTS.md` in the dowse repo.
- CLI flags: `dowse index --help`, `dowse query --help`, `dowse init --help`, `dowse serve --help`.
- Schema and hybrid query internals: `dowse/store.py`, `dowse/service.py`.
- Copy-pasteable `jq` recipes and troubleshooting: see [EXAMPLES.md](EXAMPLES.md).
