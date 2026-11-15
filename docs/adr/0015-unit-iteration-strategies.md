# Unit Iteration Strategies and Loop Primitives

Status: accepted (2026-11-07; amended 2026-11-14)

> **Amendment (2026-11-14):** corrected the `func()` implementation note (it is
> implemented and already backs the shared field readers), the new-unit
> snapshot arithmetic (`4 + 336 × 1699 = 570,868 B`), and the reader-cost
> comparison: the `~35–40T executed × live nodes` row is per-node loop
> overhead, while `20T` is the planned pointer read alone. The §4 crossover is
> therefore ~130–150 live units at the corrected per-node cost. Decisions 1–6
> remain unimplemented; decision 7 is implemented.

## Context

`eudplib` exposes a family of unit loops: `EUDLoopUnit`, `EUDLoopUnit2`,
`EUDLoopPlayerUnit`, `EUDLoopNewUnit`, `EUDLoopList`, and the `CUnit`
variants. The family is widely used but its costs are easy to misread:
`EUDLoopUnit2` looks like a 1700-trigger unroll, when it is actually a runtime
loop with self-modifying trigger fields. `eudkit` has no equivalent
first-class surface, and `UnitGroup.for_each` is a compile-time unroll whose
body is duplicated per slot.

This ADR records the iteration algorithms, the corrected cost model, the
Py/TS API surface, and the size/gating rules. Per
[ADR 0001](./0001-trigger-16c-64a-architecture.md), optimization priority is
**executed cost > static cost > byte footprint**.

### Corrected cost model

The earlier claim that `EUDLoopUnit2` costs "~1700 static triggers" is
withdrawn. `EUDLoopUnit2` emits roughly **4T static plus the body once** and
patches its death-check EPD and two `SetNumber` action values while running.
The 1699 is the number of slots scanned at game time:

- **static cost**: ~4T static + body once per loop site.
- **executed cost**: ~3T executed per slot (bound check, death check, advance) × 1699
  ≈ **5.1kT executed per invocation**, plus body cost × live units.

Reference costs for the eudplib family, using the glossary's qualified
`T static` / `T executed` units:

| Strategy | Static cost (`T static`) | Executed cost per invocation (`T executed`) |
|---|---|---|
| Global list walk (`EUDLoopUnit`) | ~35T static per site with a shared reader | ~35–40T executed × live nodes |
| Fixed-slot scan (`EUDLoopUnit2` / `EUDLoopCUnit`) | ~4T static + body once | ~3T executed × 1699 ≈ 5.1kT executed + body × live |
| Per-player list (`EUDLoopPlayerUnit` / `EUDLoopPlayerCUnit`) | ~35T static per site with a shared reader | ~35–40T executed × that player's live units |
| New-unit scan (`EUDLoopNewUnit` / `EUDLoopNewCUnit`) | ~35T static + 570,868 B (~558 KiB) snapshot per site | O(allowance) per loop after init; full free-list scan at init |
| Generic list (`EUDLoopList`, bullet/sprite wrappers) | ~35T static per site with a shared reader | ~35–40T executed × nodes |

The reader in the "~35T static" list rows is eudplib's shared `EUDFunc` pointer
reader (32 bit tests). The `~35–40T executed × live nodes` figures are
per-node loop overhead (liveness/advance plus the pointer read), while the
`20T` figure in §8 is the pointer read **alone**; the 20T reader does not make
a whole list step cost 20T. See **Implementation notes**.

Eudkit already has the primitive needed to share a reader: `func()` is
implemented (`py/temporal.py`, `ts/temporal.ts`) and backs the shared
full-dword field reader (`py/view.py` / `ts/view.ts`), which keeps one body per
distinct address. A future CUnit reader can therefore be one shared body, not
a per-site 48 KB cost.

## Decision

> **Scope note (2026-11-07).** Decision 7 is implemented, by issue #101.
> Decisions 1–6 are confirmed as the accepted design direction, but remain
> unimplemented: none of `for_each_unit_list`, `for_each_unit_slots`,
> `for_each_player_unit`, `for_each_new_unit`, `for_each_tracked`, or
> `Break` exists in `py/` or `ts/` today. This ADR is not a current API
> contract for those names until their implementation tickets land.
>
> The confirmation covers Q1, Q3, and Q6 as written, including the Q3
> requirement to restore the actual previous `curpl` on the break path and
> the Q6 named, non-boolean `Break` sentinel, together with the seven edge
> decisions recorded below.

