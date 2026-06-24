"""Packaging metadata and install docs for release readiness (issue #13)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_includes_release_metadata() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'readme = "README.md"' in pyproject
    assert 'license = { file = "LICENSE" }' in pyproject
    assert "authors = [" in pyproject
    assert "maintainers = [" in pyproject
    assert "keywords = [" in pyproject
    assert "classifiers = [" in pyproject
    assert "[project.urls]" in pyproject
    assert 'Programming Language :: Python :: 3.10' in pyproject
    assert 'Programming Language :: Python :: 3.11' in pyproject
    assert 'Programming Language :: Python :: 3.12' in pyproject


def test_readme_separates_user_and_development_installs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "### End-user install" in readme
    assert "### Development" in readme
    assert "pip install dowse" in readme
    assert 'pip install -e ".[dev]"' in readme


def test_readme_documents_query_token_savings_report() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "dowse query \"retry with backoff\" --tokens" in readme
    assert '"token_savings"' in readme
    assert "regex-v1" in readme
    assert "full files containing the returned snippets" in readme
