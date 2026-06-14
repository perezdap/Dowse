"""Core index/query operations, independent of any interface.

Both the Typer CLI (`cli.py`) and the MCP server (`server.py`) call these, so
the indexing loop and hybrid-search wiring live in exactly one place. Functions
return plain Python data (dicts / lists) and never touch stdout; callers decide
how to present it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from .embed import DEFAULT_MODEL, Embedder
from .extract import (
    extract_file,
    known_extensions,
    scan_language_coverage,
    supported_extensions,
    walk_directory,
)
from .store import Store

# Cache embedders by model name so repeated queries in a long-lived process
# (notably the MCP server) don't reload the model every call.
_EMBEDDERS: dict[str, Embedder] = {}


def get_embedder(model: str = DEFAULT_MODEL) -> Embedder:
    if model not in _EMBEDDERS:
        _EMBEDDERS[model] = Embedder(model)
    return _EMBEDDERS[model]


def build_filter(raw: Optional[str], kind: Optional[str], lang: Optional[str]) -> Optional[str]:
    """Combine an optional raw SQL filter with kind/language shortcuts."""
    clauses = []
    if raw:
        clauses.append(f"({raw})")
    if kind:
        clauses.append(f"kind = '{kind}'")
    if lang:
        clauses.append(f"language = '{lang}'")
    return " AND ".join(clauses) if clauses else None


def run_index(
    path: str | Path,
    db: str | Path = "./.dowse_index",
    model: str = DEFAULT_MODEL,
    reset: bool = False,
    batch: int = 128,
    definitions: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Index a directory; return a summary dict. `log` receives progress lines."""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    root = Path(path).resolve()
    exts = supported_extensions(include_definitions=definitions)
    _log(f"[index] root={root}")
    _log(f"[index] extensions={sorted(exts)}")

    embedder = get_embedder(model)
    _log(f"[index] loading model '{model}' ...")
    dim = embedder.dimension
    store = Store.create(db, dimension=dim, reset=reset)
    _log(f"[index] model dim={dim}; db={db}")

    # Walk once over the union of indexable extensions and every known grammar
    # extension (installed or not). The union lets coverage flag uninstalled
    # grammars without a second directory walk; the index loop only processes
    # the indexable subset below.
    all_files = list(walk_directory(root, exts=exts | known_extensions()))
    files = [f for f in all_files if f.suffix.lower() in exts]
    _log(f"[index] {len(files)} source files found")

    # Surface files we recognised but couldn't parse because the grammar wheel
    # isn't installed. Skipped entirely when there's no log sink (the MCP
    # `index_codebase` tool), so a server never pays for the coverage pass.
    if log is not None:
        coverage = scan_language_coverage(root, files=all_files)
        for cov in coverage:
            if not cov.installed and cov.install_hint:
                ext_list = " ".join(cov.extensions)
                _log(f"[index] skipped {cov.file_count} {ext_list} files "
                     f"({cov.language}) - {cov.install_hint}")

    total_symbols = 0
    current_relpaths: set[str] = set()
    t0 = time.time()
    for i, fp in enumerate(files, start=1):
        symbols = extract_file(fp, root, include_definitions=definitions)
        rel = fp.relative_to(root).as_posix()
        current_relpaths.add(rel)
        if not symbols:
            stats = store.sync_file(rel, [], [])
            _log(f"[index] ({i}/{len(files)}) {rel}: +0 -{stats['deleted']}")
            continue
        vectors: list = []
        for start in range(0, len(symbols), batch):
            vectors.extend(embedder.embed_symbols(symbols[start:start + batch]))
        stats = store.sync_file(rel, symbols, vectors)
        total_symbols += len(symbols)
        _log(f"[index] ({i}/{len(files)}) {rel}: +{stats['upserted']} -{stats['deleted']}")

    for orphan in sorted(store.list_indexed_files() - current_relpaths):
        stats = store.sync_file(orphan, [], [])
        _log(f"[index] (removed) {orphan}: -{stats['deleted']}")

    _log("[index] building vector index (optimize) ...")
    store.optimize()

    return {
        "status": "ok",
        "indexed_files": len(files),
        "indexed_symbols": total_symbols,
        "dimension": dim,
        "db": str(db),
        "elapsed_seconds": round(time.time() - t0, 2),
    }


def run_query(
    text: str,
    db: str | Path = "./.dowse_index",
    model: str = DEFAULT_MODEL,
    top: int = 3,
    candidates: int = 30,
    filter: Optional[str] = None,
    kind: Optional[str] = None,
    lang: Optional[str] = None,
    w_dense: float = 0.7,
    w_lexical: float = 0.3,
) -> dict:
    """Hybrid-search the index; return {query, filter, results}."""
    store = Store.open(db)
    sql_filter = build_filter(filter, kind, lang)
    qvec = get_embedder(model).embed_query(text)
    results = store.hybrid_query(
        query_vector=qvec,
        query_text=text,
        top=top,
        candidate_k=candidates,
        sql_filter=sql_filter,
        w_dense=w_dense,
        w_lexical=w_lexical,
    )
    return {"query": text, "filter": sql_filter, "results": results}
