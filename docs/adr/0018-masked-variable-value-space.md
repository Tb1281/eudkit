# Masked variables compare in raw lane units, fold like fields, and emit the un-masked constant

Status: accepted (2026-11-09; amended 2026-11-14)

> **Amendment (2026-11-14):** the shared-helper follow-up in Consequences is
> actioned in Cycle 1 by FE1. `FieldValueSpace` in `py/view.py` / `ts/view.ts`
> is now the single comparison kernel for `TriggerFieldRef`, `MemFieldHandle`,
> and `Var(explicit_mask=...)`; writes, lane resolution, and the concrete HIR
> record stay with the adapters. The mirrored matrix tests are
> `test_field_surface_kernel.py` / `.ts`, and the ADR-0017/0018 HIR pins are
> unchanged. No new ADR number and no semantic change to this decision.

## Context

ADR 0017 settled the value-space rule for the two *field* surfaces
(`TriggerFieldRef` and `MemFieldHandle`), and its Consequences deferred the
third surface in one bullet: `Var(explicit_mask=...)` comparisons "still compare
in masked units" and were "deliberately not covered". That surface turned out to
be wrong in two independent ways, in both frontends, in exactly the same places.

The baseline scan measured it across **26 `(mask, raw)` pairs across six
masks** × six operations × eight constants is **1,248 rows**, compared against
the shipped field rule (ADR 0017 §2) at `shift = 0`. **184 rows disagree**:

- **176 fold-class rows**: a constant outside the space was not folded, so a
  condition that should be `Never()` or `Always()` still read memory and
  compared. Negative constants are in this class.
- **8 pure re-masking rows**: the constant is *numerically within `max`* but
  not a bit-subset of the mask (`c & ~max != 0`), so the emitted threshold
  changed because it was ANDed with the mask before being emitted
  (`mask=0x0F0F .ge(0xFF)`, `.lt(0xFF)`; `mask=0x0F00 .ge(0x1)`,
  `.ge(0xFF)`, `.lt(0x1)`, `.lt(0xFF)`; `mask=0x00E0 .ge(0x1)`, `.lt(0x1)`).

With the fix recorded here (measured in a scratch tree) the same table is
**0**. The TS frontend has the same 1,248-row table and the same 184 → 0, because
both frontends implement the same six comparison bodies independently.

Four public `TriggerStruct` surfaces are wrong today (native simulator, expected
= `(raw & mask) op c` in raw lane units; `byte(1)` is `0xFF << 1` — a *bit*
shift in `FieldSpec`, not a byte index — and `word(1)` is `0xFFFF << 1`):

| field | mask | raw | wrong rows at raw 0 (baseline) |
|---|---|---:|---:|
| `bit(3, 5)` | `0x000000E0` | `0x00` | 8 |
| `byte(1)` | `0x000001FE` | `0x00` | 8 |
| `word(1)` | `0x0001FFFE` | `0x00` | 4 |
| `masked(0x0F00)` | `0x00000F00` | `0x00` | 12 |

The column is the raw-0 slice; over all three raws in the 288-row
`TriggerStruct` matrix the same fields are 16 / 16 / 4 / 20.

The headline: `bits.f.eq(1)` on the `bit(3, 5)` field at raw `0` fires today,
because the constant is re-masked to `1 & 0xE0 == 0` and the condition becomes
`eq(0)`. Correct is `Never()`.

The emitted-constant half is visible in the simulator. When `eudx == EUDX_MAGIC`
it computes `lhs = raw_val & mask` and compares it to `rhs = amount`
**unmasked** (`crates/eudkit-lir/src/lib.rs:891-905`). The field path already
emits `(c << shift) & 0xFFFFFFFF`; the `Var` path emitted `c & bitMask`, which is
what breaks ordering thresholds on masks that are not a low-bit-aligned suffix.

## Decision

### 1. Unit convention — raw lane units

Masked-`Var` comparisons are in **raw lane units**. The compared value is
`raw & bitMask`, and the value space is the bit patterns of `bitMask` as-is, so
`max = bitMask & 0xFFFFFFFF`. `shift` is a **field-surface** concept and is
**not** applied to struct-field `Var`s: `TriggerStruct.instantiate` turns a field
into `Var(init, explicit_id=vid, explicit_mask=fmeta.mask, storage="page")`
(`py/struct.py:295-298`; `ts/struct.ts:203` builds
`new VarImpl(initConst, vid, fmeta.mask, "page")`) and **drops
`FieldSpec.shift`**.

This is consistent with how `set`, `iadd`, and struct init already mask — "the
`bit(3,5)` init must already be shifted" (`py/tests/test_lazy_struct.py:155`).
It differs from the field surfaces only because those compare in field units,
`(raw & bitMask) >> shift`; that difference is the point of stating the
convention.

### 2. Out-of-space constants fold like field constants

