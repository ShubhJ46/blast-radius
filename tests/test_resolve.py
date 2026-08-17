import textwrap

from blastradius.classes import ClassGraph
from blastradius.imports import build_import_index
from blastradius.parse import parse_module, parse_source
from blastradius.resolve import ModuleReferences, declared_attributes, resolve_module

PKG = {
    "pkg/__init__.py": "",
    "pkg/mod.py": (
        "def func():\n"
        "    pass\n"
        "\n"
        "\n"
        "def other():\n"
        "    pass\n"
        "\n"
        "\n"
        "class Widget:\n"
        "    pass\n"
        "\n"
        "\n"
        "def deco(f):\n"
        "    return f\n"
    ),
}


def resolve(files: dict[str, str], path: str) -> ModuleReferences:
    sources = {p: textwrap.dedent(s).lstrip("\n") for p, s in files.items()}
    trees = {p: parse_source(p, s) for p, s in sources.items()}
    parses = {p: parse_module(p, sources[p], tree=trees[p]) for p in sources}
    index, _ = build_import_index(parses)
    graph = ClassGraph.build(parses, index)
    return resolve_module(parses[path], trees[path], index, graph)


def targets(files: dict[str, str], path: str = "m.py") -> list[str]:
    return [str(reference.target) for reference in resolve(files, path).references]


def vias(files: dict[str, str], path: str = "m.py") -> list[str]:
    return [reference.via for reference in resolve(files, path).references]


def self_targets(files: dict[str, str], path: str = "m.py") -> list[str]:
    """Only the `self.x` references, since a `class C(Base)` line also produces one."""
    return [
        str(reference.target)
        for reference in resolve(files, path).references
        if reference.via == "self_attr"
    ]


class TestSameFile:
    def test_call_to_a_module_level_function(self):
        files = {
            "m.py": """
            def helper():
                pass

            def caller():
                helper()
            """
        }
        assert targets(files) == ["m.py::helper"]

    def test_name_used_as_a_value_not_only_called(self):
        files = {
            "m.py": """
            def helper():
                pass

            handlers = [helper]
            """
        }
        assert targets(files) == ["m.py::helper"]

    def test_assignment_target_is_not_a_reference(self):
        files = {"m.py": "def helper():\n    pass\n\nhelper = 1\n"}
        assert targets(files) == []

    def test_reference_records_its_line(self):
        files = {
            "m.py": """
            def helper():
                pass

            def caller():
                return helper()
            """
        }
        (reference,) = resolve(files, "m.py").references
        assert reference.line == 5
        assert reference.path == "m.py"
        assert reference.confidence == "resolved"


class TestAcrossFiles:
    def test_imported_function_call(self):
        files = {**PKG, "m.py": "from pkg.mod import func\n\nfunc()\n"}
        assert targets(files) == ["pkg/mod.py::func"]
        assert vias(files) == ["name"]

    def test_aliased_import_resolves_to_the_real_symbol(self):
        files = {**PKG, "m.py": "from pkg.mod import func as run\n\nrun()\n"}
        assert targets(files) == ["pkg/mod.py::func"]

    def test_import_from_outside_the_repository_is_not_a_reference(self):
        files = {**PKG, "m.py": "from os.path import join\n\njoin('a', 'b')\n"}
        assert targets(files) == []

    def test_passed_as_a_keyword_argument(self):
        files = {**PKG, "m.py": "from pkg.mod import func\n\nregister(handler=func)\n"}
        assert targets(files) == ["pkg/mod.py::func"]

    def test_function_local_import(self):
        files = {
            **PKG,
            "m.py": """
            def caller():
                from pkg.mod import func
                func()
            """,
        }
        assert targets(files) == ["pkg/mod.py::func"]


