"""Index/query regressions for constants attached only to real global loads."""
from __future__ import annotations

from pathlib import Path

import pytest

import dowse.service as service


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
    ],
    ids=["parameter"],
)
def test_index_does_not_attach_constant_for_same_spelling_non_global(
    tmp_path: Path, db_path: Path, function: str
) -> None:
    match = _indexed_consume(tmp_path, db_path, "LIMIT = 3\n\n" + function + "\n")

    assert match["code_content"].splitlines() == function.splitlines()
