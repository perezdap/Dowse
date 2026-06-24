"""CI wheel smoke workflow validation (issue #18)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_includes_wheel_smoke_job() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "wheel-smoke" in content
    assert "python -m build" in content
    assert "dowse --help" in content
    assert "dowse serve --help" in content
    assert "dowse status" in content