class TestShadowing:
    def test_local_assignment_shadows_an_import(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller():
                func = 1
                func()
            """,
        }
        assert targets(files) == []

    def test_assignment_below_the_use_still_shadows(self):
        """A name assigned anywhere in a function is local throughout it."""
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller():
                func()
                func = 1
            """,
        }
        assert targets(files) == []

    def test_parameter_shadows_an_import(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(func):
                func()
            """,
        }
        assert targets(files) == []

    def test_for_target_shadows_an_import(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(items):
                for func in items:
                    func()
            """,
        }
        assert targets(files) == []

    def test_except_alias_shadows_an_import(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller():
                try:
                    pass
                except ValueError as func:
                    func()
            """,
        }
        assert targets(files) == []

    def test_shadowing_in_one_function_does_not_affect_another(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def shadowed():
                func = 1
                func()

            def clean():
                func()
            """,
        }
        assert targets(files) == ["pkg/mod.py::func"]

    def test_comprehension_target_is_treated_as_binding_in_the_enclosing_scope(self):
        """Deliberately conservative: drops a reference rather than inventing one."""
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(items):
                return [func for func in items]
            """,
        }
        assert targets(files) == []


class TestScopeChain:
    def test_class_body_is_skipped_for_methods_nested_inside_it(self):
        """A bare name in a method does not see its own class's attributes."""
        files = {
            "m.py": """
            def helper():
                pass

            class Holder:
                helper = 1

                def call(self):
                    return helper()
            """
        }
        assert targets(files) == ["m.py::helper"]

    def test_class_body_sees_its_own_bindings(self):
        files = {
            "m.py": """
            def helper():
                pass

            class Holder:
                helper = 1
                value = helper
            """
        }
        assert targets(files) == []

    def test_nested_function_reaches_module_scope(self):
        files = {
            "m.py": """
            def helper():
                pass

            def outer():
                def inner():
                    helper()
            """
        }
        assert targets(files) == ["m.py::helper"]

    def test_enclosing_function_local_shadows_for_nested_functions(self):
        files = {
            "m.py": """
            def helper():
                pass

            def outer():
                helper = 1

                def inner():
                    helper()
            """
        }
        assert targets(files) == []

    def test_global_declaration_sends_the_lookup_to_module_scope(self):
        files = {
            "m.py": """
            def helper():
                pass

            def caller():
                global helper
                helper = 1
                helper()
            """
        }
        assert targets(files) == ["m.py::helper"]

    def test_nested_definition_is_referenceable_within_its_file(self):
        files = {
            "m.py": """
            def outer():
                def inner():
                    pass
                inner()
            """
        }
        assert targets(files) == ["m.py::outer.<locals>.inner"]


