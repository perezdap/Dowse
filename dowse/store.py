"""Zvec storage layer: schema, idempotent indexing, and hybrid retrieval.

Hybrid = dense semantic similarity (zvec/HNSW/cosine) re-ranked with a cheap
lexical signal, plus optional native SQL-style scalar filters. The lexical pass
matters for error messages, which usually contain the literal symbol name.

Verified against zvec 0.5.0:
  * query() returns Doc objects with .id, .score, .fields (dict); fields are
    returned inline, so no second fetch is needed.
  * For COSINE, .score is a DISTANCE -> lower is better -> sim = 1 - score.
  * Filters are SQL-ish:  field = 'value' , field LIKE '%x%' , AND/OR/NOT/IN.
  * insert() ignores existing ids and a re-inserted *deleted* id is tombstoned;
    upsert() overwrites/adds cleanly. So indexing uses upsert + reconcile:
    upsert the file's current symbols, then delete only the ids that vanished.
  * doc ids may not contain '/', '.', ':'  -> ids are sha1 hex digests.
"""
from __future__ import annotations

import hashlib
import math
import re
import shutil
from pathlib import Path

import zvec

COLLECTION_NAME = "snippets"
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_ENUM_TOPK = 100_000  # cap when enumerating a single file's existing symbols


class LockedIndexError(RuntimeError):
    """Raised when zvec refuses to open a collection because another handle owns it."""

    def __init__(self, path: str) -> None:
        super().__init__(f"dowse index is already open: {path}")
        self.path = path


def _is_lock_error(exc: BaseException) -> bool:
    # zvec phrases the lock refusal per mode: "Can't lock read-write collection"
    # when a writer is blocked, "Can't lock read-only collection" when a reader
    # is blocked by an active writer. Both mean "another handle owns it".
    return "Can't lock" in str(exc) and "collection" in str(exc)


def _sql_str(value: str) -> str:
    """Quote/escape a string literal for a zvec SQL filter."""
    return "'" + value.replace("'", "''") + "'"


