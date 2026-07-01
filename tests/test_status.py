"""Status / self-diagnosis for the index (issue #3)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from typer.testing import CliRunner

import dowse.cli as cli
import dowse.extract as extract
import dowse.service as service

runner = CliRunner()


def test_status_of_existing_index(sample_repo: Path, db_path: Path) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    status = service.run_index_status(db=db_path)

    assert status["exists"] is True
    assert status["db_path"] == str(db_path)
    assert status["indexed_symbols"] == 8
    assert status["indexed_files"] == 2
    assert status["dimension"] == 64
    assert status["languages"] == ["python"]


def test_status_of_missing_index(tmp_path: Path, db_path: Path) -> None:
    # No index has been built yet — an agent asks "do I need to index?" first.
    status = service.run_index_status(db=db_path, root=tmp_path)

    assert status["exists"] is False
    assert status["db_path"] == str(db_path)
    assert status["indexed_symbols"] == 0
    assert status["indexed_files"] == 0
    assert status["dimension"] is None
    assert status["languages"] == []
    assert status["last_indexed_at"] is None
    assert status["stale"] is None
    assert status["missing_grammars"] == []


def test_status_missing_grammars(sample_repo: Path, db_path: Path, monkeypatch) -> None:
    """Files on disk for an uninstalled grammar surface as actionable hints."""
    # Pretend the Go grammar is absent regardless of the real environment.
    patched = {k: v for k, v in extract._REGISTRY.items() if k != ".go"}
    monkeypatch.setattr(extract, "_REGISTRY", patched)

    # A repo with one indexed Python file plus two orphaned Go files.
    service.run_index(path=sample_repo, db=db_path, reset=True)
    (sample_repo / "app.go").write_text("package main\nfunc main() {}\n")
    (sample_repo / "util.go").write_text("package main\nfunc helper() {}\n")

    status = service.run_index_status(db=db_path, root=sample_repo)
    missing = {m["language"]: m for m in status["missing_grammars"]}

    assert "go" in missing
    assert missing["go"]["file_count"] == 2
    assert missing["go"]["install_hint"] == 'pip install "dowse-context[go]"'
    # Python is installed, so it must not be listed as missing.
    assert "python" not in missing


def test_status_stale_after_edit(sample_repo: Path, db_path: Path) -> None:
    """A source file newer than the index marks it stale."""
    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    # Touch a source file well after the index was written.
    touched = sample_repo / "pkg" / "auth.py"
    future = time.time() + 3600
    os.utime(touched, (future, future))

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_stale_after_indexed_file_deleted(sample_repo: Path, db_path: Path) -> None:
    """A deleted indexed file is stale so indexing can remove orphaned symbols."""
    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    (sample_repo / "pkg" / "db.py").unlink()

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_stale_after_new_indexable_file_with_old_mtime(
    sample_repo: Path, db_path: Path
) -> None:
    """A copied-in source file is stale even when its preserved mtime predates the index."""
    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    copied = sample_repo / "pkg" / "copied.py"
    copied.write_text("def restored():\n    return True\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(copied, (past, past))

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_stale_after_existing_file_changed_with_old_mtime(
    sample_repo: Path, db_path: Path
) -> None:
    """A restored source file is stale even when its preserved mtime predates the index."""
    source = sample_repo / "pkg" / "auth.py"
    original = source.read_text(encoding="utf-8")
    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    source.write_text(original + "\ndef restored():\n    return True\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(source, (past, past))

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_definitions_index_is_fresh_when_files_exist(
    sample_repo: Path, db_path: Path
) -> None:
    """Definition files in the index are not mistaken for deleted source files."""
    (sample_repo / "definition.md").write_text("# Package\n\n## Install\nRun it.\n")
    service.run_index(path=sample_repo, db=db_path, reset=True, definitions=True)

    status = service.run_index_status(db=db_path, root=sample_repo)

    assert status["stale"] is False


def test_status_stale_after_indexed_definition_file_deleted(
    sample_repo: Path, db_path: Path
) -> None:
    """Deleted definition files are stale so indexing can remove their sections."""
    definition = sample_repo / "definition.md"
    definition.write_text("# Package\n\n## Install\nRun it.\n")
    service.run_index(path=sample_repo, db=db_path, reset=True, definitions=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    definition.unlink()

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_stale_includes_missing_grammar_files(
    sample_repo: Path, db_path: Path, monkeypatch
) -> None:
    """Edits to files whose grammar wheel is missing still mark the index stale."""
    patched = {k: v for k, v in extract._REGISTRY.items() if k != ".go"}
    monkeypatch.setattr(extract, "_REGISTRY", patched)

    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    # Add a .go file after the index was written; Go grammar is not installed.
    (sample_repo / "main.go").write_text("package main\nfunc main() {}\n")
    future = time.time() + 3600
    os.utime(sample_repo / "main.go", (future, future))

    stale = service.run_index_status(db=db_path, root=sample_repo)
    assert stale["stale"] is True


def test_status_stale_after_new_extension_becomes_indexable(
    sample_repo: Path, db_path: Path, monkeypatch
) -> None:
    """A file skipped by an older grammar set is stale after that grammar appears."""
    (sample_repo / "main.go").write_text("package main\nfunc main() {}\n")
    grammar_exts = {".py", ".pyi"}
    monkeypatch.setattr(
        service,
        "supported_extensions",
        lambda include_definitions=False: set(grammar_exts),
    )
    service.run_index(path=sample_repo, db=db_path, reset=True)

    fresh = service.run_index_status(db=db_path, root=sample_repo)
    assert fresh["stale"] is False

    grammar_exts.add(".go")
    stale = service.run_index_status(db=db_path, root=sample_repo)

    assert stale["stale"] is True


def test_status_stale_without_root(sample_repo: Path, db_path: Path) -> None:
    """No root to compare against -> stale is None (unknown), not False."""
    service.run_index(path=sample_repo, db=db_path, reset=True)
    status = service.run_index_status(db=db_path)
    assert status["stale"] is None


def test_cli_status_emits_json(sample_repo: Path, db_path: Path) -> None:
    service.run_index(path=sample_repo, db=db_path, reset=True)

    r = runner.invoke(
        cli.app,
        ["status", "--db", str(db_path), "--root", str(sample_repo)],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["exists"] is True
    assert out["indexed_symbols"] == 8
    assert out["languages"] == ["python"]


def test_cli_status_smart_default_db(sample_repo: Path) -> None:
    """`dowse status --root X` resolves db to X/.dowse_index when --db is omitted."""
    service.run_index(path=sample_repo, db=sample_repo / ".dowse_index", reset=True)

    r = runner.invoke(cli.app, ["status", "--root", str(sample_repo)])
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["exists"] is True
    assert out["db_path"].replace("\\", "/").endswith("sample_repo/.dowse_index")
