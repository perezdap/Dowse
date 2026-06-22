"""MCP server exposing the Context Engine to a coding harness over stdio.

Uses the FastMCP class bundled with the official `mcp` Python SDK (stable for
local stdio servers; the standalone `fastmcp` v3 line rebuilt its architecture
and auth model in early 2026). Both tools delegate to `service.py`, so the MCP
surface and the CLI run identical logic.

Launch via `dowse serve`, or point a harness straight at:
    command = "dowse", args = ["serve", "--db", "./.dowse_index"]
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import service
from .embed import DEFAULT_MODEL


def build_server(default_db: str = "./.dowse_index", default_model: str = DEFAULT_MODEL) -> FastMCP:
    mcp = FastMCP("dowse")

    @mcp.tool()
    def query_context(
        query: str,
        top: int = 3,
        kind: Optional[str] = None,
        language: Optional[str] = None,
        filter: Optional[str] = None,
        db: Optional[str] = None,
    ) -> list[dict]:
        """Find code by meaning, returning only the relevant function/class/section snippets.

        Use this for semantic recall when you don't know the exact symbol name:
        describe behaviour ("where do we validate the auth token"), paste an error
        message, or ask for a concept. It complements text search (grep/glob) —
        reach for grep when you know the literal string, reach for this when you
        only know the intent. Each result is a single definition (not a whole
        file), so it's cheap to pull several into context.

        Args:
            query: Natural-language description or error message.
            top: Number of snippets to return (default 3).
            kind: Optional filter — "function", "class", or "section".
            language: Optional filter — e.g. "python", "powershell", "csharp", "yaml".
            filter: Optional raw zvec SQL filter, e.g. "file_path LIKE 'src/%'".
            db: Index path; defaults to the server's configured collection.

        Returns:
            A list of results, each with file_path, symbol_name, kind, language,
            start_line, end_line, code_content, and ranking scores.
        """
        payload = service.run_query(
            text=query,
            db=db or default_db,
            model=default_model,
            top=top,
            kind=kind,
            lang=language,
            filter=filter,
        )
        return payload["results"]

    @mcp.tool()
    def index_codebase(
        path: str,
        reset: bool = False,
        definitions: bool = False,
        db: Optional[str] = None,
    ) -> dict:
        """Build or refresh the searchable index for a code directory.

        Run once before querying, and again after substantial code changes.
        Re-indexing is idempotent: unchanged symbols stay, edited ones are
        updated in place, and deleted ones are removed. The first run downloads
        the local embedding model (~80 MB) and may take a little while.

        Args:
            path: Directory to index (recursively).
            reset: Rebuild the collection from scratch instead of reconciling.
            definitions: Also index PSADT YAML profiles and Markdown package
                definitions as sections (off by default to avoid pulling in
                every README/CI file).
            db: Index path; defaults to the server's configured collection.

        Returns:
            A summary: indexed_files, indexed_symbols, dimension, db, elapsed_seconds.
        """
        return service.run_index(
            path=path,
            db=db or default_db,
            model=default_model,
            reset=reset,
            definitions=definitions,
        )

    @mcp.tool()
    def index_status(
        workspace: Optional[str] = None,
        db: Optional[str] = None,
    ) -> dict:
        """Check index health before deciding whether to index or query.

        Call this first when you are unsure whether an index exists, whether it
        covers the languages in this repo, or whether it has gone stale after
        edits. It never throws on a missing/stale index — it reports the state
        so you can choose to run `index_codebase` (missing/stale) or
        `query_context` (present and fresh).

        Args:
            workspace: The repo root. Used to resolve a default db path
                (`<workspace>/.dowse_index`) and to compute the `stale` and
                `missing_grammars` signals. Defaults to the server's working dir.
            db: Index path; defaults to `<workspace>/.dowse_index`.

        Returns:
            A status object: exists, db_path, indexed_files, indexed_symbols,
            dimension, languages, last_indexed_at, stale, missing_grammars.
            `missing_grammars` lists each language seen on disk whose grammar
            wheel is not installed, with an actionable `install_hint`.
        """
        root = Path(workspace) if workspace else Path.cwd()
        return service.run_index_status(db=db or str(root / ".dowse_index"), root=root)

    return mcp