With `max = bitMask & 0xFFFFFFFF` and `c = rhs & 0xFFFFFFFF`:

| test | `eq` | `ne` | `ge` | `le` | `gt` | `lt` |
| --- | --- | --- | --- | --- | --- | --- |
| `c & ~max` (subset test) | `Never` | `Always` | — | — | — | — |
| `c > max` (numeric test) | — | — | `Never` | `Always` | — | `Always` |
| `c >= max` | — | — | — | — | `Never` | — |
| `c == 0` | — | — | — | — | — | `Never` |

The `eq`/`ne` surface uses the **subset** test ("can the mask hold this
pattern?"), while the orderings use the **numeric** test ("is this threshold
outside the space?"). That asymmetry is ADR 0017 §2 and it must not be
flattened: on the non-contiguous mask `0x0F0F`, `ge(0x10)` stays live because
`0x10 < max`, even though `0x10` is not a bit-subset of the mask.

Negative constants follow ADR 0017 §3 unchanged. `-1` normalises to
`0xFFFFFFFF`, which no sub-dword mask can hold, so
`Var(explicit_mask=0xFF).eq(-1)` becomes `Never()` and `.ne(-1)` becomes
`Always()`. This is a **break** from today's behaviour, where
`0xFFFFFFFF & 0xFF == 0xFF` made `.eq(-1)` equal `.eq(0xFF)`. **Migration: write
the mask's all-ones pattern** — `0xFF` for a byte mask — to mean all ones. A
dword `Var` (`mask = 0xFFFFFFFF`) is unaffected: `eq(-1)` is still
`eq(0xFFFFFFFF)`.

Only constants outside the space fold, so well-formed HIR is unchanged.

### 3. The emitted constant is the 32-bit normalised constant

The condition carries `c = rhs & 0xFFFFFFFF`, **not** `c & bitMask`. The
simulator compares `lhs = raw & mask` against `rhs = amount` unmasked
(`crates/eudkit-lir/src/lib.rs:891-905`), exactly as the field path already
emits. Re-masking the constant is what changes ordering thresholds on masks that
are not a low-bit-aligned suffix — the 8 re-masking rows in the Context.

The `ne` middle-constant path (`DeferredCond(self, "ne", rhs, ne_const=c)`) is
unaffected by the change, because a constant that reaches it has already passed
the subset test, so `c & bitMask == c`. That sentence covers the *constant*; it
says nothing about the *left-hand read*, which §6 fixes: a masked `Var` reads
its lhs as `raw & bitMask` there too.

### 4. Always-true folds are `Always()`

Same rationale as ADR 0017 §4: a folded condition is target-free — no memory
read, no comparison, and for `ne` no deferred diff temp. Measured on
`dump-ir --json --no-exec`, `Var(0, explicit_mask=0xFF).ne(0x180)` inside a
`when` with an `iadd` body:

| | triggers | conditions | actions | total bytes |
|---|---:|---:|---:|---:|
| baseline | 4 | 1 | 74 | 9,640 |
| fixed | **2** | 1 | **2** | **4,824** |

The fold removes the 72-action deferred diff marker. An **in-space** constant
moves one action — the read-side fix in §6 masks the diff temp — so
`ne(0x80)` is 4T / 1C / **73A** / 9,640 B, down from 74A (the HIR gains one act
per site while the lowered count drops one; the mechanism is in §6). Total
bytes are unchanged, and this is the one golden row this cycle moves. The
no-move rows: a **full-mask** `ne(middle)` stays 4T / 1C / 72A / 9,640 B, and
the out-of-space fold stays 2T / 1C / 2A / 4,824 B.

### 5. The HIR pins

Two pins are the reproducible "no golden moves" guards for this rule, the way
ADR 0017 §5 pins the field rule.

**Full-mask no-move pin.** The fixture is `SetDeaths(3000, SetTo, 0x12345678, 0)`,
a plain `Var(0)` compared with `eq`/`ne`/`ge`/`le`/`gt`/`lt` against
`0, 1, 0x7FFFFFFF, 0xFFFFFFFF, 0x100`, and a `Var(0,
explicit_mask=0xFFFFFFFF)` compared the same six ways against `5`. It serialises
to 17,531 characters in 45 statements with sha256
`a23820c5cee6630262b92010a46cbeb7889249833c032d994be3bdd0eb5fd441`, computed on
the **unmodified** baseline. A full-mask `Var` has no constant outside its
space — `c & ~max` and `c > max` are both vacuous — so the fix adds no new fold
and must leave this sha unchanged (the pre-existing `gt(max)` and `lt(0)` folds
stay as they were).

**Shared masked-fixture pin.** A fixture over `Var(0, explicit_mask=0xFF)` at
`0x7F`, `Var(0, explicit_mask=0xFFFF)` at `0x1234`,
`Var(0, explicit_mask=0x0F0F)` at `0x0F00`, and a full-mask `Var` at `5`, with
21 comparisons spanning the fold classes and an `iadd` body,
serialises to 22 statements (9,443 characters) with sha256
`404d4ecbffd41a80b03f4a8be72e6669b33063ffdde5ee2a3ff1943b6f6c28f3`. The
semantically identical fixture hashes to the **same** value in the py and ts
suites after the fix, so the pair proves py↔ts byte-identical HIR while each
suite runs standalone. On HEAD the same fixture is 10,533 characters /
`sha256 5037cfaf…`: the pin is load-bearing new behaviour, not a re-pin of
whatever the build prints.

### 6. Comparisons read the left operand in its value space

§1–§5 govern which constants are representable and what constant is emitted.
They do not by themselves make the comparison read the left operand correctly.
The convention is `(lhs.raw & lhs.bitmask) op rhs_raw` for a constant right
operand.

The five bodies that emit `DeathsX(..., mask)` already let the simulator mask
the lhs. The `ne` **middle-constant** path did not: `DeferredCond.materialize`
accumulated the **raw** lane into the diff temp, so a masked `Var` whose lane
held a dirty pattern answered `v.ne(0xA0)` with 1 where the value-space answer
is 0. (The dirty lane is reachable through a same-id full-mask alias; ordinary
writes mask.) The fix reuses the existing diff temp — no new temp, no new
`TriggerPage`: it allocates the temp at `0`, accumulates
`target.raw & target.bitmask` into it, then adds `init` when the mask is not
full. The full-mask path is unchanged. The masked accumulate is **one HIR act
per site more** than the raw accumulate it replaces (a masked set plus an add,
where the raw path emitted a single `diff += target` transfer), while the
**lowered module's action count drops by one per site**, because the optimizer
folds the masked set+add pair into one masked accumulate action. Measured
identically in py and ts on a one-, two- and three-site fixture (mask `0xFF`,
`ne(0x80)`, a fresh hit `Var` per site): HIR acts 3 → 4, 5 → 7, 7 → 10;
lowered actions **74 → 73, 83 → 81, 92 → 89**; total bytes unchanged at
**9,640 / 16,872 / 24,104 B**, and no golden moves.

**Variable-RHS comparisons are raw 32-bit, by decision — documented, not
fixed.** `v.eq(w)` compares the raw lane (`raw == w`), not
`(raw & bitmask) == w`. Measured with mask `0xE0`, raw `0x0F`, `w = 0`: `eq` =
0, `ne` = 1, `ge` = 1, `le` = 0, `gt` = 1, `lt` = 0 — exactly `raw op w`.
Reading both sides in the value space needs a second masked read, which costs a
`TriggerPage` per site for the same low-reachability dirty-lane case; it is
deferred and recorded here so the behaviour is a decision, not an unrecorded
bug. The value-space guarantee in this ADR therefore covers **constant**
comparisons only.

## Consequences

- One value-space rule now spans three surfaces: `TriggerFieldRef`,
  `MemFieldHandle`, and `Var(explicit_mask=...)`, the last in raw lane units.
  The comparison guarantee covers **constant** right operands only; a variable
  right operand is compared against the raw lane by decision (§6).
- Representative constants keep byte-identical HIR. The cost-gate fixture module
  serialises byte-identically (`sha256
  063e6941512b2840725e5e2cb70fc6e6bc2ca6c57cd9c3c0b5f75e51ad22a10f`),
  `eudkit synth --compact` is 167,658 B on both frontends before and after, and
  no cost golden moves: the gate stays at 355 triggers / 442 executed /
  855,004 bytes.
- `MemCond` and raw escapes still do not fold. This decision covers the six
  comparison bodies `eq`/`ne`/`ge`/`le`/`gt`/`lt` in `py/var.py` and
  `ts/var.ts`, not the raw-condition surface.
- The shared-helper extraction (one `ValueSpace` helper for all three surfaces)
  was a later-cycle follow-up in the original decision; FE1 actioned it in
  Cycle 1 as `FieldValueSpace` (see the amendment above). The rule itself is
  unchanged.

## Rejected alternatives

- **(a) Apply `shift` and compare in field units.** Rejected: `Var` never
  stores the `FieldSpec.shift` the struct path drops, and comparing in field
  units would contradict how `set`, `iadd`, and struct init already mask.
- **(b) Keep masking the constant (`c & bitMask`).** Rejected: it changes every
  ordering threshold on a mask that is not a low-bit-aligned suffix — the 8
  measured re-masking rows.
- **(c) Fold only `eq`/`ne` and leave the orderings.** Rejected: the 176-row
  fold class stays, and the orderings would keep reading memory for conditions
  that can never hold (or always hold).
- **(d) Treat negative constants as signed.** Rejected: it contradicts ADR 0017
  §3 and would make `eq(-1)` and `ge(-1)` disagree about what `-1` means.
