# blast-radius

*Working title.*

Ask what breaks before you change it. Given a Python symbol, report the call
sites and overrides that a change to it would force — deterministically, from
the AST, with no model in the path.

Built for coding agents. Today an agent greps for a function name, gets a list
of hits that mixes comments, unrelated same-named methods, and real call sites,
reads several files to disambiguate, and still misses the subclass that
overrides it. The question is entirely mechanical, so it should be answered
mechanically.

## Status

Stage 1 is complete and measured. Scored against **62 signature-changing commits
mined from three real repositories**, with `grep` for the symbol name as the
baseline, because that is what an agent does today:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| **this tool** | **65%** | 63% | **64%** |
| `grep` | 14% | 100% | 24% |

`grep` finds everything and is wrong six times out of seven. The tool is right
about two times in three, in both directions. Full per-corpus breakdown,
including where it does badly and why, is in [results](#results).

| Component | State |
| --- | --- |
| `model.py` — symbol identity, signatures, imports, references | done |
| `parse.py` — file → definitions, scope tree, raw imports | done |
| `imports.py` — module table, import bindings, re-export chains | done |
| `resolve.py` — scope-aware reference resolution | done |
| `classes.py` — base graph, MRO, override detection | done |
| `index.py` — orchestration over a directory | done |
| `impact.py` + `cli.py` — the query and the tool | done |
| `evaluation/schema.py` — the mined-case format | done |
| `evaluation/mine.py` — git history → cases | done |
| `evaluation/run.py` — scoring against the corpus | done |

## The corpus problem

Mining works. The first corpus does not, and finding out why cost more than
building the miner.

Mining [click](https://github.com/pallets/click) — 2,137 commits — yields 44
cases, of which 18 are forcing changes. **Only 3 of those 18 have a non-empty
source-file ground truth.** Recall is undefined for the rest, so the corpus can
measure precision and nothing else.

Hand-auditing individual commits, rather than trusting the aggregate, found
three miner bugs behind part of that: test functions were being mined as
subjects (their caller is pytest), nested functions too (they cannot be
imported), and the ground-truth filter searched diffs for `__init__` when a
caller writes `Widget(...)`. Fixing those took usable cases from 1 to 6.

What remains is not a bug. click is a small library: its functions are called by
*users*, so a signature change touches its own tests and nothing else internal.
A recall number needs corpora with deep internal call graphs — applications and
large frameworks, not focused libraries.

**That prediction held.** Three corpora, most recent 6,000 commits of each:

| Corpus | Cases | Forcing | Usable for recall | On a private `_name` |
| --- | ---: | ---: | ---: | ---: |
| [mypy](https://github.com/python/mypy) | 248 | 148 | **46 (31%)** | 27 |
| [scrapy](https://github.com/scrapy/scrapy) | 155 | 74 | 8 (11%) | 47 |
| [sphinx](https://github.com/sphinx-doc/sphinx) | 131 | 83 | 8 (10%) | 37 |

mypy is a type checker — an application whose modules call each other
constantly — and it yields three times the usable rate of the two libraries.
That is the corpus shape a recall number needs, and it supplies 46 of the 62
scored cases on its own.

The last column explains most of the gap, and it is a property of file-level
evaluation rather than of either tool. A private helper's callers live in its
own file; the defining file is excluded from ground truth because every tool
gets it right. So a signature change to a `_name` usually has an *empty*
cross-file blast radius, and cannot be scored at all:

| | private forcing cases | of those, empty ground truth |
| --- | ---: | ---: |
| scrapy | 47 of 74 (64%) | 45 (96%) |
| sphinx | 37 of 83 (45%) | 34 (92%) |
| mypy | 27 of 148 (18%) | 15 (56%) |

That is the mechanism behind the usable-case share, not a separate fact about
it: scrapy spends 64% of its forcing cases on private helpers and almost none of
those can be measured, while mypy spends 18% and keeps nearly half of even those.
No amount of resolution accuracy moves this, which is worth knowing before
reading a low usable share as a verdict on the tool.

Exercised against the Python 3.14 standard library — 719 modules, 19,892
definitions, 4,293 module-level import bindings, zero parse failures — because
fixtures do not contain the code that breaks a parser.

## Using it

```
blast impact hybrid_search --root path/to/repo
blast refs  Widget.render  --root path/to/repo --json
blast stats --root path/to/repo
```

```
app/retrieval.py::hybrid_search  function  lines 159-171

callers  7 references in 6 files
  app/agent.py:81  (name)
  app/api.py:106  (name)
  ...

overrides  0
overridden  none

blast radius  6 files
  app/agent.py
  app/api.py
  ...
```

`--json` on every command, because a tool an agent has to parse out of prose is
not a tool an agent can rely on. A bare name is resolved to an exact match
first and a trailing segment second (`render` finds `Widget.render`); if it is
still ambiguous, every candidate is listed and the command fails rather than
guessing.

## What resolves today

Measured over the Python 3.14 standard library — 719 modules, 19,892
definitions, zero parse failures. This is reference coverage, not impact
accuracy; the precision/recall table below is the number that actually matters.

| Strategy | References | Share |
| --- | ---: | ---: |
| `name` — scope chain and imports | 15,883 | 44.7% |
| `self_attr` — the class hierarchy | 10,324 | 29.0% |
| `module_attr` — an imported module | 5,560 | 15.6% |
| `constructor` — `C(...)` reaching `C.__init__` | 3,150 | 8.9% |
| `class_attr` — a member of an already-resolved class | 624 | 1.8% |
| **Total resolved** | **35,541** | |

The last two rows were added because the evaluation demanded them, not because
they seemed like good ideas — see [what the evaluation
changed](#what-the-evaluation-changed). Together they are 10.6% of everything
the tool resolves, and without them every `__init__` in a repository reported
zero callers.

(An earlier revision of this table read 1,123 modules and 46,856 references.
That root also included `Lib/site-packages`, so it was never the standard
library on its own. The figures above exclude it and are reproducible with
`blast stats --root <stdlib>`.)

Adding class-hierarchy resolution took unresolved `self.`/`cls.` *calls* from
**11,417 down to 1,093** — a 90.4% reduction, against a predicted 88.9%. It
beat the prediction because the forecast counted only *called* attributes, while
the implementation also resolves a bound method passed as a value
(`handler = self.helper`), which is a reference too.

What remains unresolved is 27,394 attribute calls on values whose type is
unknown (`result.close()`, `logger.warning()`) — **43.5% of all call-shaped
references**. Recovering those needs type inference and is out of scope; the
1,093 remaining `self.`/`cls.` calls are ones whose class inherits from outside
the repository.

The same measurement on a small, mostly-functional codebase found *zero*
`self.` calls to resolve, which is the more useful version of the finding: what
class-hierarchy resolution is worth depends entirely on how object-oriented the
target codebase is.

## The class hierarchy

Also measured over the standard library — 2,861 classes, graph built in 0.33s:

| | Count |
| --- | ---: |
| Classes with at least one base resolved in-repo | 1,831 |
| Classes with at least one base *outside* the repo | 563 |

That second row is the honest caveat. A class deriving from `abc.ABC` or a C
extension type has a real parent this tool cannot see, so every linearisation
it computes is a projection of the true MRO onto indexed classes. Deepest chain
found: 11.

## How it is measured

Ground truth comes from git, so it costs nothing and involves no labelling.
When a developer changed a function's signature, the other files that same
commit touched are the blast radius they actually had to absorb. The harness
mines those commits, checks out the parent revision into a throwaway worktree,
runs the tool against that tree, and compares.

The baseline is `grep` for the bare symbol name, because that is what an agent
does today. It searches the same file set the index covers — grepping a vendored
tree the tool never reads would rig the denominator.

Four filters decide whether the resulting numbers mean anything, and they are
documented in `evaluation/schema.py` and `evaluation/run.py`: only signature
changes that *force* a caller to change are scored, commits touching many files
are capped, test files are reported in a separate column because stage 1 does
not attempt coverage mapping, and predictions are filtered to non-test files
exactly as ground truth is — otherwise a correctly identified test caller scores
as a false positive against ground truth that deliberately withheld it.

A symbol the index cannot find is recorded as an error and excluded, never as an
empty prediction. Counting it as a miss would launder a tool bug into an
honest-looking limitation.

## Results

62 cases, all with non-empty cross-file ground truth:

| Corpus | Cases | precision | recall | F1 |
| --- | ---: | ---: | ---: | ---: |
| sphinx | 8 | 100% | 91% | 95% |
| scrapy | 8 | 88% | 70% | 78% |
| mypy | 46 | 57% | 57% | 57% |
| **all** | **62** | **65%** | **63%** | **64%** |
| `grep`, all | 62 | 14% | 100% | 24% |

**mypy is where it does badly, and the aggregate hides that.** It also supplies
46 of the 62 cases, so it sets the headline. Two effects account for most of the
gap, and reading the individual cases showed neither is a resolution failure:

**22 of mypy's 28 false positives come from two commits touching one function.**
`get_proper_types` is called in twelve files; two commits renamed one of its
*parameters*. Every one of those call sites passes positionally, so none of them
had to change — but all eleven are real dependencies that the tool correctly
found. This is the precision lower bound in its purest form: being right about a
dependency that did not need editing is scored as being wrong.

**Ground truth for `__init__` is too broad.** Since a caller writes `Widget(...)`
and never `__init__`, the miner matches the *class* name in diffs. That also
matches every import and every type annotation of the class. For
`IRBuilder.__init__`, all four "misses" are files that merely annotate a
parameter as `builder: 'IRBuilder'` — which a change to the initialiser's
signature does not oblige to change at all. Narrowing that filter to call-shaped
mentions is the next fix, and it will raise recall without touching the tool.

### What the fixes were worth

Both fixes in [what the evaluation changed](#what-the-evaluation-changed) were
made *because* of the first scored run. On sphinx and scrapy:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| before | 93% | 52% | 67% |
| after | 94% | **81%** | **87%** |

Recall rose 29 points. The denominators are not identical — 18 cases before and
16 after, because the ground-truth fix correctly emptied two of them — so this
is the direction and rough size of the win rather than a controlled A/B.

## What the evaluation changed

The first scored run produced **twelve recall misses**. The useful work was
reading all twelve individually instead of accepting the rate, which is how the
three miner bugs in the previous section were found too. They fell into three
equal groups, and only one group was a real limitation.

**Four were the harness's fault, not the tool's.** The commit itself introduced
the caller — and in one case the file did not exist at the parent revision at
all. Nothing analysing the tree the tool is handed could name a file that gains
its first reference to the symbol in the same commit. Ground truth now requires
the symbol to be mentioned *both* before and after, which is still mechanical.

**Four were a real gap, and none of them needed type inference.**

| Call site | Should resolve to |
| --- | --- |
| `H2ConnectionPool(reactor, crawler.settings)` | `H2ConnectionPool.__init__` |
| `AddonManager()` | `AddonManager.__init__` |
| `Config.read(self.confdir, ...)` | `Config.read` |
| `_DomainsContainer._from_environment(self)` | `_DomainsContainer._from_environment` |

In every one of these the receiver is a class the scope chain or the import
graph had *already resolved*. The tool knew what the name meant and stopped one
step short. Calling a class runs its initialiser, but the call site never writes
`__init__` — so **every `__init__` in every repository reported zero callers**,
which is the most misleading empty answer this tool can give, since adding a
required parameter to one forces every construction site to change. The second
pair is attribute access on a resolved class; `class_attr` was declared in the
model's vocabulary and produced by nothing. Both now resolve through the MRO,
and both stay conservative where they must: `cls(...)` inside a classmethod, and
attribute access on a variable, are left unresolved rather than guessed.

**Four were the genuine limitation** — `stream.close()`,
`self.config.pre_init_values()` — a method on a value whose type cannot be
determined without inference. Those are the honest floor for stage 1.

The single false positive turned out not to be one. `gunzip` was predicted to
affect `sitemap.py`, which the commit did not touch — but `sitemap.py` really
does call `gunzip`, and the reordering simply did not affect its one-argument
call. That is exactly why precision here is reported as a lower bound: a real
dependency that did not *have* to be edited counts against the tool, and is not
a defect.

## Two gaps found by running it on real code

Neither shows up in a test suite; both came from pointing the tool at a project
and checking its answer against `grep` by hand.

**String references are invisible.** In one repository, `hybrid_search` has 14
things depending on it. Seven are code references and all seven are found. The
other seven are `mock.patch("app.agent.hybrid_search")` — a real dependency
expressed as a string. Renaming the function breaks all seven, and this tool
would not warn you. The same blind spot covers Django-style `"app.Model"`
settings, entry points, and dynamic imports.

**A method called through a variable is not found.** `BM25Index.search` reports
zero callers, but `retrieval.py` does call it — as `index.search(...)`, where
`index` came back from a function. Knowing that requires type inference. This is
the honest shape of the 43.5% of call-shaped references that stay unresolved.

Both were predicted to show up as recall misses once the evaluation existed, and
the second one did: a third of the misses in the first scored run were a method
reached through a variable. The first — string references — cannot appear in
this evaluation at all, because ground truth is mined from files whose diff
mentions the symbol, and a `mock.patch("app.agent.hybrid_search")` does mention
it. It stays a known blind spot rather than a measured one.

## Scope, stated up front

- **Stage 1 answers one question**: direct callers and overrides. Not transitive
  importers, not test coverage, not public-API exposure.
- **Attribute calls on values of unknown type are not resolvable** without type
  inference — `stream.close()`, where `stream` came out of a dict. These are
  counted as unresolved attributes and never guessed at. They are 43.5% of all
  call-shaped references in the standard library, and the largest remaining
  source of recall misses.
- **Python first.** C++ needs `compile_commands.json` and libclang; text-level
  parsing is not sufficient there, and demonstrating that gap is its own result.
- **No caching yet.** The index is rebuilt per invocation: 0.34s on this
  26-module repository, 16.7s on the 719-module standard library. Fine for a
  person, far too slow for an agent calling it in a loop, and the fix is a
  content hash per file rather than anything structural. (An earlier draft said
  7.3s for the same 719 modules; that does not reproduce here. Measured at the
  commit before the resolver change, it is 16.75s, so the constructor and
  class-attribute passes did not cause it.)

## Development

```bash
python -m venv venv && venv\Scripts\activate
pip install -e ".[dev]"

ruff check .
pytest --cov
```

Requires Python 3.10+.
