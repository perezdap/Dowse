"""dowse doctor diagnostics (issue #15)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

import dowse.cli as cli
import dowse.service as service
from dowse._dist import distribution_name
from dowse.server_lock import acquire_server_lock

runner = CliRunner()


def test_doctor_missing_index(tmp_path: Path) -> None:
    db = tmp_path / ".dowse_index"
    report = service.run_doctor(db=db, root=tmp_path)

    assert report["status"] == "ok"
    assert report["index"]["exists"] is False
    assert report["install"]["python_version"]
    assert report["install"]["dowse_module"]
    assert report["install"]["mcp_sdk"]["installed"] in (True, False)
    assert report["locks"]["serve"]["held"] is False
    assert report["locks"]["index"]["readable"] is False
    assert report["locks"]["index"]["locked"] is False


def test_doctor_healthy_index(sample_repo: Path, db_path: Path) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    report = service.run_doctor(db=db_path, root=sample_repo)

    assert report["status"] == "ok"
    assert report["index"]["exists"] is True
    assert report["index"]["indexed_symbols"] == 8
    assert report["locks"]["serve"]["held"] is False
    assert report["locks"]["index"]["readable"] is True
    assert report["locks"]["index"]["locked"] is False
    assert report["workspace"]["root"].replace("\\", "/").endswith("sample_repo")


def test_doctor_reports_serve_lock_held(sample_repo: Path, db_path: Path) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)
    lock = acquire_server_lock(db_path)
    try:
        report = service.run_doctor(db=db_path, root=sample_repo)
        assert report["locks"]["serve"]["held"] is True
        pid = report["locks"]["serve"]["holder_pid"]
        if pid is not None:
            assert pid == os.getpid()
    finally:
        lock.release()


def test_doctor_harness_mcp_json(sample_repo: Path, db_path: Path) -> None:
    (sample_repo / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dowse": {"command": "dowse", "args": ["serve", "--db", ".dowse_index"]}
                }
            }
        ),
        encoding="utf-8",
    )
    report = service.run_doctor(db=db_path, root=sample_repo)
    mcp = report["harness"]["mcp_configs"][".mcp.json"]
    assert mcp["present"] is True
    assert mcp["has_dowse_server"] is True


def test_cli_doctor_emits_json(sample_repo: Path, db_path: Path) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    r = runner.invoke(
        cli.app,
        ["doctor", "--db", str(db_path), "--root", str(sample_repo)],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "ok"
    assert out["index"]["exists"] is True


def test_doctor_mcp_sdk_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        service,
        "_mcp_sdk_info",
        lambda: {"installed": False, "version": None},
    )
    report = service.run_doctor(db=tmp_path / ".dowse_index", root=tmp_path)
    assert report["install"]["mcp_sdk"]["installed"] is False
    assert report["install"]["mcp_sdk"]["version"] is None


def test_doctor_uses_distribution_name_for_dowse_version(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        return "9.9.9"

    monkeypatch.setattr(service, "version", fake_version)

    report = service.run_doctor(db=tmp_path / ".dowse_index", root=tmp_path)

    assert calls[0] == distribution_name()
    assert report["install"]["dowse_version"] == "9.9.9"