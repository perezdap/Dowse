"""dowse: a fluff-free code Context Engine.

Commands:
  index   walk a directory, extract function/class symbols, embed, store in zvec
  query   embed a natural-language string / error, hybrid-search, emit JSON
  serve   expose index/query as MCP tools over stdio for a coding harness

Design rule: stdout carries ONLY machine-readable JSON. All human/progress
output goes to stderr, so `dowse query ... | jq` always works.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .embed import DEFAULT_MODEL
from . import service

app = typer.Typer(add_completion=False, help="Local code Context Engine (tree-sitter + zvec).")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _emit(payload) -> None:
    """Write a JSON payload to stdout (and nothing else)."""
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


@app.command()
def index(
    path: Path = typer.Argument(..., exists=True, file_okay=False, help="Directory to index."),
    db: Path = typer.Option(Path("./.dowse_index"), "--db", help="Zvec collection path."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="sentence-transformers model."),
    reset: bool = typer.Option(False, "--reset", help="Recreate the collection from scratch."),
    batch: int = typer.Option(128, "--batch", help="Embedding batch size."),
    definitions: bool = typer.Option(
        False, "--definitions", "-D",
        help="Also index PSADT YAML profiles and Markdown package definitions as sections.",
    ),
):
    """Recursively index function/class definitions under PATH."""
    summary = service.run_index(
        path=path, db=db, model=model, reset=reset,
        batch=batch, definitions=definitions, log=_err,
    )
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
):
    """Return the top-N most relevant code snippets as JSON."""
    payload = service.run_query(
        text=text, db=db, model=model, top=top, candidates=candidates,
        filter=filter, kind=kind, lang=lang, w_dense=w_dense, w_lexical=w_lexical,
    )
    _emit(payload)


@app.command()
def serve(
    db: Path = typer.Option(Path("./.dowse_index"), "--db", help="Default Zvec collection path for tools."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Default embedding model for tools."),
):
    """Run an MCP server (stdio) exposing `index` and `query` to a coding harness."""
    try:
        from .server import build_server
    except ModuleNotFoundError as exc:  # mcp not installed
        _err(f"[serve] missing dependency: {exc}. Install with: pip install 'dowse[mcp]'")
        raise typer.Exit(code=1)
    _err(f"[serve] starting MCP stdio server (default db={db}, model={model})")
    mcp = build_server(default_db=str(db), default_model=model)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    app()
