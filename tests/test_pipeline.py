"""Integration tests for index/query pipeline (stub embedder, no model download)."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
import zvec
from typer.testing import CliRunner

import dowse.cli as cli
import dowse.service as service
from conftest import _symbol_docs, _symbol_names
from dowse.server_lock import acquire_server_lock
from dowse.store import Store

runner = CliRunner()
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")


def _approx_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


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


def test_query_token_report_compares_snippets_to_containing_files(
    sample_repo: Path, db_path: Path
) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    payload = service.run_query(
        "connection pool exhausted",
        db=db_path,
        top=1,
        root=sample_repo,
        include_token_report=True,
    )

    report = payload["token_savings"]
    result = payload["results"][0]
    full_file = (sample_repo / result["file_path"]).read_text()
    snippet_tokens = _approx_tokens(result["code_content"])
    full_file_tokens = _approx_tokens(full_file)

    assert report["estimator"] == "regex-v1"
    assert report["snippet_tokens"] == snippet_tokens
    assert report["full_file_tokens"] == full_file_tokens
    assert report["saved_tokens"] == full_file_tokens - snippet_tokens
    assert report["reduction_percent"] == round(
        (full_file_tokens - snippet_tokens) / full_file_tokens * 100,
        2,
    )
    assert report["results"] == [
        {
            "rank": result["rank"],
            "file_path": result["file_path"],
            "symbol_name": result["symbol_name"],
            "snippet_tokens": snippet_tokens,
        }
    ]
    assert report["files"] == [
        {"file_path": result["file_path"], "full_file_tokens": full_file_tokens}
    ]


def test_build_filter_accepts_known_shortcut_filters() -> None:
    sql_filter = service.build_filter(
        "file_path LIKE 'src/%'",
        kind="section",
        lang="msbuild",
    )

    assert (
        sql_filter
        == "(file_path LIKE 'src/%') AND kind = 'section' AND language = 'msbuild'"
    )


def test_build_filter_rejects_invalid_shortcut_filters() -> None:
    with pytest.raises(ValueError, match="invalid kind"):
        service.build_filter(None, kind="function' OR 1=1 --", lang=None)

    with pytest.raises(ValueError, match="invalid language"):
        service.build_filter(None, kind=None, lang="python' OR 1=1 --")


def test_cli_query_rejects_invalid_shortcut_filter_without_traceback(
    sample_repo: Path, db_path: Path
) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    r = runner.invoke(
        cli.app,
        [
            "query",
            "manage sessions",
            "--db",
            str(db_path),
            "--kind",
            "function' OR 1=1 --",
        ],
    )

    assert r.exit_code == 2
    assert r.stdout == ""
    assert "invalid kind" in r.stderr
    assert "Traceback" not in r.stderr


def test_query_token_report_handles_missing_containing_files(
    sample_repo: Path, db_path: Path
) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)
    (sample_repo / "pkg" / "db.py").unlink()

    payload = service.run_query(
        "connection pool exhausted",
        db=db_path,
        top=1,
        root=sample_repo,
        include_token_report=True,
    )

    report = payload["token_savings"]
    result = payload["results"][0]
    snippet_tokens = _approx_tokens(result["code_content"])

    assert report["snippet_tokens"] == snippet_tokens
    assert report["full_file_tokens"] == 0
    assert report["saved_tokens"] == 0
    assert report["reduction_percent"] == 0.0
    assert report["files"] == []
    assert report["unavailable_files"] == [
        {"file_path": result["file_path"], "reason": "not_found"}
    ]


def test_cli_query_tokens_flag_emits_token_savings_report(
    sample_repo: Path, db_path: Path
) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    r = runner.invoke(
        cli.app,
        [
            "query",
            "connection pool exhausted",
            "--db",
            str(db_path),
            "--top",
            "1",
            "--root",
            str(sample_repo),
            "--tokens",
        ],
    )

    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stderr == ""
    out = json.loads(r.stdout)
    assert out["results"]
    assert out["token_savings"]["estimator"] == "regex-v1"
    assert out["token_savings"]["snippet_tokens"] > 0
    assert out["token_savings"]["full_file_tokens"] > out["token_savings"]["snippet_tokens"]
    assert out["token_savings"]["saved_tokens"] > 0
    assert out["token_savings"]["reduction_percent"] > 0


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


def test_existing_yaml_and_markdown_definition_sections_still_index(
    sample_repo: Path, db_path: Path
) -> None:
    (sample_repo / "package.yaml").write_text(
        'name: 7zip\n'
        'install:\n'
        '  command: setup.exe /S\n'
        'uninstall:\n'
        '  command: uninstall.exe /S\n'
    )
    (sample_repo / "definition.md").write_text(
        '# Google Chrome\n'
        '## Install\n'
        '### Pre-Install\n'
        'Stop running browser processes.\n'
    )

    r = runner.invoke(
        cli.app,
        ["index", str(sample_repo), "--db", str(db_path), "--reset", "--definitions"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    docs = _symbol_docs(db_path)
    assert any(
        d["language"] == "yaml"
        and d["kind"] == "section"
        and d["symbol_name"] == "7zip.install"
        and "setup.exe /S" in d["code_content"]
        for d in docs
    )
    assert any(
        d["language"] == "markdown"
        and d["kind"] == "section"
        and d["symbol_name"] == "Google Chrome.Install.Pre-Install"
        and "Stop running browser processes." in d["code_content"]
        for d in docs
    )


def test_dotnet_project_files_are_indexed_as_definition_sections(
    sample_repo: Path, db_path: Path
) -> None:
    (sample_repo / "src").mkdir()
    (sample_repo / "src" / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net8.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <PackageReference Include="Microsoft.Extensions.Logging" Version="8.0.0" />\n'
        '    <ProjectReference Include="..\\Shared\\Shared.csproj" />\n'
        '  </ItemGroup>\n'
        '</Project>\n'
    )

    r = runner.invoke(
        cli.app,
        ["index", str(sample_repo), "--db", str(db_path), "--reset", "--definitions"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    docs = [d for d in _symbol_docs(db_path) if d["file_path"] == "src/App.csproj"]
    assert {d["kind"] for d in docs} == {"section"}
    assert {d["language"] for d in docs} == {"msbuild"}
    assert any(d["symbol_name"] == "App.PropertyGroup.TargetFramework.Nullable" for d in docs)
    assert any(
        d["symbol_name"]
        == "App.ItemGroup.PackageReference.Microsoft Extensions Logging.ProjectReference.Shared"
        for d in docs
    )

    r = runner.invoke(
        cli.app,
        [
            "query",
            "logging package reference",
            "--db",
            str(db_path),
            "--kind",
            "section",
            "--lang",
            "msbuild",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert any(
        '<PackageReference Include="Microsoft.Extensions.Logging"' in res["code_content"]
        for res in out["results"]
    )


def test_msbuild_props_and_targets_are_indexed_as_definition_sections(
    sample_repo: Path, db_path: Path
) -> None:
    (sample_repo / "Directory.Build.props").write_text(
        '<Project>\n'
        '  <PropertyGroup>\n'
        '    <ManagePackageVersionsCentrally>true</ManagePackageVersionsCentrally>\n'
        '    <VersionPrefix>1.2.3</VersionPrefix>\n'
        '  </PropertyGroup>\n'
        '</Project>\n'
    )
    (sample_repo / "Custom.targets").write_text(
        '<Project>\n'
        '  <Target Name="GenerateVersion" BeforeTargets="BeforeBuild">\n'
        '    <Message Importance="high" Text="Generating version" />\n'
        '    <WriteLinesToFile File="$(IntermediateOutputPath)version.txt" Lines="$(VersionPrefix)" />\n'
        '  </Target>\n'
        '</Project>\n'
    )

    r = runner.invoke(
        cli.app,
        ["index", str(sample_repo), "--db", str(db_path), "--reset", "--definitions"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    docs = [d for d in _symbol_docs(db_path) if d["language"] == "msbuild"]
    assert any(
        d["file_path"] == "Directory.Build.props"
        and d["symbol_name"]
        == "Directory Build.PropertyGroup.ManagePackageVersionsCentrally.VersionPrefix"
        for d in docs
    )
    assert any(
        d["file_path"] == "Custom.targets"
        and d["symbol_name"] == "Custom.Target.GenerateVersion.Message.WriteLinesToFile"
        and "BeforeTargets=\"BeforeBuild\"" in d["code_content"]
        for d in docs
    )


def test_malformed_msbuild_files_do_not_crash_and_emit_best_effort_sections(
    sample_repo: Path, db_path: Path
) -> None:
    (sample_repo / "Broken.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net8.0</TargetFramework>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <PackageReference Include="Serilog" Version="3.1.1" />\n'
    )

    r = runner.invoke(
        cli.app,
        ["index", str(sample_repo), "--db", str(db_path), "--reset", "--definitions"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stderr

    docs = [d for d in _symbol_docs(db_path) if d["file_path"] == "Broken.csproj"]
    assert any(d["symbol_name"] == "Broken.PropertyGroup.TargetFramework" for d in docs)
    assert any(
        d["symbol_name"] == "Broken.ItemGroup.PackageReference.Serilog"
        and '<PackageReference Include="Serilog"' in d["code_content"]
        for d in docs
    )


def test_reindex_idempotent(sample_repo: Path, db_path: Path) -> None:
    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path), "--reset"])
    assert r.exit_code == 0, r.stdout + r.stderr

    r = runner.invoke(cli.app, ["index", str(sample_repo), "--db", str(db_path)])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _doc_count(db_path) == 8


def test_index_skips_default_dowse_index_directory(sample_repo: Path, db_path: Path) -> None:
    index_dir = sample_repo / ".dowse_index"
    index_dir.mkdir()
    (index_dir / "shadow.py").write_text("def should_not_index():\n    pass\n")

    summary = service.run_index(path=sample_repo, db=db_path, reset=True)

    assert summary["indexed_files"] == 2
    assert "should_not_index" not in _symbol_names(db_path)


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


def test_run_index_refuses_home_directory(tmp_path: Path, monkeypatch) -> None:
    # Indexing the user's home directory would walk the entire home tree.
    home = tmp_path / "myhome"
    home.mkdir()
    (home / "pkg.py").write_text("def f():\n    pass\n")
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(service.UnsafeRootError):
        service.run_index(path=home, db=tmp_path / "idx")


def test_run_index_refuses_ancestor_of_home(tmp_path: Path, monkeypatch) -> None:
    # Indexing a parent of home (e.g. C:\) walks home and far more.
    home = tmp_path / "myhome"
    home.mkdir()
    (home / "pkg.py").write_text("def f():\n    pass\n")
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(service.UnsafeRootError):
        service.run_index(path=tmp_path, db=tmp_path / "idx")


def test_run_index_force_overrides_home_guard(sample_repo: Path, db_path: Path, monkeypatch) -> None:
    # Treat the repo itself as home -> normally refused, but --force allows it.
    monkeypatch.setattr(Path, "home", lambda: sample_repo)

    with pytest.raises(service.UnsafeRootError):
        service.run_index(path=sample_repo, db=db_path)

    r = service.run_index(path=sample_repo, db=db_path, force=True)
    assert r["status"] == "ok"


def test_run_init_refuses_home_before_writing(tmp_path: Path, monkeypatch) -> None:
    # `dowse init $HOME` must refuse BEFORE creating .mcp.json/.gitignore under home.
    home = tmp_path / "myhome"
    home.mkdir()
    (home / "pkg.py").write_text("def f():\n    pass\n")
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(service.UnsafeRootError):
        service.run_init(root=home, db=home / ".dowse_index")

    # No config files written to home on refusal.
    assert not (home / ".mcp.json").exists()
    assert not (home / ".gitignore").exists()


def test_run_init_skip_index_allows_home(tmp_path: Path, monkeypatch) -> None:
    # --skip-index never walks the tree, so home is fine for config-only init.
    home = tmp_path / "myhome"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    payload = service.run_init(root=home, skip_index=True, log=lambda _m: None)
    assert payload["status"] == "ok"
    assert (home / ".mcp.json").exists()


def test_cli_index_refuses_home_with_clear_error(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "myhome"
    home.mkdir()
    (home / "pkg.py").write_text("def f():\n    pass\n")
    monkeypatch.setattr(Path, "home", lambda: home)

    r = runner.invoke(cli.app, ["index", str(home), "--db", str(tmp_path / "idx")])
    assert r.exit_code == 1
    assert r.stdout == ""
    assert "home directory" in r.stderr.lower() or "unsafe" in r.stderr.lower()
    assert "Traceback" not in r.stderr

    # --force overrides the guard and indexes successfully.
    r = runner.invoke(cli.app, ["index", str(home), "--db", str(tmp_path / "idx"), "--force"])
    assert r.exit_code == 0, r.stdout + r.stderr


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
