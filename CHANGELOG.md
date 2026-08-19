# Changelog

All notable changes to **dowse** are documented here. Dates are in UTC.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Id-map corruption is no longer reported as a locked index:** `_is_lock_error`
  matched four zvec strings, but only `"Can't lock" … "collection"` means "another
  handle owns it". `"create id map failed"` fires *after* this process takes the
  collection lock, so it signals real id-map/disk failure; `"lock hold by"` is in
  no published wheel; `"No locks available"` is the POSIX `ENOLCK` text from the
  C++ `system_error` table. Matching them hid corruption behind "wait for the
  other process". The surviving phrase set is now the named, version-anchored
  `_LOCK_REFUSAL_PHRASES` (#41).
- **`dowse status`, `query`, and `index` no longer print a traceback** when the
  collection cannot be read. They fail like a held index does: exit 1, no stdout,
  one stderr line carrying zvec's message and the `--reset` remedy. This crash
  predates the matcher change — no version of it ever matched zvec's actual
  open-path failure, `"recovery idmap failed"`.
- **`dowse doctor` survives a broken or busy index.** It previously raised out of
  `run_index_status` before reaching its own lock probe, so the one command for
  diagnosing an unusable index was the command that died on it.

### Changed
- **Index status reports unreadable collections instead of raising.**
  `run_index_status` (so `status`, `doctor`, and the MCP `index_status` tool) gains
  an `error` field: null on a healthy index, set when the collection exists but
  zvec cannot open it. Contention still raises `LockedIndexError`, because waiting
  for another handle is a different remedy than rebuilding.
- **Unknown counts are null, not 0.** For an index that exists but could not be
  read, `indexed_files` / `indexed_symbols` are null — unknown is not empty.
  Consumers treating `0` as "nothing indexed" are unaffected on healthy indexes.
- `doctor` keeps exiting 0 on a damaged index and reports the damage in its JSON:
  describing a broken index is what it is for. Contention appears only as
  `locks.index.locked`, never as an `index.error`.

### Tested
- Real zvec contention across `Store.create` / `open` / `open_readonly`, so the
  upstream lock wording itself is under test rather than a hand-written string.
- A genuinely corrupted id-map pointer (not a stub) driving `status`, `query`,
  `index`, and `doctor` through the CLI, asserting exit codes and no traceback.
- Non-lock failures propagate unchanged across all three entry points and all
  three dropped phrases.

## [0.3.0] - 2026-08-18

### Added
- **`.dowseignore`:** an opt-in, gitignore-syntax file at the repo root that
  excludes paths from the index. It is purely subtractive and applied in
  `walk_directory` (shared by `index`, staleness checks, and language
  coverage), so every walk honors it. It closes the gap `git check-ignore`
  cannot: a tracked file matching a `.gitignore` pattern is not reported as
  ignored, so dowse's gitignore pass keeps it — `.dowseignore` lets you drop
  such paths (and any tracked, non-gitignored tree) without editing
  `.gitignore`. Patterns use gitignore globs: bare `knowledge/` matches at
  any depth, `/knowledge/` is anchored to the index root; negation (`!`)
  applies within `.dowseignore` only and cannot rescue a path dropped by the
  hardcoded skip set, the agent-doc blocklist, or git ignore. Requires the
  new `pathspec` dependency.
- **`dowse --version`:** prints `{"dowse": "<version>"}` to stdout and exits 0,
  so scripts and support workflows can check which install is on PATH without
  running the full `dowse doctor` diagnostics.

### Changed
- **`dowse serve` now requires `mcp>=2.0` (breaking).** SDK 2.0.0 (released
  2026-07-28) removed the `mcp.server.fastmcp` module and renamed `FastMCP` to
  `MCPServer` under `mcp.server`, so `build_server` imports and returns
  `MCPServer`. The `mcp` optional extra moved from `mcp>=1.27` to `mcp>=2.0` in
  both `pyproject.toml` and `requirements.txt`. If you installed the extra
  before this release, upgrade it — `pip install -U "dowse-context[mcp]"`; left
  on 1.x, `dowse serve` exits 1 with the install hint (see below). Nothing else
  changes: the exposed tools, their arguments, and their JSON payloads are
  identical, and `service.py` was untouched. Note that the standalone `fastmcp`
  package is a different project and is still not used.

### Fixed
- **`dowse serve` install hint on an outdated SDK:** the missing-dependency
  guard in `cli.serve` caught only `ModuleNotFoundError`, so it handled "mcp
  isn't installed" but not "mcp is installed and too old" — 1.x ships the
  `mcp.server` module without `MCPServer` in it, which raises the parent
  `ImportError`. That escaped the guard and printed a raw traceback on exactly
  the upgrade this release requires. The guard now catches `ImportError`, so
  both cases exit 1 with `[serve] missing dependency: ... Install with: pip
  install "dowse-context[mcp]"`.

### Tested
- `tests/test_mcp.py` asserts against the SDK 2.0 return shape directly.
  `MCPServer.call_tool` returns a `CallToolResult`, so the helper parses the
  `TextContent` blocks off `result.content` instead of branching over the dict
  / tuple / list shapes the 1.x line could return.
- A regression test stubs an `mcp.server` without `MCPServer` (the 1.x shape)
  and asserts `dowse serve` exits 1 with the install hint. It fails against the
  old `ModuleNotFoundError` guard.

## [0.2.7] - 2026-07-28

### Fixed
- **Pi session extension failure warnings:** `pi-extension.ts` now surfaces the
  hook's `detail` (the underlying indexing exception) in the "dowse index
  failed" notification instead of the opaque `index_failed` reason, falling
  back to `reason` when `detail` is absent, non-string, or blank, and clipping
  long details so the notification stays readable.

### Tested
- Node `node:test` regression suite for the Pi extension's session-start
  notifications (`skills/dowse-cli/pi-extension.test.ts`, run with
  `node --test "skills/dowse-cli/*.test.ts"`), covering error-detail fallback,
  malformed payloads, skipped/success behavior, and detail clipping. Runs in
  the CI and Release workflows alongside ruff and pytest.

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
