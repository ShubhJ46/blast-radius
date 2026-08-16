import io
import json
import shutil

import pytest

from blastradius.model import SymbolId
from evaluation.run import (
    Score,
    Totals,
    affected_files,
    aggregate,
    corpus_health,
    grep_baseline,
    is_private,
    main,
    predict,
    report,
    run,
    score_case,
    select,
    source_only,
    to_json,
)
from evaluation.schema import Case, save_cases

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

HELPER = SymbolId("lib.py", "helper")


def make_case(
    case_id: str = "corpus@abc0000000::helper",
    repo: str = "corpus",
    commit: str = "abc0000000",
    parent: str = "def0000000",
    symbol: SymbolId = HELPER,
    change_kind: str = "added_required",
    changed_parameters: tuple[str, ...] = ("b",),
    source_files: tuple[str, ...] = ("app.py",),
    test_files: tuple[str, ...] = (),
    commit_file_count: int = 2,
    committed_at: str = "2024-01-01",
) -> Case:
    return Case(
        id=case_id,
        repo=repo,
        commit=commit,
        parent=parent,
        symbol=symbol,
        change_kind=change_kind,
        changed_parameters=changed_parameters,
        source_files=source_files,
        test_files=test_files,
        commit_file_count=commit_file_count,
        committed_at=committed_at,
    )


class TestScore:
    def test_partitions_a_prediction_against_the_truth(self):
        score = Score(predicted=frozenset({"a.py", "b.py"}), actual=frozenset({"b.py", "c.py"}))
        assert score.hits == frozenset({"b.py"})
        assert score.spurious == frozenset({"a.py"})
        assert score.missed == frozenset({"c.py"})


class TestTotals:
    def test_computes_the_usual_three(self):
        totals = Totals(hits=3, spurious=1, missed=1)
        assert totals.precision == 0.75
        assert totals.recall == 0.75
        assert totals.f1 == 0.75

    def test_precision_is_undefined_when_nothing_was_predicted(self):
        """Predicting nothing is not the same as predicting badly."""
        assert Totals(hits=0, spurious=0, missed=2).precision is None

    def test_recall_is_undefined_when_there_is_no_ground_truth(self):
        """The failure mode this whole harness is guarding against."""
        assert Totals(hits=0, spurious=2, missed=0).recall is None

    def test_f1_is_undefined_when_either_half_is(self):
        assert Totals(hits=0, spurious=0, missed=0).f1 is None
        assert Totals(hits=0, spurious=1, missed=1).f1 is None


class TestAggregate:
    def test_pools_counts_across_cases(self):
        scores = [
            Score(predicted=frozenset({"a.py"}), actual=frozenset({"a.py"})),
            Score(predicted=frozenset({"b.py"}), actual=frozenset({"c.py"})),
        ]
        assert aggregate(scores) == Totals(hits=1, spurious=1, missed=1)

    def test_no_cases_is_not_a_division_by_zero(self):
        assert aggregate([]) == Totals()


class TestIsPrivate:
    @pytest.mark.parametrize(
        ("qualname", "expected"),
        [
            ("_load_handler", True),
            ("DownloadHandlers._load_handler", True),
            ("Response.replace", False),
            ("_Private.public", False),
        ],
    )
    def test_looks_at_the_final_segment_only(self, qualname, expected):
        """A public method on a private class is still reachable by importers."""
        assert is_private(make_case(symbol=SymbolId("m.py", qualname))) is expected


class TestCorpusHealth:
    def test_counts_what_each_corpus_can_actually_support(self):
        cases = [
            make_case(case_id="a", repo="one"),
            make_case(case_id="b", repo="one", source_files=()),
            make_case(case_id="c", repo="one", change_kind="added_optional"),
            make_case(case_id="d", repo="two"),
        ]
        health = {entry.repo: entry for entry in corpus_health(cases)}
        assert (health["one"].cases, health["one"].forcing, health["one"].usable) == (3, 2, 1)
        assert health["one"].usable_share == 0.5
        assert health["two"].usable == 1

    def test_counts_private_symbols_because_they_explain_the_gap(self):
        cases = [
            make_case(case_id="a", symbol=SymbolId("m.py", "_helper"), source_files=()),
            make_case(case_id="b", symbol=SymbolId("m.py", "helper")),
        ]
        assert corpus_health(cases)[0].private == 1

    def test_a_corpus_with_no_forcing_cases_reports_zero_not_an_error(self):
        cases = [make_case(change_kind="added_optional")]
        assert corpus_health(cases)[0].usable_share == 0.0


