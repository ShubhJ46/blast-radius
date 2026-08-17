"""Work out what each imported name in a module actually refers to.

This is the layer that turns a repository of files into a graph. Without it,
`hybrid_search` in one file and `hybrid_search` in another are the same string
and nothing more; with it, one of them is a definition and the other is an edge
pointing at it.

Three rules here are easy to get wrong and are each pinned by a test:

**`import a.b.c` binds `a`, not `a.b.c`.** Only the top package name enters the
namespace. `import a.b.c as x` is the different statement that binds `x`.

**`from pkg import thing` is ambiguous in the source and resolved by the disk.**
`thing` may be a submodule or a name defined in `pkg/__init__.py`. Submodule
wins, because that is the case where a file exists to point at.

**Re-exports are followed.** A package whose `__init__.py` says
`from .impl import Widget` makes `from pkg import Widget` reach a definition two
hops away. Not following that chain would silently drop most of the edges in any
codebase with a package-level public API.

What is deliberately not handled: `__all__` is not honoured when expanding a
star import, and PEP 420 namespace packages (directories with no `__init__.py`)
are not recognised as packages. Both are recorded here rather than discovered
later by a reader of the results.
"""

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from blastradius.model import ModuleParse, RawImport, SymbolId

INIT_BASENAME = "__init__.py"

# A re-export chain longer than this is either pathological or a cycle the
# `seen` guard has not closed yet. Bounded so indexing cannot hang on hostile
# input; no real package nests public re-exports this deep.
MAX_REEXPORT_DEPTH = 16


@dataclass(frozen=True)
class ModuleRef:
    """A module inside this repository."""

    name: str  # dotted: "blastradius.parse"
    path: str  # repo-relative: "blastradius/parse.py"


@dataclass(frozen=True)
class ExternalRef:
    """A dotted name that resolves to nothing in this repository.

    Kept rather than dropped: knowing a call went to `requests.get` is more
    useful than knowing it went nowhere, and it keeps the unresolved fraction
    measurable instead of invisible.
    """

    name: str


ImportTarget = SymbolId | ModuleRef | ExternalRef


@dataclass(frozen=True)
class Binding:
    """A name in a module's namespace, and what importing it brought in."""

    local_name: str
    target: ImportTarget
    line: int
    scope_qualname: str  # "" for a module-level import

    @property
    def is_module_level(self) -> bool:
        return self.scope_qualname == ""


def local_name_of(raw: RawImport) -> str | None:
    """The name a statement binds, or None for `from x import *`.

    Computable without touching the disk, which is what lets the resolver look
    a name up directly instead of expanding every import in a module to find it.
    """
    if raw.is_star:
        return None
    if raw.asname:
        return raw.asname
    if raw.kind == "from":
        return raw.name
    # `import a.b.c` binds only `a`.
    return (raw.module or "").split(".")[0] or None


