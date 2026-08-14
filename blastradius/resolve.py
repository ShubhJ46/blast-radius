"""Decide, for every name used in a module, which definition it refers to.

This is the pass that separates the tool from `grep`. A grep for `search` finds
comments, a local variable, an unrelated method on another class, and the one
call that matters. Here, a name is resolved against the scope it appears in and
the imports that were actually in effect, so those four are four different
answers.

Python's scoping rules that this implements, each of which grep cannot see:

- A name assigned *anywhere* in a function is local *throughout* it, so a local
  shadows an import even on lines above the assignment.
- A class body is skipped in the lookup chain of methods nested inside it: a
  bare name in a method does not see its own class's attributes.
- `global x` in a function makes `x` resolve at module scope regardless of any
  local assignment.

Two things are deliberately conservative. Comprehensions and lambdas get their
own scopes in real Python; here their targets and parameters are treated as
binding in the enclosing function. That over-shadows in the rare case where a
comprehension variable shares a name with an imported symbol, which drops a
reference rather than inventing one -- the direction this tool prefers to err in.

Attribute calls on values of unknown type (`handler.run()`) cannot be resolved
without type inference and are not guessed at. They are counted instead, in
`unresolved_attributes`, so the size of what is being left on the table is a
number rather than an impression.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field

from blastradius.imports import ImportIndex, ModuleRef
from blastradius.model import (
    ModuleParse,
    Reference,
    ReferenceVia,
    Scope,
    ScopeKind,
    SymbolId,
)
from blastradius.parse import LOCALS_MARKER

# Base expressions we could not name at all, e.g. `get_handler().run()`.
OPAQUE_BASE = "<expr>"


@dataclass(frozen=True)
class UnresolvedAttribute:
    """An attribute access this pass declined to guess at.

    Recorded rather than dropped because the count is the argument for building
    the next resolution strategy: if most of these have base `self`, that says
    where the remaining recall is.
    """

    attribute: str
    base: str  # "self", "os.path", or OPAQUE_BASE
    path: str
    line: int
    scope_qualname: str
    # True when the attribute is what is being called: `self.helper()` rather
    # than `self.helper`. Only the former can reach a definition this tool
    # indexes, so counting both together would overstate what type inference
    # could ever recover.
    in_call: bool = False


@dataclass(frozen=True)
class ModuleReferences:
    path: str
    references: tuple[Reference, ...]
    unresolved_attributes: tuple[UnresolvedAttribute, ...]


@dataclass
class _ScopeFrame:
    kind: ScopeKind
    qualname: str
    bound: set[str] = field(default_factory=set)
    declared_global: set[str] = field(default_factory=set)


def _import_alias_name(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str | None:
    """The name an import alias binds. Mirrors `imports.local_name_of`."""
    if alias.asname:
        return alias.asname
    if isinstance(node, ast.ImportFrom):
        return None if alias.name == "*" else alias.name
    return alias.name.split(".")[0] or None


def bound_names(body: list[ast.stmt], parameters: tuple[str, ...] = ()) -> _ScopeFrame:
    """Names a scope binds, without descending into nested function or class bodies.

    A nested `def` binds its own *name* here; its contents belong to its own
    scope and are collected when that scope is entered.
    """
    frame = _ScopeFrame(kind="function", qualname="", bound=set(parameters))
    stack: list[ast.AST] = list(body)

    while stack:
        node = stack.pop()

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            frame.bound.add(node.name)
            continue  # its body is a different scope
        if isinstance(node, ast.Global):
            frame.declared_global.update(node.names)
            continue
        if isinstance(node, ast.Nonlocal):
            # The name resolves in an enclosing function, never at module level,
            # so it can never reach a module-level definition either way.
            frame.bound.update(node.names)
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                name = _import_alias_name(node, alias)
                if name:
                    frame.bound.add(name)
            continue

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            frame.bound.add(node.id)
        elif isinstance(node, ast.arg):
            frame.bound.add(node.arg)  # reached through a lambda
        elif isinstance(node, ast.ExceptHandler) and node.name:
            frame.bound.add(node.name)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            frame.bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            frame.bound.add(node.rest)

        stack.extend(ast.iter_child_nodes(node))

    return frame


def _defines_by_scope(scope: Scope) -> dict[str, dict[str, SymbolId]]:
    """Flatten the parser's scope tree to qualname -> the definitions it binds."""
    flattened = {scope.qualname: scope.defines}
    for child in scope.children:
        flattened.update(_defines_by_scope(child))
    return flattened


