"""Integration tests for index/query pipeline (stub embedder, no model download)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import zvec
from typer.testing import CliRunner

import dowse.cli as cli
import dowse.service as service
from conftest import _symbol_names
from dowse.server_lock import acquire_server_lock
from dowse.store import Store

runner = CliRunner()


def _doc_count(db: str | Path) -> int:
    store = Store.open(db)
    return store.count()


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


def test_cli_query_reports_locked_index_without_traceback(sample_repo: Path, db_path: Path) -> None:
    service_result = runner.invoke(
        cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"]
    )
    assert service_result.exit_code == 0, service_result.stdout + service_result.stderr

    held_open = Store.open(db_path)
    try:
        r = runner.invoke(
            cli.app,
            ["query", "how do I authenticate a user", "--db", str(db_path)],
        )
    finally:
        del held_open

    assert r.exit_code == 1
    assert r.stdout == ""
    assert "index is already open" in r.stderr
    assert "dowse serve" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_reset_reports_locked_index_without_traceback(sample_repo: Path, db_path: Path) -> None:
    service_result = runner.invoke(
        cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"]
    )
    assert service_result.exit_code == 0, service_result.stdout + service_result.stderr

    held_open = Store.open(db_path)
    try:
        r = runner.invoke(
            cli.app,
            ["index", str(sample_repo), "--db", str(db_path), "--reset"],
        )
    finally:
        del held_open

    assert r.exit_code == 1
    assert r.stdout == ""
    assert "index is already open" in r.stderr
    assert "dowse serve" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_query_allows_another_readonly_user(sample_repo: Path, db_path: Path) -> None:
    service_result = runner.invoke(
        cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"]
    )
    assert service_result.exit_code == 0, service_result.stdout + service_result.stderr

    held_readonly = zvec.open(str(db_path), zvec.CollectionOption(read_only=True))
    try:
        r = runner.invoke(
            cli.app,
            ["query", "how do I authenticate a user", "--db", str(db_path)],
        )
    finally:
        del held_readonly

    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["results"]


def test_cli_status_allows_another_readonly_user(sample_repo: Path, db_path: Path) -> None:
    service_result = runner.invoke(
        cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"]
    )
    assert service_result.exit_code == 0, service_result.stdout + service_result.stderr

    held_readonly = zvec.open(str(db_path), zvec.CollectionOption(read_only=True))
    try:
        r = runner.invoke(
            cli.app,
            ["status", "--db", str(db_path), "--root", str(sample_repo)],
        )
    finally:
        del held_readonly

    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["exists"] is True
    assert out["indexed_symbols"] == 8


def test_server_lock_is_exclusive_without_creating_index_dir(db_path: Path) -> None:
    lock = acquire_server_lock(db_path)
    try:
        assert not db_path.exists()
        r = runner.invoke(cli.app, ["serve", "--db", str(db_path)])
    finally:
        lock.release()

    assert r.exit_code == 1
    assert r.stdout == ""
    assert "another dowse serve is already running" in r.stderr
    assert str(db_path) in r.stderr
    assert "Traceback" not in r.stderr

    second = acquire_server_lock(db_path)
    second.release()


def test_cli_serve_refuses_to_start_when_index_writer_is_active(
    sample_repo: Path, db_path: Path
) -> None:
    service_result = runner.invoke(
        cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"]
    )
    assert service_result.exit_code == 0, service_result.stdout + service_result.stderr

    held_write = Store.open(db_path)
    try:
        r = runner.invoke(cli.app, ["serve", "--db", str(db_path)])
    finally:
        del held_write

    assert r.exit_code == 1
    assert r.stdout == ""
    assert "index is already open" in r.stderr
    assert "dowse serve" in r.stderr
    assert "Traceback" not in r.stderr


def test_service_serializes_concurrent_queries_for_same_index(
    sample_repo: Path, db_path: Path, monkeypatch
) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    first_query_holds_store_open = threading.Event()
    release_first_query = threading.Event()
    calls_lock = threading.Lock()
    calls = 0
    original_hybrid_query = Store.hybrid_query

    def blocking_hybrid_query(self, *args, **kwargs):
        nonlocal calls
        with calls_lock:
            first = calls == 0
            calls += 1
        if first:
            first_query_holds_store_open.set()
            assert release_first_query.wait(timeout=5)
        return original_hybrid_query(self, *args, **kwargs)

    monkeypatch.setattr(Store, "hybrid_query", blocking_hybrid_query)

    errors: list[BaseException] = []
    results: list[dict] = []

    def query() -> None:
        try:
            results.append(service.run_query("authenticate user", db=db_path))
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=query)
    t1.start()
    assert first_query_holds_store_open.wait(timeout=5)

    t2 = threading.Thread(target=query)
    t2.start()
    time.sleep(0.1)
    with calls_lock:
        assert calls == 1
    release_first_query.set()

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert errors == []
    assert len(results) == 2


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
