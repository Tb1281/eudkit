# Auto-chunking structured gates above 16 conditions

Status: accepted (2026-11-15)

## Context

A raw `Trigger` is capped at 16 conditions (`Trigger (16C, 64A)`, ADR 0001).
That cap is correct for the authored-trigger surface: a 17-condition
`HirTrigger` is rejected with `EUDSL-BOUNDS`, and authors who write one must
split it themselves.

The structured forms are different. `when`, `once`, and `whileLoop` (and the
`until` / `onRising` / `onFalling` nodes that opt desugars into them) describe
one logical gate. Before this decision a 17-condition structured gate lowered
to a single `LIR` trigger and failed with `Err(LirError::TooManyConditions(17))`
at `lir2payload`, even though the author had no hand-written
trigger to split. The optimizer cannot pack such a node into one trigger either:
step 4's packer only accepts `conds.len() <= COND_COUNT`, so the structured
node reached lowering intact.

The lowering seam needs to split the condition list itself while preserving the
existing gate/body/restore semantics. The `<= 16` shape is already the pinned
contract for every `when`/`once`/`while`; changing it would churn every existing
map.

## Decision

### 1. Chunk count

Lowering computes

```text
k = max(1, ceil(conds.len() / 16))
```

and emits `k` chunk gates, `g_0 .. g_(k-1)`, where `g_i` carries
`conds[i*16 .. (i+1)*16]`.

`k == 1` (including an empty condition list) takes the pre-existing
single-gate path verbatim. The chunked path is used only for `k >= 2`, so the
`<= 16` `LIR` shape and all compiled bytes are unchanged.

A raw `Trigger` above 16 conditions still fails with `EUDSL-BOUNDS`. Only the
structured control-flow lowering desugars; `Trigger (16C, 64A)` remains the
storage and packing unit.

### 2. The three invariants

The chunk chain must be indistinguishable from one all-conditions-AND gate on
every pass, including passes where the game walks a trigger whose `next` was
mutated by an earlier pass. Three invariants define correctness:

- **I1 — default at pass end.** At the end of every trigger-loop pass, every
  chunk gate's `next` equals its static default. A later frame must never start
  from a pointer left over by an earlier one.
- **I2 — own chunk only.** A gate mutates its own `next` only on a pass where
  its own chunk passed. A false chunk writes nothing, so it cannot make an
  earlier chunk look successful.
- **I3 — one restore path.** Every path that leaves a mutated gate behind
  passes through a restore target that resets all `k` gates in the same pass.
  It is not enough to restore the last gate: `g_0 .. g_(i-1)` were mutated while
  walking to the failing `g_i`.

Concretely:

- `g_0`'s static `next` is the bare fail target: the join for
  `when`-without-else, `once`, and `whileLoop`, or `else_begin` for
  `when`-with-else.
- For `i >= 1`, `g_i`'s static `next` is the failure restore (`S` for
  `when`-without-else and `once`, `R` for `when`-with-else and `whileLoop`).
- Each gate's one act is a `SetNextPtr` to the next gate, or to the body start
  from the last gate. Gate edges are explicit `ExecItem`, never
  `DefaultChain`, so a chunk cannot accidentally fall into the next physical
  trigger when its own condition is false.
- Every gate is `preserved: true, flags: 4`, except `once`'s **last** gate,
  which is the only self-disabling gate (`preserved: false, flags: 0`).

### 3. `S` versus `R + E`

There are two restore shapes because a failure can happen after the body has
already been skipped, while a success must not run the failure arm.

```text
when (no else):  g0 -> g1 -> ... -> g(k-1) -> body -> S -> join
                       \____________________________-> S
                 g0.next = join, gi.next = S

when + else:     g0 -> g1 -> ... -> g(k-1) -> body -> S -> join
                                     \-> R -> else_body -> join
                 g0.next = else_begin, gi.next = R

once:            g0 -> g1 -> ... -> g(k-1) -> body -> S -> join
                 g0.next = join, gi.next = S

whileLoop:       g0 -> g1 -> ... -> g(k-1) -> body -> E -> g0
                                     \-> R -> join
                 g0.next = join, gi.next = R
```

`S` in the no-else/`once` forms is both the success restore and the failure
target of `g_i` (`i >= 1`), so it must be a separate trigger: a failing later
gate jumps over the body directly to it. `S` in the `when`-with-else form and
`E` in `whileLoop` are only reached from a successful body, so they may ride on
the last body trigger when it is unconditional, preserved, `DefaultChain`, and
has room for all `k` acts. `R` always sits **before** the body; `S`/`E` always
sit **after** it.

Empty bodies are normal cases:

- `when`-with-else, empty then-body: `R` restores every gate to `else_begin`
  and falls into the else body. A separate `S` sits between `R` and the else
  body, restores every gate to `else_begin`, then exits to the join, so a
  successful pass skips the else arm.
- `once`, empty body: the last gate targets `S`, and `S` restores all gates
  before the join, keeping the following chain live once per pass.
- `whileLoop`, empty body: the layout is `R` then `E`; a successful chain
  enters `E -> g0`, while a later-chunk failure still exits through `R` to the
  join instead of spinning.

