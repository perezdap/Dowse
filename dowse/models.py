"""Shared data model for extracted symbols.

Kept in its own module so both the tree-sitter extractor (`extract.py`) and the
declarative-definition extractor (`definitions.py`) can import it without a
circular dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Symbol:
    file_path: str      # POSIX-normalised, relative to the indexed root
    symbol_name: str    # qualified, e.g. "Widget.render" or "7zip.install"
    kind: str           # "function" | "class" | "section"
    language: str       # "python" | "powershell" | "yaml" | "markdown" | ...
    start_line: int     # 1-based, inclusive
    end_line: int
    code_content: str

    def to_fields(self) -> dict:
        return asdict(self)
