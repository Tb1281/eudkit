# Unit Iteration Strategies and Loop Primitives

Status: accepted (2026-11-07; amended 2026-11-14, 2026-11-16)

> **Amendment (2026-11-14):** corrected the `func()` note (implemented; backs the
> shared field readers), the new-unit snapshot arithmetic (`4 + 336 × 1699 =
> 570,868 B`), and the reader comparison: ~35–40T per live node is loop overhead,
> `20T` the read alone; the §5 crossover is ~130–150 live units.

> **Amendment (2026-11-16):** recorded the shipped `for_each_unit_list` /
> `forEachUnitList` walk, its `UnitLoop` handle, and the `Break` sentinel, plus
> `UnitGroup` compact mode; the remaining iteration primitives stay design
> direction.

## Context

`eudplib` exposes `EUDLoopUnit`, `EUDLoopUnit2`, `EUDLoopPlayerUnit`,
`EUDLoopNewUnit`, `EUDLoopList`, and the `CUnit` variants. Its costs are easy to
misread: `EUDLoopUnit2` looks like a 1700-trigger unroll, but is a runtime loop
with self-modifying trigger fields. `eudkit` has no equivalent first-class
surface, and `UnitGroup.for_each` is a compile-time unroll. Per
[ADR 0001](./0001-trigger-16c-64a-architecture.md), optimization priority is
**executed cost > static cost > byte footprint**.

`EUDLoopUnit2` emits ~**4T static plus the body once** and patches its
death-check EPD and two `SetNumber` values at runtime: 1699 slots at ~3T
executed each (bound, death, advance) ≈ **5.1kT executed per invocation**, plus
body × live units. The list strategies share a ~35T-per-site pointer reader and
cost ~35–40T executed per live node (liveness/advance plus the read); `20T` is
the pointer read **alone**. The new-unit scan instead allocates a 570,868 B
snapshot per site. Eudkit already has the primitive to share a reader: `func()`
is implemented (`py/temporal.py`, `ts/temporal.ts`) and backs the shared
full-dword field reader, so a CUnit reader can be one shared body, not a per-site
48 KB cost.

## Decision

> **Shipped:** `UnitGroup` compact mode (§8); `for_each_unit_list` /
> `forEachUnitList` with `UnitLoop` and `Break` (§2). **Deferred:**
> `for_each_unit_slots` (the documented default), `for_each_player_unit`,
> `for_each_new_unit`, `for_each_tracked`, and tier-2 `slotsUpperBound` (§10).
> Deferred names are design direction, not a current API contract.

### 1. Scope: five algorithms

The `CUnit` variants are zero-cost casts: `EUDLoopCUnit` = `EUDLoopUnit2` +
`CUnit.cast`, `EUDLoopNewCUnit` = `EUDLoopNewUnit` + cast, and
`EUDLoopPlayerCUnit` = `EUDLoopPlayerUnit` + cast. The five algorithms are
global linked list, fixed-slot scan, per-player list, new-unit scan, and generic
list. Eudkit ports the **semantics**, not the eudplib implementation.

### 2. API surface and the shipped list walk

| Surface | Status |
|---|---|
| `for_each_unit_list` / `forEachUnitList` | **implemented** — walk one global CUnit list |
| `for_each_unit_slots` | not implemented — scan the fixed 1699 slots; the documented default for all-units iteration |
| `for_each_player_unit` | not implemented — walk one player's unit list |
| `for_each_new_unit` | not implemented — detect new units; ships only if the §6 two-call-site test passes |
| `for_each_tracked` | not implemented — iterate a `UnitGroup` at its capacity; static size follows §8, advisory warning above ~512 static triggers (≈1.2 MB) |

`for_each_unit` is **reserved** for a future auto-selecting version and is not
shipped fixed to the slot scan: auto-selection needs runtime density measurement
and a simulator benchmark, and changing the meaning later would be a semantic
break. The generic list walker behind the list primitives is not separately
exposed; the loop lowers to the HIR `while` form with no generator plumbing.

**Implemented contract.** Python: `for_each_unit_list(list_name, body,
reentrant=False)`; TypeScript: `forEachUnitList(listName, body, { reentrant =
false } = {})` (the option-object divergence mirrors `compact=True`). The body
receives `(unit_epd, loop)`: `unit_epd` is the runtime `Var` in absolute EPD
space (`with_epd(unit_epd, ...)` reads `CUnit` fields), and `loop` is a
`UnitLoop` exposing `list_name`, `epd`, and `break_()` (TS `breakLoop()`).

