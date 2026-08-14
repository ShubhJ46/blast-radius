import textwrap

from blastradius.classes import ClassGraph
from blastradius.imports import build_import_index
from blastradius.model import SymbolId
from blastradius.parse import parse_module

PKG = {
    "pkg/__init__.py": "",
    "pkg/mod.py": (
        "class Widget:\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "def func():\n"
        "    pass\n"
    ),
}


def graph(files: dict[str, str]) -> ClassGraph:
    parses = {
        path: parse_module(path, textwrap.dedent(source).lstrip("\n"))
        for path, source in files.items()
    }
    index, _ = build_import_index(parses)
    return ClassGraph.build(parses, index)


def sym(path: str, qualname: str) -> SymbolId:
    return SymbolId(path, qualname)


class TestBaseResolution:
    def test_base_defined_in_the_same_file(self):
        files = {"m.py": "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n"}
        node = graph(files).node(sym("m.py", "Child"))
        assert node.bases == (sym("m.py", "Base"),)
        assert node.unresolved_bases == ()

    def test_imported_base(self):
        files = {**PKG, "m.py": "from pkg.mod import Widget\n\nclass Big(Widget):\n    pass\n"}
        node = graph(files).node(sym("m.py", "Big"))
        assert node.bases == (sym("pkg/mod.py", "Widget"),)

    def test_base_reached_through_an_imported_module(self):
        files = {**PKG, "m.py": "from pkg import mod\n\nclass Big(mod.Widget):\n    pass\n"}
        assert graph(files).node(sym("m.py", "Big")).bases == (sym("pkg/mod.py", "Widget"),)

    def test_base_reached_through_a_dotted_module_path(self):
        files = {**PKG, "m.py": "import pkg.mod\n\nclass Big(pkg.mod.Widget):\n    pass\n"}
        assert graph(files).node(sym("m.py", "Big")).bases == (sym("pkg/mod.py", "Widget"),)

    def test_nested_class_as_a_base(self):
        files = {
            "m.py": """
            class Outer:
                class Inner:
                    pass

            class Sub(Outer.Inner):
                pass
            """
        }
        assert graph(files).node(sym("m.py", "Sub")).bases == (sym("m.py", "Outer.Inner"),)

    def test_base_outside_the_repository_is_recorded_as_unresolved(self):
        files = {"m.py": "import abc\n\nclass Big(abc.ABC):\n    pass\n"}
        node = graph(files).node(sym("m.py", "Big"))
        assert node.bases == ()
        assert node.unresolved_bases == ("abc.ABC",)
        assert node.has_unresolved_bases

    def test_base_naming_a_function_is_not_treated_as_a_class(self):
        files = {**PKG, "m.py": "from pkg.mod import func\n\nclass Big(func):\n    pass\n"}
        node = graph(files).node(sym("m.py", "Big"))
        assert node.bases == ()
        assert node.unresolved_bases == ("func",)

    def test_unknown_name_as_a_base(self):
        files = {"m.py": "class Big(Missing):\n    pass\n"}
        assert graph(files).node(sym("m.py", "Big")).unresolved_bases == ("Missing",)

    def test_missing_attribute_on_a_known_module(self):
        files = {**PKG, "m.py": "from pkg import mod\n\nclass Big(mod.Absent):\n    pass\n"}
        assert graph(files).node(sym("m.py", "Big")).unresolved_bases == ("mod.Absent",)

    def test_module_used_as_a_base(self):
        """`class Big(mod)` where `mod.Widget` was meant: a module is not a class."""
        files = {**PKG, "m.py": "from pkg import mod\n\nclass Big(mod):\n    pass\n"}
        node = graph(files).node(sym("m.py", "Big"))
        assert node.bases == ()
        assert node.unresolved_bases == ("mod",)

    def test_multiple_bases_keep_declaration_order(self):
        files = {
            "m.py": """
            class First:
                pass

            class Second:
                pass

            class Both(First, Second):
                pass
            """
        }
        assert graph(files).node(sym("m.py", "Both")).bases == (
            sym("m.py", "First"),
            sym("m.py", "Second"),
        )

    def test_class_nested_in_a_function_resolves_its_base_at_module_scope(self):
        files = {
            "m.py": """
            class Base:
                pass

            def factory():
                class Built(Base):
                    pass
            """
        }
        node = graph(files).node(sym("m.py", "factory.<locals>.Built"))
        assert node.bases == (sym("m.py", "Base"),)


class TestHierarchy:
    CHAIN = {
        "m.py": """
        class A:
            pass

        class B(A):
            pass

        class C(B):
            pass
        """
    }

    def test_direct_subclasses(self):
        assert graph(self.CHAIN).direct_subclasses(sym("m.py", "A")) == (sym("m.py", "B"),)

    def test_descendants_are_transitive(self):
        assert graph(self.CHAIN).descendants(sym("m.py", "A")) == (
            sym("m.py", "B"),
            sym("m.py", "C"),
        )

    def test_descendants_reached_by_two_paths_are_reported_once(self):
        files = {
            "m.py": """
            class A:
                pass

            class B(A):
                pass

            class C(A):
                pass

            class D(B, C):
                pass
            """
        }
        assert graph(files).descendants(sym("m.py", "A")) == (
            sym("m.py", "B"),
            sym("m.py", "C"),
            sym("m.py", "D"),
        )

    def test_leaf_has_no_descendants(self):
        assert graph(self.CHAIN).descendants(sym("m.py", "C")) == ()

    def test_unknown_symbol_is_not_a_node(self):
        assert graph(self.CHAIN).node(sym("m.py", "Absent")) is None


