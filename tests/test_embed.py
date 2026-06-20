"""Tests for embed.py (avoid a real model download)."""
from __future__ import annotations

from types import SimpleNamespace

from dowse.embed import Embedder


class _FutureModel:
    """Stand-in for `SentenceTransformer` exposing only the new name."""
    def get_embedding_dimension(self) -> int:
        return 384


class _LegacyModel:
    """Stand-in for the current `SentenceTransformer` exposing only the old name."""
    def get_sentence_embedding_dimension(self) -> int:
        return 384


def test_dimension_prefers_new_api_when_available() -> None:
    e = Embedder()
    e._model = _FutureModel()  # type: ignore[assignment]
    assert e.dimension == 384


def test_dimension_falls_back_to_legacy_api() -> None:
    e = Embedder()
    e._model = _LegacyModel()  # type: ignore[assignment]
    assert e.dimension == 384


def test_dimension_raises_when_neither_api_present() -> None:
    e = Embedder()
    e._model = SimpleNamespace()  # type: ignore[assignment]
    try:
        _ = e.dimension
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError when neither dimension API exists")
