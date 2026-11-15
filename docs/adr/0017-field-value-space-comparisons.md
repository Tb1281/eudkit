# Field value space governs every field comparison, and negative constants fold to `Never`/`Always`

Status: accepted (2026-11-09)

## Context

Field comparison correctness was decided in three passes. #20 made `read()`
mask-aware, so a masked read returns the value in field units
(`(raw & bitMask) >> shift`) instead of the raw lane value. #21 gave `eq` and
`ne` one representable-value rule for both field surfaces: `TriggerFieldRef`
(a deferred `TriggerStruct`/`MemView` field) and `MemFieldHandle` (a bound
address, with `DatFieldHandle` inheriting it). The ordering ticket finished
`ge`, `le`, `gt`, and `lt` on both surfaces and removed the
`mask == default_mask` shortcut that returned the nominal width for a
*straddling* field.

Before #21 the rule was whatever the shift happened to do. A word at byte 2
compared its constant against the nominal 16-bit width, so `w.eq(0x10000)`
silently became `w.eq(0)`; the cycle-9 review measured 15 of 48 probe rows
wrong. The cycle-10 scan re-measured 120 rows and found 35 wrong for the
orderings, because `ge`/`le`/`gt`/`lt` still consulted the nominal width for
straddling fields. One formula is correct for both cases, and the shortcut was
the source of the false positives.

The corrected rule now lives in code: `_field_value_space` (Python) /
`fieldValueSpace` (TypeScript) derives the space, and `_fold_ordering` /
`foldOrdering` folds constants outside it. The negative-constant decision had
its only written explanation in a test docstring
(`py/tests/test_deferred_cond.py:1633-1641`,
`ts/tests/test_deferred_cond.ts:1458-1461`). Neither the term nor the decision
was discoverable from the docs. `GLOSSARY.md` now defines **field value
space**, and this ADR records the decision and its migration.

## Decision

### 1. One rule for every field surface

The field value space is the set of bit patterns a field can hold, expressed
in field units: `(raw & bitMask) >> shift`. Its largest member is
`max = (bitMask & 0xFFFFFFFF) >> shift`. Because every bit-subset of the mask
is representable, the whole mask shifted down is the exact maximum — including
for a *straddling* declaration (a dword at byte 2, a word at byte 3), where it
is smaller than the nominal width and not a power-of-two-minus-one.

Every comparison obeys the space: `read`, `eq`, `ne`, `ge`, `le`, `gt`, `lt`.
There is one space helper per frontend (`_field_value_space` in `py/view.py`,
`fieldValueSpace` in `ts/view.ts`) and one fold helper per frontend
(`_fold_ordering` in `py/view.py`, `foldOrdering` in `ts/view.ts`), and both
field surfaces call them rather than re-deriving a width. The helper has no
default-mask shortcut: a straddling declaration uses `mask >> shift`, never the
nominal width.

### 2. Constants are 32-bit

The right-hand constant is normalised to 32 bits (`c = rhs & 0xFFFFFFFF`)
before the representability test, on every comparison.

- `eq` and `ne` ask **"can the mask hold this pattern?"** — `c & ~max`. A
  pattern with a bit outside the mask can never be held, so `eq` folds to
  `Never()` and `ne` folds to `Always()`.
- The orderings ask **"is this threshold outside the space?"** — a numeric
  `c > max` / `c >= max`, not a subset test. A non-contiguous mask such as
  `0x0F0F` keeps `ge(0x10)` live because `0x10` is below `max`, even though it
  would be out of range for a nominal byte.

| Constant | `ge` | `le` | `gt` | `lt` |
| --- | --- | --- | --- | --- |
| `c > max` | `Never` | `Always` | `Never` | `Always` |
| `c == max` | emit | emit | `Never` | emit |
| `c == 0` | emit | emit | emit | `Never` |

Only constants outside the space fold, so well-formed HIR is unchanged.

### 3. Negative constants are a deliberate break

`-1` normalises to `0xFFFFFFFF`, which no sub-dword field can hold. So
`word.eq(-1)` is `Never()`, `word.ne(-1)` is `Always()`, `word.ge(-1)` is
`Never()`, and `word.lt(-1)` is `Always()`.

The old behaviour had no rule behind it. `eq(-1)` shifted `0xFFFFFFFF` into the
lane and *happened* to mean "all ones" on a word (`0xFFFF` at `shift = 16`);
`ge(-1)` became "true only at the top of the space"; `lt(-1)` became never. None
of those coincidences survives a byte field (which keeps `0xFF`) or a
straddling lane.

