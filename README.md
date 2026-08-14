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
| `evaluation/schema.py` — the mined-case format | done |
| `impact.py` + `cli.py` | not started |
| `evaluation/mine.py`, `evaluation/run.py` | not started |

Exercised against the Python 3.14 standard library — 1,123 modules, 26,025
definitions, 11,648 import bindings, zero parse failures — because fixtures do
not contain the code that breaks a parser.

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

## Scope, stated up front

- **Stage 1 answers one question**: direct callers and overrides. Not transitive
  importers, not test coverage, not public-API exposure.
- **Attribute calls on values of unknown type are not resolvable** without type
  inference. Those are reported as name matches, behind a flag, never in the
  default output.
- **Python first.** C++ needs `compile_commands.json` and libclang; text-level
  parsing is not sufficient there, and demonstrating that gap is its own result.

## Development

```bash
python -m venv venv && venv\Scripts\activate
pip install -e ".[dev]"

ruff check .
pytest --cov
```

Requires Python 3.10+.
