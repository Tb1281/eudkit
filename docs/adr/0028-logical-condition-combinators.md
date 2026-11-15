# Logical condition combinators: `condAll`, `condAny`, and `condNot`

Status: accepted (2026-11-15)

## Context

Conditions are the only boolean surface the DSL has. A raw `Trigger` stores up
to 16 independent condition entries (ADR 0001), and structured gates
(`when`, `once`, `until`, `whileLoop`, `onRising`, `onFalling`) take a list of
them. Those entries are implicitly conjoined: `when([a, b], body)` runs `body`
iff `a` AND `b`.

That leaves two shapes with no surface at all. A disjunction has to be written
as an `else` chain or as an explicit flag variable maintained by hand, and a
negation has to be spelled as the complementary leaf (`ne` for `eq`) or as a
manual flag. Both hand-written forms are easy to get wrong: the flag reset can
be hoisted out of a loop, the `else` arm can be run during plan construction,
and `not`/`!` on a condition is a build-time truthiness error, not a runtime
negation (ADR 0020).

The lowering layer already knows how to split more than 16 conditions into
chunk gates (ADR 0027) and already exposes leaf complements
(`complement()` on `DeferredNe`/field reads). The missing piece is a
build-time composition API that produces ordinary conditions, so combinators
compose with the existing gate, chunking, and diagnostics machinery instead of
introducing a parallel runtime representation.

## Decision

### 1. API and type rules

Three combinators, exported under both spellings:

| TypeScript | Python | Arity | Meaning |
|---|---|---|---|
| `condAll(...conds)` | `cond_all(*conds)` | varargs, `>= 0` | conjunction |
| `condAny(...conds)` | `cond_any(*conds)` | varargs, `>= 0` | disjunction |
| `condNot(cond)` | `cond_not(cond)` | exactly 1 | negation |

All three return a `CondLike` in TypeScript and a runtime condition in Python,
so they can be passed wherever a condition is accepted: gate condition lists,
raw `Trigger` `conds`, and other combinators.

Operands must be runtime conditions. A build-time `bool` (`condAll(true)`), a
number, a string, `None`/`null`/`undefined`, or a value handle is rejected with
`EUDSL-TYPE`; the frontends never silently truthify an operand. `condNot`
requires exactly one operand: `condNot()` and `condNot(a, b)` are `EUDSL-TYPE`.
The varargs combinators accept zero operands (see the identity rules below).

`condAll` is the only combinator that splices into the enclosing condition
list. `condAny` and `condNot` return one condition entry backed by a gate, so
they never widen the enclosing gate's condition count.

### 2. Identity rules

The identities are build-time rewrites; none of them emits a trigger.

- `condAll()` is `true` and emits `0C`. An empty condition list already means
  "always" to every gate, so an all-empty conjunction disappears.
- `condAny()` is `Never()` (`1C`). An empty disjunction is false, and `Never()`
  is the existing false condition.
- `condNot(condNot(x))` is `x`. Negation is pushed through pairs at build
  time, so the double-negation wrapper never reaches lowering.
- One-operand forms are identities: `condAll(x)` and `condAny(x)` both reduce
  to `x` (subject to the same normalization as their `N`-operand forms).
- Same-kind nesting is flattened before planning:
  `condAll(a, condAll(b, c))` becomes `condAll(a, b, c)` and likewise for
  `condAny`. This keeps the De Morgan rewrites below finite.

### 3. De Morgan and the `complement()` leaf optimization

`condNot` normalizes in this order:

1. Strip double negation (`condNot(condNot(x)) -> x`).
2. Push negation inward with De Morgan:
   - `condNot(condAll(a, b, ...)) -> condAny(condNot(a), condNot(b), ...)`
   - `condNot(condAny(a, b, ...)) -> condAll(condNot(a), condNot(b), ...)`
3. Negate each leaf with its `complement()` rewrite when one exists.
4. Fall back to a flag gate for a leaf with no complement.

Step 3 is the zero-trigger path: a leaf complement rewrites the condition in
place, so `condNot(field.ne(k))` and `field.eq(k)` produce the same condition
entry (`1C`, no extra trigger). A leaf is complementable when it is a direct
comparison against a constant or a `DeferredCond`/`DeferredNe` marker.

The asymmetry is deliberate and pinned. `DeferredNe.complement()` returns
`.eq(k)`, which is a first-class condition (`1C`, `0T`). The reverse is not
true: the `.eq(k)` condition has no direct "not equal" entry in the condition
encoding, so `condNot(v.eq(k))` takes the flag-gate fallback. The optimizer
must not pretend the two directions are symmetric; a future leaf table that
adds a direct `ne` entry can retire the fallback without changing the surface.

A flag-gate fallback allocates one scratch flag, emits a setup that stores the
inner truth value, and gates on the stored flag. It is correct for every
condition, including `Memory`/`Deaths` conditions with no complement, and it
is the only path that costs a trigger.

