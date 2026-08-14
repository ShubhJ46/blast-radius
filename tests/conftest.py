"""Fixtures for tests that need a repository on disk rather than in memory."""

import textwrap
from pathlib import Path

import pytest

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
