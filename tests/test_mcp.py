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

import dowse.service as service


def _build_server():
    from dowse.server import build_server

    return build_server()


async def _call_tool_json(mcp, name: str, arguments: dict):
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        content, metadata = result
        if isinstance(metadata, dict) and "result" in metadata:
            return metadata["result"]
        result = content
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        values = [json.loads(item.text) for item in result]
        return values[0] if len(values) == 1 else values
    return result


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
