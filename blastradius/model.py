"""The vocabulary the rest of the tool is written in.

Two decisions here shape everything downstream.

**Symbol identity is (file, qualname).** Not a dotted module path: two files can
resolve to the same module name under different roots, and a repo-relative path
is what git diffs, coverage tools, and error messages already speak. Everything
that crosses a module boundary is keyed on this.

**References record how they were resolved, not just that they were.** A call
found by walking a scope chain is a fact; a call matched only because the
attribute happened to share a name is a guess. Collapsing the two into one
"reference" makes the tool's output untrustworthy in exactly the cases where an
agent most needs to trust it, so the distinction is carried in the data rather
than decided at the point of discovery.
"""

from dataclasses import dataclass
from typing import Literal

DefinitionKind = Literal["function", "method", "class"]

ParameterKind = Literal[
    "positional_only",
    "positional_or_keyword",
    "var_positional",
    "keyword_only",
    "var_keyword",
]

ScopeKind = Literal["module", "class", "function"]

# How a reference was arrived at. Ordered loosely from most to least certain;
# the evaluation harness reports recall broken down by this field, so that the
# contribution of each resolution strategy is visible rather than assumed.
ReferenceVia = Literal[
    "name",
    "self_attr",
    "class_attr",
    "module_attr",
    "constructor",  # `C(...)`, which runs `C.__init__` without ever naming it
    "typed_attr",  # `w.render()` where `w` was *declared* to be a `Widget`
    "unresolved_attr",
]

# `resolved` means a scope chain, class MRO, or import binding proved the target.
# `name_match` means only the name agreed. The CLI reports `resolved` alone by
# default; `name_match` is opt-in, because a tool an agent cannot trust by
# default is worse than one that admits it does not know.
Confidence = Literal["resolved", "name_match"]


@dataclass(frozen=True, order=True)
class SymbolId:
    """A definition's stable identity across the repository."""

    path: str  # repo-relative, POSIX separators: "blastradius/parse.py"
    qualname: str  # Python __qualname__ form: "Walker.visit_ClassDef"

    def __str__(self) -> str:
        return f"{self.path}::{self.qualname}"

    @classmethod
    def parse(cls, text: str) -> "SymbolId":
        path, separator, qualname = text.partition("::")
        if not separator or not path or not qualname:
            raise ValueError(f"Not a symbol id: {text!r}. Expected 'path/to/file.py::qualname'.")
        return cls(path=path, qualname=qualname)


@dataclass(frozen=True)
class Parameter:
    """One entry in a signature, reduced to what can force a caller to change.

    Annotations and default *values* are deliberately not recorded. Retyping a
    parameter or changing its default alters behaviour but does not oblige any
    call site to be edited, and including them would flood the evaluation set
    with commits whose blast radius is genuinely empty.
    """

    name: str
    kind: ParameterKind
    has_default: bool


@dataclass(frozen=True)
class Signature:
    parameters: tuple[Parameter, ...]

    def positional_names(self) -> tuple[str, ...]:
        return tuple(
            parameter.name
            for parameter in self.parameters
            if parameter.kind in ("positional_only", "positional_or_keyword")
        )

    def required_names(self) -> frozenset[str]:
        return frozenset(
            parameter.name
            for parameter in self.parameters
            if not parameter.has_default
            and parameter.kind not in ("var_positional", "var_keyword")
        )


@dataclass(frozen=True)
class Definition:
    """A function, method, or class, as found by the parser.

    `bases` and `decorators` hold source-level dotted names, not resolved
    symbols: resolution needs the whole index and happens later. Keeping the
    raw text means a base this tool cannot resolve is still visible to a human
    reading the output, instead of vanishing.
    """

    symbol: SymbolId
    kind: DefinitionKind
    start_line: int
    end_line: int
    signature: Signature | None = None  # None for classes
    bases: tuple[str, ...] = ()
    decorators: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.symbol.qualname.rsplit(".", 1)[-1]

    @property
    def is_nested(self) -> bool:
        """True for definitions inside a function body.

        These cannot be imported, so they are unreachable from another file and
        carry no cross-file blast radius.
        """
        return "<locals>" in self.symbol.qualname

    @property
    def is_overload_stub(self) -> bool:
        """True for an `@overload` arm, which declares a type and no callable.

        The implementation below the stubs is the only signature a caller has
        to satisfy, so a stub's parameters must never be mistaken for the
        symbol's own.

        Matched on the decorator's source text, which is what a `Definition`
        carries. A local `def overload` shadowing `typing.overload` would be
        read as the real thing -- see `canonical_definition` for why that is
        tolerable here and where it stops being so.
        """
        return any(name == "overload" or name.endswith(".overload") for name in self.decorators)

    @property
    def is_property_mutator(self) -> bool:
        """True for a `@x.setter` or `@x.deleter` arm of a property.

        These share a qualname with the `@property` getter without replacing
        it: the three compose into one descriptor rather than the last one
        winning, which is what makes the getter the definition the name means.

        Text-matched like `is_overload_stub`: a descriptor of one's own that
        borrows the `.setter` spelling would read as a property.
        """
        return any(name.endswith((".setter", ".deleter")) for name in self.decorators)