class _ReferenceWalker(ast.NodeVisitor):
    def __init__(
        self,
        parse: ModuleParse,
        index: ImportIndex,
        imports_by_scope: Mapping[str, Mapping[str, object]],
        defines_by_scope: Mapping[str, Mapping[str, SymbolId]],
    ):
        self._parse = parse
        self._index = index
        self._imports = imports_by_scope
        self._defines = defines_by_scope
        self.references: list[Reference] = []
        self.unresolved: list[UnresolvedAttribute] = []
        self._scopes: list[_ScopeFrame] = []
        self._prefix: list[str] = []

    # -- scope handling ---------------------------------------------------

    def run(self, tree: ast.Module) -> None:
        frame = bound_names(tree.body)
        frame.kind = "module"
        self._scopes.append(frame)
        for statement in tree.body:
            self.visit(statement)
        self._scopes.pop()

    @property
    def _scope(self) -> _ScopeFrame:
        return self._scopes[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Bases and decorators are evaluated in the *enclosing* scope.
        for expression in [*node.bases, *node.decorator_list]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)

        qualname = ".".join([*self._prefix, node.name])
        frame = bound_names(node.body)
        frame.kind = "class"
        frame.qualname = qualname
        self._enter(frame, node.body, prefix_parts=[node.name])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expression in [*node.decorator_list, *_annotation_expressions(node)]:
            self.visit(expression)

        qualname = ".".join([*self._prefix, node.name])
        frame = bound_names(node.body, parameters=_parameter_names(node.args))
        frame.kind = "function"
        frame.qualname = qualname
        self._enter(frame, node.body, prefix_parts=[node.name, LOCALS_MARKER])

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _enter(self, frame: _ScopeFrame, body: list[ast.stmt], prefix_parts: list[str]) -> None:
        self._scopes.append(frame)
        self._prefix.extend(prefix_parts)
        for statement in body:
            self.visit(statement)
        del self._prefix[len(self._prefix) - len(prefix_parts) :]
        self._scopes.pop()

    # -- reference handling -----------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        target = self._resolve(node.id)
        if isinstance(target, SymbolId):
            self._record(target, node.lineno, via="name")

    def visit_Call(self, node: ast.Call) -> None:
        # Handled here rather than in visit_Attribute so that an attribute can
        # be told apart from a method call, which needs the parent node.
        if isinstance(node.func, ast.Attribute):
            self._attribute(node.func, in_call=True)
        else:
            self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._attribute(node, in_call=False)

    def _attribute(self, node: ast.Attribute, *, in_call: bool) -> None:
        if not isinstance(node.ctx, ast.Load):
            self.generic_visit(node)
            return

        base_module = self._module_of(node.value)
        if base_module is not None:
            member = self._index.member(base_module, node.attr)
            if isinstance(member, SymbolId):
                self._record(member, node.lineno, via="module_attr")
                return
            if isinstance(member, ModuleRef):
                # `pkg.sub` on the way to `pkg.sub.func`: a module, not a
                # definition, so there is nothing to record and nothing unknown.
                return

        self.unresolved.append(
            UnresolvedAttribute(
                attribute=node.attr,
                base=_base_name(node.value),
                path=self._parse.path,
                line=node.lineno,
                scope_qualname=self._scope.qualname,
                in_call=in_call,
            )
        )
        self.generic_visit(node)

    def _module_of(self, expression: ast.expr) -> ModuleRef | None:
        """The module an expression names, following dotted module paths.

        `import pkg.sub` binds only `pkg`, so reaching `pkg.sub.func` means
        walking `pkg` -> `pkg.sub` -> `func` one attribute at a time.
        """
        if isinstance(expression, ast.Name):
            target = self._resolve(expression.id)
            return target if isinstance(target, ModuleRef) else None
        if isinstance(expression, ast.Attribute):
            base = self._module_of(expression.value)
            if base is None:
                return None
            member = self._index.member(base, expression.attr)
            return member if isinstance(member, ModuleRef) else None
        return None

    def _record(self, target: SymbolId, line: int, *, via: ReferenceVia) -> None:
        self.references.append(
            Reference(
                target=target,
                path=self._parse.path,
                line=line,
                confidence="resolved",
                via=via,
            )
        )

    def _resolve(self, name: str) -> object | None:
        """Walk the scope chain the way the interpreter would."""
        chain = list(reversed(self._scopes))

        # `global x` anywhere in an enclosing function sends the lookup straight
        # to module scope, past any local of the same name.
        if any(name in frame.declared_global for frame in chain if frame.kind != "module"):
            chain = chain[-1:]

        for position, frame in enumerate(chain):
            # A class body is not in the lookup chain of functions nested inside
            # it -- only of code written directly in the body.
            if frame.kind == "class" and position != 0:
                continue

            imported = self._imports.get(frame.qualname, {}).get(name)
            if imported is not None:
                return imported

            defined = self._defines.get(frame.qualname, {}).get(name)
            if defined is not None:
                return defined

            if name in frame.bound:
                return None  # a local shadows everything further out
        return None


