"""Core index/query operations, independent of any interface.

Both the Typer CLI (`cli.py`) and the MCP server (`server.py`) call these, so
the indexing loop and hybrid-search wiring live in exactly one place. Functions
return plain Python data (dicts / lists) and never touch stdout; callers decide
how to present it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Iterator, Optional

import dowse as dowse_pkg

from ._dist import distribution_name
from .embed import DEFAULT_MODEL, Embedder
from .extract import (
    extract_file,
    known_extensions,
    known_languages,
    scan_language_coverage,
    supported_extensions,
    walk_directory,
)
from .store import LockedIndexError, Store, _sql_str
from .server_lock import probe_server_lock

# Cache embedders by model name so repeated queries in a long-lived process
# (notably the MCP server) don't reload the model every call.
_EMBEDDERS: dict[str, Embedder] = {}
_TOKEN_ESTIMATOR = "regex-v1"
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")
_VALID_KINDS = frozenset({"function", "class", "section"})


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
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"invalid kind {kind!r}; expected one of: {', '.join(sorted(_VALID_KINDS))}"
            )
        clauses.append(f"kind = {_sql_str(kind)}")
    if lang:
        valid_langs = known_languages(include_definitions=True)
        if lang not in valid_langs:
            raise ValueError(
                f"invalid language {lang!r}; expected one of: {', '.join(sorted(valid_langs))}"
            )
        clauses.append(f"language = {_sql_str(lang)}")
    return " AND ".join(clauses) if clauses else None


class UnsafeRootError(RuntimeError):
    """Raised when an index root would walk the user's home directory.

    Indexing ``$HOME`` (or an ancestor like the drive root) recursively parses
    and embeds every source file under home — typically tens of thousands of
    files across every cloned repo. That is almost never intended and is
    usually an agent/CLI mistake (running from the wrong cwd). Refuse unless
    the caller passes ``force=True``.
    """

    def __init__(self, root: Path, home: Path) -> None:
        self.root = root
        self.home = home
        super().__init__(
            f"refusing to index {root}: it is the home directory or an ancestor of it "
            f"(home={home}). Indexing here would walk your entire home tree. "
            f"Pass force=True / --force only if this is intentional."
        )


def _is_unsafe_root(root: Path, home: Path) -> bool:
    """True if indexing ``root`` would walk ``home`` (root is home or an ancestor)."""
    try:
        root = root.resolve()
        home = home.resolve()
    except OSError:
        return False
    return root == home or home.is_relative_to(root)


def _assert_safe_root(
    root: Path, *, home: Path | None = None, force: bool = False
) -> None:
    if force:
        return
    home = home if home is not None else Path.home()
    if _is_unsafe_root(root, home):
        raise UnsafeRootError(Path(root), home)


def assert_safe_root(root: str | Path, *, force: bool = False) -> None:
    """Raise UnsafeRootError when indexing root would walk the user's home tree."""
    _assert_safe_root(Path(root).resolve(), force=force)


def run_index(
    path: str | Path,
    db: str | Path = "./.dowse_index",
    model: str = DEFAULT_MODEL,
    reset: bool = False,
    batch: int = 128,
    definitions: bool = False,
    log: Callable[[str], None] | None = None,
    force: bool = False,
) -> dict:
    """Index a directory; return a summary dict. `log` receives progress lines."""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    root = Path(path).resolve()
    assert_safe_root(root, force=force)
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
    current_hashes: dict[str, str] = {}
    t0 = time.time()
    for i, fp in enumerate(files, start=1):
        symbols = extract_file(fp, root, include_definitions=definitions)
        rel = fp.relative_to(root).as_posix()
        current_relpaths.add(rel)
        file_hash = _file_hash(fp)
        if file_hash is not None:
            current_hashes[rel] = file_hash
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
    _write_index_metadata(
        db=Path(db),
        root=root,
        indexed_files=current_relpaths,
        indexed_file_hashes=current_hashes,
        indexed_extensions=exts,
        definitions=definitions,
    )

    return {
        "status": "ok",
        "indexed_files": len(files),
        "indexed_symbols": total_symbols,
        "dimension": dim,
        "db": str(db),
        "elapsed_seconds": round(time.time() - t0, 2),
    }


def _metadata_path(db: Path) -> Path:
    return db / "dowse-meta.json"