def _build_schema(dimension: int) -> "zvec.CollectionSchema":
    return zvec.CollectionSchema(
        name=COLLECTION_NAME,
        fields=[
            zvec.FieldSchema(name="file_path", data_type=zvec.DataType.STRING,
                             index_param=zvec.InvertIndexParam()),
            zvec.FieldSchema(name="symbol_name", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name="kind", data_type=zvec.DataType.STRING,
                             index_param=zvec.InvertIndexParam()),
            zvec.FieldSchema(name="language", data_type=zvec.DataType.STRING,
                             index_param=zvec.InvertIndexParam()),
            zvec.FieldSchema(name="start_line", data_type=zvec.DataType.INT32),
            zvec.FieldSchema(name="end_line", data_type=zvec.DataType.INT32),
            zvec.FieldSchema(name="code_content", data_type=zvec.DataType.STRING),
        ],
        vectors=[
            zvec.VectorSchema(
                name="embedding",
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=dimension,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        ],
    )


class Store:
    def __init__(self, collection):
        self._c = collection
        self._dim = int(collection.schema.vectors[0].dimension)

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def create(cls, path: str | Path, dimension: int, reset: bool = False) -> "Store":
        path = Path(path)
        if reset and path.exists():
            try:
                shutil.rmtree(path)
            except PermissionError:
                # On Windows a live zvec handle keeps the collection's own LOCK
                # files open, so rmtree fails. That specifically means another
                # process is using the collection -> report it as locked.
                raise LockedIndexError(str(path)) from None
        try:
            if path.exists():
                return cls(zvec.open(str(path)))
            return cls(zvec.create_and_open(path=str(path), schema=_build_schema(dimension)))
        except RuntimeError as exc:
            if _is_lock_error(exc):
                raise LockedIndexError(str(path)) from None
            raise

    @classmethod
    def open(cls, path: str | Path) -> "Store":
        try:
            return cls(zvec.open(str(Path(path))))
        except RuntimeError as exc:
            if _is_lock_error(exc):
                raise LockedIndexError(str(Path(path))) from None
            raise

    @classmethod
    def open_readonly(cls, path: str | Path) -> "Store":
        """Open the collection read-only so multiple readers can coexist.

        zvec permits any number of concurrent read-only handles, so several
        agents can query the same index at once. A read-only open still fails if
        a writer (an in-progress `index`) holds the collection — surfaced as the
        same LockedIndexError so callers report it uniformly.
        """
        try:
            option = zvec.CollectionOption(read_only=True)
            return cls(zvec.open(str(Path(path)), option))
        except RuntimeError as exc:
            if _is_lock_error(exc):
                raise LockedIndexError(str(Path(path))) from None
            raise

    # -- write -------------------------------------------------------------
    @staticmethod
    def _doc_id(symbol) -> str:
        # Stable across line moves (no line number) so upsert overwrites in place.
        key = f"{symbol.file_path}::{symbol.symbol_name}::{symbol.kind}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _existing_ids_for_file(self, file_path: str) -> set[str]:
        unit = [1.0 / math.sqrt(self._dim)] * self._dim  # nonzero (cosine-safe)
        docs = self._c.query(
            queries=zvec.Query(field_name="embedding", vector=unit),
            topk=_ENUM_TOPK,
            filter=f"file_path = {_sql_str(file_path)}",
        )
        return {d.id for d in docs}

    def list_indexed_files(self) -> set[str]:
        """Return relative file paths that currently have at least one indexed symbol."""
        unit = [1.0 / math.sqrt(self._dim)] * self._dim
        docs = self._c.query(
            queries=zvec.Query(field_name="embedding", vector=unit),
            topk=_ENUM_TOPK,
        )
        return {
            f.get("file_path")
            for d in docs
            if (f := dict(d.fields)).get("file_path")
        }

    def list_indexed_languages(self) -> list[str]:
        """Return distinct `language` values present in the index, sorted.

        Uses a vector probe that asks for every indexed document so the set is
        complete for typical codebases. Very large indexes rely on zvec's ANN
        recall, so the result is best-effort rather than transactionally exact.
        """
        unit = [1.0 / math.sqrt(self._dim)] * self._dim
        total = self.count()
        topk = max(total, 1) if total >= 0 else _ENUM_TOPK
        docs = self._c.query(
            queries=zvec.Query(field_name="embedding", vector=unit),
            topk=topk,
        )
        langs = {dict(d.fields).get("language") for d in docs}
        langs.discard(None)
        langs.discard("")
        return sorted(langs)

    def sync_file(self, file_path: str, symbols, vectors) -> dict:
        """Idempotently reconcile one file's symbols: upsert current, drop stale."""
        current = {self._doc_id(s): (s, v) for s, v in zip(symbols, vectors, strict=True)}
        existing = self._existing_ids_for_file(file_path)

        docs = [
            zvec.Doc(id=doc_id, vectors={"embedding": v}, fields=s.to_fields())
            for doc_id, (s, v) in current.items()
        ]
        if docs:
            self._c.upsert(docs)

        stale = list(existing - set(current))
        if stale:
            self._c.delete(ids=stale)
        return {"upserted": len(docs), "deleted": len(stale)}

    def optimize(self) -> None:
        self._c.optimize()

    def count(self) -> int:
        try:
            # zvec.stats shape varies across versions; -1 below means unknown, not 0
            return int(self._c.stats.doc_count)
        except Exception:
            return -1

    @property
    def dimension(self) -> int:
        return self._dim

    # -- read --------------------------------------------------------------
    def hybrid_query(
        self,
        query_vector,
        query_text: str,
        top: int = 3,
        candidate_k: int = 30,
        sql_filter: str | None = None,
        w_dense: float = 0.7,
        w_lexical: float = 0.3,
    ) -> list[dict]:
        docs = self._c.query(
            queries=zvec.Query(field_name="embedding", vector=query_vector),
            topk=max(candidate_k, top),
            filter=sql_filter,
        )
        tokens = {t.lower() for t in _TOKEN_RE.findall(query_text)}

        scored = []
        for d in docs:
            f = dict(d.fields)
            dense_sim = max(0.0, 1.0 - float(d.score))  # cosine distance -> similarity
            lex = self._lexical_score(tokens, f.get("symbol_name", ""), f.get("code_content", ""))
            final = w_dense * dense_sim + w_lexical * lex
            scored.append((final, dense_sim, lex, f))

        scored.sort(key=lambda t: t[0], reverse=True)
        results = []
        for rank, (final, dense_sim, lex, f) in enumerate(scored[:top], start=1):
            results.append({
                "rank": rank,
                "score": round(final, 4),
                "dense_similarity": round(dense_sim, 4),
                "lexical_score": round(lex, 4),
                "file_path": f.get("file_path"),
                "symbol_name": f.get("symbol_name"),
                "kind": f.get("kind"),
                "language": f.get("language"),
                "start_line": f.get("start_line"),
                "end_line": f.get("end_line"),
                "code_content": f.get("code_content"),
            })
        return results

    @staticmethod
    def _lexical_score(tokens: set[str], symbol_name: str, code: str) -> float:
        if not tokens:
            return 0.0
        name_l = symbol_name.lower()
        code_l = code.lower()
        in_name = sum(1 for t in tokens if t in name_l) / len(tokens)
        in_code = sum(1 for t in tokens if t in code_l) / len(tokens)
        return min(1.0, 0.7 * in_name + 0.3 * in_code)
