"""Answer the question the whole tool exists for: what breaks if I change this?

Stage 1 answers it with two things, both of which are facts rather than
estimates: the places that call the symbol, and the subclass methods that
override it. Callers must change when a signature changes; overrides must keep a
compatible one. `overridden` points the other way -- at the inherited definition
whose contract is being altered -- which is the thing most likely to make
someone stop and reconsider the change.

Not included, deliberately: transitive importers, test coverage, and public-API
exposure. Each would widen the predicted set, and a prediction that widens
without evidence trades precision for the appearance of thoroughness.

`Impact.files` is the prediction the evaluation harness scores. It excludes the
file the symbol is defined in: every tool gets that one right, so counting it
inflates precision and recall equally and tells a reader nothing.
"""

from dataclasses import dataclass

from blastradius.index import RepoIndex
from blastradius.model import Definition, Reference, SymbolId


@dataclass(frozen=True)
class Impact:
    symbol: SymbolId
    definition: Definition
    callers: tuple[Reference, ...]
    overrides: tuple[SymbolId, ...]  # subclass methods that must stay compatible
    overridden: SymbolId | None  # the inherited definition this one overrides

    @property
    def files(self) -> tuple[str, ...]:
        """Files a change to this symbol is predicted to touch."""
        paths = {reference.path for reference in self.callers}
        paths |= {override.path for override in self.overrides}
        paths.discard(self.symbol.path)
        return tuple(sorted(paths))

    def parameter_index(self, name: str) -> int | None:
        """Where `name` sits in the parameter list, counting `self`."""
        if self.definition.signature is None:
            return None
        for position, parameter in enumerate(self.definition.signature.parameters):
            if parameter.name == name:
                # Only a parameter that can be passed positionally has a
                # meaningful index; a keyword-only one is never reached by
                # counting arguments.
                if parameter.kind in ("positional_only", "positional_or_keyword"):
                    return position
                return None
        return None

    def affected_by(self, parameter: str, *, supplied: bool = True) -> tuple[Reference, ...]:
        """Callers a change to `parameter` actually forces to move.

        Every caller is a real dependency; not every one is work. Removing or
        renaming a parameter only breaks the call sites that pass it, and
        `f(x)` is untouched while `f(x, can_borrow=True)` is not. On the mined
        evaluation this is the difference between eleven files and two.

        `supplied=False` asks the opposite question, for a parameter that has
        just *lost* its default: there it is the callers omitting it that break.

        A caller whose arguments could not be lined up with the signature --
        `f(*args)`, or a member reached through its class -- counts as affected
        either way, because the alternative is dropping a dependency on a guess.
        """
        index = self.parameter_index(parameter)
        chosen = []
        for reference in self.callers:
            if reference.call is None:
                continue  # a bound method passed as a value, not called here
            passes = reference.call.supplies(parameter, index)
            if passes == supplied or reference.call.opaque:
                chosen.append(reference)
        return tuple(chosen)

    def files_affected_by(self, parameter: str, *, supplied: bool = True) -> tuple[str, ...]:
        paths = {reference.path for reference in self.affected_by(parameter, supplied=supplied)}
        paths |= {override.path for override in self.overrides}
        paths.discard(self.symbol.path)
        return tuple(sorted(paths))

    @property
    def caller_files(self) -> tuple[str, ...]:
        return tuple(sorted({reference.path for reference in self.callers}))

    @property
    def is_empty(self) -> bool:
        return not self.callers and not self.overrides


def impact_of(index: RepoIndex, symbol: SymbolId) -> Impact:
    """Collect the blast radius of a change to one symbol."""
    definition = index.definitions.get(symbol)
    if definition is None:
        raise KeyError(f"No such symbol in the index: {symbol}")

    return Impact(
        symbol=symbol,
        definition=definition,
        callers=index.references_to(symbol),
        overrides=index.classes.overrides_of(symbol),
        overridden=index.classes.overridden(symbol),
    )


def find_symbols(index: RepoIndex, query: str) -> list[SymbolId]:
    """Resolve what a caller typed into the symbols it could mean.

    Three forms, tried in order, so that a precise query is never widened by a
    looser match: a full `path::qualname`, then an exact qualname in any file,
    then a bare final segment -- `render` finding `Widget.render`.

    Returning every match rather than picking one keeps the ambiguity the
    caller's to resolve. Guessing between two same-named methods is how a tool
    reports a confident answer about the wrong function.
    """
    if "::" in query:
        symbol = SymbolId.parse(query)
        return [symbol] if symbol in index.definitions else []

    exact = [symbol for symbol in index.definitions if symbol.qualname == query]
    if exact:
        return sorted(exact)

    suffix = f".{query}"
    return sorted(
        symbol for symbol in index.definitions if symbol.qualname.endswith(suffix)
    )