- **Break sentinel.** Return `BREAK` (the `Break()` singleton) or call
  `loop.break_()` to stop after the current node. `break_()` emits one
  `done.set(1)` at the current build point, so the author gates it at runtime
  (`when([...], loop.break_)`); repeated calls add acts but still stop once.
  `Break` is a named value, **not a boolean**: the identity check (`ret is BREAK`
  / `ret === BREAK`) lets `True` and every other value continue.
- **No `continue` in v1.** Gate remaining acts with `when`; there is no
  `Continue` sentinel.
- **`reentrant`.** Default `False` reads the successor link *after* the body;
  `reentrant=True` reads it *before*, so a body that frees the current node
  still reaches the successor. A non-plain-`bool` value is `EUDSL-TYPE`.
- **Diagnostics.** A non-`str`/unknown/lowercase list name, a non-plain-`bool`
  `reentrant`, a non-callable body, and `break_()` outside a walk are
  `EUDSL-TYPE`. `"F"` is `EUDSL-BOUNDS`: `the free list is not a unit list:
  walking it enumerates unallocated slots; use free_list_empty() /
  free_list_walk()`. An empty list runs zero iterations; the CP restore runs.

### 3. Head/tail table and list-walk cost

`CUnit.prev` is at `+0` and `CUnit.next` at `+4`
(`schema/layouts/cunit.json`): a forward link is `unit_epd + 1`, a backward
link `unit_epd + 0`.

| List | Walk | Head/tail dword (EPD) | Link offset |
|---|---|---|---|
| `U` (in use) | forward via `next` | `0x628430` (161843) | `+1` |
| `T` (hidden) | forward via `next` | `0x628428` (161841) | `+1` |
| `S` (revealer) | forward via `next` | `0x628434` (161844) | `+1` |
| `D` (dying) | **backward via `prev`** | **tail** `0x62842C` (161842) | `+0` |
| `F` (free) | — | `0x628438` (161845) / `0x62843C` (161846) | rejected |

`D` is walked backward from the tail because `0x59CC98` is not a usable head on
Remastered: a forward `D` walk yields an empty set and silently misses every
death — a correctness requirement, not an optimization. `F` is rejected with
`EUDSL-BOUNDS`; use `free_list_empty` (tier 1, §10) or the tier-3 walk.

**Cost per site / per node**, measured with the 20T
`read_cunit_pointer_saved` reader (§9): ambient CP save 33T and head bind once
per site; per-node scaffolding ~5T, link add ~2T (U/T/S only), and the saved
reader ~29T (store + call + 20T body + restore). Measured **U/T/S 36T** and
**D 34T** per node; final CP restore once per site, unconditional. The U/T/S
figure is 36T against the 35T design band because `ptr + 1` materialises as a
two-act transfer; both stay inside `~35–40T`, so the variance is accepted. Do
not hoist the ambient read into the loop: that is the per-node regression the
saved reader prevents.

### 4. Cost policy and gating

Executed cost is primary. The slot scan's ~5.1kT is accepted **only** when docs
state the cost plainly (5.1kT executed + body × live, excluding the gate) and
docs/examples wrap the loop in `every(2, ...)` or `when(dirty, ...)`. The
primitives stay un-gated: iteration and scheduling are separate concerns; an
optional lint warning may come later. If the 2T fusion lands, the scan drops to
~3.4kT.

### 5. Density crossover

The slot scan is the documented default; below the crossover the list walk wins,
above it the scan wins. With list walk ≈ 35–40T × live nodes and slot scan ≈
5.1kT per invocation, the crossover is roughly **130–150 live units** (arithmetic
bounds about 128–146). Docs say "measure; roughly 130–150" rather than treating
the estimate as a contract; a simulator benchmark replaces it with a table.

### 6. New-unit detection and the snapshot gate

`EUDLoopNewUnit` allocates a per-call-site 1699-entry table aligned with the
CUnit free-list slots. `_UniqueIdentifier.GetDataSize` is `4 + 336 × 1699` =
**570,868 B (~558 KiB) per call site**, acceptable only with **exactly one** call
site. With more, a single shared table is required if the state proves read-only
after init, or `for_each_new_unit` is deferred; sharing ships only if two call
sites use different allowances, a unit is created between them, and neither
consumes the other's state. If the snapshot mutates, share nothing and defer.

