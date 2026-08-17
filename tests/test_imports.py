import textwrap
from dataclasses import replace

import pytest

from blastradius.imports import (
    Binding,
    ExternalRef,
    ImportIndex,
    ModuleRef,
    ModuleTable,
    build_import_index,
    local_name_of,
)
from blastradius.model import RawImport, SymbolId
from blastradius.parse import parse_module


def build(files: dict[str, str]) -> tuple[ImportIndex, ModuleTable]:
    parses = {
        path: parse_module(path, textwrap.dedent(source).lstrip("\n"))
        for path, source in files.items()
    }
    return build_import_index(parses)


def target(files: dict[str, str], path: str, local_name: str):
    index, _ = build(files)
    return index.bindings_for(path)[local_name].target


class TestModuleTable:
    def test_flat_package_layout(self):
        table = ModuleTable.build(["pkg/__init__.py", "pkg/mod.py"])
        assert table.by_name == {"pkg": "pkg/__init__.py", "pkg.mod": "pkg/mod.py"}

    def test_src_layout_finds_the_source_root_without_configuration(self):
        table = ModuleTable.build(["src/flask/__init__.py", "src/flask/app.py"])
        assert table.path_for_module("flask.app") == "src/flask/app.py"
        assert table.path_for_module("src.flask.app") is None

    def test_nested_packages(self):
        table = ModuleTable.build(
            ["a/__init__.py", "a/b/__init__.py", "a/b/c.py"]
        )
        assert table.module_for_path("a/b/c.py") == "a.b.c"

    def test_top_level_script_is_its_own_module(self):
        table = ModuleTable.build(["setup.py"])
        assert table.by_name == {"setup": "setup.py"}

    def test_directory_without_init_is_a_source_root_not_a_package(self):
        table = ModuleTable.build(["scripts/run.py"])
        assert table.module_for_path("scripts/run.py") == "run"

    def test_colliding_names_are_recorded_and_resolved_deterministically(self):
        table = ModuleTable.build(["tools/run.py", "scripts/run.py"])
        assert table.path_for_module("run") == "scripts/run.py"  # first in sorted order
        assert table.conflicts == (("scripts/run.py", "tools/run.py"),)

    def test_root_init_names_nothing_and_is_skipped(self):
        table = ModuleTable.build(["__init__.py", "mod.py"])
        assert table.by_name == {"mod": "mod.py"}

    def test_an_unreadable_init_still_makes_its_directory_a_package(self):
        # scrapy's Python 2 `scrapy/__init__.py` did not parse, so it was
        # absent from the parses the table was built from -- which renamed
        # every module under it and made the whole repository's imports
        # resolve as external.
        table = ModuleTable.build(["pkg/mod.py"], ["pkg/__init__.py"])
        assert table.module_for_path("pkg/mod.py") == "pkg.mod"

    def test_an_unreadable_module_is_not_given_a_name_of_its_own(self):
        # It counts towards which directories are packages and nothing more:
        # there is no parse behind it to point a reference at.
        table = ModuleTable.build(["pkg/mod.py"], ["pkg/__init__.py"])
        assert table.path_for_module("pkg") is None

    def test_a_package_is_still_recognised_from_the_parses_alone(self):
        table = ModuleTable.build(["pkg/__init__.py", "pkg/mod.py"], [])
        assert table.module_for_path("pkg/mod.py") == "pkg.mod"


class TestPackageOf:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("pkg/mod.py", "pkg"),
            ("pkg/__init__.py", "pkg"),  # a package's __init__ *is* the package
            ("setup.py", ""),
        ],
    )
    def test_package_of(self, path, expected):
        table = ModuleTable.build(["pkg/__init__.py", "pkg/mod.py", "setup.py"])
        assert table.package_of(path) == expected


class TestResolveRelative:
    def setup_method(self):
        self.table = ModuleTable.build(
            [
                "pkg/__init__.py",
                "pkg/mod.py",
                "pkg/other.py",
                "pkg/sub/__init__.py",
                "pkg/sub/deep.py",
            ]
        )

    def test_single_dot_without_module_is_the_containing_package(self):
        assert self.table.resolve_relative("pkg/mod.py", 1, None) == "pkg"

    def test_single_dot_with_module(self):
        assert self.table.resolve_relative("pkg/mod.py", 1, "other") == "pkg.other"

    def test_double_dot_climbs_one_package(self):
        assert self.table.resolve_relative("pkg/sub/deep.py", 2, "mod") == "pkg.mod"

    def test_dot_inside_an_init_does_not_climb(self):
        assert self.table.resolve_relative("pkg/sub/__init__.py", 1, "deep") == "pkg.sub.deep"

    def test_climbing_past_the_source_root_is_unresolvable(self):
        assert self.table.resolve_relative("pkg/mod.py", 4, "x") is None


