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

    def test_an_unreadable_file_is_skipped_rather_than_fatal(self, make_repo, monkeypatch):
        """A broken symlink or a permissions problem must not lose the whole index."""
        root = make_repo({"a.py": "def f():\n    pass\n", "b.py": ""})
        original = Path.read_text

        def refuse(self, *args, **kwargs):
            if self.name == "b.py":
                raise OSError("permission denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", refuse)

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
