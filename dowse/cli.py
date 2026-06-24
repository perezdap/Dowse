"""dowse: a fluff-free code Context Engine.

Commands:
  index   walk a directory, extract function/class symbols, embed, store in zvec
  query   embed a natural-language string / error, hybrid-search, emit JSON
  status  report index health (exists, stale, missing grammars)
  doctor  install + index + lock + harness diagnostics as JSON
  init    one-command bootstrap: MCP config, gitignore, coverage, index
  hook    install Cursor sessionStart auto-index (opt-in)
  serve   expose index/query as MCP tools over stdio for a coding harness

Design rule: stdout carries ONLY machine-readable JSON. All human/progress
output goes to stderr, so `dowse query ... | jq` always works.
"""
from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from .embed import DEFAULT_MODEL
from . import cursor_hooks
from . import service
from .server_lock import ServerLockHeld, acquire_server_lock
from .store import LockedIndexError, Store

app = typer.Typer(add_completion=False, help="Local code Context Engine (tree-sitter + zvec).")
hook_app = typer.Typer(help="Opt-in Cursor session hooks for incremental indexing.")
app.add_typer(hook_app, name="hook")


class InitHarness(str, Enum):
    PI = "pi"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _emit(payload) -> None:
    """Write a JSON payload to stdout (and nothing else)."""
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _locked_index_exit(exc: LockedIndexError) -> None:
    _err(
        f"[dowse] index is already open: {exc.path}\n"
        "[dowse] Another dowse/zvec process is using this collection. "
        "Wait for any indexing job to finish, stop the competing process, or use "
        "one long-lived `dowse serve` MCP server instead of competing servers."
    )
    raise typer.Exit(code=1) from None


def _server_lock_exit(exc: ServerLockHeld, db: Path) -> None:
    holder = f" (pid {exc.holder_pid})" if exc.holder_pid else ""
    _err(
        f"[serve] another dowse serve is already running for {db}{holder}\n"
        f"[serve] lock file: {exc.lock_path}"
    )
    raise typer.Exit(code=1) from None


def _probe_serve_index(db: Path) -> None:
    """Fail fast if an existing index is currently held by an active writer."""
    if not db.exists() or not any(db.iterdir()):
        return
    try:
        store = Store.open_readonly(db)
        del store
    except LockedIndexError as exc:
        _locked_index_exit(exc)


@app.command()
def index(
    path: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory to index."),
    db: Path = typer.Option(Path("./.dowse_index"), "--db", help="Zvec collection path."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="sentence-transformers model."),
    reset: bool = typer.Option(False, "--reset", help="Recreate the collection from scratch."),
    batch: int = typer.Option(128, "--batch", help="Embedding batch size."),
    definitions: bool = typer.Option(
        False, "--definitions", "-D",
        help="Also index YAML, Markdown, and .NET/MSBuild definition files as sections.",
    ),
):
    """Recursively index function/class definitions under PATH."""
    try:
        summary = service.run_index(
            path=path, db=db, model=model, reset=reset,
            batch=batch, definitions=definitions, log=_err,
        )
    except LockedIndexError as exc:
        _locked_index_exit(exc)
    _emit(summary)


