# Security Policy

## Local execution model

Dowse is a local developer tool. It reads the workspace path you ask it to index,
extracts function/class/section snippets, embeds them with a local model, and stores
the resulting vectors and metadata in the zvec directory you choose.

The MCP server runs over stdio and exposes the same local operations as the CLI to
the harness process that launched it. It does not open a network listener.

## Data and secrets

Dowse indexes source snippets, so do not index repositories or paths containing
secrets you do not want stored in the local index. Treat `.dowse_index/` as a local
artifact derived from your source tree and keep it out of version control.

The first real embedding run may download the configured `sentence-transformers`
model through the normal model cache. After required models and grammar wheels are
installed, indexing and querying do not need a remote service.

## Reporting vulnerabilities

Please report security issues privately through the repository owner rather than
opening a public issue with exploit details. Include the affected version, the
command or MCP tool involved, and a minimal reproduction when possible.
