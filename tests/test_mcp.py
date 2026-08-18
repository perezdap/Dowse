"""MCP server tool registration and delegation (issue #9).

The MCP SDK is an optional install (`pip install "dowse-context[mcp]"`), so every test
uses `pytest.importorskip("mcp")` to skip cleanly where the SDK is absent. CI
installs `.[dev,mcp]` so these tests run there instead of skipping.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dowse.service as service
from dowse._dist import pip_extra_hint


def _build_server():
    from dowse.server import build_server

    return build_server()


async def _call_tool_json(mcp, name: str, arguments: dict):
    """Call an MCP tool and return the JSON payload it produced.

    `MCPServer.call_tool` (mcp 2.0) returns a `CallToolResult`; the tool's
    structured return is JSON-serialised into one `TextContent` block per
    top-level item (one block for a dict, one block per element for a list),
    so we parse those blocks back into Python to assert on the payload.
    """
    result = await mcp.call_tool(name, arguments)
    values = [json.loads(block.text) for block in result.content if hasattr(block, "text")]
    return values[0] if len(values) == 1 else values


def test_mcp_index_status_tool(sample_repo: Path) -> None:
    """The MCP server exposes index_status and it delegates to service."""
    pytest.importorskip("mcp")
    service.run_index(path=sample_repo, db=sample_repo / ".dowse_index", reset=True)
    mcp = _build_server()

    # Registered under the right name.
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "index_status" in names

    result = asyncio.run(_call_tool_json(mcp, "index_status", {"workspace": str(sample_repo)}))
    assert result["exists"] is True
    assert result["indexed_symbols"] == 8
    assert result["languages"] == ["python"]
    assert result["db_path"].replace("\\", "/").endswith("sample_repo/.dowse_index")


def test_mcp_query_context_tool(sample_repo: Path) -> None:
    """query_context delegates to service.run_query and returns ranked snippets."""
    pytest.importorskip("mcp")
    service.run_index(path=sample_repo, db=sample_repo / ".dowse_index", reset=True)
    mcp = _build_server()

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "query_context" in names

    results = asyncio.run(_call_tool_json(
        mcp,
        "query_context",
        {
            "query": "how do I authenticate a user and get a token",
            "db": str(sample_repo / ".dowse_index"),
        },
    ))
    assert len(results) > 0
    top = results[0]
    assert top["symbol_name"] in ("login", "make_token")
    assert "file_path" in top
    assert "code_content" in top


def test_mcp_index_codebase_tool(sample_repo: Path) -> None:
    """index_codebase delegates to service.run_index and returns a summary dict."""
    pytest.importorskip("mcp")
    mcp = _build_server()

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "index_codebase" in names

    result = asyncio.run(_call_tool_json(
        mcp,
        "index_codebase",
        {
            "path": str(sample_repo),
            "db": str(sample_repo / ".dowse_index"),
            "reset": True,
        },
    ))
    assert result["status"] == "ok"
    assert result["indexed_symbols"] == 8
    assert result["indexed_files"] == 2
    assert result["dimension"] == 64


def test_mcp_server_registers_all_three_tools(sample_repo: Path) -> None:
    """build_server registers exactly the three documented MCP tools."""
    pytest.importorskip("mcp")
    mcp = _build_server()

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"query_context", "index_codebase", "index_status"}


def test_serve_reports_install_hint_when_mcp_is_too_old(tmp_path: Path, monkeypatch) -> None:
    """An mcp older than 2.0 gets the install hint, not a raw traceback.

    SDK 1.x ships `mcp.server` but no `MCPServer` in it, so `dowse.server`'s
    import raises plain `ImportError` rather than `ModuleNotFoundError`. The
    guard in `cli.serve` has to catch the parent class or the upgrade this
    release forces surfaces as an unhandled traceback.
    """
    import sys
    import types

    import dowse.cli as cli

    stub = types.ModuleType("mcp")
    stub.__path__ = []
    server_mod = types.ModuleType("mcp.server")  # deliberately has no MCPServer
    stub.server = server_mod
    monkeypatch.setitem(sys.modules, "mcp", stub)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    monkeypatch.delitem(sys.modules, "dowse.server", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["serve", "--db", str(tmp_path / ".dowse_index")])

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "missing dependency" in output
    assert "MCPServer" in output
    assert pip_extra_hint("mcp") in output
