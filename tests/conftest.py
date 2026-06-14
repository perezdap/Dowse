"""Shared pytest fixtures: stub embedder (no model download) and sample repo."""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest
import zvec

import dowse.service as service
from dowse.embed import Embedder

DIM = 64
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _vec(text: str) -> list[float]:
    v = [0.0] * DIM
    for t in _TOK.findall(text.lower()):
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class StubEmbedder(Embedder):
    @property
    def dimension(self) -> int:
        return DIM

    def embed_symbols(self, symbols):
        return [_vec(Embedder._symbol_text(s)) for s in symbols]

    def embed_query(self, text: str):
        return _vec(text)


@pytest.fixture(autouse=True)
def stub_embedder():
    service.Embedder = StubEmbedder
    service._EMBEDDERS.clear()
    yield
    service._EMBEDDERS.clear()


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample_repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "auth.py").write_text(
        'def login(user, password):\n'
        '    """Authenticate a user and return a session token."""\n'
        '    if not user:\n'
        '        raise ValueError("missing user")\n'
        '    return make_token(user)\n'
        '\n'
        'def make_token(user):\n'
        '    return f"tok-{user}"\n'
        '\n'
        'class SessionManager:\n'
        '    def __init__(self):\n'
        '        self.sessions = {}\n'
        '    def revoke(self, token):\n'
        '        self.sessions.pop(token, None)\n'
    )
    (repo / "pkg" / "db.py").write_text(
        'def connect(dsn):\n'
        '    """Open a database connection from a DSN string."""\n'
        '    return Connection(dsn)\n'
        '\n'
        'class Connection:\n'
        '    def query(self, sql):\n'
        '        raise RuntimeError("connection pool exhausted")\n'
    )
    return repo


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "idx"


# Shared DB-read helpers used by several test modules. Kept here so the zvec
# enumeration boilerplate isn't copy-pasted per file.
def _symbol_docs(db: str | Path) -> list[dict]:
    """Return the field-dicts of every symbol in the index."""
    c = zvec.open(str(db))
    dim = c.schema.vectors[0].dimension
    unit = [1.0 / (dim ** 0.5)] * dim
    docs = c.query(
        queries=zvec.Query(field_name="embedding", vector=unit),
        topk=10_000,
    )
    return [dict(d.fields) for d in docs]


def _symbol_names(db: str | Path) -> list[str]:
    """Sorted symbol names in the index."""
    return sorted(f["symbol_name"] for f in _symbol_docs(db))