class TestSelect:
    def test_keeps_only_forcing_changes_by_default(self):
        cases = [make_case(case_id="a"), make_case(case_id="b", change_kind="added_optional")]
        assert [case.id for case in select(cases)] == ["a"]

    def test_the_filter_is_reversible_because_the_cases_were_kept(self):
        cases = [make_case(case_id="a"), make_case(case_id="b", change_kind="added_optional")]
        assert len(select(cases, forcing_only=False)) == 2

    def test_re_filters_by_commit_size_without_re_mining(self):
        cases = [
            make_case(case_id="small", commit_file_count=2),
            make_case(case_id="large", commit_file_count=40),
        ]
        assert [case.id for case in select(cases, max_files=10)] == ["small"]


class TestSourceOnly:
    def test_drops_tests(self):
        assert source_only(["app.py", "tests/test_app.py"]) == frozenset({"app.py"})


class TestGrepBaseline:
    def test_finds_whole_word_mentions(self, make_repo):
        root = make_repo(
            {
                "lib.py": "def helper():\n    pass\n",
                "app.py": "from lib import helper\n\nhelper()\n",
                "other.py": "helpers = 1\nmy_helper = 2\n",
            }
        )
        assert grep_baseline(root, "helper", "lib.py") == frozenset({"app.py"})

    def test_excludes_tests_so_both_columns_answer_one_question(self, make_repo):
        root = make_repo(
            {
                "lib.py": "def helper():\n    pass\n",
                "tests/test_lib.py": "from lib import helper\n\nhelper()\n",
            }
        )
        assert grep_baseline(root, "helper", "lib.py") == frozenset()

    def test_searches_only_what_the_index_covers(self, make_repo):
        """Grepping a vendored tree the tool never indexes would rig the comparison."""
        root = make_repo(
            {
                "lib.py": "def helper():\n    pass\n",
                "venv/site.py": "helper()\n",
                "app.py": "helper()\n",
            }
        )
        assert grep_baseline(root, "helper", "lib.py") == frozenset({"app.py"})


class TestPredict:
    def test_reports_callers_and_overrides(self, sample_repo):
        source, tests = predict(sample_repo, SymbolId("pkg/base.py", "Widget.render"))
        assert source == frozenset({"pkg/impl.py"})
        assert tests == frozenset()

    def test_finds_every_calling_file(self, sample_repo):
        source, _ = predict(sample_repo, SymbolId("pkg/base.py", "helper"))
        assert source == frozenset({"app.py", "pkg/impl.py"})

    def test_a_symbol_with_no_users_predicts_nothing(self, sample_repo):
        source, _ = predict(sample_repo, SymbolId("pkg/base.py", "Widget.alone"))
        assert source == frozenset()

    def test_test_callers_are_separated_not_discarded(self, make_repo):
        """Ground truth withholds tests, so the prediction must too -- but the
        tool did find them, and that is the argument for coverage mapping."""
        root = make_repo(
            {
                "lib.py": "def helper():\n    pass\n",
                "app.py": "from lib import helper\n\nhelper()\n",
                "tests/test_lib.py": "from lib import helper\n\nhelper()\n",
            }
        )
        source, tests = predict(root, SymbolId("lib.py", "helper"))
        assert source == frozenset({"app.py"})
        assert tests == frozenset({"tests/test_lib.py"})

    def test_an_unknown_symbol_raises(self, sample_repo):
        with pytest.raises(KeyError):
            predict(sample_repo, SymbolId("pkg/base.py", "nonexistent"))

    def test_old_revisions_do_not_flood_the_progress_output(self, make_repo, recwarn):
        """One SyntaxWarning per file per case buries the run; the miner hit this too."""
        root = make_repo({"lib.py": 'def helper():\n    return "\\d"\n'})
        predict(root, SymbolId("lib.py", "helper"))
        assert [w for w in recwarn if issubclass(w.category, SyntaxWarning)] == []