class TestSignatureAndDecoratorPositions:
    """Names outside a function body still reference definitions."""

    def test_decorator(self):
        files = {**PKG, "m.py": "from pkg.mod import deco\n\n@deco\ndef f():\n    pass\n"}
        assert targets(files) == ["pkg/mod.py::deco"]

    def test_default_value(self):
        files = {
            **PKG,
            "m.py": "from pkg.mod import func\n\ndef f(handler=func):\n    pass\n",
        }
        assert targets(files) == ["pkg/mod.py::func"]

    def test_keyword_only_default(self):
        files = {
            **PKG,
            "m.py": "from pkg.mod import func\n\ndef f(*, handler=func):\n    pass\n",
        }
        assert targets(files) == ["pkg/mod.py::func"]

    def test_annotation_and_return_type(self):
        files = {
            **PKG,
            "m.py": "from pkg.mod import Widget\n\ndef f(w: Widget) -> Widget:\n    pass\n",
        }
        assert targets(files) == ["pkg/mod.py::Widget", "pkg/mod.py::Widget"]

    def test_base_class(self):
        files = {**PKG, "m.py": "from pkg.mod import Widget\n\nclass Big(Widget):\n    pass\n"}
        assert targets(files) == ["pkg/mod.py::Widget"]

    def test_defaults_are_evaluated_in_the_enclosing_scope(self):
        """A parameter cannot shadow the default expression that initialises it."""
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def outer():
                def inner(func=func):
                    pass
            """,
        }
        assert targets(files) == ["pkg/mod.py::func"]


class TestModuleAttributes:
    def test_attribute_on_an_imported_module(self):
        files = {**PKG, "m.py": "from pkg import mod\n\nmod.func()\n"}
        assert targets(files) == ["pkg/mod.py::func"]
        assert vias(files) == ["module_attr"]

    def test_dotted_import_walks_to_the_submodule(self):
        files = {**PKG, "m.py": "import pkg.mod\n\npkg.mod.func()\n"}
        assert targets(files) == ["pkg/mod.py::func"]

    def test_aliased_dotted_import(self):
        files = {**PKG, "m.py": "import pkg.mod as m\n\nm.func()\n"}
        assert targets(files) == ["pkg/mod.py::func"]

    def test_attribute_through_a_package_re_export(self):
        files = {
            "pkg/__init__.py": "from .impl import Widget\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "m.py": "import pkg\n\npkg.Widget()\n",
        }
        assert targets(files) == ["pkg/impl.py::Widget"]

    def test_missing_attribute_on_a_known_module_is_unresolved_not_invented(self):
        files = {**PKG, "m.py": "from pkg import mod\n\nmod.absent()\n"}
        result = resolve(files, "m.py")
        assert result.references == ()
        assert [u.attribute for u in result.unresolved_attributes] == ["absent"]

    def test_submodule_on_the_way_to_a_symbol_is_not_reported_unresolved(self):
        files = {**PKG, "m.py": "import pkg.mod\n\npkg.mod.func()\n"}
        assert resolve(files, "m.py").unresolved_attributes == ()


class TestLessCommonBindingForms:
    """Every way a name can become local, since each one can shadow an import."""

    def test_nonlocal_declaration_never_reaches_module_scope(self):
        files = {
            "m.py": """
            def helper():
                pass

            def outer():
                helper = 1

                def inner():
                    nonlocal helper
                    helper()
            """
        }
        assert targets(files) == []

    def test_lambda_parameter(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(items):
                return list(map(lambda func: func(), items))
            """,
        }
        assert targets(files) == []

    def test_match_capture_pattern(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(value):
                match value:
                    case func:
                        func()
            """,
        }
        assert targets(files) == []

    def test_match_mapping_rest_pattern(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func

            def caller(value):
                match value:
                    case {**func}:
                        func()
            """,
        }
        assert targets(files) == []

    def test_star_args_and_kwargs(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import func, other

            def caller(*func, **other):
                func()
                other()
            """,
        }
        assert targets(files) == []

    def test_builtin_name_is_not_a_reference(self):
        files = {"m.py": "def caller(items):\n    return len(items)\n"}
        assert targets(files) == []


class TestMoreSignaturePositions:
    def test_metaclass_keyword(self):
        files = {
            **PKG,
            "m.py": "from pkg.mod import Widget\n\nclass Big(metaclass=Widget):\n    pass\n",
        }
        assert targets(files) == ["pkg/mod.py::Widget"]

    def test_star_args_annotations(self):
        files = {
            **PKG,
            "m.py": """
            from pkg.mod import Widget

            def caller(*args: Widget, **kwargs: Widget):
                pass
            """,
        }
        assert targets(files) == ["pkg/mod.py::Widget", "pkg/mod.py::Widget"]


class TestSelfAttributes:
    def test_method_on_the_same_class(self):
        files = {
            "m.py": """
            class Holder:
                def helper(self):
                    pass

                def call(self):
                    return self.helper()
            """
        }
        assert targets(files) == ["m.py::Holder.helper"]
        assert vias(files) == ["self_attr"]

    def test_method_inherited_from_a_base(self):
        files = {
            "m.py": """
            class Base:
                def helper(self):
                    pass

            class Child(Base):
                def call(self):
                    return self.helper()
            """
        }
        assert self_targets(files) == ["m.py::Base.helper"]
        # The base-class name on the `class Child(Base)` line is a reference too.
        assert targets(files) == ["m.py::Base", "m.py::Base.helper"]

    def test_base_in_another_file(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/mod.py": "class Widget:\n    def render(self):\n        pass\n",
            "m.py": """
            from pkg.mod import Widget

            class Big(Widget):
                def call(self):
                    return self.render()
            """,
        }
        assert self_targets(files) == ["pkg/mod.py::Widget.render"]

    def test_cls_resolves_the_same_way(self):
        files = {
            "m.py": """
            class Holder:
                def helper(cls):
                    pass

                def build(cls):
                    return cls.helper()
            """
        }
        assert targets(files) == ["m.py::Holder.helper"]

    def test_diamond_follows_the_mro(self):
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
                def call(self):
                    return self.run()
            """
        }
        assert self_targets(files) == ["m.py::B.run"]

    def test_bound_method_passed_as_a_value(self):
        files = {
            "m.py": """
            class Holder:
                def helper(self):
                    pass

                def register(self):
                    handler = self.helper
                    return handler
            """
        }
        assert targets(files) == ["m.py::Holder.helper"]

    def test_closure_inside_a_method_still_sees_the_class(self):
        files = {
            "m.py": """
            class Holder:
                def helper(self):
                    pass

                def call(self):
                    def inner():
                        return self.helper()
                    return inner
            """
        }
        assert "m.py::Holder.helper" in targets(files)

    def test_class_declared_inside_a_method_uses_its_own_self(self):
        files = {
            "m.py": """
            class Outer:
                def helper(self):
                    pass

                def make(self):
                    class Inner:
                        def helper(self):
                            pass

                        def call(self):
                            return self.helper()
                    return Inner
            """
        }
        assert self_targets(files) == ["m.py::Outer.make.<locals>.Inner.helper"]

    def test_self_outside_any_class_resolves_to_nothing(self):
        files = {"m.py": "def free(self):\n    return self.helper()\n"}
        result = resolve(files, "m.py")
        assert result.references == ()
        assert [u.base for u in result.unresolved_attributes] == ["self"]

    def test_attribute_that_is_not_a_method_stays_unresolved(self):
        files = {
            "m.py": """
            class Holder:
                def call(self):
                    return self.value
            """
        }
        result = resolve(files, "m.py")
        assert result.references == ()
        assert [u.attribute for u in result.unresolved_attributes] == ["value"]

    def test_unknown_base_class_leaves_its_methods_unreachable(self):
        """The 11% this cannot recover: a base outside the repository."""
        files = {
            "m.py": """
            import abc

            class Big(abc.ABC):
                def call(self):
                    return self.render()
            """
        }
        result = resolve(files, "m.py")
        assert result.references == ()
        # `abc.ABC` is itself an unresolvable attribute on an external module.
        assert [u.attribute for u in result.unresolved_attributes] == ["ABC", "render"]