### 7. Player loops and curpl

The player loop checks existence at runtime over an explicit range: default
`0..7`, with `0..11` allowed; there is no compile-time map filtering until a
map-info channel reaches the frontends. "Nest-safe" restores the **actual
previous `curpl`**, not `with_cp`'s bound base, on every exit path: normal
completion, break, and nesting.

### 8. `UnitGroup`: unrolled by default, opt-in compact mode (implemented)

`UnitGroup.for_each` remains a compile-time unroll by default: the body is
emitted once per slot. For capacity `C`, lowered body triggers per slot `B`, and
`P` shared triggers outside the sweep,
`unroll_static = P + C*(B+1) + ceil(C/64)` (`T static`); the last term is one
storage page trigger per 64-slot page. §2's `for_each_tracked` warning uses this
formula. `compact=False` emits byte-identical HIR (SHA-256 verified in both
frontends). `UnitGroup(capacity, ..., compact=True)` (Py) / `new
UnitGroup(capacity, { compact: true })` (TS) instead add a runtime-cursor mode;
`lazyUnitGroup(..., compact=True)` carries the flag through deferred resolution,
while pure reads (`capacity`, `autoPrune`, `unlimiter`, `compact`) do not resolve.

- Storage is unchanged: one `arrayDword` `FixedArray` of `capacity` slots,
  backed by 64-slot `TriggerPage`s (2,408 B per page, 37.625 B/entry). Liveness
  is `Deaths(CURRENT_PLAYER, AtLeast, 1, 0)`; an empty slot is `Exactly 0`. Slot
  writes target `CURRENT_PLAYER` (player 13), not `EPD_CURRENT_PLAYER`; only
  cursor/seed/restore writes touch the register at `0x6509B0`. Values use the
  general 32-bit slot reader, not the CUnit-only 20-bit reader.
- `for_each` seeds CurrentPlayer from `slots[0]`, then walks page slots with the
  CP register as cursor: +8 EPDs per slot, +90 correction after slot 63 (slot
  63 → 64 is +98 total). It runs exactly `capacity` iterations, then restores
  the actual previous CurrentPlayer value. Each iteration re-seeds CP from
  `slots[0].value` before advancing, so `with_cp` or a nested compact loop cannot
  corrupt the cursor. The body is emitted once and receives a reused, read-only
  scratch `Var` holding the current slot EPD; writes to it do not mutate storage.
  The unroll passes the actual slot `Var`, so a compact callback must not rely on
  writable-slot semantics or retain the scratch.
- `autoPrune` emits one runtime sweep: read the live EPD, test the death field
  (`+3` with mask `0xFFFFFFFF`, or `+0x13` with mask `0xFF00` for the unlimiter
  order), clear the slot, and `length -= 1` once. `deadCheck(check)` receives
  `slot_value + check_offset` at build time and returns a *dead* condition;
  compact calls it once per group and reuses the condition, the unroll once per
  slot. Predicate HIR lands inside the per-slot live gate in both modes.
- `add` scans low-to-high for the first empty slot, has no duplicate check, is a
  silent no-op when full, and increments `length` once. `remove` clears every
  matching slot and decrements `length` per match, preserving hole order.
  `clear` sweeps once to zero all slots and sets `length = 0`, keeping static
  cost O(1).

**Static and whole-group cost.** Isolated `for_each` with `autoPrune=False` fits
`compact_static = 79 + floor((C+1)/64) + B_once` (`T static`), `B_once` being the
body emitted once (0 empty, 5 for one transfer plus three `when`s). At capacity
1,000, empty is 1,016T / 2,446,532 B unrolled vs 94T / 226,368 B compact, and
3-when is 7,016T / 16,894,536 B vs 99T / 238,408 B. Each whole-group site adds a
sweep scaffold: at 1,000, `autoPrune` 82T, `add` 44T, `remove` 44T, `clear` 43T.
With default `autoPrune=True`, capacity 1,000 empty is 176T /
423,832 B (423.832 B/entry) / 18,076T, or 307T / 739,308 B / 43,191T with
`add`/`remove`/`clear`; a 3-live 3-when body is 180T / 433,464 B / 18,188T, or
311T / 748,940 B / 43,303T. `autoPrune` alone adds ~197 KB; executed figures
include every default sweep.

