"""Expose impact analysis to an agent over MCP.

The CLI answers one question and exits, which throws away the index every time
-- 20 seconds of work on a 441-module repository, per question. This server
keeps the index in memory instead, so the reuse in `build_index` can actually
pay off: an unchanged tree answers in about a third of a second, and a tree
with one edited file re-parses only that file.

That is the whole reason this module exists. Every other transport for this
tool is a process that exits, and a process that exits can never be fast enough
for an agent to ask several questions while it works.

Deliberately *not* named `mcp.py`: that shadows the `mcp` package it imports,
and the failure is a confusing ImportError rather than an obvious one.

Tool descriptions here say **when to call**, not just what the tool does. An
agent decides from the description alone, and "reports callers of a symbol"
does not tell it that the moment to call is *before* editing a signature.
"""

import argparse
import sys
import threading
from pathlib import Path

from blastradius.impact import Impact, find_symbols, impact_of
from blastradius.index import RepoIndex, build_index
from blastradius.model import SymbolId

# One index per repository root, reused across calls. This is the entire point
# of the server: `build_index` returns the previous index untouched when no file
# has changed, so repeated questions between edits cost a pass of hashing.
_INDEXES: dict[Path, RepoIndex] = {}
# Tools may be dispatched from a worker thread. Two concurrent calls would
# otherwise each rebuild from scratch and one would discard the other's work.
_LOCK = threading.Lock()

_DEFAULT_ROOT = Path.cwd()


def _index_for(root: str | None) -> RepoIndex:
    resolved = Path(root).expanduser().resolve() if root else _DEFAULT_ROOT
    with _LOCK:
        index = build_index(resolved, previous=_INDEXES.get(resolved))
        _INDEXES[resolved] = index
        return index


def _resolve(index: RepoIndex, symbol: str) -> SymbolId:
    """Turn what the agent typed into one symbol, or explain why it cannot.

    Ambiguity is reported rather than guessed at. Picking between two same-named
    methods is how a tool returns a confident answer about the wrong function,
    and an agent has no way to notice.
    """
    matches = find_symbols(index, symbol)
    if not matches:
        raise ValueError(
            f"No symbol matching {symbol!r} in {index.root}. "
            "Try a qualified name like 'Class.method' or 'path/to/file.py::qualname'."
        )
    if len(matches) > 1:
        listed = ", ".join(str(match) for match in matches[:10])
        raise ValueError(
            f"{symbol!r} is ambiguous -- it matches {len(matches)} definitions: {listed}. "
            "Call again with the full 'path/to/file.py::qualname' form."
        )
    return matches[0]


def _caveats(index: RepoIndex, impact: Impact) -> list[str]:
    """What this answer cannot see. Returned with every result on purpose.

    An agent that trusts a blast radius as complete will rename a symbol and
    break the callers this tool never resolved. Saying so costs a line of JSON.
    """
    notes = [
        "A dependency expressed as a string -- mock.patch('pkg.thing'), a Django "
        "'app.Model' setting, an entry point -- is invisible to this tool.",
    ]
    if index.unresolved_attribute_count:
        notes.append(
            f"{index.unresolved_attribute_count} attribute accesses in this repository "
            "are on values whose type is not declared, so some callers may be missing. "
            "Annotating the receiver makes them resolvable."
        )
    if impact.also_defined:
        notes.append(
            f"This name has {len(impact.also_defined) + 1} definitions in the same file "
            "-- a property's getter and setter, or an `@overload` group. `lines` and "
            "the signature describe the one the name means; the rest are under "
            "`also_defined` and usually have to change with it."
        )
    if impact.unverified:
        notes.append(
            f"{len(impact.unverified)} file(s) could not be parsed but mention this "
            "name -- listed under `unverified`. They are not in the blast radius "
            "because nothing resolved them, but they may contain real callers."
        )
    elif index.skipped:
        notes.append(
            f"{len(index.skipped)} file(s) could not be parsed and are not indexed, "
            "though none of them mention this symbol: "
            + ", ".join(path for path, _ in index.skipped[:5])
        )
    return notes


