"""Embedding regressions with a bounded, model-free encoder."""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import _vec
from dowse.embed import Embedder
from dowse.extract import extract_file


class _TruncatingModel:
    def encode(self, texts, **kwargs):
        # Model loading is unnecessary to reproduce loss of a truncated body.
        return [_vec(" ".join(text.split()[:64])) for text in texts]


@pytest.mark.parametrize(
    "prefix",
    [
        "LIMITS = (" + ", ".join('"x"' for _ in range(150)) + ")\n\n",
        "# " + "x\u2028" * 100 + "\nLIMITS = ('x',)\n\n",
        "# " + "x\f" * 100 + "\nLIMITS = ('x',)\n\n",
    ],
    ids=["dense-constant", "unicode-line-separators", "form-feeds"],
)
def test_constant_context_does_not_hide_distinct_function_bodies(
    tmp_path: Path, prefix: str
) -> None:
    embedder = Embedder()
    embedder._model = _TruncatingModel()
    vectors = []
    for i, error in enumerate(("authentication denied", "socket timeout")):
        source = prefix + f"def consume():\n    return LIMITS[0], {error!r}\n"
        path = tmp_path / f"sample_{i}.py"
        path.write_text(source, encoding="utf-8")
        symbol = next(s for s in extract_file(path, tmp_path) if s.symbol_name == "consume")
        assert "LIMITS =" in symbol.code_content
        assert symbol.code_content.splitlines() == source.splitlines()
        vectors.append(embedder.embed_symbols([symbol])[0])

    assert vectors[0] != vectors[1]
