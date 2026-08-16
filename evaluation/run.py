"""Score the tool's predictions against mined cases, next to a grep baseline.

A case says: at revision `parent`, someone was about to change `symbol`, and the
files they then had to edit were `source_files`. So scoring is mechanical --
check out the parent tree, ask the tool what a change to that symbol would
touch, and compare. The baseline is `grep` for the symbol's name, because that
is what an agent does today, and a tool that cannot beat it has not earned the
indexing cost.

Four decisions here determine whether the resulting numbers mean anything.

**Predictions are filtered to non-test files, exactly as ground truth is.** The
miner splits touched tests into their own column because stage 1 does no
coverage mapping. If the tool's prediction were left unfiltered, a correctly
identified test caller would score as a false positive against ground truth that
deliberately withheld it -- punishing the tool for being right. Tests found are
reported in their own column instead, where they inform the argument for
building coverage mapping rather than distorting the headline.

**The baseline searches the same file set the tool indexes.** Running grep over
the whole checkout while the tool sees only discovered Python modules would beat
the baseline by rigging the denominator.

**Recall is only defined where ground truth is non-empty, and precision only
where something was predicted.** Both denominators are reported alongside the
percentages. A recall figure over a corpus of mostly-empty ground truth is the
specific way this evaluation could look rigorous and mean nothing, which is why
`corpus_health` exists and prints first.

**A symbol the index cannot find is an error, not a miss.** Recording it as an
empty prediction would quietly convert a tool bug -- a parse failure, a
resolution gap -- into a recall number that looks like an honest limitation.
Errors are counted, listed, and excluded from the metric.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from blastradius.impact import impact_of
from blastradius.index import build_index, discover
from blastradius.model import SymbolId
from evaluation.mine import Git, GitError, is_test_path, mention_name
from evaluation.schema import Case, load_cases

# Every reference the resolver currently emits is `resolved`; `name_match` is
# reserved for the attribute matching that stage 1 keeps behind a flag. Filtering
# here rather than assuming means the headline number stays a resolved-only
# figure on the day those start being emitted.
SCORED_CONFIDENCE = "resolved"


@dataclass(frozen=True)
class Score:
    """One prediction against one ground truth, kept as sets rather than counts.

    The sets are retained so a disagreement can be read afterwards. Aggregate
    precision told us nothing useful about the miner's three bugs; the files
    themselves did.
    """

    predicted: frozenset[str]
    actual: frozenset[str]

    @property
    def hits(self) -> frozenset[str]:
        return self.predicted & self.actual

    @property
    def spurious(self) -> frozenset[str]:
        return self.predicted - self.actual

    @property
    def missed(self) -> frozenset[str]:
        return self.actual - self.predicted


@dataclass(frozen=True)
class CaseResult:
    case: Case
    tool: Score | None = None
    baseline: Score | None = None
    tests_found: frozenset[str] = frozenset()
    error: str | None = None

    @property
    def scored(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Totals:
    """Pooled counts. Micro-averaged, so a case with ten touched files carries
    ten times the weight of a case with one -- which is the honest weighting
    when the question is "how much of the real blast radius did it find"."""

    hits: int = 0
    spurious: int = 0
    missed: int = 0

    @property
    def precision(self) -> float | None:
        predicted = self.hits + self.spurious
        return self.hits / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.hits + self.missed
        return self.hits / actual if actual else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if not precision or not recall:
            return None
        return 2 * precision * recall / (precision + recall)


def aggregate(scores: Iterable[Score]) -> Totals:
    hits = spurious = missed = 0
    for score in scores:
        hits += len(score.hits)
        spurious += len(score.spurious)
        missed += len(score.missed)
    return Totals(hits=hits, spurious=spurious, missed=missed)


def is_private(case: Case) -> bool:
    """Whether the symbol is a module-private helper by convention.

    Not enforced by Python, so this is a hint rather than a fact -- some of
    these do have importers in other files. It is reported because it explains
    most of the gap between `forcing` and `usable`: in scrapy, 45 of the 65
    forcing cases with empty ground truth are private, against 2 of the 9 with
    ground truth. A private helper's callers live in its own file, and the
    definer's file is excluded from ground truth by construction, so its blast
    radius is invisible to a file-level evaluation no matter how good the tool.
    """
    return case.symbol.qualname.rsplit(".", 1)[-1].startswith("_")


@dataclass(frozen=True)
class CorpusHealth:
    """Whether a corpus can support a recall number at all.

    Mining `click` produced 44 cases of which 3 forcing ones had any source
    ground truth -- because a focused library's callers are its users, not its
    own code. That is a property of the corpus, not a bug, and no amount of
    scoring code fixes it. Printing this before the metrics stops a confident
    percentage from being read off a denominator of three.
    """

    repo: str
    cases: int
    forcing: int
    usable: int  # forcing cases with at least one source file in ground truth
    private: int  # forcing cases on a `_name`, which a file-level metric cannot see

    @property
    def usable_share(self) -> float:
        return self.usable / self.forcing if self.forcing else 0.0


def corpus_health(cases: Iterable[Case]) -> list[CorpusHealth]:
    by_repo: dict[str, list[Case]] = {}
    for case in cases:
        by_repo.setdefault(case.repo, []).append(case)
    return [
        CorpusHealth(
            repo=repo,
            cases=len(group),
            forcing=sum(1 for case in group if case.is_forcing),
            usable=sum(1 for case in group if case.is_forcing and case.source_files),
            private=sum(1 for case in group if case.is_forcing and is_private(case)),
        )
        for repo, group in sorted(by_repo.items())
    ]


def select(
    cases: Iterable[Case],
    forcing_only: bool = True,
    max_files: int | None = None,
    since: str | None = None,
) -> list[Case]:
    """Apply the reporting-time filters the miner deliberately did not apply.

    `since` is a `YYYY` or `YYYY-MM-DD` bound on the committer date. It exists
    because the corpus spans 2007 to 2026 and the tool cannot parse Python 2 at
    all, so a blended figure measures the parser's era coverage as much as the
    resolver. Filtering here rather than while mining keeps the decision
    visible and reversible, the same as `forcing_only`.
    """
    chosen = []
    for case in cases:
        if forcing_only and not case.is_forcing:
            continue
        if max_files is not None and case.commit_file_count > max_files:
            continue
        if since is not None and case.committed_at < since:
            continue
        chosen.append(case)
    return chosen


@contextmanager
def checkout(git: Git, revision: str) -> Iterator[Path]:
    """Materialise a revision without touching the corpus working tree.

    A `git checkout` would mutate a clone that other cases are being scored
    against, and a half-finished run would leave it on a detached decade-old
    revision. A worktree is independent and disposable.
    """
    directory = Path(tempfile.mkdtemp(prefix="blastradius-eval-"))
    tree = directory / "tree"
    try:
        git.run("worktree", "add", "--detach", "--quiet", str(tree), revision)
        yield tree
    finally:
        git.run("worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(directory, ignore_errors=True)


def source_only(paths: Iterable[str]) -> frozenset[str]:
    return frozenset(path for path in paths if not is_test_path(path))


def affected_files(impact, change_kind: str, parameters: tuple[str, ...]) -> set[str] | None:
    """Files a *specific* signature edit forces to change, or None if all of them.

    Every caller is a real dependency, but only some are work, and which ones
    depends on the edit:

    - `removed` and `renamed` break the call sites that pass the parameter at
      all. `f(x)` survives the removal of `can_borrow`; `f(x, can_borrow=True)`
      does not.
    - `made_required` is the mirror image of a removal: a parameter that lost
      its default breaks the callers *omitting* it.
    - `added_required` obliges every caller to pass something new, and
      `reordered` has no one parameter to blame, so both fall back to the whole
      set rather than inventing a narrower answer.

    `renamed` deliberately does *not* use `by_keyword`, though a pure rename
    only breaks the call sites naming the parameter. Trying it cost 23 points
    of recall: across sixteen renamed cases it produced one hit and twenty-two
    misses, because `classify` calls any one-out-one-in swap at the same arity
    a rename. In practice that is usually a parameter *replaced* by a different
    one -- `crawler` becoming `settings` -- and positional callers must change
    for those. The classifier cannot tell the two apart, so the broader rule is
    the correct one here.
    """
    if change_kind in ("added_required", "reordered") or not parameters:
        return None
    files: set[str] = set()
    for parameter in parameters:
        files.update(
            impact.files_affected_by(parameter, supplied=change_kind != "made_required")
        )
    return files


def predict(
    root: Path,
    symbol: SymbolId,
    change_kind: str | None = None,
    parameters: tuple[str, ...] = (),
) -> tuple[frozenset[str], frozenset[str]]:
    """The tool's answer for one symbol: (source files, test files).

    Raises KeyError if the symbol is not in the index, which the caller records
    as an error rather than an empty prediction.
    """
    # Historical revisions are full of invalid string escapes. Left alone they
    # emit a SyntaxWarning per file per case and bury the progress output, which
    # is the same thing that made the miner unusable interactively. Suppressed
    # here in the harness rather than in the library: a warning about code the
    # user is actually working on is worth seeing.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        index = build_index(root)
    impact = impact_of(index, symbol)
    files = {
        reference.path
        for reference in impact.callers
        if reference.confidence == SCORED_CONFIDENCE
    }
    files |= {override.path for override in impact.overrides}

    if change_kind is not None:
        narrowed = affected_files(impact, change_kind, parameters)
        if narrowed is not None:
            files &= narrowed | {override.path for override in impact.overrides}

    files.discard(symbol.path)
    return source_only(files), frozenset(path for path in files if is_test_path(path))


def grep_baseline(root: Path, name: str, definer: str) -> frozenset[str]:
    """Files whose text mentions `name` as a whole word.

    Restricted to the same modules the index covers, and to non-test files, so
    both columns of the report answer the same question.
    """
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    found = set()
    for path in discover(root):
        relative = path.relative_to(root).as_posix()
        if relative == definer or is_test_path(relative):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if pattern.search(text):
            found.add(relative)
    return frozenset(found)


def score_case(git: Git, case: Case) -> CaseResult:
    """Check out the tree the developer was looking at, and ask both approaches."""
    name = mention_name(case.symbol.qualname)
    if name is None:  # pragma: no cover - the miner drops these before writing
        return CaseResult(case=case, error="no mentionable name")

    actual = frozenset(case.source_files)
    try:
        with checkout(git, case.parent) as tree:
            try:
                predicted, tests_found = predict(
                    tree, case.symbol, case.change_kind, case.changed_parameters
                )
            except KeyError as error:
                return CaseResult(case=case, error=f"symbol not indexed: {error}")
            baseline = grep_baseline(tree, name, case.symbol.path)
    except GitError as error:
        return CaseResult(case=case, error=f"git: {error}")

    return CaseResult(
        case=case,
        tool=Score(predicted=predicted, actual=actual),
        baseline=Score(predicted=baseline, actual=actual),
        tests_found=tests_found,
    )


def run(
    cases: Iterable[Case], corpora: Path, progress=None
) -> list[CaseResult]:
    results = []
    ordered = sorted(cases, key=lambda case: (case.repo, case.id))
    for position, case in enumerate(ordered, start=1):
        root = corpora / case.repo
        if not (root / ".git").exists():
            results.append(CaseResult(case=case, error=f"no corpus clone at {root}"))
        else:
            results.append(score_case(Git(root.resolve()), case))
        if progress is not None:
            progress(position, len(ordered), results[-1])
    return results


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


# Type annotations arrived in Python in 2015 and took years to become ordinary,
# and Python 2 lingered well past its end of life. The split is a blunt line
# through a gradual change, which is why the report shows both sides rather
# than picking one.
ERA_BOUNDARY = "2021"


def _by_era(results: list[CaseResult]) -> list[tuple[str, list[CaseResult]]]:
    """Group scored cases either side of the boundary, dropping empty sides."""
    older = [r for r in results if r.case.committed_at and r.case.committed_at < ERA_BOUNDARY]
    newer = [r for r in results if r.case.committed_at >= ERA_BOUNDARY]
    groups = [(f"before {ERA_BOUNDARY}", older), (f"{ERA_BOUNDARY} onward", newer)]
    return [(label, group) for label, group in groups if group]


def report(results: list[CaseResult], out) -> None:
    """Print corpus health first, then the metrics it licenses.

    The primary table covers only cases with non-empty ground truth. Pooling the
    empty ones into it would compute precision against "nothing was edited",
    where every correct answer is a false positive -- so those are reported
    below as an over-prediction figure, which is what they actually measure.
    """
    scored = [result for result in results if result.scored]
    errors = [result for result in results if not result.scored]

    print("Corpus health", file=out)
    header = f"  {'repo':<12} {'cases':>6} {'forcing':>8} {'usable':>7} {'share':>7} {'private':>8}"
    print(header, file=out)
    for health in corpus_health(result.case for result in results):
        print(
            f"  {health.repo:<12} {health.cases:>6} {health.forcing:>8} "
            f"{health.usable:>7} {health.usable_share:>7.0%} {health.private:>8}",
            file=out,
        )

    with_truth = [result for result in scored if result.case.source_files]
    without_truth = [result for result in scored if not result.case.source_files]
    print(f"\nScored {len(scored)} cases, {len(with_truth)} with source ground truth", file=out)
    if errors:
        print(f"  {len(errors)} excluded as errors", file=out)

    print(f"\nPrimary metric, over the {len(with_truth)} cases with ground truth", file=out)
    print(f"  {'':<10} {'precision':>10} {'recall':>8} {'f1':>7}", file=out)
    for label, pick in (("tool", lambda r: r.tool), ("grep", lambda r: r.baseline)):
        totals = aggregate(pick(result) for result in with_truth)
        print(
            f"  {label:<10} {_percent(totals.precision):>10} "
            f"{_percent(totals.recall):>8} {_percent(totals.f1):>7}",
            file=out,
        )
    print(
        "  Precision is a lower bound: a real dependency that did not have to be\n"
        "  edited -- a caller passing a reordered argument positionally -- counts\n"
        "  against it here, and is not a defect.",
        file=out,
    )

    eras = _by_era(with_truth)
    if len(eras) > 1:
        print("\nBy the age of the code, which is what the parser's reach tracks", file=out)
        print(f"  {'':<12} {'cases':>6} {'precision':>10} {'recall':>8}", file=out)
        for label, group in eras:
            totals = aggregate(result.tool for result in group)
            print(
                f"  {label:<12} {len(group):>6} {_percent(totals.precision):>10} "
                f"{_percent(totals.recall):>8}",
                file=out,
            )

    if without_truth:
        print(
            f"\nOver-prediction, over the {len(without_truth)} cases where nothing "
            "outside the definer was edited",
            file=out,
        )
        for label, pick in (("tool", lambda r: r.tool), ("grep", lambda r: r.baseline)):
            files = sum(len(pick(result).predicted) for result in without_truth)
            loud = sum(1 for result in without_truth if pick(result).predicted)
            print(f"  {label:<10} {files:>4} files across {loud} of them", file=out)

    tests = sum(len(result.tests_found) for result in scored)
    print(
        f"\n  {tests} test files predicted, scored in neither column: "
        "stage 1 does no coverage mapping.",
        file=out,
    )

    if errors:
        print("\nErrors", file=out)
        for result in errors[:10]:
            print(f"  {result.case.id}: {result.error}", file=out)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more", file=out)


def to_json(results: list[CaseResult]) -> list[dict]:
    return [
        {
            "id": result.case.id,
            "symbol": str(result.case.symbol),
            "change_kind": result.case.change_kind,
            "actual": sorted(result.case.source_files),
            "error": result.error,
            **(
                {}
                if result.tool is None or result.baseline is None
                else {
                    "predicted": sorted(result.tool.predicted),
                    "hits": sorted(result.tool.hits),
                    "spurious": sorted(result.tool.spurious),
                    "missed": sorted(result.tool.missed),
                    "baseline_predicted": sorted(result.baseline.predicted),
                    "tests_found": sorted(result.tests_found),
                }
            ),
        }
        for result in results
    ]


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="Score impact predictions against mined cases.",
    )
    parser.add_argument("cases", type=Path, nargs="+", help="mined case files")
    parser.add_argument(
        "--corpora", type=Path, required=True, help="directory of clones, named by repo key"
    )
    parser.add_argument("--output", type=Path, help="write per-case detail as JSON")
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="include changes that oblige no caller to move (off the primary metric)",
    )
    parser.add_argument(
        "--max-files", type=int, default=None, help="skip commits touching more files than this"
    )
    parser.add_argument("--limit", type=int, default=None, help="score only the first N cases")
    parser.add_argument(
        "--since",
        metavar="DATE",
        help="skip commits older than this YYYY or YYYY-MM-DD committer date",
    )
    arguments = parser.parse_args(argv)

    cases: list[Case] = []
    for path in arguments.cases:
        if not path.exists():
            print(f"error: no such case file: {path}", file=err)
            return 2
        cases.extend(load_cases(path))

    chosen = select(
        cases,
        forcing_only=not arguments.all_kinds,
        max_files=arguments.max_files,
        since=arguments.since,
    )
    if arguments.limit is not None:
        chosen = chosen[: arguments.limit]
    if not chosen:
        print("error: no cases selected", file=err)
        return 2

    def show(position: int, total: int, result: CaseResult) -> None:
        marker = "!" if not result.scored else "."
        print(f"  {position}/{total} {marker} {result.case.id}", file=err)

    results = run(chosen, arguments.corpora, progress=show)
    report(results, out)

    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(to_json(results), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nPer-case detail written to {arguments.output}", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