class TestMro:
    def test_linear_chain(self):
        files = {
            "m.py": "class A:\n    pass\n\n\nclass B(A):\n    pass\n\n\nclass C(B):\n    pass\n"
        }
        assert graph(files).mro(sym("m.py", "C")) == (
            sym("m.py", "C"),
            sym("m.py", "B"),
            sym("m.py", "A"),
        )

    def test_diamond_follows_c3_not_depth_first(self):
        files = {
            "m.py": """
            class A:
                pass

            class B(A):
                pass

            class C(A):
                pass

            class D(B, C):
                pass
            """
        }
        assert graph(files).mro(sym("m.py", "D")) == (
            sym("m.py", "D"),
            sym("m.py", "B"),
            sym("m.py", "C"),
            sym("m.py", "A"),
        )

    def test_class_with_no_indexed_bases_linearises_to_itself(self):
        files = {"m.py": "import abc\n\nclass Big(abc.ABC):\n    pass\n"}
        assert graph(files).mro(sym("m.py", "Big")) == (sym("m.py", "Big"),)

    def test_inconsistent_hierarchy_falls_back_instead_of_raising(self):
        """Real Python raises TypeError here; one bad hierarchy must not break every query."""
        files = {
            "m.py": """
            class A:
                pass

            class B:
                pass

            class X(A, B):
                pass

            class Y(B, A):
                pass

            class Z(X, Y):
                pass
            """
        }
        result = graph(files).mro(sym("m.py", "Z"))
        assert result[0] == sym("m.py", "Z")
        assert set(result) == {sym("m.py", name) for name in ("Z", "X", "Y", "A", "B")}

    def test_result_is_cached_between_calls(self):
        built = graph({"m.py": "class A:\n    pass\n"})
        first = built.mro(sym("m.py", "A"))
        assert built.mro(sym("m.py", "A")) is first


class TestMethodLookup:
    FILES = {
        "m.py": """
        class Base:
            def render(self):
                pass

            def only_on_base(self):
                pass

        class Child(Base):
            def render(self):
                pass
        """
    }

    def test_method_declared_on_the_class_itself(self):
        assert graph(self.FILES).lookup_method(sym("m.py", "Child"), "render") == sym(
            "m.py", "Child.render"
        )

    def test_method_inherited_from_a_base(self):
        assert graph(self.FILES).lookup_method(sym("m.py", "Child"), "only_on_base") == sym(
            "m.py", "Base.only_on_base"
        )

    def test_absent_method(self):
        assert graph(self.FILES).lookup_method(sym("m.py", "Child"), "absent") is None

    def test_lookup_on_an_unknown_class(self):
        assert graph(self.FILES).lookup_method(sym("m.py", "Absent"), "render") is None

    def test_diamond_resolution_follows_the_mro(self):
        files = {
            "m.py": """
            class A:
                def run(self):
                    pass

            class B(A):
                def run(self):
                    pass

            class C(A):
                def run(self):
                    pass

            class D(B, C):
                pass
            """
        }
        assert graph(files).lookup_method(sym("m.py", "D"), "run") == sym("m.py", "B.run")


class TestOverrides:
    FILES = {
        "m.py": """
        class Base:
            def render(self):
                pass

            def alone(self):
                pass

        class Middle(Base):
            def render(self):
                pass

        class Leaf(Middle):
            def render(self):
                pass
        """
    }

    def test_overrides_span_every_descendant_not_only_direct_ones(self):
        assert graph(self.FILES).overrides_of(sym("m.py", "Base.render")) == (
            sym("m.py", "Leaf.render"),
            sym("m.py", "Middle.render"),
        )

    def test_method_nobody_overrides(self):
        assert graph(self.FILES).overrides_of(sym("m.py", "Base.alone")) == ()

    def test_overrides_of_something_that_is_not_a_method(self):
        assert graph(self.FILES).overrides_of(sym("m.py", "Base")) == ()

    def test_overridden_returns_the_nearest_ancestor_definition(self):
        assert graph(self.FILES).overridden(sym("m.py", "Leaf.render")) == sym(
            "m.py", "Middle.render"
        )

    def test_root_definition_overrides_nothing(self):
        assert graph(self.FILES).overridden(sym("m.py", "Base.render")) is None

    def test_overridden_of_something_that_is_not_a_method(self):
        assert graph(self.FILES).overridden(sym("m.py", "Base")) is None

    def test_owner_of_a_method(self):
        assert graph(self.FILES).owner_of(sym("m.py", "Leaf.render")) == sym("m.py", "Leaf")

    def test_overrides_across_files(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import Widget

            class Big(Widget):
                def render(self):
                    pass
            """,
        }
        assert graph(files).overrides_of(sym("pkg/mod.py", "Widget.render")) == (
            sym("m.py", "Big.render"),
        )
