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


def _db_mtime(db: Path) -> float | None:
    """Newest mtime among files inside the index directory (trackable proxy).

    We look at the directory contents rather than the dir mtime itself, since a
    plain `Path.stat().st_mtime` on a folder only moves when entries are added
    or removed, not when existing files are rewritten.
    """
    if not db.exists():
        return None
    mtimes = [p.stat().st_mtime for p in db.rglob("*") if p.is_file()]
    if not mtimes:
        # Directory exists but is empty — fall back to the dir itself.
        return db.stat().st_mtime
    return max(mtimes)


def run_index_status(
    db: str | Path = "./.dowse_index",
    root: str | Path | None = None,
) -> dict:
    """Report index health so a caller can decide whether to (re)index.

    `root` is the workspace the index was built from. When supplied it enables
    two extra signals: `missing_grammars` (files on disk whose grammar wheel is
    not installed) and `stale` (a source file newer than the index). Both are
    best-effort heuristics; `stale` is None when there is no root to compare.
    """
    db_path = Path(db)
    exists = db_path.exists() and any(db_path.iterdir())

    if not exists:
        return {
            "exists": False,
            "db_path": str(db_path),
            "indexed_files": 0,
            "indexed_symbols": 0,
            "dimension": None,
            "languages": [],
            "last_indexed_at": None,
            "stale": None,
            "missing_grammars": _missing_grammars_for(root) if root else [],
        }

    store = Store.open(db_path)
    last_indexed = _db_mtime(db_path)
    stale = _is_stale(root, last_indexed) if root is not None else None

    return {
        "exists": True,
        "db_path": str(db_path),
        "indexed_files": len(store.list_indexed_files()),
        "indexed_symbols": store.count(),
        "dimension": store.dimension,
        "languages": store.list_indexed_languages(),
        "last_indexed_at": last_indexed,
        "stale": stale,
        "missing_grammars": _missing_grammars_for(root) if root else [],
    }


def _missing_grammars_for(root: str | Path) -> list[dict]:
    """Per-language coverage for files on disk that have no installed grammar."""
    return [
        {
            "language": cov.language,
            "extensions": list(cov.extensions),
            "file_count": cov.file_count,
            "install_hint": cov.install_hint,
        }
        for cov in scan_language_coverage(Path(root))
        if not cov.installed and cov.install_hint
    ]


def _is_stale(root: str | Path, last_indexed: float | None) -> bool | None:
    """True if any indexed-eligible source file is newer than the index.

    Returns None when the comparison can't be made (no index mtime yet).
    """
    if last_indexed is None:
        return None
    exts = supported_extensions()
    root_path = Path(root)
    for p in walk_directory(root_path, exts=exts):
        try:
            if p.stat().st_mtime > last_indexed:
                return True
        except OSError:
            continue
    return False


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
