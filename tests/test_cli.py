import io
import json

import pytest

from blastradius.cli import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, main


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def root(sample_repo) -> str:
    return str(sample_repo)


class TestImpactCommand:
    def test_human_output_names_callers_and_the_blast_radius(self, root):
        code, out, _ = run(["impact", "helper", "--root", root])
        assert code == EXIT_OK
        assert "pkg/base.py::helper" in out
        assert "app.py:5" in out
        assert "pkg/impl.py:9" in out
        assert "blast radius  2 files" in out

    def test_human_output_for_a_symbol_nothing_touches(self, root):
        code, out, _ = run(["impact", "Widget.alone", "--root", root])
        assert code == EXIT_OK
        assert "blast radius  0 files" in out
        assert "none" in out

    def test_overrides_are_reported(self, root):
        _, out, _ = run(["impact", "Widget.render", "--root", root])
        assert "pkg/impl.py::Big.render" in out

    def test_json_output(self, root):
        code, out, _ = run(["impact", "helper", "--root", root, "--json"])
        assert code == EXIT_OK
        payload = json.loads(out)
        assert payload["symbol"] == "pkg/base.py::helper"
        assert payload["kind"] == "function"
        assert payload["files"] == ["app.py", "pkg/impl.py"]
        assert {caller["path"] for caller in payload["callers"]} == {"app.py", "pkg/impl.py"}

    def test_json_output_for_an_override(self, root):
        _, out, _ = run(["impact", "Big.render", "--root", root, "--json"])
        payload = json.loads(out)
        assert payload["overridden"] == "pkg/base.py::Widget.render"
        assert payload["overrides"] == []
        assert payload["files"] == []


class TestRefsCommand:
    def test_human_output(self, root):
        code, out, _ = run(["refs", "helper", "--root", root])
        assert code == EXIT_OK
        assert "2 references" in out
        assert "app.py:5" in out

    def test_json_output(self, root):
        _, out, _ = run(["refs", "helper", "--root", root, "--json"])
        payload = json.loads(out)
        assert payload["symbol"] == "pkg/base.py::helper"
        assert len(payload["references"]) == 2
        assert payload["references"][0]["via"] == "name"

    def test_symbol_with_no_references(self, root):
        code, out, _ = run(["refs", "Widget.alone", "--root", root])
        assert code == EXIT_OK
        assert "0 references" in out


class TestStatsCommand:
    def test_human_output(self, root):
        code, out, _ = run(["stats", "--root", root])
        assert code == EXIT_OK
        assert "modules              4" in out

    def test_json_output(self, root):
        _, out, _ = run(["stats", "--root", root, "--json"])
        payload = json.loads(out)
        assert payload["modules"] == 4
        assert payload["classes"] == 2
        assert payload["skipped"] == []
        assert payload["references"] > 0


class TestFailures:
    def test_unknown_symbol_fails_with_a_message_on_stderr(self, root):
        code, out, err = run(["impact", "absent", "--root", root])
        assert code == EXIT_NOT_FOUND
        assert out == ""
        assert "No symbol matching 'absent'" in err

    def test_ambiguous_symbol_lists_the_candidates(self, root):
        code, out, err = run(["impact", "render", "--root", root])
        assert code == EXIT_NOT_FOUND
        assert out == ""
        assert "ambiguous" in err
        assert "pkg/base.py::Widget.render" in err
        assert "pkg/impl.py::Big.render" in err
        assert "path/to/file.py::qualname" in err

    def test_ambiguity_is_resolvable_with_a_full_symbol_id(self, root):
        code, out, _ = run(["impact", "pkg/impl.py::Big.render", "--root", root])
        assert code == EXIT_OK
        assert "pkg/impl.py::Big.render" in out

    def test_root_that_is_not_a_directory(self, sample_repo):
        code, _, err = run(["stats", "--root", str(sample_repo / "app.py")])
        assert code == EXIT_USAGE
        assert "Not a directory" in err

    def test_refs_of_an_unknown_symbol(self, root):
        code, _, err = run(["refs", "absent", "--root", root])
        assert code == EXIT_NOT_FOUND
        assert "No symbol matching" in err


