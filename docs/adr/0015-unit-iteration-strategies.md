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

> **Scope note (2026-11-07; amended 2026-11-16).** Decision 7 is implemented
> (issue #101). Decision 2's `for_each_unit_list` / `forEachUnitList`, its
> `UnitLoop` handle, and the named `Break` sentinel are implemented by
> `T-WP7` (`py/units.py`, `ts/units.ts`). The other four primitives in
> decision 2 — `for_each_unit_slots`, `for_each_player_unit`,
> `for_each_new_unit`, and `for_each_tracked` — remain confirmed design
> direction but unimplemented; this ADR is not a current API contract for
> those names until their implementation tickets land.
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

### 2. API surface (Q1, Q6, Q9) — `for_each_unit_list` implemented; the rest confirmed (not implemented)

Ship a small first-class core with Py/TS parity and one docs page. The
explicit primitives are:

- `for_each_unit_list` / `forEachUnitList` — **implemented** (`T-WP7`): walk
  one global CUnit list. See the implementation contract below.
- `for_each_unit_slots` — scan the fixed 1699 slots. This is the documented
  default for all-units iteration. Not implemented.
- `for_each_player_unit` — walk one player's unit list. Not implemented.
- `for_each_new_unit` — detect units created since the previous invocation.
  Ships only if the two-call-site test in decision 5 passes; otherwise the
  API is deferred per decision 5. Not implemented.
- `for_each_tracked` — iterate a `UnitGroup`; delegates to
  `UnitGroup.for_each` and inherits its capacity. Static size follows the
  formula in decision 7; warn above ~512 static triggers (≈1.2 MB). The
  warning is advisory, not a hard cap; no hard cap is imposed, and large
  tracked sets can opt into compact mode. Not implemented.

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
element. The loop lowers to the HIR `while` form; no generator plumbing is
introduced.

**Implemented contract (`for_each_unit_list`).** The Python signature is
`for_each_unit_list(list_name, body, reentrant=False)`; the TypeScript
signature is `forEachUnitList(listName, body, { reentrant = false } = {})`.
The build-time option-object divergence mirrors `compact=True` versus
`{ compact: true }`. The body receives `(unit_epd, loop)`: `unit_epd` is the
runtime `Var` in eudkit's absolute EPD space (so `with_epd(unit_epd, ...)`
reads `CUnit` fields), and `loop` is a `UnitLoop` exposing `list_name`,
`epd`, and `break_()` (alias `breakLoop()` in TypeScript).

- **Break sentinel.** Return `BREAK` (the singleton `Break()`) from the body,
  or call `loop.break_()`, to stop the walk after the current node.
  `break_()` emits one `done.set(1)` at the current build point, so the
  author gates it at runtime (`when([...], loop.break_)`); it may be called
  more than once (one act each, still one stop). The check is identity
  (`ret is BREAK` / `ret === BREAK`), never truthiness: `True` and every
  other value continue. `BREAK` is not `True`, `1`, or a sentinel-by-value.
- **No `continue` in v1.** The body is emitted once inside a runtime `while`,
  so there is no body-level "skip the rest of this node" form. Gate the
  remaining acts with `when` instead; there is no `Continue` sentinel.
- **`reentrant`.** Default `False` reads the successor link *after* the body;
  `reentrant=True` reads it *before*, so a body that frees the current node
  still reaches the successor. The flag is a plain `bool`; anything else is
  `EUDSL-TYPE`.
- **Diagnostics.** A non-`str`/unknown/lowercase list name, a non-plain-`bool`
  `reentrant`, a non-callable body, and `break_()` outside a walk are
  `EUDSL-TYPE`. `"F"` (the free list) is `EUDSL-BOUNDS`, with the message
  `the free list is not a unit list: walking it enumerates unallocated slots;
  use free_list_empty() / free_list_walk()`. An empty list is not an error:
  it is a zero-iteration walk, and the CP restore still runs.