### 4. `GatePlan` / `PlanContext` unification

Both frontends now plan gates through one seam instead of ad-hoc markers:

- `ts/conditions.ts` exports the `GatePlan` union (`lit`, `and`, `or`,
  `not_opaque`), `PlanContext`, `PlanHooks`, and the shared
  `makePlanHooks()` helper; `py/conditions.py` mirrors the same tree and
  context. `PlanContext`/`PlanHooks` are an internal seam: the Python module
  does not list `PlanContext` in `__all__`, and the public surface stays the
  three combinator names above.
- `GatePlan` is a boolean tree, not a list of already-emitted triggers. A `lit`
  is a resolved condition, `and` splices its children into the caller's cond
  list, and `or` / `not_opaque` own a flag plus the prelude that fills it. This
  is what makes `condAny` and `condNot` compose at any depth: a nested gate is
  emitted while its parent branch is being filled.
- `PlanContext` carries the hooks for the consuming construct (builder check,
  flag allocation, reset trigger, set-when) and maps each plan object to
  exactly one scratch flag. It also carries the cycle guard so a malformed or
  self-referential plan fails with `EUDSL-TYPE` instead of recursing forever.
- The old `gateTerms` / `gate_terms` duck-typed markers on
  `DeferredNe`/`DeferredCond` are removed. `ts/temporal.ts` and
  `py/temporal.py` ask the planner for a `GatePlan` instead of probing a
  condition object for a `gateTerms` method. This deletes the two divergent
  marker protocols (ADR 0026) rather than adding a third.
- The TS `CompositeCond` interface in `ts/trigger.ts` replaces the throwing
  `materialize()` method on composites: composites expose `gatePlan()` and
  `complement()`, while `CondLike` remains the union of HIR leaves,
  materializable markers, and composites. `DeferredNe` registers its detached
  raw-`Trigger` fallback with the planner, so a detached raw trigger keeps its
  historical `EUDSL-SCOPE` error instead of an implementation-specific one.
- `whileLoop` builds its plan once and shares the resulting `GatePlan` between
  the loop entry and the tail re-entry. The entry and tail therefore reuse the
  same flag cell and the same clause triggers; the condition list is not
  planned twice, so a combinator inside a `whileLoop` cannot double-emit its
  prelude. Its emitter materializes top-level literal leaves before any gate
  prelude, which preserves the pre-ADR act order for mixed lists such as
  `[DeferredNe, DeferredCond]` while keeping the caller's condition order.
- Raw `Trigger` counts planned condition slots with `plannedCondCount()` /
  `plan_cond_count()` before emitting any prelude, so a `condAll` splice cannot
  allocate a flag and then fail the 16C cap, and a failing later item cannot
  leave a half-emitted prelude behind.

### 5. Raw `Trigger` parity

Combinators work in a raw `Trigger`'s `conds`, because a raw trigger accepts
the same condition entries as a gate:

- `condAll` splices its entries into the array. The 16-condition storage cap
  applies to the **expanded** list: `Trigger({ conds: [condAll(...17...)] })`
  reports `EUDSL-BOUNDS`, exactly as if the 17 entries had been written out.
  A `condAll` that brings the list to `<= 16` is free. The raw-trigger path
  counts the planned slots before it emits anything, so an over-cap splice
  never allocates a flag or leaves a prelude behind.
- `condAny` and `condNot` with an opaque leaf cannot be represented as a
  condition entry, so they emit their prelude (flag setup and clause triggers)
  into the active module builder and return one flag condition for the raw
  trigger. The prelude is ordered before the trigger that consumes the flag.
- If no builder is active when the prelude would be emitted, the call throws
  `EUDSL-SCOPE`. In particular,
  `Trigger({ conds: [condAny(a, b)] })` constructed outside `buildModule`
  cannot allocate its flag and is rejected instead of producing a dangling
  condition. `condAll` of already-described conditions has no prelude and is
  not affected.

The editor is lexical and cannot expand `condAll` or see a combinator's
prelude. `trigger_capacity_diagnostics` in `crates/eudkit-lsp` counts the
literal entries inside a `Trigger` array, so a `condAll(...)` counts as one
entry and an over-16 splice is under-reported by the LSP; the compiler's
pre-count is authoritative and still reports `EUDSL-BOUNDS`. The editor must
not guess in the other direction either: structured forms are never subject
to the 16-condition cap, so no `EUDSL-BOUNDS` is emitted for
`when`/`once`/`until`/`whileLoop`/`onRising`/`onFalling` at any length
(ADR 0027). In particular, a raw `Trigger` with one `condAll(...17...)` entry
stays diagnostic-free in the editor (its literal list has length one) and is
still rejected by the build. This gap is documented rather than papered over:
a real expansion check needs the frontend's condition model, which the LSP
deliberately does not embed.

