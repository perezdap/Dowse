"""Extractors for declarative package-definition files.

These are NOT code, so tree-sitter's function/class model doesn't fit. Instead
we carve each file into meaningful *sections* and emit one Symbol per section,
so a query like "uninstall command for 7zip" or "detection rule" retrieves just
that block instead of the whole file.

Two formats, both relevant to PSADT v4 packaging workflows:
  * YAML profiles (Payload-style): top-level keys become sections, qualified by
    the package name if one is present (``name:`` / ``id:`` / ``packageName:``).
  * Markdown definitions (PowerPacker-style): ATX headings become sections,
    qualified by their heading ancestry (``Install.Pre-Install``).

Deliberately dependency-light: no PyYAML, no Markdown parser. The structure of
these files is regular enough that an indentation/heading scan is more robust to
half-finished files than a strict parser that throws on the first error.

These extractors are opt-in (``dowse index --definitions``) so a normal code index
doesn't slurp every README.md and CI yaml in a repo.
"""
from __future__ import annotations

import re
from pathlib import Path

from .models import Symbol

# A top-level YAML key: starts at column 0, a bareword key, then a colon.
_YAML_TOP_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:(\s|$)")
# A scalar "name" we can use to qualify sections, e.g.  name: 7zip
_YAML_NAME_KEYS = ("name", "id", "packagename", "appname", "displayname", "product")
# An ATX Markdown heading:  ## Title
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Fenced code block delimiter (``` or ~~~), so we don't treat '#' inside as headings.
_MD_FENCE = re.compile(r"^\s*(```+|~~~+)")


def _slug(text: str) -> str:
    """Collapse a heading/key into a compact symbol-name component."""
    text = text.strip().strip("#").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def extract_yaml(path: Path, root: Path) -> list[Symbol]:
    """Carve a YAML profile into top-level sections."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    rel = path.relative_to(root).as_posix()

    # First pass: find a package name to qualify section names with.
    package = None
    for line in lines:
        m = _YAML_TOP_KEY.match(line)
        if m and m.group(1).lower() in _YAML_NAME_KEYS:
            value = line.split(":", 1)[1].strip().strip("'\"")
            if value:
                package = _slug(value)
                break

    # Second pass: each top-level key starts a section that runs until the next
    # top-level key (or EOF). Blank/comment lines between sections attach to the
    # section above them, which keeps trailing context with its block.
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _YAML_TOP_KEY.match(line)
        if m:
            starts.append((i, m.group(1)))

    symbols: list[Symbol] = []
    for idx, (line_no, key) in enumerate(starts):
        end = starts[idx + 1][0] - 1 if idx + 1 < len(starts) else len(lines) - 1
        body = "\n".join(lines[line_no:end + 1]).rstrip()
        if not body.strip():
            continue
        name = f"{package}.{key}" if package else key
        symbols.append(Symbol(
            file_path=rel,
            symbol_name=name,
            kind="section",
            language="yaml",
            start_line=line_no + 1,
            end_line=end + 1,
            code_content=body,
        ))
    return symbols


def extract_markdown(path: Path, root: Path) -> list[Symbol]:
    """Carve a Markdown definition file into heading sections (ancestry-qualified)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    rel = path.relative_to(root).as_posix()

    # Collect headings, ignoring any '#' that appears inside a fenced code block.
    headings: list[tuple[int, int, str]] = []  # (line_index, level, title)
    in_fence = False
    for i, line in enumerate(lines):
        if _MD_FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _MD_HEADING.match(line)
        if m:
            headings.append((i, len(m.group(1)), _slug(m.group(2))))

    symbols: list[Symbol] = []
    stack: list[tuple[int, str]] = []  # (level, title) ancestry
    for idx, (line_no, level, title) in enumerate(headings):
        # Section body runs until the next heading of the same or higher level.
        end = len(lines) - 1
        for j in range(idx + 1, len(headings)):
            if headings[j][1] <= level:
                end = headings[j][0] - 1
                break
        # Maintain ancestry stack for a qualified name (Install.Pre-Install).
        while stack and stack[-1][0] >= level:
            stack.pop()
        qualified = ".".join(t for _, t in stack + [(level, title)])
        stack.append((level, title))

        body = "\n".join(lines[line_no:end + 1]).rstrip()
        symbols.append(Symbol(
            file_path=rel,
            symbol_name=qualified or title,
            kind="section",
            language="markdown",
            start_line=line_no + 1,
            end_line=end + 1,
            code_content=body,
        ))
    return symbols


# extension -> extractor. Wired into extract.py's dispatch when --definitions is on.
DEFINITION_EXTRACTORS = {
    ".yaml": extract_yaml,
    ".yml": extract_yaml,
    ".md": extract_markdown,
    ".markdown": extract_markdown,
}


def definition_extensions() -> set[str]:
    return set(DEFINITION_EXTRACTORS)