@needs_git
class TestScoreCase:
    @pytest.fixture
    def scored_repo(self, repo):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        parent = repo.run("rev-parse", "HEAD").strip()
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        return repo, parent, sha

    def test_scores_against_the_tree_the_developer_was_looking_at(self, scored_repo):
        repo, parent, sha = scored_repo
        result = score_case(repo.git, make_case(commit=sha, parent=parent))
        assert result.scored
        assert result.tool.hits == frozenset({"app.py"})
        assert result.tool.spurious == frozenset()
        assert result.tool.missed == frozenset()

    def test_the_baseline_answers_the_same_question(self, scored_repo):
        repo, parent, sha = scored_repo
        result = score_case(repo.git, make_case(commit=sha, parent=parent))
        assert result.baseline.predicted == frozenset({"app.py"})

    def test_the_corpus_working_tree_is_left_alone(self, scored_repo):
        """A checkout would strand the clone on an old revision mid-run."""
        repo, parent, sha = scored_repo
        score_case(repo.git, make_case(commit=sha, parent=parent))
        assert repo.run("rev-parse", "HEAD").strip() == sha
        assert repo.run("status", "--porcelain").strip() == ""

    def test_a_symbol_the_index_cannot_find_is_an_error_not_a_miss(self, scored_repo):
        repo, parent, sha = scored_repo
        case = make_case(commit=sha, parent=parent, symbol=SymbolId("lib.py", "gone"))
        result = score_case(repo.git, case)
        assert not result.scored
        assert "symbol not indexed" in result.error
        assert result.tool is None

    def test_an_unreachable_revision_is_an_error(self, repo):
        repo.commit({"lib.py": "def helper(a):\n    pass\n"})
        result = score_case(repo.git, make_case(parent="0" * 40))
        assert not result.scored
        assert result.error.startswith("git:")


@needs_git
class TestRun:
    def test_a_missing_clone_is_reported_per_case(self, tmp_path):
        results = run([make_case(repo="absent")], tmp_path)
        assert "no corpus clone" in results[0].error

    def test_progress_sees_every_case(self, tmp_path):
        seen = []
        cases = [make_case(repo="absent"), make_case(case_id="b", repo="absent")]
        run(cases, tmp_path, progress=lambda position, *_: seen.append(position))
        assert seen == [1, 2]


class TestReport:
    def test_health_is_printed_before_the_metrics_it_licenses(self, tmp_path):
        results = run([make_case(repo="absent")], tmp_path)
        out = io.StringIO()
        report(results, out)
        text = out.getvalue()
        assert text.index("Corpus health") < text.index("precision")
        assert "1 excluded as errors" in text

    def test_a_long_error_list_is_truncated(self, tmp_path):
        cases = [make_case(case_id=f"case-{n}", repo="absent") for n in range(12)]
        out = io.StringIO()
        report(run(cases, tmp_path), out)
        assert "... and 2 more" in out.getvalue()

    def test_undefined_metrics_print_as_such(self, tmp_path):
        out = io.StringIO()
        report(run([make_case(repo="absent")], tmp_path), out)
        assert "n/a" in out.getvalue()

    @needs_git
    def test_empty_ground_truth_is_over_prediction_not_bad_precision(self, repo):
        """Scoring "nothing was edited" as precision makes every hit a miss."""
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        parent = repo.run("rev-parse", "HEAD").strip()
        sha = repo.commit({"lib.py": "def helper(a, b):\n    pass\n"})

        result = score_case(repo.git, make_case(commit=sha, parent=parent, source_files=()))
        out = io.StringIO()
        report([result], out)
        text = out.getvalue()
        assert "Over-prediction, over the 1 cases" in text
        assert "tool          1 files across 1 of them" in text
        assert "Primary metric, over the 0 cases" in text


