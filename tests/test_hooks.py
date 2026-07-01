"""Opt-in Cursor sessionStart hooks (issues #4, #19)."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import dowse.cli as cli
import dowse.bootstrap as bootstrap
import dowse.cursor_hooks as cursor_hooks
import dowse.service as service

runner = CliRunner()

DOWSE_HOOK_CMD = "dowse hook session-start"


def test_install_creates_hooks_json(tmp_path: Path) -> None:
    cursor_dir = tmp_path / ".cursor"
    result = cursor_hooks.install_cursor_session_hook(cursor_dir=cursor_dir)

    hooks_path = cursor_dir / "hooks.json"
    assert hooks_path.is_file()
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    entries = data["hooks"]["sessionStart"]
    assert any(e.get("command") == DOWSE_HOOK_CMD for e in entries)
    assert result["created"] is True
    assert result["merged"] is False
    assert result["target"] == "cursor"


def test_install_merges_preserving_other_hooks(tmp_path: Path) -> None:
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    existing = {
        "version": 1,
        "hooks": {
            "sessionStart": [{"command": "echo hello"}],
            "stop": [{"command": "echo bye"}],
        },
    }
    (cursor_dir / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")

    result = cursor_hooks.install_cursor_session_hook(cursor_dir=cursor_dir)

    data = json.loads((cursor_dir / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"]["stop"][0]["command"] == "echo bye"
    starts = data["hooks"]["sessionStart"]
    assert any(e["command"] == "echo hello" for e in starts)
    assert sum(1 for e in starts if e["command"] == DOWSE_HOOK_CMD) == 1
    assert result["created"] is False
    assert result["merged"] is True


def test_install_idempotent(tmp_path: Path) -> None:
    cursor_dir = tmp_path / ".cursor"
    cursor_hooks.install_cursor_session_hook(cursor_dir=cursor_dir)
    cursor_hooks.install_cursor_session_hook(cursor_dir=cursor_dir)

    data = json.loads((cursor_dir / "hooks.json").read_text(encoding="utf-8"))
    dowse_entries = [e for e in data["hooks"]["sessionStart"] if e["command"] == DOWSE_HOOK_CMD]
    assert len(dowse_entries) == 1


def test_session_start_skips_without_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cursor_hooks.run_session_start_index()
    assert result["status"] == "skipped"
    assert result["reason"] == "no_opted_in_workspace"


def test_session_start_skips_when_index_is_fresh(sample_repo: Path, monkeypatch) -> None:
    db = sample_repo / ".dowse_index"
    service.run_index(path=sample_repo, db=db, log=lambda _m: None)
    monkeypatch.chdir(sample_repo)

    def fail_if_called(**_kwargs):
        raise AssertionError("fresh session hook should not reindex")

    monkeypatch.setattr(service, "run_index", fail_if_called)

    result = cursor_hooks.run_session_start_index(db_rel=".dowse_index")

    assert result["status"] == "skipped"
    assert result["reason"] == "index_fresh"
    assert result["indexed_symbols"] == 8


def test_session_start_indexes_when_dowse_index_is_stale(
    sample_repo: Path, monkeypatch
) -> None:
    db = sample_repo / ".dowse_index"
    service.run_index(path=sample_repo, db=db, log=lambda _m: None)
    (sample_repo / "pkg" / "auth.py").write_text(
        "def login(user):\n    return user\n\ndef logout(user):\n    return user\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(sample_repo)

    result = cursor_hooks.run_session_start_index(db_rel=".dowse_index")

    assert result["status"] == "ok"
    assert result["indexed_symbols"] >= 0


def test_session_start_does_not_status_scan_unsafe_home_root(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".dowse_index").mkdir()
    monkeypatch.chdir(fake_home)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def fail_if_called(**_kwargs):
        raise AssertionError("unsafe roots should fail before status scans")

    monkeypatch.setattr(service, "run_index_status", fail_if_called)

    result = cursor_hooks.run_session_start_index(db_rel=".dowse_index")

    assert result["status"] == "error"
    assert result["reason"] == "index_failed"
    assert "refusing to index" in result["detail"]


def test_init_without_auto_index_does_not_touch_hooks(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cursor_dir = fake_home / ".cursor"
    monkeypatch.setattr(cursor_hooks, "default_cursor_dir", lambda: cursor_dir)

    bootstrap.run_init(root=tmp_path, db=tmp_path / ".dowse_index", skip_index=True)

    assert not (cursor_dir / "hooks.json").exists()


def test_init_auto_index_installs_hook(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cursor_dir = fake_home / ".cursor"
    monkeypatch.setattr(cursor_hooks, "default_cursor_dir", lambda: cursor_dir)

    result = bootstrap.run_init(
        root=tmp_path,
        db=tmp_path / ".dowse_index",
        skip_index=True,
        auto_index=True,
    )

    assert (cursor_dir / "hooks.json").is_file()
    assert result["auto_index"]["installed"] is True
    assert result["auto_index"]["target"] == "cursor"


def test_cli_hook_install_emits_json(tmp_path: Path, monkeypatch) -> None:
    cursor_dir = tmp_path / ".cursor"
    monkeypatch.setattr(cursor_hooks, "default_cursor_dir", lambda: cursor_dir)

    r = runner.invoke(cli.app, ["hook", "install"])
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "ok"
    assert out["hook"]["target"] == "cursor"


def test_cli_init_auto_index_flag(tmp_path: Path, monkeypatch) -> None:
    cursor_dir = tmp_path / "home" / ".cursor"
    monkeypatch.setattr(cursor_hooks, "default_cursor_dir", lambda: cursor_dir)

    r = runner.invoke(
        cli.app,
        ["init", str(tmp_path), "--skip-index", "--auto-index"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["auto_index"]["installed"] is True
    assert (cursor_dir / "hooks.json").is_file()