import asyncio
import json
from pathlib import Path

import pytest
from mcp import Client

from blastradius import mcp_server
from blastradius.impact import impact_of
from blastradius.index import build_index
from blastradius.mcp_server import _caveats, _index_for, _resolve, build_server, main
from blastradius.model import SymbolId

WIDGET = {
    "lib.py": (
        "class Widget:\n"
        "    def render(self, mode, fast=False):\n"
        "        pass\n"
        "\n"
        "\n"
        "def helper():\n"
        "    pass\n"
    ),
    "app.py": (
        "from lib import Widget, helper\n"
        "\n"
        "\n"
        "def go(w: Widget):\n"
        "    helper()\n"
        "    w.render('a')\n"
    ),
    "kw.py": "from lib import Widget\n\n\ndef go(w: Widget):\n    w.render('a', fast=True)\n",
}


def call(server, tool: str, arguments: dict):
    """Drive a tool the way a client does, rather than calling the closure."""

    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(go())


def payload(result) -> dict:
    return json.loads(result.content[0].text)


def message(result) -> str:
    return result.content[0].text


@pytest.fixture
def server(make_repo):
    root = make_repo(WIDGET)
    return build_server(root), root


class TestIndexReuse:
    """The reason this server exists: the index outlives the question."""

    def test_a_second_call_reuses_every_parse(self, make_repo):
        root = make_repo(WIDGET)
        first = _index_for(str(root))
        second = _index_for(str(root))
        assert first.reused == 0
        assert second.reused == second.module_count == 3

    def test_an_unchanged_tree_returns_the_same_index(self, make_repo):
        root = make_repo(WIDGET)
        first = _index_for(str(root))
        second = _index_for(str(root))
        assert second.references is first.references

    def test_an_edit_is_picked_up(self, make_repo):
        root = make_repo(WIDGET)
        _index_for(str(root))
        (root / "app.py").write_text("from lib import Widget\n", encoding="utf-8")
        after = _index_for(str(root))
        assert after.references_to(SymbolId("lib.py", "helper")) == ()

    def test_two_roots_are_cached_separately(self, make_repo, tmp_path):
        one = make_repo(WIDGET, root=tmp_path / "one")
        two = make_repo({"solo.py": "def f():\n    pass\n"}, root=tmp_path / "two")
        assert _index_for(str(one)).module_count == 3
        assert _index_for(str(two)).module_count == 1


class TestResolve:
    def test_a_unique_name_resolves(self, make_repo):
        index = build_index(make_repo(WIDGET))
        assert _resolve(index, "helper") == SymbolId("lib.py", "helper")

    def test_an_unknown_name_says_how_to_qualify_it(self, make_repo):
        index = build_index(make_repo(WIDGET))
        with pytest.raises(ValueError, match="No symbol matching"):
            _resolve(index, "nothing_like_this")

    def test_an_ambiguous_name_lists_the_candidates_rather_than_guessing(self, make_repo):
        """Choosing between two same-named methods is how a tool answers
        confidently about the wrong function, and an agent cannot tell."""
        other = "class Other:\n    def render(self):\n        pass\n"
        index = build_index(make_repo({**WIDGET, "other.py": other}))
        with pytest.raises(ValueError, match="ambiguous") as error:
            _resolve(index, "render")
        assert "lib.py::Widget.render" in str(error.value)
        assert "other.py::Other.render" in str(error.value)


class TestCaveats:
    def test_string_references_are_always_declared(self, make_repo):
        index = build_index(make_repo(WIDGET))
        impact = impact_of(index, SymbolId("lib.py", "helper"))
        assert any("mock.patch" in note for note in _caveats(index, impact))

    def test_unresolved_attributes_are_reported(self, make_repo):
        root = make_repo({**WIDGET, "loose.py": "def go(thing):\n    thing.render()\n"})
        index = build_index(root)
        impact = impact_of(index, SymbolId("lib.py", "helper"))
        assert any("not declared" in note for note in _caveats(index, impact))

    def test_a_property_declares_that_the_name_has_another_arm(self, make_repo):
        root = make_repo(
            {
                "lib.py": "class S:\n"
                "    @property\n"
                "    def q(self):\n"
                "        return self._q\n"
                "    @q.setter\n"
                "    def q(self, value):\n"
                "        self._q = value\n"
            }
        )
        index = build_index(root)
        impact = impact_of(index, SymbolId("lib.py", "S.q"))
        assert any("2 definitions" in note for note in _caveats(index, impact))

    def test_unparseable_files_are_reported(self, make_repo):
        index = build_index(make_repo({**WIDGET, "broken.py": "def f(:\n"}))
        impact = impact_of(index, SymbolId("lib.py", "helper"))
        assert any("could not be parsed" in note for note in _caveats(index, impact))