**Executed cost.** Compact never wins: at capacity 1,000 all-live it is 43,038T
(empty body) / 49,038T (3-when) vs unroll's 1,000T / 8,000T, and zero-live is
~9,038T. It pays ~72T header + ~9T/slot + ~34T/live entry + ~6T body/live entry;
unroll pays ~1T per slot plus the body.

**Trade, crossover, and unreachable targets.** Compact wins static cost when
`C*(B+1) + ceil(C/64) > 79 + floor((C+1)/64) + B_once + S`, where `S` sums
additional whole-group scaffolds. Measured crossings: ~79–80 slots for an empty
body, ~17 for three empty `when`s, ~12 for one transfer plus three `when`s (78
is an 80T static tie). The 1T executed/entry and 72 B/entry targets are
unreachable: a 32-bit EPD costs 32 executed bit tests (~33T before the body), and
37.625 B/slot is **storage-only**, excluding the ~188 KB `for_each` and ~197 KB
`autoPrune` scaffolds; with the current 2,408-byte `RawTrigger`, either needs a
smaller trigger representation or a new lowering primitive. Compact also adds
~34T per live entry and a CurrentPlayer save/restore per invocation, so it is
neither a drop-in replacement below the crossover nor a speed win at any measured
capacity. The default remains `compact=False`.

### 9. Upstream reader shape

Eudkit lands first, using eudplib as reference; the upstream implementation
has two candidates, both **opt-in new functions with tests**, never in-place
changes to `EUDLoopUnit2`:

1. **20T executed pointer reader — implemented in eudkit; upstream
   implementation is still a candidate.** The CUnit slot pool is
   `[0x59CCA8, 0x628297]`: 1699 slots of 336 bytes, slot starts 84 dwords
   apart. In shifted pointer space
   `S = A >> 2`, slot starts span `[0x16732A, 0x18A052]`
   (`142,632 = 84 × 1698` dwords). Pool base / last slot start:
   `0x59CCA8` / `0x628148`; `EPD_BASE >> 2 = 0x1628D9`; `EPD(A) = S - 0x1628D9`
   gives base `19025` and last slot `161657`. The invariant is that `S` bits 0,
   1, and 20 are 0, 1, and 1; tested bits are `S` bits 2–19 = `A` bits 4–21,
   under mask `0x3FFFF0` and shift `2`, seeded `0x100002`. One-shot bias is
   `-0x1628D9` = `0xFFE9D727`; a null pointer reads `0xFFF9D729`.

   The reader is 1 seeded reset + 18 bit tests + 1 bias = **20T executed**, down
   from 32T (37.5% fewer tests), and applies to all three list loops. A null
   pointer reads `0xFFF9D729`, **above** `CUNIT_LAST_EPD` (161,657), so
   end-of-list is an EPD outside `[19025, 161657]` — the upper bound, not `S <
   0x167F2A`, rejects it. `in_cunit_pool` returns the two bound conditions and
   never raises: a hijacked list must stay observable. Forms: inline literal 20T;
   shared body + ~5T call; `read_cunit_pointer(Var)` ≈ 60T (33T ambient read +
   store + call + 20T + restore); `read_cunit_pointer_saved` ≈ 28T per node (the
   33T ambient save hoisted once per site). Tests: null head, first/last slot,
   unlimiter on/off, closed maps.
2. **2T executed/slot scan fusion — unimplemented.** Fuse the loop-bound and
   death checks into the body-entry trigger (`ptr < end`, alive); the advance
   trigger handles continue/exit. This drops the scan to ~2T/slot (~3.4kT per
   invocation), with a break/continue/nesting audit before it ships.

### 10. Capacity truth and tiers

Three tiers; this series ships tiers 1 and 3 and defers tier 2.

1. **Tier 1 — one-condition truth (`free_list_empty`).** `CUNIT_FREE_HEAD =
   0x628438` holds the free-list head; `free_list_empty()` is
   `Memory(CUNIT_FREE_HEAD, Exactly, 0)`, one condition 15 / one trigger when
   gated, zero pointer reads. It is exact only for "is the free list empty now",
   and **advisory, not authoritative**: the allocator can still refuse a creation
   for reasons this dword cannot see. Returning a condition leaves the caller's
   16-condition budget untouched and lets `when`/`once`/`until` fuse it;
   `CUNIT_FREE_TAIL = 0x62843C` documents the other end, but no helper uses it.
