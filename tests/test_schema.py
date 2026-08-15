import pytest

from blastradius.model import SymbolId
from evaluation.schema import FORCING_CHANGES, Case, load_cases, save_cases


def make_case(**overrides) -> Case:
    defaults = dict(
        id="flask@a1b2c3d::Flask.add_url_rule",
        repo="flask",
        commit="a1b2c3d",
        parent="9f8e7d6",
        symbol=SymbolId("src/flask/app.py", "Flask.add_url_rule"),
        change_kind="added_required",
        changed_parameters=("provide_automatic_options",),
        source_files=("src/flask/blueprints.py",),
        test_files=("tests/test_basic.py",),
        commit_file_count=4,
    )
    return Case(**{**defaults, **overrides})


class TestSymbolId:
    def test_round_trips_through_text(self):
        symbol = SymbolId("src/flask/app.py", "Flask.add_url_rule")
        assert SymbolId.parse(str(symbol)) == symbol

    def test_qualname_may_contain_the_locals_marker(self):
        symbol = SymbolId("a/b.py", "outer.<locals>.inner")
        assert SymbolId.parse(str(symbol)) == symbol

    @pytest.mark.parametrize("text", ["no-separator", "::qualname", "path.py::", ""])
    def test_rejects_malformed_text(self, text):
        with pytest.raises(ValueError):
            SymbolId.parse(text)


class TestCase:
    def test_round_trips_through_json_shape(self):
        case = make_case()
        assert Case.from_dict(case.to_dict()) == case

    @pytest.mark.parametrize("kind", sorted(FORCING_CHANGES))
    def test_forcing_kinds_are_marked(self, kind):
        assert make_case(change_kind=kind).is_forcing

    @pytest.mark.parametrize("kind", ["added_optional", "other"])
    def test_non_forcing_kinds_are_not(self, kind):
        """Adding a defaulted parameter obliges no caller to change."""
        assert not make_case(change_kind=kind).is_forcing


def test_cases_are_written_sorted_so_re_mining_diffs_cleanly(tmp_path):
    path = tmp_path / "cases.json"
    save_cases(path, [make_case(id="b"), make_case(id="a")])
    assert [case.id for case in load_cases(path)] == ["a", "b"]


def test_written_file_ends_with_a_newline(tmp_path):
    path = tmp_path / "cases.json"
    save_cases(path, [make_case()])
    assert path.read_text(encoding="utf-8").endswith("\n")
