import json, math, hashlib, re, sys
from pathlib import Path
from typer.testing import CliRunner

import dowse.cli as cli
import dowse.service as service
from dowse.embed import Embedder

DIM = 64
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

def _vec(text: str):
    v = [0.0] * DIM
    for t in _TOK.findall(text.lower()):
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]

class StubEmbedder(Embedder):
    @property
    def dimension(self): return DIM
    def embed_symbols(self, symbols):
        return [_vec(self._symbol_text(s)) for s in symbols]
    def embed_query(self, text):
        return _vec(text)

service.Embedder = StubEmbedder  # service.get_embedder() constructs this
service._EMBEDDERS.clear()        # drop any cached real embedder

# --- build a tiny sample repo ---
repo = Path("/home/claude/build/sample_repo")
(repo / "pkg").mkdir(parents=True, exist_ok=True)
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

runner = CliRunner()

print("=== INDEX ===", file=sys.stderr)
r = runner.invoke(cli.app, ["index", str(repo), "--db", "/home/claude/build/idx", "--reset"])
assert r.exit_code == 0, r.stdout + r.stderr
summary = json.loads(r.stdout)
print(json.dumps(summary, indent=2))
assert summary["status"] == "ok"
assert summary["indexed_symbols"] == 8, summary  # login, make_token, SessionManager(+__init__,revoke), connect, Connection(+query)

print("\n=== QUERY: natural language ===", file=sys.stderr)
r = runner.invoke(cli.app, ["query", "how do I authenticate a user and get a token", "--db", "/home/claude/build/idx"])
assert r.exit_code == 0, r.stdout + r.stderr
out = json.loads(r.stdout)
print(json.dumps([{k: v for k, v in res.items() if k != "code_content"} for res in out["results"]], indent=2))
top = out["results"][0]
assert top["symbol_name"] in ("login", "make_token"), top

print("\n=== QUERY: error message (lexical should pull exact symbol) ===", file=sys.stderr)
r = runner.invoke(cli.app, ["query", 'RuntimeError: connection pool exhausted in Connection.query', "--db", "/home/claude/build/idx"])
out = json.loads(r.stdout)
print(json.dumps([{k: v for k, v in res.items() if k != "code_content"} for res in out["results"]], indent=2))
assert any(res["symbol_name"] == "Connection.query" for res in out["results"]), out

print("\n=== QUERY: with --kind class filter ===", file=sys.stderr)
r = runner.invoke(cli.app, ["query", "manage sessions", "--db", "/home/claude/build/idx", "--kind", "class"])
out = json.loads(r.stdout)
print(json.dumps([res["symbol_name"] + " (" + res["kind"] + ")" for res in out["results"]], indent=2))
assert all(res["kind"] == "class" for res in out["results"]), out

print("\nALL TESTS PASSED", file=sys.stderr)

# === idempotency + reconcile ===
import zvec, math as _m
def _count(db):
    c = zvec.open(db)
    dim = c.schema.vectors[0].dimension
    u = [1.0/_m.sqrt(dim)]*dim
    return len({d.id for d in c.query(queries=zvec.Query(field_name="embedding", vector=u), topk=10000)})

print("\n=== RE-INDEX (no --reset): expect stable count ===", file=sys.stderr)
r = runner.invoke(cli.app, ["index", str(repo), "--db", "/home/claude/build/idx"])
assert r.exit_code == 0, r.stdout + (r.stderr or "")
n = _count("/home/claude/build/idx")
print("doc count after re-index:", n)
assert n == 8, f"expected 8, got {n} (duplication / tombstone bug)"

print("\n=== EDIT a file (remove make_token, rename login->signin), reconcile ===", file=sys.stderr)
(repo / "pkg" / "auth.py").write_text(
    'def signin(user, password):\n'
    '    return make_token(user)\n'
    '\n'
    'class SessionManager:\n'
    '    def revoke(self, token):\n'
    '        pass\n'
)
r = runner.invoke(cli.app, ["index", str(repo), "--db", "/home/claude/build/idx"])
assert r.exit_code == 0, r.stdout + (r.stderr or "")
c = zvec.open("/home/claude/build/idx")
dim = c.schema.vectors[0].dimension; u=[1.0/_m.sqrt(dim)]*dim
names = sorted(dict(d.fields)["symbol_name"] for d in c.query(queries=zvec.Query(field_name="embedding", vector=u), topk=10000))
print("symbols now:", names)
assert "signin" in names and "make_token" not in names and "login" not in names, names
assert "Connection.query" in names, names  # db.py untouched

print("\nIDEMPOTENCY + RECONCILE PASSED", file=sys.stderr)
