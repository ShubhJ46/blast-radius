from pathlib import Path

import pytest

from blastradius.index import build_index, discover
from blastradius.model import SymbolId


class TestDiscover:
    def test_finds_python_files_recursively(self, make_repo):
        root = make_repo({"a.py": "", "pkg/b.py": "", "pkg/sub/c.py": ""})
        assert [p.relative_to(root).as_posix() for p in discover(root)] == [
            "a.py",
            "pkg/b.py",
            "pkg/sub/c.py",
        ]

    def test_ignores_non_python_files(self, make_repo):
        root = make_repo({"a.py": "", "README.md": "", "data.json": ""})
        assert [p.name for p in discover(root)] == ["a.py"]

    @pytest.mark.parametrize("directory", ["venv", "node_modules", "__pycache__", "build"])
    def test_skips_vendored_and_generated_directories(self, make_repo, directory):
        root = make_repo({"a.py": "", f"{directory}/b.py": ""})
        assert [p.name for p in discover(root)] == ["a.py"]

    def test_skips_hidden_directories(self, make_repo):
        root = make_repo({"a.py": "", ".tox/b.py": "", ".git/c.py": ""})
        assert [p.name for p in discover(root)] == ["a.py"]

    def test_a_hidden_directory_above_the_root_does_not_hide_the_project(
        self, make_repo, tmp_path
    ):
        """Indexing a checkout under ~/.local/src must not yield an empty index."""
        root = make_repo({"a.py": "", "pkg/b.py": ""}, root=tmp_path / ".local" / "src")
        assert [p.name for p in discover(root)] == ["a.py", "b.py"]


class TestBuildIndex:
    def test_indexes_every_module(self, sample_repo):
        index = build_index(sample_repo)
        assert index.module_count == 4
        assert set(index.parses) == {"app.py", "pkg/__init__.py", "pkg/base.py", "pkg/impl.py"}

    def test_records_definitions(self, sample_repo):
        index = build_index(sample_repo)
        assert SymbolId("pkg/base.py", "Widget.render") in index.definitions
        assert SymbolId("pkg/base.py", "helper") in index.definitions

    def test_a_property_is_the_getter_not_the_setter(self, make_repo):
        # The setter is written last, so a dict keyed by qualname kept it and
        # `blast-radius impact` reported `(self, value)` and the setter's lines
        # as the definition of the name.
        root = make_repo(
            {
                "m.py": """
                    class Store:
                        @property
                        def query(self):
                            return self._q

                        @query.setter
                        def query(self, value):
                            self._q = value
                """
            }
        )
        definition = build_index(root).definitions[SymbolId("m.py", "Store.query")]
        assert definition.start_line == 3  # the getter's `def`, not the setter's
        assert definition.decorators == ("property",)

    def test_an_overload_group_is_the_implementation_not_a_stub(self, make_repo):
        # A stub declares a type for a checker and is not callable, so its
        # parameters are not the ones a caller has to satisfy.
        root = make_repo(
            {
                "m.py": """
                    from typing import overload

                    @overload
                    def f(a: int) -> int: ...
                    @overload
                    def f(a: str, b: str) -> str: ...
                    def f(a, b=None):
                        return a
                """
            }
        )
        definition = build_index(root).definitions[SymbolId("m.py", "f")]
        assert definition.decorators == ()
        assert definition.signature.positional_names() == ("a", "b")

    def test_a_plain_redefinition_keeps_the_last_one(self, make_repo):
        # Not a decorator protocol: the second `def` replaces the first, which
        # is what Python leaves bound, so deferring to it is correct.
        root = make_repo(
            {
                "m.py": """
                    if True:
                        def f(a):
                            pass
                    else:
                        def f(a, b):
                            pass
                """
            }
        )
        definition = build_index(root).definitions[SymbolId("m.py", "f")]
        assert definition.start_line == 5

    def test_the_other_arms_stay_reachable(self, make_repo):
        root = make_repo(
            {
                "m.py": """
                    class Store:
                        @property
                        def query(self):
                            return self._q

                        @query.setter
                        def query(self, value):
                            self._q = value
                """
            }
        )
        index = build_index(root)
        arms = index.definitions_of(SymbolId("m.py", "Store.query"))
        assert [(d.start_line, d.decorators) for d in arms] == [
            (3, ("property",)),
            (7, ("query.setter",)),
        ]

    def test_definitions_of_an_unknown_file_is_empty(self, sample_repo):
        index = build_index(sample_repo)
        assert index.definitions_of(SymbolId("nope.py", "thing")) == ()

    def test_inverts_references_onto_their_target(self, sample_repo):
        index = build_index(sample_repo)
        uses = index.references_to(SymbolId("pkg/base.py", "helper"))
        assert [(use.path, use.line) for use in uses] == [("app.py", 5), ("pkg/impl.py", 9)]

    def test_references_are_sorted_for_reproducible_output(self, sample_repo):
        index = build_index(sample_repo)
        uses = index.references_to(SymbolId("pkg/base.py", "helper"))
        assert list(uses) == sorted(uses, key=lambda use: (use.path, use.line))

    def test_symbol_with_no_references(self, sample_repo):
        index = build_index(sample_repo)
        assert index.references_to(SymbolId("pkg/base.py", "Widget.alone")) == ()

    def test_builds_the_class_graph(self, sample_repo):
        index = build_index(sample_repo)
        assert index.classes.mro(SymbolId("pkg/impl.py", "Big")) == (
            SymbolId("pkg/impl.py", "Big"),
            SymbolId("pkg/base.py", "Widget"),
        )

    def test_reports_build_time_and_module_count(self, sample_repo):
        index = build_index(sample_repo)
        assert index.build_seconds >= 0
        assert index.module_count == len(index.parses)