def canonical_definition(previous: Definition, current: Definition) -> Definition:
    """Which of two definitions sharing one `SymbolId` the name actually means.

    A qualname is not a unique key. Three shapes produce more than one
    definition under one name, and they do not all resolve the same way:

    * A plain redefinition -- two `def`s in different branches of an `if` --
      *replaces* the earlier binding. The last one is what Python leaves bound,
      so the last one wins, which is also what this function does by default.
    * An `@overload` group *composes*: the stubs declare types for a checker
      and only the undecorated implementation is callable. Taking the last arm
      is right only when the implementation happens to be written last, which
      across four corpora it is in 8 of 19 groups -- so in the other 11 the
      index held a stub's parameters and answered `--argument` against a
      signature that does not exist at runtime.
    * A property composes the same way, and here the last arm is the *setter*
      in every case measured. Reporting `(self, value)` and the setter's line
      range for `Widget.name` is wrong in both halves.

    Preferring the real callable is not a guess between two candidates: the
    stub and the mutator are parts of one object, not rivals to it. Where there
    is a genuine choice -- two plain redefinitions -- this defers to Python.

    The two predicates read decorator *text*, so a local name that borrows the
    `overload` or `.setter` spelling is taken at its word. That is a weaker
    footing than `CallShape.opaque`, which refuses to guess when a decorator
    would have to be resolved, and the difference is deliberate: here a wrong
    answer picks a same-named arm a few lines away, where there it would
    mis-count every argument at a call site. Resolving the decorator through
    `ImportIndex` is what to do if that ever stops being true.

    Lives here rather than beside its caller because it is a fact about what a
    `Definition` means, and three places build a map keyed by `SymbolId` --
    `RepoIndex`, `ClassGraph`, and `ModuleParse.by_qualname` -- each of which
    would otherwise have to rediscover it.
    """
    if current.is_overload_stub != previous.is_overload_stub:
        return previous if current.is_overload_stub else current
    if current.is_property_mutator != previous.is_property_mutator:
        return previous if current.is_property_mutator else current
    return current


@dataclass(frozen=True)
class CallShape:
    """How a call site passed its arguments.

    Enough to answer "does this call supply parameter `p`?" without recording
    the arguments themselves. That question is what separates a dependency from
    a dependency that has to *change*: when `can_borrow` is removed from a
    method, `f(x, can_borrow=True)` breaks and `f(x)` does not, and both are
    equally real callers.

    `positional` counts an implicit receiver, so it lines up with the parameter
    list as written: `w.render(x)` reports 2, because `render(self, x)` has `x`
    at index 1.

    `opaque` marks a call whose arguments cannot be lined up with the signature
    at all -- `f(*args)`, or an access through a class where a `classmethod`
    binds `cls` and a plain method does not, which cannot be told apart without
    resolving decorators. An opaque call answers "yes" to every question, so an
    unanalysable caller is over-reported rather than quietly dropped.
    """

    positional: int
    keywords: frozenset[str]
    opaque: bool = False

    def supplies(self, name: str, index: int | None) -> bool:
        """Whether this call passes `name`, given its index in the signature."""
        if self.opaque or name in self.keywords:
            return True
        return index is not None and self.positional > index


@dataclass(frozen=True)
class Reference:
    """A use of a definition, somewhere in the repository."""

    target: SymbolId
    path: str
    line: int
    confidence: Confidence
    via: ReferenceVia
    # Present only when the reference *is* a call. A bound method passed as a
    # value (`handler = self.helper`) has no arguments to describe.
    call: CallShape | None = None


ImportKind = Literal["import", "from"]


@dataclass(frozen=True)
class RawImport:
    """One import statement, recorded verbatim before anything is resolved.

    Resolution needs the whole repository -- whether `from pkg import thing`
    names a submodule or a function depends on what else exists on disk -- so
    the parser records what the statement said and `imports.py` decides later.
    Re-parsing each file to answer that question would double indexing cost for
    no benefit.

    `scope_qualname` is "" for a module-level import. Function-local imports are
    common enough (circular-import workarounds, optional dependencies) that
    dropping them would lose real edges.
    """

    kind: ImportKind
    module: str | None  # "a.b.c" for `import a.b.c`; None for `from . import x`
    name: str | None  # imported attribute for a from-import; None for plain import
    asname: str | None  # the alias, if one was given
    level: int  # 0 absolute, 1 for `.`, 2 for `..`
    line: int
    scope_qualname: str

    @property
    def is_star(self) -> bool:
        return self.name == "*"


@dataclass
class Scope:
    """A lexical scope, and the definitions bound directly inside it.

    Only `def` and `class` bindings are recorded at parse time. Assignments,
    imports, parameters, and comprehension variables also bind names, but
    resolving those needs the import table, so they are added by a later pass
    rather than half-modelled here.
    """

    kind: ScopeKind
    qualname: str  # "" for the module scope
    start_line: int
    end_line: int
    defines: dict[str, SymbolId]
    children: list["Scope"]


@dataclass(frozen=True)
class ModuleParse:
    """Everything one file yields on a single pass."""

    path: str
    definitions: tuple[Definition, ...]
    scope: Scope
    imports: tuple[RawImport, ...] = ()

    def by_qualname(self) -> dict[str, Definition]:
        """Definitions keyed by qualname, last one winning.

        A qualname is not unique -- a property's arms and an `@overload` group
        share one -- so this drops definitions silently and is only safe when
        the caller already knows the name is singular. Building the same map
        over a whole repository is what made `blastradius impact` report a
        property's setter as the definition of its name; `RepoIndex` picks the
        right arm through `canonical_definition` rather than reusing this.
        """
        return {definition.symbol.qualname: definition for definition in self.definitions}
