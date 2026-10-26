"""Manual real-model check. Run from the checkout with python -m scripts.evaluate_retrieval."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dowse.service import run_index, run_query


CASES = (
    (
        "T1",
        "How are symbols removed from the index when a source file is deleted?",
        ("_run_index_locked", "Store.sync_file"),
    ),
    (
        "T2",
        "How does the session start hook skip indexing when the workspace has not changed?",
        ("run_session_start_index",),
    ),
    (
        "T3",
        "Where are .dowseignore patterns applied to exclude tracked files?",
        ("walk_directory",),
    ),
    (
        "T4",
        "How does Dowse distinguish index corruption from another process holding the collection lock?",
        ("_is_lock_error",),
    ),
    (
        "T5",
        "How are semantic similarity and keyword matches combined to rank search results?",
        ("Store.hybrid_query",),
    ),
    (
        "T6",
        "How are Markdown headings extracted while ignoring headings inside fenced code blocks?",
        ("extract_markdown",),
    ),
    ("T4-error", "Can't lock collection", ("_is_lock_error",)),
)


def evaluate(root: Path, db: Path) -> dict:
    summary = run_index(root, db=db)
    cases = []
    for case_id, query, targets in CASES:
        results = run_query(query, db=db, top=30)["results"]
        hits = [
            r for r in results
            if r["symbol_name"] in targets and r["file_path"].startswith("dowse/")
        ]
        cases.append({
            "id": case_id,
            "query": query,
            "rank": hits[0]["rank"] if hits else None,
            "top5": [
                {key: r[key] for key in ("rank", "file_path", "symbol_name", "score")}
                for r in results[:5]
            ],
        })
    return {"index": summary, "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check retrieval against a Dowse source snapshot.")
    parser.add_argument("root", type=Path, help="Dowse checkout or fixed source snapshot")
    parser.add_argument("--db", type=Path, required=True, help="Index directory to reconcile")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.root, args.db), indent=2))
