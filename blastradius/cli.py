"""Command line entry point.

Two output modes on every command. The human one is for reading; `--json` is
for an agent or a script, and is the reason the tool exists in this shape --
something that has to be parsed out of prose is not a tool an agent can rely on.

Ambiguity is reported, never resolved silently. If `render` names three methods,
all three are listed and the command fails: guessing between them is how a tool
gives a confident answer about the wrong function.
"""

import argparse
import json
import sys
from pathlib import Path

from blastradius.impact import Impact, find_symbols, impact_of
from blastradius.index import RepoIndex, build_index
from blastradius.model import Reference, SymbolId

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _resolve_one(index: RepoIndex, query: str, stream) -> SymbolId | None:
    """Find the one symbol a query names, or explain why it did not."""
    matches = find_symbols(index, query)
    if not matches:
        print(f"No symbol matching {query!r} in {index.root}", file=stream)
        return None
    if len(matches) > 1:
        print(f"{query!r} is ambiguous; {len(matches)} symbols match:", file=stream)
        for symbol in matches:
            print(f"  {symbol}", file=stream)
        print("\nRe-run with a full 'path/to/file.py::qualname'.", file=stream)
        return None
    return matches[0]


def _warn_about_skipped(index: RepoIndex, stream) -> None:
    """A file that failed to parse is a hole in every answer that follows."""
    if not index.skipped:
        return
    print(
        f"warning: {_plural(len(index.skipped), 'file')} could not be parsed "
        f"and are missing from these results",
        file=stream,
    )
    for path, reason in index.skipped[:5]:
        print(f"  {path}: {reason}", file=stream)
    if len(index.skipped) > 5:
        print(f"  ... and {len(index.skipped) - 5} more", file=stream)


def _reference_rows(references: tuple[Reference, ...]) -> list[dict]:
    return [
        {"path": reference.path, "line": reference.line, "via": reference.via}
        for reference in references
    ]


def _print_impact(impact: Impact, stream, argument: str | None = None) -> None:
    definition = impact.definition
    print(
        f"{impact.symbol}  {definition.kind}  "
        f"lines {definition.start_line}-{definition.end_line}",
        file=stream,
    )

    print(
        f"\ncallers  {_plural(len(impact.callers), 'reference')} "
        f"in {_plural(len(impact.caller_files), 'file')}",
        file=stream,
    )
    for reference in impact.callers:
        print(f"  {reference.path}:{reference.line}  ({reference.via})", file=stream)
    if not impact.callers:
        print("  none", file=stream)

    print(f"\noverrides  {len(impact.overrides)}", file=stream)
    for override in impact.overrides:
        print(f"  {override}", file=stream)
    if not impact.overrides:
        print("  none", file=stream)

    print(f"\noverridden  {impact.overridden or 'none'}", file=stream)

    files = impact.files if argument is None else impact.files_affected_by(argument)
    heading = "blast radius" if argument is None else f"blast radius of changing {argument!r}"
    print(f"\n{heading}  {_plural(len(files), 'file')}", file=stream)
    for path in files:
        print(f"  {path}", file=stream)
    if not files:
        print("  none", file=stream)

    signature = impact.definition.signature
    known = signature is not None and any(p.name == argument for p in signature.parameters)
    if argument is not None and not known:
        # Better to say so than to report an empty radius that looks like an
        # answer: a typo in the parameter name would otherwise read as
        # "nothing depends on this".
        print(f"  note: {argument!r} is not a parameter of this symbol", file=stream)


def _impact_payload(impact: Impact) -> dict:
    return {
        "symbol": str(impact.symbol),
        "kind": impact.definition.kind,
        "start_line": impact.definition.start_line,
        "end_line": impact.definition.end_line,
        "callers": _reference_rows(impact.callers),
        "overrides": [str(override) for override in impact.overrides],
        "overridden": str(impact.overridden) if impact.overridden else None,
        "files": list(impact.files),
    }


