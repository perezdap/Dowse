"""Local embedding model wrapper (sentence-transformers).

Heavy deps (torch, sentence-transformers) are imported lazily so that `--help`,
extraction, and querying without embedding stay fast.
"""
from __future__ import annotations

from functools import cached_property

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, ~80MB, CPU-friendly
_MAX_CHARS = 2000  # cap embed input; the model truncates to ~256 tokens anyway


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name

    @cached_property
    def _model(self):
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(self.model_name)

    @property
    def dimension(self) -> int:
        # `get_embedding_dimension()` is the new name; the old
        # `get_sentence_embedding_dimension()` triggers a FutureWarning on cold install.
        get_dim = getattr(self._model, "get_embedding_dimension", None)
        if get_dim is None:
            get_dim = self._model.get_sentence_embedding_dimension
        return int(get_dim())

    @staticmethod
    def _symbol_text(symbol) -> str:
        # Prefix the qualified name so the symbol itself influences the vector,
        # then the (capped) body. Helps match error messages naming a symbol.
        body = symbol.code_content[:_MAX_CHARS]
        return f"{symbol.kind} {symbol.symbol_name}\n{body}"

    def embed_symbols(self, symbols) -> list[list[float]]:
        texts = [self._symbol_text(s) for s in symbols]
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text])[0]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        arr = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in arr]
