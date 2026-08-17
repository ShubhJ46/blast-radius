"""The class hierarchy: who inherits from whom, and which methods override which.

Two of the three things `impact_of` reports come from here. Changing a method's
signature obliges every subclass that overrides it to change too, and knowing
that a method overrides an inherited one tells you the contract being altered
belongs to someone else.

Base classes arrive from the parser as source text -- `Widget`, `abc.ABC`,
`pkg.mod.Base` -- because resolving them needs the whole repository. Resolving
that text is a name lookup at module scope followed by attribute walks through
modules and nested classes, which is the same machinery imports already use.

**Bases outside the repository stay unresolved on purpose.** A class deriving
from `abc.ABC` or `django.db.models.Model` has a real base this tool cannot see,
and the linearisation computed here is therefore a projection of the true MRO
onto the classes that are indexed. Reporting it as complete would be a lie in
exactly the codebases most likely to rely on a framework base class, so
`unresolved_bases` keeps the omission visible.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from blastradius.imports import ImportIndex, ModuleRef
from blastradius.model import Definition, ModuleParse, SymbolId, canonical_definition


@dataclass(frozen=True)
class ClassNode:
    """One class, its resolved parents, and the methods it declares itself."""

    symbol: SymbolId
    bases: tuple[SymbolId, ...]  # declaration order, resolved ones only
    unresolved_bases: tuple[str, ...]  # source text of bases outside the index
    methods: Mapping[str, SymbolId] = field(default_factory=dict)

    @property
    def has_unresolved_bases(self) -> bool:
        return bool(self.unresolved_bases)


class ClassGraph:
    """Inheritance across the whole repository."""

    def __init__(self, nodes: Mapping[SymbolId, ClassNode]):
        self._nodes = dict(nodes)
        self._mro_cache: dict[SymbolId, tuple[SymbolId, ...]] = {}

        self._direct_subclasses: dict[SymbolId, list[SymbolId]] = {}
        for node in self._nodes.values():
            for base in node.bases:
                self._direct_subclasses.setdefault(base, []).append(node.symbol)

        # Which class declares each method, and under what name. Storing the
        # name here rather than scanning the owner's methods for it later keeps
        # every override query a dictionary lookup.
        self._owner: dict[SymbolId, tuple[SymbolId, str]] = {}
        for node in self._nodes.values():
            for name, method in node.methods.items():
                self._owner[method] = (node.symbol, name)

    # -- construction -----------------------------------------------------

    @classmethod
    def build(cls, parses: Mapping[str, ModuleParse], index: ImportIndex) -> "ClassGraph":
        definitions = _definitions_by_symbol(parses.values())
        methods = _methods_by_owner(definitions.values())

        nodes: dict[SymbolId, ClassNode] = {}
        for parse in parses.values():
            for definition in parse.definitions:
                if definition.kind != "class":
                    continue
                resolved: list[SymbolId] = []
                unresolved: list[str] = []
                for base in definition.bases:
                    target = _resolve_base(base, parse, index, definitions)
                    if target is None:
                        unresolved.append(base)
                    else:
                        resolved.append(target)
                nodes[definition.symbol] = ClassNode(
                    symbol=definition.symbol,
                    bases=tuple(resolved),
                    unresolved_bases=tuple(unresolved),
                    methods=methods.get(definition.symbol, {}),
                )
        return cls(nodes)

    # -- queries ----------------------------------------------------------

    def node(self, symbol: SymbolId) -> ClassNode | None:
        return self._nodes.get(symbol)

    def direct_subclasses(self, symbol: SymbolId) -> tuple[SymbolId, ...]:
        return tuple(sorted(self._direct_subclasses.get(symbol, ())))

    def descendants(self, symbol: SymbolId) -> tuple[SymbolId, ...]:
        """Every class transitively deriving from this one."""
        found: set[SymbolId] = set()
        queue = list(self._direct_subclasses.get(symbol, ()))
        while queue:
            current = queue.pop()
            if current in found:
                continue  # also closes any cycle a malformed index produced
            found.add(current)
            queue.extend(self._direct_subclasses.get(current, ()))
        return tuple(sorted(found))

    def mro(self, symbol: SymbolId) -> tuple[SymbolId, ...]:
        """C3 linearisation over the classes this repository can see."""
        cached = self._mro_cache.get(symbol)
        if cached is None:
            cached = self._linearize(symbol, frozenset())
            self._mro_cache[symbol] = cached
        return cached

    def lookup_method(self, class_symbol: SymbolId, name: str) -> SymbolId | None:
        """Find a method on a class or anything it inherits from.

        This is what resolving `self.helper()` needs: the class is known from
        the enclosing scope, and the method is whatever the MRO reaches first.
        """
        for ancestor in self.mro(class_symbol):
            node = self._nodes.get(ancestor)
            if node is not None and name in node.methods:
                return node.methods[name]
        return None

    def owner_of(self, method: SymbolId) -> SymbolId | None:
        entry = self._owner.get(method)
        return entry[0] if entry else None

    def overrides_of(self, method: SymbolId) -> tuple[SymbolId, ...]:
        """Subclass methods that override this one, and so must change with it."""
        entry = self._owner.get(method)
        if entry is None:
            return ()
        owner, name = entry

        overriding = [
            self._nodes[descendant].methods[name]
            for descendant in self.descendants(owner)
            if name in self._nodes[descendant].methods
        ]
        return tuple(sorted(overriding))

    def overridden(self, method: SymbolId) -> SymbolId | None:
        """The inherited definition this method overrides, if any."""
        entry = self._owner.get(method)
        if entry is None:
            return None
        owner, name = entry

        # Skip the owner itself: the first ancestor after it that declares the
        # same name is the definition being overridden.
        for ancestor in self.mro(owner)[1:]:
            ancestor_node = self._nodes.get(ancestor)
            if ancestor_node is not None and name in ancestor_node.methods:
                return ancestor_node.methods[name]
        return None

    # -- linearisation ----------------------------------------------------

    def _linearize(self, symbol: SymbolId, stack: frozenset[SymbolId]) -> tuple[SymbolId, ...]:
        node = self._nodes.get(symbol)
        # The stack guard cannot fire on valid Python -- a class cannot be its
        # own ancestor -- but a partial index can produce one, and hanging is a
        # worse answer than a short one.
        if node is None or symbol in stack:
            return (symbol,)

        stack = stack | {symbol}
        sequences = [list(self._linearize(base, stack)) for base in node.bases]
        sequences.append(list(node.bases))
        return (symbol, *_merge(sequences))


def _merge(sequences: list[list[SymbolId]]) -> list[SymbolId]:
    """The C3 merge: take the first head that appears in no other sequence's tail."""
    result: list[SymbolId] = []
    remaining = [sequence for sequence in sequences if sequence]

    while remaining:
        for sequence in remaining:
            head = sequence[0]
            if not any(head in other[1:] for other in remaining):
                break
        else:
            # No valid head: the hierarchy has no consistent linearisation.
            # Real Python would raise here; this tool falls back to declaration
            # order so one malformed hierarchy cannot break every query.
            for sequence in remaining:
                for item in sequence:
                    if item not in result:
                        result.append(item)
            return result

        result.append(head)
        remaining = [
            trimmed
            for trimmed in ([s[1:] if s[0] == head else s for s in remaining])
            if trimmed
        ]
    return result


