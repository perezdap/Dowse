"""Flatten source files into function/class symbols using tree-sitter.

Each file is parsed once; we capture only definition nodes (functions, classes,
methods) and emit a small, embeddable record per symbol. No whole-file content
is ever stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from tree_sitter import Language, Parser

from .models import Symbol

# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------
# Each entry maps file extensions to a grammar loader + the node types that
# count as a "definition". A language is only enabled if its grammar wheel is
# importable, so the tool degrades gracefully instead of crashing.
#
# Only Python is verified end-to-end here. The others use well-known, stable
# node-type names and work once you `pip install tree-sitter-<lang>`.


def _load(module_name: str) -> Callable[[], object] | None:
    try:
        mod = __import__(module_name)
    except ImportError:
        return None
    return mod.language


@dataclass(frozen=True)
class LangSpec:
    name: str
    loader: Callable[[], object]
    def_types: frozenset[str]    # nodes treated as definitions
    class_types: frozenset[str]  # subset of def_types reported as kind="class"
    # Some grammars don't expose a "name" field on def nodes (PowerShell puts
    # it in a `function_name` / `simple_name` child). List those child node
    # types here; the resolver checks them after the standard "name" field.
    name_child_types: frozenset[str] = frozenset()


# (extensions, grammar module, def node types, class node types, name child types)
_RAW_SPECS = {
    "python": (
        [".py", ".pyi"],
        "tree_sitter_python",
        {"function_definition", "class_definition"},
        {"class_definition"},
        set(),
    ),
    "powershell": (
        [".ps1", ".psm1"],
        "tree_sitter_powershell",
        {"function_statement", "class_statement", "class_method_definition"},
        {"class_statement"},
        {"function_name", "simple_name"},
    ),
    "csharp": (
        [".cs"],
        "tree_sitter_c_sharp",
        {"class_declaration", "interface_declaration", "struct_declaration",
         "record_declaration", "method_declaration", "constructor_declaration"},
        {"class_declaration", "interface_declaration", "struct_declaration",
         "record_declaration"},
        set(),
    ),
    "javascript": (
        [".js", ".jsx", ".mjs", ".cjs"],
        "tree_sitter_javascript",
        {"function_declaration", "method_definition", "class_declaration"},
        {"class_declaration"},
        set(),
    ),
    "typescript": (
        [".ts", ".tsx"],
        "tree_sitter_typescript",
        {"function_declaration", "method_definition", "class_declaration"},
        {"class_declaration"},
        set(),
    ),
    "go": (
        [".go"],
        "tree_sitter_go",
        {"function_declaration", "method_declaration", "type_spec"},
        {"type_spec"},
        set(),
    ),
}

# Build the active registry (extension -> LangSpec), skipping uninstalled grammars.
_REGISTRY: dict[str, LangSpec] = {}
_PARSERS: dict[str, Parser] = {}
for _lang, (_exts, _mod, _defs, _classes, _name_children) in _RAW_SPECS.items():
    _loader = _load(_mod)
    if _loader is None:
        continue
    spec = LangSpec(_lang, _loader, frozenset(_defs), frozenset(_classes),
                    frozenset(_name_children))
    for _ext in _exts:
        _REGISTRY[_ext] = spec


def supported_extensions(include_definitions: bool = False) -> set[str]:
    exts = set(_REGISTRY)
    if include_definitions:
        from .definitions import definition_extensions
        exts |= definition_extensions()
    return exts


def _parser_for(spec: LangSpec) -> Parser:
    if spec.name not in _PARSERS:
        _PARSERS[spec.name] = Parser(Language(spec.loader()))
    return _PARSERS[spec.name]


def _name_of(node, src: bytes, name_child_types: frozenset[str] = frozenset()) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return src[name.start_byte:name.end_byte].decode("utf-8", "replace")
    # Grammar-specific name carriers (e.g. PowerShell's function_name / simple_name).
    if name_child_types:
        for child in node.children:
            if child.type in name_child_types:
                return src[child.start_byte:child.end_byte].decode("utf-8", "replace")
    # Fallback: first identifier child (covers grammars without a "name" field).
    for child in node.children:
        if "identifier" in child.type:
            return src[child.start_byte:child.end_byte].decode("utf-8", "replace")
    return None


def _qualified_name(node, src: bytes, spec: LangSpec) -> str | None:
    parts: list[str] = []
    cur = node
    while cur is not None:
        if cur.type in spec.def_types:
            nm = _name_of(cur, src, spec.name_child_types)
            if nm:
                parts.append(nm)
        cur = cur.parent
    if not parts:
        return None
    return ".".join(reversed(parts))


def extract_file(path: Path, root: Path, include_definitions: bool = False) -> list[Symbol]:
    """Parse one file into function/class/section symbols (or [] if unsupported)."""
    suffix = path.suffix.lower()
    spec = _REGISTRY.get(suffix)
    if spec is None:
        # Fall back to declarative-definition extractors (YAML/Markdown) if asked.
        if include_definitions:
            from .definitions import DEFINITION_EXTRACTORS
            extractor = DEFINITION_EXTRACTORS.get(suffix)
            if extractor is not None:
                return extractor(path, root)
        return []
    try:
        src = path.read_bytes()
    except OSError:
        return []

    parser = _parser_for(spec)
    tree = parser.parse(src)

    rel = path.relative_to(root).as_posix()
    symbols: list[Symbol] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in spec.def_types:
            qname = _qualified_name(node, src, spec)
            if qname:
                kind = "class" if node.type in spec.class_types else "function"
                symbols.append(
                    Symbol(
                        file_path=rel,
                        symbol_name=qname,
                        kind=kind,
                        language=spec.name,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        code_content=src[node.start_byte:node.end_byte].decode("utf-8", "replace"),
                    )
                )
        stack.extend(node.children)
    return symbols


def walk_directory(root: Path, ignore: Iterable[str] = (), exts: set[str] | None = None) -> Iterable[Path]:
    """Yield candidate source files, skipping common noise directories."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__",
            ".mypy_cache", ".pytest_cache", "dist", "build", ".tox", *ignore}
    if exts is None:
        exts = supported_extensions()
    root = root.resolve()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Only consider directories *below* the indexed root, so a noise name
        # in the absolute prefix (e.g. a project living under .../build/) is fine.
        rel_parts = p.relative_to(root).parts
        if any(part in skip for part in rel_parts):
            continue
        if p.suffix.lower() in exts:
            yield p
