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

Stage 1 is complete and measured. Scored against **113 signature-changing
commits mined from the full history of four real repositories**, with `grep`
for the symbol name as the baseline, because that is what an agent does today:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| **this tool** | **83%** | 64% | **72%** |
| `grep` | 24% | 100% | 38% |

`grep` finds everything and is wrong three times out of four.

That corpus spans 2007 to 2026, and **the tool's recall depends heavily on how
modern the code is** — 84% on commits from 2021 onward, 46% before. Precision
does not vary that way (85% and 81%). The blended number above is the honest
headline; the split is the useful fact, and [results](#results) has both plus
why the old code scores as it does.

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
| `mcp_server.py` — the tool an agent actually calls | done |

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
A recall number needs corpora where the callers live in the repository.

Four corpora — sphinx and scrapy to the root commit, mypy and django to a
6,000-commit window:

| Corpus | Commits walked | Cases | Forcing | Usable for recall | On a private `_name` |
| --- | ---: | ---: | ---: | ---: | ---: |
| [mypy](https://github.com/python/mypy) | 6,000 | 300 | 165 | **49 (30%)** | 35 |
| [sphinx](https://github.com/sphinx-doc/sphinx) | 16,578 | 383 | 219 | 38 (17%) | 84 |
| [scrapy](https://github.com/scrapy/scrapy) | 9,222 | 303 | 156 | 23 (15%) | 96 |
| [django](https://github.com/django/django) | 6,000 | 106 | 47 | 3 (6%) | 23 |

mypy is a type checker — an application whose modules call each other
constantly — and it yields the highest usable rate of the four. sphinx and
scrapy are mined to the root commit, which is why their case counts are large
relative to the window: the first pass took only the most recent 6,000 commits
and left them resting on eight scored cases each. (Merge commits are excluded,
so the walked counts are below each repository's raw total.)

**django was added to test the rule above, and broke it.** The prediction was
that a large framework would behave like mypy. It is the biggest repository of
the four — 2,925 modules against mypy's 441 — and yields the *lowest* usable
share of any corpus mined, click included. Size is not the predictor, and
neither is application-versus-library. What predicts a usable case is whether
the repository *contains* the callers, and django's callers are the projects
that install it. It is shaped like click, several hundred times over.

Adding it was still worth the clone, for a reason that had nothing to do with
django: half of what it mined was fabricated by a bug in the miner, and finding
that [moved every number on this page](#a-getter-and-a-setter-are-not-a-change).

The last column explains most of the gap, and it is a property of file-level
evaluation rather than of either tool. A private helper's callers live in its
own file; the defining file is excluded from ground truth because every tool
gets it right. So a signature change to a `_name` usually has an *empty*
cross-file blast radius, and cannot be scored at all:

| | private forcing cases | of those, empty ground truth |
| --- | ---: | ---: |
| scrapy | 96 of 156 (62%) | 85 (89%) |
| django | 23 of 47 (49%) | 23 (**100%**) |
| sphinx | 84 of 219 (38%) | 71 (85%) |
| mypy | 35 of 165 (21%) | 22 (63%) |

That is the mechanism behind the usable-case share, not a separate fact about
it: scrapy spends 62% of its forcing cases on private helpers and almost none of
those can be measured, while mypy spends 21% and keeps a third of even those.
django is the limiting case — every one of its private forcing changes has an
empty cross-file ground truth, and a further third of its remaining empties are
absorbed by its test suite, the highest of the four. No amount of resolution
accuracy moves this, which is worth knowing before reading a low usable share as
a verdict on the tool.

Exercised against the Python 3.14 standard library — 719 modules, 19,892
definitions, 4,293 module-level import bindings, zero parse failures — because
fixtures do not contain the code that breaks a parser.

## Using it

```
blast impact hybrid_search --root path/to/repo
blast impact Config.read   --root path/to/repo --argument confdir
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

## Using it from an agent

The CLI answers one question and exits, which throws the index away every time.
The MCP server keeps it in memory, which is the only way the reuse above is
reachable: repeated questions between edits cost a pass of hashing rather than a
rebuild.

```bash
pip install -e ".[mcp]"          # installs the `blast-mcp` entry point
claude mcp add blast-radius -- blast-mcp --root .
claude mcp list                  # blast-radius: ✔ Connected
```

Registering at the default `local` scope keeps the server in your own Claude
Code config rather than writing a `.mcp.json` into the repository. Servers are
loaded when a session starts, so an already-running session will not see the
tools until it is restarted.

| Tool | Call it when |
| --- | --- |
| `blast_impact` | **Before** editing, renaming, or deleting a function or method signature |
| `blast_refs` | Reading the call sites once you know which files are affected |
| `blast_find` | A bare name came back ambiguous and you need the qualified form |
| `blast_stats` | The answers look thin — shows unindexed files and unresolved receivers |

`blast_impact` takes the same `argument` narrowing as the CLI. Pointed at this
repository, an agent asking about `build_index` gets:

| question | files | call sites |
| --- | ---: | ---: |
| what depends on `build_index`? | 6 | 68 |
| what breaks if I remove its `previous` parameter? | **2** | **16** |

Four of the six files call `build_index` without ever passing `previous`, so
that edit cannot break them — and the agent never opens them.

Two things are deliberate. **Ambiguity is refused, not guessed** — a bare
`render` matching three classes returns the candidates and an instruction to
re-call with `path.py::qualname`, because picking one is how a tool answers
confidently about the wrong function and an agent has no way to notice. And
**every result carries its own caveats**: the string-reference blind spot
always, plus the count of unresolved receivers and unparseable files when there
are any. An agent that treats a blast radius as complete will rename a symbol
and break the callers this tool never resolved; saying so costs a line of JSON.

## What resolves today

Reference coverage, not impact accuracy; the precision/recall table below is the
number that actually matters. Measured over the Python 3.14 standard library
(719 modules, 19,892 definitions, zero parse failures) and over mypy at HEAD
(441 modules).

Two corpora rather than one, because the difference between them is the finding:

| Strategy | stdlib | share | mypy | share |
| --- | ---: | ---: | ---: | ---: |
| `name` — scope chain and imports | 15,883 | 44.6% | 34,940 | 65.5% |
| `self_attr` — the class hierarchy | 10,324 | 29.0% | 7,721 | 14.5% |
| `module_attr` — an imported module | 5,560 | 15.6% | 871 | 1.6% |
| `constructor` — `C(...)` reaching `C.__init__` | 3,150 | 8.8% | 5,380 | 10.1% |
| `class_attr` — a member of a resolved class | 624 | 1.8% | 293 | 0.5% |
| `typed_attr` — a receiver with a declared type | 60 | 0.2% | 4,109 | **7.7%** |
| **Total resolved** | **35,601** | | **53,314** | |

The last three rows were added because the evaluation demanded them, not because
they seemed like good ideas — see [what the evaluation
changed](#what-the-evaluation-changed). Without `constructor`, every `__init__`
in every repository reported zero callers.

`typed_attr` is 0.1% of the standard library and 6.5% of mypy. That 65-fold gap
is not noise: it is the difference between a codebase written before type hints
and one that is thoroughly annotated. What this strategy is worth depends
entirely on the target, and the modern code an agent is asked to edit looks like
mypy.

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

113 cases, all with non-empty cross-file ground truth:

| Corpus | Cases | precision | recall | F1 |
| --- | ---: | ---: | ---: | ---: |
| scrapy | 23 | 100% | 25% | 40% |
| sphinx | 38 | 83% | 64% | 72% |
| mypy | 49 | 81% | 83% | 82% |
| django | 3 | — | 0% | — |
| **all** | **113** | **83%** | **64%** | **72%** |
| `grep`, all | 113 | 24% | 100% | 38% |

django predicted nothing at all on its three scored cases, so it has no
precision to report and contributes only three misses. Two are worth naming
because they are the tool's two known gaps, one each:
`self.query.add_select_col(...)` is an unannotated receiver, and
`self.alter_db_table(...)` is a `self.` call that dispatches across the class
hierarchy into the subclass whose signature changed. Three cases is not a
measurement, but it is a consistent one.

### A getter and a setter are not a change

Adding django as a fourth corpus was meant to test whether the accuracy claims
generalise. It found a bug in the harness instead, and the bug was worth more
than the corpus.

`signature_changes` kept one signature per qualname. A property declares two:

```python
@property
def query(self):            # QuerySet.query -- (self)
    return self._query

@query.setter
def query(self, value):     # QuerySet.query -- (self, value)
    self._query = value
```

Keyed by qualname alone, the setter overwrote the getter. The comparison then
ran the *new* getter against the *old* setter and reported a gained required
parameter — in a file where both signatures were byte-identical to the parent
revision. `@overload` groups fail the same way.

These phantoms are worse than noise, because they are cases with **no true blast
radius at all**: nothing changed, so nothing had to move, and every file the
tool correctly named as a caller scored as a false positive. They were
concentrated in exactly the code that uses properties heavily:

| | forcing cases | phantoms |
| --- | ---: | ---: |
| django | 47 | **51%** |
| mypy | 165 | 16% |
| scrapy | 156 | 3% |
| sphinx | 219 | 1% |

Eight had reached the scored set, six of them in mypy — the corpus carrying the
headline. The fix drops a qualname that carries more than one signature rather
than guessing which arm to diff: a missed real change is a smaller error than an
invented one.

It also cost cases in the other direction, which is the part worth keeping in
mind. A commit carrying two signature changes is rejected as ambiguous, and
phantoms counted toward that quota — so real cases were being thrown away beside
the fake ones. mypy went from 248 mined cases to 300 on the same 6,000 commits.

| | precision | recall | F1 | scored cases |
| --- | ---: | ---: | ---: | ---: |
| before | 67% | 63% | 65% | 109 |
| after | **83%** | 64% | **72%** | 113 |

Sixteen points of precision were being paid to measure eight fabricated cases,
on symbols like `QuerySet.query` that a repository references everywhere. The
baseline moved too — `grep` went from 15% to 24% precision — which is the
signature of a corpus artifact rather than a change in the tool: nothing in
`blastradius/` was touched.

The era split is the clearest evidence that the old numbers were distorted
rather than merely pessimistic:

| | precision before | precision after |
| --- | ---: | ---: |
| commits before 2021 | 56% | **85%** |
| commits from 2021 on | 77% | 81% |

Precision does not depend on the age of the code and never did — the apparent
27-point gap was phantoms clustering in older commits. What the era genuinely
predicts is *recall*, and that survived the fix intact (46% against 84%). One
real effect, not two.

### Thickening the denominator moved the numbers down

sphinx and scrapy previously rested on **eight scored cases each**, drawn from
the most recent 6,000 commits. Mining both to the root commit took them to 38
and 23, and their scores fell hard — sphinx from 91% recall to 64%, scrapy from
60% to 25%. The earlier figures were not wrong so much as **unrepresentative**:
eight cases of recent, well-annotated code.

The cause is almost entirely the age of the code:

| | cases | precision | recall |
| --- | ---: | ---: | ---: |
| commits from 2021 on | 50 | 81% | **84%** |
| commits before 2021 | 63 | 85% | **46%** |

The split is not hand-computed. `Case.committed_at` records the committer date
so the runner can filter by era without re-mining — the same reasoning as
`commit_file_count`, which exists so the file cap can be re-tuned the same way.
`--since 2021` scores one side; the report prints both whenever the corpus
spans the boundary. Filtering at reporting time rather than while mining keeps
the choice visible and reversible, exactly like `forcing_only`.

Year by year the trend is monotone enough to be worth stating plainly: 12–30%
recall on 2009–2012 commits, 75–92% on 2019 onward. Two mechanisms, both
measured rather than assumed:

**The tool cannot parse Python 2 at all.** Checking out scrapy at a 2008 commit
gives 200 indexed modules and **30 unparseable ones** — `print` statements that
a Python 3 `ast` rejects. Those files are excluded from the index, so a caller
inside one is invisible. Of the 52 missed files across the whole run, **8 are
source Python 3 cannot parse — every one of them before 2021, none after.**
Excluding them lifts pre-2021 recall from 46% to 51%.

Those eight are no longer silent. A file the parser rejects that *mentions* the
symbol is now reported as `unverified` — see [what it will not
guess](#what-it-will-not-guess). That does not move the score, by design, but it
converts 15% of the misses from a hole into a stated one.

**The rest is the unannotated receiver.** `self.middleware.download(request)` in
2009 scrapy resolves to nothing because nothing declares what `self.middleware`
holds — type annotations did not exist in Python until 2015 and were not
commonplace for years after. That is the same mechanism behind `typed_attr`
being worth 7.7% on mypy and 0.2% on the standard library, seen from the other
end.

Neither is a reason to drop the old cases. They are in the corpus and in the
headline, because a tool that only works on code written after 2021 should have
to say so with a number rather than a caveat.

**A single case accounts for most of the remaining precision gap.**
`get_proper_types` renamed its parameter `it`; twelve files call it, and the
commit edited one. It carries 11 of the run's 19 false positives on its own.

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| all 113 cases | 83% | 64% | 72% |
| `get_proper_types` alone | 8% | 100% | 15% |
| the other 112 | **92%** | 63% | **75%** |

Every caller writes `get_proper_types(result.arg_types)` — passing the renamed
parameter *positionally*, where the rename cannot reach it. This was previously
described as one of six such symbols; four of the other five were
[phantoms](#a-getter-and-a-setter-are-not-a-change), including `IRBuilder.accept`,
which was quoted here as the clearest example and was in fact an `@overload`
group that never changed signature at all.

The case is left in the corpus. Dropping the cases that make your own numbers
look bad is how an evaluation stops meaning anything, so the 92% figure is
quoted beside the 83% rather than instead of it.

### Every false positive in the run, audited

Rather than keep asserting that precision is a lower bound, all 19
false-positive files were checked against the tree the tool was given, asking
one question: does this file contain a call site the edit actually breaks?

| | files | |
| --- | ---: | --- |
| a **rename**, passed positionally | 14 (74%) | a real call site the rename cannot reach |
| a real forced call site | 3 (16%) | correct, and scored wrong — see below |
| a **removal** of a parameter never passed | 1 (5%) | a real call site, unaffected |
| an aliased import | 1 (5%) | `from … import init as init_locale` |

**Every one of the 19 names a file that genuinely references the symbol.** None
is a resolution error — the tool did not invent a caller anywhere in the run.
What it cannot see is whether a *particular* edit reaches a *particular* call.

The dominant category is now sharp enough to name. Fourteen of nineteen are
renamed parameters whose callers pass positionally: `get_proper_types(x)`,
`builder.gen_import("builtins", 1)`, `read_literal(data, marker)`,
`wrap_displaymath(node.astext(), label, …)`. A rename breaks only the callers
that *write the name*, so every one of these is safe. That is visible in the
per-kind scores — renames are the worst category by a wide margin:

| kind | cases | precision | recall |
| --- | ---: | ---: | ---: |
| `added_required` | 54 | 94% | 67% |
| `removed` | 25 | 88% | 47% |
| `renamed` | 29 | **65%** | 65% |
| `reordered` | 5 | 100% | 100% |

Narrowing renames to keyword callers is the obvious fix and it is *already
documented as tried and reverted* — see [a rule that sounded right and was
not](#a-rule-that-sounded-right-and-was-not). The reason it failed has nothing
to do with the phantoms: `classify` cannot distinguish a pure rename from a
parameter *replacement*, and replacements do break positional callers. The
numbers on this page moved; that argument did not.

Three of the nineteen are correct predictions scored as wrong. They are
`inline_all_toctrees`, called across several lines in three sphinx builders:

```python
largetree = inline_all_toctrees(self, self.docnames, indexfile, tree,
                                colorfunc, [])          # the new argument lands here
```

The added argument goes on a *continuation* line, which never mentions the
symbol — so the miner's ground truth does not include the file, and a correct
prediction scores as wrong. That is the trade-off `Git.paths_mentioning`
documents in as many words, now priced: 3 of 19.

The last is `from sphinx.locale import _, init as init_locale` — a file that
imports the changed symbol under another name. The import is a real dependency;
whether the aliased call had to change is not something a file-level score can
say.

But this is not purely a measurement artifact, and it would be convenient to
pretend it is. **The tool answers "what calls this", when the question asked is
"what must change if I make *this* edit".** For a removed or renamed parameter
those differ, and an agent handed twelve files to read when one needs editing is
paying a real cost. That gap is what `--argument` closes — see
[below](#which-callers-a-change-actually-breaks) — and it is the first thing the
evaluation pointed at that was a *feature* rather than a bug.

Initialisers remain the best-scoring category — 34 cases at **97% precision and
74% recall**.

### What the fixes were worth

Every fix in [what the evaluation changed](#what-the-evaluation-changed) was
made *because* of a scored run rather than from reading the code. The first two,
measured on sphinx and scrapy:

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

### An initialiser's blast radius is where it is constructed

Scoring the second time round, with mypy added, put `IRBuilder.__init__` at the
top of the miss list: four missed files, no hits. All four turned out to use the
class only as an annotation —

```python
def __init__(self, builder: 'IRBuilder') -> None:
```

— which a change to the initialiser's signature does not oblige to change. The
cause was the *first* miner fix overshooting: because a caller writes
`Widget(...)` and never `__init__`, ground truth searches diffs for the class
name, and that also matches every import and every annotation of the class.

Ground truth for `__init__` now requires the call shape `Class(`. A subclass
declaration deliberately does not match, since `class Child(Base):` writes
`Base)` — the declaration itself need not change when the base initialiser does,
only the code that constructs it.

Operator dunders are dropped as subjects entirely, alongside test and nested
functions: a caller of `__getitem__` writes `obj[key]` and of `__eq__` writes
`a == b`, neither of which names anything a diff search can find, so any ground
truth mined for them would be assembled from unrelated mentions of the class.
`__init__` is the one dunder kept, because construction does write the name.

Initialisers went from the worst-scoring category to the best: **11 cases, 80%
precision, 100% recall, zero misses** — measured when the corpus held 104 cases.
They are still the best category on the current 113: 34 cases, 97% and 74%.

The overall effect is smaller than that suggests, and worth stating exactly.
Removing annotation matches took 5 files out of the missed column and 2 out of
the hit column, leaving false positives untouched at 29:

| | hits | false positives | misses | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| before | 54 | 29 | 32 | 65% | 63% |
| after | 52 | 29 | 27 | 64% | 66% |

So recall rose three points and precision fell one — the fix removed ground
truth the tool was wrong about more often than right about, which is what a
correct narrowing should do. It does not make the tool better; it stops the
harness from asking it the wrong question.

### Reading declared types, and not inferring any

The largest remaining miss category was a method called on a value whose type
the tool did not know. The obvious response is type inference, which is a large
and error-prone thing to build. Measuring first showed most of it does not need
any:

| | unresolved attribute calls | receiver has a declared type |
| --- | ---: | ---: |
| mypy | 16,083 | 6,295 (**39%**) |
| stdlib | 27,394 | 3,130 (11%) |

Split by where the declaration comes from, annotations alone account for 34.4%
of mypy's, while tracking `x = Foo()` adds only 2.2% more. So this reads
declarations and infers nothing. The distinction is the whole point:
`def render(w: Widget)` is a statement the author wrote down, the same class of
evidence as an import, whereas what a variable was last assigned stops being
true the moment it is reassigned — and buys almost nothing anyway.

Handled: parameter annotations, `x: Widget = ...`, string forward references,
annotations reached through a module attribute, inherited methods through the
MRO, and **`self.config: Config` declared once and read from every method** —
whether it is written in the class body or in `__init__`, which is where it
almost always is. That last form is the reason this walks method bodies at all:
the declaration is written in one method and used from all the others.

Refused, because resolving them would be guessing: `list[Widget]` and
`Widget | None` name a container and a union rather than the class, `*args:
Widget` binds a tuple, an inner binding without its own declaration shadows an
outer annotation, a nested class's `self` is its own rather than the enclosing
class's, and `self.config = build()` with no annotation says nothing about the
type. An attribute annotated on a *base* class is also not inherited: that
needs the declarations to live on the class graph rather than on the module
walk, and reading through an unresolved base would be a guess.

The scored effect is the sharpest illustration in this project of why one number
is not enough. Measured on the 59-case corpus this predated, so the figures are
not comparable with the current headline — the direction is the point:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| all 59 cases, before | 64% | 66% | 65% |
| all 59 cases, after | 47% | **77%** | 59% |
| the other 53, before | 88% | 69% | 78% |
| the other 53, after | **89%** | **76%** | **82%** |

On 53 of 59 cases it raises recall seven points *and* precision one. On the six
heavily-called symbols it finds many more genuine callers than the commit
touched, and the headline precision falls seventeen points as a result. (Four of
those six were later found to be
[phantoms](#a-getter-and-a-setter-are-not-a-change); the trade-off this table
shows was real, but its size was inflated by them.) Recall
is the more trustworthy half here — ground truth is files that certainly had to
change, a true subset of the dependencies — and it improved everywhere.

The `self.x: Widget` form landed later, and was worth more than the 1.7%
predicted for it. Keeping the declaration on the *class* rather than the method
that wrote it added **710 references on mypy** — `typed_attr` from 6.5% to 7.7%
of everything resolved — and removed 1,420 unresolved attributes, exactly twice
the gain, because resolving `self.config.read()` also stops counting the
`self.config` underneath it as unknown.

Scored, it is the only change in this project that moved **both halves in the
same direction** (on the 59-case corpus it was measured against):

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| before `self.x` | 61% | 75% | 67% |
| after | **62%** | **80%** | **70%** |

Recall rose five points on mypy's 43 cases specifically (74% → 81%), which is
where the annotated `self.x` receivers are. Everything else this project added
traded one half against the other; reading a declaration the author already
wrote is the one move that costs nothing.

### Which callers a change actually breaks

Every caller is a real dependency. Not every one is *work*. Removing
`can_borrow` breaks `f(x, can_borrow=True)` and leaves `f(x)` alone, and until
now the tool reported both.

```
blast impact Builder.accept --root .
  blast radius  2 files
    keyword.py
    plain.py

blast impact Builder.accept --root . --argument can_borrow
  blast radius of changing 'can_borrow'  1 file
    keyword.py
```

References now carry the shape of the call that produced them — how many
positional arguments, which keywords — which answers "was this parameter
supplied?" without storing the arguments. Two details decide whether it is
right rather than merely plausible:

**The receiver is folded into the positional count**, so it lines up with the
parameter list as written: `b.accept(1, True)` fills `accept(self, expr,
can_borrow)` three deep. An off-by-one here reports the *wrong* callers, which
is worse than reporting too many.

**Anything that cannot be lined up counts as affected** — `f(*args)`, and
members reached through their class, where a `classmethod` binds `cls` and a
plain method does not. Over-reporting a small category beats guessing at it.

Precision rose from 47% to 61% and F1 from 59% to 67% at a cost of two points
of recall — and recall came back, with interest, once declared attribute types
landed (below). Both files behind that cost were read
individually, and neither was over-narrowing: `format_str_tokenizer.py` calls
`builder.accept(x)` and was edited in that commit by an unrelated `call_c` →
`primitive_op` refactor, and `scrapy/shell.py` grew a `shells=` argument because
the commit added bpython support. Both changed for reasons other than the
signature edit.

#### A rule that sounded right and was not

A *pure* rename only breaks the call sites that name the parameter: `f(x, 1)`
does not mention `flag`, so renaming it cannot affect that call. Implementing
that reasoning cost **23 points of recall** — across sixteen renamed cases it
produced one hit and twenty-two misses.

The reason is that `classify` labels any one-out-one-in swap at the same arity a
rename, and in real commits that is usually a parameter being *replaced*:
`Spider.update_settings` "renamed" `self`, and `H2ConnectionPool.__init__`
"renamed" `crawler` to `settings` in a commit titled "Use settings instead of
crawler". Positional callers must change for those. The classifier cannot tell a
pure rename from a replacement, so the broader rule is the correct one, and the
failed attempt is written into `affected_files` so it does not get "fixed" back.

The corpus has since grown to 29 renamed cases and renames remain the
worst-scoring kind — 65% precision, carrying 14 of the run's 19 false
positives. That is a standing invitation to try this rule again, which is
exactly why the reason it failed is recorded here rather than the fact that it
did. Telling a rename from a replacement is the prerequisite, and nothing in
this project can do that yet.

Scored by change kind, which shows where the narrowing does and does not apply:

| kind | rule |
| --- | --- |
| `removed` | callers that pass the parameter, by keyword or by position |
| `renamed` | the same, deliberately — see below |
| `made_required` | the mirror image: callers *omitting* it |
| `reordered` | callers that reach the moved position **by counting arguments only** |
| `added_required` | every caller; there is nothing to narrow by |

The reorder rule is the one the audit changed, and it took two attempts.

It used to fall back to every caller, because a reorder has no single parameter
to blame. The miner can in fact name them — and from there two kinds of caller
are provably safe: one that passes by keyword, since order is irrelevant to it,
and one that never counts far enough along the argument list to reach the change.

The first attempt named only parameters that *swapped places* with each other,
and it did not fix the case that motivated it. `classify` calls three different
edits a reorder, and the real `gunzip` commit was the second of them:

```python
-def gunzip(data: bytes, max_size: int = 0) -> bytes:
+def gunzip(data: bytes, *, max_size: int = 0) -> bytes:
```

`max_size` swapped with nothing; it left the positional list entirely. The rule
now compares each parameter's positional index before and after, treating
*absent* as its own state, which covers all three shapes:

| edit | named | why |
| --- | --- | --- |
| `f(a, b)` → `f(b, a)` | `a`, `b` | both swapped |
| `f(d, m=0)` → `f(d, *, m=0)` | `m` | left the list, so a positional caller breaks |
| `f(a, *, b)` → `f(a, b)` | `b` | gained a slot; nobody was passing it positionally |

The third looks strange to name and is right: the index is read from the tree
the tool is given, where `b` is still keyword-only and therefore has no
position, so the rule correctly predicts that nobody breaks.

The narrowed kinds score worse than the un-narrowed ones, which looks damning
until you notice all six outlier cases are `removed` or `renamed`. That is the
same concentration as everywhere else in this evaluation: a handful of
heavily-called symbols, not a property of the rule.

## What a cache can and cannot buy

The plan recorded here for a while was "a content hash per file". Measuring it
first showed that plan was wrong.

Resolution walks the AST, so a cache that survives the process has to store and
reload the trees. Over 300 standard-library files:

| | time |
| --- | ---: |
| `ast.parse` from source | 2.09s |
| `pickle.loads` of the same trees | **3.46s** |

**Reloading a cached tree costs 1.65x what parsing the source again costs**, for
about 30MB of cache on the whole standard library. A process that exits cannot
win here however clever the cache is. The saving exists only where the trees
stay in memory, so the reuse lives in `build_index(root, previous=index)` rather
than on disk.

| | modules | cold | after an edit | nothing changed |
| --- | ---: | ---: | ---: | ---: |
| this repository | 26 | 0.37s | 0.30s | **0.18s** |
| mypy | 441 | 20.8s | 11.6s | **0.33s** |
| standard library | 719 | 28.5s | 15.9s | **0.49s** |

Two different wins, and the second is much larger than the first.

**After an edit**, reusing the parse of every untouched file removes 44% of the
work — not the 66% parsing occupies on a cold run, because with the file cache
warm, reading and parsing get cheaper relative to resolution.

**When nothing changed at all**, the previous index is not merely reusable, it
*is* the answer, and the whole rebuild collapses to one pass of hashing: 19s to
0.33s on mypy, **58x**. That case is worth optimising because it is the common
one — an agent asks several questions between edits, not one question per edit.
It is also the only part of this that is correct by construction rather than by
argument: if every file is byte-identical and none were added or removed, there
is nothing an index could compute differently.

**Only parsing is reused. Everything downstream is recomputed**, and that is a
deliberate choice rather than an unfinished one. Whether a module's references
can change is a question about the whole import graph: a re-export means an
edit two modules away alters what a name binds to. There is a test for exactly
that — `pkg/__init__.py` stops re-exporting `helper`, `app.py` is byte-identical
and its parse *is* reused, and the reference correctly disappears anyway.
Reporting stale callers is the one failure this tool cannot afford, so the next
2x has to wait for real dependency tracking.

The digest is over the file's bytes rather than its mtime, because a checkout, a
branch switch, and a formatter that rewrites a file identically all move the
mtime without changing what the parser would produce.

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
`index` came back from a function.

The second of those has since been *partly* closed, and by reading rather than
inferring: where the receiver carries a declared type, the call now resolves —
whether that is a parameter annotated `BM25Index`, a local `index: BM25Index`,
or `self.index: BM25Index` declared once in `__init__` and used from every
method. `index = build_index()` still does not, because that would mean trusting
a function's return annotation and then tracking the variable through
reassignment. This is why the strategy is worth 7.7% on mypy and 0.2% on the
standard library — the gap between those two numbers is how thoroughly the
codebase is annotated, not how good the resolver is.

The first — string references — cannot appear in this evaluation at all, because
ground truth is mined from files whose diff mentions the symbol, and
`mock.patch("app.agent.hybrid_search")` does mention it. It stays a known blind
spot rather than a measured one.

## What it will not guess

Everything above is resolved: a scope chain, an import binding, an MRO, or a
type the author declared. There is exactly one thing the tool reports without
proving, and it is kept in its own column for that reason.

A file the parser cannot read contributes nothing to the index, so a caller
inside it is invisible — and understating a blast radius is the failure this
tool exists to prevent. When such a file *mentions* the symbol, it is listed as
`unverified`:

```
blast radius  1 file
  good.py

unverified  1 file could not be parsed but mention this name
  legacy.py
```

Three constraints keep that from becoming a text search wearing a hat. It is
never folded into the blast radius, because a text match in a file nothing
resolved is not evidence. Only files already known to have failed parsing are
searched — never the repository at large, since text-matching what the parser
*could* read would reintroduce precisely the noise this tool exists to remove.
And an initialiser is searched by its class name, because a caller writes
`Widget(...)` and never `__init__`.

The MCP server returns it under its own key with its own caveat, so an agent
can tell *"I could not see this"* from *"nothing depends on this"* — which are
the same empty list otherwise.

## Scope, stated up front

- **Stage 1 answers one question**: direct callers and overrides. Not transitive
  importers, not test coverage, not public-API exposure.
- **Attribute calls on values of *undeclared* type are not resolvable** —
  `stream.close()`, where `stream` came out of a dict. Where the receiver's type
  is declared in an annotation the call now resolves; where it is not, the
  access is counted and never guessed at. This is still the largest source of
  recall misses, and on unannotated code it is nearly all of them.
- **"What calls this" and "what must change if I make this edit" are different
  questions**, and `--argument NAME` is needed to ask the second. Without it
  every caller is reported, which is correct but not always useful.
- **Python first.** C++ needs `compile_commands.json` and libclang; text-level
  parsing is not sufficient there, and demonstrating that gap is its own result.
- **The first index of a large repository is still slow**, and so is the
  rebuild after an edit: 29s cold and 16s after a change on the standard
  library. Repeat queries between edits are 0.5s
  ([why](#what-a-cache-can-and-cannot-buy)). Cutting the after-an-edit case needs
  import-graph dependency tracking, so that only modules that could have
  changed are re-resolved.

## Development

```bash
python -m venv venv && venv\Scripts\activate
pip install -e ".[dev]"

ruff check .
pytest --cov
```

Requires Python 3.10+.