class TestLocalNameOf:
    """`import a.b.c` binding only `a` is the rule most often gotten wrong."""

    def one(self, source: str):
        parse = parse_module("m.py", source)
        (raw,) = parse.imports
        return local_name_of(raw)

    def test_dotted_import_binds_only_the_top_package(self):
        assert self.one("import a.b.c\n") == "a"

    def test_aliased_dotted_import_binds_the_alias(self):
        assert self.one("import a.b.c as x\n") == "x"

    def test_from_import_binds_the_name(self):
        assert self.one("from a import b\n") == "b"

    def test_aliased_from_import_binds_the_alias(self):
        assert self.one("from a import b as c\n") == "c"

    def test_star_import_binds_no_single_name(self):
        assert self.one("from a import *\n") is None


class TestBindings:
    FILES = {
        "pkg/__init__.py": "",
        "pkg/mod.py": "def func():\n    pass\n",
        "consumer.py": "",
    }

    def test_dotted_import_resolves_to_the_top_package(self):
        files = {**self.FILES, "consumer.py": "import pkg.mod\n"}
        assert target(files, "consumer.py", "pkg") == ModuleRef("pkg", "pkg/__init__.py")

    def test_aliased_dotted_import_resolves_to_the_full_module(self):
        files = {**self.FILES, "consumer.py": "import pkg.mod as m\n"}
        assert target(files, "consumer.py", "m") == ModuleRef("pkg.mod", "pkg/mod.py")

    def test_submodule_wins_over_a_same_named_attribute(self):
        files = {**self.FILES, "consumer.py": "from pkg import mod\n"}
        assert target(files, "consumer.py", "mod") == ModuleRef("pkg.mod", "pkg/mod.py")

    def test_from_import_of_a_definition(self):
        files = {**self.FILES, "consumer.py": "from pkg.mod import func\n"}
        assert target(files, "consumer.py", "func") == SymbolId("pkg/mod.py", "func")

    def test_alias_changes_the_local_name_not_the_target(self):
        files = {**self.FILES, "consumer.py": "from pkg.mod import func as run\n"}
        index, _ = build(files)
        bindings = index.bindings_for("consumer.py")
        assert "func" not in bindings
        assert bindings["run"].target == SymbolId("pkg/mod.py", "func")

    def test_relative_import(self):
        files = {**self.FILES, "pkg/other.py": "from .mod import func\n"}
        assert target(files, "pkg/other.py", "func") == SymbolId("pkg/mod.py", "func")

    def test_relative_import_of_the_package_itself(self):
        files = {**self.FILES, "pkg/other.py": "from . import mod\n"}
        assert target(files, "pkg/other.py", "mod") == ModuleRef("pkg.mod", "pkg/mod.py")

    @pytest.mark.parametrize(
        ("source", "name", "expected"),
        [
            ("import os\n", "os", ExternalRef("os")),
            ("from os import path\n", "path", ExternalRef("os.path")),
            ("import requests\n", "requests", ExternalRef("requests")),
        ],
    )
    def test_imports_outside_the_repository_are_kept_as_external(
        self, source, name, expected
    ):
        files = {**self.FILES, "consumer.py": source}
        assert target(files, "consumer.py", name) == expected

    def test_missing_name_in_a_real_module_is_external_not_a_false_symbol(self):
        files = {**self.FILES, "consumer.py": "from pkg.mod import absent\n"}
        assert target(files, "consumer.py", "absent") == ExternalRef("pkg.mod.absent")

    def test_function_local_import_records_its_scope(self):
        files = {
            **self.FILES,
            "consumer.py": "def caller():\n    from pkg.mod import func\n",
        }
        index, _ = build(files)
        binding = index.bindings_for("consumer.py")["func"]
        assert binding.scope_qualname == "caller"
        assert not binding.is_module_level
        assert binding.target == SymbolId("pkg/mod.py", "func")

    def test_later_import_rebinds_the_name(self):
        files = {
            **self.FILES,
            "pkg/two.py": "def func():\n    pass\n",
            "consumer.py": "from pkg.mod import func\nfrom pkg.two import func\n",
        }
        assert target(files, "consumer.py", "func") == SymbolId("pkg/two.py", "func")