class TestToJson:
    def test_an_errored_case_carries_no_prediction(self, tmp_path):
        payload = to_json(run([make_case(repo="absent")], tmp_path))
        assert payload[0]["error"] is not None
        assert "predicted" not in payload[0]


@needs_git
class TestMain:
    @pytest.fixture
    def corpus(self, repo, tmp_path):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        parent = repo.run("rev-parse", "HEAD").strip()
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        path = tmp_path / "cases.json"
        save_cases(path, [make_case(commit=sha, parent=parent)])
        return path, tmp_path

    def test_scores_a_corpus_and_writes_detail(self, corpus):
        cases_path, corpora = corpus
        output = corpora / "results" / "detail.json"
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [str(cases_path), "--corpora", str(corpora), "--output", str(output)],
            out=out,
            err=err,
        )
        assert code == 0
        assert "100%" in out.getvalue()
        assert json.loads(output.read_text(encoding="utf-8"))[0]["hits"] == ["app.py"]

    def test_a_missing_case_file_is_refused(self, tmp_path):
        err = io.StringIO()
        code = main([str(tmp_path / "nope.json"), "--corpora", str(tmp_path)], err=err)
        assert code == 2
        assert "no such case file" in err.getvalue()

    def test_filtering_everything_out_is_refused_rather_than_reported_as_zero(self, tmp_path):
        path = tmp_path / "cases.json"
        save_cases(path, [make_case(change_kind="added_optional")])
        err = io.StringIO()
        code = main([str(path), "--corpora", str(tmp_path)], err=err)
        assert code == 2
        assert "no cases selected" in err.getvalue()

    def test_limit_and_all_kinds_are_honoured(self, tmp_path):
        path = tmp_path / "cases.json"
        save_cases(
            path,
            [
                make_case(case_id="a", repo="absent", change_kind="added_optional"),
                make_case(case_id="b", repo="absent"),
            ],
        )
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [str(path), "--corpora", str(tmp_path), "--all-kinds", "--limit", "1"],
            out=out,
            err=err,
        )
        assert code == 0
        assert "1/1" in err.getvalue()

    def test_max_files_is_passed_through(self, tmp_path):
        path = tmp_path / "cases.json"
        save_cases(path, [make_case(repo="absent", commit_file_count=99)])
        err = io.StringIO()
        code = main([str(path), "--corpora", str(tmp_path), "--max-files", "5"], err=err)
        assert code == 2