def _impact_payload_for(impact: Impact, argument: str) -> dict:
    payload = _impact_payload(impact)
    payload["argument"] = argument
    payload["files"] = list(impact.files_affected_by(argument))
    payload["all_caller_files"] = list(impact.files)
    return payload


def _command_impact(
    index: RepoIndex, query: str, as_json: bool, out, err, argument: str | None = None
) -> int:
    symbol = _resolve_one(index, query, err)
    if symbol is None:
        return EXIT_NOT_FOUND

    impact = impact_of(index, symbol)
    if as_json:
        payload = (
            _impact_payload(impact)
            if argument is None
            else _impact_payload_for(impact, argument)
        )
        print(json.dumps(payload, indent=2), file=out)
    else:
        _print_impact(impact, out, argument)
    return EXIT_OK


def _command_refs(index: RepoIndex, query: str, as_json: bool, out, err) -> int:
    symbol = _resolve_one(index, query, err)
    if symbol is None:
        return EXIT_NOT_FOUND

    references = index.references_to(symbol)
    if as_json:
        payload = {"symbol": str(symbol), "references": _reference_rows(references)}
        print(json.dumps(payload, indent=2), file=out)
        return EXIT_OK

    print(f"{symbol}  {_plural(len(references), 'reference')}", file=out)
    for reference in references:
        print(f"  {reference.path}:{reference.line}  ({reference.via})", file=out)
    if not references:
        print("  none", file=out)
    return EXIT_OK


def _command_stats(index: RepoIndex, as_json: bool, out, _err) -> int:
    classes = sum(
        1
        for definition in index.definitions.values()
        if definition.kind == "class"
    )
    payload = {
        "root": str(index.root),
        "modules": index.module_count,
        "definitions": len(index.definitions),
        "classes": classes,
        "referenced_symbols": len(index.references),
        "references": sum(len(uses) for uses in index.references.values()),
        "unresolved_attributes": index.unresolved_attribute_count,
        "skipped": [{"path": path, "reason": reason} for path, reason in index.skipped],
        "build_seconds": round(index.build_seconds, 3),
    }
    if as_json:
        print(json.dumps(payload, indent=2), file=out)
        return EXIT_OK

    print(f"{index.root}", file=out)
    print(f"  modules              {payload['modules']}", file=out)
    print(f"  definitions          {payload['definitions']} ({classes} classes)", file=out)
    print(f"  references resolved  {payload['references']}", file=out)
    print(f"  unresolved attrs     {payload['unresolved_attributes']}", file=out)
    print(f"  files skipped        {len(index.skipped)}", file=out)
    print(f"  built in             {payload['build_seconds']}s", file=out)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository to index (default: the current directory)",
    )
    shared.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="blast",
        description="Deterministic impact analysis: what breaks if I change this?",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    impact = subcommands.add_parser(
        "impact", parents=[shared], help="callers and overrides of a symbol"
    )
    impact.add_argument("symbol", help="'name', 'Class.method', or 'path.py::qualname'")
    impact.add_argument(
        "--argument",
        metavar="NAME",
        help=(
            "narrow to callers a change to this parameter would force to edit, "
            "rather than every caller"
        ),
    )

    refs = subcommands.add_parser(
        "refs", parents=[shared], help="every reference to a symbol"
    )
    refs.add_argument("symbol", help="'name', 'Class.method', or 'path.py::qualname'")

    subcommands.add_parser("stats", parents=[shared], help="what the index contains")
    return parser


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    arguments = _build_parser().parse_args(argv)

    try:
        index = build_index(arguments.root)
    except ValueError as error:
        print(f"error: {error}", file=err)
        return EXIT_USAGE

    if not arguments.json:
        _warn_about_skipped(index, err)

    if arguments.command == "impact":
        return _command_impact(
            index, arguments.symbol, arguments.json, out, err, arguments.argument
        )
    if arguments.command == "refs":
        return _command_refs(index, arguments.symbol, arguments.json, out, err)
    return _command_stats(index, arguments.json, out, err)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
