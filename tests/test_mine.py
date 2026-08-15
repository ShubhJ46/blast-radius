import io
import shutil

import pytest

from blastradius.model import Signature, SymbolId
from blastradius.parse import parse_module
from evaluation.mine import (
    GitError,
    classify,
    is_test_path,
    main,
    mention_name,
    mine,
    mine_commit,
    signature_changes,
)
from evaluation.schema import load_cases

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def signature(source: str) -> Signature:
    definition = parse_module("m.py", source).definitions[0]
    assert definition.signature is not None
    return definition.signature


class TestClassify:
    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            ("def f(a): pass", "def f(a, b): pass", "added_required"),
            ("def f(a): pass", "def f(a, *, b): pass", "added_required"),
            ("def f(a): pass", "def f(a, b=1): pass", "added_optional"),
            ("def f(a): pass", "def f(a, *args): pass", "added_optional"),
            ("def f(a): pass", "def f(a, **kwargs): pass", "added_optional"),
            ("def f(a, b): pass", "def f(a): pass", "removed"),
            ("def f(a, b): pass", "def f(a, c): pass", "renamed"),
            ("def f(a, b): pass", "def f(b, a): pass", "reordered"),
            ("def f(a, b=1): pass", "def f(a, b): pass", "made_required"),
            ("def f(a, b): pass", "def f(a, b=1): pass", "other"),
            ("def f(a, b, c): pass", "def f(a, d): pass", "other"),
        ],
    )
    def test_kinds(self, before, after, expected):
        assert classify(signature(before), signature(after)) == expected

    def test_keyword_only_order_is_not_a_forcing_change(self):
        """Keyword-only parameters are passed by name, so shuffling them costs nothing."""
        assert classify(signature("def f(*, a, b): pass"), signature("def f(*, b, a): pass")) == (
            "other"
        )

    def test_annotation_change_alone_is_not_reported(self):
        """Signature equality ignores annotations, so this is not a change at all."""
        assert signature("def f(a): pass") == signature("def f(a: int): pass")


class TestIsTestPath:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_thing.py",
            "src/tests/helpers.py",
            "test_module.py",
            "pkg/thing_test.py",
            "testing/fixtures.py",
        ],
    )
    def test_recognises_tests(self, path):
        assert is_test_path(path)

    @pytest.mark.parametrize(
        "path", ["src/app.py", "pkg/latest.py", "contest.py", "src/attest/thing.py"]
    )
    def test_leaves_source_alone(self, path):
        assert not is_test_path(path)


class TestSignatureChanges:
    def test_reports_a_changed_signature(self):
        changes = signature_changes("m.py", "def f(a):\n    pass\n", "def f(a, b):\n    pass\n")
        assert [(c.qualname, c.kind) for c in changes] == [("f", "added_required")]

    def test_ignores_an_unchanged_signature(self):
        assert signature_changes("m.py", "def f(a):\n    pass\n", "def f(a):\n    return 1\n") == []

    def test_ignores_a_newly_added_function(self):
        assert signature_changes("m.py", "", "def f(a):\n    pass\n") == []

    def test_ignores_a_deleted_function(self):
        assert signature_changes("m.py", "def f(a):\n    pass\n", "") == []

    def test_methods_are_compared_by_qualname(self):
        before = "class C:\n    def m(self):\n        pass\n"
        after = "class C:\n    def m(self, extra):\n        pass\n"
        assert [c.qualname for c in signature_changes("m.py", before, after)] == ["C.m"]

    def test_a_revision_that_does_not_parse_yields_nothing(self):
        assert signature_changes("m.py", "def f(:\n", "def f(a, b):\n    pass\n") == []


