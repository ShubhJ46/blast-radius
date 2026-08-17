"""Fixtures for tests that need a repository on disk rather than in memory."""

import subprocess
import textwrap
from pathlib import Path

import pytest

from evaluation.mine import Git

# A small repository exercising every edge stage 1 reports: a plain function
# called from two files, a base class overridden in another, and a self-call
# that resolves through the class hierarchy.
SAMPLE_REPO = {
    "pkg/__init__.py": "",
    "pkg/base.py": """
        def helper():
            pass


        class Widget:
            def render(self):
                pass

            def alone(self):
                pass
    """,
    "pkg/impl.py": """
        from pkg.base import Widget, helper


        class Big(Widget):
            def render(self):
                pass

            def call(self):
                helper()
                return self.render()
    """,
    "app.py": """
        from pkg.base import helper


        def run():
            helper()
    """,
}


# One symbol written as two definitions. Shared because the index, the CLI and
# the MCP server each have to answer for it, and four hand-copied versions had
# already drifted apart on the class and method names.
PROPERTY_REPO = {
    "m.py": """
        class Store:
            @property
            def query(self):
                return self._q

            @query.setter
            def query(self, value):
                self._q = value
    """,
}


@pytest.fixture
def property_repo(make_repo) -> Path:
    """A repository whose only symbol is a property with a getter and a setter."""
    return make_repo(PROPERTY_REPO)


@pytest.fixture
def make_repo(tmp_path):
    """Write a {path: source} mapping to disk and return the root."""

    def _make(files: dict[str, str], root: Path | None = None) -> Path:
        base = root if root is not None else tmp_path
        for relative, source in files.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return base

    return _make


@pytest.fixture
def sample_repo(make_repo) -> Path:
    return make_repo(SAMPLE_REPO)


class GitRepo:
    """A throwaway git repository, for the miner and the scorer alike.

    Both need real history rather than a mocked one: the miner reads diffs, and
    the scorer checks out a parent revision into a worktree. Faking git would
    only test the fake.
    """

    def __init__(self, root: Path):
        self.root = root
        self.git = Git(root)

    def run(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for relative, source in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        self.run("add", "-A")
        self.run("commit", "-m", message)
        return self.run("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path) -> GitRepo:
    root = tmp_path / "corpus"
    root.mkdir()
    built = GitRepo(root)
    built.run("init", "-b", "main")
    built.run("config", "user.email", "test@example.com")
    built.run("config", "user.name", "Test")
    built.run("config", "commit.gpgsign", "false")
    return built