@app.command()
def query(
    text: str = typer.Argument(..., help="Natural-language query or error message."),
    db: Path = typer.Option(Path("./.dowse_index"), "--db", help="Zvec collection path."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Must match the index model."),
    top: int = typer.Option(3, "--top", "-n", help="Number of snippets to return."),
    candidates: int = typer.Option(30, "--candidates", help="Dense candidates before re-rank."),
    filter: Optional[str] = typer.Option(None, "--filter", help="Raw zvec SQL filter, e.g. \"kind = 'function'\"."),
    kind: Optional[str] = typer.Option(None, "--kind", help="Shortcut filter: function|class|section."),
    lang: Optional[str] = typer.Option(None, "--lang", help="Shortcut filter by language."),
    w_dense: float = typer.Option(0.7, "--w-dense", help="Weight for semantic similarity."),
    w_lexical: float = typer.Option(0.3, "--w-lexical", help="Weight for lexical overlap."),
    root: Optional[Path] = typer.Option(
        None, "--root",
        help="Workspace root for --tokens full-file comparison. Defaults to cwd.",
    ),
    tokens: bool = typer.Option(
        False, "--tokens",
        help="Include approximate token savings versus containing full files.",
    ),
):
    """Return the top-N most relevant code snippets as JSON."""
    try:
        payload = service.run_query(
            text=text, db=db, model=model, top=top, candidates=candidates,
            filter=filter, kind=kind, lang=lang, w_dense=w_dense, w_lexical=w_lexical,
            root=root, include_token_report=tokens,
        )
    except LockedIndexError as exc:
        _locked_index_exit(exc)
    _emit(payload)


@app.command()
def status(
    db: Optional[Path] = typer.Option(
        None, "--db",
        help="Index path. Defaults to <root>/.dowse_index (or ./.dowse_index).",
    ),
    root: Optional[Path] = typer.Option(
        None, "--root",
        help="Workspace root for stale + missing-grammar signals. Defaults to cwd.",
    ),
):
    """Report index health: does it exist, how big, which languages, is it stale?"""
    root_path = Path(root) if root else Path.cwd()
    db_path = Path(db) if db else root_path / ".dowse_index"
    try:
        payload = service.run_index_status(db=db_path, root=root_path)
    except LockedIndexError as exc:
        _locked_index_exit(exc)
    _emit(payload)


@app.command()
def doctor(
    db: Optional[Path] = typer.Option(
        None, "--db",
        help="Index path. Defaults to <root>/.dowse_index (or ./.dowse_index).",
    ),
    root: Optional[Path] = typer.Option(
        None, "--root",
        help="Workspace root for index, grammar, and MCP config checks. Defaults to cwd.",
    ),
):
    """Report install, index, lock, and harness configuration health as JSON."""
    root_path = Path(root) if root else Path.cwd()
    db_path = Path(db) if db else root_path / ".dowse_index"
    try:
        payload = service.run_doctor(db=db_path, root=root_path)
    except LockedIndexError as exc:
        _locked_index_exit(exc)
    _emit(payload)


@app.command()
def init(
    path: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory to initialise."),
    db: Optional[Path] = typer.Option(
        None, "--db",
        help="Index path. Defaults to <path>/.dowse_index.",
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="sentence-transformers model."),
    skip_index: bool = typer.Option(
        False, "--skip-index",
        help="Write MCP config and gitignore but do not run an initial index.",
    ),
    harness: Optional[InitHarness] = typer.Option(
        None, "--harness",
        help="Harness-specific config preset to generate (currently: pi).",
    ),
    auto_index: bool = typer.Option(
        False,
        "--auto-index",
        help=(
            "Also install a user-level Cursor sessionStart hook (opt-in). "
            "Does not run without this flag. May contend with dowse serve/index locks."
        ),
    ),
):
    """One-command bootstrap: MCP config, .gitignore, grammar coverage, index."""
    root_path = Path(path).resolve()
    db_path = Path(db).resolve() if db else root_path / ".dowse_index"
    try:
        payload = service.run_init(
            root=root_path,
            db=db_path,
            model=model,
            skip_index=skip_index,
            harness=harness.value if harness else None,
            auto_index=auto_index,
            log=_err,
        )
    except LockedIndexError as exc:
        _locked_index_exit(exc)
    _emit(payload)


@hook_app.command("install")
def hook_install():
    """Install or update ~/.cursor/hooks.json with dowse sessionStart auto-index."""
    payload = cursor_hooks.run_hook_install()
    _emit(payload)


@hook_app.command("session-start")
def hook_session_start():
    """Cursor sessionStart target: incremental index when workspace opted in (fail-open)."""
    payload = cursor_hooks.run_session_start_index(log=_err)
    _emit(payload)
    # Hooks must never block the editor session.
    if payload.get("status") == "error":
        raise typer.Exit(code=0)


@app.command()
def serve(
    db: Path = typer.Option(Path("./.dowse_index"), "--db", help="Default Zvec collection path for tools."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Default embedding model for tools."),
):
    """Run an MCP server (stdio) exposing `index` and `query` to a coding harness."""
    try:
        server_lock = acquire_server_lock(db)
    except ServerLockHeld as exc:
        _server_lock_exit(exc, db)

    try:
        _probe_serve_index(db)
        try:
            from .server import build_server
        except ModuleNotFoundError as exc:  # mcp not installed
            _err(f"[serve] missing dependency: {exc}. Install with: pip install 'dowse[mcp]'")
            raise typer.Exit(code=1) from None
        _err(f"[serve] starting MCP stdio server (default db={db}, model={model})")
        mcp = build_server(default_db=str(db), default_model=model)
        mcp.run(transport="stdio")
    finally:
        server_lock.release()


if __name__ == "__main__":
    app()
