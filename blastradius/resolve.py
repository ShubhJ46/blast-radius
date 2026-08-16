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

from blastradius.classes import ClassGraph
from blastradius.imports import ImportIndex, ModuleRef
from blastradius.model import (
    CallShape,
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

# Attribute bases treated as referring to the enclosing class. This is the
# convention every Python tool relies on, not a language rule: a method could
# name its first parameter anything. Being wrong here means resolving a call on
# a staticmethod's oddly-named first argument, which is a defect in the source
# either way.
SELF_NAMES = frozenset({"self", "cls"})


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
    # name -> the annotation expression declaring its type. A parameter
    # annotation or an `x: Widget` assignment is a statement the author made
    # about the code, not an inference this pass performed, which is why acting
    # on it does not cross the line into guessing.
    declared: dict[str, ast.expr] = field(default_factory=dict)
    # Class frames only: attribute -> its declared type, from `self.x: Widget`
    # anywhere in the class or `x: Widget` in the class body. Kept on the class
    # rather than the method that wrote it, because that is the scope every
    # other method reads it from.
    attributes: dict[str, ast.expr] = field(default_factory=dict)


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

        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            frame.declared[node.target.id] = node.annotation

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


def declared_attributes(node: ast.ClassDef) -> dict[str, ast.expr]:
    """Types the class declares for its own attributes.

    Two forms, because both are the author writing the type down: `x: Widget`
    in the class body, and `self.x: Widget` anywhere inside a method -- almost
    always `__init__`. The second is why this walks method bodies at all: the
    declaration is written in one method and read from every other one.

    Nested classes are skipped. Their `self` is their own, and inheriting the
    outer class's attribute types into them would be a guess.

    Pre-order so the first declaration of a name wins, which makes the result
    depend on the source rather than on dictionary ordering.
    """
    found: dict[str, ast.expr] = {}
    stack: list[ast.AST] = list(reversed(node.body))
    while stack:
        current = stack.pop()
        if isinstance(current, ast.ClassDef):
            continue
        if isinstance(current, ast.AnnAssign) and current.annotation is not None:
            target = current.target
            if isinstance(target, ast.Name):
                found.setdefault(target.id, current.annotation)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in SELF_NAMES
            ):
                found.setdefault(target.attr, current.annotation)
        stack.extend(reversed(list(ast.iter_child_nodes(current))))
    return found


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
        graph: ClassGraph,
        imports_by_scope: Mapping[str, Mapping[str, object]],
        defines_by_scope: Mapping[str, Mapping[str, SymbolId]],
    ):
        self._parse = parse
        self._index = index
        self._graph = graph
        self._imports = imports_by_scope
        self._defines = defines_by_scope
        self.references: list[Reference] = []
        self.unresolved: list[UnresolvedAttribute] = []
        self._scopes: list[_ScopeFrame] = []
        self._prefix: list[str] = []
        self._calls: list[CallShape] = []

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
        frame.attributes = declared_attributes(node)
        self._enter(frame, node.body, prefix_parts=[node.name])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expression in [*node.decorator_list, *_annotation_expressions(node)]:
            self.visit(expression)

        qualname = ".".join([*self._prefix, node.name])
        frame = bound_names(node.body, parameters=_parameter_names(node.args))
        frame.kind = "function"
        frame.qualname = qualname
        # Parameter annotations are the single largest source of declared types
        # -- 27% of all unresolved attribute calls in mypy -- so they are added
        # after the body walk, which cannot see them.
        frame.declared.update(_annotated_parameters(node.args))
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
        #
        # The call's shape is pushed while the callee is resolved, so the
        # reference recorded for it carries the arguments it was given. The
        # stack is popped before the arguments themselves are walked, so a name
        # used *as* an argument is not tagged with the enclosing call.
        shape = _call_shape(node)
        self._calls.append(shape)
        if isinstance(node.func, ast.Attribute):
            self._attribute(node.func, in_call=True)
        else:
            self.visit(node.func)
        self._calls.pop()

        self._constructor(node.func, shape)
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

        if isinstance(node.value, ast.Name) and node.value.id in SELF_NAMES:
            method = self._method_on_enclosing_class(node.attr)
            if method is not None:
                self._record(method, node.lineno, via="self_attr")
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

        base_class = self._class_of(node.value)
        if base_class is not None:
            member = self._graph.lookup_method(base_class, node.attr)
            if member is not None:
                self._record(member, node.lineno, via="class_attr")
                # The class itself is still referred to here, and dropping that
                # would lose the edge that a rename of the class depends on.
                self.visit(node.value)
                return

        # Both forms are the same evidence -- the author declared the receiver's
        # type -- so both are recorded as `typed_attr`.
        declared_class = self._declared_class(node.value) or self._declared_attribute_class(
            node.value
        )
        if declared_class is not None:
            member = self._graph.lookup_method(declared_class, node.attr)
            if member is not None:
                self._record(member, node.lineno, via="typed_attr")
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

    def _constructor(self, func: ast.expr, shape: "CallShape | None" = None) -> None:
        """Record `C(...)` as a use of `C.__init__`.

        Calling a class runs its initialiser, but the call site names the class
        and never the method. Without this, asking for the blast radius of an
        `__init__` returns nothing at all -- the most misleading empty answer
        this tool can give, since adding a required parameter to an initialiser
        forces every construction site in the repository to change. Two of the
        twelve recall misses in the first scored evaluation were exactly this.

        An inherited initialiser resolves through the MRO, so a subclass built
        with `Child()` still points at the base that actually defines it.
        """
        target = self._class_of(func)
        if target is None:
            return
        initialiser = self._graph.lookup_method(target, "__init__")
        if initialiser is not None:
            self._record(initialiser, func.lineno, via="constructor", call=shape)

    def _class_of(self, expression: ast.expr) -> SymbolId | None:
        """The class an expression names, if it names one at all.

        Deliberately only handles a bare name and a module attribute -- both
        cases where the scope chain or the import graph *proves* what the class
        is. `value.attr` where `value` is a variable needs type inference, and
        guessing there is how a tool starts reporting confident nonsense.
        """
        if isinstance(expression, ast.Name):
            target = self._resolve(expression.id)
        elif isinstance(expression, ast.Attribute):
            base = self._module_of(expression.value)
            target = self._index.member(base, expression.attr) if base is not None else None
        else:
            return None
        if isinstance(target, SymbolId) and self._graph.node(target) is not None:
            return target
        return None

    def _declared_class(self, expression: ast.expr) -> SymbolId | None:
        """The class a name was *declared* to hold, from an annotation.

        This is the one place the tool acts on a type, and it is deliberately
        reading rather than inferring: `def render(w: Widget)` is a statement
        the author wrote down, the same kind of evidence as an import. No
        attempt is made to track what a variable was last assigned, because
        that is inference and it is wrong as soon as the variable is reassigned.

        The declared type may name a base class while the value is a subclass
        overriding the method. That is the same assumption `self_attr` already
        makes, and `overrides` reports those subclasses separately.

        Worth 27% of unresolved attribute calls in mypy and 5% in the standard
        library -- the gap is how heavily annotated the codebase is, so this is
        worth much more on the modern code an agent is asked to edit.
        """
        if not isinstance(expression, ast.Name):
            return None
        annotation = self._declared_annotation(expression.id)
        return None if annotation is None else self._class_of_annotation(annotation)

    def _declared_attribute_class(self, expression: ast.expr) -> SymbolId | None:
        """The class `self.x` was declared to hold, for `self.x.method()`.

        The declaration is read from the nearest enclosing class, which is where
        `self` points -- not from the method that happened to write it down.

        Only the enclosing class's own declarations are consulted. An attribute
        annotated on a base class is not inherited here: that needs the
        declarations to live on the class graph rather than on this walk, and
        reading through an unresolved base would be a guess.
        """
        if not (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id in SELF_NAMES
        ):
            return None
        owner = next(
            (frame for frame in reversed(self._scopes) if frame.kind == "class"), None
        )
        if owner is None:
            return None
        annotation = owner.attributes.get(expression.attr)
        return None if annotation is None else self._class_of_annotation(annotation)

    def _class_of_annotation(self, annotation: ast.expr) -> SymbolId | None:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            # A forward reference, `w: 'Widget'`. Only a bare name is honoured;
            # anything with brackets or dots inside the string is left alone.
            target = self._resolve(annotation.value.strip())
            if isinstance(target, SymbolId) and self._graph.node(target) is not None:
                return target
            return None
        # `_class_of` handles a bare name and a module attribute, and rejects
        # everything else -- so `list[Widget]` and `Widget | None` resolve to
        # nothing rather than to their container.
        return self._class_of(annotation)

    def _declared_annotation(self, name: str) -> ast.expr | None:
        """Find a declaration for `name`, walking scopes the way a lookup does."""
        for position, frame in enumerate(reversed(self._scopes)):
            if frame.kind == "class" and position != 0:
                continue
            declared = frame.declared.get(name)
            if declared is not None:
                return declared
            if name in frame.bound:
                return None  # bound here without a declaration: shadows any outer one
        return None

    def _method_on_enclosing_class(self, name: str) -> SymbolId | None:
        """Resolve `self.name` against the class the current code sits inside.

        The nearest enclosing class scope is the right one even from a function
        nested inside a method, where `self` is a closure variable rather than a
        parameter -- and also when a class is declared inside a method, where
        `self` belongs to the inner class.
        """
        owner = next(
            (frame.qualname for frame in reversed(self._scopes) if frame.kind == "class"),
            None,
        )
        if owner is None:
            return None
        return self._graph.lookup_method(SymbolId(self._parse.path, owner), name)

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

    def _record(
        self, target: SymbolId, line: int, *, via: ReferenceVia, call: CallShape | None = None
    ) -> None:
        shape = call if call is not None else (self._calls[-1] if self._calls else None)
        self.references.append(
            Reference(
                target=target,
                path=self._parse.path,
                line=line,
                confidence="resolved",
                via=via,
                call=None if shape is None else _bind_receiver(shape, via),
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


# Strategies where the receiver is bound by the call itself, so the first
# declared parameter -- `self`, or `cls` on a constructor -- is already filled.
_BOUND_VIA = frozenset({"self_attr", "typed_attr", "constructor"})


def _call_shape(node: ast.Call) -> CallShape:
    """The arguments a call site passes, before any receiver adjustment.

    `*args` and `**kwargs` make the call opaque: what they expand to is a
    runtime value, so no claim can be made about which parameters are supplied.
    """
    opaque = any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    )
    return CallShape(
        positional=sum(1 for a in node.args if not isinstance(a, ast.Starred)),
        keywords=frozenset(k.arg for k in node.keywords if k.arg is not None),
        opaque=opaque,
    )


def _bind_receiver(shape: CallShape, via: ReferenceVia) -> CallShape:
    """Line a call's arguments up with the parameter list as written.

    `w.render(x)` fills `render(self, x)` two slots deep, because the receiver
    took the first. Getting this wrong is an off-by-one that silently reports
    the wrong callers, which is worse than reporting too many.

    A member reached through the class (`Config.read(...)`) is left opaque: a
    `classmethod` binds `cls` there and a plain method does not, and telling
    them apart needs decorator resolution. Over-reporting that small category
    beats guessing at it.
    """
    if via in _BOUND_VIA:
        return CallShape(shape.positional + 1, shape.keywords, shape.opaque)
    if via == "class_attr":
        return CallShape(shape.positional, shape.keywords, opaque=True)
    return shape


def _annotated_parameters(args: ast.arguments) -> dict[str, ast.expr]:
    """Parameters carrying a type annotation.

    `*args` and `**kwargs` are excluded: their annotation describes the element
    type, not the tuple or dict actually bound to the name.
    """
    return {
        argument.arg: argument.annotation
        for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if argument.annotation is not None
    }


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


def resolve_module(
    parse: ModuleParse, tree: ast.Module, index: ImportIndex, graph: ClassGraph
) -> ModuleReferences:
    """Resolve every name used in one module against the whole repository."""
    imports_by_scope: dict[str, dict[str, object]] = {}
    for binding in index.bindings_for(parse.path).values():
        imports_by_scope.setdefault(binding.scope_qualname, {})[binding.local_name] = (
            binding.target
        )

    walker = _ReferenceWalker(
        parse=parse,
        index=index,
        graph=graph,
        imports_by_scope=imports_by_scope,
        defines_by_scope=_defines_by_scope(parse.scope),
    )
    walker.run(tree)
    return ModuleReferences(
        path=parse.path,
        references=tuple(walker.references),
        unresolved_attributes=tuple(walker.unresolved),
    )
