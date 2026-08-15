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

import hashlib
import time
from dataclasses import dataclass, field, replace
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
    # Carried so a later build over the same tree can skip the parsing, which
    # is two thirds of the cost. Held in memory rather than written to disk on
    # purpose: see `build_index`.
    digests: dict[str, str] = field(default_factory=dict)
    trees: dict[str, object] = field(default_factory=dict)
    reused: int = 0  # modules whose parse came from a previous index

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
    digests: dict[str, str] = field(default_factory=dict)
    reused: int = 0


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


def _collect(root: Path, paths: list[Path], previous: "RepoIndex | None" = None) -> _Collected:
    collected = _Collected()
    for path in paths:
        repo_path = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as error:
            collected.skipped.append((repo_path, f"could not read: {error}"))
            continue

        # Hashing the bytes rather than comparing mtimes: a checkout, a branch
        # switch, or a tool that rewrites a file identically all move the mtime
        # without changing what the parser would produce.
        digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
        collected.digests[repo_path] = digest

        if (
            previous is not None
            and previous.digests.get(repo_path) == digest
            and repo_path in previous.parses
        ):
            collected.parses[repo_path] = previous.parses[repo_path]
            collected.trees[repo_path] = previous.trees[repo_path]
            collected.reused += 1
            continue

        source = raw.decode("utf-8", errors="replace")
        try:
            tree = parse_source(repo_path, source)
        except ParseError as error:
            collected.skipped.append((repo_path, str(error)))
            continue
        collected.trees[repo_path] = tree
        collected.parses[repo_path] = parse_module(repo_path, source, tree=tree)
    return collected


def build_index(root: Path, previous: RepoIndex | None = None) -> RepoIndex:
    """Index a repository. `root` must be a directory.

    Passing the previous index over the same tree does two things. If every
    file is byte-identical and none were added or removed, the previous index
    is returned outright -- it cannot be stale, so there is nothing to rebuild,
    and a 441-module repository answers in 0.33s instead of 19s. Otherwise the
    parse of every unchanged file is reused, which is 44% of the work.

    This is deliberately an *in-memory* reuse rather than a cache on disk.
    Resolution walks the AST, so a disk cache would have to store and reload
    the trees -- and unpickling them measures 1.65x the cost of simply parsing
    the source again, for about 30MB of cache on the standard library. A
    process that exits therefore cannot win here however clever the cache is;
    only a process that stays alive can, which is what this signature is for.

    Everything downstream of parsing is recomputed. Whether a module's
    references can change is a question about the whole import graph -- a
    re-export chain means an edit two modules away can alter what a name binds
    to -- and getting that wrong would silently report stale callers, which is
    the one failure this tool cannot afford.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    if previous is not None and previous.root != root:
        raise ValueError(f"Previous index is for a different root: {previous.root}")

    started = time.perf_counter()
    collected = _collect(root, discover(root), previous)

    if previous is not None and collected.digests == previous.digests:
        # Every file is byte-identical and none were added or removed, so the
        # previous index is not merely reusable -- it is already the answer.
        # This is the common case for an agent asking several questions between
        # edits, and it costs one pass of hashing rather than a rebuild.
        return replace(
            previous,
            build_seconds=time.perf_counter() - started,
            reused=collected.reused,
        )

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
        digests=collected.digests,
        trees=collected.trees,
        reused=collected.reused,
    )