2. **Tier 2 — tracker-derived upper bound (`slotsUpperBound`, deferred).** Not
   coded. The cheap form is `1699 − (|U|+|T|+|S|+|D|)` from the four list
   counters, which needs the five-list membership tracker (excluded here,
   awaiting seven Remastered measurements). It is an upper bound contaminated by
   subunits, not a free-slot count. `0x6283F0` is **not** a substitute: it is
   `|U|+|T|+|S|+|D|+subunits`, so it over-reports headroom and goes stale when a
   list is hijacked. The walk-derived form `CUNIT_SLOT_COUNT - free_slot_count()`
   is a second name for one subtraction, inviting per-tick ~29.6kT scans.
3. **Tier 3 — exact one-shot walk (implemented).** Walk the free list from
   `CUNIT_FREE_HEAD` through each node's `next` link (`ptr + 1`), at a measured
   ~37T executed per free node — one T above the forward `U` walk because of the
   `remaining` guard and successor read — once per module (map start or on
   author request), never per tick. Exact and tracker-independent.

   | Surface | Python | TypeScript |
   |---|---|---|
   | walk | `free_list_walk(body, *, max_nodes=CUNIT_FREE_SCAN_MAX)` | `freeListWalk(body, { maxNodes = CUNIT_FREE_SCAN_MAX } = {})` |
   | count | `free_slot_count() -> Var` | `freeSlotCount(): VarImpl` |
   | bound | `CUNIT_FREE_SCAN_MAX = CUNIT_SLOT_COUNT = 1699` | same |

   `body` takes no arguments (a free node has no meaningful identity, and
   exposing a raw slot EPD invites writes into an unallocated CUnit), and may
   return `BREAK` to stop after the current node (identity, so every other
   return continues). `max_nodes` must be a plain int in `1..1699`: a bool or
   non-integer is `EUDSL-TYPE`, out of range `EUDSL-BOUNDS`.

   **One call site per module.** A module-level counter keyed by the active
   builder increments on every call; the second is `EUDSL-BOUNDS`:
   `free_list_walk()/free_slot_count() is a tier-3 one-shot scan; one call site
   per module (ADR 0015 §10)`. The check runs **before any emission**, so a
   rejected call leaves no HIR; `free_slot_count()` shares the emitter and
   budget, while `free_list_empty()` does not consume it. Enforced at build time
   because a per-frame exact count is a ~29.6kT scan at 800 free nodes.

   **Termination is three-way and cycle-safe.** The gate is
   `[*in_cunit_pool(ptr), done.eq(0)]`; each pass reads the successor **before**
   the body, runs the body, decrements `remaining`, sets `done` when it reaches
   zero, and advances only while `done == 0`. It stops on (a) a null pointer,
   whose reader result `0xFFF9D729` is above `CUNIT_LAST_EPD` and rejected by
   the **upper** pool bound; (b) any pointer outside `[CUNIT_BASE_EPD,
   CUNIT_LAST_EPD]`; or (c) `remaining == 0` after exactly `max_nodes` nodes.
   Every pass decrements, so a cycle cannot outlast 1699 iterations: a corrupted
   list costs at most `1699 × 37T ≈ 62.9kT` and can never hang the game. The
   2T/node guard beats a sentinel node, which would mutate game memory.

## Consequences

- Eudkit gains one documented default (`for_each_unit_slots`) plus explicit
  alternatives, replacing a single auto-selecting primitive with unclear costs.
- Large all-units passes stay on the runtime scan; `UnitGroup` remains unrolled
  by default, and large tracked sets can opt into compact mode behind
  `compact=True` in `py/queue.py` / `ts/queue.ts` (CP cursor helpers in
  `py/cp.py` / `ts/cp.ts`) with measured costs instead of enormous unrolls.
- `for_each_new_unit` has a hard prerequisite: one call site, or a shared
  snapshot passing the two-call-site test. Upstream work is limited to the 20T
  reader and 2T fusion, keeping the rest of the eudplib family untouched.
