"""Project bootstrap: one-command onboarding (`dowse init`).

A cohesive concern kept out of `service.py` (index/query orchestration):
write/merge `.mcp.json`, ignore `.dowse_index/`, report grammar coverage,
resolve optional harness presets, and run an initial index. The actual index
delegates to `service.run_index`; the unsafe-root guard and coverage helper
are borrowed from `service` so they stay single-source.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from .embed import DEFAULT_MODEL
from .service import (
    UnsafeRootError,
    _assert_safe_root,
    _missing_grammars_for,
    run_index,
)

# Re-exported so callers catch the error from the module that raises it
# (`run_init` raises `UnsafeRootError` from its pre-write guard).
__all__ = ["run_init", "UnsafeRootError"]

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
    force: bool = False,
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

    # Refuse an unsafe root before any writes, otherwise `dowse init $HOME`
    # would create/merge .mcp.json and .gitignore under home and only then
    # error when run_index trips the guard. Skip the check when --skip-index
    # is set, since no indexing (and thus no tree walk) happens in that mode.
    if not skip_index:
        _assert_safe_root(root_path, force=force)

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
            force=force,
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
