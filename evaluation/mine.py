"""Turn a repository's history into evaluation cases.

The ground truth for impact analysis is already written down: when a developer
changed a function's signature, the other files that same commit touched are the
blast radius they actually had to absorb. Mining that costs nothing and involves
no judgement calls, which is what lets this project be measured without a
labelling budget.

Getting it *right* is entirely a matter of what gets thrown away. Four rules do
that work, and each one exists because keeping the cases it drops would make the
resulting numbers mean something other than what they claim:

**One signature change per commit.** A commit that changes three signatures has
one set of touched files covering all three blast radii. Scoring each change
against that union would inflate every ground-truth set and punish a correct
prediction.

**Forcing changes only.** Adding a parameter *with a default* obliges no caller
to change, so a tool that correctly predicts an empty blast radius would be
marked wrong. The classification is recorded per case and the filter applied at
reporting time, so the decision stays visible.

**No renames, no merges, no test, nested, or operator-dunder subjects.**
Tracking a symbol across a file rename is a rabbit hole, and a merge commit's
"touched files" are a union of two branches. A test function's caller is the
test runner and a nested function cannot be imported, so neither has a
cross-file blast radius to predict. A caller of `__getitem__` writes
`obj[key]`, which names nothing findable in a diff; `__init__` is kept because
construction writes the class name.

**Ground truth is Python files that mentioned the symbol both before and after.**
The tool predicts `.py` paths, so counting a touched changelog against it would
measure documentation habits. More importantly, the commit's full file list is
not usable on its own -- see `Git.paths_mentioning` for the real case that
forced this narrowing -- and neither is the after-state alone, since a caller
introduced by the same commit never existed in the tree the tool is given. See
`Git.paths_mentioning_at`. The unfiltered file count is recorded separately so
the size cap can be re-tuned without re-mining.
"""

import argparse
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

from blastradius.errors import ParseError
from blastradius.model import Signature, SymbolId
from blastradius.parse import parse_module
from evaluation.schema import Case, ChangeKind, save_cases

# Only fetch and parse files whose diff plausibly touches a definition line.
# Most commits touch no signatures at all, and this turns those into a single
# extra git call rather than a parse of every changed file. Deliberately loose:
# git's -G takes a POSIX *basic* regex, where grouping and alternation would
# need escaping, and an over-inclusive filter costs one wasted parse while an
# over-clever one silently drops cases.
DEF_PATTERN = r"def[[:space:]]"

# A generous default: the runner re-filters from `commit_file_count`, so mining
# is the wrong place to be strict.
DEFAULT_MAX_FILES = 25


class GitError(RuntimeError):
    """A git command failed in a way the miner cannot work around."""


