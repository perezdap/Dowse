"""Indexing must ignore non-code/agent files: .gitignore'd paths and agent docs.

These exercise the public index path (`service.run_index`) and the
`walk_directory` chokepoint that index, staleness, and coverage all share.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import dowse.service as service
from conftest import _symbol_names
from dowse.extract import walk_directory

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True,
                   capture_output=True)


@requires_git
def test_index_skips_gitignored_source(tmp_path: Path) -> None:
    """A .py file matched by .gitignore is never extracted/indexed."""
    repo = tmp_path / "repo"
    (repo / "vendor").mkdir(parents=True)
    (repo / "app.py").write_text("def kept_symbol():\n    return 1\n")
    (repo / "vendor" / "gen.py").write_text("def ignored_symbol():\n    return 2\n")
    (repo / ".gitignore").write_text("vendor/\n")
    _git_repo(repo)

    db = tmp_path / "idx"
    service.run_index(path=repo, db=db, reset=True)

    names = _symbol_names(db)
    assert "kept_symbol" in names
    assert "ignored_symbol" not in names


@requires_git
def test_walk_directory_respects_gitignore(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "build").mkdir(parents=True)
    (repo / "keep.py").write_text("x = 1\n")
    (repo / "build" / "out.py").write_text("y = 2\n")
    (repo / ".gitignore").write_text("build/\n")
    _git_repo(repo)

    found = {p.relative_to(repo).as_posix() for p in walk_directory(repo)}
    assert "keep.py" in found
    assert "build/out.py" not in found


@requires_git
def test_walk_directory_handles_non_ascii_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "café.py").write_text("def crème():\n    return 1\n")
    _git_repo(repo)

    found = {p.relative_to(repo).as_posix() for p in walk_directory(repo)}
    assert "café.py" in found


def test_non_git_tree_indexes_normally(tmp_path: Path) -> None:
    """Without a git repo we degrade gracefully and index everything as before."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 1\n")

    db = tmp_path / "idx"
    service.run_index(path=repo, db=db, reset=True)
    assert "alpha" in _symbol_names(db)


def test_agent_docs_skipped_even_with_definitions(tmp_path: Path) -> None:
    """AGENTS.md / CLAUDE.md are agent-only instructions, never indexed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# AGENTS\n\nDo not index me.\n")
    (repo / "CLAUDE.md").write_text("# Claude\n\n## Rules\nNope.\n")
    (repo / "README.md").write_text("# Real Docs\n\n## Install\nDo this.\n")

    db = tmp_path / "idx"
    service.run_index(path=repo, db=db, reset=True, definitions=True)

    found = {p.name for p in walk_directory(repo, exts={".md"})}
    assert "README.md" in found
    assert "AGENTS.md" not in found
    assert "CLAUDE.md" not in found
