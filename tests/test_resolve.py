import textwrap

from blastradius.imports import build_import_index
from blastradius.parse import parse_module, parse_source
from blastradius.resolve import ModuleReferences, resolve_module

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
    return resolve_module(parses[path], trees[path], index)


def targets(files: dict[str, str], path: str = "m.py") -> list[str]:
    return [str(reference.target) for reference in resolve(files, path).references]


def vias(files: dict[str, str], path: str = "m.py") -> list[str]:
    return [reference.via for reference in resolve(files, path).references]


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
