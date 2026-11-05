"""Index/query regressions for constants attached only to real global loads."""
from __future__ import annotations

from pathlib import Path
import warnings

import pytest

import dowse.service as service
from dowse.extract import extract_file


def _indexed_consume(tmp_path: Path, db_path: Path, source: str) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(source, encoding="utf-8")

    service.run_index(path=repo, db=db_path)
    result = service.run_query(
        "consume", db=db_path, filter="symbol_name = 'consume'", top=1
    )
    return result["results"][0]


@pytest.mark.parametrize(
    "function",
    [
        "def consume(LIMIT):\n    return LIMIT",
        "def consume():\n    LIMIT = 1\n    return LIMIT",
        "def consume(items):\n    return [LIMIT for LIMIT in items]",
        "def consume():\n    def inner(LIMIT):\n        return LIMIT\n    return inner",
        "def consume(config):\n    return config.LIMIT",
        "def consume(build):\n    return build(LIMIT=1)",
        "def consume():\n    nonlocal LIMIT\n    return LIMIT",
        "def consume():\n    global LIMIT\n    LIMIT = 1",
        "def consume():\n    global LIMIT",
    ],
    ids=[
        "parameter",
        "local",
        "comprehension-target",
        "nested-shadow",
        "attribute",
        "keyword",
        "syntax-error",
        "global-write-only",
        "global-declaration-only",
    ],
)
def test_index_does_not_attach_constant_for_same_spelling_non_global(
    tmp_path: Path, db_path: Path, function: str
) -> None:
    match = _indexed_consume(tmp_path, db_path, "LIMIT = 3\n\n" + function + "\n")

    assert match["code_content"].splitlines() == function.splitlines()


@pytest.mark.parametrize(
    "function",
    [
        "def consume():\n    return LIMIT",
        "def consume(value=LIMIT):\n    return value",
        "def consume(items):\n    return [item for item in items if item < LIMIT]",
        "def consume():\n    def inner():\n        return LIMIT\n    return inner",
        "def consume(LIMIT_OVERRIDE=None):\n    def inner(LIMIT):\n        return LIMIT\n    return inner(LIMIT)",
        "def consume():\n    global LIMIT\n    return LIMIT",
        "@register(LIMIT)\ndef consume():\n    return 1",
        "def consume():\n    global LIMIT\n    LIMIT += 1",
    ],
    ids=[
        "body",
        "default",
        "comprehension",
        "nested",
        "shadowed-only-inside",
        "global-load",
        "decorator",
        "global-augmented-assignment",
    ],
)
def test_index_attaches_constant_for_module_global_load(
    tmp_path: Path, db_path: Path, function: str
) -> None:
    source = "# Upper bound for consumers.\nLIMIT = 3\n\n" + function + "\n"
    match = _indexed_consume(tmp_path, db_path, source)

    assert match["start_line"] == 1
    assert match["code_content"].splitlines() == source.splitlines()


def test_index_does_not_attach_constant_used_only_in_postponed_annotations(
    tmp_path: Path, db_path: Path
) -> None:
    function = "def consume(value: LIMIT) -> LIMIT:\n    return value"
    source = "from __future__ import annotations\n\nLIMIT = 3\n\n" + function + "\n"

    match = _indexed_consume(tmp_path, db_path, source)

    assert match["code_content"].splitlines() == function.splitlines()


def test_scope_analysis_ignores_compile_warnings(tmp_path: Path) -> None:
    source = 'LIMIT = 3\n\ndef consume():\n    return LIMIT, "\\d"\n'
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        symbols = extract_file(path, tmp_path)

    assert symbols[0].code_content.splitlines() == source.splitlines()


def test_extraction_survives_function_too_deep_for_scope_analysis(tmp_path: Path) -> None:
    function = "def consume():\n    return LIMIT" + "+1" * 200_000
    path = tmp_path / "app.py"
    path.write_text("LIMIT = 3\n" + function + "\n", encoding="utf-8")

    symbols = extract_file(path, tmp_path)

    assert symbols[0].code_content.splitlines() == function.splitlines()