@dataclass(frozen=True)
class Git:
    root: Path

    def run(self, *arguments: str, check: bool = True) -> str:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as error:  # pragma: no cover - environment specific
            raise GitError("git is not on PATH") from error
        if completed.returncode != 0:
            if check:
                raise GitError(f"git {' '.join(arguments)}: {completed.stderr.strip()}")
            return ""
        return completed.stdout

    def commits(self, revision: str, limit: int | None) -> list[tuple[str, list[str]]]:
        """(sha, parents) walking back from a revision, merges excluded.

        Parents come from the same call rather than one `rev-parse` per commit.
        On a corpus of a few thousand commits that is a third of all the process
        spawns, which dominates runtime on Windows.
        """
        arguments = ["rev-list", "--parents", "--no-merges", revision]
        if limit is not None:
            arguments.extend(["--max-count", str(limit)])
        rows = []
        for line in self.run(*arguments).splitlines():
            parts = line.split()
            if parts:
                rows.append((parts[0], parts[1:]))
        return rows

    def parents(self, sha: str) -> list[str]:
        return self.run("rev-list", "--parents", "-n", "1", sha).split()[1:]

    def changed_paths(self, sha: str) -> list[tuple[str, str]]:
        """(status, path) for everything the commit touched.

        `-M` turns rename detection on. Without it a rename arrives as a delete
        plus an add, so both the old and the new path land in the ground truth
        for a file that merely moved -- two entries no tool would ever predict.
        """
        output = self.run("diff-tree", "--no-commit-id", "-r", "-M", "--name-status", sha)
        rows = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0], parts[-1]))
        return rows

    def paths_mentioning(self, sha: str, pattern: re.Pattern[str]) -> set[str]:
        """Files whose diff adds or removes a line matching `pattern`.

        The commit's full file list is not usable as ground truth on its own.
        A real example from a mined corpus: a commit titled "hybrid retrieval
        fusing BM25 with vector search" also happened to drop a parameter from
        one test function, and its nine touched files became that test's
        supposed blast radius. Narrowing to files whose diff actually mentions
        the symbol removes almost all of that, and stays entirely mechanical.

        The trade-off is stated rather than hidden: a caller edited across
        several lines, where only an argument line changed, is missed. That
        errs toward a ground truth slightly too small, which costs the tool
        precision rather than inventing recall it did not earn.
        """
        patch = self.run(
            "diff-tree", "--no-commit-id", "-r", "-M", "-p", sha, "--", "*.py"
        )
        found: set[str] = set()
        current: str | None = None
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                # Paths containing spaces are quoted by git and not handled;
                # they are vanishingly rare in Python projects.
                current = line.split(" b/", 1)[-1]
            elif current and line[:1] in "+-" and not line.startswith(("+++", "---")):
                if pattern.search(line[1:]):
                    found.add(current)
        return found

    def paths_mentioning_at(
        self, sha: str, pattern: re.Pattern[str], paths: list[str]
    ) -> set[str]:
        """Of `paths`, those already matching `pattern` at revision `sha`.

        A file that gains its first reference to the symbol in the very commit
        being mined was not part of the blast radius -- it is a *new* dependent,
        and no tool analysing the parent tree could have predicted it. Four of
        the twelve recall misses in the first scored evaluation were this: a
        caller added in the same commit, or in one case a file that did not
        exist at the parent at all, counted against the tool as something it
        failed to find.

        Restricting ground truth to files that already mentioned the symbol
        keeps it to what was genuinely there to be found, and stays mechanical.
        """
        found: set[str] = set()
        for path in paths:
            content = self.file_at(sha, path)
            if content is not None and pattern.search(content):
                found.add(path)
        return found

    def paths_touching_definitions(self, sha: str) -> set[str]:
        output = self.run(
            "diff-tree",
            "--no-commit-id",
            "-r",
            "--name-only",
            f"-G{DEF_PATTERN}",
            sha,
            "--",
            "*.py",
        )
        return set(output.split())

    def file_at(self, sha: str, path: str) -> str | None:
        content = self.run("show", f"{sha}:{path}", check=False)
        return content or None


def is_test_path(path: str) -> bool:
    """Whether a repo-relative path looks like a test.

    Reported separately from source files because stage 1 does no coverage
    mapping, so a touched test is a guaranteed miss rather than a fair one.
    """
    parts = path.split("/")
    name = parts[-1]
    if any(part in ("test", "tests", "testing") for part in parts[:-1]):
        return True
    return name.startswith("test_") or name.endswith("_test.py")


def mention_name(qualname: str) -> str | None:
    """The identifier a caller would actually write, for the ground-truth filter.

    A dunder is never named at a call site: constructing `Exit(...)` or entering
    a `with` block mentions the class, not `__init__` or `__enter__`. Searching
    diffs for the method name instead finds nothing, which silently turns every
    such case into empty ground truth -- found by hand-auditing a corpus where
    `Exit.__init__` looked like it had no callers at all.
    """
    parts = qualname.split(".")
    final = parts[-1]
    if final.startswith("__") and final.endswith("__"):
        return parts[-2] if len(parts) >= 2 else None
    return final


def is_dunder(qualname: str) -> bool:
    final = qualname.rsplit(".", 1)[-1]
    return final.startswith("__") and final.endswith("__")


