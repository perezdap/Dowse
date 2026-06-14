"""Integration tests for index/query pipeline (stub embedder, no model download)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import zvec
from typer.testing import CliRunner

import dowse.cli as cli
from dowse.store import Store

runner = CliRunner()


def _doc_count(db: str | Path) -> int:
    store = Store.open(db)
    return store.count()


def _symbol_names(db: str | Path) -> list[str]:
    c = zvec.open(str(db))
    dim = c.schema.vectors[0].dimension
    unit = [1.0 / (dim ** 0.5)] * dim
    docs = c.query(
        queries=zvec.Query(field_name="embedding", vector=unit),
        topk=10_000,
    )
    return sorted(dict(d.fields)["symbol_name"] for d in docs)


def _symbols_for_file(db: str | Path, file_path: str) -> list[str]:
    c = zvec.open(str(db))
    dim = c.schema.vectors[0].dimension
    unit = [1.0 / (dim ** 0.5)] * dim
    docs = c.query(
        queries=zvec.Query(field_name="embedding", vector=unit),
        topk=10_000,
        filter=f"file_path = '{file_path}'",
    )
    return sorted(dict(d.fields)["symbol_name"] for d in docs)


def test_index_and_query(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr
    summary = json.loads(r.stdout)
    assert summary["status"] == "ok"
    assert summary["indexed_symbols"] == 8

    r = runner.invoke(
        cli.app,
        ["query", "how do I authenticate a user and get a token", "--db", str(db_path)],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    top = out["results"][0]
    assert top["symbol_name"] in ("login", "make_token")

    r = runner.invoke(
        cli.app,
        [
            "query",
            "RuntimeError: connection pool exhausted in Connection.query",
            "--db",
            str(db_path),
        ],
    )
    out = json.loads(r.stdout)
    assert any(res["symbol_name"] == "Connection.query" for res in out["results"])

    r = runner.invoke(
        cli.app,
        ["query", "manage sessions", "--db", str(db_path), "--kind", "class"],
    )
    out = json.loads(r.stdout)
    assert all(res["kind"] == "class" for res in out["results"])


def test_reindex_idempotent(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr

    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path)])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _doc_count(db_path) == 8


def test_reconcile_edited_file(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr

    (sample_repo / "pkg" / "auth.py").write_text(
        'def signin(user, password):\n'
        '    return make_token(user)\n'
        '\n'
        'class SessionManager:\n'
        '    def revoke(self, token):\n'
        '        pass\n'
    )
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path)])
    assert r.exit_code == 0, r.stdout + r.stderr

    names = _symbol_names(db_path)
    assert "signin" in names
    assert "make_token" not in names
    assert "login" not in names
    assert "Connection.query" in names


def test_reconcile_empty_file(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _symbols_for_file(db_path, "pkg/auth.py")

    (sample_repo / "pkg" / "auth.py").write_text("# no symbols here\n")
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path)])
    assert r.exit_code == 0, r.stdout + r.stderr

    assert _symbols_for_file(db_path, "pkg/auth.py") == []
    names = _symbol_names(db_path)
    assert "login" not in names
    assert "Connection.query" in names


def test_reconcile_deleted_file(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Connection.query" in _symbol_names(db_path)

    (sample_repo / "pkg" / "db.py").unlink()
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path)])
    assert r.exit_code == 0, r.stdout + r.stderr

    names = _symbol_names(db_path)
    assert "Connection.query" not in names
    assert "connect" not in names
    assert "login" in names
