import io
import json
import shutil

import pytest

from blastradius.model import SymbolId
from evaluation.run import (
    Score,
    Totals,
    aggregate,
    corpus_health,
    grep_baseline,
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
    source_files: tuple[str, ...] = ("app.py",),
    test_files: tuple[str, ...] = (),
    commit_file_count: int = 2,
) -> Case:
    return Case(
        id=case_id,
        repo=repo,
        commit=commit,
        parent=parent,
        symbol=symbol,
        change_kind=change_kind,
        source_files=source_files,
        test_files=test_files,
        commit_file_count=commit_file_count,
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