class TestUnresolvedAttributes:
    def test_self_attribute_is_counted_with_its_base(self):
        files = {
            "m.py": """
            class Holder:
                def call(self):
                    return self.helper()
            """
        }
        result = resolve(files, "m.py")
        assert result.references == ()
        (unresolved,) = result.unresolved_attributes
        assert (unresolved.attribute, unresolved.base) == ("helper", "self")
        assert unresolved.scope_qualname == "Holder.call"
        assert unresolved.in_call

    def test_attribute_read_is_distinguished_from_a_method_call(self):
        """Only a called attribute can reach a definition this tool indexes."""
        files = {
            "m.py": """
            class Holder:
                def call(self):
                    return self.value
            """
        }
        (unresolved,) = resolve(files, "m.py").unresolved_attributes
        assert unresolved.attribute == "value"
        assert not unresolved.in_call

    def test_attribute_on_an_opaque_expression(self):
        files = {
            "m.py": """
            def make():
                pass

            def caller():
                return make().run()
            """
        }
        result = resolve(files, "m.py")
        (unresolved,) = result.unresolved_attributes
        assert (unresolved.attribute, unresolved.base) == ("run", "<expr>")
        # The call to `make` inside the opaque expression is still resolved.
        assert [str(r.target) for r in result.references] == ["m.py::make"]

    def test_attribute_on_an_external_module_is_unresolved(self):
        files = {"m.py": "import os\n\nos.getcwd()\n"}
        result = resolve(files, "m.py")
        assert result.references == ()
        assert [u.base for u in result.unresolved_attributes] == ["os"]

    def test_dotted_base_is_reported_whole(self):
        files = {"m.py": "import os\n\nos.path.join('a', 'b')\n"}
        bases = [u.base for u in resolve(files, "m.py").unresolved_attributes]
        assert "os.path" in bases

    def test_submodule_used_as_a_value_is_neither_a_reference_nor_unresolved(self):
        files = {**PKG, "m.py": "import pkg\n\nhandler = pkg.mod\n"}
        result = resolve(files, "m.py")
        assert result.references == ()
        assert result.unresolved_attributes == ()

    def test_attribute_assignment_is_not_counted(self):
        files = {"m.py": "class Holder:\n    def call(self):\n        self.value = 1\n"}
        assert resolve(files, "m.py").unresolved_attributes == ()


def via_targets(files: dict[str, str], via: str, path: str = "m.py") -> list[str]:
    return [
        str(reference.target)
        for reference in resolve(files, path).references
        if reference.via == via
    ]


class TestConstructors:
    """`C(...)` runs `C.__init__`, but the call site never writes that name.

    Found by the mined evaluation: two of twelve recall misses were a class
    being constructed in another file, where the tool reported the initialiser
    as having no callers at all.
    """

    def test_constructing_a_class_uses_its_initialiser(self):
        files = {
            "lib.py": "class Pool:\n    def __init__(self, size):\n        pass\n",
            "m.py": "from lib import Pool\n\npool = Pool(4)\n",
        }
        assert via_targets(files, "constructor") == ["lib.py::Pool.__init__"]

    def test_the_class_itself_is_still_referenced(self):
        """Losing this edge would break the blast radius of renaming the class."""
        files = {
            "lib.py": "class Pool:\n    def __init__(self, size):\n        pass\n",
            "m.py": "from lib import Pool\n\npool = Pool(4)\n",
        }
        assert via_targets(files, "name") == ["lib.py::Pool"]

    def test_an_inherited_initialiser_resolves_through_the_mro(self):
        files = {
            "lib.py": (
                "class Pool:\n"
                "    def __init__(self, size):\n"
                "        pass\n"
                "\n"
                "\n"
                "class Child(Pool):\n"
                "    pass\n"
            ),
            "m.py": "from lib import Child\n\nchild = Child(4)\n",
        }
        assert via_targets(files, "constructor") == ["lib.py::Pool.__init__"]

    def test_a_class_reached_through_a_module_attribute_counts(self):
        files = {
            **PKG,
            "pkg/pool.py": "class Pool:\n    def __init__(self, size):\n        pass\n",
            "m.py": "import pkg.pool\n\npool = pkg.pool.Pool(4)\n",
        }
        assert via_targets(files, "constructor") == ["pkg/pool.py::Pool.__init__"]

    def test_a_class_without_an_initialiser_records_nothing(self):
        files = {**PKG, "m.py": "from pkg.mod import Widget\n\nw = Widget()\n"}
        assert via_targets(files, "constructor") == []

    def test_calling_a_plain_function_is_not_a_construction(self):
        files = {**PKG, "m.py": "from pkg.mod import func\n\nfunc()\n"}
        assert via_targets(files, "constructor") == []

    def test_cls_is_not_resolved_to_the_class(self):
        """`cls(...)` inside a classmethod needs type inference, not a guess."""
        files = {
            "m.py": (
                "class Pool:\n"
                "    def __init__(self, size):\n"
                "        pass\n"
                "\n"
                "    @classmethod\n"
                "    def make(cls):\n"
                "        return cls(1)\n"
            )
        }
        assert via_targets(files, "constructor") == []

    def test_calling_the_result_of_a_call_is_not_a_construction(self):
        files = {**PKG, "m.py": "from pkg.mod import func\n\nfunc()()\n"}
        assert via_targets(files, "constructor") == []


