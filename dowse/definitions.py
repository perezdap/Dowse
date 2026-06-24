"""Extractors for declarative package-definition files.

These are NOT code, so tree-sitter's function/class model doesn't fit. Instead
we carve each file into meaningful *sections* and emit one Symbol per section,
so a query like "uninstall command for 7zip" or "detection rule" retrieves just
that block instead of the whole file.

Three formats are supported today:
  * YAML profiles (Payload-style): top-level keys become sections, qualified by
    the package name if one is present (``name:`` / ``id:`` / ``packageName:``).
  * Markdown definitions (PowerPacker-style): ATX headings become sections,
    qualified by their heading ancestry (``Install.Pre-Install``).
  * .NET/MSBuild XML (``.csproj`` / ``.props`` / ``.targets``): property groups,
    item groups, and build targets become sections.

Deliberately dependency-light: no PyYAML, no Markdown parser, no MSBuild SDK. The
structure of these files is regular enough that structural scans (with stdlib XML
parsing where useful) are more robust to half-finished files than strict parsers
that throw on the first error.

These extractors are opt-in (``dowse index --definitions``) so a normal code index
doesn't slurp every README.md and CI yaml in a repo.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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
# MSBuild containers worth indexing as standalone sections. The pattern is a
# scanner, not a full XML parser, so malformed project files can still yield
# useful sections after ElementTree rejects them.
_MSBUILD_SECTION_START = re.compile(
    r"<(?P<tag>PropertyGroup|ItemGroup|ItemDefinitionGroup|Target)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_MSBUILD_OPEN = re.compile(r"<([A-Za-z_][\w.-]*)(?:\s[^>]*)?>", re.DOTALL)
_MSBUILD_ATTR = re.compile(r"([A-Za-z_][\w.-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)


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


def _local_xml_name(tag: str) -> str:
    """Return an XML tag without namespace or prefix noise."""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def _compact_name(text: str, *, strip_extension: bool = True) -> str:
    """Make an MSBuild element/identity safe for a dotted symbol name."""
    text = _slug(text.replace("\\", "/"))
    text = text.rstrip("/").rsplit("/", 1)[-1]
    if strip_extension and "." in text:
        text = text.rsplit(".", 1)[0]
    text = re.sub(r"[^A-Za-z0-9_-]+", " ", text).strip()
    return _slug(text)


def _attrs(text: str) -> dict[str, str]:
    return {m.group(1): m.group(3).strip() for m in _MSBUILD_ATTR.finditer(text)}


def _direct_children_from_xml(section: str) -> list[tuple[str, dict[str, str]]]:
    try:
        element = ET.fromstring(section)
    except ET.ParseError:
        return []
    return [(_local_xml_name(child.tag), dict(child.attrib)) for child in list(element)]


def _direct_children_from_scan(section: str) -> list[tuple[str, dict[str, str]]]:
    children: list[tuple[str, dict[str, str]]] = []
    depth = 0
    for token in re.finditer(r"<!--.*?-->|<[^>]+>", section, re.DOTALL):
        raw = token.group(0)
        if raw.startswith("<!--") or raw.startswith("<?") or raw.startswith("<!"):
            continue
        if raw.startswith("</"):
            depth = max(0, depth - 1)
            continue
        m = _MSBUILD_OPEN.match(raw)
        if m and depth == 1:
            children.append((_local_xml_name(m.group(1)), _attrs(raw)))
        if not raw.endswith("/>"):
            depth += 1
    return children


def _direct_msbuild_children(section: str) -> list[tuple[str, dict[str, str]]]:
    return _direct_children_from_xml(section) or _direct_children_from_scan(section)


def _msbuild_identity(attrs: dict[str, str], *, path_like: bool = False) -> str | None:
    for key in ("Include", "Update", "Remove", "Name"):
        value = attrs.get(key)
        if not value:
            continue
        first = value.split(";", 1)[0].strip()
        if first:
            return _compact_name(first, strip_extension=path_like)
    return None


def _iter_msbuild_sections(text: str) -> list[tuple[int, int, str]]:
    """Yield complete or best-effort MSBuild metadata sections from text."""
    starts = list(_MSBUILD_SECTION_START.finditer(text))
    sections: list[tuple[int, int, str]] = []
    for idx, start in enumerate(starts):
        tag = start.group("tag")
        close = re.search(rf"</{re.escape(tag)}\s*>", text[start.end():], re.IGNORECASE)
        if close is not None:
            end = start.end() + close.end()
        elif idx + 1 < len(starts):
            end = starts[idx + 1].start()
        else:
            end = len(text)
        section = text[start.start():end].rstrip()
        if section.strip():
            sections.append((start.start(), end, section))
    return sections


def _msbuild_section_name(project: str, section: str) -> str:
    tag_match = re.match(r"<([A-Za-z_][\w.-]*)\b([^>]*)>", section, re.DOTALL)
    if tag_match is None:
        return project
    tag = _local_xml_name(tag_match.group(1))
    attrs = _attrs(tag_match.group(2))
    children = _direct_msbuild_children(section)

    parts = [project, tag]
    if tag.lower() == "target":
        target_name = _msbuild_identity(attrs)
        if target_name:
            parts.append(target_name)
        parts.extend(_compact_name(child) for child, _ in children)
    else:
        for child, child_attrs in children:
            child_name = _compact_name(child)
            if child_name:
                parts.append(child_name)
            identity = _msbuild_identity(child_attrs, path_like=child == "ProjectReference")
            if identity:
                parts.append(identity)

    return ".".join(part for part in parts if part)


def extract_msbuild(path: Path, root: Path) -> list[Symbol]:
    """Carve .NET/MSBuild XML files into project metadata sections."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = path.relative_to(root).as_posix()
    project = _compact_name(path.stem, strip_extension=False) or path.stem

    symbols: list[Symbol] = []
    for start, end, section in _iter_msbuild_sections(text):
        start_line = text.count("\n", 0, start) + 1
        end_line = text.count("\n", 0, end) + 1
        symbols.append(Symbol(
            file_path=rel,
            symbol_name=_msbuild_section_name(project, section),
            kind="section",
            language="msbuild",
            start_line=start_line,
            end_line=end_line,
            code_content=section,
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
    ".csproj": extract_msbuild,
    ".props": extract_msbuild,
    ".targets": extract_msbuild,
}


def definition_extensions() -> set[str]:
    return set(DEFINITION_EXTRACTORS)
