# Context — dowse

A lightweight domain glossary for the dowse Context Engine. Terms here name
good seams; architecture reviews and ADRs defer to this vocabulary.

## Bootstrap

**Bootstrap** — the one-command project onboarding concern: write or merge
`.mcp.json` with a `dowse` server entry, add `.dowse_index/` to `.gitignore`
idempotently, report missing grammar coverage, and run an initial index.
Surfaced as `dowse init`. Implemented in `dowse/bootstrap.py` behind
`run_init`; delegates the actual index to the index/query orchestration in
`service.py`. Optional `--harness` presets (currently `pi`) adjust the MCP
entry and report harness-specific requirements.
