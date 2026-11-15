# Gate satisfiability and dead-body folding

Status: accepted (2026-11-15)

## Context

Structured gates (`when`, `once`, `whileLoop`) store their conditions as one
implicit conjunction. `opt` already fuses compatible `Deaths`/`DeathsX`
(condtype 15) comparisons, but it had no contradictory case: `when([deaths >= 5,
deaths <= 3], body)` survived optimization even though its body can never run.
The gate then cost a condition evaluation every pass and kept its body alive.

`HirCond::never()` is the existing false condition (condtype 23), and the LIR
simulator already treats condtype 22/23 as constant true/false. No new sentinel
or encoding is needed.

ADR 0027 splits a structured gate wider than 16 conditions into chunk gates at
the lowering seam. The optimizer must fold the **full** condition list before
that split: one logical gate is one conjunction, so a contradiction anywhere in
the list makes the whole gate false even when the contradictory pair lands in
different chunks.

## Decision

### 1. Comparison algebra (same target only)

`fuse_and_dedup_conditions` compares an incoming condition with a previously
kept condition through `fold_pair`. A pair folds only when every target field
matches exactly:

```text
condtype == 15 (Deaths/DeathsX)
player, unit, locid, eudx, relocs identical
```

Comparison codes are `0 = AtLeast`, `1 = AtMost`, `10 = Exactly`.

| Existing | Incoming | Condition | Result |
|---|---|---|---|
| AtLeast a | AtLeast b | any | AtLeast max(a,b) |
| AtMost a | AtMost b | any | AtMost min(a,b) |
| AtLeast a | AtMost b | a == b | Exactly a |
| AtLeast a | AtMost b | a > b | **Never** |
| AtLeast a | AtMost b | a < b | keep both (range) |
| AtMost a | AtLeast b | a == b | Exactly a |
| AtMost a | AtLeast b | a < b | **Never** |
| AtMost a | AtLeast b | a > b | keep both (range) |
| Exactly a | AtLeast b | a >= b | Exactly a (redundant) |
| Exactly a | AtLeast b | a < b | **Never** |
| Exactly a | AtMost b | a <= b | Exactly a |
| Exactly a | AtMost b | a > b | **Never** |
| Exactly a | Exactly b | a == b | duplicate (dropped) |
| Exactly a | Exactly b | a != b | **Never** |
| any | `Never` | derived below | **Never** (absorbing) |

The inverse `AtLeast`/`AtMost` + `Exactly` rows are symmetric: an exact value
below an `AtLeast` bound, or above an `AtMost` bound, is also `Never`. The
replacement keeps the existing condition's list position, so a satisfiable
list's output order (and therefore its bytes) does not move.

`Never` is the existing `HirCond::never()` (condtype 23), not a new sentinel.
When a comparison pair *derives* `Never`, it is absorbing: the whole condition
list collapses to `[Never]`. An already-`Never` single condition is an opaque
frontend constant (`condAny()`, a statically false field read) and is preserved
as written; this pass only rewrites a contradiction it derives from the
comparison pairs above. Folding never crosses different `player`, `unit`,
`locid`, `eudx`, or `relocs`; a masked `Var` comparison with different
relocations is a different target. Non-15 condtypes never fold here.

### 2. Gate handling

`settle_when` folds the full condition list before ADR 0027 chunking, then:

1. **`[Never]` derived by folding** — the body is dead. If the body declares a
   trigger id that the module-wide `TriggerRefs` names, the gate and body are
   kept exactly as written; the pass only records `GateDrop` with
   `after = "kept(referenced)"`. Otherwise the body is dropped and:
   - a non-empty `else_body` is optimized and emitted in place of the gate
     (`after = "else-promoted"`), as a constant `false` would select the else
     arm;
   - no else emits nothing (`after = "dropped"`).
