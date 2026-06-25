"""Flatten source files into function/class symbols using tree-sitter.

Each file is parsed once; we capture only definition nodes (functions, classes,
methods) and emit a small, embeddable record per symbol. No whole-file content
is ever stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, Iterable

from tree_sitter import Language, Parser

from .definitions import DEFINITION_EXTRACTORS, definition_extensions, definition_languages
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
    # Most grammar wheels expose `language()`. The TypeScript wheel exposes
    # split entrypoints instead (`language_typescript()` / `language_tsx()`).
    return getattr(mod, "language", None) or getattr(mod, "language_typescript", None)


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
    # pip extra that installs this grammar (None for core dependencies). Used
    # to render actionable install hints when files exist on disk but the
    # grammar wheel is missing.
    extra: str | None = None
    # Ancestor node types that qualify a descendant symbol's name but are not
    # symbols themselves (e.g. Rust `impl_item` -> methods read as `Type.method`).
    # The qualifier name is resolved by `_qualifier_name_of` (the `type` then
    # `name` field), so it works for grammars that carry the name off the
    # standard `name` field.
    qualifier_types: frozenset[str] = frozenset()


# (extensions, grammar module, def node types, class node types, name child
# types, pip extra). The extra is None for languages bundled into the core
# install (python/powershell/csharp) and a named extra for the rest.
_RAW_SPECS = {
    "python": (
        [".py", ".pyi"],
        "tree_sitter_python",
        {"function_definition", "class_definition"},
        {"class_definition"},
        set(),
        None,
    ),
    "powershell": (
        [".ps1", ".psm1"],
        "tree_sitter_powershell",
        {"function_statement", "class_statement", "class_method_definition"},
        {"class_statement"},
        {"function_name", "simple_name"},
        None,
    ),
    "csharp": (
        [".cs"],
        "tree_sitter_c_sharp",
        {"class_declaration", "interface_declaration", "struct_declaration",
         "record_declaration", "method_declaration", "constructor_declaration"},
        {"class_declaration", "interface_declaration", "struct_declaration",
         "record_declaration"},
        set(),
        None,
    ),
    "javascript": (
        [".js", ".jsx", ".mjs", ".cjs"],
        "tree_sitter_javascript",
        {"function_declaration", "method_definition", "class_declaration"},
        {"class_declaration"},
        set(),
        "javascript",
    ),
    "typescript": (
        [".ts", ".tsx"],
        "tree_sitter_typescript",
        {"function_declaration", "method_definition", "class_declaration"},
        {"class_declaration"},
        set(),
        "typescript",
    ),
    "go": (
        [".go"],
        "tree_sitter_go",
        {"function_declaration", "method_declaration", "type_spec"},
        {"type_spec"},
        set(),
        "go",
    ),
    "rust": (
        [".rs"],
        "tree_sitter_rust",
        {"function_item", "function_signature_item", "struct_item",
         "enum_item", "trait_item"},
        {"struct_item", "enum_item", "trait_item"},
        set(),
        "rust",
    ),
    "bash": (
        [".sh", ".bash"],
        "tree_sitter_bash",
        {"function_definition"},
        set(),
        set(),
        "bash",
    ),
}

# Build the active registry (extension -> LangSpec), skipping uninstalled grammars.
# qualifier_types is opt-in per language via this side table (only Rust needs
# it today); keeping it out of the raw tuples avoids growing every entry.
_QUALIFIER_TYPES: dict[str, frozenset[str]] = {
    "rust": frozenset({"impl_item"}),
}
_REGISTRY: dict[str, LangSpec] = {}
_PARSERS: dict[str, Parser] = {}
for _lang, (_exts, _mod, _defs, _classes, _name_children, _extra) in _RAW_SPECS.items():
    _loader = _load(_mod)
    if _loader is None:
        continue
    spec = LangSpec(_lang, _loader, frozenset(_defs), frozenset(_classes),
                    frozenset(_name_children), extra=_extra,
                    qualifier_types=_QUALIFIER_TYPES.get(_lang, frozenset()))
    for _ext in _exts:
        _REGISTRY[_ext] = spec

# Full extension -> language map for every supported language, installed or
# not. Lets us surface "files exist on disk but no grammar wheel" without
# needing to parse them. `_REGISTRY` above only covers installed grammars.
_EXT_TO_LANG: dict[str, str] = {}
_LANG_META: dict[str, tuple[tuple[str, ...], str | None]] = {}
for _lang, (_exts, _mod, _defs, _classes, _name_children, _extra) in _RAW_SPECS.items():
    _LANG_META[_lang] = (tuple(_exts), _extra)
    for _ext in _exts:
        _EXT_TO_LANG[_ext] = _lang


@dataclass(frozen=True)
class LangCoverage:
    """Per-language file count discovered on disk, with grammar status."""
    language: str
    extensions: tuple[str, ...]
    file_count: int
    installed: bool
    extra: str | None = None

    @property
    def install_hint(self) -> str | None:
        """`pip install` hint, or None when already installed / a core dep."""
        if self.installed or self.extra is None:
            return None
        from ._dist import pip_extra_hint

        return pip_extra_hint(self.extra)


def scan_language_coverage(
    root: Path,
    files: Iterable[Path] | None = None,
) -> list[LangCoverage]:
    """Count source files per language under ROOT; flag missing grammars.

    Walks the known-extension superset (every language in the registry, even
    if its grammar wheel is not installed) so callers can report files that
    were skipped for lack of a parser. Read live from `_REGISTRY` so tests can
    monkeypatch the installed set.

    `files` lets a caller that has already walked the tree (e.g. `run_index`)
    pass its file list in and avoid a second directory walk; it must already
    cover the full known-extension superset (`set(_EXT_TO_LANG)`).
    """
    installed_names = {spec.name for spec in _REGISTRY.values()}
    walked = files if files is not None else walk_directory(root, exts=set(_EXT_TO_LANG))
    counts: dict[str, int] = {}
    for p in walked:
        # `files` may carry non-grammar extensions (e.g. when run_index walks the
        # union with --definitions); only grammar extensions have a language.
        lang = _EXT_TO_LANG.get(p.suffix.lower())
        if lang is None:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    out: list[LangCoverage] = []
    for lang, n in counts.items():
        exts, extra = _LANG_META[lang]
        out.append(LangCoverage(
            language=lang,
            extensions=exts,
            file_count=n,
            installed=lang in installed_names,
            extra=extra,
        ))
    out.sort(key=lambda c: c.language)
    return out


def supported_extensions(include_definitions: bool = False) -> set[str]:
    exts = set(_REGISTRY)
    if include_definitions:
        exts |= definition_extensions()
    return exts


def known_extensions() -> frozenset[str]:
    """Every grammar extension dowse recognises, installed or not.

    Used by callers (e.g. run_index) that want to walk the full superset once
    so they can spot files whose grammar wheel is missing without a second
    directory traversal.
    """
    return frozenset(_EXT_TO_LANG)


def known_languages(include_definitions: bool = True) -> frozenset[str]:
    """Every language value accepted by shortcut filters."""
    langs = set(_LANG_META)
    if include_definitions:
        langs.update(definition_languages())
    return frozenset(langs)


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


def _qualifier_name_of(node, src: bytes) -> str | None:
    """Name contributed by a qualifier ancestor (not a symbol itself).

    Rust's `impl_item` exposes the implementing type via its `type` field; the
    `name` field is tried first for any future qualifier that uses it.
    """
    for field in ("type", "name"):
        c = node.child_by_field_name(field)
        if c is not None:
            return src[c.start_byte:c.end_byte].decode("utf-8", "replace")
    return None


def _qualified_name(node, src: bytes, spec: LangSpec) -> str | None:
    parts: list[str] = []
    cur = node
    while cur is not None:
        if cur.type in spec.def_types:
            nm = _name_of(cur, src, spec.name_child_types)
            if nm:
                parts.append(nm)
        elif cur.type in spec.qualifier_types:
            nm = _qualifier_name_of(cur, src)
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


_AGENT_INSTRUCTION_FILES = frozenset({
    ".cursorrules",
    "agents.md",
    "claude.md",
    "codex.md",
    "copilot-instructions.md",
    "gemini.md",
})


def _git_ignored_relpaths(root: Path, relpaths: list[str]) -> set[str]:
    """Return paths ignored by git, or empty when git/check-ignore is unavailable."""
    if not relpaths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
            input="\0".join(relpaths) + "\0",
            # git emits/consumes raw UTF-8 path bytes under -z; force UTF-8 so a
            # non-ASCII filename can't trip the locale (cp1252) codec on Windows.
            encoding="utf-8",
            errors="surrogateescape",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    if proc.returncode not in (0, 1):
        # Not a git work tree (or another git error): degrade to the static skips.
        return set()
    return {p.replace("\\", "/") for p in proc.stdout.split("\0") if p}


def walk_directory(root: Path, ignore: Iterable[str] = (), exts: set[str] | None = None) -> Iterable[Path]:
    """Yield candidate source files, skipping common noise, agent docs, and gitignored paths."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__",
            ".mypy_cache", ".pytest_cache", ".dowse_index", "dist", "build", ".tox", *ignore}
    if exts is None:
        exts = supported_extensions()
    root = root.resolve()

    candidates: list[tuple[str, Path]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Only consider directories *below* the indexed root, so a noise name
        # in the absolute prefix (e.g. a project living under .../build/) is fine.
        rel_parts = p.relative_to(root).parts
        if any(part in skip for part in rel_parts):
            continue
        if p.name.lower() in _AGENT_INSTRUCTION_FILES:
            continue
        if p.suffix.lower() in exts:
            candidates.append((p.relative_to(root).as_posix(), p))

    ignored = _git_ignored_relpaths(root, [rel for rel, _ in candidates])
    for rel, p in candidates:
        if rel not in ignored:
            yield p
