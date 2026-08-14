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