2. **`[Always]` or empty** — the gate is unconditional. The body is emitted and
   an else arm is dropped (`GateOpen`, `after = "opened"`). A then-only
   `[Always]`/empty gate keeps the pre-existing late packing path so satisfiable
   fixtures stay byte-identical; the else arm is the only case that must be
   rewritten here.
3. **Otherwise** the gate survives with its folded conditions.

A dead gate must not recurse into its body first: optimizing the body can pack
leaf gates and mint trigger ids, which would shift later ids even though the
body is discarded. `settle_when` therefore settles the folded list before body
recursion, and only optimizes the arm it keeps.

`Until` (and the other temporal desugars) expand to `when`s before this pass,
so contradictions in their trip gate and body gate are folded normally. That is
intended: the desugared gates have no user-visible liveness beyond the latch
the desugar itself defines.

### 3. `once` and `while` exceptions

`HirStmt::Once` and `HirStmt::While` use the same comparison fusion with a mode
that suppresses `Never` formation. A pair that would be contradictory is left
as written (an explicit `Never` condition also stays); non-contradictory bounds
still fuse. Their bodies are never dropped by this pass.

Rationale:

- a `once` is a self-disabling liveness gate; silently deleting it changes when
  the latch write happens and can strand a chain;
- a `while` with an unsatisfiable condition is a deliberate spin-wait.
  `drop_dead_triggers` already keeps an empty `while` for exactly this reason;
  folding it to a dead node would be a behavior change, not an optimization.

### 4. Fold events

The pass emits through the `FoldLog` API from ADR 0030:

- `CondFold` for any changed condition list, including the `Never` result;
- `GateDrop` for a gate whose folded conditions are `[Never]`, with `before` =
  the original conditions and `after` = `dropped`, `else-promoted`, or
  `kept(referenced)`;
- `GateOpen` for an unconditional gate.

No new event kind is introduced.

### 5. Determinism and cost

- No variable or trigger ids are minted by the folding decision.
- Dropping a body does not change the rank order of vars: all vars were
  allocated by the frontend before opt.
- The pass is conservative: a `Never` whose body declares a referenced trigger
  keeps the gate and body exactly as written.

## Consequences

- The four contradictory comparison families fold to `Never` on one target and
  the dead gate disappears (or its else arm runs).
- Different player/unit/mask/`eudx`/relocation targets never fold.
- `once` and `while` conditions and bodies are untouched by contradictory
  folding.
- An already-`Never` frontend condition is preserved (it is not a derived
  contradiction), so `condAny()`/statically-false gates keep their existing
  lowering and cost goldens.
- Folding runs on the full list before ADR 0027 chunks it, so a contradiction
  in the first chunk, the second chunk, or spanning the boundary drops the whole
  gate.
- Py/TS parity is preserved by construction: the pass is Rust-only and both
  frontends feed the same HIR.
- Cost goldens may move only for programs that contain a contradiction. No
  checked-in satisfiable fixture contains one: `cond_chunking`, `e2e_parity`,
  `bgm_parity`, `noaircollision_parity`, and the `explain_folds` fixture keep
  their existing output. The new `gate_sat` fixture pins a satisfiable module's
  optimized JSON byte-for-byte.
- A `Never` gate that drops an empty-bodied body still goes through the normal
  post-schedule dead-gate pass; surviving trigger ids are never renumbered.

## Rejected alternatives

- **A new `Never` sentinel or HIR field.** Condtype 23 already exists and the
  simulator already folds it.
- **Folding per ADR 0027 chunk.** A contradiction spanning chunks would survive
  as one satisfiable chunk and one single-condition chunk, so the gate would
  not drop. The full list is one conjunction and must be folded as one.
- **Dropping contradictory `once`/`while` gates.** Their liveness and spin-wait
  behavior is user-visible; `drop_dead_triggers` deliberately keeps empty
  `while` loops.
- **Dropping a body that declares a referenced trigger.** The reference would
  dangle and lowering would reject the module.