def mention_pattern(qualname: str) -> re.Pattern[str] | None:
    """What a line must contain to count as depending on `qualname`.

    For an ordinary function the name itself is enough. For `__init__` it is
    not. The caller writes the *class* name, and that also appears in every
    import and every type annotation of the class -- neither of which a change
    to an initialiser's signature obliges to change. Requiring the call shape
    `Class(` keeps ground truth to actual construction sites.

    Found by scoring rather than by reading the code: all four "misses" for
    `IRBuilder.__init__` were files whose only use of the class was
    `def __init__(self, builder: 'IRBuilder') -> None:`. Counting those as a
    blast radius punished the tool for correctly ignoring an annotation.

    A subclass declaration does not match, since `class Child(Base):` writes
    `Base)` rather than `Base(`. That is the conservative side to be on: the
    subclass *declaration* need not change when the base initialiser does, only
    the places that construct it.
    """
    name = mention_name(qualname)
    if name is None:
        return None
    if is_dunder(qualname):
        return re.compile(rf"\b{re.escape(name)}\s*\(")
    return re.compile(rf"\b{re.escape(name)}\b")


def _is_optional(parameter) -> bool:
    return parameter.has_default or parameter.kind in ("var_positional", "var_keyword")


def classify(before: Signature, after: Signature) -> ChangeKind:
    """Name the kind of signature change, and therefore whether callers must move."""
    before_names = [parameter.name for parameter in before.parameters]
    after_names = [parameter.name for parameter in after.parameters]

    removed = [name for name in before_names if name not in after_names]
    added = [name for name in after_names if name not in before_names]

    if removed and added:
        # One out, one in, same arity: a rename rather than two edits.
        if len(removed) == len(added) == 1 and len(before_names) == len(after_names):
            return "renamed"
        return "other"
    if removed:
        return "removed"
    if added:
        by_name = {parameter.name: parameter for parameter in after.parameters}
        if all(_is_optional(by_name[name]) for name in added):
            return "added_optional"
        return "added_required"

    # Same parameter names. Only order and defaults are left to differ.
    if before.positional_names() != after.positional_names():
        # Keyword-only order is not checked: those are passed by name, so
        # shuffling them obliges nobody to change.
        return "reordered"
    if after.required_names() - before.required_names():
        return "made_required"
    return "other"


@dataclass(frozen=True)
class SignatureChange:
    qualname: str
    before: Signature
    after: Signature

    @property
    def kind(self) -> ChangeKind:
        return classify(self.before, self.after)


def signature_changes(path: str, before: str, after: str) -> list[SignatureChange]:
    """Functions present in both versions of a file whose parameters differ."""
    try:
        # Old revisions are full of things a modern Python warns about --
        # invalid string escapes especially. They are irrelevant to the shape of
        # a signature, and left unsuppressed they bury the progress output.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            old = parse_module(path, before)
            new = parse_module(path, after)
    except ParseError:
        # A revision that does not parse cannot be compared. This is common in
        # older history, where files targeted a Python this parser predates.
        return []

    old_signatures = {
        definition.symbol.qualname: definition.signature
        for definition in old.definitions
        if definition.signature is not None
    }
    changes = []
    for definition in new.definitions:
        if definition.signature is None:
            continue
        previous = old_signatures.get(definition.symbol.qualname)
        if previous is not None and previous != definition.signature:
            changes.append(
                SignatureChange(definition.symbol.qualname, previous, definition.signature)
            )
    return changes