class TestReExports:
    def test_package_init_re_export_is_followed(self):
        files = {
            "pkg/__init__.py": "from .impl import Widget\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "consumer.py": "from pkg import Widget\n",
        }
        assert target(files, "consumer.py", "Widget") == SymbolId("pkg/impl.py", "Widget")

    def test_two_hop_re_export_chain(self):
        files = {
            "pkg/__init__.py": "from .middle import Widget\n",
            "pkg/middle.py": "from .impl import Widget\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "consumer.py": "from pkg import Widget\n",
        }
        assert target(files, "consumer.py", "Widget") == SymbolId("pkg/impl.py", "Widget")

    def test_function_local_import_cannot_satisfy_a_re_export(self):
        """A name bound inside a function is not importable from another module."""
        files = {
            "pkg/__init__.py": "def loader():\n    from .impl import Widget\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "consumer.py": "from pkg import Widget\n",
        }
        assert target(files, "consumer.py", "Widget") == ExternalRef("pkg.Widget")

    def test_circular_re_export_terminates(self):
        files = {
            "a/__init__.py": "",
            "a/x.py": "from a.y import thing\n",
            "a/y.py": "from a.x import thing\n",
            "consumer.py": "from a.x import thing\n",
        }
        assert target(files, "consumer.py", "thing") == ExternalRef("a.x.thing")


class TestStarImports:
    FILES = {
        "pkg/__init__.py": "",
        "pkg/mod.py": "def public():\n    pass\n\n\ndef _private():\n    pass\n",
    }

    def test_star_expands_to_public_definitions(self):
        files = {**self.FILES, "consumer.py": "from pkg.mod import *\n"}
        index, _ = build(files)
        bindings = index.bindings_for("consumer.py")
        assert set(bindings) == {"public"}
        assert bindings["public"].target == SymbolId("pkg/mod.py", "public")

    def test_star_from_a_package_follows_its_re_exports(self):
        files = {
            "pkg/__init__.py": "from .impl import Widget\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "consumer.py": "from pkg import *\n",
        }
        assert target(files, "consumer.py", "Widget") == SymbolId("pkg/impl.py", "Widget")

    def test_star_from_outside_the_repository_expands_to_nothing(self):
        files = {**self.FILES, "consumer.py": "from os.path import *\n"}
        index, _ = build(files)
        assert index.bindings_for("consumer.py") == {}

    def test_name_re_exported_through_a_star_is_reachable(self):
        files = {
            "pkg/__init__.py": "from .impl import *\n",
            "pkg/impl.py": "class Widget:\n    pass\n",
            "consumer.py": "from pkg import Widget\n",
        }
        assert target(files, "consumer.py", "Widget") == SymbolId("pkg/impl.py", "Widget")


class TestUnresolvableInput:
    """Paths a real repository reaches that the happy-path tests do not."""

    def test_package_of_an_unindexed_path_is_empty(self):
        table = ModuleTable.build(["pkg/__init__.py"])
        assert table.package_of("somewhere/else.py") == ""

    def test_resolve_relative_with_no_dots_returns_the_module_unchanged(self):
        table = ModuleTable.build(["pkg/__init__.py", "pkg/mod.py"])
        assert table.resolve_relative("pkg/mod.py", 0, "os.path") == "os.path"

    def test_relative_import_past_the_source_root_binds_nothing(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/mod.py": "from .... import escaped\n",
        }
        index, _ = build(files)
        assert index.bindings_for("pkg/mod.py") == {}

    def test_module_in_the_table_but_never_parsed_stays_external(self):
        """A file that failed to parse is still on disk, so it is still a module.

        Reporting a SymbolId into a file this tool could not read would be a
        confident lie; reporting it as external is merely incomplete.
        """
        parses = {
            "consumer.py": parse_module("consumer.py", "from pkg.broken import thing\n")
        }
        table = ModuleTable.build(["consumer.py", "pkg/__init__.py", "pkg/broken.py"])
        index = ImportIndex(parses, table)
        binding = index.bindings_for("consumer.py")["thing"]
        assert binding.target == ExternalRef("pkg.broken.thing")

    @pytest.mark.parametrize(
        "raw_kwargs",
        [
            {"kind": "import", "module": None, "name": None},
            {"kind": "from", "module": "os", "name": None},
        ],
        ids=["import-without-module", "from-without-name"],
    )
    def test_malformed_import_records_bind_nothing(self, raw_kwargs):
        base = parse_module("m.py", "")
        raw = RawImport(asname=None, level=0, line=1, scope_qualname="", **raw_kwargs)
        index, _ = build_import_index({"m.py": replace(base, imports=(raw,))})
        assert index.bindings_for("m.py") == {}


def test_all_bindings_covers_every_parsed_module():
    files = {
        "pkg/__init__.py": "",
        "pkg/mod.py": "import os\n",
        "consumer.py": "from pkg.mod import os as operating_system\n",
    }
    index, _ = build(files)
    everything = index.all_bindings()
    assert set(everything) == set(files)
    # `os` is bound in pkg.mod, so importing it from there resolves through the
    # re-export path even though it lands outside the repository.
    assert everything["consumer.py"]["operating_system"] == Binding(
        local_name="operating_system",
        target=ExternalRef("os"),
        line=1,
        scope_qualname="",
    )