class TestAffectedFiles:
    """Which callers a *specific* signature edit forces to move."""

    class _Impact:
        """Stands in for a real Impact: the per-kind rule is what is under test."""

        def __init__(self):
            self.calls = []

        def files_affected_by(self, parameter, *, supplied=True, by_keyword=False):
            self.calls.append((parameter, supplied, by_keyword))
            return {f"{parameter}-{'supplied' if supplied else 'omitted'}.py"}

    def test_a_removal_narrows_to_the_callers_passing_it(self):
        impact = self._Impact()
        assert affected_files(impact, "removed", ("can_borrow",)) == {"can_borrow-supplied.py"}
        assert impact.calls == [("can_borrow", True, False)]

    def test_a_rename_narrows_like_a_removal(self):
        """A pure rename would only break keyword callers, but `classify` cannot
        identify pure renames -- see the note in `affected_files`."""
        impact = self._Impact()
        affected_files(impact, "renamed", ("old",))
        assert impact.calls == [("old", True, False)]

    def test_making_a_parameter_required_narrows_to_the_callers_omitting_it(self):
        impact = self._Impact()
        assert affected_files(impact, "made_required", ("flag",)) == {"flag-omitted.py"}
        assert impact.calls == [("flag", False, False)]

    def test_a_new_required_parameter_affects_every_caller(self):
        """There is no call site that already passes it, so none can be spared."""
        assert affected_files(self._Impact(), "added_required", ("fresh",)) is None

    def test_a_reorder_affects_every_caller(self):
        assert affected_files(self._Impact(), "reordered", ()) is None

    def test_no_named_parameter_falls_back_to_every_caller(self):
        assert affected_files(self._Impact(), "removed", ()) is None

    def test_several_parameters_union_their_callers(self):
        impact = self._Impact()
        result = affected_files(impact, "removed", ("a", "b"))
        assert result == {"a-supplied.py", "b-supplied.py"}

    def test_a_specific_edit_narrows_the_prediction(self, make_repo):
        """The IRBuilder.accept shape: four callers, one forced to change."""
        root = make_repo(
            {
                "lib.py": (
                    "class Builder:\n"
                    "    def accept(self, expr, can_borrow=False):\n"
                    "        pass\n"
                ),
                "plain.py": "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1)\n",
                "kw.py": (
                    "from lib import Builder\n\n\ndef go(b: Builder):\n"
                    "    b.accept(1, can_borrow=True)\n"
                ),
            }
        )
        symbol = SymbolId("lib.py", "Builder.accept")
        assert predict(root, symbol)[0] == frozenset({"kw.py", "plain.py"})
        narrowed, _ = predict(root, symbol, "removed", ("can_borrow",))
        assert narrowed == frozenset({"kw.py"})


class TestEraSelection:
    """The corpus spans 2007 to 2026 and the parser cannot read Python 2, so
    the age of the code is a reporting-time filter like every other one."""

    def test_older_cases_are_dropped(self):
        cases = [
            make_case(case_id="old", committed_at="2009-04-01"),
            make_case(case_id="new", committed_at="2023-04-01"),
        ]
        assert [case.id for case in select(cases, since="2021")] == ["new"]

    def test_a_full_date_bound_works_too(self):
        cases = [
            make_case(case_id="just-before", committed_at="2021-05-31"),
            make_case(case_id="just-after", committed_at="2021-06-02"),
        ]
        assert [case.id for case in select(cases, since="2021-06-01")] == ["just-after"]

    def test_the_boundary_date_is_included(self):
        cases = [make_case(case_id="boundary", committed_at="2021-01-01")]
        assert [case.id for case in select(cases, since="2021")] == ["boundary"]

    def test_no_bound_keeps_everything(self):
        cases = [
            make_case(case_id="old", committed_at="2009-04-01"),
            make_case(case_id="new", committed_at="2023-04-01"),
        ]
        assert len(select(cases)) == 2

    def test_a_case_without_a_date_is_dropped_by_a_bound(self):
        """An undated case predates the field; it cannot be claimed as modern."""
        assert select([make_case(committed_at="")], since="2021") == []


class TestEraReport:
    @needs_git
    def test_both_sides_are_shown_when_the_corpus_spans_the_boundary(self, repo):
        repo.commit(
            {
                "lib.py": "def helper(a):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1)\n",
            }
        )
        parent = repo.run("rev-parse", "HEAD").strip()
        sha = repo.commit(
            {
                "lib.py": "def helper(a, b):\n    pass\n",
                "app.py": "from lib import helper\n\nhelper(1, 2)\n",
            }
        )
        results = [
            score_case(repo.git, make_case(case_id="old", commit=sha, parent=parent,
                                           committed_at="2009-01-01")),
            score_case(repo.git, make_case(case_id="new", commit=sha, parent=parent,
                                           committed_at="2023-01-01")),
        ]
        out = io.StringIO()
        report(results, out)
        text = out.getvalue()
        assert "By the age of the code" in text
        assert "before 2021" in text
        assert "2021 onward" in text

    def test_a_single_era_prints_no_breakdown(self, tmp_path):
        """Nothing to compare, so the section would be noise."""
        out = io.StringIO()
        report(run([make_case(repo="absent", committed_at="2023-01-01")], tmp_path), out)
        assert "By the age of the code" not in out.getvalue()
