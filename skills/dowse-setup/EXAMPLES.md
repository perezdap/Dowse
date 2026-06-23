# dowse — examples & troubleshooting

## jq recipes

All recipes assume `--db .\.dowse_index` and that `jq` is on PATH. On Windows,
`jq` is available via `winget install jqlang.jq` or `scoop install jq`.

### File:line list of hits

```powershell
dowse query "retry with backoff" --db .\.dowse_index `
  | jq -r '.results[] | "\(.file_path):\(.start_line)  \(.symbol_name)"'
```

Output:

```text
pkg/db.py:42  Connection.query_with_retry
pkg/backoff.py:11  exponential_sleep
```

### Just the snippet bodies (build a prompt-context file)

```powershell
dowse query "where do we validate JWT claims" --db .\.dowse_index `
  | jq -r '.results[].code_content' > context.txt
```

### Ranked table with scores

```powershell
dowse query "auth token generation" --db .\.dowse_index -n 10 `
  | jq -r '.results[] | "\(.rank)\t\(.score)\t\(.file_path):\(.start_line)\t\(.symbol_name)"'
```

### Filter to one language, top 5 functions

```powershell
dowse query "parse config" --db .\.dowse_index --kind function --lang python -n 5 `
  | jq -r '.results[] | "\(.symbol_name)\n\(.code_content)\n"'
```

### Raw zvec SQL filter (shortcuts don't cover it)

```powershell
dowse query "db connection" --db .\.dowse_index `
  --filter "language = 'python' AND file_path LIKE 'pkg/%'"
```

### Definitions-mode (YAML/Markdown sections)

```powershell
# Index first with -D
dowse index .\packages --db .\.dowse_index --definitions
# Then query, filtering to yaml sections
dowse query "silent uninstall command" --db .\.dowse_index --lang yaml --kind section
```

## Troubleshooting

### `skipped N .go files (go) - pip install "dowse[go]"`

Not an error — the grammar wheel for that language isn't installed. Either
install the extra (`pip install "dowse[go]"`) or accept that those files are
excluded from the index. Missing grammars never crash the run.

### First `index`/`query` is slow / seems to hang

The first run downloads the ~80 MB MiniLM model
(`sentence-transformers/all-MiniLM-L6-v2`) and caches it under your HF cache
(`~/.cache/huggingface` on Linux/macOS, `%USERPROFILE%\.cache\huggingface` on
Windows). Subsequent runs load from cache and are offline. If you're on a
locked-down network, pre-download on a connected machine and copy the cache
directory over.

### Model dimension mismatch on query

`Store.create()` reads the dimension from the model at index time and bakes
it into the zvec schema. If you later query with a different `--model`, the
embedding dimension won't match the schema and the query will fail. **Always
use the same `--model` for query as for index.** If you need to switch models,
rebuild with `--reset`.

### `==` is a syntax error in `--filter`

zvec SQL uses `=` for equality, not `==`. Also escape single quotes inside
string literals by doubling them: `'it''s'`. The `--kind` and `--lang`
shortcuts handle escaping for you — prefer them when possible.

### Multi-agent worktrees

If agents work in separate git worktrees, prefer a **per-worktree index**:

```powershell
# Run from inside each worktree
dowse index . --db .\.dowse_index
dowse serve --db .\.dowse_index
```

Because `.\.dowse_index` resolves inside each worktree, each agent gets a
separate zvec collection and separate `.serve.lock`; there is no cross-agent
contention. Use a shared absolute `--db` only when agents intentionally share one
checkout/index.

### zvec lock / "collection is in use" errors

zvec allows many concurrent **readers** but only one **writer**, and a writer
excludes readers. `dowse query` and `dowse status` open read-only, so multiple
agents can query the same `--db` at once. Contention only arises when a writer
is involved (an `index`/`index_codebase`, or another `dowse serve` startup),
and dowse reports it as `index is already open` with exit code 1 rather than a
traceback. Common causes:

- A SessionStart hook runs `dowse index ... --db X` while a query/server is
  using `--db X`. Fix: run the hook against a different `--db`, or ensure the
  hook finishes before readers/servers start.
- Two `dowse serve` processes are configured against the same `--db`. The second
  one refuses to start because the first holds `<db>.serve.lock` for its
  lifetime. Fix: run a single shared server per repo.
- A previous `dowse` process crashed and left a stale lock. Fix: stop all
  `dowse` processes; if needed, delete the `--db` directory and rebuild with
  `--reset`.

### Re-index is a no-op but I expected changes

`run_index` re-walks and re-parses every file, then reconciles per file via
`Store.sync_file()` (upsert current symbols, delete vanished ids). If a file
genuinely changed, its symbols will update. If you see no change:

- Confirm you're pointing at the same `--db` you indexed into originally.
- Confirm the file's extension is in `supported_extensions` (run
  `dowse index --help` or check `dowse/extract.py` for the registry).
- For YAML/Markdown, remember `--definitions` is opt-in — a normal index
  doesn't touch them.

### `dowse serve` tools don't appear in the harness

- Confirm `pip install "dowse[mcp]"` succeeded (adds the official `mcp` SDK).
- Confirm the harness config points at the `dowse` executable on PATH (or use
  the full path: `C:\path\to\.venv\Scripts\dowse.exe`).
- Confirm the `--db` path exists and was indexed. `query_context` against an
  empty/unindexed DB returns `[]`.
- Restart the harness after editing its MCP config — most don't hot-reload.

### `stdout` has non-JSON noise breaking `jq`

Something is printing to stdout that shouldn't be. `dowse` writes JSON to
stdout and all progress/diagnostics to stderr. If you see banners or progress
in stdout, it's a bug — report it. Workaround: redirect stderr to `$null`
(`2>$null` in PowerShell, `2>/dev/null` in bash) so only stdout reaches `jq`.

### Tests fail with a model download

Tests use the stub embedder in `tests/conftest.py` and never download MiniLM.
If a test is trying to download, it's likely not using the `sample_repo` /
`db_path` fixtures, or it's importing `Embedder` directly instead of going
through `service.run_index()` / `service.run_query()`. See `AGENTS.md` for
the test conventions.