class TestClassAttributes:
    """`Config.read(...)` names a member of a class the scope chain already proved."""

    def test_a_method_on_an_imported_class_resolves(self):
        files = {
            "lib.py": "class Config:\n    @classmethod\n    def read(cls, path):\n        pass\n",
            "m.py": "from lib import Config\n\nConfig.read('x')\n",
        }
        assert via_targets(files, "class_attr") == ["lib.py::Config.read"]

    def test_the_class_is_referenced_as_well_as_the_member(self):
        files = {
            "lib.py": "class Config:\n    @classmethod\n    def read(cls, path):\n        pass\n",
            "m.py": "from lib import Config\n\nConfig.read('x')\n",
        }
        assert via_targets(files, "name") == ["lib.py::Config"]

    def test_an_inherited_member_resolves_through_the_mro(self):
        files = {
            "lib.py": (
                "class Base:\n"
                "    def helper(self):\n"
                "        pass\n"
                "\n"
                "\n"
                "class Child(Base):\n"
                "    pass\n"
            ),
            "m.py": "from lib import Child\n\nChild.helper(None)\n",
        }
        assert via_targets(files, "class_attr") == ["lib.py::Base.helper"]

    def test_an_unknown_member_stays_unresolved(self):
        files = {**PKG, "m.py": "from pkg.mod import Widget\n\nWidget.missing()\n"}
        result = resolve(files, "m.py")
        assert via_targets(files, "class_attr") == []
        assert [u.attribute for u in result.unresolved_attributes] == ["missing"]

    def test_an_attribute_on_a_variable_is_not_guessed(self):
        """The receiver's type is unknown; inventing an answer here is the failure
        mode the confidence vocabulary exists to prevent."""
        files = {
            "lib.py": "class Config:\n    def read(self):\n        pass\n",
            "m.py": "from lib import Config\n\ndef go(cfg):\n    cfg.read()\n",
        }
        assert via_targets(files, "class_attr") == []