### 1. Scope: five algorithms, one family (Q2, Q4) — confirmed (not implemented)

The `CUnit` variants are zero-cost casts over the underlying loop, not
separate algorithms:

- `EUDLoopCUnit` = `EUDLoopUnit2` + `CUnit.cast`
- `EUDLoopNewCUnit` = `EUDLoopNewUnit` + `CUnit.cast`
- `EUDLoopPlayerCUnit` = `EUDLoopPlayerUnit` + `CUnit.cast`

The five underlying algorithms are: global linked list, fixed-slot scan,
per-player list, new-unit scan, and generic list. Eudkit ports the
**semantics**, not the eudplib implementation.

### 2. API surface (Q1, Q6, Q9) — confirmed (not implemented)

Ship a small first-class core with Py/TS parity and one docs page. The
explicit primitives are:

- `for_each_unit_list` — walk the global CUnit list.
- `for_each_unit_slots` — scan the fixed 1699 slots. This is the documented
  default for all-units iteration.
- `for_each_player_unit` — walk one player's unit list.
- `for_each_new_unit` — detect units created since the previous invocation.
  Ships only if the two-call-site test in decision 5 passes; otherwise the
  API is deferred per decision 5.
- `for_each_tracked` — iterate a `UnitGroup`; delegates to
  `UnitGroup.for_each` and inherits its capacity. Static size follows the
  formula in decision 7; warn above ~512 static triggers (≈1.2 MB). The
  warning is advisory, not a hard cap; no hard cap is imposed, and large
  tracked sets can opt into compact mode.

`for_each_unit` is **reserved** for a future auto-selecting version and is
not shipped fixed to the slot scan. Auto-selection requires runtime density
measurement and must earn its place with a simulator benchmark; shipping
`for_each_unit` as slots now and later changing its meaning to auto would be
a semantic break.

The generic list walker is the shared primitive behind the list primitives.
It is not separately exposed in the first surface.

The callback API returns a named `Break` sentinel to stop iteration. `Break`
is a named value, **not a boolean**: an accidental truthy return must not
terminate the loop. Any other return, including `None`, continues to the next
element; `continue` is a normal return. The loop lowers to the HIR `while`
form; no generator plumbing is introduced.

### 3. Cost policy and gating (Q5r) — confirmed (not implemented)

Executed cost is the primary constraint. The slot scan's ~5.1kT executed per
invocation is accepted **only** when:

- the docs state the cost plainly (5.1kT executed + body × live, excluding the gate),
  and
- docs and examples wrap the loop in `every(2, ...)` or a `when(dirty, ...)`
  guard.

The loop primitives themselves stay un-gated: iteration and scheduling are
separate concerns. An optional lint warning for un-gated loops may come
later; no runtime gate is added.

If the 2T executed fusion candidate lands, the scan drops to ~3.4kT executed and the
crossover moves down proportionally (roughly 85–100 live units at the corrected
~35–40T per-node list cost).

### 4. Density crossover (Q8) — confirmed (not implemented)

Eudkit does not bet on a single density regime. The slot scan is the
documented default; below the crossover the list walk is expected to win,
and above it the scan wins:

- list walk ≈ 35–40T executed × live nodes (per-node loop overhead; the
  planned 20T reader is the pointer read alone, §8)
- slot scan ≈ 5.1kT executed per invocation

That puts the crossover at roughly **130–150 live units** at the corrected
per-node cost (arithmetic bounds are about 128–146). The earlier 150–200
estimate used the withdrawn 20–40T range. The docs say "measure; roughly
130–150" rather than treating the estimate as a contract. A simulator
benchmark replaces the estimate with a table.

### 5. New-unit detection and the snapshot gate (Q11) — confirmed (not implemented)

`EUDLoopNewUnit` allocates a per-call-site 1699-entry table aligned with the
CUnit free-list slots. `_UniqueIdentifier.GetDataSize` is `4 + 336 × 1699` =
**570,868 B (~558 KiB) per call site**.

That price is acceptable only when there is **exactly one** call site in the
module. With more than one call site, a single shared table is required if
the state proves read-only after init, or `for_each_new_unit` is deferred.

Shared detection ships only if a two-call-site test passes:

- the two call sites use different allowances,
- a unit is created between the two calls, and
- neither call site consumes the other's state.

If the snapshot turns out to mutate, share nothing and defer the API.

