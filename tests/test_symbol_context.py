"""Index/query regressions for symbols that depend on nearby declarations."""
from __future__ import annotations

from pathlib import Path

import pytest

import dowse.service as service


@pytest.mark.parametrize(
    "prefix", ["", "class Other:\n    def __init__(self):\n        self.path = None\n\n\n"]
)
@pytest.mark.parametrize("decorator", ["", "@checked\n"])
def test_query_finds_lock_classifier_using_preceding_constant(
    tmp_path: Path, db_path: Path, prefix: str, decorator: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = prefix + (
        "# Collection lock contention is not index corruption.\n"
        "# Another process holding the collection lock must release it.\n"
        "_LOCK_REFUSAL_PHRASES = (\"Can't lock\", \"collection\")\n"
        "\n"
        "\n"
        f"{decorator}"
        "def _is_lock_error(exc):\n"
        "    msg = str(exc)\n"
        "    return all(phrase in msg for phrase in _LOCK_REFUSAL_PHRASES)\n"
        "\n"
    )
    source += "\n".join(
        f"def probe_{i}():\n    return 'collection lock index'\n"
        for i in range(8)
    )
    (repo / "store.py").write_text(source, encoding="utf-8")

    service.run_index(path=repo, db=db_path)
    result = service.run_query(
        "How does Dowse distinguish index corruption from another process "
        "holding the collection lock?",
        db=db_path,
        top=5,
    )

    matches = [r for r in result["results"] if r["symbol_name"] == "_is_lock_error"]
    assert matches, result["results"]
    match = matches[0]
    assert '_LOCK_REFUSAL_PHRASES = ("Can\'t lock", "collection")' in match["code_content"]
    assert "# Collection lock contention is not index corruption." in match["code_content"]
    lines = source.splitlines()
    assert match["code_content"].splitlines() == lines[
        match["start_line"] - 1:match["end_line"]
    ]


@pytest.mark.parametrize(
    ("preamble", "body"),
    [
        ("LIMIT = 3\n\n", "return 1"),
        ("limit = 3\n\n", "return limit"),
        ("LIMIT = 3\n\n", "return 'LIMIT'"),
        (f"LIMIT = {'x' * 1300!r}\n\n", "return LIMIT"),
        ("LIMIT = 3\n" + "\n" * 10, "return LIMIT"),
        ("LIMIT = 3\n\ndef other():\n    pass\n\n", "return LIMIT"),
    ],
    ids=["unused", "lowercase", "string", "oversized", "distant", "other-function"],
)
def test_index_does_not_attach_unrelated_declarations(
    tmp_path: Path, db_path: Path, preamble: str, body: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    function = f"def consume():\n    {body}"
    (repo / "app.py").write_text(preamble + function + "\n", encoding="utf-8")

    service.run_index(path=repo, db=db_path)
    result = service.run_query(
        "consume", db=db_path, filter="symbol_name = 'consume'", top=1
    )

    assert result["results"][0]["code_content"].splitlines() == function.splitlines()