**Migration: write the field-width all-ones constant** — `0xFFFF` for a word,
`0xFF` for a byte — to mean all ones. It is representable and stays eager. A
dword field is unaffected, because `0xFFFFFFFF` is representable there:
`hp.ne(-1)` still means `hp.ne(0xFFFFFFFF)`.

### 4. Always-true folds are `Always()`

A folded condition is target-free: no memory read and no `with_cp` hop.
`Always()` is an unconditional condition — no memory read, no comparison — and
the fold runs **before** `_cond_target()`, so the `with_cp(0)` sentinel path is
not materialised for a folded condition. Folded-false conditions already
returned `Never()`; folded-true conditions now return `Always()`, including
`ne`'s unrepresentable-constant fold. Measured on the `with_cp(0)` sentinel
path, the change is 3T / 1C / 4A / 7,224 B → 2T / 1C / 2A / 4,816 B.

### 5. The HIR pin

The rule is pinned by `test_ordering_representable_hir_sha_unchanged`
(`py/tests/test_deferred_cond.py:1857-1886`). Its input is a 13-condition
fixture: `SetDeaths(3000, SetTo, 0x12345678, 0)`; `WordAt2.w`
(`FieldSpec(2, 2)`) compared with `eq(3)`, `ne(5)`, `ge(6)`, `le(0xFFFF)`,
`gt(2)`, `lt(9)`; `TriggerFieldRef(-1, 22, 2, 0, value.id, None)` with the same
six constants; and `CUnit(1000).hp.le(0xFFFFFFFF)` — 13 conditions in 20
statements. Its HIR sha256 is
`d17e451ba3ad6e06f36ea8526601afe98f1f449f7d27a69c8ea8d2d798f15fe1`, identical
before and after the change. The pin exists because every constant in the
fixture is representable: the value-space work must not move it, so the sha is
the reproducible "no golden moves" guard for this rule.

## Consequences

- No HIR change for representable constants. The cost gate stays at
  355 triggers / 442 executed / 855,004 bytes and no golden moves.
- `ge`, `le`, `gt`, and `lt` do not validate masks (only `ne` and `read()` do).
  A declaration with mask bits below `shift` therefore has value space
  `mask >> shift` and folds to `Never`/`Always`; hit counts are unchanged, and
  this is the honest reading of an already-invalid declaration.
- **Out-of-domain literals: fail loudly or continue silently?** The right-hand
  constant is normalised to 32 bits (§2) *before* the value-space test, so a
  literal outside the 32-bit domain is indistinguishable from its wrapped
  value: `hp.ne(0x100000000)` is exactly `hp.ne(0)` (measured: hits
  `[0, 1, 1]` for `hp = 0, 1, 0xFFFFFFFF`, identical to `ne(0)`), and
  `hp.ne(0x100000001)` is exactly `hp.ne(1)`. That wrap is the documented
  normalisation, **not** a wrong answer. What is undecided is the failure mode:
  should an out-of-domain literal fail the build loudly (`EUDSL-RANGE`, which
  would make such programs stop building) or continue silently under
  normalisation? That is a recorded decision follow-up, not part of this rule.
- `Var(explicit_mask=...)` comparisons were the third surface of this rule and
  compared in masked units when this ADR was accepted. ADR 0018 now applies the
  same space in **raw lane units** (`shift = 0`) and stops re-masking the emitted
  constant: `eq`/`ne` fold by the subset test, the orderings by the numeric test,
  and the condition carries the 32-bit normalised constant. A dword `Var` is
  unaffected.

## Rejected alternatives

- **Keep the `mask == default_mask` shortcut and special-case straddling
  fields.** Rejected: one formula is correct for both, and the shortcut is
  what produced the false positives.
- **Fold only in `MemFieldHandle` and let `TriggerFieldRef` mirror it by
  hand.** Rejected: eight near-copies of the rule are what let it drift twice
  (#21 and the ordering ticket).
- **Keep `DeathsX(target, AtLeast, 0)` for the always-true fold.** Rejected:
  it reads memory, needs a target, and materialises the sentinel CP hop for a
  condition that is unconditionally true.
- **Compare negative constants as signed values.** Rejected: it contradicts
  the 32-bit normalisation #21 pinned for `eq`/`ne`, and it would make
  `eq(-1)` and `ge(-1)` disagree about what `-1` means.
