# AGENTS.md

Guidance for AI agents and contributors working on **dowse**.

## What this project is

**dowse** is a small, fluff-free local **Context Engine** for codebases. It:

1. Walks a directory and parses supported source files with **tree-sitter**
2. Extracts **function/class symbols** (not whole files) into a shared `Symbol` record
3. Embeds symbols with a local **sentence-transformers** model
4. Stores vectors + metadata in **zvec** (embedded vector DB)
5. Answers natural-language or error-message queries via **hybrid search** (dense + lexical re-rank)

There is no TUI, no dashboard, no progress bars. **`stdout` is JSON only**; human/progress output goes to **`stderr`** so `dowse query ... | jq` always works.

Primary surfaces:

| Surface | Role |
|---------|------|
| `dowse index` | Build or refresh the index (incremental, idempotent) |
| `dowse query` | Hybrid-search; emit ranked snippets as JSON |
| `dowse status` | Index health, staleness, missing grammars |
| `dowse doctor` | One-shot JSON diagnostics (install, locks, MCP wiring) |
| `dowse init` | Bootstrap `.mcp.json`, `.gitignore`, optional initial index |
| `dowse hook` | Opt-in Cursor `sessionStart` auto-index (`install`, `session-start`) |
| `dowse serve` | MCP stdio server exposing the same logic to coding harnesses |

## Layout

```
dowse/
  models.py       # Symbol dataclass shared by all extractors
  extract.py      # tree-sitter → function/class symbols
  definitions.py  # optional YAML/Markdown section extractors (--definitions)
  embed.py        # sentence-transformers wrapper (lazy-loaded)
  store.py        # zvec schema, idempotent sync_file(), hybrid query
  service.py      # run_index() / run_query() — single implementation
  cursor_hooks.py # opt-in Cursor sessionStart hook install + session-start runner
  cli.py          # Typer CLI (thin wrapper over service)
  server.py       # MCP server (thin wrapper over service)
tests/
  conftest.py     # stub embedder + sample repo fixtures
  test_pipeline.py
```

**Rule of thumb:** put business logic in `service.py` or the layer below it (`store`, `extract`, `embed`). `cli.py` and `server.py` should stay thin — parse args, call service, format output.

## Indexing model (important)

Indexing is **idempotent reconcile**, not blind insert:

- Document id = `sha1(file_path::symbol_name::kind)` (stable across line moves)
- Per file: `Store.sync_file()` upserts current symbols, deletes stale ids
- Empty files and deleted files must be reconciled (no orphaned docs)
- After all files: one `store.optimize()`

Paths in the index are **POSIX-normalised**, relative to the indexed root.

## Setup and commands

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

| Task | Command |
|------|---------|
| Run tests | `pytest -q` |
| Bootstrap repo | `dowse init ./path/to/repo` (add `--harness pi` for Pi preset) |
| Index a tree | `dowse index ./path/to/repo --db ./.dowse_index` |
| Query | `dowse query "how does auth work" --db ./.dowse_index` |
| Index health | `dowse status --root ./path/to/repo` |
| Setup diagnostics | `dowse doctor --root ./path/to/repo` |
| Cursor session hook | `dowse hook install` (once per machine; requires `dowse` on PATH) |
| MCP server | `pip install "dowse[mcp]"` then `dowse serve --db ./.dowse_index` |

CI runs on **Windows** (`windows-latest`, Python 3.12) for every PR to `main`. Keep tests portable — no hardcoded absolute paths, no embedding model download in tests.

---

## Test-driven development (required)

All behaviour changes and bug fixes follow **red → green → refactor** in **vertical slices** (one failing test, one minimal fix, repeat). Do not batch-write all tests then all implementation.

### What to test

- Test **behaviour through public interfaces**: CLI via `typer.testing.CliRunner`, or `service.run_index()` / `service.run_query()` directly.
- Prefer **integration-style** tests that exercise real extraction, storage, and query paths.
- Use **`tmp_path`** for repos and index directories — never hardcode host paths.
- Use the **stub embedder** in `tests/conftest.py` so tests never download MiniLM.

### What not to test

- Do not mock internal collaborators when a real end-to-end path is cheap (this codebase's tests are fast).
- Do not test private helpers in isolation unless there is no reasonable public surface.
- Do not add tests that only assert data-structure shape or trivial getters.

### Workflow for every change

1. **RED** — write or extend a pytest test that fails for the missing/wrong behaviour
2. **GREEN** — implement the smallest change to pass
3. **REFACTOR** — clean up while tests stay green
4. Run `pytest -q` locally before opening a PR

### Test file conventions

- Tests live under `tests/`
- Shared fixtures belong in `tests/conftest.py`
- Name tests by behaviour: `test_reconcile_deleted_file`, not `test_sync_file_helper`
- Reuse `sample_repo` and `db_path` fixtures when exercising index/query flows

---

## Coding standards

### Scope and style

- **Minimal diffs** — change only what the task requires; match surrounding style.
- **`from __future__ import annotations`** in every module.
- **Imports at the top of the file.** No inline imports unless there is a documented circular-dependency reason.
- **Exhaustive switches** — on discriminated unions or enums, use a `never` check in the `default` case so new variants cause compile-time failures until handled.
- Comments explain *why*, not *what*. Good code should mostly read on its own.

### Architecture rules

- **`service.py` is the single source of truth** for index/query orchestration. CLI and MCP must call it, not duplicate loops.
- **Lazy-load heavy deps** (`sentence-transformers`, `torch`) — see `embed.py`. Extraction and `--help` must stay fast.
- **Graceful degradation** for optional grammars: if a `tree-sitter-*` wheel is not installed, skip that language; do not crash.
- **Do not use `tree-sitter-language-pack`** — it fetches grammars at runtime and breaks offline use.

### CLI / output contract

- Commands that emit results write **JSON to stdout only** via `_emit()`.
- Progress and diagnostics go to **stderr** via `_err()` or the `log` callback in `run_index()`.
- Never print banners, spinners, or debug text to stdout.

### Storage / zvec

- Cosine `query()` scores are **distances** (lower = closer). Convert to similarity as `1 - score`.
- Use `Store.sync_file()` for per-file reconcile; do not hand-roll insert/delete unless there is a strong reason.
- Escape string literals in zvec SQL filters via `_sql_str()`.

### Windows-first

This project is developed and CI-tested on **native Windows**. Use:

- PowerShell-friendly examples in docs
- `Path` and `.as_posix()` for index paths
- Portable tests with `tmp_path`

---

## Pull requests

- Branch from `main`; open PRs back to `main`
- CI must be green before merge
- `main` is protected — changes require owner review (see `.github/CODEOWNERS`)
- Commit messages: one sentence on **why**, not a bullet list of files touched
- Reference the GitHub issue in the PR body when applicable (`Closes #N`)

## Out of scope unless explicitly requested

- SQL filter escaping hardening for `kind` / `lang` shortcuts
- Model dimension mismatch validation on query
- New MCP tools or CLI commands beyond what the issue specifies
- Refactors unrelated to the task at hand
