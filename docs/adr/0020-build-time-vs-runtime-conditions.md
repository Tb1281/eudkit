# Build-time branching on a runtime condition is an error

Status: accepted (2026-11-10)

## Context

`when(conds, body)` executes `body` **once, at build time**; `body` builds HIR
nodes that the game walks later. Every comparison — `v.eq(1)`, `v == 1`, a
field read, `Never()` — returns a HIR condition record describing an in-game
test, or a deferred marker that materialises one. None of them returns a
Python `bool`. Python, however, calls `bool()` implicitly in `if`, `while`,
`assert`, a conditional expression, and on both operands of `and`/`or`/`not`.
Nothing in `py/` defined `__bool__`, so a plain `dict` (or an object) answered
`True` and the program silently picked a branch while `buildModule` ran.

Measured on the baseline before this change with
`build_module(...).to_json()`:

| form | measured | what it silently did |
|---|---|---|
| `if v.eq(1): hit.iadd(1) else: hit.iadd(2)` | builds; HIR byte-identical to the then-only module (369 == 369 chars) | `else` is dead code |
| `if v == 1:` (dunder) | builds; same 369-char then-only HIR | `else` is dead code |
| `if v.eq(1) and v.lt(5):` | builds; surviving cond is `lt` only | one condition dropped |
| `if v.eq(1) or v.lt(5):` | builds; surviving cond is `eq` only | one condition dropped |
| `if not v.eq(1):` | builds (84 chars) | body always runs |
| `hit.iadd(1 if v.eq(1) else 2)` | builds (369 chars) | always takes the then value |
| `assert v.eq(1)` | builds (66 chars) | assert never fires |
| `if v:` / `if CUnit(1000).hp:` | builds | always true |
| `if Expr:` | builds | always true |
| `if Never():` / `if Always():` | both build (351 chars each) | both truthy |
| `if CUnit(1000).hp.ne(5):` | builds | `bool(DeferredNe)` is `True` |
| `while v == 0: pass` | never returns (killed at 5 s) | build hang; an emitting body grows the module until OOM |
| `when(v.eq(1) == True, body)` | builds; HIR identical to an empty module | block dropped (`dict == True` is `False`) |

Every emitted cond goes through one chokepoint, `_build_cond` (`py/hir.py`);
the stock condition helpers and `MemCond` route through it. Two cond value
types are not produced there: `DeferredCond` (a `Var` comparison with a
variable RHS) and `DeferredNe` (`field.ne(k)` with a middle constant). Both
are reachable as a bare `if` operand.

eudplib has the same trap. Making it loud is an "or better" advantage: the
sanctioned spellings already express runtime branching, and they all still
work.

## Decision

### 1. A runtime condition is not a build-time value

Using a condition as a Python `bool` raises **`EUDSL-COND-TRUTHIFY`** at the
truthiness test — before any act is queued. The seven value/cond sites:

| type | file | raised as |
|---|---|---|
| `Expr` | `py/var.py` | runtime value |
| `DeferredCond` | `py/var.py` | runtime condition |
| `Var` | `py/var.py` | runtime value |
| `ReadonlyVar` | `py/var.py` | runtime value |
| `TriggerFieldRef` | `py/view.py` | runtime value |
| `MemFieldHandle` | `py/view.py` | runtime value |
| `DeferredNe` | `py/view.py` | runtime condition |

The message keeps the stable substrings `runtime condition` / `runtime value`
and the spelling pointer `when()`/`once()`/`while_loop()`; tests assert the
code plus one substring, never the whole sentence (ADR 0019 §1). Measured
cost of the guard: ~0.02 ms per form (200 iterations); `while v == 0` fails in
~0.01 ms instead of hanging.

### 2. `HirCond` subclasses `dict`

`_build_cond` returns `HirCond(cond)`, a `dict` subclass whose only change is
`__bool__`. The public dict API is unchanged: indexing (`cond["condtype"]`),
`json.dumps(cond)`, `dict(cond)`, `cond == dict(cond)`, and `assertEqual` on
whole HIR trees all still work. A wrapper class would be a breaking change for
every consumer that already passes plain cond dicts (`Trigger`,
`_normalize_conds`, the JSON serializer), with no benefit.

`MemViewInstance` and containers keep their truthiness: `if CUnit(1000):` is
not a condition test.

### 3. The sanctioned spellings, and their mapping

| write this instead of | Python `if`/`while` does | sanctioned spelling |
|---|---|---|
| `if c: A` | build-time branch | `when([c], A)` or `with when([c]):` |
| `if c: A else: B` | `else` is dead | `when([c], A, B)` or `with when([c]) as w: …; w.otherwise(B)` |
| `while c: body` | build hang | `while_loop([c], body)` |
| `a and b` | drops one cond | pass both: `when([a, b], body)` |
| `a or b` | drops one cond | two blocks: `when([a], body)`, `when([b], body)` |
| `not c`, `x if c else y`, `assert c` | silent | write the complement / move the choice into a `when` |