class TestDeclaredTypes:
    """`w.render()` resolves when `w` was *declared* to hold a `Widget`.

    An annotation is a statement the author wrote down, so acting on it is
    reading rather than inferring. Worth 27% of unresolved attribute calls on a
    heavily annotated codebase and 5% on the standard library.
    """

    WIDGET = {
        "lib.py": (
            "class Widget:\n"
            "    def render(self):\n"
            "        pass\n"
            "\n"
            "\n"
            "class Big(Widget):\n"
            "    pass\n"
        )
    }

    def test_a_parameter_annotation_resolves_the_call(self):
        source = "from lib import Widget\n\n\ndef go(w: Widget):\n    w.render()\n"
        assert via_targets({**self.WIDGET, "m.py": source}, "typed_attr") == [
            "lib.py::Widget.render"
        ]

    def test_a_string_forward_reference_resolves(self):
        files = {
            **self.WIDGET,
            "m.py": "from lib import Widget\n\n\ndef go(w: 'Widget'):\n    w.render()\n",
        }
        assert via_targets(files, "typed_attr") == ["lib.py::Widget.render"]

    def test_an_annotated_assignment_resolves(self):
        files = {
            **self.WIDGET,
            "m.py": (
                "from lib import Widget\n\n\ndef go():\n"
                "    x: Widget = build()\n    x.render()\n"
            ),
        }
        assert via_targets(files, "typed_attr") == ["lib.py::Widget.render"]

    def test_an_annotation_through_a_module_attribute_resolves(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/lib.py": self.WIDGET["lib.py"],
            "m.py": "import pkg.lib\n\n\ndef go(w: pkg.lib.Widget):\n    w.render()\n",
        }
        assert via_targets(files, "typed_attr") == ["pkg/lib.py::Widget.render"]

    def test_an_inherited_method_resolves_through_the_mro(self):
        source = "from lib import Big\n\n\ndef go(b: Big):\n    b.render()\n"
        assert via_targets({**self.WIDGET, "m.py": source}, "typed_attr") == [
            "lib.py::Widget.render"
        ]

    def test_a_container_annotation_resolves_to_nothing(self):
        """`list[Widget]` is a list. Resolving to the element type would be a guess."""
        files = {
            **self.WIDGET,
            "m.py": "from lib import Widget\n\n\ndef go(w: list[Widget]):\n    w.render()\n",
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_union_annotation_resolves_to_nothing(self):
        files = {
            **self.WIDGET,
            "m.py": "from lib import Widget\n\n\ndef go(w: Widget | None):\n    w.render()\n",
        }
        assert via_targets(files, "typed_attr") == []

    def test_an_unannotated_parameter_is_left_unresolved(self):
        files = {**self.WIDGET, "m.py": "def go(w):\n    w.render()\n"}
        result = resolve(files, "m.py")
        assert via_targets(files, "typed_attr") == []
        assert [u.attribute for u in result.unresolved_attributes] == ["render"]

    def test_an_annotation_naming_something_outside_the_repo_is_left_alone(self):
        files = {"m.py": "import os\n\n\ndef go(p: os.PathLike):\n    p.render()\n"}
        assert via_targets(files, "typed_attr") == []

    def test_a_method_the_class_does_not_have_stays_unresolved(self):
        source = "from lib import Widget\n\n\ndef go(w: Widget):\n    w.missing()\n"
        files = {**self.WIDGET, "m.py": source}
        result = resolve(files, "m.py")
        assert via_targets(files, "typed_attr") == []
        assert [u.attribute for u in result.unresolved_attributes] == ["missing"]

    def test_an_inner_binding_without_a_declaration_shadows_an_outer_one(self):
        """The inner `w` is a different variable; carrying the outer type in
        would be resolving against a declaration that does not apply."""
        files = {
            **self.WIDGET,
            "m.py": (
                "from lib import Widget\n"
                "\n"
                "\n"
                "def outer(w: Widget):\n"
                "    def inner():\n"
                "        w = build()\n"
                "        w.render()\n"
                "    return inner\n"
            ),
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_star_parameter_annotation_is_not_the_type_of_the_name(self):
        """`*args: Widget` binds a tuple, not a Widget."""
        files = {
            **self.WIDGET,
            "m.py": "from lib import Widget\n\n\ndef go(*args: Widget):\n    args.render()\n",
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_forward_reference_to_something_unknown_resolves_to_nothing(self):
        files = {**self.WIDGET, "m.py": "def go(w: 'Nowhere'):\n    w.render()\n"}
        assert via_targets(files, "typed_attr") == []

    def test_a_class_body_declaration_is_not_visible_to_its_methods(self):
        """A bare name in a method does not see the class's own attributes, so
        neither should the declaration attached to one."""
        files = {
            **self.WIDGET,
            "m.py": (
                "from lib import Widget\n"
                "\n"
                "\n"
                "class Holder:\n"
                "    w: Widget\n"
                "\n"
                "    def go(self):\n"
                "        w.render()\n"
            ),
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_name_bound_nowhere_at_all_resolves_to_nothing(self):
        files = {**self.WIDGET, "m.py": "mystery.render()\n"}
        assert via_targets(files, "typed_attr") == []


class TestDeclaredAttributes:
    """`self.config: Config` written once, read from every method.

    The declaration lives in one method -- almost always `__init__` -- and is
    used from all the others, so it is kept on the class rather than on the
    scope that wrote it.
    """

    CONFIG = {
        "lib.py": (
            "class Config:\n"
            "    def read(self, path):\n"
            "        pass\n"
            "\n"
            "\n"
            "class Sub(Config):\n"
            "    pass\n"
        )
    }

    def test_a_class_body_declaration_resolves(self):
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    config: Config\n"
                "\n"
                "    def go(self):\n"
                "        self.config.read('x')\n"
            ),
        }
        assert via_targets(files, "typed_attr") == ["lib.py::Config.read"]

    def test_a_declaration_in_init_is_visible_to_other_methods(self):
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    def __init__(self):\n"
                "        self.config: Config = build()\n"
                "\n"
                "    def go(self):\n"
                "        self.config.read('x')\n"
            ),
        }
        assert via_targets(files, "typed_attr") == ["lib.py::Config.read"]

    def test_an_inherited_method_resolves_through_the_mro(self):
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Sub\n"
                "\n"
                "\n"
                "class App:\n"
                "    config: Sub\n"
                "\n"
                "    def go(self):\n"
                "        self.config.read('x')\n"
            ),
        }
        assert via_targets(files, "typed_attr") == ["lib.py::Config.read"]

    def test_an_unannotated_assignment_is_still_a_guess(self):
        """`self.config = build()` says nothing about the type, and tracking it
        would be inference rather than reading a declaration."""
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    def __init__(self):\n"
                "        self.config = build()\n"
                "\n"
                "    def go(self):\n"
                "        self.config.read('x')\n"
            ),
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_nested_class_does_not_inherit_the_outer_self(self):
        """The inner `self` is the inner class's, and carrying the outer
        class's attribute types into it would resolve the wrong receiver."""
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    config: Config\n"
                "\n"
                "    def outer(self):\n"
                "        class Inner:\n"
                "            def go(self):\n"
                "                self.config.read('x')\n"
                "        return Inner\n"
            ),
        }
        assert via_targets(files, "typed_attr") == []

    def test_an_undeclared_attribute_stays_unresolved(self):
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    config: Config\n"
                "\n"
                "    def go(self):\n"
                "        self.other.read('x')\n"
            ),
        }
        result = resolve(files, "m.py")
        assert via_targets(files, "typed_attr") == []
        # Both halves are counted: `read`, because the receiver's type is
        # unknown, and `other` itself, which no declaration names either.
        assert sorted(u.attribute for u in result.unresolved_attributes) == ["other", "read"]

    def test_a_container_annotation_resolves_to_nothing(self):
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    configs: list[Config]\n"
                "\n"
                "    def go(self):\n"
                "        self.configs.read('x')\n"
            ),
        }
        assert via_targets(files, "typed_attr") == []

    def test_a_declaration_outside_any_class_resolves_to_nothing(self):
        """`self` in a plain function is just a parameter name."""
        files = {
            **self.CONFIG,
            "m.py": "def go(self):\n    self.config.read('x')\n",
        }
        assert via_targets(files, "typed_attr") == []

    def test_the_receiver_shifts_the_positional_index(self):
        """`self.config.read(p)` fills `read(self, path)` two deep."""
        files = {
            **self.CONFIG,
            "m.py": (
                "from lib import Config\n"
                "\n"
                "\n"
                "class App:\n"
                "    config: Config\n"
                "\n"
                "    def go(self):\n"
                "        self.config.read('x')\n"
            ),
        }
        call = next(
            r.call for r in resolve(files, "m.py").references if r.via == "typed_attr"
        )
        assert call.positional == 2
        assert call.supplies("path", 1)