def _definitions_by_symbol(parses: Iterable[ModuleParse]) -> dict[SymbolId, Definition]:
    """Definitions keyed by symbol, picking the arm the name means.

    Only `.kind` and `.symbol` are read back out of this, and neither varies
    between a property's arms or an `@overload` group's, so last-wins would
    give the same answers today. It goes through `canonical_definition` anyway
    because the next thing to read `.signature` or `.start_line` off this map
    would silently inherit the bug that one already caused in `RepoIndex`.
    """
    definitions: dict[SymbolId, Definition] = {}
    for parse in parses:
        for definition in parse.definitions:
            existing = definitions.get(definition.symbol)
            definitions[definition.symbol] = (
                definition if existing is None else canonical_definition(existing, definition)
            )
    return definitions


def _methods_by_owner(
    definitions: Iterable[Definition],
) -> dict[SymbolId, dict[str, SymbolId]]:
    owners: dict[SymbolId, dict[str, SymbolId]] = {}
    for definition in definitions:
        if definition.kind != "method":
            continue
        # A definition is only marked "method" when its enclosing scope is a
        # class, so the qualname always has an owner segment to split off.
        owner_qualname, _, name = definition.symbol.qualname.rpartition(".")
        owner = SymbolId(definition.symbol.path, owner_qualname)
        owners.setdefault(owner, {})[name] = definition.symbol
    return owners


def _resolve_base(
    dotted: str,
    parse: ModuleParse,
    index: ImportIndex,
    definitions: Mapping[SymbolId, Definition],
) -> SymbolId | None:
    """Resolve one base-class expression to a class in this repository.

    Lookup starts at module scope. A class whose bases name something local to
    an enclosing function is not resolved -- it is rare enough that guessing
    would cost more in wrong edges than it gains in right ones.
    """
    head, _, rest = dotted.partition(".")

    current: object | None = None
    for binding in index.bindings_for(parse.path).values():
        if binding.is_module_level and binding.local_name == head:
            current = binding.target
            break
    if current is None:
        current = parse.scope.defines.get(head)
    if current is None:
        return None

    for attribute in (part for part in rest.split(".") if part):
        if isinstance(current, ModuleRef):
            current = index.member(current, attribute)
        elif isinstance(current, SymbolId):
            # A nested class: `Outer.Inner` is a definition in the same file.
            nested = SymbolId(current.path, f"{current.qualname}.{attribute}")
            current = nested if nested in definitions else None
        else:
            return None
        if current is None:
            return None

    if not isinstance(current, SymbolId):
        return None
    definition = definitions.get(current)
    return current if definition is not None and definition.kind == "class" else None
