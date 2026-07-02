# Changelog

All notable changes to **dowse** are documented here. Dates are in UTC.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.6] - 2026-07-01

### Changed
- **Single-sourced version:** `dowse.__version__` now derives from installed
  package metadata (`importlib.metadata.version`) instead of a hardcoded string,
  so it can never drift from the `pyproject.toml` version again.

### Fixed
- The Release workflow now runs the test suite (ruff + pytest) before building and
  publishing, so a package that fails CI can no longer reach PyPI. (The published
  0.2.5 wheel carries a stale `__version__` of `0.2.3` because the previous Release
  workflow skipped tests; 0.2.6 is the first release where the imported version
  matches the distribution version.)

## [0.2.5] - 2026-07-01

### Added
- **Pi session auto-index extension:** `skills/dowse-cli/pi-extension.ts` runs
  `dowse hook session-start` on Pi session start, keeping the local
  `.dowse_index` fresh without manual reindexing. Mirrors the Cursor
  `sessionStart` hook behavior (opt-in, fail-open).
- **Content-aware staleness detection:** `dowse status` now detects deleted
  files, new files with old mtimes, content changes with preserved mtimes, and
  newly-supported grammar extensions — not just mtime newer than index. Uses
  SHA1 hashes for content comparison when mtime is unreliable.
- **Index metadata:** `dowse index` writes `dowse-meta.json` with indexed files,
  hashes, extensions, and definitions flag so status checks have ground truth.

### Changed
- Session hook (`dowse hook session-start`) skips reindexing when the index is
  already fresh, avoiding redundant work on session reload.
- Bootstrap logic extracted into `dowse/bootstrap.py` so `service.py` remains
  the single source of truth for index/query orchestration.

### Tested
- Added integration tests covering fresh-index skip, stale-after-delete,
  stale-after-copy-with-old-mtime, stale-after-content-change, definition file
  staleness, and new-extension staleness.

## [0.2.3] - 2026-06-25

### Changed
- Index walking now respects Git ignore rules: candidate files are filtered
  through `git check-ignore`, so paths excluded by `.gitignore`,
  `.git/info/exclude`, or a global git excludes file are no longer indexed.
  Matching fails open when git is unavailable or the tree is not a work tree,
  preserving the prior index-everything behavior.

### Security
- Agent-instruction files are skipped during indexing even with
  `--definitions`: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CODEX.md`,
  `copilot-instructions.md`, and `.cursorrules`. These exist for AI agents, not
  as code context, and are usually committed (so `.gitignore` would not catch
  them).

### Tested
- Added integration tests covering gitignored source exclusion, directory-pattern
  ignores, non-ASCII path handling, non-git graceful degradation, and
  agent-instruction doc exclusion under `--definitions`.

## [0.2.2] - 2026-06-25

### Added
- Safety guard for `dowse index` and non-`--skip-index` `dowse init`: refuse to
  index the user's home directory or any ancestor of it by default, preventing
  accidental whole-home indexing when run from the wrong working directory.

### Changed
- `dowse index` and `dowse init` now expose `--force` to override the home-root
  safety guard when intentionally indexing a very broad tree.
- `.mcp.json` is now ignored by default in `.gitignore`, avoiding accidental
  commits of local harness wiring.

### Tested
- Added integration tests covering home-directory refusal, ancestor refusal,
  `--force` override behavior, and the `init` pre-write refusal path.

## [0.2.1] - 2026-06-24

### Changed
- PyPI distribution renamed to **`dowse-context`** (import package and CLI command
  remain **`dowse`**). Production name **`dowse`** is owned by another project on PyPI.

## [0.2.0] - 2026-06-24

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
