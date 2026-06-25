"""Core index/query operations, independent of any interface.

Both the Typer CLI (`cli.py`) and the MCP server (`server.py`) call these, so
the indexing loop and hybrid-search wiring live in exactly one place. Functions
return plain Python data (dicts / lists) and never touch stdout; callers decide
how to present it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
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


# ---------------------------------------------------------------------------
# dowse init — one-command project bootstrap (#5, #16)
# ---------------------------------------------------------------------------

_DOWSE_MCP_KEY = "dowse"
_HARNESS_CONFIGS = {
    "pi": {
        "config_path": ".mcp.json",
        "mcp_entry_overrides": {"directTools": True},
    }
}


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        return Path(configured)
    return Path.home() / ".pi" / "agent"


def _pi_package_installed(root: Path, package_name: str) -> bool:
    package_parts = package_name.split("/")
    candidates = [
        _pi_agent_dir() / "npm" / "node_modules" / Path(*package_parts),
        root / ".pi" / "npm" / "node_modules" / Path(*package_parts),
    ]
    return any(path.exists() for path in candidates)


def _pi_requirements(root: Path) -> dict:
    pi_exe = shutil.which("pi")
    adapter_installed = _pi_package_installed(root, "pi-mcp-adapter")
    return {
        "pi": {
            "installed": pi_exe is not None,
            "executable": pi_exe,
            "install_hint": "npm install -g @earendil-works/pi-coding-agent",
        },
        "pi_mcp_adapter": {
            "installed": adapter_installed,
            "install_hint": "pi install npm:pi-mcp-adapter",
        },
    }


def _pi_guidance(requirements: dict) -> list[str]:
    guidance = [
        "Pi core does not include MCP; Dowse's Pi preset expects npm:pi-mcp-adapter."
    ]
    if not requirements["pi"]["installed"]:
        guidance.append(
            "Install Pi first: npm install -g @earendil-works/pi-coding-agent"
        )
    if not requirements["pi_mcp_adapter"]["installed"]:
        guidance.append("Install the adapter: pi install npm:pi-mcp-adapter")
    return guidance


def _harness_result(root: Path, harness: str | None) -> dict | None:
    if not harness:
        return None
    if harness == "pi":
        requirements = _pi_requirements(root)
        return {
            "name": harness,
            "config_path": _HARNESS_CONFIGS[harness]["config_path"],
            "requirements": requirements,
            "guidance": _pi_guidance(requirements),
        }
    raise ValueError(f"unsupported harness: {harness}")


def _relative_db_path(root: Path, db: Path) -> str:
    """DB path relative to root for MCP config args; falls back to absolute."""
    try:
        return db.relative_to(root).as_posix()
    except ValueError:
        return str(db)


def _dowse_mcp_entry(db_rel: str, harness: str | None = None) -> dict:
    entry = {
        "command": "dowse",
        "args": ["serve", "--db", db_rel],
    }
    if harness:
        entry.update(_HARNESS_CONFIGS[harness]["mcp_entry_overrides"])
    return entry


def _merge_mcp_config(root: Path, db_rel: str, harness: str | None = None) -> dict:
    """Create or merge .mcp.json with a dowse server entry.

    Returns ``{"created": bool, "merged": bool}`` describing what happened.
    """
    mcp_path = root / ".mcp.json"
    dowse_entry = _dowse_mcp_entry(db_rel, harness)

    if not mcp_path.exists():
        data = {"mcpServers": {_DOWSE_MCP_KEY: dowse_entry}}
        mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return {"created": True, "merged": False}

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}

    if not isinstance(data, dict):
        data = {}

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    servers[_DOWSE_MCP_KEY] = dowse_entry
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"created": False, "merged": True}


def _merge_gitignore(root: Path) -> bool:
    """Append ``.dowse_index/`` to .gitignore if not already present.

    Returns True if a line was added, False if it was already there.
    """
    gi_path = root / ".gitignore"
    marker = ".dowse_index/"

    if not gi_path.exists():
        gi_path.write_text(marker + "\n", encoding="utf-8")
        return True

    content = gi_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if any(line.strip() == marker for line in lines):
        return False

    # Ensure trailing newline before appending.
    prefix = content if content.endswith("\n") else content + "\n"
    gi_path.write_text(prefix + marker + "\n", encoding="utf-8")
    return True


def run_init(
    root: str | Path,
    db: str | Path | None = None,
    model: str = DEFAULT_MODEL,
    skip_index: bool = False,
    harness: str | None = None,
    auto_index: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict:
    """One-command project bootstrap: MCP config, gitignore, coverage, index.

    - Writes or merges ``.mcp.json`` with a ``dowse`` server entry (#16)
    - Adds ``.dowse_index/`` to ``.gitignore`` idempotently (#16)
    - Reports missing grammar coverage (#5)
    - Runs an initial index unless ``skip_index`` is True (#5)
    - Optionally installs Cursor sessionStart hook when ``auto_index`` is True (#19)
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    root_path = Path(root).resolve()
    db_path = Path(db).resolve() if db else root_path / ".dowse_index"
    db_rel = _relative_db_path(root_path, db_path)
    if harness and harness not in _HARNESS_CONFIGS:
        raise ValueError(f"unsupported harness: {harness}")

    _log("[init] configuring .mcp.json ...")
    mcp_result = _merge_mcp_config(root_path, db_rel, harness)

    _log("[init] updating .gitignore ...")
    _merge_gitignore(root_path)

    missing = _missing_grammars_for(root_path)
    if missing:
        for m in missing:
            _log(f"[init] missing grammar: {m['language']} ({m['install_hint']})")

    index_summary = None
    if not skip_index:
        _log("[init] running initial index ...")
        index_summary = run_index(
            path=root_path, db=db_path, model=model, reset=False, log=_log,
        )

    auto_index_result = None
    if auto_index:
        from . import cursor_hooks as _cursor_hooks

        _log("[init] installing Cursor sessionStart hook (opt-in auto-index) ...")
        hook = _cursor_hooks.install_cursor_session_hook()
        auto_index_result = {
            "installed": True,
            "target": hook["target"],
            "hooks_path": hook["hooks_path"],
        }

    payload = {
        "status": "ok",
        "workspace": {"root": str(root_path), "db_path": str(db_path)},
        "mcp_config": mcp_result,
        "harness": _harness_result(root_path, harness),
        "gitignore": {"path": str(root_path / ".gitignore")},
        "missing_grammars": missing,
        "index": index_summary,
    }
    if auto_index_result is not None:
        payload["auto_index"] = auto_index_result
    return payload


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