def _base_name(expression: ast.expr) -> str:
    """Name the thing an attribute hangs off, for counting purposes only.

    Deliberately stricter than `parse.dotted_name`, which unwraps calls so that
    `@cache(maxsize=1)` names the decorator `cache`. Here a call result is
    genuinely opaque: lumping `make().run()` in with `make.run()` would make the
    unresolved-attribute tally useless for deciding what to build next.
    """
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        base = _base_name(expression.value)
        return OPAQUE_BASE if base == OPAQUE_BASE else f"{base}.{expression.attr}"
    return OPAQUE_BASE


def _parameter_names(args: ast.arguments) -> tuple[str, ...]:
    names = [argument.arg for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return tuple(names)


def _annotation_expressions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """Defaults and annotations, which are evaluated in the enclosing scope.

    Missing these would drop real edges: a default of `handler=default_handler`
    is a reference, and so is every annotation naming a class in this repository.
    """
    args = node.args
    expressions: list[ast.expr] = [*args.defaults]
    expressions.extend(default for default in args.kw_defaults if default is not None)
    for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if argument.annotation:
            expressions.append(argument.annotation)
    for optional in (args.vararg, args.kwarg):
        if optional is not None and optional.annotation:
            expressions.append(optional.annotation)
    if node.returns:
        expressions.append(node.returns)
    return expressions


def resolve_module(parse: ModuleParse, tree: ast.Module, index: ImportIndex) -> ModuleReferences:
    """Resolve every name used in one module against the whole repository."""
    imports_by_scope: dict[str, dict[str, object]] = {}
    for binding in index.bindings_for(parse.path).values():
        imports_by_scope.setdefault(binding.scope_qualname, {})[binding.local_name] = (
            binding.target
        )

    walker = _ReferenceWalker(
        parse=parse,
        index=index,
        imports_by_scope=imports_by_scope,
        defines_by_scope=_defines_by_scope(parse.scope),
    )
    walker.run(tree)
    return ModuleReferences(
        path=parse.path,
        references=tuple(walker.references),
        unresolved_attributes=tuple(walker.unresolved),
    )