class TestTools:
    def test_the_expected_tools_are_advertised(self, server):
        built, _ = server

        async def go():
            async with Client(built) as client:
                return await client.list_tools()

        names = {tool.name for tool in asyncio.run(go()).tools}
        assert names == {"blast_impact", "blast_refs", "blast_find", "blast_stats"}

    def test_impact_reports_the_blast_radius(self, server):
        built, root = server
        result = payload(call(built, "blast_impact", {"symbol": "helper", "root": str(root)}))
        assert result["blast_radius"] == ["app.py"]
        assert result["callers"][0]["path"] == "app.py"
        assert result["caveats"]

    def test_impact_reports_overrides_and_the_definition_site(self, server):
        built, root = server
        result = payload(
            call(built, "blast_impact", {"symbol": "Widget.render", "root": str(root)})
        )
        assert result["kind"] == "method"
        assert result["lines"] == [2, 3]
        assert sorted(result["blast_radius"]) == ["app.py", "kw.py"]

    def test_an_argument_narrows_to_the_callers_that_pass_it(self, server):
        """The whole point of the flag: every caller is a dependency, not every
        caller is work."""
        built, root = server
        result = payload(
            call(
                built,
                "blast_impact",
                {"symbol": "Widget.render", "argument": "fast", "root": str(root)},
            )
        )
        assert result["blast_radius"] == ["kw.py"]
        assert result["narrowed_to_argument"] == "fast"

    def test_an_unknown_argument_lists_the_real_parameters(self, server):
        built, root = server
        result = call(
            built,
            "blast_impact",
            {"symbol": "Widget.render", "argument": "nope", "root": str(root)},
        )
        assert result.is_error
        assert "Parameters: self, mode, fast" in message(result)

    def test_an_ambiguous_symbol_is_refused_over_the_wire(self, server):
        built, root = server
        result = call(built, "blast_impact", {"symbol": "go", "root": str(root)})
        assert result.is_error
        assert "ambiguous" in message(result)

    def test_refs_lists_every_resolved_use(self, server):
        built, root = server
        result = payload(call(built, "blast_refs", {"symbol": "helper", "root": str(root)}))
        assert [(r["path"], r["line"]) for r in result["references"]] == [("app.py", 5)]

    def test_find_resolves_a_bare_name_to_candidates(self, server):
        built, root = server
        result = payload(call(built, "blast_find", {"name": "render", "root": str(root)}))
        assert result["matches"] == ["lib.py::Widget.render"]

    def test_stats_reports_what_the_tool_cannot_see(self, server):
        built, root = server
        result = payload(call(built, "blast_stats", {"root": str(root)}))
        assert result["modules"] == 3
        assert result["skipped"] == []
        assert "unresolved_attributes" in result

    def test_root_defaults_to_the_one_the_server_started_in(self, make_repo):
        """An agent should not have to repeat the repository path every call."""
        root = make_repo(WIDGET)
        built = build_server(root)
        result = payload(call(built, "blast_stats", {}))
        assert result["root"] == str(root)


class TestMain:
    def test_a_missing_root_is_refused_before_serving(self, tmp_path, capsys):
        assert main(["--root", str(tmp_path / "absent")]) == 2
        assert "not a directory" in capsys.readouterr().err

    def test_serving_runs_over_stdio(self, make_repo, monkeypatch):
        root = make_repo(WIDGET)
        seen = {}

        class _Stub:
            def run(self, transport):
                seen["transport"] = transport

        monkeypatch.setattr(mcp_server, "build_server", lambda _root: _Stub())
        assert main(["--root", str(root)]) == 0
        assert seen["transport"] == "stdio"

    def test_the_root_defaults_to_the_working_directory(self, make_repo, monkeypatch):
        root = make_repo(WIDGET)
        seen = {}

        class _Stub:
            def run(self, transport):
                seen["transport"] = transport

        def record(captured_root):
            seen["root"] = captured_root
            return _Stub()

        monkeypatch.chdir(root)
        monkeypatch.setattr(mcp_server, "build_server", record)
        assert main([]) == 0
        assert Path(seen["root"]).resolve() == root.resolve()


class TestUnverifiedOverMcp:
    LEGACY = {
        "lib.py": "class Widget:\n    def render(self):\n        pass\n",
        "good.py": "from lib import Widget\n\n\ndef go(w: Widget):\n    w.render()\n",
        "legacy.py": "from lib import Widget\n\ndef go(w):\n    print 'py2'\n    w.render()\n",
    }

    def test_unparseable_mentions_are_a_separate_key_and_a_caveat(self, make_repo):
        """An agent must be able to tell 'I could not see this' from 'nothing
        depends on this'."""
        root = make_repo(self.LEGACY)
        built = build_server(root)
        result = payload(call(built, "blast_impact", {"symbol": "Widget.render",
                                                      "root": str(root)}))
        assert result["blast_radius"] == ["good.py"]
        assert result["unverified"] == ["legacy.py"]
        assert any("could not be parsed but mention this name" in n for n in result["caveats"])

    def test_unrelated_unparseable_files_are_reported_as_such(self, make_repo):
        root = make_repo({**self.LEGACY, "legacy.py": "def other():\n    print 'py2'\n"})
        built = build_server(root)
        result = payload(call(built, "blast_impact", {"symbol": "Widget.render",
                                                      "root": str(root)}))
        assert result["unverified"] == []
        assert any("none of them mention this symbol" in n for n in result["caveats"])