@needs_git
class TestMineCommit:
    def test_a_signature_change_becomes_a_case(self, repo):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )

        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case is not None
        assert case.symbol == SymbolId("lib.py", "helper")
        assert case.change_kind == "added_required"
        assert case.source_files == ("app.py",)
        assert case.test_files == ()
        assert case.commit == sha
        assert case.is_forcing
        assert case.id.startswith("corpus@")

    def test_the_defining_file_is_not_part_of_the_ground_truth(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit({"lib.py": "def helper(a, b):\n    pass\n"})
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ()

    def test_tests_are_reported_separately(self, repo):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
                "tests/test_lib.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1, 2)\n",
                "tests/test_lib.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ("app.py",)
        assert case.test_files == ("tests/test_lib.py",)

    def test_a_file_changed_for_an_unrelated_reason_is_not_ground_truth(self, repo):
        """Commits do more than one thing; only files naming the symbol count."""
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "caller.py": "from lib import helper\n\nhelper(1)\n",
                "unrelated.py": "VALUE = 1\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "caller.py": "from lib import helper\n\nhelper(1, 2)\n",
                "unrelated.py": "VALUE = 2\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ("caller.py",)
        assert case.commit_file_count == 3

    def test_a_substring_match_is_not_a_mention(self, repo):
        """`helper` must not be found inside `helpers` or `my_helper`."""
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "other.py": "helpers = []\nmy_helper = 1\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "other.py": "helpers = [1]\nmy_helper = 2\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ()

    def test_non_python_files_count_toward_size_but_not_ground_truth(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n", "CHANGELOG.md": "one\n"})
        sha = repo.commit(
            {"lib.py": "def helper(a, b):\n    pass\n", "CHANGELOG.md": "two\n"}
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ()
        assert case.commit_file_count == 2

    def test_optional_parameter_is_kept_but_marked_non_forcing(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit({"lib.py": "def helper(a, b=1):\n    pass\n"})
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.change_kind == "added_optional"
        assert not case.is_forcing

    def test_two_signature_changes_in_one_commit_are_rejected(self, repo):
        """Their blast radii are unioned in the touched files and cannot be separated."""
        repo.commit({"lib.py": "def one(a):\n    pass\n\n\ndef two(a):\n    pass\n"})
        sha = repo.commit({"lib.py": "def one(a, b):\n    pass\n\n\ndef two(a, b):\n    pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_a_commit_touching_no_signatures_is_rejected(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit({"lib.py": "def helper(a):\n    return 1\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_a_commit_with_no_python_at_all_is_rejected(self, repo):
        repo.commit({"README.md": "one\n"})
        sha = repo.commit({"README.md": "two\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_the_first_commit_has_no_before_state(self, repo):
        sha = repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_an_oversized_commit_is_rejected(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                **{f"noise{n}.py": f"x = {n}\n" for n in range(5)},
            }
        )
        assert mine_commit(repo.git, "corpus", sha, max_files=3) is None

    def test_a_rename_in_the_commit_is_rejected(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n", "other.py": "x = 1\n"})
        repo.run("mv", "other.py", "renamed.py")
        (repo.root / "lib.py").write_text("def helper(a, b):\n    pass\n", encoding="utf-8")
        repo.run("add", "-A")
        repo.run("commit", "-m", "rename and change")
        sha = repo.run("rev-parse", "HEAD").strip()
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None


class TestMentionName:
    @pytest.mark.parametrize(
        ("qualname", "expected"),
        [
            ("helper", "helper"),
            ("Widget.render", "render"),
            ("Exit.__init__", "Exit"),
            ("Ctx.__enter__", "Ctx"),
            ("__getattr__", None),
        ],
    )
    def test_uses_the_name_a_caller_would_write(self, qualname, expected):
        assert mention_name(qualname) == expected


@needs_git
class TestSubjectsThatCannotHaveCallers:
    def test_a_test_function_is_not_a_subject(self, repo):
        """Its caller is pytest, not code, so there is no blast radius to predict."""
        repo.commit({"tests/test_thing.py": "def test_one(a):\n    pass\n"})
        sha = repo.commit({"tests/test_thing.py": "def test_one(a, b):\n    pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_a_nested_function_is_not_a_subject(self, repo):
        repo.commit({"lib.py": "def outer():\n    def inner(a):\n        pass\n"})
        sha = repo.commit({"lib.py": "def outer():\n    def inner(a, b):\n        pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_a_module_level_dunder_has_no_name_to_search_for(self, repo):
        """PEP 562 module `__getattr__`: no owning class, and callers name neither."""
        repo.commit({"lib.py": "def __getattr__(name):\n    pass\n"})
        sha = repo.commit({"lib.py": "def __getattr__(name, default):\n    pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None

    def test_a_constructor_is_matched_by_its_class_name(self, repo):
        """Callers write `Widget(...)`, never `__init__`."""
        repo.commit(
            {
                "lib.py": "class Widget:\n    def __init__(self, a):\n        pass\n",
                "app.py": "from lib import Widget\n\nWidget(1)\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "class Widget:\n    def __init__(self, a, b):\n        pass\n",
                "app.py": "from lib import Widget\n\nWidget(1, 2)\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case is not None
        assert case.symbol.qualname == "Widget.__init__"
        assert case.source_files == ("app.py",)


@needs_git
class TestGitWrapper:
    def test_a_failing_command_raises(self, repo):
        repo.commit({"a.py": "x = 1\n"})
        with pytest.raises(GitError, match="rev-parse"):
            repo.git.run("rev-parse", "--verify", "no-such-revision")

    def test_a_path_absent_at_a_revision_reads_as_none(self, repo):
        sha = repo.commit({"a.py": "x = 1\n"})
        assert repo.git.file_at(sha, "a.py") == "x = 1\n"
        assert repo.git.file_at(sha, "never_existed.py") is None

    def test_a_newly_added_file_of_definitions_yields_no_case(self, repo):
        """The file has no previous version, so nothing about it can have changed."""
        repo.commit({"lib.py": "x = 1\n"})
        sha = repo.commit({"lib.py": "x = 2\n", "new.py": "def added(a):\n    pass\n"})
        assert mine_commit(repo.git, "corpus", sha, max_files=25) is None


@needs_git
class TestMineCli:
    def test_writes_cases_and_reports_the_forcing_split(self, repo, tmp_path):
        repo.commit({"lib.py": "def one(a):\n    pass\n", "app.py": "x = 1\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n", "app.py": "x = 2\n"})
        repo.commit({"lib.py": "def one(a, b, c=1):\n    pass\n"})

        output = tmp_path / "cases" / "corpus.json"
        out, err = io.StringIO(), io.StringIO()
        code = main([str(repo.root), "--output", str(output)], out=out, err=err)

        assert code == 0
        cases = load_cases(output)
        # Sorted, not indexed: cases are ordered by id, which embeds a commit
        # sha, so a fresh repository orders them differently every run.
        assert sorted(case.change_kind for case in cases) == [
            "added_optional",
            "added_required",
        ]
        assert "2 cases written" in out.getvalue()
        assert "1 forcing, 1 excluded" in out.getvalue()

    def test_corpus_name_defaults_to_the_directory(self, repo, tmp_path):
        repo.commit({"lib.py": "def one(a):\n    pass\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n"})
        output = tmp_path / "cases.json"
        main([str(repo.root), "--output", str(output)], out=io.StringIO(), err=io.StringIO())
        assert load_cases(output)[0].repo == "corpus"

    def test_explicit_corpus_name_is_used(self, repo, tmp_path):
        repo.commit({"lib.py": "def one(a):\n    pass\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n"})
        output = tmp_path / "cases.json"
        main(
            [str(repo.root), "--output", str(output), "--name", "flask"],
            out=io.StringIO(),
            err=io.StringIO(),
        )
        assert load_cases(output)[0].repo == "flask"

    def test_a_directory_that_is_not_a_repository(self, tmp_path):
        err = io.StringIO()
        code = main([str(tmp_path), "--output", str(tmp_path / "x.json")], err=err)
        assert code == 2
        assert "not a git repository" in err.getvalue()

    def test_a_bad_revision_is_reported_rather_than_traced(self, repo, tmp_path):
        repo.commit({"lib.py": "x = 1\n"})
        err = io.StringIO()
        code = main(
            [str(repo.root), "--output", str(tmp_path / "x.json"), "--rev", "no-such-rev"],
            out=io.StringIO(),
            err=err,
        )
        assert code == 2
        assert "error:" in err.getvalue()


@needs_git
class TestMine:
    def test_walks_history_and_collects_every_qualifying_commit(self, repo):
        repo.commit({"lib.py": "def one(a):\n    pass\n", "app.py": "x = 1\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n", "app.py": "x = 2\n"})
        repo.commit({"lib.py": "def one(a, b):\n    return 1\n"})
        repo.commit({"lib.py": "def one(a, b, c):\n    return 1\n", "app.py": "x = 3\n"})

        cases = mine(repo.git, "corpus")
        assert [case.change_kind for case in cases] == ["added_required", "added_required"]
        assert all(case.symbol.qualname == "one" for case in cases)

    def test_progress_callback_is_invoked_per_commit(self, repo):
        repo.commit({"lib.py": "def one(a):\n    pass\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n"})
        seen = []
        mine(repo.git, "corpus", progress=lambda *args: seen.append(args))
        assert len(seen) == 2
        assert seen[-1][0] == seen[-1][1]

    def test_max_commits_bounds_the_walk(self, repo):
        repo.commit({"lib.py": "def one(a):\n    pass\n"})
        repo.commit({"lib.py": "def one(a, b):\n    pass\n"})
        repo.commit({"lib.py": "def one(a, b, c):\n    pass\n"})
        assert len(mine(repo.git, "corpus", max_commits=1)) == 1


@needs_git
class TestGroundTruthIsWhatWasThereToFind:
    """A caller the commit itself introduced was never in the tree the tool sees."""

    def test_a_caller_added_by_the_same_commit_is_not_ground_truth(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "newcaller.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ()

    def test_a_caller_that_already_existed_is_kept(self, repo):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "caller.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "caller.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.source_files == ("caller.py",)

    def test_a_test_added_by_the_same_commit_is_not_ground_truth_either(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "tests/test_new.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        case = mine_commit(repo.git, "corpus", sha, max_files=25)
        assert case.test_files == ()
