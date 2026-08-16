import pytest

from blastradius.impact import find_symbols, impact_of, written_name
from blastradius.index import build_index
from blastradius.model import SymbolId


@pytest.fixture
def index(sample_repo):
    return build_index(sample_repo)


def sym(path: str, qualname: str) -> SymbolId:
    return SymbolId(path, qualname)


class TestCallers:
    def test_function_called_from_two_files(self, index):
        impact = impact_of(index, sym("pkg/base.py", "helper"))
        assert [(c.path, c.line) for c in impact.callers] == [
            ("app.py", 5),
            ("pkg/impl.py", 9),
        ]
        assert impact.caller_files == ("app.py", "pkg/impl.py")

    def test_uncalled_method(self, index):
        impact = impact_of(index, sym("pkg/base.py", "Widget.alone"))
        assert impact.callers == ()
        assert impact.is_empty

    def test_self_call_resolves_to_the_nearest_definition(self, index):
        """`self.render()` inside Big reaches Big.render, not the base's."""
        impact = impact_of(index, sym("pkg/impl.py", "Big.render"))
        assert [(c.path, c.line) for c in impact.callers] == [("pkg/impl.py", 10)]


class TestOverrides:
    def test_base_method_reports_its_overrides(self, index):
        impact = impact_of(index, sym("pkg/base.py", "Widget.render"))
        assert impact.overrides == (sym("pkg/impl.py", "Big.render"),)
        assert impact.overridden is None
        assert not impact.is_empty

    def test_subclass_method_reports_what_it_overrides(self, index):
        impact = impact_of(index, sym("pkg/impl.py", "Big.render"))
        assert impact.overrides == ()
        assert impact.overridden == sym("pkg/base.py", "Widget.render")

    def test_a_plain_function_has_neither(self, index):
        impact = impact_of(index, sym("pkg/base.py", "helper"))
        assert impact.overrides == ()
        assert impact.overridden is None


class TestBlastRadius:
    def test_files_from_callers(self, index):
        impact = impact_of(index, sym("pkg/base.py", "helper"))
        assert impact.files == ("app.py", "pkg/impl.py")

    def test_files_from_overrides_alone(self, index):
        """A base method with no callers still forces its overriders to change."""
        impact = impact_of(index, sym("pkg/base.py", "Widget.render"))
        assert impact.callers == ()
        assert impact.files == ("pkg/impl.py",)

    def test_the_defining_file_is_excluded(self, index):
        """Every tool gets the definition's own file right; counting it says nothing."""
        impact = impact_of(index, sym("pkg/impl.py", "Big.render"))
        assert impact.caller_files == ("pkg/impl.py",)
        assert impact.files == ()

    def test_base_class_reference_counts_as_a_caller(self, index):
        impact = impact_of(index, sym("pkg/base.py", "Widget"))
        assert impact.files == ("pkg/impl.py",)


def test_unknown_symbol_raises(index):
    with pytest.raises(KeyError, match="No such symbol"):
        impact_of(index, sym("pkg/base.py", "absent"))


class TestFindSymbols:
    def test_full_symbol_id(self, index):
        assert find_symbols(index, "pkg/base.py::helper") == [sym("pkg/base.py", "helper")]

    def test_full_symbol_id_that_does_not_exist(self, index):
        assert find_symbols(index, "pkg/base.py::absent") == []

    def test_bare_qualname_unique_across_files(self, index):
        assert find_symbols(index, "helper") == [sym("pkg/base.py", "helper")]

    def test_dotted_qualname(self, index):
        assert find_symbols(index, "Widget.render") == [sym("pkg/base.py", "Widget.render")]

    def test_final_segment_matches_every_owner(self, index):
        assert find_symbols(index, "render") == [
            sym("pkg/base.py", "Widget.render"),
            sym("pkg/impl.py", "Big.render"),
        ]

    def test_exact_qualname_is_not_widened_by_a_looser_match(self, index):
        """`Big.render` must not also drag in `Widget.render`."""
        assert find_symbols(index, "Big.render") == [sym("pkg/impl.py", "Big.render")]

    def test_no_match(self, index):
        assert find_symbols(index, "nothing_like_this") == []

    def test_an_exact_name_wins_over_a_method_sharing_its_final_segment(self, make_repo):
        """`search` means the top-level function, not every `X.search` as well.

        Reporting both would make a bare name unusable in any codebase that has
        a function and a method of the same name, which is most of them. The
        output always prints the full symbol id, so which one was chosen is
        never in doubt.
        """
        root = make_repo(
            {
                "m.py": """
                def search():
                    pass


                class Index:
                    def search(self):
                        pass
                """
            }
        )
        built = build_index(root)
        assert find_symbols(built, "search") == [sym("m.py", "search")]
        assert find_symbols(built, "Index.search") == [sym("m.py", "Index.search")]


