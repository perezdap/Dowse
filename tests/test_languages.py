"""Language-extraction coverage for newly wired grammars (stub embedder)."""
from __future__ import annotations

from pathlib import Path

import dowse.extract as extract
import dowse.service as service

from conftest import _symbol_docs, _symbol_names


def test_index_rust(tmp_path: Path, db_path: Path) -> None:
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
        "enum Mode { Read, Write }\n"
        "\n"
        "trait Authenticator {\n"
        "    fn authenticate(&self, u: &str) -> bool;\n"
        "}\n"
    )

    summary = service.run_index(path=repo, db=db_path, reset=True)
    assert summary["indexed_symbols"] >= 5
    syms = {s["symbol_name"]: s["kind"] for s in _symbol_docs(db_path)}

    # Free functions and trait methods stay qualified by their enclosing def.
    assert syms["login"] == "function"
    assert syms["Authenticator.authenticate"] == "function"
    # struct / enum / trait are classes, matching the Python convention.
    assert syms["SessionManager"] == "class"
    assert syms["Mode"] == "class"
    assert syms["Authenticator"] == "class"


def test_index_rust_impl_methods_qualified(tmp_path: Path, db_path: Path) -> None:
    """Methods inside `impl Type { ... }` are qualified by their implementing
    type (Class.method), just like Python methods and Rust trait methods."""
    repo = tmp_path / "rustrepo"
    repo.mkdir()
    (repo / "lib.rs").write_text(
        "struct Foo { x: u32 }\n"
        "impl Foo {\n"
        "    fn bar(&self) -> u32 { self.x }\n"
        "    fn baz(&self) -> u32 { 0 }\n"
        "}\n"
        "impl Drop for Foo {\n"
        "    fn drop(&mut self) {}\n"
        "}\n"
    )

    service.run_index(path=repo, db=db_path, reset=True)
    names = _symbol_names(db_path)

    # Inherent impl -> Type.method.
    assert "Foo.bar" in names
    assert "Foo.baz" in names
    assert "bar" not in names  # must be qualified, not bare
    # Trait impl (`impl Trait for Type`) qualifies by the implementing TYPE.
    assert "Foo.drop" in names
    assert "Foo" in names  # the struct itself


def test_index_bash(tmp_path: Path, db_path: Path) -> None:
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


def test_index_typescript(tmp_path: Path, db_path: Path) -> None:
    repo = tmp_path / "tsrepo"
    repo.mkdir()
    (repo / "auth.ts").write_text(
        "export function login(user: string): Token {\n"
        "  return makeToken(user);\n"
        "}\n"
        "\n"
        "export class SessionManager {\n"
        "  revoke(id: string): void {\n"
        "    deleteSession(id);\n"
        "  }\n"
        "}\n"
    )

    summary = service.run_index(path=repo, db=db_path, reset=True)
    assert summary["indexed_symbols"] >= 2
    syms = {s["symbol_name"]: s["kind"] for s in _symbol_docs(db_path)}

    assert syms["login"] == "function"
    assert syms["SessionManager"] == "class"
    assert syms["SessionManager.revoke"] == "function"


def test_index_logs_missing_grammar(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """Files on disk for an uninstalled grammar produce an actionable skip log."""
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


def test_index_coverage_skipped_without_log(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """The MCP path (no log callback) must not compute coverage at all (#2)."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.go").write_text("package main\nfunc main() {}\n")
    (repo / "m.py").write_text("def f(): pass\n")

    calls = {"coverage": 0}
    real = extract.scan_language_coverage

    def spy(root, files=None):
        calls["coverage"] += 1
        return real(root, files=files)

    monkeypatch.setattr(service, "scan_language_coverage", spy)

    # No log callback -> coverage never computed (MCP `index_codebase` path).
    service.run_index(path=repo, db=db_path, reset=True)
    assert calls["coverage"] == 0

    # With a log callback -> coverage computed exactly once.
    calls["coverage"] = 0
    service.run_index(path=repo, db=db_path, reset=True, log=lambda _m: None)
    assert calls["coverage"] == 1


def test_index_single_directory_walk(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """run_index walks the tree once even when computing coverage (#4)."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "m.py").write_text("def f(): pass\n")
    (repo / "app.go").write_text("package main\nfunc main() {}\n")

    walks = {"n": 0}
    real = extract.walk_directory

    def spy(root, ignore=(), exts=None):
        walks["n"] += 1
        return real(root, ignore=ignore, exts=exts)

    monkeypatch.setattr(service, "walk_directory", spy)
    service.run_index(path=repo, db=db_path, reset=True, log=lambda _m: None)
    assert walks["n"] == 1, f"expected a single walk, got {walks['n']}"