The restore list is one `SetNextPtr` act per gate. A single unconditional,
preserved restore trigger holds up to 64 acts. When `k > 64`, the list is split
across chained restore triggers using the same `chunks(ACT_COUNT)` split as
ordinary action runs; the first trigger is the failure target and the last
carries the exit target.

### 4. Cost model

The important property is the **1T all-false steady state**. Because `g_0`'s
static default is the fail target, a pass whose first chunk is false executes
exactly one gate trigger and stops, just like today's single gate. Later chunks
are not evaluated and no body trigger runs. A `when` with an else arm still
runs its else body; that is the form's semantics, not chunking overhead.

The table counts control triggers, excluding the user body/else body. `B` is
one restore block; for `k <= 64` that is one trigger, and in general
`B = ceil(k / 64)`. The success restore may be appended to a body trigger,
saving one trigger, so the `when`-else and `whileLoop` static counts are
upper bounds.

| Conditions `n` | `k` | `when` | `when`+else | `once` | `whileLoop` | all-false pass | all-true pass |
|---|---|---|---|---|---|---|---|
| `<= 16` | 1 | today's path | today's path | today's path | today's path | 1 gate | 1 gate + body + restore |
| 17-32 | 2 | 2 gates + 1 `S` | 2 gates + `R` + `S` | 2 gates + 1 `S` | 2 gates + `R` + `E` | 1 gate | 2 gates + body + restore |
| 33-48 | 3 | 3 gates + 1 `S` | 3 gates + `R` + `S` | 3 gates + 1 `S` | 3 gates + `R` + `E` | 1 gate | 3 gates + body + restore |
| `16(k-1)+1 .. 16k` | `k` | `k` gates + `B` | `k` gates + `2B` | `k` gates + `B` | `k` gates + `2B` | 1 gate | `k` gates + body + `B` |

Static cost is linear in `k`: one gate per 16 conditions plus one or two
restore blocks. Chunking never duplicates the body. The all-true pass pays the
`k - 1` extra gate evaluations that a single hypothetical 16C trigger would
not, which is unavoidable when the hardware cap is 16.

### 5. `once` after firing

Only the last chunk gate self-disables. Before the first complete pass, a
partial match (for example chunk 0 true, the last chunk false) restores and
leaves every gate live, so the `once` can still fire on a later full match.
After it fires, the disabled last gate is skipped before its conditions are
evaluated, but the earlier chunks still run and the success restore still
executes, so a fired `once` costs `k - 1` evaluated gate triggers plus the
restore.

The alternative, disabling every gate on success, would require either a
separate "fired" flag or making earlier gates self-disable on a partial match,
which would break the "fire on the first all-true pass" contract. We accept the
post-fire re-evaluation of the earlier chunks as the cheaper correct trade.
There is no `k`-bit state and no per-gate disable bit to allocate.

## Rejected alternatives

- **Rely on opt to nest/conjoin whens.** `opt`'s `flatten_nested_when` conjoins
  then-only nested whens only while the fused list is `<= 16`, and its step-4
  packer also refuses `> 16`. More importantly, a nested `when` chain is not
  equivalent for `once` and `whileLoop`: each nested node would acquire its own
  body/restore or self-disable behaviour instead of acting as one conjunction,
  and a failed inner chunk would have no path that restores the outer gate in
  the same pass. Lowering the structured node directly is the only place that
  owns the shared restore invariant.
- **A flag gate (`flag = AND(chunks)` then one body gate).** This needs `k`
  fresh variables or a counter, extra setup/reset triggers, and a second gate.
  It also loses short-circuiting: an all-false pass evaluates every chunk
  instead of stopping at `g_0`, breaking the 1T all-false property. The
  chunk-gate chain gets the same result with no allocated state.
- **Splitting the body per chunk or duplicating it.** Duplicates static bytes
  and turns one logical `once`/`while` into several independent firings.
- **Raising the raw `Trigger` limit.** The 16C/64A bound is the storage and
  packing contract (ADR 0001); structured forms desugar, authored triggers do
  not.
- **Appending `S` in the no-else/`once` forms.** A failing `g_i` jumps to `S`
  without running the body, so `S` cannot be hidden at the end of a body
  trigger. Only the `when`-else and `whileLoop` success restores may append.

## Deferred follow-up

Relaxing `opt`'s `flatten_nested_when` `<= COND_COUNT` guard is a separate
change. It could fuse a latch/nested gate into one longer condition list before
lowering, but it changes which nodes survive to `k == 1` and therefore risks the
pinned byte-identical `<= 16` shapes. It needs its own measurements and ADR
amendment; this decision works with the guard as-is.

## Consequences

- A structured gate with any number of conditions lowers and assembles: it is
  split into `ceil(n / 16)` gates with one shared body.
- `<= 16` structured forms are byte-identical to before, including `n == 0`.
- A raw `Trigger` above 16 conditions still reports `EUDSL-BOUNDS`
  (see `docs/diagnostics.md`).
- Failure and success both restore all chunk gates in the same pass, so stale
  pointers cannot make a later frame re-enter a body that no longer matches
  (I1-I3).
- `once` keeps one self-disable bit (the last gate); after firing, earlier
  chunks are re-evaluated but the body never runs again.