def build_server(root: Path) -> "object":
    """Construct the MCP server. Imported lazily so the library needs no `mcp`."""
    from mcp.server import MCPServer

    global _DEFAULT_ROOT
    _DEFAULT_ROOT = root

    server = MCPServer(
        name="blast-radius",
        version="0.1.0",
        instructions=(
            "Deterministic impact analysis for Python, from the AST. Call "
            "blast_impact before editing a function or method signature, and before "
            "renaming or deleting one, to find what depends on it. The answer is "
            "resolved against the scope chain, imports, and class hierarchy -- it is "
            "not a text search, so a same-named method on an unrelated class is not "
            "reported."
        ),
    )

    @server.tool()
    def blast_impact(symbol: str, argument: str | None = None, root: str | None = None) -> dict:
        """What breaks if you change this symbol. Call this BEFORE editing a
        function or method signature, renaming it, or deleting it.

        Answers from the AST, not a text search: `render` resolves to the one
        `Widget.render` you mean, not every method with that name.

        `symbol` accepts 'name', 'Class.method', or 'path/to/file.py::qualname'.
        Ambiguous names are refused with the list of candidates rather than
        guessed at -- call again with the fully qualified form.

        `argument` narrows the answer to a *specific* edit. Without it you get
        every caller, which is correct but includes callers the edit would not
        break. Pass the parameter you are removing or renaming and only the call
        sites that actually pass it come back -- on real commits that is the
        difference between eleven files to read and two.

        `root` defaults to the repository the server was started in.
        """
        index = _index_for(root)
        target = _resolve(index, symbol)
        impact = impact_of(index, target)

        signature = impact.definition.signature
        if argument is not None and not (
            signature is not None and any(p.name == argument for p in signature.parameters)
        ):
            known = (
                ", ".join(p.name for p in signature.parameters) if signature else "none"
            )
            raise ValueError(
                f"{argument!r} is not a parameter of {target}. Parameters: {known}. "
                "Omit `argument` to see every caller."
            )

        files = impact.files if argument is None else impact.files_affected_by(argument)
        callers = [
            {"path": reference.path, "line": reference.line, "via": reference.via}
            for reference in (
                impact.callers if argument is None else impact.affected_by(argument)
            )
        ]
        return {
            "symbol": str(target),
            "kind": impact.definition.kind,
            "lines": [impact.definition.start_line, impact.definition.end_line],
            "narrowed_to_argument": argument,
            "blast_radius": list(files),
            "callers": callers,
            "overrides": [str(override) for override in impact.overrides],
            "overridden": str(impact.overridden) if impact.overridden else None,
            # Files the parser could not read that mention this name. Not part
            # of the blast radius -- a text match is not evidence -- but listed
            # so "I could not see this" is distinguishable from "nothing
            # depends on this".
            "unverified": list(impact.unverified),
            "also_defined": [
                {"lines": [other.start_line, other.end_line], "decorators": list(other.decorators)}
                for other in impact.also_defined
            ],
            "caveats": _caveats(index, impact),
        }

    @server.tool()
    def blast_refs(symbol: str, root: str | None = None) -> dict:
        """Every resolved reference to a symbol, with file and line.

        Use this to read the call sites after blast_impact tells you which files
        are affected -- it saves grepping for a name that may appear in comments,
        strings, and unrelated same-named methods.
        """
        index = _index_for(root)
        target = _resolve(index, symbol)
        references = index.references_to(target)
        return {
            "symbol": str(target),
            "references": [
                {"path": reference.path, "line": reference.line, "via": reference.via}
                for reference in references
            ],
        }

    @server.tool()
    def blast_find(name: str, root: str | None = None) -> dict:
        """Resolve a bare name to the symbols it could mean.

        Call this when blast_impact reports an ambiguous name, to choose the
        fully qualified form to pass back.
        """
        index = _index_for(root)
        return {"query": name, "matches": [str(match) for match in find_symbols(index, name)]}

    @server.tool()
    def blast_stats(root: str | None = None) -> dict:
        """How much of the repository this tool can actually see.

        Call this once when the answers look thin: `skipped` files are not
        indexed at all, and a high `unresolved_attributes` means many callers go
        through values whose type is undeclared and cannot be resolved.
        """
        index = _index_for(root)
        return {
            "root": str(index.root),
            "modules": index.module_count,
            "definitions": len(index.definitions),
            "references": sum(len(uses) for uses in index.references.values()),
            "unresolved_attributes": index.unresolved_attribute_count,
            "skipped": [{"path": path, "reason": reason} for path, reason in index.skipped],
            "build_seconds": round(index.build_seconds, 3),
            "parses_reused": index.reused,
        }

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blast-mcp",
        description="Serve blast-radius impact analysis to an agent over MCP (stdio).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository to index (default: the working directory)",
    )
    arguments = parser.parse_args(argv)

    root = arguments.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    build_server(root).run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
