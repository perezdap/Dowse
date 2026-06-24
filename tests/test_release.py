"""Release workflow validation (issue #14).

Asserts the GitHub Actions release workflow exists, triggers on tags (not
ordinary PRs), builds wheel+sdist with python -m build, validates with
twine check, and publishes to TestPyPI and PyPI via Trusted Publishing (OIDC).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_workflow_exists() -> None:
    assert WORKFLOW.is_file(), f"Expected release workflow at {WORKFLOW}"


def test_release_workflow_triggers_on_tags_not_prs() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    # Must trigger on tag pushes (release trigger).
    assert "on:" in content
    assert "tags" in content
    # Must NOT publish on ordinary pull requests — no pull_request trigger.
    assert "pull_request" not in content


def test_release_workflow_builds_wheel_and_sdist() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m build" in content


def test_release_workflow_validates_with_twine_check() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "twine check dist/*" in content


def test_release_workflow_uses_trusted_publishing() -> None:
    """Publishing must use OIDC Trusted Publishing, not API tokens."""
    content = WORKFLOW.read_text(encoding="utf-8")
    # pypa/gh-action-pypi-publish supports Trusted Publishing via
    # the `permissions: id-token: write` + `with: skip-existing` pattern.
    assert "id-token" in content
    assert "pypi-publish" in content
    # Must NOT use API tokens.
    assert "pypi_token" not in content.lower()
    assert "pypi_password" not in content.lower()


def test_release_workflow_has_testpypi_environment() -> None:
    """TestPyPI publishing path exists for release rehearsals."""
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "testpypi" in content.lower()


def test_release_workflow_gates_pypi_on_testpypi_success() -> None:
    """The real PyPI publish job must depend on the TestPyPI job."""
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "testpypi" in content.lower()
    assert "pypi" in content.lower()
    assert "needs:" in content
    assert "publish-pypi" in content
    assert "needs: publish-testpypi" in content or "needs:\n      - publish-testpypi" in content


def test_release_workflow_publish_jobs_use_linux() -> None:
    """gh-action-pypi-publish requires GNU/Linux (Trusted Publishing)."""
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "publish-testpypi" in content
    idx = content.index("publish-testpypi")
    publish_section = content[idx : idx + 900]
    assert "ubuntu-latest" in publish_section
    assert "windows-latest" not in publish_section.split("publish-pypi")[0]
