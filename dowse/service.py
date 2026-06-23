"""Core index/query operations, independent of any interface.

Both the Typer CLI (`cli.py`) and the MCP server (`server.py`) call these, so
the indexing loop and hybrid-search wiring live in exactly one place. Functions
return plain Python data (dicts / lists) and never touch stdout; callers decide
how to present it.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Iterator, Optional

import dowse as dowse_pkg

from .embed import DEFAULT_MODEL, Embedder
from .extract import (
    extract_file,
    known_extensions,
    scan_language_coverage,
    supported_extensions,
    walk_directory,
)
from .store import LockedIndexError, Store
from .server_lock import probe_server_lock

# Cache embedders by model name so repeated queries in a long-lived process
# (notably the MCP server) don't reload the model every call.
_EMBEDDERS: dict[str, Embedder] = {}


def get_embedder(model: str = DEFAULT_MODEL) -> Embedder:
    if model not in _EMBEDDERS:
        _EMBEDDERS[model] = Embedder(model)
    return _EMBEDDERS[model]


# zvec allows only one read-write handle per collection, even in-process. When a
# single process (notably the long-lived MCP server) fields concurrent tool
# calls against the same index, serialize them per resolved db path so the
# second caller waits instead of hitting a lock error.
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _lock_for(db: str | Path) -> threading.Lock:
    key = str(Path(db).resolve())
    with _INDEX_LOCKS_GUARD:
        lock = _INDEX_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INDEX_LOCKS[key] = lock
        return lock


@contextmanager
def _index_lock(db: str | Path) -> Iterator[None]:
    lock = _lock_for(db)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


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
    with _index_lock(db):
        return _run_index_locked(
            root=root,
            db=db,
            embedder=embedder,
            dim=dim,
            reset=reset,
            batch=batch,
            definitions=definitions,
            exts=exts,
            log_enabled=log is not None,
            _log=_log,
        )


def _run_index_locked(
    *,
    root: Path,
    db: str | Path,
    embedder: Embedder,
    dim: int,
    reset: bool,
    batch: int,
    definitions: bool,
    exts: set[str],
    log_enabled: bool,
    _log: Callable[[str], None],
) -> dict:
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
    if log_enabled:
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

    # Even though this is read-only, zvec's Python binding isn't thread-safe for
    # concurrent in-process handles, so serialize to avoid binding-level errors.
    with _index_lock(db_path):
        store = Store.open_readonly(db_path)
        last_indexed = _db_mtime(db_path)
        stale = _is_stale(root, last_indexed) if root is not None else None
        indexed_files = len(store.list_indexed_files())
        indexed_symbols = store.count()
        dimension = store.dimension
        languages = store.list_indexed_languages()
        del store

    return {
        "exists": True,
        "db_path": str(db_path),
        "indexed_files": indexed_files,
        "indexed_symbols": indexed_symbols,
        "dimension": dimension,
        "languages": languages,
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
    """True if any source file is newer than the index.

    Walks every extension dowse recognises (installed or not) so that edits to
    files with a missing grammar wheel still flip `stale` to `True`. Returns
    None when the comparison can't be made (no index mtime yet).
    """
    if last_indexed is None:
        return None
    exts = known_extensions()
    root_path = Path(root)
    for p in walk_directory(root_path, exts=exts):
        try:
            if p.stat().st_mtime > last_indexed:
                return True
        except OSError:
            continue
    return False


def _mcp_sdk_info() -> dict:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        return {"installed": False, "version": None}
    try:
        ver = version("mcp")
    except PackageNotFoundError:
        ver = None
    return {"installed": True, "version": ver}


def _dowse_install_info() -> dict:
    try:
        pkg_version = version("dowse")
    except PackageNotFoundError:
        pkg_version = getattr(dowse_pkg, "__version__", None)
    module_root = Path(dowse_pkg.__file__).resolve().parent
    return {
        "python_version": sys.version.split()[0],
        "dowse_version": pkg_version,
        "dowse_module": str(module_root),
        "mcp_sdk": _mcp_sdk_info(),
    }


def _probe_index_access(db: Path) -> dict:
    if not db.exists() or not any(db.iterdir()):
        return {"readable": False, "locked": False}
    try:
        with _index_lock(db):
            store = Store.open_readonly(db)
            del store
        return {"readable": True, "locked": False}
    except LockedIndexError:
        return {"readable": False, "locked": True}


def _mcp_config_has_dowse(data: dict) -> bool:
    for key in ("mcpServers", "servers"):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        if "dowse" in block:
            return True
        for cfg in block.values():
            if not isinstance(cfg, dict):
                continue
            cmd = str(cfg.get("command") or cfg.get("cmd") or "")
            if "dowse" in cmd.lower():
                return True
    return False


def _harness_mcp_configs(root: Path) -> dict[str, dict]:
    configs: dict[str, dict] = {}
    for name in (".mcp.json", ".cursor/mcp.json"):
        path = root / name
        entry: dict = {"present": path.is_file(), "has_dowse_server": False}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    entry["has_dowse_server"] = _mcp_config_has_dowse(data)
            except (OSError, json.JSONDecodeError):
                pass
        configs[name] = entry
    return configs


def run_doctor(
    db: str | Path | None = None,
    root: str | Path | None = None,
) -> dict:
    """Unified health report: install, index, locks, and harness MCP config."""
    root_path = Path(root).resolve() if root else Path.cwd().resolve()
    db_path = Path(db).resolve() if db else root_path / ".dowse_index"

    index_status = run_index_status(db=db_path, root=root_path)

    return {
        "status": "ok",
        "workspace": {"root": str(root_path), "db_path": str(db_path)},
        "install": _dowse_install_info(),
        "index": index_status,
        "locks": {
            "serve": probe_server_lock(db_path),
            "index": _probe_index_access(db_path),
        },
        "harness": {"mcp_configs": _harness_mcp_configs(root_path)},
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
    sql_filter = build_filter(filter, kind, lang)
    qvec = get_embedder(model).embed_query(text)
    # Even though this is read-only, zvec's Python binding isn't thread-safe for
    # concurrent in-process handles, so serialize to avoid binding-level errors.
    with _index_lock(db):
        store = Store.open_readonly(db)
        results = store.hybrid_query(
            query_vector=qvec,
            query_text=text,
            top=top,
            candidate_k=candidates,
            sql_filter=sql_filter,
            w_dense=w_dense,
            w_lexical=w_lexical,
        )
        del store
    return {"query": text, "filter": sql_filter, "results": results}