### 6. Player loops and curpl (Q3) — confirmed (not implemented)

The player loop performs a runtime existence check over an explicit player
range: default `0..7`, with `0..11` allowed. There is no compile-time map
filtering until a map-info channel reaches the frontends.

"Nest-safe" includes restoring the **actual previous `curpl`**, not
`with_cp`'s bound base, on every exit path: normal completion, break, and
nesting. The break path is part of the contract, not an afterthought.

### 7. `UnitGroup`: unrolled by default, opt-in compact mode (Q10) — implemented

`UnitGroup.for_each` remains a compile-time unroll by default: the body is
emitted once per slot. For capacity `C`, lowered body triggers per slot `B`,
and `P` shared triggers outside the sweep,
`unroll_static = P + C*(B+1) + ceil(C/64)` (`T static`); the last term is one
storage page trigger per 64-slot page. The `for_each_tracked` warning in
decision 2 is evaluated against this formula; compact mode is the opt-in
alternative above it. The unroll is unchanged, and `compact=False` emits
byte-identical HIR (verified by SHA-256 in both frontends).

`UnitGroup(capacity, ..., compact=True)` (Py) and
`new UnitGroup(capacity, { compact: true })` (TS) add a runtime-cursor mode
for large tracked sets. `lazyUnitGroup(..., compact=True)` carries the flag
through deferred resolution. Pure reads (`capacity`, `autoPrune`,
`unlimiter`, `compact`) do not resolve a deferred handle.

**Implemented design.**

- Storage is unchanged: one `arrayDword` `FixedArray` of `capacity` slots,
  backed by 64-slot `TriggerPage`s (2,408 B per page, 37.625 B/entry).
- `for_each` seeds the CurrentPlayer register from `slots[0]`, then walks
  page slots with the CP register as cursor: +8 EPDs per slot, with a +90
  correction after slot 63 (slot 63 → 64 is +98 total). The loop runs
  exactly `capacity` iterations, then restores the actual previous
  CurrentPlayer value. The body is emitted once and receives a reused,
  read-only scratch `Var` holding the current slot EPD; writes to it do not
  mutate storage. The unroll instead passes the actual slot `Var`, so a
  compact callback must not rely on writable-slot semantics or retain/alias
  the scratch across iterations.
- Each iteration re-seeds CP from `slots[0].value` before advancing by the
  loop offset, so a body that clobbers CurrentPlayer via `with_cp` (or a
  nested compact loop) cannot corrupt the cursor.
- Liveness is `Deaths(CURRENT_PLAYER, AtLeast, 1, 0)`; an empty slot is
  `Exactly 0`. Slot writes target `CURRENT_PLAYER` (player 13), not
  `EPD_CURRENT_PLAYER`; only cursor/seed/restore writes touch the
  CurrentPlayer register at `0x6509B0`.
- Values are read with the general 32-bit slot reader, not the CUnit-only
  20-bit reader, so arbitrary tracked EPDs keep working.
- `autoPrune` emits one runtime sweep: read the live EPD, test the death
  field (`+3` with mask `0xFFFFFFFF`, or `+0x13` with mask `0xFF00` for
  the unlimiter order), and clear the slot plus `length -= 1` exactly once.
  `deadCheck(check)` receives `slot_value + check_offset` at build time and
  returns a *dead* condition. Compact mode calls it once per group and
  reuses the returned condition for every live slot; the unroll calls it
  once per slot. HIR emitted by the predicate is placed inside the per-slot
  live gate in both modes, but the build-time call count differs by design.
- `add` scans low-to-high for the first empty slot, has no duplicate
  check, is a silent no-op when full, and increments `length` once.
- `remove` clears every matching slot and decrements `length` per match,
  preserving hole order.
- `clear` uses a single runtime sweep to zero all slots and sets
  `length = 0`; static cost stays O(1) rather than `capacity` triggers.

**Measured static cost.** Isolated top-level `for_each` with
`autoPrune=False` and no slot setup (`T static` / total bytes). "3-when"
means one transfer plus three `when` statements in the body; "empty" means
no body statements:

