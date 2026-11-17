# HIR nodes for until, edge, and counted forms (temporal promotion)

Status: accepted (2026-11-04; amended 2026-11-10, 2026-11-11)

> **Amendment (2026-11-11):** the 2026-11-10 amendment's body-first ordering is
> superseded. `until(True, …)` runs the body **zero** times, because `until` is
> `while(!cond)`. Opt now emits only the reset trip-when for an
> **unconditional** `until` gate (`0` conds or all-`Always`) and drops the body
> entirely; every gated `until` keeps the original trip-first order
> byte-for-byte. See the amendment sections below.

## Context

`until`, edge (`onRising` / `onFalling`), and the counted forms (`every` /
`after`) existed only as DSL desugar: both the TypeScript and Python mirrors
expanded them to `when` blocks plus ordinary `Var`s, duplicating the shapes and
hiding the semantics from Rust. Any opt or lower improvement first had to
reverse-engineer the desugared shape, and the forms could not be inspected,
tested, or optimised as single temporal ideas. `once` had already made the jump
to a native HIR node despite being desugarable, setting the precedent. The
temporal module is now in place (`opt/temporal.rs` plus `control.rs`
path-complete restore), so the promotion is unblocked.

## Decision

1. **One node per form.** `HirUntil { conds, body, latch_var }`,
   `HirEdge { direction, conds, body, curr_var, prev_var }`,
   `HirEvery { n, counter_var, body }`, `HirAfter { n, counter_var, body }`,
   with serde kinds `"until"`, `"edge"`, `"every"`, `"after"`.
   Edge carries a `direction` field (`rising` / `falling`); the polarities
   share all logic except the fire-gate pair.
2. **HIR 3.0 → 3.1, additive bump.** The version field is exact-checked, so
   old readers fail clean on 3.1 documents. The LIR wire schema is unchanged.
3. **Opt desugar, byte-identical.** `crates/eudkit-opt/src/temporal.rs`
   expands each node to exactly today's DSL shape: until to the 2-`when` latch
   shape, edge to reset act plus sample gate plus nested fire gate plus two
   `prev` updates, every to a counter increment plus a fire `when`, after to
   the 3-`when` arm/decrement/fire sequence. Phase-1 output is byte-identical
   to the DSL-desugar baseline (the cost-gate golden is unchanged).
4. **DSL-reserved node-carried state vars.** The DSL allocates each state var
   at node-emission time, in exactly the old allocation order, and carries it
   on the node as a required field. Opt desugar uses the id verbatim and never
   mints var ids. See "Rejected alternatives" for why opt-minting was
   rejected.
5. **Validation in both layers.** DSL validation is kept (build-time, fails
   fast: `every` / `after` require a positive integer `n`). HIR validation
   adds reference integrity for the node-carried ids (`UnknownVarId` on
   undeclared ids) and a new `HirError::NonPositiveCount` for `n == 0`
   (negative `n` already fails loud at serde parse, since `n: u32`).
6. **No lowering change.** `control.rs` is untouched. Desugared output is only
   `when` blocks, so path-complete restore is inherited, not re-proven. The
   lower dispatch arms for the four nodes stay as unreachable
   defense-in-depth (lower runs opt first, so no node reaches lowering
   undesugared).
7. **Nesting is structurally free.** Bodies are `Vec<HirStmt>`, so nodes nest
   inside `when` / `once` / `while` and inside each other at any depth. Desugar
   recurses into bodies (until: the expansion is optimized; edge / every /
   after: body first, then desugar), so flatten/fuse keeps working inside
   nested nodes. The context matrix, the full 4x4 node-pair matrix, a 3-deep
   case, and a flatten proof pin this.

## Consequences

- There is a single desugar home (opt), and temporal behaviour has Rust unit
  tests instead of living only in DSL tests.
- Phase-1 output is byte-identical, so no map, cost-gate, or golden change
  rides on this work (gated forms only; the unconditional `until` gate is the
  documented 2026-11-11 amendment exception: only the reset trip-when is
  emitted).
- The lower dispatch arms are dead code by construction today; they exist so
  a future hand-written-HIR path fails loud rather than silently dropping a
  node.
- Phase-2 native lowering in `control.rs` is explicitly deferred, gated on
  measurement: on average ≥ 2 triggers saved per edge site on realistic
  programs, or a correctness case opt desugar cannot express. If measurement
  later justifies it, it gets its own change with a fresh
  path-complete-restore proof per new gate shape.
- A repeat-N loop is explicitly deferred as a separate feature: its semantics
  (intra-frame N× vs N-frame countdown) are unsettled, and it is not a counted
  primitive.
- Glossary terms for this work (`temporal form`, `desugar`, `counted form`,
  `path-complete restore`, `HIR version`, plus desugar lines on the `until` /
  `edge` / `every` / `after` entries) live in `GLOSSARY.md`.

## Rejected alternatives

- **Opt-minted state vars via `next_id` plus appended `HirVarDecls` — rejected
  after proof, not preference.** `next_id` is the trigger-id namespace
  (optimize seeds it from the max trigger id and consumes it for packed
  trigger ids), so minted var ids would collide with var ids, not merely
  shift ranks. Opt has no channel to `module.vars`, and `analyze_storage`
  ranks vars in id order, so late ids sort last and shift ranks. Measured
  under minting: until lost a declaration (`total_vars` 236 → 235) with a
  13-item LIR shift, and counted showed a packing split (+2408 bytes) plus a
  dword rank shift. The corrected mechanism (decision 4) makes byte-identity
  hold by construction.
