"""Build every layer over a directory, in the one order that works.

Parsing, import resolution, the class graph, and reference resolution each need
the one before it, and the middle two need *every* file parsed before either can
start: whether `from pkg import thing` names a submodule or a function depends
on files that have not been read yet. So this is a whole-repository operation,
not a per-file one, and it parses each file exactly once for all four passes.

A file that cannot be parsed is recorded in `skipped` rather than dropped.
Silently ignoring it would understate every impact query that should have
included it, and understating a blast radius is the failure mode this tool
exists to prevent.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from blastradius.classes import ClassGraph
from blastradius.errors import ParseError
from blastradius.imports import ImportIndex, ModuleTable, build_import_index
from blastradius.model import Definition, ModuleParse, Reference, SymbolId
from blastradius.parse import parse_module, parse_source
from blastradius.resolve import resolve_module

# Directories that hold code this tool should not treat as part of the project.
IGNORED_DIRECTORIES = frozenset(
    {
        "venv",
        "node_modules",
        "__pycache__",
        "site-packages",
        "build",
        "dist",
        ".tox",
        ".nox",
        ".eggs",
    }
)


@dataclass(frozen=True)
class RepoIndex:
    """Everything known about one repository."""

    root: Path
    parses: dict[str, ModuleParse]
    imports: ImportIndex
    modules: ModuleTable
    classes: ClassGraph
    definitions: dict[SymbolId, Definition]
    # Inverted: the definition being referred to, mapped to every use of it.
    references: dict[SymbolId, tuple[Reference, ...]]
    skipped: tuple[tuple[str, str], ...] = ()
    unresolved_attribute_count: int = 0
    build_seconds: float = 0.0

    @property
    def module_count(self) -> int:
        return len(self.parses)

    def references_to(self, symbol: SymbolId) -> tuple[Reference, ...]:
        return self.references.get(symbol, ())


@dataclass
class _Collected:
    parses: dict[str, ModuleParse] = field(default_factory=dict)
    trees: dict[str, object] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def discover(root: Path) -> list[Path]:
    """Every Python file in the tree that belongs to the project.

    Only the part of the path *below* the root is inspected. Checking the
    absolute path would skip everything whenever an ancestor happens to be
    hidden -- indexing a checkout under `~/.local/src`, say -- and produce a
    silently empty index.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        relative_parts = path.relative_to(root).parts
        if any(
            part in IGNORED_DIRECTORIES or part.startswith(".") for part in relative_parts
        ):
            continue
        if path.is_file():
            found.append(path)
    return found


def _collect(root: Path, paths: list[Path]) -> _Collected:
    collected = _Collected()
    for path in paths:
        repo_path = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = parse_source(repo_path, source)
        except ParseError as error:
            collected.skipped.append((repo_path, str(error)))
            continue
        except OSError as error:
            collected.skipped.append((repo_path, f"could not read: {error}"))
            continue
        collected.trees[repo_path] = tree
        collected.parses[repo_path] = parse_module(repo_path, source, tree=tree)
    return collected


def build_index(root: Path) -> RepoIndex:
    """Index a repository. `root` must be a directory."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    started = time.perf_counter()
    collected = _collect(root, discover(root))

    imports, modules = build_import_index(collected.parses)
    classes = ClassGraph.build(collected.parses, imports)

    references: dict[SymbolId, list[Reference]] = {}
    unresolved = 0
    for repo_path, parse in collected.parses.items():
        resolved = resolve_module(parse, collected.trees[repo_path], imports, classes)
        for reference in resolved.references:
            references.setdefault(reference.target, []).append(reference)
        unresolved += len(resolved.unresolved_attributes)

    definitions = {
        definition.symbol: definition
        for parse in collected.parses.values()
        for definition in parse.definitions
    }

    return RepoIndex(
        root=root,
        parses=collected.parses,
        imports=imports,
        modules=modules,
        classes=classes,
        definitions=definitions,
        # Sorted so two runs over the same tree produce byte-identical output.
        references={
            symbol: tuple(sorted(uses, key=lambda use: (use.path, use.line)))
            for symbol, uses in references.items()
        },
        skipped=tuple(collected.skipped),
        unresolved_attribute_count=unresolved,
        build_seconds=time.perf_counter() - started,
    )
