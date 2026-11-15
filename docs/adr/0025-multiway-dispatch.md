# Multi-way integer branching: `dispatch`

Status: accepted (2026-11-14)

## Context

Multi-way integer choices were hand-written `when` chains (`when(v.eq(0), …)`,
`when(v.eq(1), …)`, …) with no fallthrough and no shared fallback. Ticket #51's
runtime `else_body` arm (ADR 0020's branch surface, decided with `when`)
removed the first limitation, but authors still wrote the chain by hand.

Cycle 5 measured four strategies for the same one-of-N integer choice:

1. an else-chain of `when(v.eq(k), arm_k, next)` blocks;
2. the same chain with a flag-gated shared fallback;
3. a sparse balanced binary search tree of `HirWhen` nodes;
4. a dense jump table built from `Db` relocations.

Ticket #07 is the Phase B slice: expose the two existing-HIR strategies through
one frontend construct. No Rust change, no new HIR node, and no HIR version
bump are involved.

## Decision

### 1. Name: `dispatch`

The canonical name is `dispatch` in both frontends. `switch` was rejected
because it is a TypeScript reserved word and collides with StarCraft's
`Switch` / `SwitchSet` / `SwitchCleared` / `SwitchAction*` condition family.
`match` is legal in both languages but implies pattern matching, guards, and
bindings that this surface does not provide. ADR 0002 requires one canonical
vocabulary across the two DSLs, so the fallback is named `fallback`, not
`default` (also reserved in TypeScript/JavaScript).

### 2. Semantics

```python
dispatch(value, cases, fallback=None)
```

```ts
dispatch(value, cases, fallback?)
```

- `value` is a `Var`, `Expr`, `ReadonlyVar`, or a plain integer. A plain
  integer is folded at build time. An `Expr` is materialized exactly once into a
  fresh `Var`; a `Var`/`ReadonlyVar` is used directly.
- `cases` is a non-empty ordered list of `(key, body)` pairs. Keys are
  compile-time plain integers (a runtime `Var`/`Expr` key is `EUDSL-TYPE`,
  "runtime keys are not supported"), normalized to their unsigned 32-bit value
  and unique after normalization. `-1` and `0xFFFFFFFF` are the same key.
  `bool` keys are rejected: `True` is not `1`.
- Every arm is a zero-argument body builder. There is no fallthrough, `break`,
  binding, or guard.
- The first matching key wins (else-chain) / exactly one leaf is reachable
  (tree).
- `fallback` is an optional zero-argument body builder. A miss without a
  fallback is a documented no-op. A fallback-only call is not expressible; use
  `when([], fallback)`.
- Validation is eager, deterministic, and identical in both frontends:
  `EUDSL-SCOPE` outside `buildModule`, `EUDSL-TYPE` for operand/callable/shape/
  key-domain mistakes, `EUDSL-BOUNDS` for an empty case set or duplicate keys.

### 3. Lowering

- **Constant `value`:** only the matching arm, the fallback, or nothing is
  emitted, into the current parent scope. No `when` node is emitted, no gate is
  paid, and unselected bodies do not run at build time. This is a build-time
  branch, not a runtime one.
- **Runtime `value`, `1 <= N <= 9`:** an else-chain built from the last case
  backwards. The last case's node carries the fallback as `else_body`; each
  earlier case wraps the following node as its sole `else_body` statement. An
  empty fallback/else arm omits the `else_body` key, matching ticket #06's
  then-only byte identity.
- **Runtime `value`, `N >= 10`:** a balanced `HirWhen` tree. Internal nodes use
  `value.ge(mid)` (`AtLeast`); leaves use `value.eq(key)` (`Exactly`). The right
  subtree includes `mid`, the left excludes it. Bodies are captured in caller
  order before the tree is built, so variable ids and build-time side effects
  do not depend on the tree shape.
- **Fallback in the tree:** the same fallback statement list is attached to
  every leaf's `else_body`. Only one leaf is reachable, so it still runs at most
  once. This is the stateless HIR-only way to get one fallback without a match
  flag; the trade is `O(N × fallback size)` static bytes. A large fallback
  should prefer `N <= 9` or a manually hoisted flag/body. A flag-based single
  fallback is out of scope.
- No new HIR node, no dense jump table, and no Rust lowering change.

### 4. Cost model

ADR 0001 ranks `executed > static > bytes`. The pinned fixture is a page-backed
`Var` (the `1T / 64A / 2,408 B` seed) with arms `SetDeaths(9000 + k, SetTo, k,
0)`:

| N | lowering | static (T/C/A/B) | hit first | hit last | miss |
|---|---|---|---|---|---|
| 4 | else-chain | `8/4/74/19,264` | `2/1/3` | `4/4/1` | `4/4/0` |
| 8 | else-chain | `16/8/86/38,528` | `2/1/3` | `8/8/1` | `8/8/0` |
| 9 | else-chain | `18/9/89/43,344` | `2/1/3` | `9/9/1` | `9/9/0` |
| 10 | BST | `29/19/92/69,832` | `4/4/1` | `9/5/9` | `9/5/8` |
| 16 | BST | `47/31/110/113,176` | `5/5/1` | `9/5/9` | `9/5/8` |
| 32 | BST | `95/63/158/228,760` | `6/6/1` | `11/6/11` | `11/6/10` |

The else-chain costs `2N` static triggers (including the page seed); the tree
costs `3N - 1` static triggers including the seed (`3N - 2` pure tree). The
measured crossover is `N = 10`: at `N = 9` the else-chain wins the uniform
average (`5.89` vs `6.00` executed triggers) and the tree's worst case only ties
it at `9`. `N = 10` is the first size where the tree wins both the average
(`6.30` vs `6.40`) and the worst case (`9` vs `10`). The measured `N = 9..16`
sweep (same fixture, uniform hits over all keys) is:

| N | chain static | chain avg/worst | tree static | tree avg/worst |
|---|---|---|---|---|
| 9 | `18/9/89/43,344` | 5.89 / 9 | `26/17/89/62,608` | 6.00 / 9 |
| 10 | `20/10/92/48,160` | 6.40 / 10 | `29/19/92/69,832` | 6.30 / 9 |
| 11 | `22/11/95/52,976` | 6.91 / 11 | `32/21/95/77,056` | 6.55 / 9 |
| 12 | `24/12/98/57,792` | 7.42 / 12 | `35/23/98/84,280` | 6.67 / 9 |
| 13 | `26/13/101/62,608` | 7.92 / 13 | `38/25/101/91,504` | 6.85 / 9 |
| 14 | `28/14/104/67,424` | 8.43 / 14 | `41/27/104/98,728` | 6.93 / 9 |
| 15 | `30/15/107/72,240` | 8.93 / 15 | `44/29/107/105,952` | 7.00 / 9 |
| 16 | `32/16/110/77,056` | 9.44 / 16 | `47/31/110/113,176` | 7.00 / 9 |

The tree's higher static cost buys an average-case `O(log N)` executed path.
Its worst-case path is also `O(log N)` and beats the chain's `O(N)` from
`N = 10`; the advantage is material by `N ≈ 16` (`9` vs `16` executed triggers),
while at `N = 9` both worst cases are `9`. With a fallback duplicated at every
leaf, the `N = 10` tree is `49/19/122/117,992` static.

### 5. Dense jump table deferred (Q5)

A dense table is expressible with existing HIR (`Db` relocations,
`SetDeaths(trigger.next, Add, …)`, and `DeathsX(x.value, AtLeast, 1, 0, 1 << i)`
tests), but correctness depends on the engine's undocumented
`[prev][next][TRIGGER]` node layout and `lFlags` at `+2376`. The simulator
follows trigger indices and cannot validate skip nodes, and a stride-4 table is
limited to 593 entries. Defer it behind an ADR plus a real-engine test, then
opt in as `dispatch.dense` with a dense-span guard and a BST fallback. Do not
make it the default.

### 6. Consequences

- `dispatch` is the preferred multi-way surface. `when` chains remain the manual
  escape hatch for cold/static-only code and for shapes this surface does not
  cover (runtime keys, guards, fallthrough).
- The BST's duplicated fallback is a documented static-size trade.
- No Rust, HIR, lower, opt, or HIR-version change; existing then-only `when`
  HIR and the `if c: A else: B` surface are byte-identical.
- Both frontends emit byte-identical HIR for the parity matrix
  (N=1/4/8/9/10/16/32, fallback shapes, constant folds, sparse keys).

## Rejected alternatives

- **`switch` alias.** Reserved in TypeScript and collides with the StarCraft
  `Switch*` condition family.
- **`match` alias.** Implies pattern matching the surface does not provide.
- **Runtime keys.** A key is part of the search shape; runtime comparison is a
  different construct.
- **A flag-based single BST fallback.** Adds state and gates; duplicating the
  fallback at leaves is stateless and correct because one leaf is reachable.
- **Make the dense jump table the default.** Unvalidated engine layout; defer.
