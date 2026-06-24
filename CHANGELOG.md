# Changelog

All notable changes to **dowse** are documented here. Dates are in UTC.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Cursor session auto-index (opt-in, #4 / #19):** `dowse hook install` merges a
  `sessionStart` entry into `~/.cursor/hooks.json` that runs `dowse hook
  session-start`. On each Cursor session, that command incrementally indexes only
  workspaces that already have `.dowse_index/` (or `.dowse.yaml`), and **fails open**
  so hook errors never block the editor. `dowse init --auto-index` runs the same
  installer once per machine; default `init` does not touch hooks.
- README documents global installs via **pipx** and **uv tool** (minimal, MCP, and
  `all-langs` variants) and summarizes **core vs optional** language extras near
  the end-user install section.
- CI **wheel-smoke** job: build wheel, install into an isolated venv, run
  `dowse --help`, `dowse serve --help`, and `dowse status` (issue #18).
- Release workflow (`.github/workflows/release.yml`) — builds wheel + sdist with
  `python -m build`, validates with `twine check dist/*`, publishes to TestPyPI
  then PyPI via **PyPI Trusted Publishing** (OIDC, no API tokens). Triggers on
  `v*` tag pushes only; never on ordinary PRs. See `RELEASE.md` for setup.
- `dowse init` — one-command project bootstrap: writes or merges `.mcp.json`
  with a `dowse` server entry, adds `.dowse_index/` to `.gitignore`
  idempotently, reports missing grammar coverage, and runs an initial index.
  Supports `--skip-index` for config-only runs.
- `dowse doctor` — JSON diagnostics for Python/dowse install, MCP SDK presence,
  index health (via `run_index_status`), serve/index lock probes, and
  `.mcp.json` / `.cursor/mcp.json` harness wiring hints.

### Changed
- **Docs:** `AGENTS.md` and `skills/dowse-setup/SKILL.md` aligned with `hook install`,
  `init --auto-index`, three MCP tools, and Pi/global install quickstart.
- `dowse index`, `dowse query`, `dowse status`, and `dowse serve` now report
  locked zvec collections with a concise stderr message and exit code 1 instead
  of leaking a traceback. The message points harness users toward one long-lived
  `dowse serve` process rather than competing server/index processes.
- `dowse query` and `dowse status` open zvec collections read-only, allowing
  multiple independent agents/processes to query or inspect the same
  `.dowse_index` concurrently. They still fail cleanly while an index/write is
  in progress, because zvec does not allow readers and writers at the same time.
- Service-level index operations are serialized per resolved index path inside a
  process. Concurrent MCP tool calls against the same `.dowse_index` now wait
  for each other instead of fighting over zvec's single-writer collection lock.
- `dowse serve` holds a dedicated OS-level server lock (`<db>.serve.lock`) for
  its lifetime, guaranteeing only one MCP server can run for a given index path.
  It still performs an active-writer zvec lock preflight before importing the
  optional MCP dependency, so it refuses to start immediately if indexing is
  already using the configured collection.

### Documented
- Multi-agent worktree guidance: use a per-worktree relative `--db ./.dowse_index`
  for fully isolated indexes and locks; use a shared absolute `--db` only when
  agents intentionally share one checkout/index.

## [0.1.1] - 2026-06-20

### Added
- `CHANGELOG.md` to document releases going forward.
- `.gitignore` covering `.venv/`, `.dowse_index/`, Python build artefacts, and
  common IDE / cache directories.
- Ruff dev-extras dependency and a narrow `tool.ruff` config in `pyproject.toml`.
  Rule-set is deliberately small (`F`, `B904`, `B905`) so CI stays green without
  re-litigating prior style choices; widen on purpose as existing warnings are
  triaged.
- A `ruff check dowse tests` step in CI before `pytest -q`.
- `tests/test_embed.py` covering both new and legacy `SentenceTransformer`
  dimension APIs plus a negative case.

### Changed
- `Embedder.dimension` now prefers the new `get_embedding_dimension()` API and
  falls back to `get_sentence_embedding_dimension()` when the new name is
  absent. Removes the `FutureWarning` printed on cold installs of modern
  `sentence-transformers`.
- `Store.sync_file` passes `strict=True` to `zip()` (B905).
- `dowse.cli.serve` raises `typer.Exit(code=1) from None` so the missing-dep
  message is the only error the caller sees (B904).
- `tests/test_pipeline.py` no longer imports `pytest` (unused) (F401).

### Documented
- `Store.count` now has a one-line comment explaining why a bare `except
  Exception` is intentional: `zvec.stats` shape varies across releases, and `-1`
  means "unknown" rather than "zero".

## [0.1.0] - 2026-06-19

### Added
- Initial release. Local code Context Engine: `dowse index` / `dowse query` /
  `dowse serve` (MCP stdio), backed by `tree-sitter` extraction and `zvec`
  hybrid retrieval with re-ranker-style lexical boosting.