- **Status quo (no nodes).** Rejected: the Rust-invisibility gap persists, and
  every future temporal improvement still starts by reverse-engineering DSL
  desugar.
- **Native lowering now.** Rejected: it needs a restore re-proof plus
  lowering-time var minting, with no measurement showing it beats desugared
  whens. Deferred per the measurement gate above.
- **One generic temporal node with a kind field.** Rejected: one node per form
  matches the When/Once/While precedent, and Edge's direction field already
  shares the polarities' logic.
- **`When` with an edge field.** Rejected: it muddies `When` and saves nothing
  over a dedicated node.

## Amendment (2026-11-10, superseded)

> Superseded — see the 2026-11-11 amendment above; retained for audit trail only. Do not read the ordering below as current semantics.

The unconditional-gate deviation. Decision 3 said opt expands `until` to
exactly today's DSL shape, and a Consequences bullet claimed Phase-1 output is
byte-identical. That is true for every real gate, but it was wrong as a
description of the unconditional gate. For `conds.is_empty() || conds.iter().all(|c|
c.condtype == 22)` — `until(True, …)` and `until(Always(), …)` — `desugar_until`
emitted `[body_when, trip_when]` instead of `[trip_when, body_when]`, because
the trip is unconditional and fires in the same loop as the body gate. That
made the body run once, while gated `until` forms stayed exclusive.

`Always()` is reachable: it is condtype 22, not an empty cond list.
`ConstFixedArrayValue._cmp` (`py/array.py`, TS mirror) and masked-`Var`
folds return it, and `_normalize_conds` wraps it as `[Always]`. A narrow
`conds.is_empty()` predicate would miss those paths, so the predicate includes
all-`Always` lists.

Variant O (lower unconditional `until` to `once`) was measured and rejected.
It is cheaper only for flat transfer bodies and regresses the nested-`when`
body:

| shape | baseline | variant O |
|---|---|---|
| `until(True, d.set(s))` | 5T/2C/70A/12,048 B | 4T/0C/69A/9,640 B |
| mixed leaf+transfer | 5T/2C/71A/12,052 B | 4T/0C/70A/9,644 B |
| two transfers | 6T/2C/73A/14,460 B | 5T/0C/72A/12,052 B |
| nested `when` body | 2T/3C/2A/4,824 B | 3T/1C/3A/7,232 B (**+1T / +2,408 B**) |
| nested `once` body | 4T/2C/4A/9,640 B | 3T/0C/3A/7,232 B |
| `until(Always(), …)` | 5T/2C/70A | 4T/1C/69A/9,640 B |

O also needs six pin updates instead of one and leaves the DSL-reserved latch
id referenceless, breaking the "reserved id used verbatim" invariant for 0-cond
nodes. The 2026-11-11 amendment supersedes this ordering fix.

## Amendment (2026-11-11)

**Exclusive semantics, superseding the 2026-11-10 ordering fix.** The
2026-11-10 amendment made the unconditional body run exactly once; that is
rejected. `until(True, …)` must run the body **zero** times — `until` is
`while(!cond)`, exclusive. The reason is consistency: real gates are trip-first,
so the body never runs on the trip loop, which means `until(x.eq(5), …)` with
`x == 5` on loop 1 runs zero times while `until(True, …)` ran once. Same entry
situation, different outcome — the body-first order made unconditional gates
inclusive while gated gates stayed exclusive.

**What changed.** `desugar_until` now emits only the reset trip-when for an
unconditional gate and drops the body entirely. Trip-only (not trip-first)
matters: restoring the old `[trip_when, body_when]` order alone would still run
a flat leaf-act body once, because the 0-cond trip flattens to a leaf and packs
with the body-when into one `[reset, body_act]` trigger. With no body statement
emitted, no shape can run — leaf act, transfer, mixed, nested `when`, `Always()`
alike. The latch id is still used verbatim (trip cond + reset reference it), so
the reserved-id invariant holds, unlike variant O. Measured trip-only costs:
`until(True, d.set(s))` and the mixed / `Always()` shapes are
`1T/1C/1A/2,416 B`; the count / nested-`when` / leaf-act / CM shapes are
`1T/1C/1A/2,412 B`. `var_values` end with latch `0` in every shape (the trip
fires on loop 1).

**The 2026-11-10 measurements stand.** Its "measured bug" (zero execution for
non-leaf bodies under trip-first) was the correct behavior all along: it
matches the original decided spec's "`until(True)` shape (latch trips
immediately, body never runs)" parenthetical. Only the leaf-once was a packing
artifact. The `Always()` reachability note still applies: the unconditional
predicate still includes all-`Always` lists, now to drop the body rather than
to order it first.

**Variant O stays rejected — decisively.** Lowering an unconditional `until`
to `once` would run the body once, which contradicts exclusive semantics
outright (in addition to the nested-`when` regression and the
referenceless-latch problem recorded above). It is not to be re-proposed.

The pins are flipped on both frontends
(`py/tests/test_0cond_boolean.py`, `ts/tests/test_0cond_boolean.ts`) to
zero-run `var_values` plus the trip-only static cost; `executed_cost.triggers`
is not a valid pin because it reports the last loop and skips disabled
triggers.
