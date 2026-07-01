"""Cursor user-level sessionStart hook for opt-in incremental indexing (#4, #19)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import service

DOWSE_SESSION_HOOK_COMMAND = "dowse hook session-start"
_HOOK_MARKER = "dowse_session_auto_index"


def default_cursor_dir() -> Path:
    return Path.home() / ".cursor"


def _hooks_path(cursor_dir: Path) -> Path:
    return cursor_dir / "hooks.json"


def _is_dowse_session_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    cmd = str(entry.get("command") or "")
    return DOWSE_SESSION_HOOK_COMMAND in cmd or _HOOK_MARKER in cmd


def install_cursor_session_hook(*, cursor_dir: Path | None = None) -> dict:
    """Merge a sessionStart hook into ~/.cursor/hooks.json (idempotent)."""
    base = cursor_dir if cursor_dir is not None else default_cursor_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = _hooks_path(base)
    created = not path.is_file()

    if created:
        data: dict = {"version": 1, "hooks": {}}
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"version": 1, "hooks": {}}
    if not isinstance(data, dict):
        data = {"version": 1, "hooks": {}}

    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    session_list = hooks.get("sessionStart")
    if not isinstance(session_list, list):
        session_list = []
    kept = [e for e in session_list if not _is_dowse_session_entry(e)]
    kept.append({"command": DOWSE_SESSION_HOOK_COMMAND, "type": "command"})
    hooks["sessionStart"] = kept

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "target": "cursor",
        "hooks_path": str(path),
        "created": created,
        "merged": not created,
    }


def _find_opted_in_workspace(start: Path) -> Path | None:
    """Walk up from start while a parent contains .dowse_index/."""
    current = start.resolve()
    for directory in (current, *current.parents):
        if (directory / ".dowse_index").is_dir():
            return directory
        if (directory / ".dowse.yaml").is_file():
            return directory
    return None


def run_session_start_index(
    *,
    db_rel: str = ".dowse_index",
    log: Callable[[str], None] | None = None,
) -> dict:
    """Fail-open session hook: incremental index when workspace opted in."""
    workspace = _find_opted_in_workspace(Path.cwd())
    if workspace is None:
        return {"status": "skipped", "reason": "no_opted_in_workspace"}

    db_path = workspace / db_rel
    try:
        service.assert_safe_root(workspace)
        status = service.run_index_status(db=db_path, root=workspace)
        if status.get("exists") is True and status.get("stale") is False:
            return {
                "status": "skipped",
                "reason": "index_fresh",
                "workspace": str(workspace),
                "db_path": str(db_path),
                "indexed_symbols": status.get("indexed_symbols", 0),
            }

        summary = service.run_index(
            path=workspace,
            db=db_path,
            reset=False,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 — hook must fail open
        return {
            "status": "error",
            "reason": "index_failed",
            "workspace": str(workspace),
            "detail": str(exc),
        }

    return {
        "status": "ok",
        "workspace": str(workspace),
        "db_path": str(db_path),
        "indexed_symbols": summary.get("indexed_symbols", 0),
    }


def run_hook_install(*, cursor_dir: Path | None = None) -> dict:
    hook = install_cursor_session_hook(cursor_dir=cursor_dir)
    return {"status": "ok", "hook": hook}