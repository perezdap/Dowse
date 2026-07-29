"""Indexing must ignore non-code/agent files: .gitignore'd paths and agent docs.

These exercise the public index path (`service.run_index`) and the
`walk_directory` chokepoint that index, staleness, and coverage all share.
"""
from __future__ import annotations

import os
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


def _git_commit(root: Path, msg: str = "init") -> None:
    """Commit with a throwaway identity so tracked-file tests can force-add."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(root), "commit", "-m", msg, "--quiet"],
                   check=True, capture_output=True, env=env)


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


@requires_git
def test_dowseignore_excludes_tracked_file_gitignore_cannot_reach(tmp_path: Path) -> None:
    """A tracked file matching a .gitignore pattern is NOT reported by
    `git check-ignore` (git ignore applies to untracked files), so dowse's
    gitignore pass keeps it — the gap `.dowseignore` closes.
    """
    repo = tmp_path / "repo"
    (repo / "knowledge").mkdir(parents=True)
    (repo / "app.py").write_text("def kept_symbol():\n    return 1\n")
    (repo / "knowledge" / "gen.py").write_text("def ignored_symbol():\n    return 2\n")
    (repo / ".gitignore").write_text("knowledge/\n")
    _git_repo(repo)
    # Force-track the ignored file (committed-before-ignore or `git add -f`),
    # so .gitignore cannot exclude it via git check-ignore.
    subprocess.run(["git", "-C", str(repo), "add", "-f", "knowledge/gen.py", "app.py"],
                   check=True, capture_output=True)
    _git_commit(repo)

    db = tmp_path / "idx"
    # Without .dowseignore, the tracked-but-gitignored file IS indexed: this is
    # the gap (gitignore can't reach a tracked file).
    service.run_index(path=repo, db=db, reset=True)
    names = _symbol_names(db)
    assert "kept_symbol" in names
    assert "ignored_symbol" in names, (
        "precondition: tracked+gitignored file is indexed without .dowseignore"
    )

    # Adding .dowseignore closes the gap without touching .gitignore or tracking.
    (repo / ".dowseignore").write_text("knowledge/\n")
    service.run_index(path=repo, db=db, reset=True)
    names = _symbol_names(db)
    assert "kept_symbol" in names
    assert "ignored_symbol" not in names


def test_dowseignore_anchored_glob_excludes_top_level_only(tmp_path: Path) -> None:
    """A leading-slash pattern is anchored to the index root: /knowledge/
    drops the top-level dir but leaves src/knowledge/ intact."""
    repo = tmp_path / "repo"
    (repo / "knowledge").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "knowledge").mkdir(parents=True)
    (repo / "knowledge" / "top.py").write_text("def top():\n    return 1\n")
    (repo / "src" / "knowledge" / "nested.py").write_text("def nested():\n    return 2\n")
    (repo / ".dowseignore").write_text("/knowledge/\n")

    found = {p.relative_to(repo).as_posix() for p in walk_directory(repo)}
    assert "src/knowledge/nested.py" in found
    assert "knowledge/top.py" not in found


def test_dowseignore_excludes_markdown_under_definitions(tmp_path: Path) -> None:
    """Under --definitions, .dowseignore drops a markdown tree while keeping
    other markdown — the case gitignore cannot reach once the tree is tracked."""
    repo = tmp_path / "repo"
    (repo / "knowledge").mkdir(parents=True)
    (repo / "README.md").write_text("# Real\n\n## Install\nDo this.\n")
    (repo / "knowledge" / "notes.md").write_text("# Notes\n\n## Junk\nskip.\n")
    (repo / ".dowseignore").write_text("knowledge/\n")

    found = {p.name for p in walk_directory(repo, exts={".md"})}
    assert "README.md" in found
    assert "notes.md" not in found
