"""Language-extraction coverage for newly wired grammars (stub embedder)."""
from __future__ import annotations

from pathlib import Path

import zvec


def _symbol_names(db: str | Path) -> list[str]:
    c = zvec.open(str(db))
    dim = c.schema.vectors[0].dimension
    unit = [1.0 / (dim ** 0.5)] * dim
    docs = c.query(
        queries=zvec.Query(field_name="embedding", vector=unit),
        topk=10_000,
    )
    return sorted(dict(d.fields)["symbol_name"] for d in docs)


def test_index_rust(tmp_path: Path, db_path: Path) -> None:
    import dowse.service as service

    repo = tmp_path / "rustrepo"
    repo.mkdir()
    (repo / "auth.rs").write_text(
        "pub fn login(user: &str) -> Token {\n"
        "    make_token(user)\n"
        "}\n"
        "\n"
        "struct SessionManager {\n"
        "    sessions: HashMap<String, Session>,\n"
        "}\n"
        "\n"
        "trait Authenticator {\n"
        "    fn authenticate(&self, u: &str) -> bool;\n"
        "}\n"
    )

    summary = service.run_index(path=repo, db=db_path, reset=True)
    assert summary["indexed_symbols"] >= 4
    names = _symbol_names(db_path)
    assert "login" in names
    assert "SessionManager" in names
    assert "Authenticator" in names
    # trait method is qualified by its trait (Class.method style)
    assert "Authenticator.authenticate" in names


def test_index_bash(tmp_path: Path, db_path: Path) -> None:
    import dowse.service as service

    repo = tmp_path / "bashrepo"
    repo.mkdir()
    (repo / "deploy.sh").write_text(
        "#!/usr/bin/env bash\n"
        "\n"
        "login() {\n"
        "  local user=\"$1\"\n"
        "  make_token \"$user\"\n"
        "}\n"
        "\n"
        "function deploy {\n"
        "  echo \"deploying\"\n"
        "}\n"
    )

    summary = service.run_index(path=repo, db=db_path, reset=True)
    assert summary["indexed_symbols"] >= 2
    names = _symbol_names(db_path)
    assert "login" in names
    assert "deploy" in names


def test_index_logs_missing_grammar(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """Files on disk for an uninstalled grammar produce an actionable skip log."""
    import dowse.extract as extract
    import dowse.service as service

    # Pretend the Go grammar isn't installed, regardless of the real environment,
    # so the skip path is deterministic.
    patched = {k: v for k, v in extract._REGISTRY.items() if k != ".go"}
    monkeypatch.setattr(extract, "_REGISTRY", patched)

    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "app.go").write_text("package main\n\nfunc main() {}\n")
    (repo / "util.go").write_text("package main\n\nfunc helper() int { return 1 }\n")

    logs: list[str] = []
    summary = service.run_index(path=repo, db=db_path, reset=True, log=logs.append)

    # Nothing was indexed (Go grammar absent), but the run still succeeds.
    assert summary["indexed_symbols"] == 0
    skip_lines = [l for l in logs if "skip" in l.lower() and ".go" in l]
    assert skip_lines, f"expected a skip log for .go, got: {logs}"
    # Counts both .go files and points at the install extra.
    assert "2" in skip_lines[0]
    assert "dowse[go]" in skip_lines[0]


def test_scan_language_coverage_contract(tmp_path: Path, monkeypatch) -> None:
    """Coverage splits on-disk files into installed vs missing-grammar groups."""
    import dowse.extract as extract

    # Deterministic installed set: pretend only Python is available.
    py_spec = next(v for k, v in extract._REGISTRY.items() if k == ".py")
    monkeypatch.setattr(extract, "_REGISTRY", {
        ".py": py_spec, ".pyi": py_spec,
    })

    repo = tmp_path / "cov"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def f(): pass\n")
    (repo / "pkg" / "b.py").write_text("def g(): pass\n")
    (repo / "one.go").write_text("package main\n")
    (repo / "two.go").write_text("package main\n")
    (repo / "three.go").write_text("package main\n")

    cov = {c.language: c for c in extract.scan_language_coverage(repo)}

    assert set(cov) == {"python", "go"}
    assert cov["python"].installed is True
    assert cov["python"].file_count == 2
    assert cov["python"].install_hint is None

    assert cov["go"].installed is False
    assert cov["go"].file_count == 3
    assert cov["go"].extra == "go"
    assert cov["go"].install_hint == 'pip install "dowse[go]"'