**Head/tail table.** `CUnit.prev` is at `+0` and `CUnit.next` at `+4`
(`schema/layouts/cunit.json`), so a forward link is `unit_epd + 1` and a
backward link is `unit_epd + 0`:

| List | Walk | Head/tail dword (EPD) | Link offset |
|---|---|---|---|
| `U` (in use) | forward via `next` | `0x628430` (161843) | `+1` |
| `T` (hidden) | forward via `next` | `0x628428` (161841) | `+1` |
| `S` (revealer) | forward via `next` | `0x628434` (161844) | `+1` |
| `D` (dying) | **backward via `prev`** | **tail** `0x62842C` (161842) | `+0` |
| `F` (free) | — | `0x628438` (161845) / `0x62843C` (161846) | rejected |

`D` is walked backward from the tail because `0x59CC98` is not a usable head
on Remastered: a forward `D` walk yields an empty set and silently misses
every death. This is a correctness requirement, not an optimization. `F` is
not an iteration target — it enumerates unallocated slots — and is rejected
with `EUDSL-BOUNDS`; use `free_list_empty` (tier 1, §9) or the tier-3
one-shot walk (`T-WP10-T2`) instead.

**Cost table (per site / per node).** Measured by the simulator fixture
(`crates/eudkit-cli/tests/unit_list_walk.rs`) with the 20T
`read_cunit_pointer_saved` reader from §8:

| Item | Executed |
|---|---|
| ambient CP save (`read_current_player_register`) | 33T, once per site |
| head bind (`ptr.set(read_cunit_pointer_saved(saved, head))`) | once per site |
| per-node loop scaffolding (3-condition gate + advance gate/act) | ~5T |
| link add (U/T/S only; `ptr + 1`) | ~2T |
| `read_cunit_pointer_saved` per node (store + call + 20T body + restore) | ~29T |
| **U/T/S per node (measured)** | **36T** |
| **D per node (measured)** | **34T** |
| final CP restore | once per site, unconditional |

The measured U/T/S figure is above the ticket's ~30–33T estimate because
`ptr + 1` materialises as a two-act transfer rather than one; both figures
stay inside the corrected `~35–40T` per-node row. The restore runs after the
loop unconditionally, including the break and empty-list paths. Do not hoist
the ambient read into the loop: that is the per-node regression the saved
reader exists to prevent.

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

1. **20T executed pointer reader — implemented in eudkit by `T-WP6`; the
   upstream hand-off is still a candidate.** The CUnit slot pool is
   `[0x59CCA8, 0x628297]`: 1699 slots of 336 bytes, so slot starts are 84
   dwords apart. In the shifted pointer space `S = A >> 2`, slot starts span
   `[0x16732A, 0x18A052]` (`142,632 = 84 × 1698` dwords). The earlier text's
   upper bound `0x18A052` was correct, but its lower bound `0x167F2A` was
   `0xC00` too high, its span `139,560` wrong, its bit window and seed wrong,
   and its end-of-list test wrong. The corrected facts:

   | Fact | Value |
   |---|---|
   | pool base / last slot start | `0x59CCA8` / `0x628148` |
   | shifted space `S = A >> 2` | `[0x16732A, 0x18A052]` |
   | `EPD_BASE >> 2` | `0x1628D9` |
   | `EPD(A) = S - 0x1628D9` | base `19025`, last slot `161657` |
   | invariant `S` bits | 0 is 0, 1 is 1, 20 is 1 |
   | tested bits | `S` bits 2–19 = `A` bits 4–21 |
   | mask / shift | `0x3FFFF0` / `2` |
   | seed | `0x100002` |
   | one-shot bias | `-0x1628D9` = `0xFFE9D727` |
   | null-pointer result | `0xFFF9D729` |

   So the reader is exactly 1 seeded reset + 18 bit tests + 1 bias = **20T
   executed**, down from 32T (37.5% fewer executed tests), and applies to all
   three list loops. A null pointer reads as `0xFFF9D729`, which is **above**
   `CUNIT_LAST_EPD` (161,657), so end-of-list is an EPD outside
   `[19025, 161657]` — the upper bound, not `S < 0x167F2A`, rejects it.
   `in_cunit_pool` returns the two bound conditions and never raises: a
   hijacked list must stay observable.

   | Form | Executed |
   |---|---|
   | literal `int` EPD, inline | **20T** (1 reset + 18 tests + 1 bias) |
   | shared CP body, per call | 20T + call sequence (~5T) |
   | `read_cunit_pointer(Var)` | 33T ambient read + 1 store + ~5T call + 20T + 1 restore ≈ **60T** |
   | `read_cunit_pointer_saved` per loop node | 1 store + ~5T call + 20T + 1 restore + 1 advance ≈ **28T** |
   | ambient CP save, once per loop site | 33T (hoisted by `T-WP7`) |

   The per-node ambient read is why the loop form is
   `read_cunit_pointer_saved`: the 33T read belongs outside the loop, not
   repeated per node.

   Tests: null head, first/last slot, unlimiter on/off, closed maps.