def mine_commit(
    git: Git, repo: str, sha: str, max_files: int, parents: list[str] | None = None
) -> Case | None:
    """Build a case from one commit, or None if it does not qualify.

    `parents` may be supplied by a caller that already knows them, which saves
    one process spawn per commit across a whole history walk.
    """
    parents = git.parents(sha) if parents is None else parents
    if len(parents) != 1:
        return None  # a root commit has no "before"; merges are excluded earlier
    parent = parents[0]

    changed = git.changed_paths(sha)
    if not changed or len(changed) > max_files:
        return None
    if any(status.startswith("R") for status, _ in changed):
        return None  # symbol identity across a rename is a different problem

    candidates = git.paths_touching_definitions(sha)
    if not candidates:
        return None

    found: list[tuple[str, SignatureChange]] = []
    for path in sorted(candidates):
        before = git.file_at(parent, path)
        after = git.file_at(sha, path)
        if before is None or after is None:
            continue
        for change in signature_changes(path, before, after):
            found.append((path, change))
            if len(found) > 1:
                # More than one signature moved: the touched files are a union
                # of several blast radii and cannot be attributed to either.
                return None

    if len(found) != 1:
        return None
    definer, change = found[0]

    # A test function's caller is the test runner, and a nested function cannot
    # be imported at all. Neither has a cross-file blast radius to predict, so
    # scoring against one measures nothing.
    if is_test_path(definer) or "<locals>" in change.qualname:
        return None

    # An operator dunder has no mechanical call-site form: a caller of
    # `__getitem__` writes `obj[key]` and of `__eq__` writes `a == b`, neither
    # of which names anything findable in a diff. `__init__` is the exception,
    # since construction writes the class name. Mining the rest would produce
    # ground truth built from unrelated mentions of the class.
    if is_dunder(change.qualname) and change.qualname.rsplit(".", 1)[-1] != "__init__":
        return None

    pattern = mention_pattern(change.qualname)
    if pattern is None:
        return None
    mentioning = git.paths_mentioning(sha, pattern)
    touched = [
        path
        for _status, path in changed
        if path.endswith(".py") and path != definer and path in mentioning
    ]
    # ...and of those, only the ones that already depended on the symbol before
    # the commit. A caller introduced by the same commit is a new dependent, not
    # a blast-radius casualty.
    existing = git.paths_mentioning_at(parent, pattern, touched)
    touched = [path for path in touched if path in existing]
    return Case(
        id=f"{repo}@{sha[:10]}::{change.qualname}",
        repo=repo,
        commit=sha,
        parent=parent,
        symbol=SymbolId(definer, change.qualname),
        change_kind=change.kind,
        source_files=tuple(sorted(p for p in touched if not is_test_path(p))),
        test_files=tuple(sorted(p for p in touched if is_test_path(p))),
        commit_file_count=len(changed),
    )


def mine(
    git: Git,
    repo: str,
    revision: str = "HEAD",
    max_commits: int | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    progress=None,
) -> list[Case]:
    cases = []
    commits = git.commits(revision, max_commits)
    for position, (sha, parents) in enumerate(commits, start=1):
        case = mine_commit(git, repo, sha, max_files, parents=parents)
        if case is not None:
            cases.append(case)
        if progress is not None:
            progress(position, len(commits), len(cases))
    return cases


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    parser = argparse.ArgumentParser(
        prog="python -m evaluation.mine",
        description="Mine signature-changing commits into evaluation cases.",
    )
    parser.add_argument("repo", type=Path, help="path to a git clone")
    parser.add_argument("--name", help="corpus key (default: the directory name)")
    parser.add_argument("--output", type=Path, required=True, help="where to write the JSON")
    parser.add_argument("--rev", default="HEAD", help="revision to walk back from")
    parser.add_argument("--max-commits", type=int, default=None)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    arguments = parser.parse_args(argv)

    if not (arguments.repo / ".git").exists():
        print(f"error: not a git repository: {arguments.repo}", file=err)
        return 2

    git = Git(arguments.repo.resolve())
    name = arguments.name or arguments.repo.resolve().name

    def report(position: int, total: int, kept: int) -> None:
        if position % 200 == 0 or position == total:
            print(f"  {position}/{total} commits, {kept} cases", file=err)

    try:
        cases = mine(
            git,
            name,
            revision=arguments.rev,
            max_commits=arguments.max_commits,
            max_files=arguments.max_files,
            progress=report,
        )
    except GitError as error:
        print(f"error: {error}", file=err)
        return 2

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    save_cases(arguments.output, cases)

    forcing = sum(1 for case in cases if case.is_forcing)
    print(f"{len(cases)} cases written to {arguments.output}", file=out)
    print(f"  {forcing} forcing, {len(cases) - forcing} excluded from the metric", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