class TestSkippedFileWarning:
    def test_human_mode_warns_on_stderr(self, make_repo):
        root = make_repo({"good.py": "def f():\n    pass\n", "bad.py": "def f(:\n"})
        code, _, err = run(["stats", "--root", str(root)])
        assert code == EXIT_OK
        assert "1 file could not be parsed" in err
        assert "bad.py" in err

    def test_json_mode_keeps_stderr_clean_and_reports_in_the_payload(self, make_repo):
        root = make_repo({"good.py": "def f():\n    pass\n", "bad.py": "def f(:\n"})
        code, out, err = run(["stats", "--root", str(root), "--json"])
        assert code == EXIT_OK
        assert err == ""
        assert [entry["path"] for entry in json.loads(out)["skipped"]] == ["bad.py"]

    def test_many_skipped_files_are_truncated(self, make_repo):
        files = {f"bad{n}.py": "def f(:\n" for n in range(8)}
        root = make_repo({**files, "good.py": ""})
        _, _, err = run(["stats", "--root", str(root)])
        assert "8 files could not be parsed" in err
        assert "and 3 more" in err


class TestImpactArgument:
    """`--argument` asks what a *specific* edit forces, not what calls the symbol."""

    FILES = {
        "lib.py": "class Builder:\n    def accept(self, expr, can_borrow=False):\n        pass\n",
        "plain.py": "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1)\n",
        "kw.py": (
            "from lib import Builder\n\n\ndef go(b: Builder):\n    b.accept(1, can_borrow=True)\n"
        ),
    }

    def test_without_the_flag_every_caller_is_reported(self, make_repo):
        root = make_repo(self.FILES)
        code, out, _ = run(["impact", "Builder.accept", "--root", str(root)])
        assert code == EXIT_OK
        assert "plain.py" in out and "kw.py" in out

    def test_the_flag_drops_callers_the_change_would_not_break(self, make_repo):
        root = make_repo(self.FILES)
        code, out, _ = run(
            ["impact", "Builder.accept", "--root", str(root), "--argument", "can_borrow"]
        )
        assert code == EXIT_OK
        radius = out.split("blast radius")[-1]
        assert "kw.py" in radius
        assert "plain.py" not in radius

    def test_an_unknown_parameter_says_so_rather_than_reporting_nothing(self, make_repo):
        """An empty radius and a typo must not look the same to an agent."""
        root = make_repo(self.FILES)
        _, out, _ = run(
            ["impact", "Builder.accept", "--root", str(root), "--argument", "no_such"]
        )
        assert "is not a parameter of this symbol" in out

    def test_json_reports_both_the_narrowed_and_the_full_set(self, make_repo):
        root = make_repo(self.FILES)
        _, out, _ = run(
            ["impact", "Builder.accept", "--root", str(root), "--argument", "can_borrow", "--json"]
        )
        payload = json.loads(out)
        assert payload["argument"] == "can_borrow"
        assert payload["files"] == ["kw.py"]
        assert sorted(payload["all_caller_files"]) == ["kw.py", "plain.py"]


class TestUnverifiedFiles:
    """A file the parser cannot read is a hole in the answer, so it is named."""

    LEGACY = {
        "lib.py": "class Widget:\n    def render(self):\n        pass\n",
        "good.py": "from lib import Widget\n\n\ndef go(w: Widget):\n    w.render()\n",
        "legacy.py": "from lib import Widget\n\ndef go(w):\n    print 'py2'\n    w.render()\n",
    }

    def test_human_output_names_them_separately_from_the_radius(self, make_repo):
        root = make_repo(self.LEGACY)
        code, out, _ = run(["impact", "Widget.render", "--root", str(root)])
        assert code == EXIT_OK
        radius, _, unverified = out.partition("unverified")
        assert "good.py" in radius
        assert "legacy.py" not in radius
        assert "legacy.py" in unverified

    def test_json_reports_them_in_their_own_key(self, make_repo):
        root = make_repo(self.LEGACY)
        _, out, _ = run(["impact", "Widget.render", "--root", str(root), "--json"])
        payload = json.loads(out)
        assert payload["files"] == ["good.py"]
        assert payload["unverified"] == ["legacy.py"]

    def test_nothing_is_printed_when_every_file_parses(self, make_repo):
        root = make_repo({k: v for k, v in self.LEGACY.items() if k != "legacy.py"})
        _, out, _ = run(["impact", "Widget.render", "--root", str(root)])
        assert "unverified" not in out
