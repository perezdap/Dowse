# dowse

A small, fluff-free CLI that turns a code tree into a queryable **Context Engine**. It parses files with tree-sitter, keeps only function/class definitions (not whole files), embeds them with a local `sentence-transformers` model, and stores them in **zvec** (Alibaba's embedded vector DB). Querying returns a clean JSON payload of the top-N most relevant snippets — designed to be piped into `jq`, `grep`, or straight into a prompt file.

No TUI, no dashboards, no progress bars. `stdout` is JSON only; all human/progress output goes to `stderr`, so pipelines stay clean.

## Layout

```
dowse/
  models.py       # the Symbol record shared by all extractors
  extract.py      # tree-sitter -> flattened function/class symbols
  definitions.py  # YAML/Markdown/.NET project definitions -> sections
  embed.py        # sentence-transformers wrapper (lazy-loaded)
  store.py        # zvec schema, idempotent indexing, hybrid query
  service.py      # core index/query logic (one impl, shared)
  cli.py          # Typer CLI: `index`, `query`, `status`, `doctor`, `init`, `serve`
  server.py       # MCP (FastMCP) stdio server wrapping the same logic
requirements.txt
pyproject.toml    # installs the `dowse` entrypoint; extras: [mcp], [go], ...
```

## Install

### End-user install

Install `dowse` into an existing Python environment when you want to index or query code without a development checkout:

```bash
pip install dowse
pip install "dowse[mcp]"           # add the MCP server dependencies
pip install "dowse[mcp,all-langs]" # add MCP + every optional grammar
```

### Development

Use an editable install when you are working on dowse itself:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install -e ".[dev,mcp]"   # if you want to exercise `dowse serve`
```

This was built and tested against `zvec 0.5.0`, `tree-sitter 0.25.2`, `tree-sitter-python 0.25.0`, `tree-sitter-powershell 0.26.4`, `tree-sitter-c-sharp 0.23.5`, `typer 0.26`, on CPython 3.12. zvec ships prebuilt wheels for Linux (x86_64/ARM64), macOS (ARM64), and **Windows x86-64** (added in zvec 0.3.0) — so on Windows just use 64-bit Python 3.12 and `pip install` works with no compiler. The first `index`/`query` downloads the ~80 MB MiniLM model once, then runs fully offline.

## The zvec schema (the "flattened AST" record)

Each symbol becomes one zvec document: a single dense vector plus scalar fields. Schema (in `store.py`):

| field          | type           | purpose                                  |
|----------------|----------------|------------------------------------------|
| `embedding`    | `VECTOR_FP32`  | dense vector, HNSW + **cosine**          |
| `file_path`    | `STRING` (idx) | POSIX path relative to the indexed root  |
| `symbol_name`  | `STRING`       | qualified name, e.g. `SessionManager.revoke` |
| `kind`         | `STRING` (idx) | `function` or `class`                    |
| `language`     | `STRING` (idx) | e.g. `python`                            |
| `start_line` / `end_line` | `INT32` | location for jumping to source      |
| `code_content` | `STRING`       | the exact snippet text                   |

`(idx)` fields carry an inverted index so SQL filters on them are fast. The embedding dimension is taken from the model at index time (MiniLM → 384), so the schema always matches the model.

A couple of zvec specifics worth knowing, since they're easy to get wrong:

- For the cosine metric, the `score` returned by `query()` is a **distance** (0 = identical). The tool converts it to a similarity as `1 - score`.
- `query()` returns the scalar `fields` inline, so retrieval needs no second fetch.
- Filters are SQL-style: `kind = 'function'`, `code_content LIKE '%retry%'`, `AND`/`OR`/`NOT`/`IN`. (`==` is a syntax error.)

## The indexing loop

`dowse index` walks the directory (skipping `.git`, `node_modules`, `__pycache__`, virtualenvs, build dirs — but only *below* the root, so a project living under a path like `.../build/...` still indexes), and for each supported file:

1. Parse once with tree-sitter; collect every `function_definition` / `class_definition` node. Names are qualified by walking enclosing definitions (so a method reads as `Class.method`).
2. Embed each symbol as `"{kind} {qualified_name}\n{body}"` (body capped at ~2k chars).
3. Reconcile the file in zvec **idempotently**.

The reconcile step is deliberate. zvec's `insert` ignores ids that already exist, and re-inserting a *deleted* id is tombstoned — so a naive "delete then re-insert" loses data. Instead each document id is `sha1(file_path::symbol_name::kind)` (stable across line moves), and per file the tool `upsert`s the current symbols, then deletes only the ids that have disappeared. Result: re-running `index` on an unchanged tree is a no-op; editing a file updates changed symbols, adds new ones, and removes deleted ones — without ever duplicating rows. After all files, one `optimize()` builds the vector index.

```bash
dowse index ./my_project --db ./.dowse_index          # incremental, idempotent
dowse index ./my_project --db ./.dowse_index --reset   # clean rebuild
```

`index` prints a JSON summary to stdout:

```json
{ "status": "ok", "indexed_files": 42, "indexed_symbols": 311, "dimension": 384, "db": "./.dowse_index", "elapsed_seconds": 8.4 }
```

## Checking index health

`dowse status` reports whether an index exists, how big it is, which languages it covers, and whether it has gone stale — so an agent (or you) can decide whether to index before querying instead of guessing. With `--root` set, `--db` defaults to `<root>/.dowse_index`, and two extra signals light up: `stale` (a source file newer than the index) and `missing_grammars` (files on disk whose grammar wheel isn't installed, each with an actionable `install_hint`).

```bash
dowse status --root ./my_project            # db defaults to ./my_project/.dowse_index
dowse status --db ./.dowse_index             # exists only, no root to compare
```

```json
{
  "exists": true, "db_path": "./.dowse_index",
  "indexed_files": 42, "indexed_symbols": 311, "dimension": 384,
  "languages": ["python", "rust"],
  "last_indexed_at": 1781460324.23, "stale": false,
  "missing_grammars": [
    { "language": "go", "extensions": [".go"], "file_count": 12,
      "install_hint": "pip install \"dowse[go]\"" }
  ]
}
```

`dowse doctor` bundles install facts (Python version, dowse module path, MCP SDK),
index health (same fields as `status`), serve/index lock probes, and whether
`.mcp.json` / `.cursor/mcp.json` reference a dowse MCP server — one JSON blob for
agents debugging setup.

```bash
dowse doctor --root ./my_project
```

## One-command bootstrap

`dowse init` wires a repo for agent use in one step: it writes or merges
`.mcp.json` with a `dowse` server entry, adds `.dowse_index/` to `.gitignore`,
reports any missing grammar extras, and runs the initial index.

```bash
dowse init ./my_project                         # full bootstrap with initial index
dowse init ./my_project --skip-index             # config + gitignore only, no index
dowse init ./my_project --db ./my_project/.dowse_index  # explicit db path
```

The generated `.mcp.json` uses the global `dowse` command (not a dev venv path)
and runs `serve --db .dowse_index` relative to the repo root. Re-running `init`
is idempotent: no duplicate `.gitignore` lines, no clobbered MCP servers, no
duplicate `dowse` entries.

```json
{
  "status": "ok",
  "workspace": {"root": "/path/to/my_project", "db_path": "/path/to/my_project/.dowse_index"},
  "mcp_config": {"created": true, "merged": false},
  "gitignore": {"path": "/path/to/my_project/.gitignore"},
  "missing_grammars": [
    {"language": "go", "extensions": [".go"], "file_count": 12,
     "install_hint": "pip install \"dowse[go]\""}
  ],
  "index": {"status": "ok", "indexed_files": 42, "indexed_symbols": 311, "dimension": 384,
            "db": "/path/to/my_project/.dowse_index", "elapsed_seconds": 8.4}
}
```

## Querying (hybrid search)

`dowse query` embeds your text, pulls a pool of dense candidates from zvec, then re-ranks them by combining semantic similarity with a cheap lexical overlap score (`final = 0.7·dense + 0.3·lexical`). The lexical pass is what makes pasting a raw error message work well — error text usually names the exact symbol, and the symbol-name match floats it to the top even if the embedding alone wouldn't. You can also push a native scalar filter down into zvec.

```bash
# Natural language
dowse query "how are auth tokens generated" --db ./.dowse_index

# Paste an error; restrict to functions; take top 5
dowse query "RuntimeError: connection pool exhausted" --db ./.dowse_index --kind function -n 5

# Pipe straight into jq — get just file:line for each hit
dowse query "retry with backoff" --db ./.dowse_index \
  | jq -r '.results[] | "\(.file_path):\(.start_line)  \(.symbol_name)"'

# Build a prompt-context file of just the snippets
dowse query "where do we validate JWT claims" --db ./.dowse_index \
  | jq -r '.results[].code_content' > context.txt

# Raw zvec filter for anything the shortcuts don't cover
dowse query "db connection" --filter "language = 'python' AND file_path LIKE 'pkg/%'"

# Estimate prompt-token savings versus the full files containing the returned snippets
dowse query "retry with backoff" --tokens --root ./my_project --db ./.dowse_index
```

Query output shape:

```json
{
  "query": "...",
  "filter": "kind = 'function'",
  "results": [
    {
      "rank": 1, "score": 0.72, "dense_similarity": 0.82, "lexical_score": 0.48,
      "file_path": "pkg/db.py", "symbol_name": "Connection.query", "kind": "function",
      "language": "python", "start_line": 6, "end_line": 7,
      "code_content": "def query(self, sql): ..."
    }
  ]
}
```

With `--tokens`, the same JSON payload includes a `token_savings` report:

```json
{
  "token_savings": {
    "estimator": "regex-v1",
    "snippet_tokens": 120,
    "full_file_tokens": 980,
    "saved_tokens": 860,
    "reduction_percent": 87.76,
    "results": [
      {"rank": 1, "file_path": "pkg/db.py", "symbol_name": "Connection.query", "snippet_tokens": 42}
    ],
    "files": [
      {"file_path": "pkg/db.py", "full_file_tokens": 230}
    ]
  }
}
```

The token report uses a lightweight deterministic approximation (`regex-v1`) that counts code-like words, numbers, and punctuation. It is not model-tokenizer exact, but it is stable, dependency-free, and good enough to show relative savings. Full-file comparison counts each of the full files containing the returned snippets once, so multiple snippets from one file do not double-count the baseline.

Tuning knobs: `--top/-n`, `--candidates` (dense pool size before re-rank), `--w-dense` / `--w-lexical`. Use the same `--model` for `query` as you used for `index`.

## Using it from a coding harness (MCP)

The CLI is already harness-usable as-is: any agent that can run a shell command can call `dowse query "..."` and read the JSON. But for harnesses that speak MCP (Claude Code, Claude Desktop, Cursor, Copilot CLI), `dowse serve` exposes the same logic as three native tools over stdio:

```bash
pip install "dowse[mcp]"   # adds the official mcp SDK
dowse serve --db ./.dowse_index          # speaks MCP on stdio
```

- **`query_context`** — semantic code lookup. Returns the same ranked snippet list as `dowse query`. Its description tells the agent to use it for *meaning-based recall* (describe behaviour, paste an error) as a complement to `grep`/`glob`, which stay better when you know the literal string.
- **`index_codebase`** — build/refresh the index (idempotent; `definitions` and `reset` flags exposed).
- **`index_status`** — self-diagnosis. Call before indexing/querying to learn whether an index exists, which languages it covers, whether it's gone stale, and which grammars are missing (with install hints). Never throws on a missing index — it reports state so the agent can choose its next step.

Prefer one long-lived MCP server per repo over competing server/index processes. `dowse query` and `dowse status` open the collection read-only, so multiple independent agents can query the same `.dowse_index` concurrently. Indexing still needs write access, and zvec does not allow readers and writers at the same time; those conflicts are reported as a concise stderr error instead of a traceback. `dowse serve` serializes in-process tool calls for the same index, holds a dedicated `<db>.serve.lock` for its lifetime so a second server for the same index refuses to start, and performs an active-writer preflight before startup.

For parallel agents in separate git worktrees, prefer a per-worktree relative `--db ./.dowse_index`: each worktree gets its own collection and `.serve.lock`, so agents can index/query/serve independently and the index matches that worktree's code. Use a shared absolute `--db` only when agents intentionally share one checkout/index.

Register it with a harness by pointing at the command. For Claude Code / Claude Desktop (`claude_desktop_config.json` on Windows lives at `%APPDATA%\Claude\`):

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

This deliberately uses the FastMCP class bundled with the official `mcp` SDK rather than the standalone `fastmcp` package — the latter's v3 line rebuilt its architecture and auth model in early 2026, and for a local two-tool stdio server the bundled one is the stable, lower-churn choice.

## Definition files (YAML, Markdown, .NET/MSBuild)

Declarative definition files aren't code, so the function/class model doesn't fit them — but they're often exactly what you want to search ("what's the uninstall command for 7zip", "which target framework does this project use", "where is this build target defined"). Pass `--definitions` (`-D`) to additionally index them as **sections**:

```bash
dowse index ./packages --db ./.dowse_index --definitions
dowse query "silent uninstall command" --db ./.dowse_index --lang yaml

dowse index ./dotnet-repo --db ./.dowse_index --definitions
dowse query "target framework and nullable settings" --db ./.dowse_index --lang msbuild
dowse query "custom GenerateVersion build target" --db ./.dowse_index --kind section --lang msbuild
```

- **YAML profiles** (Payload-style): each top-level key becomes a section, qualified by the package name if the file has a `name:`/`id:`/`packageName:` field — e.g. `7zip.install`, `7zip.uninstall`, `7zip.detection`.
- **Markdown definitions** (PowerPacker-style): each ATX heading becomes a section qualified by its heading ancestry — e.g. `Google Chrome.Install`, `Google Chrome.Install.Pre-Install`. Headings inside fenced code blocks are ignored.
- **.NET/MSBuild XML** (`.csproj`, `.props`, `.targets`): `PropertyGroup`, `ItemGroup`, `ItemDefinitionGroup`, and `Target` blocks become sections, qualified by the file name and useful child names — e.g. `App.PropertyGroup.TargetFramework.Nullable`, `App.ItemGroup.PackageReference.Microsoft Extensions Logging.ProjectReference.Shared`, `Custom.Target.GenerateVersion.Message.WriteLinesToFile`.

These extractors are pure-stdlib (no PyYAML, no Markdown parser, no MSBuild SDK): they scan regular structure and use Python's built-in XML parser where useful, which is more forgiving of half-finished files than a strict project-system dependency. The flag is **opt-in** so a normal code index doesn't slurp every `README.md`, CI YAML, or project metadata file in the repo. The sections land in the same collection with `kind` set to `section` and `language` set to `yaml`, `markdown`, or `msbuild`, so you can filter them with `--lang msbuild` or `--kind section`. To add other declarative formats, drop an extractor into `definitions.py` and register its extension.



`extract.py` has a small registry mapping extensions to a grammar loader and the node types that count as definitions. A language activates automatically if its grammar wheel is installed; uninstalled grammars are skipped rather than erroring.

**Verified end-to-end** (load offline from a self-contained wheel, no compiler, correct symbol + qualified-name extraction):

| Language   | Extensions     | Wheel                      | Notes                                                        |
|------------|----------------|----------------------------|--------------------------------------------------------------|
| Python     | `.py` `.pyi`   | `tree-sitter-python`       | reference grammar                                            |
| PowerShell | `.ps1` `.psm1` | `tree-sitter-powershell`   | `function`/`filter`/`class` + methods; `param()` aware       |
| C#         | `.cs`          | `tree-sitter-c-sharp`      | class/interface/struct/record + methods + constructors       |

PowerShell needs no `name` field handling out of the box — the grammar puts identifiers in `function_name`/`simple_name` children, which the registry resolves via `name_child_types`.

**Optional grammars** (install the extra; verified node names): JavaScript, TypeScript, Go, Rust, Bash. Install via the extras, e.g. `pip install dowse[go,rust]`, or grab them all with `pip install "dowse[all-langs]"`.

| Language   | Extensions   | Extra           | Wheel                    | Notes                                                       |
|------------|--------------|-----------------|--------------------------|-------------------------------------------------------------|
| JavaScript | `.js` `.jsx` `.mjs` `.cjs` | `javascript` | `tree-sitter-javascript` | function/method/class                                       |
| TypeScript | `.ts` `.tsx` | `typescript`     | `tree-sitter-typescript` | function/method/class                                       |
| Go         | `.go`        | `go`             | `tree-sitter-go`         | `type_spec` modelled as `kind=class` (known compromise)     |
| Rust       | `.rs`        | `rust`           | `tree-sitter-rust`       | fn/struct/enum/trait; trait methods qualified by trait      |
| Bash       | `.sh` `.bash`| `bash`           | `tree-sitter-bash`       | `function_definition` (both `name()` and `function name` forms) |

When a grammar is missing, `dowse index` reports it, e.g. `skipped 12 .go files (go) - pip install "dowse[go]"`, so polyglot repos never fail silently.

**Deliberately not auto-handled:** most declarative/data formats (Bicep, `.psd1`, arbitrary XML/JSON) don't have a function/class shape, so the symbol model doesn't fit them. The definition extractors above are explicit opt-ins for formats with a stable section shape; other formats should get similarly small custom extractors rather than being forced through a code grammar.

> Avoid `tree-sitter-language-pack` for this tool. Despite advertising bundled wheels, version 1.8.1 fetches grammars from GitHub releases on first use — it fails the moment the network is blocked, which defeats the offline/locked-down goal. The per-language wheels above are genuinely self-contained.

## What was verified

Exercised end-to-end in a sandbox: tree-sitter extraction for Python, PowerShell, and C# (loaded offline from self-contained wheels, with correct qualified-name resolution); the full zvec lifecycle (schema, upsert, filtered queries, cosine distance→similarity, idempotent reconcile on edits); the YAML/Markdown/.NET definition extractors (package-name and heading-ancestry qualification, fence-aware Markdown, MSBuild property/item/target sections, malformed XML fallback); the CLI; and the MCP server (both tools register with correct schemas, and an in-process `query_context` call returns ranked results). The one piece run only through its standard, stable API — not against a downloaded model in the sandbox — is the `sentence-transformers` `encode()` call in `embed.py`; the first real `index` will download MiniLM and exercise it. Likewise the MCP server was verified in-process via the SDK's own client API rather than over a live stdio pipe to an external harness.
