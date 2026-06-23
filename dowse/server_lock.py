"""A persistent, OS-level lock guaranteeing at most one `dowse serve` per index.

zvec's own collection lock is point-in-time and mode-dependent (many readers OR
one writer), so it can't enforce "exactly one long-lived server per repo": two
idle servers could both pass a startup probe. This module adds a dedicated lock
file held for the server's whole lifetime, independent of the zvec collection.

The lock lives next to the index (``<db>.serve.lock``) rather than inside it, so
acquiring it never creates or mutates the zvec collection directory. The lock is
advisory and OS-enforced: the byte-range lock is released automatically if the
process dies, so a crashed server never leaves a permanent stale lock.

Windows uses ``msvcrt.locking``; POSIX uses ``fcntl.flock``. The owning PID is
written to byte 0 for human diagnostics; the lock itself is taken on a byte well
past it so a separate best-effort reader can read the PID without contending.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # Windows
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]
    _HAVE_MSVCRT = False

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

# Take the byte-range lock past the PID text so a best-effort PID reader on a
# separate handle doesn't touch the locked region.
_LOCK_OFFSET = 1024


class ServerLockHeld(RuntimeError):
    """Raised when another live process already holds the server lock."""

    def __init__(self, lock_path: str, holder_pid: int | None) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        suffix = f" (held by pid {holder_pid})" if holder_pid else ""
        super().__init__(f"dowse serve lock is held: {lock_path}{suffix}")


class ServerLock:
    """An acquired server lock. Call ``release()`` (or use as a context manager)."""

    def __init__(self, path: Path, fh) -> None:
        self.path = path
        self._fh = fh
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._fh.seek(_LOCK_OFFSET)
            if _HAVE_MSVCRT:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif _HAVE_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()

    def __enter__(self) -> "ServerLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path_for(db: str | Path) -> Path:
    db_path = Path(db)
    return db_path.parent / f"{db_path.name}.serve.lock"


def _read_holder_pid(path: Path) -> int | None:
    """Best-effort read of the PID recorded by the current lock holder."""
    try:
        with open(path, "r") as fh:
            return int(fh.readline().strip())
    except (OSError, ValueError):
        return None


def acquire_server_lock(db: str | Path) -> ServerLock:
    """Acquire the exclusive server lock for ``db`` or raise ``ServerLockHeld``.

    The lock is released automatically by the OS if this process exits, so a
    crashed server never strands the lock.
    """
    path = lock_path_for(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    fh.seek(_LOCK_OFFSET)
    try:
        if _HAVE_MSVCRT:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        elif _HAVE_FCNTL:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise ServerLockHeld(str(path), _read_holder_pid(path)) from None

    # We own it: record our pid for diagnostics, then keep the handle open for
    # the lock's lifetime.
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return ServerLock(path, fh)


def probe_server_lock(db: str | Path) -> dict[str, object]:
    """Return whether another ``dowse serve`` holds the lock (non-mutating probe)."""
    path = lock_path_for(db)
    try:
        lock = acquire_server_lock(db)
    except ServerLockHeld as exc:
        return {
            "lock_path": str(path),
            "held": True,
            "holder_pid": exc.holder_pid,
        }
    lock.release()
    return {
        "lock_path": str(path),
        "held": False,
        "holder_pid": None,
    }