### 6. Cost model and composition with ADR 0027

`N` is the number of operands. Costs count control triggers, not the user body.
"Extra triggers" excludes the consuming gate; the tests pin the absolute total
for a bare `when` consumer.

| Form | Extra triggers | Consuming conds | Notes |
|---|---|---|---|
| `condAll(a, b, ...)` | `0T` | `N` | spliced; no scratch state |
| `condAll()` | `0T` | `0` | `0C`, gate is unconditional |
| `condAny(a, b, ...)`, `N >= 2` | `N+1T` | `1` | one preserved reset trigger, `N` clause `when`s, one flag gate (`N+2T` including the consumer) |
| `condAny(x)`, `N = 1` | `0T` | `1` | identity `x`; no flag or prelude |
| `condAny()` | `0T` | `1` | lowers to `Never()` (`1C`) |
| `condNot(leaf)` with `complement()` | `0T` | `1` | rewritten in place |
| `condNot(opaque)` | `1T` | `1` | preserved reset + one set-when + the gate (`2T` including the consumer); one scratch flag |

The `condAny` reset is its own preserved empty-condition trigger, not folded
into the first clause trigger, so a full miss costs `N+2T` (one reset, `N`
clause evaluations, one gate) both statically and at runtime. The flag is one
bit; the combinator does not allocate a counter or an array. Because
`condAny(x)` is the identity `x`, wrapping a single condition adds nothing;
same-kind flattening keeps `condAny(condAny(a, b), c)` on one flag at the
`N = 3` cost.

Composition with ADR 0027 chunking:

- `condAll` is transparent to chunking. Lowering sees the flattened condition
  list, so `when([condAll(...20 conditions...)], body)` chunks into
  `ceil(20 / 16) = 2` gates exactly like a hand-written 20-condition list.
- `condAny` and `condNot` expose one flag condition to the enclosing gate, so
  they never push that gate above 16C. Their own clause/flag triggers are
  ordinary structured gates and are chunked independently.
- A raw `Trigger` is still capped after expansion, so the ADR 0027 guarantee
  ("structured forms chunk, raw triggers fail") is unchanged.

## Rejected alternatives

- **A runtime `not`/`or` HIR node.** A new node would have to be taught to
  every pass, the optimizer, and lowering, and it would not compose with the
  existing 16C chunker. Build-time normalization to existing conditions keeps
  the runtime surface unchanged.
- **Truthifying conditions with language `!`/`not`.** This is the
  `EUDSL-COND-TRUTHIFY` defect (ADR 0020), not a negation operator. It would
  silently pick a branch while the module builds.
- **Emitting a flag gate for `condNot` even when a leaf complement exists.**
  That trades a `0T` rewrite for a scratch flag and a trigger, and it makes
  `condNot(field.ne(k))` differ from `field.eq(k)` in generated bytes.
- **Splicing `condAny` into the enclosing gate as `N` conditions.** Appending
  the clauses would conjoin them, not disjoin them. The disjunction needs the
  shared flag gate.
- **Making the LSP expand combinators.** The editor has no builder, no
  condition model, and no prelude order. A second, approximate expansion would
  disagree with the compiler and produce false diagnostics.
- **Keeping `gateTerms`/`gate_terms` alongside `GatePlan`.** Two marker
  protocols already diverged between the frontends (ADR 0026). The combinators
  are the point at which one planner seam is cheaper than three duck-typed
  probes.

## Consequences

- `condAll`/`cond_all` gives an explicit conjunction with no trigger cost;
  `condAny`/`cond_any` gives an explicit disjunction over one shared flag;
  `condNot`/`cond_not` gives an explicit negation that is free when a leaf
  complement exists.
- Empty forms are total: `condAll()` is true, `condAny()` is `Never()`, and
  double negation disappears. Identity reduction is structural: `condAny(x)`
  returns `x` itself, and same-kind nesting shares one flag.
- Combinators compose with the ADR 0027 chunker: `condAll` expands before
  chunking, and the flagged forms present one condition to the enclosing gate.
- Raw triggers keep their 16C cap on the expanded list, and a combinator that
  needs a prelude outside a builder reports `EUDSL-SCOPE`. The cap is checked
  against planned slots before prelude emission.
- The editor vocabulary documents all six spellings with their own casing;
  capacity diagnostics stay raw-`Trigger`-only and do not false-positive on
  structured gates or on a `condAll` splice.
- Conditions exposed through `GatePlan` are planned once per gate;
  `whileLoop` shares the plan between entry and tail and preserves the
  pre-ADR act order on mixed condition lists.
- The TS and Python frontends emit byte-identical HIR for the combinator
  matrix, pinned by a parity test that runs the TS builder through `node` and
  compares `to_json()` plus the `EUDSL-TYPE`/`EUDSL-BOUNDS`/`EUDSL-SCOPE`
  error codes.