The runtime else arm is the false path of the gate trigger, not a synthesized
complement: it runs iff `NOT(all conds)`, so `when([a, b], A, B)` runs `B` when
`a` fails or `b` fails. `when(True, A, B)` and `when([], A, B)` never run `B`;
`when(False, A, B)` runs `B` unconditionally, while the two-arg
`when(False, body)` still drops the block. The then-only form is byte-
identical (the `else_body` key is omitted when empty), and only `when` gets an
else arm (`once` / `until` / `while_loop` / edge forms do not). The same table
lives in `py/README.md`.

### 4. Residuals, recorded rather than hidden

The guard is preventive for **truthiness**, not for **equality**. This
section records both the equality items now fixed and the residuals that
remain, each with its measured behaviour:

- **Equality dunders on `Var` / `Expr` / `TriggerFieldRef` (fixed).**
  `==` / `!=` now raise `EUDSL-TYPE` for `float`, `str`, and `bool`, matching
  `v.eq(1.5)`, `v < 1.5`, and `u.hp == 1.5`. `None` and unrelated objects
  still return `NotImplemented`, so `v == None` stays `False`; plain `int`,
  `Var`, `Expr`, and `ReadonlyVar` operands keep their current HIR.
  Convert-to-`DeferredCond` was rejected: it contradicts ADR 0019 §1, which
  already rejects `v.eq(1.5)`.
- **`cond == True` / `cond is None`.** A condition compared to `True` is
  `False` (`dict.__eq__`), so `when(v.eq(1) == True, body)` still drops the
  block; `when([cond == True], …)` is now rejected eagerly with `EUDSL-TYPE`
  (the bare form still drops the block and is pinned). `cond is None` is
  `False` and can gate a branch the same way. `HirCond.__eq__` is deliberately
  **not** overridden: doing so would break `assertEqual` on whole statement
  trees.
- **Hand-built or copied conds.** `when([{"condtype": 15, ...}], body)` and
  any copied/serialized cond (`dict(cond)`, `json.loads(cond_json)`) are
  plain `dict`s and stay unguarded. Copying a cond is an explicit opt-out of
  the type.
- **TypeScript is guarded statically, opt-in, not at build time.** JS has no
  truthiness hook, so no runtime guard is possible. `tsc --noEmit` already
  reports `if (v == 1)` / `if (v != 1.5)` as TS2367 (`VarImpl` and `number` have
  no overlap). The remaining outcomes — `if (v.eq(1))`, `if (u.hp)` and
  `if (u.hp == u.hp.initialValue)` — are caught by
  `ts/scripts/check-truthiness.ts`, a dependency-free TypeScript-parser walk
  wired as `npm run typecheck:truthiness` and included in `npm run typecheck`.
  The rule is syntactic, so it can false-positive on unrelated `.eq(...)` calls
  and can miss aliased view handles; it is opt-in and **cannot** run on the real
  build path (`crates/eudkit-cli/src/frontend.rs` uses
  `node --experimental-strip-types`, which erases types unchecked). Do not read
  it as a guarantee for `eudkit synth`.

## Consequences

- py programs that used `if`/`while`/`assert`/ternary/`and`/`or`/`not` on a
  runtime condition now fail at build time with the code and a pointer to the
  sanctioned spelling, instead of emitting a silently wrong module.
- No HIR moves: the sanctioned spellings are unchanged, so the py suite's
  existing assertions, the `crates/` + `tests/` fixtures, the cost gates, and
  `eudkit synth --compact` stay byte-identical (both frontends,
  `sha256 4d152c2bd9455c2c9f0d494f8bca1e2bed82b0db5fd08040c6089cc0fd3d502d`).
- The guard is one `__bool__` per value type plus `HirCond.__bool__`; there is
  no AST inspection and no per-comparison cost.

## Rejected alternatives

- **(a) A Python AST linter for `if`/`while` on conds.** Rejected: a compare
  rule false-positives on 36 % of legitimate comparisons and still misses
  `v == 1` (a dunder call the linter sees as an ordinary comparison); the
  `__bool__` hook is exact and runs at the site.
- **(b) Wrap conditions in a non-dict class.** Rejected: every consumer
  already passes plain cond dicts, so a wrapper either breaks `Trigger` /
  `_normalize_conds` / `json`, or has to expose the dict API anyway — which is
  what subclassing does without the migration.
- **(c) Override `HirCond.__eq__` to catch `cond == True`.** Rejected: it
  breaks `assertEqual` on whole HIR trees, which compare dicts recursively;
  the residual is recorded instead.
- **(d) Guard TS with a compiler-API lint.** Rejected: no JS truthiness hook
  exists, the three TS outcomes are different bugs, and a static rule needs
  its own measurement. The shipped opt-in parser checker
  (`ts/scripts/check-truthiness.ts`, `npm run typecheck:truthiness`) is that
  static lint and does not run on the real build path.