@dataclass
class ModuleTable:
    """Maps between repo-relative file paths and dotted module names."""

    by_name: dict[str, str] = field(default_factory=dict)
    by_path: dict[str, str] = field(default_factory=dict)
    # Two files that claim the same dotted name. Surfaced rather than silently
    # resolved, because the loser's references would otherwise vanish.
    conflicts: tuple[tuple[str, str], ...] = ()

    @classmethod
    def build(cls, paths: Iterable[str], unreadable: Iterable[str] = ()) -> "ModuleTable":
        """Name every module, given the files that parsed and the ones that did not.

        `unreadable` exists because a directory is a package by holding an
        `__init__.py`, not by that file being parseable, and the difference is
        not local. Names are built by climbing while each parent is a package,
        so a package root that drops out of the set renames everything beneath
        it: with scrapy's Python 2 `scrapy/__init__.py` unreadable,
        `scrapy/utils/encoding.py` called itself `utils.encoding`, every
        `from scrapy.utils.encoding import ...` in 294 modules failed to match,
        and the whole repository's import graph resolved as external.

        Only parsed files are given names here. An unreadable file still has no
        definitions to point at -- the fix is to stop it from taking its
        siblings down with it, not to pretend it was read.
        """
        ordered = sorted(set(paths))
        package_dirs = {
            posixpath.dirname(path)
            for path in (*ordered, *unreadable)
            if posixpath.basename(path) == INIT_BASENAME
        }

        by_name: dict[str, str] = {}
        by_path: dict[str, str] = {}
        conflicts: list[tuple[str, str]] = []

        for path in ordered:
            name = _module_name(path, package_dirs)
            if not name:
                continue  # an __init__.py at the repository root names nothing
            by_path[path] = name
            if name in by_name:
                conflicts.append((by_name[name], path))
                continue  # first in sorted order wins, deterministically
            by_name[name] = path

        return cls(by_name=by_name, by_path=by_path, conflicts=tuple(conflicts))

    def path_for_module(self, name: str) -> str | None:
        return self.by_name.get(name)

    def module_for_path(self, path: str) -> str | None:
        return self.by_path.get(path)

    def package_of(self, path: str) -> str:
        """The package a relative import in this file counts up from.

        A package's `__init__.py` *is* its package, so `from . import x` there
        refers to the package itself rather than to its parent.
        """
        module = self.module_for_path(path)
        if module is None:
            return ""
        if posixpath.basename(path) == INIT_BASENAME:
            return module
        return module.rpartition(".")[0]

    def resolve_relative(self, path: str, level: int, module: str | None) -> str | None:
        """Absolute dotted name for a relative import, or None if it escapes the root."""
        if level <= 0:
            return module
        parts = [part for part in self.package_of(path).split(".") if part]
        ascend = level - 1
        if ascend > len(parts):
            return None
        base = parts[: len(parts) - ascend]
        if module:
            base = base + module.split(".")
        return ".".join(base) or None


def _module_name(path: str, package_dirs: set[str]) -> str:
    directory, basename = posixpath.split(path)
    stem = basename[:-3] if basename.endswith(".py") else basename

    parts: list[str] = []
    if stem != "__init__":
        parts.append(stem)
    # Climb while each parent is a package. The first directory that is not one
    # is the source root, which makes `src/` layouts work without configuration.
    while directory and directory in package_dirs:
        directory, name = posixpath.split(directory)
        parts.append(name)

    return ".".join(reversed(parts))