| capacity | empty, unroll | empty, compact | 3-when, unroll | 3-when, compact |
|---:|---:|---:|---:|---:|
| 1 | 2T / 4,820 B | 79T / 190,248 B | 8T / 19,272 B | 84T / 202,288 B |
| 63 | 64T / 154,116 B | 80T / 192,656 B | 442T / 1,064,344 B | 85T / 204,696 B |
| 64 | 65T / 156,524 B | 80T / 192,656 B | 449T / 1,081,200 B | 85T / 204,696 B |
| 65 | 67T / 161,340 B | 80T / 192,656 B | 457T / 1,100,464 B | 85T / 204,696 B |
| 1000 | 1,016T / 2,446,532 B | 94T / 226,368 B | 7,016T / 16,894,536 B | 99T / 238,408 B |

The isolated compact `for_each` scaffold fits
`compact_static = 79 + floor((C+1)/64) + B_once` (`T static`), where
`B_once` is the body emitted once (0 for empty, 5 for the
transfer-plus-three-`when` body above). Each additional whole-group site adds its own fixed sweep scaffold:
measured at capacity 1,000, `autoPrune` adds 82T static, `add` 44T static,
`remove` 44T static, and `clear` 43T static. Compact is a **static-size
optimization, not a speed optimization**.

**Whole-group benchmark at capacity 1,000 (default `autoPrune=True`).**
Top-level `for_each` plus optional top-level `add(4000)`, `remove(4000)`,
and `clear()`; the 3-live rows pre-set three slots and use a three-`when`
body. `T static` / total bytes / bytes per entry / `T executed` for one
simulator invocation:

| program | `T static` | bytes | B/entry | `T executed` |
|---|---:|---:|---:|---:|
| empty body, default `for_each` + `autoPrune` | 176 | 423,832 | 423.832 | 18,076 |
| empty body, default `for_each` + `autoPrune` + add/remove/clear | 307 | 739,308 | 739.308 | 43,191 |
| 3 live, 3-when body, default `for_each` + `autoPrune` | 180 | 433,464 | 433.464 | 18,188 |
| 3 live, 3-when body, default `for_each` + `autoPrune` + add/remove/clear | 311 | 748,940 | 748.940 | 43,303 |

The earlier for_each-only figure of 226 B/entry understated the default
group: `autoPrune` alone adds ~197 KB, so the default empty group is
~424 B/entry and the mutation-heavy group is ~739 B/entry. The executed
figures likewise include every default sweep, not just `for_each`.

**Measured executed cost.** Isolated `for_each` (`autoPrune=False`),
`T executed` per site invocation (simulator, one top-level execution),
empty body / 3-when body:

| capacity | fill | unroll | compact |
|---:|---|---:|---:|
| 1 | 1 live | 1 / 8 | 81 / 87 |
| 63 | 3 live | 63 / 84 | 707 / 725 |
| 63 | all live | 63 / 504 | 2,747 / 3,125 |
| 64 | 3 live | 64 / 85 | 716 / 734 |
| 64 | all live | 64 / 512 | 2,790 / 3,174 |
| 65 | 3 live | 65 / 86 | 725 / 743 |
| 65 | all live | 65 / 520 | 2,833 / 3,223 |
| 1000 | 3 live | 1,000 / 1,021 | 9,140 / 9,158 |
| 1000 | all live | 1,000 / 8,000 | 43,038 / 49,038 |

Residual compact costs: ~72T executed fixed header (CP save/restore, seed,
gate, cursor update) + ~9T executed per slot + ~34T executed per live entry
for the 32-bit reader and liveness gate, plus ~6T executed per live entry
for the 3-when body. All-live capacity 1,000 is 43,038T executed empty /
49,038T executed with the body; zero-live is ~9,038T executed. Unroll's
per-slot cost is ~1T executed plus the body, so compact never wins executed
cost in the measured range.

**Trade and crossover.** Compact wins static cost when
`C*(B+1) + ceil(C/64) > 79 + floor((C+1)/64) + B_once + S`, where `S` is
the sum of additional whole-group site scaffolds. Measured crossings:

| body | `B` | `B_once` | crossover |
|---|---:|---:|---:|
| empty | 0 | 0 | ~79–80 slots |
| three empty `when`s | 4 | 3 | ~17 slots |
| one transfer + three `when`s | 6 | 5 | ~12 slots |

At the empty-body crossover, capacity 78 is a static tie (80T each; unroll
is 12 B smaller), capacity 79 is 81T unroll vs 80T compact, and capacity 80
is 82T vs 80T. For the six-trigger body, capacity 11 is 78T unroll vs 84T
compact and capacity 12 is 85T vs 84T. Below the crossover unroll is both
smaller and faster; above it compact trades executed cost for byte budget.
Choose compact only when the group is large and the static budget binds.

