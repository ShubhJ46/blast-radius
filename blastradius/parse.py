"""Turn one Python file into definitions and a scope tree.

Uses the standard library's `ast` rather than a third-party grammar. For Python
specifically this is the exact parser the interpreter uses, so it cannot drift
from the language, it already reports `end_lineno`, and it costs no dependency.
The C++ side of this project will not have that luxury.

This pass is deliberately incomplete in one respect: a scope's `defines` map
holds only `def` and `class` bindings. Assignments and imports also bind names,
but resolving what they point at needs the whole-repository import table, so
they are filled in by a later pass instead of being half-guessed here.
"""

import ast
from pathlib import Path

from blastradius.errors import ParseError
from blastradius.model import (
    Definition,
    DefinitionKind,
    ModuleParse,
    Parameter,
    RawImport,
    Scope,
    Signature,
    SymbolId,
)

# Python's own marker for "defined inside a function body". Reproduced rather
# than invented so a qualname this tool prints matches the one the interpreter
# would report for the same object.
LOCALS_MARKER = "<locals>"


def dotted_name(node: ast.expr) -> str | None:
    """Best-effort source-level dotted name for a base class or decorator.

    Returns None for expressions that have no name to speak of (a lambda, a
    dict literal). Two unwrappings are deliberate: `Base[T]` yields `Base`,
    because the subscript is a parameterisation rather than a different base;
    and `@cache(maxsize=1)` yields `cache`, because the call is applying the
    decorator, not naming a different one.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Subscript):
        return dotted_name(node.value)
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return None


def signature_of(args: ast.arguments) -> Signature:
    """Reduce an argument list to the parts that can force a caller to change.

    Order is declaration order, so a reordering shows up as a different tuple
    even when the same names are present.
    """
    parameters: list[Parameter] = []

    positional = [*args.posonlyargs, *args.args]
    # `defaults` right-aligns against the positional parameters: the last N
    # positional parameters are the ones carrying the N defaults.
    first_defaulted = len(positional) - len(args.defaults)
    for index, argument in enumerate(positional):
        kind = "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        parameters.append(Parameter(argument.arg, kind, index >= first_defaulted))

    if args.vararg:
        parameters.append(Parameter(args.vararg.arg, "var_positional", False))

    # kw_defaults is positionally aligned with kwonlyargs, holding None where a
    # keyword-only parameter has no default.
    for argument, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(Parameter(argument.arg, "keyword_only", default is not None))

    if args.kwarg:
        parameters.append(Parameter(args.kwarg.arg, "var_keyword", False))

    return Signature(tuple(parameters))


class _Walker(ast.NodeVisitor):
    """Collects definitions while tracking the qualname prefix and scope stack."""

    def __init__(self, path: str, module_end_line: int):
        self.path = path
        self.definitions: list[Definition] = []
        self.imports: list[RawImport] = []
        self.module_scope = Scope(
            kind="module",
            qualname="",
            start_line=1,
            end_line=module_end_line,
            defines={},
            children=[],
        )
        self._prefix: list[str] = []
        self._scopes: list[Scope] = [self.module_scope]

    @property
    def _scope(self) -> Scope:
        return self._scopes[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = tuple(name for name in (dotted_name(base) for base in node.bases) if name)
        definition = self._record(node, kind="class", signature=None, bases=bases)
        # A class body does not introduce a <locals> segment: methods are
        # reachable as Class.method, which is exactly what makes them
        # referenceable from another file.
        self._descend(node, definition, scope_kind="class", prefix_parts=[node.name])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind: DefinitionKind = "method" if self._scope.kind == "class" else "function"
        definition = self._record(node, kind=kind, signature=signature_of(node.args))
        self._descend(
            node, definition, scope_kind="function", prefix_parts=[node.name, LOCALS_MARKER]
        )

    # Async definitions differ only in the node type; everything recorded is the same.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                RawImport(
                    kind="import",
                    module=alias.name,
                    name=None,
                    asname=alias.asname,
                    level=0,
                    line=node.lineno,
                    scope_qualname=self._scope.qualname,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append(
                RawImport(
                    kind="from",
                    module=node.module,
                    name=alias.name,
                    asname=alias.asname,
                    level=node.level or 0,
                    line=node.lineno,
                    scope_qualname=self._scope.qualname,
                )
            )

    def _record(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        kind: DefinitionKind,
        signature: Signature | None,
        bases: tuple[str, ...] = (),
    ) -> Definition:
        symbol = SymbolId(path=self.path, qualname=".".join([*self._prefix, node.name]))
        decorators = tuple(
            name for name in (dotted_name(decorator) for decorator in node.decorator_list) if name
        )
        definition = Definition(
            symbol=symbol,
            kind=kind,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            signature=signature,
            bases=bases,
            decorators=decorators,
        )
        self.definitions.append(definition)
        self._scope.defines[node.name] = symbol
        return definition

    def _descend(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        definition: Definition,
        *,
        scope_kind: str,
        prefix_parts: list[str],
    ) -> None:
        scope = Scope(
            kind=scope_kind,  # type: ignore[arg-type]
            qualname=definition.symbol.qualname,
            start_line=definition.start_line,
            end_line=definition.end_line,
            defines={},
            children=[],
        )
        self._scope.children.append(scope)
        self._scopes.append(scope)
        self._prefix.extend(prefix_parts)

        # Only the body is walked. Decorators, base expressions, and default
        # values cannot contain a `def` or `class` statement, so nothing is
        # missed -- and walking them here would attribute their contents to the
        # wrong scope.
        for child in node.body:
            self.visit(child)

        del self._prefix[len(self._prefix) - len(prefix_parts) :]
        self._scopes.pop()


def parse_module(repo_path: str, source: str) -> ModuleParse:
    """Parse one module's source. `repo_path` is repo-relative with / separators."""
    try:
        tree = ast.parse(source, filename=repo_path)
    except (SyntaxError, ValueError) as error:
        # ValueError covers source containing null bytes, which `ast` rejects
        # separately from a syntax error.
        raise ParseError(f"{repo_path}: {error}") from error

    walker = _Walker(repo_path, module_end_line=max(len(source.splitlines()), 1))
    walker.visit(tree)
    return ModuleParse(
        path=repo_path,
        definitions=tuple(walker.definitions),
        scope=walker.module_scope,
        imports=tuple(walker.imports),
    )


def parse_file(path: Path, root: Path) -> ModuleParse:
    """Parse a file on disk, keyed by its path relative to the repository root."""
    repo_path = path.resolve().relative_to(root.resolve()).as_posix()
    # `errors="replace"` rather than "ignore": a mis-decoded byte becomes a
    # visible character instead of silently shifting every column after it.
    return parse_module(repo_path, path.read_text(encoding="utf-8", errors="replace"))