def _write_index_metadata(
    *,
    db: Path,
    root: Path,
    indexed_files: set[str],
    indexed_file_hashes: dict[str, str],
    indexed_extensions: set[str],
    definitions: bool,
) -> None:
    payload = {
        "version": 2,
        "root": str(root),
        "indexed_at": time.time(),
        "indexed_files": sorted(indexed_files),
        "indexed_file_hashes": {path: indexed_file_hashes[path] for path in sorted(indexed_file_hashes)},
        "indexed_extensions": sorted(indexed_extensions),
        "definitions": definitions,
    }
    _metadata_path(db).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_index_metadata(db: Path) -> dict | None:
    try:
        payload = json.loads(_metadata_path(db).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _file_hash(path: Path) -> str | None:
    digest = hashlib.sha1()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


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
        metadata = _read_index_metadata(db_path)
        stored_file_set = store.list_indexed_files()
        indexed_file_set = _metadata_file_set(metadata) or stored_file_set
        indexed_file_hashes = _metadata_file_hashes(metadata)
        indexed_extension_set = _metadata_extension_set(metadata)
        if root is not None and (metadata is None or indexed_file_hashes is None):
            stale = True
        else:
            stale = (
                _is_stale(
                    root,
                    last_indexed,
                    indexed_files=indexed_file_set,
                    indexed_file_hashes=indexed_file_hashes,
                    indexed_extensions=indexed_extension_set,
                    definitions=_metadata_definitions(metadata),
                )
                if root is not None
                else None
            )
        indexed_files = len(stored_file_set)
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


def _metadata_file_set(metadata: dict | None) -> set[str] | None:
    if metadata is None:
        return None
    files = metadata.get("indexed_files")
    if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
        return None
    return set(files)


def _metadata_file_hashes(metadata: dict | None) -> dict[str, str] | None:
    if metadata is None:
        return None
    hashes = metadata.get("indexed_file_hashes")
    if not isinstance(hashes, dict):
        return None
    if not all(isinstance(path, str) and isinstance(file_hash, str) for path, file_hash in hashes.items()):
        return None
    return hashes


def _metadata_extension_set(metadata: dict | None) -> set[str] | None:
    if metadata is None:
        return None
    extensions = metadata.get("indexed_extensions")
    if not isinstance(extensions, list) or not all(isinstance(ext, str) for ext in extensions):
        return None
    return set(extensions)


def _metadata_definitions(metadata: dict | None) -> bool:
    return bool(metadata and metadata.get("definitions") is True)


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


def _is_stale(
    root: str | Path,
    last_indexed: float | None,
    *,
    indexed_files: set[str] | None = None,
    indexed_file_hashes: dict[str, str] | None = None,
    indexed_extensions: set[str] | None = None,
    definitions: bool = False,
) -> bool | None:
    """True if any source file is newer than the index.

    Walks every extension dowse recognises (installed or not) so that edits to
    files with a missing grammar wheel still flip `stale` to `True`. When the
    caller provides indexed files, also treat deleted indexed files as stale so
    the next index pass can reconcile orphaned symbols. Returns None when the
    comparison can't be made (no index mtime yet).
    """
    if last_indexed is None:
        return None
    indexed_exts = {Path(file_path).suffix.lower() for file_path in indexed_files or set()}
    current_indexable_exts = supported_extensions(include_definitions=definitions)
    exts = set(known_extensions()) | current_indexable_exts | indexed_exts
    root_path = Path(root).resolve()
    current_files: set[str] = set()
    current_indexable_files: set[str] = set()
    for p in walk_directory(root_path, exts=exts):
        try:
            if p.stat().st_mtime > last_indexed:
                return True
        except OSError:
            continue
        suffix = p.suffix.lower()
        if indexed_extensions is not None and suffix in current_indexable_exts and suffix not in indexed_extensions:
            return True
        if indexed_files is not None:
            rel = p.relative_to(root_path).as_posix()
            is_relevant = suffix in indexed_exts or suffix in current_indexable_exts
            if is_relevant:
                current_files.add(rel)
                if indexed_file_hashes is not None and rel in indexed_files:
                    if indexed_file_hashes.get(rel) != _file_hash(p):
                        return True
            if suffix in current_indexable_exts:
                current_indexable_files.add(rel)
    if indexed_files is not None:
        if not indexed_files <= current_files:
            return True
        if not current_indexable_files <= indexed_files:
            return True
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
        pkg_version = version(distribution_name())
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


def estimate_tokens(text: str) -> int:
    """Approximate prompt tokens with a deterministic, dependency-free regex."""
    return len(_TOKEN_RE.findall(text))


def _token_savings_report(results: list[dict], root: str | Path) -> dict:
    root_path = Path(root)
    snippet_total = 0
    result_rows = []
    for result in results:
        snippet_tokens = estimate_tokens(str(result.get("code_content") or ""))
        snippet_total += snippet_tokens
        result_rows.append({
            "rank": result.get("rank"),
            "file_path": result.get("file_path"),
            "symbol_name": result.get("symbol_name"),
            "snippet_tokens": snippet_tokens,
        })

    file_rows = []
    unavailable_files = []
    full_file_total = 0
    seen_files: set[str] = set()
    for result in results:
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or file_path in seen_files:
            continue
        seen_files.add(file_path)
        source_path = root_path / file_path
        try:
            full_text = source_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            unavailable_files.append({"file_path": file_path, "reason": "not_found"})
            continue
        except OSError:
            unavailable_files.append({"file_path": file_path, "reason": "unreadable"})
            continue
        full_tokens = estimate_tokens(full_text)
        full_file_total += full_tokens
        file_rows.append({"file_path": file_path, "full_file_tokens": full_tokens})

    saved_tokens = max(0, full_file_total - snippet_total)
    reduction_percent = 0.0
    if full_file_total:
        reduction_percent = round(saved_tokens / full_file_total * 100, 2)

    return {
        "estimator": _TOKEN_ESTIMATOR,
        "snippet_tokens": snippet_total,
        "full_file_tokens": full_file_total,
        "saved_tokens": saved_tokens,
        "reduction_percent": reduction_percent,
        "results": result_rows,
        "files": file_rows,
        "unavailable_files": unavailable_files,
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
    root: str | Path | None = None,
    include_token_report: bool = False,
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
    payload = {"query": text, "filter": sql_filter, "results": results}
    if include_token_report:
        payload["token_savings"] = _token_savings_report(results, root or Path.cwd())
    return payload
