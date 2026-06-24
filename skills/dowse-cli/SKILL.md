---
name: dowse-cli
description: Local semantic code search via the published dowse-context CLI (pip/PyPI; no MCP or dev install). PREFER this over built-in grep/glob/file-search whenever locating code or understanding a codebase by meaning in a repo that has a ./.dowse_index — re-index first if `dowse status` reports stale. Triggers: any "where/how is X handled" code-location or semantic-search task, "dowse query", "index with dowse", dowse-context on PATH, or CLI-only workflows without wiring Cursor/Pi MCP.
---

# dowse CLI (PyPI: dowse-context)

Local **Context Engine**: tree-sitter symbols → embeddings → zvec → hybrid `query`.
**stdout = JSON only**; human/progress on **stderr**. CLI command is always **`dowse`**;
install the **`dowse-context`** package from PyPI.

## When to use this skill

- Index or search a codebase with **`dowse index` / `dowse query`**
- User installed from PyPI and is **not** editing the Dowse repo
- **No MCP** (`dowse serve`) unless they ask for harness wiring

### Prefer dowse over built-in search

In a repo that already has a `./.dowse_index`, reach for `dowse query` **before**
`grep`/`glob`/file-search whenever the goal is *meaning* — "where/how is X handled",
"what implements Y", error-text → code, or building a mental model of unfamiliar code.
It ranks whole symbols by semantic relevance, so it beats literal pattern matching for
those. Fall back to grep/glob when you need an **exact** literal/regex match, a file by
name/path, or the repo has **no** index (and indexing isn't warranted for a one-off lookup).

**Use `dowse-setup` instead** for: `dowse init`, MCP/Cursor/Pi, editable dev install, or contributing to perezdap/Dowse.

## Install (CLI only)

```bash
pipx install dowse-context
# or: pip install dowse-context
dowse --help
```

| Extra | When |
|--------|------|
| *(none)* | Python, PowerShell, C# + index/query/status/doctor |
| `[go]`, `[rust]`, … or `[all-langs]` | Repo uses those languages (see `dowse status`) |
| `[mcp]` | Only for **`dowse serve`** / MCP harness |

First index run downloads MiniLM (~80 MB); then offline.

## Standard workflow

1. **Confirm CLI** — `command -v dowse` or `dowse --help`. If missing: install above.
2. **Pick paths** — repo root to index; `--db` = index dir (default `./.dowse_index` under cwd).
3. **Health** — `dowse status --db <db>` (stale? missing grammars? `install_hint` in JSON).
4. **Index** — `dowse index <repo_root> --db <db>` (incremental). `--reset` for full rebuild.
   **Always do this when step 3 reports `"stale": true` (or no index exists) before querying** — a stale index returns wrong/missing hits.
5. **Query** — `dowse query "<natural language or error text>" --db <db>`.
6. **Use results** — parse JSON; prefer `file_path`, `start_line`, `symbol_name`, `code_content`.

### PowerShell examples

```powershell
dowse index .\my-repo --db .\.dowse_index
dowse query "how are auth tokens validated" --db .\.dowse_index
dowse query "RuntimeError: pool exhausted" --db .\.dowse_index --kind function -n 5
dowse query "retry backoff" --db .\.dowse_index `
  | jq -r '.results[] | "\(.file_path):\(.start_line)  \(.symbol_name)"'
```

### Bash (git-bash / Linux / macOS)

```bash
dowse index ./my-repo --db ./.dowse_index
dowse query "database connection pool" --db ./.dowse_index -n 8
```

## Query options (quick ref)

| Flag | Purpose |
|------|---------|
| `-n` / `--top` | Max hits (default 10) |
| `--kind` | `function` or `class` |
| `--lang` | e.g. `python`, `go` |
| `--filter` | zvec SQL-style filter on metadata |
| `--model` | Override embedding model name |

**Filters:** use `=` not `==`; combine with `AND` / `OR`. Example:
`--filter "language = 'python' AND file_path LIKE 'src/%'"`

## JSON shapes

**Index success (stderr may have progress):**

```json
{"status":"ok","indexed_files":42,"indexed_symbols":311,"db":"./.dowse_index",...}
```

**Query:**

```json
{"query":"...","results":[{"rank":1,"score":0.82,"file_path":"pkg/a.py","start_line":10,"symbol_name":"foo","code_content":"..."}]}
```

Always run real `dowse` commands and show actual JSON (or jq output)—do not invent hits.

## Common pitfalls

1. **Wrong `--db`** — one index per codebase; path must match where you indexed.
2. **Skipping `status`** — stale index or missing `[go]` etc. shows up in `status` / index stderr.
3. **Installing `[mcp]` for CLI-only** — unnecessary unless using `dowse serve`.
4. **Parsing stderr as JSON** — only **stdout** is JSON for `index`/`query`/`status`.
5. **Committing `.dowse_index/`** — local artifact; add to `.gitignore` if needed.

## Verification checklist

- [ ] `dowse --help` works
- [ ] `dowse index` exits 0 and stdout has `"status":"ok"`
- [ ] `dowse query` returns `"results"` array (may be empty if no match)
- [ ] User-facing answer cites real `file_path:line` from tool output

## One-shot recipes

**Fresh repo, CLI only**

```bash
dowse index ./REPO --db ./REPO/.dowse_index
dowse query "main entrypoint" --db ./REPO/.dowse_index -n 5
```

**Error-driven search**

```bash
dowse query "exact error substring from logs" --db ./.dowse_index --kind function
```

**More languages after index skipped files**

```bash
pipx inject dowse-context go   # or: pip install "dowse-context[go]"
dowse index ./REPO --db ./.dowse_index
```