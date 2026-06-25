"""Packaging metadata and install docs for release readiness (issue #13)."""
from __future__ import annotations

import re
from pathlib import Path

import dowse

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


def test_pyproject_distribution_name_is_dowse_context() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "dowse-context"' in pyproject


def test_import_package_version_matches_project_version() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)

    assert match is not None
    assert dowse.__version__ == match.group(1)


def test_readme_separates_user_and_development_installs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "### End-user install" in readme
    assert "### Development" in readme
    assert "pip install dowse-context" in readme
    assert 'pip install -e ".[dev]"' in readme


def test_readme_documents_query_token_savings_report() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "dowse query \"retry with backoff\" --tokens" in readme
    assert '"token_savings"' in readme
    assert "regex-v1" in readme
    assert "full files containing the returned snippets" in readme


def test_readme_documents_large_index_cleanup_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert ">100k-symbol" in readme
    assert "--reset" in readme


def test_readme_documents_pi_init_preset() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "dowse init ./my_project --harness pi" in readme
    assert "directTools" in readme
    assert "pi-mcp-adapter" in readme
    assert "Pi core does not include MCP" in readme


def test_readme_documents_global_install_with_pipx_and_uv() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pipx install" in readme
    assert 'pipx install "dowse-context[mcp,all-langs]"' in readme
    assert "uv tool install" in readme
    assert 'uv tool install "dowse-context[mcp,all-langs]"' in readme


def test_readme_documents_core_vs_optional_languages_near_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    install_section = readme.split("### Development", 1)[0]
    assert "Python" in install_section
    assert "PowerShell" in install_section
    assert "C#" in install_section
    assert "all-langs" in install_section
    assert "optional" in install_section.lower() or "Optional" in install_section


def test_security_and_local_offline_notes_are_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    security = ROOT / "SECURITY.md"

    assert "## Local/offline behavior" in readme
    assert security.is_file()
    assert "MCP" in security.read_text(encoding="utf-8")


def test_docs_do_not_repeat_distribution_name() -> None:
    checked = [
        ROOT / "README.md",
        ROOT / "tests" / "test_mcp.py",
    ]

    for path in checked:
        assert "dowse-context-context" not in path.read_text(encoding="utf-8")