class TestUnparseableFiles:
    def test_a_broken_file_is_recorded_rather_than_dropped_silently(self, make_repo):
        root = make_repo({"good.py": "def f():\n    pass\n", "bad.py": "def f(:\n"})
        index = build_index(root)
        assert index.module_count == 1
        assert [path for path, _ in index.skipped] == ["bad.py"]

    def test_a_broken_package_init_does_not_sever_its_siblings(self, make_repo):
        # The damage from an unparseable file used to spread: `pkg/__init__.py`
        # dropping out of the parses stopped `pkg/` counting as a package, so
        # `pkg/mod.py` named itself `mod` and every `from pkg.mod import ...`
        # in the repository resolved as an external reference.
        root = make_repo(
            {
                "pkg/__init__.py": "print 'python 2'\n",
                "pkg/mod.py": "def helper():\n    pass\n",
                "app.py": "from pkg.mod import helper\n\n\ndef run():\n    helper()\n",
            }
        )
        index = build_index(root)
        assert [path for path, _ in index.skipped] == ["pkg/__init__.py"]
        assert index.modules.module_for_path("pkg/mod.py") == "pkg.mod"
        uses = index.references_to(SymbolId("pkg/mod.py", "helper"))
        assert [(use.path, use.line) for use in uses] == [("app.py", 5)]

    def test_an_unreadable_file_is_skipped_rather_than_fatal(self, make_repo, monkeypatch):
        """A broken symlink or a permissions problem must not lose the whole index."""
        root = make_repo({"a.py": "def f():\n    pass\n", "b.py": ""})
        original = Path.read_bytes

        def refuse(self, *args, **kwargs):
            if self.name == "b.py":
                raise OSError("permission denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", refuse)

        index = build_index(root)
        assert index.module_count == 1
        assert [path for path, _ in index.skipped] == ["b.py"]
        assert "permission denied" in index.skipped[0][1]

    def test_the_rest_of_the_index_still_builds(self, make_repo):
        root = make_repo(
            {
                "bad.py": "def broken(:\n",
                "a.py": "def helper():\n    pass\n",
                "b.py": "from a import helper\n\nhelper()\n",
            }
        )
        index = build_index(root)
        assert index.references_to(SymbolId("a.py", "helper"))


class TestBadRoot:
    def test_a_file_is_not_a_repository(self, make_repo):
        root = make_repo({"a.py": ""})
        with pytest.raises(ValueError, match="Not a directory"):
            build_index(root / "a.py")

    def test_a_missing_path(self, tmp_path):
        with pytest.raises(ValueError, match="Not a directory"):
            build_index(tmp_path / "absent")

    def test_an_empty_directory_indexes_to_nothing(self, tmp_path):
        index = build_index(tmp_path)
        assert index.module_count == 0
        assert index.definitions == {}


