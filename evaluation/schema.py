"""The evaluation case format, and the reasoning baked into it.

Ground truth for impact analysis is free: when a developer changed a function's
signature, the other files that same commit touched are the blast radius they
actually had to absorb. Mining that from git costs nothing and involves no
judgement calls, which is the whole reason this project can be measured without
a labelling budget or an API bill.

Three fields exist purely to keep that ground truth honest, and they are worth
understanding before reading the miner:

**`change_kind`** — adding a parameter *with a default* obliges no caller to
change. Scoring a tool on such commits punishes it for correctly predicting an
empty blast radius, so only the kinds in `FORCING_CHANGES` belong in the primary
metric. This is the single most important filter in the harness.

**`test_files` split from `source_files`** — a signature change nearly always
touches its tests, but finding tests requires coverage mapping, which stage 1
does not do. Reporting them separately keeps the headline number honest about
what the tool attempts, and the gap between the two columns is the argument for
building coverage mapping later.

**`commit_file_count`** — a commit that changes a signature *and* fixes an
unrelated bug inflates the ground truth and understates precision. Recording the
size lets the runner vary the cap without re-mining a corpus.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blastradius.model import SymbolId

ChangeKind = Literal[
    "added_required",  # new parameter with no default: every call site must change
    "removed",  # parameter gone: callers passing it must change
    "renamed",  # a name changed: keyword call sites must change
    "reordered",  # positional order changed: positional call sites must change
    "made_required",  # a parameter lost its default: callers must now pass it
    "added_optional",  # new parameter with a default: callers need not change
    "other",  # mixed or unclassifiable; excluded from the primary metric
]

# The kinds that oblige a caller to change. Cases outside this set are kept in
# the corpus rather than discarded, so the decision stays visible and reversible
# at reporting time instead of being hidden in whatever ran the miner.
FORCING_CHANGES: frozenset[str] = frozenset(
    {"added_required", "removed", "renamed", "reordered", "made_required"}
)


@dataclass(frozen=True)
class Case:
    """One signature-changing commit, and what it forced to change elsewhere.

    Predictions are compared against `source_files`. The file that defines the
    symbol is excluded from both prediction and ground truth: every tool would
    get it right, so including it inflates precision and recall equally and
    tells the reader nothing.
    """

    id: str  # "flask@a1b2c3d::Flask.add_url_rule"
    repo: str  # corpus key, e.g. "flask"
    commit: str  # the commit that made the change
    parent: str  # the tree the tool is run against
    symbol: SymbolId  # the definition whose signature changed
    change_kind: ChangeKind
    # The parameters that moved. Which callers a change forces to edit depends
    # on these and not only on the kind: removing `can_borrow` breaks the call
    # sites that pass it and leaves the rest alone, and without the name there
    # is no way to tell those apart.
    changed_parameters: tuple[str, ...]
    source_files: tuple[str, ...]  # non-test files the commit touched, minus the definer
    test_files: tuple[str, ...]  # test files the commit touched
    commit_file_count: int  # total files in the commit, before any exclusion
    # Committer date, `YYYY-MM-DD`. Recorded for the same reason as
    # `commit_file_count`: so the runner can re-filter without re-mining. It
    # turned out to matter more than the file count -- accuracy tracks the age
    # of the code closely, because annotations and Python 3 syntax do.
    committed_at: str = ""

    @property
    def is_forcing(self) -> bool:
        return self.change_kind in FORCING_CHANGES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "commit": self.commit,
            "parent": self.parent,
            "symbol": str(self.symbol),
            "change_kind": self.change_kind,
            "changed_parameters": list(self.changed_parameters),
            "source_files": list(self.source_files),
            "test_files": list(self.test_files),
            "commit_file_count": self.commit_file_count,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Case":
        return cls(
            id=raw["id"],
            repo=raw["repo"],
            commit=raw["commit"],
            parent=raw["parent"],
            symbol=SymbolId.parse(raw["symbol"]),
            change_kind=raw["change_kind"],
            changed_parameters=tuple(raw.get("changed_parameters", ())),
            source_files=tuple(raw["source_files"]),
            test_files=tuple(raw["test_files"]),
            commit_file_count=raw["commit_file_count"],
            committed_at=raw.get("committed_at", ""),
        )


def load_cases(path: Path) -> list[Case]:
    return [Case.from_dict(entry) for entry in json.loads(path.read_text(encoding="utf-8"))]


def save_cases(path: Path, cases: list[Case]) -> None:
    """Write cases sorted by id, so re-mining a corpus produces a reviewable diff."""
    ordered = sorted(cases, key=lambda case: case.id)
    payload = json.dumps([case.to_dict() for case in ordered], indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