class TestDeclaredAttributeCollection:
    def test_collects_both_declaration_forms_in_source_order(self):
        source = (
            "class C:\n"
            "    first: A\n"
            "\n"
            "    def __init__(self):\n"
            "        self.second: B = None\n"
        )
        tree = parse_source("m.py", source)
        found = declared_attributes(tree.body[0])
        assert list(found) == ["first", "second"]

    def test_the_first_declaration_of_a_name_wins(self):
        source = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.thing: A = None\n"
            "\n"
            "    def later(self):\n"
            "        self.thing: B = None\n"
        )
        found = declared_attributes(parse_source("m.py", source).body[0])
        assert found["thing"].id == "A"

    def test_an_unannotated_assignment_is_not_a_declaration(self):
        found = declared_attributes(
            parse_source("m.py", "class C:\n    def __init__(self):\n        self.x = 1\n").body[0]
        )
        assert found == {}


class TestBoundTypes:
    """`w.render()` resolves when `w` was *bound* once, to something naming a class.

    Weaker evidence than an annotation and reported under its own `via` for that
    reason. The guard is what makes it safe: a name with any binding that
    disagrees resolves to nothing, so a reassigned variable is never guessed at.
    """

    WIDGET = {
        "lib.py": (
            "class Widget:\n"
            "    def render(self):\n"
            "        pass\n"
            "\n"
            "\n"
            "class Gadget:\n"
            "    def render(self):\n"
            "        pass\n"
        )
    }

    def bound(self, body: str, imports: str = "from lib import Widget, Gadget") -> list[str]:
        source = f"{imports}\n\n\n{textwrap.dedent(body).strip()}\n"
        return via_targets({**self.WIDGET, "m.py": source}, "bound_attr")

    def test_a_constructor_binding_resolves_the_call(self):
        assert self.bound("def go():\n    w = Widget()\n    w.render()\n") == [
            "lib.py::Widget.render"
        ]

    def test_the_same_class_bound_twice_still_agrees(self):
        assert self.bound(
            "def go(flag):\n"
            "    if flag:\n"
            "        w = Widget()\n"
            "    else:\n"
            "        w = Widget()\n"
            "    w.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_two_different_classes_resolve_nothing(self):
        assert self.bound(
            "def go():\n    w = Widget()\n    w = Gadget()\n    w.render()\n"
        ) == []

    def test_a_binding_that_is_not_a_construction_disagrees(self):
        assert self.bound(
            "def go(source):\n    w = Widget()\n    w = source\n    w.render()\n"
        ) == []

    def test_a_parameter_is_never_typed_by_a_later_binding(self):
        assert self.bound("def go(w):\n    w = Widget()\n    w.render()\n") == []

    def test_a_loop_target_resolves_nothing(self):
        assert self.bound("def go(items):\n    for w in items:\n        w.render()\n") == []

    def test_a_with_binding_resolves_nothing(self):
        assert self.bound(
            "def go(ctx):\n    w = Widget()\n    with ctx as w:\n        w.render()\n"
        ) == []

    def test_unpacking_resolves_nothing(self):
        assert self.bound(
            "def go():\n    w, other = Widget(), Widget()\n    w.render()\n"
        ) == []

    def test_a_deleted_name_resolves_nothing(self):
        assert self.bound("def go():\n    w = Widget()\n    del w\n    w.render()\n") == []

    def test_an_except_binding_resolves_nothing(self):
        assert self.bound(
            "def go():\n"
            "    w = Widget()\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as w:\n"
            "        w.render()\n"
        ) == []

    def test_a_call_that_is_not_a_class_resolves_nothing(self):
        assert self.bound("def go():\n    w = helper()\n    w.render()\n") == []

    def test_a_declared_type_wins_over_a_binding(self):
        # The annotation is the stronger evidence, so the call is `typed_attr`
        # and the binding never gets a say.
        source = (
            "from lib import Widget, Gadget\n\n\n"
            "def go():\n    w: Gadget = Widget()\n    w.render()\n"
        )
        files = {**self.WIDGET, "m.py": source}
        assert via_targets(files, "typed_attr") == ["lib.py::Gadget.render"]
        assert via_targets(files, "bound_attr") == []

    def test_a_nested_function_does_not_disagree_with_the_outer_name(self):
        assert self.bound(
            "def go():\n"
            "    w = Widget()\n"
            "\n"
            "    def inner():\n"
            "        w = Gadget()\n"
            "        return w\n"
            "\n"
            "    w.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_an_attribute_built_in_init_resolves_from_another_method(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self):\n"
            "        self.thing = Widget()\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_an_attribute_assigned_from_an_annotated_parameter_resolves(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self, given: Widget):\n"
            "        self.thing = given\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_an_attribute_assigned_from_a_bare_parameter_resolves_nothing(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self, given):\n"
            "        self.thing = given\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == []

    def test_an_attribute_assigned_twice_in_different_methods_disagrees(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self):\n"
            "        self.thing = Widget()\n"
            "\n"
            "    def swap(self, values):\n"
            "        self.thing = values[0]\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == []

    def test_a_class_body_assignment_types_the_attribute(self):
        assert self.bound(
            "class Holder:\n"
            "    thing = Widget()\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_a_nested_class_does_not_inherit_the_outer_binding(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self):\n"
            "        self.thing = Widget()\n"
            "\n"
            "    class Inner:\n"
            "        def use(self):\n"
            "            self.thing.render()\n"
        ) == []

    def test_a_bound_receiver_fills_the_first_parameter(self):
        # `w.render(mode)` reaches `render(self, mode)` two deep. Getting this
        # wrong silently reports the wrong callers for `--argument`.
        files = {
            "lib.py": "class Widget:\n    def render(self, mode):\n        pass\n",
            "m.py": (
                "from lib import Widget\n\n\ndef go():\n"
                "    w = Widget()\n    w.render('fast')\n"
            ),
        }
        call = next(
            reference
            for reference in resolve(files, "m.py").references
            if reference.via == "bound_attr"
        )
        assert call.call is not None
        assert call.call.positional == 2

    def test_a_local_in_another_method_does_not_type_an_attribute(self):
        # `thing` is a local in `other`, and `self.thing` is never assigned
        # anywhere in the class. Reading the two as one invents a caller out of
        # a coincidence of names.
        assert self.bound(
            "class Holder:\n"
            "    def other(self):\n"
            "        thing = Widget()\n"
            "        return thing\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == []

    def test_a_local_in_a_method_does_not_disagree_with_a_class_attribute(self):
        # The class body assigns `thing`; a method's unrelated local of the same
        # name must neither type it nor poison it.
        assert self.bound(
            "class Holder:\n"
            "    thing = Widget()\n"
            "\n"
            "    def other(self):\n"
            "        thing = Gadget()\n"
            "        return thing\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == ["lib.py::Widget.render"]

    def test_a_closure_can_still_assign_the_attribute(self):
        assert self.bound(
            "class Holder:\n"
            "    def __init__(self):\n"
            "        def build():\n"
            "            self.thing = Widget()\n"
            "\n"
            "        build()\n"
            "\n"
            "    def use(self):\n"
            "        self.thing.render()\n"
        ) == ["lib.py::Widget.render"]
