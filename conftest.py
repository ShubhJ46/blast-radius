"""Present so the repository root lands on sys.path during a bare `pytest` run.

`blastradius` is importable once the project is installed, but `evaluation` is a
development harness and is deliberately not packaged. Without a conftest here,
the tests covering the case format fail to import it -- and they fail only under
a plain `pytest` invocation, which is exactly the one a reader will try first.
"""