ACCEPT = {
    "lib.py": (
        "class Builder:\n"
        "    def accept(self, expr, can_borrow=False):\n"
        "        pass\n"
        "\n"
        "\n"
        "def helper(a, b=1, c=2):\n"
        "    pass\n"
    ),
    "plain.py": "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1)\n",
    "keyword.py": (
        "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1, can_borrow=True)\n"
    ),
    "positional.py": "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1, True)\n",
    "starred.py": "from lib import Builder\n\n\ndef go(b: Builder, a):\n    b.accept(*a)\n",
}


class TestAffectedBy:
    """Every caller is a dependency; only some are work.

    Four commits removing `IRBuilder.accept`'s `can_borrow` parameter were the
    largest single source of false positives in the scored evaluation: eleven
    files call it, and only the ones passing `can_borrow=` had to change.
    """

    def test_every_caller_is_still_reported_without_a_parameter(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.files == ("keyword.py", "plain.py", "positional.py", "starred.py")

    def test_removing_a_parameter_spares_the_callers_that_omit_it(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.files_affected_by("can_borrow") == (
            "keyword.py",
            "positional.py",
            "starred.py",
        )

    def test_a_parameter_everyone_passes_affects_everyone(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.files_affected_by("expr") == impact.files

    def test_making_a_parameter_required_breaks_the_callers_omitting_it(self, make_repo):
        """The mirror image: `f(x)` breaks when `can_borrow` loses its default."""
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.files_affected_by("can_borrow", supplied=False) == (
            "plain.py",
            "starred.py",
        )

    def test_a_receiver_shifts_the_positional_index(self, make_repo):
        """`b.accept(1, True)` fills `accept(self, expr, can_borrow)` three deep."""
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.parameter_index("can_borrow") == 2
        assert "positional.py" in impact.files_affected_by("can_borrow")

    def test_a_plain_function_has_no_receiver_to_shift(self, make_repo):
        files = {
            **ACCEPT,
            "calls.py": "from lib import helper\n\nhelper(1)\n",
            "passes_c.py": "from lib import helper\n\nhelper(1, 2, 3)\n",
        }
        impact = impact_of(build_index(make_repo(files)), SymbolId("lib.py", "helper"))
        assert impact.parameter_index("c") == 2
        assert impact.files_affected_by("c") == ("passes_c.py",)

    def test_a_keyword_only_parameter_has_no_index(self, make_repo):
        files = {"lib.py": "def helper(a, *, flag=False):\n    pass\n"}
        impact = impact_of(build_index(make_repo(files)), SymbolId("lib.py", "helper"))
        assert impact.parameter_index("flag") is None

    def test_an_unknown_parameter_has_no_index(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.parameter_index("nonexistent") is None

    def test_a_class_has_no_signature_to_index(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder"))
        assert impact.parameter_index("anything") is None

    def test_a_reference_that_is_not_a_call_is_not_affected(self, make_repo):
        """`handler = obj.accept` has no arguments to inspect."""
        files = {
            **ACCEPT,
            "valued.py": "from lib import Builder\n\n\ndef go(b: Builder):\n    return b.accept\n",
        }
        impact = impact_of(build_index(make_repo(files)), SymbolId("lib.py", "Builder.accept"))
        assert "valued.py" in impact.files
        assert "valued.py" not in impact.files_affected_by("can_borrow")

    def test_a_rename_spares_positional_callers(self, make_repo):
        """A positional argument never names the parameter, so a rename cannot break it."""
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert impact.files_affected_by("can_borrow", evidence="keyword") == (
            "keyword.py",
            "starred.py",
        )

    def test_a_removal_still_catches_positional_callers(self, make_repo):
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert "positional.py" in impact.files_affected_by("can_borrow")


class TestUnverifiedMentions:
    """A file the parser cannot read contributes nothing to the index, so a
    caller inside it is invisible. Understating a blast radius is the failure
    this tool exists to prevent, so those files are named."""

    LEGACY = {
        "lib.py": "class Widget:\n    def render(self):\n        pass\n",
        "good.py": "from lib import Widget\n\n\ndef go(w: Widget):\n    w.render()\n",
        "legacy.py": "from lib import Widget\n\ndef go(w):\n    print 'py2'\n    w.render()\n",
        "other_legacy.py": "def unrelated():\n    print 'py2'\n",
    }

    def test_an_unparseable_file_mentioning_the_symbol_is_reported(self, make_repo):
        impact = impact_of(build_index(make_repo(self.LEGACY)), sym("lib.py", "Widget.render"))
        assert impact.unverified == ("legacy.py",)

    def test_it_is_kept_out_of_the_blast_radius(self, make_repo):
        """A text match in a file nothing resolved is not evidence."""
        impact = impact_of(build_index(make_repo(self.LEGACY)), sym("lib.py", "Widget.render"))
        assert impact.files == ("good.py",)

    def test_an_unparseable_file_not_mentioning_it_is_not_reported(self, make_repo):
        impact = impact_of(build_index(make_repo(self.LEGACY)), sym("lib.py", "Widget.render"))
        assert "other_legacy.py" not in impact.unverified

    def test_files_that_parse_are_never_text_searched(self, make_repo):
        """Everything readable is resolved properly; text-matching it too would
        reintroduce the noise this tool exists to remove."""
        files = {
            "lib.py": "def helper():\n    pass\n",
            "mentions.py": "# helper is discussed here but never called\n",
        }
        impact = impact_of(build_index(make_repo(files)), sym("lib.py", "helper"))
        assert impact.unverified == ()
        assert impact.files == ()

    def test_a_clean_repository_reports_nothing(self, make_repo):
        index = build_index(make_repo({"m.py": "def f():\n    pass\n"}))
        assert impact_of(index, sym("m.py", "f")).unverified == ()

    def test_a_substring_is_not_a_mention(self, make_repo):
        files = {
            "lib.py": "def helper():\n    pass\n",
            "legacy.py": "helpers = 1\nmy_helper = 2\nprint 'py2'\n",
        }
        impact = impact_of(build_index(make_repo(files)), sym("lib.py", "helper"))
        assert impact.unverified == ()

    def test_an_initialiser_is_searched_by_its_class_name(self, make_repo):
        """A caller writes `Widget(...)`, never `__init__`."""
        files = {
            "lib.py": "class Widget:\n    def __init__(self, a):\n        pass\n",
            "legacy.py": "from lib import Widget\nw = Widget(1)\nprint 'py2'\n",
        }
        impact = impact_of(build_index(make_repo(files)), sym("lib.py", "Widget.__init__"))
        assert impact.unverified == ("legacy.py",)

    def test_a_file_that_vanished_is_skipped_not_raised(self, make_repo):
        root = make_repo(self.LEGACY)
        index = build_index(root)
        (root / "legacy.py").unlink()
        impact = impact_of(index, sym("lib.py", "Widget.render"))
        assert impact.unverified == ()


class TestWrittenName:
    @pytest.mark.parametrize(
        ("qualname", "expected"),
        [
            ("helper", "helper"),
            ("Widget.render", "render"),
            ("Widget.__init__", "Widget"),
            ("__getattr__", "__getattr__"),
        ],
    )
    def test_the_identifier_a_caller_writes(self, qualname, expected):
        assert written_name(qualname) == expected

    def test_a_reorder_spares_callers_that_never_reach_the_moved_position(self, make_repo):
        """`gunzip(response.body)` cannot be broken by a move among later
        parameters -- it was a false positive in the scored run until this."""
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        affected = impact.files_affected_by("can_borrow", evidence="positional")
        assert "plain.py" not in affected      # b.accept(1) never reaches index 2
        assert "positional.py" in affected     # b.accept(1, True) does
        assert "starred.py" in affected        # opaque, so reported either way

    def test_a_reorder_spares_keyword_callers(self, make_repo):
        """Keyword arguments are indifferent to the order of the parameters."""
        impact = impact_of(build_index(make_repo(ACCEPT)), SymbolId("lib.py", "Builder.accept"))
        assert "keyword.py" not in impact.files_affected_by("can_borrow", evidence="positional")

    def test_a_keyword_only_parameter_has_no_position_to_reach(self, make_repo):
        files = {"lib.py": "def helper(a, *, flag=False):\n    pass\n",
                 "call.py": "from lib import helper\n\nhelper(1, flag=True)\n"}
        impact = impact_of(build_index(make_repo(files)), SymbolId("lib.py", "helper"))
        assert impact.files_affected_by("flag", evidence="positional") == ()