class TestReusingAPreviousIndex:
    """Parsing is most of the cost, and a file whose bytes are unchanged cannot
    parse differently. The reuse is in memory: see `build_index` for why a cache
    on disk would be slower than parsing again."""

    FILES = {
        "pkg/__init__.py": "",
        "pkg/base.py": "def helper():\n    pass\n",
        "app.py": "from pkg.base import helper\n\nhelper()\n",
    }

    def test_an_unchanged_tree_reuses_every_parse(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        warm = build_index(root, previous=cold)
        assert cold.reused == 0
        assert warm.reused == warm.module_count == 3

    def test_a_changed_file_is_the_only_one_reparsed(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").write_text("from pkg.base import helper\n", encoding="utf-8")
        warm = build_index(root, previous=cold)
        assert warm.reused == 2

    def test_the_result_is_identical_to_a_cold_build(self, make_repo):
        """The whole point: faster must not mean different."""
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").write_text(
            "from pkg.base import helper\n\nhelper()\nhelper()\n", encoding="utf-8"
        )
        warm = build_index(root, previous=cold)
        fresh = build_index(root)
        assert warm.references == fresh.references
        assert warm.definitions == fresh.definitions
        assert warm.module_count == fresh.module_count

    def test_an_edit_that_changes_a_caller_is_seen(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        assert len(cold.references_to(SymbolId("pkg/base.py", "helper"))) == 1
        (root / "app.py").write_text("from pkg.base import helper\n", encoding="utf-8")
        warm = build_index(root, previous=cold)
        assert warm.references_to(SymbolId("pkg/base.py", "helper")) == ()

    def test_an_edit_two_modules_away_is_seen(self, make_repo):
        """A re-export means an edit can change what a name binds to elsewhere,
        which is why everything downstream of parsing is recomputed."""
        root = make_repo(
            {
                "pkg/__init__.py": "from pkg.base import helper\n",
                "pkg/base.py": "def helper():\n    pass\n",
                "app.py": "import pkg\n\npkg.helper()\n",
            }
        )
        cold = build_index(root)
        # `pkg.helper()` in app.py resolves only because __init__ re-exports it.
        assert [use.path for use in cold.references_to(SymbolId("pkg/base.py", "helper"))] == [
            "app.py"
        ]
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        warm = build_index(root, previous=cold)
        # app.py is byte-identical and its parse was reused, yet the reference
        # is correctly gone: the re-export it depended on no longer exists.
        assert warm.reused == 2
        assert warm.references_to(SymbolId("pkg/base.py", "helper")) == ()

    def test_a_new_file_is_picked_up(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "extra.py").write_text(
            "from pkg.base import helper\n\nhelper()\n", encoding="utf-8"
        )
        warm = build_index(root, previous=cold)
        assert warm.module_count == 4
        assert warm.reused == 3
        assert len(warm.references_to(SymbolId("pkg/base.py", "helper"))) == 2

    def test_a_deleted_file_is_dropped(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").unlink()
        warm = build_index(root, previous=cold)
        assert warm.module_count == 2
        assert warm.references_to(SymbolId("pkg/base.py", "helper")) == ()

    def test_identical_content_rewritten_is_still_reused(self, make_repo):
        """Hashing the bytes, not the mtime: a checkout rewrites both."""
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").write_text(self.FILES["app.py"], encoding="utf-8")
        warm = build_index(root, previous=cold)
        assert warm.reused == 3

    def test_a_previously_unparseable_file_is_reparsed_when_fixed(self, make_repo):
        root = make_repo({"broken.py": "def f(:\n"})
        cold = build_index(root)
        assert [path for path, _ in cold.skipped] == ["broken.py"]
        (root / "broken.py").write_text("def f():\n    pass\n", encoding="utf-8")
        warm = build_index(root, previous=cold)
        assert warm.skipped == ()
        assert warm.module_count == 1

    def test_an_index_from_another_root_is_refused(self, make_repo, tmp_path):
        """Reusing parses across trees would report one repo's callers for another."""
        first = make_repo(self.FILES, root=tmp_path / "one")
        second = make_repo(self.FILES, root=tmp_path / "two")
        cold = build_index(first)
        with pytest.raises(ValueError, match="different root"):
            build_index(second, previous=cold)

    def test_an_unchanged_tree_is_not_rebuilt_at_all(self, make_repo):
        """Several questions between edits is the common agent pattern, and the
        answer is already correct: 19s becomes 0.3s on a 441-module repository."""
        root = make_repo(self.FILES)
        cold = build_index(root)
        warm = build_index(root, previous=cold)
        assert warm.references is cold.references
        assert warm.definitions is cold.definitions
        assert warm.classes is cold.classes

    def test_a_single_edit_still_forces_the_rebuild(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").write_text("from pkg.base import helper\n", encoding="utf-8")
        warm = build_index(root, previous=cold)
        assert warm.references is not cold.references
        assert warm.references_to(SymbolId("pkg/base.py", "helper")) == ()

    def test_an_added_file_forces_the_rebuild(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "extra.py").write_text(
            "from pkg.base import helper\n\nhelper()\n", encoding="utf-8"
        )
        warm = build_index(root, previous=cold)
        assert warm.references is not cold.references
        assert len(warm.references_to(SymbolId("pkg/base.py", "helper"))) == 2

    def test_a_removed_file_forces_the_rebuild(self, make_repo):
        root = make_repo(self.FILES)
        cold = build_index(root)
        (root / "app.py").unlink()
        warm = build_index(root, previous=cold)
        assert warm.references is not cold.references
        assert warm.module_count == 2

    def test_a_file_that_stays_unparseable_still_short_circuits(self, make_repo):
        """Its digest is recorded before the parse is attempted, so a broken file
        that nobody touched does not force a rebuild every time."""
        root = make_repo({**self.FILES, "broken.py": "def f(:\n"})
        cold = build_index(root)
        warm = build_index(root, previous=cold)
        assert warm.references is cold.references
        assert [path for path, _ in warm.skipped] == ["broken.py"]
