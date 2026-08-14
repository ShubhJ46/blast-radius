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

Stage 1, in progress. Nothing is measured yet — this section will hold a
precision/recall table against real commits before it holds anything else.

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
| `evaluation/mine.py`, `evaluation/run.py` | not started |

Exercised against the Python 3.14 standard library — 1,123 modules, 26,025
definitions, 11,648 import bindings, zero parse failures — because fixtures do
not contain the code that breaks a parser.

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

Measured over the standard library. This is reference coverage, not impact
accuracy: the precision/recall table against real commits comes later and is
the number that actually matters.

| Strategy | References | Share |
| --- | ---: | ---: |
| `name` — scope chain and imports | 26,987 | 57.6% |
| `self_attr` — the class hierarchy | 12,259 | 26.2% |
| `module_attr` — an imported module | 7,610 | 16.2% |
| **Total resolved** | **46,856** | |

Adding class-hierarchy resolution took unresolved `self.`/`cls.` calls from
**11,589 down to 1,189** — an 89.7% reduction, against a predicted 88.9%. It
beat the prediction because the forecast counted only *called* attributes, while
the implementation also resolves a bound method passed as a value
(`handler = self.helper`), which is a reference too.

What remains unresolved is 35,716 method calls on values whose type is unknown
(`result.close()`, `logger.warning()`). Recovering those needs type inference
and is out of scope; the 1,189 remaining `self.` calls are ones whose class
inherits from outside the repository.

The same measurement on a small, mostly-functional codebase found *zero*
`self.` calls to resolve, which is the more useful version of the finding: what
class-hierarchy resolution is worth depends entirely on how object-oriented the
target codebase is.

## The class hierarchy

Also measured over the standard library — 3,735 classes, graph built in 0.14s:

| | Count |
| --- | ---: |
| Classes with at least one base resolved in-repo | 2,287 |
| Classes with at least one base *outside* the repo | 775 |

That second row is the honest caveat. A class deriving from `abc.ABC` or a C
extension type has a real parent this tool cannot see, so every linearisation
it computes is a projection of the true MRO onto indexed classes. Deepest chain
found: 11.

## How it will be measured

Ground truth comes from git, so it costs nothing and involves no labelling.
When a developer changed a function's signature, the other files that same
commit touched are the blast radius they actually had to absorb. The harness
mines those commits, runs the tool against the parent tree, and compares.

The baseline is `grep` for the bare symbol name, because that is what an agent
does today.

Three filters decide whether the resulting numbers mean anything, and they are
documented in `evaluation/schema.py`: only signature changes that *force* a
caller to change are scored, commits touching many files are capped, and test
files are reported in a separate column because stage 1 does not attempt
coverage mapping.

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
the honest shape of the 43% of call-shaped references that stay unresolved.

Both are exactly the categories the commit-mined evaluation is expected to
surface as recall misses, which is the point of building that harness next
rather than trusting the numbers above.

## Scope, stated up front

- **Stage 1 answers one question**: direct callers and overrides. Not transitive
  importers, not test coverage, not public-API exposure.
- **Attribute calls on values of unknown type are not resolvable** without type
  inference. Those are reported as name matches, behind a flag, never in the
  default output.
- **Python first.** C++ needs `compile_commands.json` and libclang; text-level
  parsing is not sufficient there, and demonstrating that gap is its own result.
- **No caching yet.** The index is rebuilt per invocation: 0.1s on a 41-module
  project, 7.3s on 719 modules. Fine for a person, too slow for an agent calling
  it in a loop, and the fix is a content hash per file rather than anything
  structural.

## Development

```bash
python -m venv venv && venv\Scripts\activate
pip install -e ".[dev]"

ruff check .
pytest --cov
```

Requires Python 3.10+.
