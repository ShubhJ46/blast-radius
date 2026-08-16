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

Stage 1 is complete and measured. Scored against **59 signature-changing commits
mined from three real repositories**, with `grep` for the symbol name as the
baseline, because that is what an agent does today:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| **this tool** | **61%** | 75% | **67%** |
| `grep` | 13% | 100% | 23% |

`grep` finds everything and is wrong seven times out of eight.

That precision number is still dominated by six cases out of fifty-nine, where
a symbol called from a dozen files had a parameter change that forced only one
or two of those callers to move. Excluding those six, the same run is **95%
precision at 75% recall**. Both figures are in [results](#results), along with
why neither is cherry-picked.

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
A recall number needs corpora with deep internal call graphs — applications and
large frameworks, not focused libraries.

**That prediction held.** Three corpora, most recent 6,000 commits of each:

| Corpus | Cases | Forcing | Usable for recall | On a private `_name` |
| --- | ---: | ---: | ---: | ---: |
| [mypy](https://github.com/python/mypy) | 248 | 148 | **43 (29%)** | 27 |
| [scrapy](https://github.com/scrapy/scrapy) | 155 | 74 | 8 (11%) | 47 |
| [sphinx](https://github.com/sphinx-doc/sphinx) | 129 | 81 | 8 (10%) | 35 |

mypy is a type checker — an application whose modules call each other
constantly — and it yields nearly three times the usable rate of the two
libraries. That is the corpus shape a recall number needs, and it supplies 43 of
the 59 scored cases on its own.

The last column explains most of the gap, and it is a property of file-level
evaluation rather than of either tool. A private helper's callers live in its
own file; the defining file is excluded from ground truth because every tool
gets it right. So a signature change to a `_name` usually has an *empty*
cross-file blast radius, and cannot be scored at all:

| | private forcing cases | of those, empty ground truth |
| --- | ---: | ---: |
| scrapy | 47 of 74 (64%) | 45 (96%) |
| sphinx | 35 of 81 (43%) | 32 (91%) |
| mypy | 27 of 148 (18%) | 18 (67%) |

That is the mechanism behind the usable-case share, not a separate fact about
it: scrapy spends 64% of its forcing cases on private helpers and almost none of
those can be measured, while mypy spends 18% and keeps a third of even those.
No amount of resolution accuracy moves this, which is worth knowing before
reading a low usable share as a verdict on the tool.

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

59 cases, all with non-empty cross-file ground truth:

| Corpus | Cases | precision | recall | F1 |
| --- | ---: | ---: | ---: | ---: |
| sphinx | 8 | 100% | 91% | 95% |
| scrapy | 8 | 86% | 60% | 71% |
| mypy | 43 | 54% | 74% | 62% |
| **all** | **59** | **61%** | **75%** | **67%** |
| `grep`, all | 59 | 13% | 100% | 23% |

**Six cases account for the entire precision figure**, and they are all the same
shape: a symbol called from a dozen files, where the signature change forced
only one or two of those callers to move.

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| all 59 cases | 61% | 75% | 67% |
| the 6 heavily-called symbols | 12% | 71% | 21% |
| the other 53 | **95%** | **75%** | **84%** |

`IRBuilder.accept` is the clearest example. Four commits removed its `can_borrow`
parameter. Eleven files call it, and only the one or two that actually passed
`can_borrow=...` had to change; the rest call it as `builder.accept(expr)` and
were untouched. `--argument` cuts most of that — those six cases went from 61
false positives to 35 — but the rest are callers that pass the parameter and
still were not edited, because the commit only converted some of them.
`get_proper_types` is the same story with a renamed parameter.

The six are left in the corpus. Dropping the cases that make your own numbers
look bad is how an evaluation stops meaning anything, so the 95% figure is
quoted beside the 61% rather than instead of it.

But this is not purely a measurement artifact, and it would be convenient to
pretend it is. **The tool answers "what calls this", when the question asked is
"what must change if I make *this* edit".** For a removed or renamed parameter
those differ, and an agent handed eleven files to read when two need editing is
paying a real cost. That gap is what `--argument` closes — see
[below](#which-callers-a-change-actually-breaks) — and it is the first thing the
evaluation pointed at that was a *feature* rather than a bug.

Initialisers are the best-scoring category — 11 cases at **80% precision and
100% recall**, no misses.

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
precision, 100% recall, zero misses.**

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
is not enough:

| | precision | recall | F1 |
| --- | ---: | ---: | ---: |
| all 59 cases, before | 64% | 66% | 65% |
| all 59 cases, after | 47% | **77%** | 59% |
| the other 53, before | 88% | 69% | 78% |
| the other 53, after | **89%** | **76%** | **82%** |

On 53 of 59 cases it raises recall seven points *and* precision one. On the six
heavily-called symbols it finds many more genuine callers than the commit
touched, and the headline precision falls seventeen points as a result. Recall
is the more trustworthy half here — ground truth is files that certainly had to
change, a true subset of the dependencies — and it improved everywhere.

The `self.x: Widget` form landed later, and cost more than the 1.7% predicted
for it. Keeping the declaration on the *class* rather than the method that wrote
it added **710 references on mypy** — `typed_attr` from 6.5% to 7.7% of
everything resolved — and removed 1,420 unresolved attributes, exactly twice the
gain, because resolving `self.config.read()` also stops counting the `self.config`
underneath it as unknown.

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

Precision rose from 47% to 61% and F1 from 59% to 67%, the best so far, at a
cost of two points of recall. Both files behind that cost were read
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

Scored by change kind, which shows where the narrowing does and does not apply:

| kind | cases | precision | recall | |
| --- | ---: | ---: | ---: | --- |
| `added_required` | 24 | 100% | 73% | every caller, no narrowing possible |
| `reordered` | 4 | 71% | 100% | no one parameter to blame |
| `renamed` | 16 | 45% | 83% | narrowed |
| `removed` | 15 | 46% | 61% | narrowed |

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