2. **2T executed/slot scan fusion — still unimplemented and unclaimed.** Fuse
   the loop-bound and death checks into the
   body-entry trigger (`ptr < end`, alive); the advance trigger handles
   continue/exit. This drops the scan to ~2T executed per slot (~3.4kT executed per
   invocation), with a break/continue/nesting audit before it ships.
   Tests: null head, first/last slot, unlimiter on/off, closed maps.

### 9. Capacity truth and tiers

Capacity questions get different tools depending on the precision required.
Three tiers are defined; this series ships tier 1 only.

1. **tier 1 — one-condition truth (`free_list_empty`).** `CUNIT_FREE_HEAD =
   0x628438` is the dword holding the head of the CUnit free list;
   `free_list_empty()` is `Memory(CUNIT_FREE_HEAD, Exactly, 0)`, one condition
   15 / one trigger when gated, with zero pointer reads. It is exact for the
   single question it asks — "is the free list empty at this instant" — and
   nothing else. It is **advisory, not authoritative**: the engine's allocator
   can still refuse a creation for reasons this dword cannot see. The helper
   returns a condition rather than wrapping a `Trigger`, so the caller's
   16-condition budget is untouched and `when`/`once`/`until` can fuse it.
   `CUNIT_FREE_TAIL = 0x62843C` documents the other end of the list, but no
   helper is built on it; the head alone carries the one-condition answer.
2. **tier 2 — tracker-derived upper bound (deferred).** An upper bound of the
   form `1700 − Σ(the four list counters)` needs the five-list tracker (WP12),
   which is excluded from this wave. It is an upper bound at best, contaminated
   by subunits, and not a free-slot count; it does not ship here.
3. **tier 3 — exact one-shot walk (`T-WP10-T2`).** Walk the free list node by
   node at ~22–28T executed per free node, once at map start or on author
   request, never per tick. It answers the exact count and is out of scope for
   tier 1.

Cost contract:

| Form | Static | Executed |
|---|---|---|
| `when([free_list_empty()], body)` idle | 1T + body | 1T + body |
| `when([free_list_empty()], body)` empty | 1T + body | 1T + body |
| counter form (`1700 −` four list counters) | 1C + 1T + read | 6–7T and wrong |

**Rejection: `0x6283F0`.** The counter at `0x6283F0` is
`|U|+|T|+|S|+|D|+subunits`, not the number of free CUnit slots.
`1700 − 0x6283F0` therefore over-reports headroom, and it goes stale the
moment any of the lists is hijacked by a runtime list rewrite. It is rejected
as an upper bound and must not be reintroduced; the one-condition head read
above is the tier-1 truth. Tier 2 remains blocked on WP12, and tier 3 is
`T-WP10-T2`.

**Series note.** §8 item 1 (the 20T pointer reader) is rewritten by `T-WP6` in
the same series; this section does not touch it.

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
