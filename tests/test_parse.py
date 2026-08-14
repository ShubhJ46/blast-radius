import textwrap

import pytest

from blastradius.errors import ParseError
from blastradius.parse import parse_module


def parse(source: str, path: str = "pkg/module.py"):
    return parse_module(path, textwrap.dedent(source).lstrip("\n"))


def qualnames(result) -> list[str]:
    return [definition.symbol.qualname for definition in result.definitions]


def find(result, qualname: str):
    return result.by_qualname()[qualname]


def test_module_level_function():
    result = parse(
        """
        def search(query):
            return query
        """
    )
    definition = find(result, "search")
    assert definition.kind == "function"
    assert str(definition.symbol) == "pkg/module.py::search"
    assert (definition.start_line, definition.end_line) == (1, 2)
    assert not definition.is_nested


def test_method_qualname_has_no_locals_marker():
    result = parse(
        """
        class Index:
            def search(self, query):
                return query
        """
    )
    assert qualnames(result) == ["Index", "Index.search"]
    method = find(result, "Index.search")
    assert method.kind == "method"
    assert not method.is_nested


def test_nested_function_is_marked_nested():
    result = parse(
        """
        def outer():
            def inner():
                pass
        """
    )
    assert qualnames(result) == ["outer", "outer.<locals>.inner"]
    assert find(result, "outer.<locals>.inner").is_nested


def test_class_nested_in_function_carries_locals_marker():
    result = parse(
        """
        def factory():
            class Built:
                def method(self):
                    pass
        """
    )
    assert "factory.<locals>.Built.method" in qualnames(result)
    assert find(result, "factory.<locals>.Built.method").kind == "method"


def test_async_function_is_recorded_like_a_function():
    result = parse(
        """
        class Client:
            async def fetch(self, url):
                pass
        """
    )
    definition = find(result, "Client.fetch")
    assert definition.kind == "method"
    assert definition.signature is not None
    assert definition.signature.positional_names() == ("self", "url")


def test_definitions_inside_conditionals_are_found():
    """A def guarded by `if` or wrapped in `try` still defines a symbol."""
    result = parse(
        """
        import sys

        if sys.version_info >= (3, 11):
            def parse():
                pass
        else:
            def parse_legacy():
                pass

        try:
            class Fast:
                pass
        except ImportError:
            pass
        """
    )
    assert set(qualnames(result)) == {"parse", "parse_legacy", "Fast"}


def test_decorators_are_reduced_to_dotted_names():
    result = parse(
        """
        import functools

        @functools.lru_cache(maxsize=1)
        @staticmethod
        def cached():
            pass
        """
    )
    assert find(result, "cached").decorators == ("functools.lru_cache", "staticmethod")


def test_bases_unwrap_subscripts_and_attributes():
    result = parse(
        """
        import abc
        import typing

        class Store(abc.ABC, typing.Generic[int]):
            pass
        """
    )
    assert find(result, "Store").bases == ("abc.ABC", "typing.Generic")


def test_unnameable_bases_are_dropped():
    """`class D(*mixins)` has a base this tool cannot name, and says so by omission.

    Silently inventing a name would put a wrong edge in the class graph, which
    is worse for override detection than a missing one.
    """
    result = parse(
        """
        class Dynamic(*mixins):
            pass
        """
    )
    assert find(result, "Dynamic").bases == ()


def test_definition_name_is_the_last_qualname_segment():
    result = parse(
        """
        class Index:
            def search(self):
                pass
        """
    )
    assert find(result, "Index.search").name == "search"
    assert find(result, "Index").name == "Index"


def test_class_has_no_signature():
    result = parse(
        """
        class Empty:
            pass
        """
    )
    assert find(result, "Empty").signature is None


class TestSignature:
    def test_records_declaration_order_and_defaults(self):
        result = parse(
            """
            def handler(a, b=1, *args, c, d=2, **kwargs):
                pass
            """
        )
        signature = find(result, "handler").signature
        assert [(p.name, p.kind, p.has_default) for p in signature.parameters] == [
            ("a", "positional_or_keyword", False),
            ("b", "positional_or_keyword", True),
            ("args", "var_positional", False),
            ("c", "keyword_only", False),
            ("d", "keyword_only", True),
            ("kwargs", "var_keyword", False),
        ]

    def test_positional_only_parameters(self):
        result = parse(
            """
            def divide(a, b, /, rounding=None):
                pass
            """
        )
        signature = find(result, "divide").signature
        kinds = [p.kind for p in signature.parameters]
        assert kinds == ["positional_only", "positional_only", "positional_or_keyword"]

    def test_required_names_excludes_defaults_and_varargs(self):
        result = parse(
            """
            def run(required, optional=None, *args, **kwargs):
                pass
            """
        )
        assert find(result, "run").signature.required_names() == frozenset({"required"})

    def test_reordering_changes_the_signature(self):
        """The eval classifier depends on order being part of identity."""
        one = find(parse("def f(a, b): pass"), "f").signature
        two = find(parse("def f(b, a): pass"), "f").signature
        assert one != two


class TestScopeTree:
    def test_module_scope_binds_top_level_names(self):
        result = parse(
            """
            def search():
                pass

            class Index:
                pass
            """
        )
        assert set(result.scope.defines) == {"search", "Index"}
        assert result.scope.kind == "module"
        assert result.scope.qualname == ""

    def test_class_scope_binds_its_methods(self):
        result = parse(
            """
            class Index:
                def search(self):
                    pass
            """
        )
        (class_scope,) = result.scope.children
        assert class_scope.kind == "class"
        assert class_scope.qualname == "Index"
        assert set(class_scope.defines) == {"search"}

    def test_nested_scopes_are_not_leaked_to_the_parent(self):
        result = parse(
            """
            def outer():
                def inner():
                    pass
            """
        )
        assert set(result.scope.defines) == {"outer"}
        (function_scope,) = result.scope.children
        assert set(function_scope.defines) == {"inner"}


def test_syntax_error_raises_parse_error_naming_the_file():
    with pytest.raises(ParseError, match="pkg/module.py"):
        parse("def broken(:\n    pass\n")


def test_parses_this_repository_without_error():
    """A smoke test over real source, so the parser is exercised on more than fixtures."""
    from pathlib import Path

    from blastradius.parse import parse_file

    root = Path(__file__).resolve().parent.parent
    modules = sorted((root / "blastradius").glob("*.py"))
    assert modules, "expected package sources to be present"
    for module in modules:
        assert parse_file(module, root).path.startswith("blastradius/")