class ImportIndex:
    """Resolves imported names against a whole repository."""

    def __init__(self, parses: Mapping[str, ModuleParse], table: ModuleTable):
        self._parses = parses
        self._table = table
        # Module-level names only: a name bound inside a function is not
        # importable from elsewhere, so it can never satisfy a re-export.
        self._module_level: dict[str, dict[str, RawImport]] = {}
        self._stars: dict[str, list[RawImport]] = {}
        for path, parse in parses.items():
            names: dict[str, RawImport] = {}
            stars: list[RawImport] = []
            for raw in parse.imports:
                if raw.scope_qualname:
                    continue
                if raw.is_star:
                    stars.append(raw)
                    continue
                local = local_name_of(raw)
                if local:
                    names[local] = raw  # a later import rebinds, as Python does
            self._module_level[path] = names
            self._stars[path] = stars

    def bindings_for(self, path: str) -> dict[str, Binding]:
        """Every name this module's imports bring into scope, resolved."""
        bindings: dict[str, Binding] = {}
        for raw in self._parses[path].imports:
            for local, target in self._expand(path, raw, seen=frozenset()):
                bindings[local] = Binding(
                    local_name=local,
                    target=target,
                    line=raw.line,
                    scope_qualname=raw.scope_qualname,
                )
        return bindings

    def all_bindings(self) -> dict[str, dict[str, Binding]]:
        return {path: self.bindings_for(path) for path in self._parses}

    def member(self, module: ModuleRef, name: str) -> ImportTarget | None:
        """What `<module>.<name>` refers to.

        Attribute access on an imported module asks exactly the question a
        from-import asks, so it answers it the same way: a submodule wins over
        an exported name, and re-export chains are followed.
        """
        submodule = f"{module.name}.{name}"
        path = self._table.path_for_module(submodule)
        if path:
            return ModuleRef(submodule, path)
        return self._exported(module.path, name, frozenset())

    def _expand(
        self, path: str, raw: RawImport, seen: frozenset[tuple[str, str]]
    ) -> list[tuple[str, ImportTarget]]:
        module_name = self._absolute_module(path, raw)

        if raw.kind == "import":
            if module_name is None:
                return []
            # Without an alias only the top package is bound, so that is what
            # gets resolved -- `import a.b.c` must not create a binding for `a`
            # that points at `a.b.c`.
            bound = module_name if raw.asname else module_name.split(".")[0]
            local = raw.asname or bound
            return [(local, self._module_target(bound))]

        if module_name is None:
            # A relative import that climbed past the source root, or a
            # from-import with nothing to anchor it.
            return []

        if raw.is_star:
            return [
                (name, target)
                for name, target in self._star_expansion(module_name, seen).items()
            ]

        if not raw.name:
            return []
        local = raw.asname or raw.name
        return [(local, self._from_target(module_name, raw.name, seen))]

    def _absolute_module(self, path: str, raw: RawImport) -> str | None:
        if raw.level > 0:
            return self._table.resolve_relative(path, raw.level, raw.module)
        return raw.module

    def _module_target(self, dotted: str) -> ImportTarget:
        path = self._table.path_for_module(dotted)
        return ModuleRef(dotted, path) if path else ExternalRef(dotted)

    def _from_target(
        self, module_name: str, name: str, seen: frozenset[tuple[str, str]]
    ) -> ImportTarget:
        submodule = f"{module_name}.{name}"
        submodule_path = self._table.path_for_module(submodule)
        if submodule_path:
            return ModuleRef(submodule, submodule_path)

        module_path = self._table.path_for_module(module_name)
        if module_path is None:
            return ExternalRef(submodule)

        resolved = self._exported(module_path, name, seen)
        return resolved if resolved is not None else ExternalRef(submodule)

    def _exported(
        self, module_path: str, name: str, seen: frozenset[tuple[str, str]]
    ) -> ImportTarget | None:
        """What `from <this module> import name` reaches, following re-exports."""
        key = (module_path, name)
        if key in seen or len(seen) >= MAX_REEXPORT_DEPTH:
            return None
        seen = seen | {key}

        parse = self._parses.get(module_path)
        if parse is None:
            return None

        defined = parse.scope.defines.get(name)
        if defined is not None:
            return defined

        raw = self._module_level.get(module_path, {}).get(name)
        if raw is not None:
            for local, target in self._expand(module_path, raw, seen):
                if local == name:
                    return target

        # `from .impl import *` in an __init__ can also be what re-exports it.
        for star in self._stars.get(module_path, []):
            starred = self._absolute_module(module_path, star)
            starred_path = self._table.path_for_module(starred) if starred else None
            if starred_path:
                found = self._exported(starred_path, name, seen)
                if found is not None:
                    return found
        return None

    def _star_expansion(
        self, module_name: str, seen: frozenset[tuple[str, str]]
    ) -> dict[str, ImportTarget]:
        """Names a `from module import *` brings in.

        `__all__` is not honoured -- evaluating it means executing module-level
        code, which this tool will not do. The underscore convention is applied
        instead, which agrees with `__all__` in the common case and is at least
        deterministic when it does not.
        """
        module_path = self._table.path_for_module(module_name)
        if module_path is None or module_path not in self._parses:
            return {}

        parse = self._parses[module_path]
        candidates = set(parse.scope.defines) | set(self._module_level.get(module_path, {}))
        expansion: dict[str, ImportTarget] = {}
        for name in sorted(candidates):
            if name.startswith("_"):
                continue
            target = self._exported(module_path, name, seen)
            if target is not None:
                expansion[name] = target
        return expansion


def build_import_index(
    parses: Mapping[str, ModuleParse], unreadable: Iterable[str] = ()
) -> tuple[ImportIndex, ModuleTable]:
    """Convenience wrapper: derive the module table from the parses themselves.

    `unreadable` names files that were discovered but could not be parsed; they
    still count towards which directories are packages. See `ModuleTable.build`.
    """
    table = ModuleTable.build(parses.keys(), unreadable)
    return ImportIndex(parses, table), table
