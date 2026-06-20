# Changelog

All notable changes to **dowse** are documented here. Dates are in UTC.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