**Why the original 1T executed/entry, 72 B/entry target is not reachable.** The
1T executed/entry figure assumed an entry could be visited with one trigger
evaluation. Materialising a 32-bit EPD with the current EPD model costs 32
executed bit tests, so a live entry costs ~33T executed before the body; the
general reader is required for non-CUnit EPDs. The 72 B/entry figure is
**storage-only**: the page layout is 37.625 B per slot, and that number
excludes all trigger scaffolds. Each sweep carries its own fixed scaffold:
the isolated `for_each` scaffold is ~188 KB (78–79T static), and `autoPrune`
adds a further ~197 KB. The default whole group is therefore several sweeps,
not one. At capacity 1,000 the default group measures 423,832 B total
(423.832 B/entry), and adding top-level `add`/`remove`/`clear` measures
739,308 B total (739.308 B/entry). Total bytes/entry therefore do not approach
72 B in any practical group; the storage-only 37.625 B/slot figure excludes
trigger scaffolds and is not a total bytes/entry measure. Meeting either
target would require a smaller trigger representation or a new lowering
primitive; with the current 2,408-byte `RawTrigger`, compact mode cannot
deliver them.

**Limitations.** Compact mode adds ~188 KB of `for_each` scaffold per
group even when the body is tiny, adds ~34T executed per live entry, and
saves/restores CurrentPlayer at every site invocation. A default group
(`autoPrune=True`) costs 176T static / 423,832 B at capacity 1,000; adding
top-level `add`/`remove`/`clear` raises that to 307T / 739,308 B. It is not
a drop-in replacement for unroll below the static crossover, and it is not
a speed improvement at any measured capacity. The default remains
`compact=False`.

### 8. Upstream shape (Q7)

Eudkit lands first, using eudplib as the reference. Upstream later sends
exactly two candidates, both as **opt-in new functions with tests**, not
in-place behavior changes to existing `EUDLoopUnit2`:

1. **20T executed pointer reader.** Valid CUnit EPDs live in
   `[0x167F2A, 0x18A052]`, so bit 20 is always set. Read only bits 0–19
   (pointer bits 2–21), seed the accumulator with `0x100000`, and treat
   `< 0x167F2A` as end-of-list. This cuts each pointer read from 32T executed to
   20T executed (~37.5% fewer executed tests) and applies to all three list loops.
   Tests: null head, first/last slot, unlimiter on/off, closed maps.
2. **2T executed/slot scan fusion.** Fuse the loop-bound and death checks into the
   body-entry trigger (`ptr < end`, alive); the advance trigger handles
   continue/exit. This drops the scan to ~2T executed per slot (~3.4kT executed per
   invocation), with a break/continue/nesting audit before it ships.
   Tests: null head, first/last slot, unlimiter on/off, closed maps.

## Implementation notes

- `func()` is implemented (`py/temporal.py`, `ts/temporal.ts`) and the shared
  full-dword field reader already uses it (`py/view.py` / `ts/view.ts`). The
  planned 20T reader can be one shared body; do not budget a per-site 48 KB
  penalty for it. The remaining work is the unit-iteration API itself, not a
  missing subroutine primitive.
- The 2T executed fusion changes the loop's failure target and must be audited
  against break, continue, and nesting; it is a prototype candidate, not a
  default.
- The density crossover is provisional until the simulator benchmark
  produces a table. The docs use "measure; roughly 130–150".
- Decision 7's compact mode is implemented in `py/queue.py` and
  `ts/queue.ts` behind `compact=True`; the shared CurrentPlayer cursor
  helpers live in `py/cp.py` and `ts/cp.ts`. The cost tables above come from
  the simulator and are covered by the Py/TS compact test suites.

## Consequences

- Eudkit gains one documented default (`for_each_unit_slots`) and explicit
  alternatives, instead of a single auto-selecting primitive with unclear
  costs.
- Large all-units passes stay on the runtime scan; `UnitGroup` remains
  unrolled by default, and large tracked sets can opt into compact mode
  with measured static and executed costs instead of silently producing
  enormous unrolls.
- `for_each_new_unit` has a hard prerequisite: one call site, or a shared
  snapshot that passes the two-call-site test.
- Upstream work is deliberately limited to the 20T executed reader and 2T executed fusion,
  keeping the rest of the eudplib family untouched.
