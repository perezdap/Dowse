"""dowse init one-command bootstrap (issues #5, #16)."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import dowse.cli as cli
import dowse.extract as extract
import dowse.service as service

runner = CliRunner()


# ---------------------------------------------------------------------------
# .mcp.json creation / merge (#16)
# ---------------------------------------------------------------------------

def test_init_creates_mcp_json_in_fresh_repo(tmp_path: Path) -> None:
    """run_init writes .mcp.json with a dowse server entry when none exists."""
    result = service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    mcp_file = tmp_path / ".mcp.json"
    assert mcp_file.is_file()
    data = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "dowse" in data["mcpServers"]
    entry = data["mcpServers"]["dowse"]
    assert entry["command"] == "dowse"
    assert entry["args"] == ["serve", "--db", ".dowse_index"]
    assert result["mcp_config"]["created"] is True


def test_init_merges_existing_mcp_json_preserving_other_servers(tmp_path: Path) -> None:
    """run_init preserves unrelated MCP servers when merging."""
    existing = {
        "mcpServers": {
            "other-tool": {"command": "other", "args": ["run"]},
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(existing), encoding="utf-8")

    result = service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "other-tool" in data["mcpServers"]
    assert data["mcpServers"]["other-tool"]["command"] == "other"
    assert "dowse" in data["mcpServers"]
    assert result["mcp_config"]["created"] is False
    assert result["mcp_config"]["merged"] is True


def test_init_mcp_json_idempotent_on_rerun(tmp_path: Path) -> None:
    """Re-running init does not duplicate the dowse entry."""
    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)
    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    dowse_entries = [k for k in data["mcpServers"] if k == "dowse"]
    assert len(dowse_entries) == 1


# ---------------------------------------------------------------------------
# .gitignore (#16)
# ---------------------------------------------------------------------------

def test_init_appends_dowse_index_to_gitignore(tmp_path: Path) -> None:
    """run_init adds .dowse_index/ to .gitignore when absent."""
    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".dowse_index/" in gitignore


def test_init_gitignore_idempotent(tmp_path: Path) -> None:
    """Re-running init does not duplicate the .gitignore line."""
    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)
    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    dowse_lines = [l for l in lines if ".dowse_index/" in l]
    assert len(dowse_lines) == 1


def test_init_preserves_existing_gitignore_content(tmp_path: Path) -> None:
    """run_init appends to an existing .gitignore without clobbering it."""
    (tmp_path / ".gitignore").write_text("*.pyc\n.venv/\n", encoding="utf-8")

    service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in content
    assert ".venv/" in content
    assert ".dowse_index/" in content


# ---------------------------------------------------------------------------
# Language coverage (#5)
# ---------------------------------------------------------------------------

def test_init_reports_missing_grammars(tmp_path: Path, monkeypatch) -> None:
    """run_init reports languages present on disk but without installed grammars."""
    # Pretend the Go grammar is absent regardless of the real environment.
    patched = {k: v for k, v in extract._REGISTRY.items() if k != ".go"}
    monkeypatch.setattr(extract, "_REGISTRY", patched)

    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")

    result = service.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    missing = result.get("missing_grammars", [])
    langs = [m["language"] for m in missing]
    assert "go" in langs


# ---------------------------------------------------------------------------
# Initial index (#5)
# ---------------------------------------------------------------------------

def test_init_runs_initial_index(sample_repo: Path, db_path: Path) -> None:
    """run_init indexes the repo and returns index summary."""
    result = service.run_init(root=sample_repo, db=db_path)

    assert result["index"]["status"] == "ok"
    assert result["index"]["indexed_symbols"] > 0
    assert (db_path).exists()


# ---------------------------------------------------------------------------
# CLI (#5)
# ---------------------------------------------------------------------------

def test_cli_init_emits_json(sample_repo: Path, db_path: Path) -> None:
    """dowse init emits valid JSON on stdout."""
    r = runner.invoke(
        cli.app,
        ["init", str(sample_repo), "--db", str(db_path)],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "ok"
    assert out["index"]["status"] == "ok"
