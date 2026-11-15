# Runtime-indexed payload backing (deferred)

Status: deferred — design record only (2026-11-15)

## Context

A payload blob (`Db`) is a read-only table whose address is resolved at
assembly time. `epd_of_db` (ADR 0029) can materialize that address as an EPD,
which is enough to seed a cursor, compare a base, or copy a payload dword.
It is **not** enough to index a payload by a runtime unit id: after the base is
materialized, a runtime offset has to select a row of the blob, and that needs
a backing allocation whose layout is known before lowering.

The Stage-2 design work (WP11) sketched a shadow backing over the fixed CUnit
slot pool so a unit EPD could select a row. This record preserves that design
and its arithmetic so the rejection is documented and a future ticket does not
re-derive it, but **the backing is not shipped here**. WP11 and WP12 are
excluded from this ticket set; `T-WP4` only ships the base-value primitive.

## Decision

### 1. Status

Deferred. Do not implement a runtime-indexed payload container, a public
container type, or a new HIR/LIR node until a ticket explicitly revives WP11.
`epd_of_db` returns a base **value**; it is not a container and does not
allocate a shadow.

### 2. The deferred shadow design

The rejected-but-recorded design is:

```
shadowBase()  = epd_of_db(shadow)                     # 1700 x 84 dword rows
addrOf(unitEpd) = shadowBase + (unitEpd - EPD(0x59CCA8))
```

- `0x59CCA8` is the first CUnit slot address; `EPD(0x59CCA8) = 19,025`.
- Slot starts are `84` dwords (`336` B) apart, so a CUnit's row index is
  `unitEpd - EPD(0x59CCA8)` and the same delta addresses the shadow row when
  the shadow stride equals the CUnit stride.
- The pool has `1700` slots: `1700 x 84 = 142,800` dwords =
  `571,200` B (`~558 KiB`) of payload.

### 3. Stride contract

Let `K` be the row stride in dwords. `K = 84`, matching the CUnit slot
stride, so:

- `row(unitEpd) = unitEpd - EPD(0x59CCA8)` in `[0, 1700)`;
- `dwordOffset(row) = K * row`;
- `shadowDwords = 1700 * K = 142,800`;
- `shadowBytes = shadowDwords * 4 = 571,200`.

The `addrOf` form above is the `K = 84` special case where the incoming EPD
delta already counts dwords. A future implementation must state `K`
explicitly rather than assuming the pool stride and the shadow stride can
never diverge.

### 4. Why it stays out

- It is a container: a public surface would need allocation, bounds policy,
  lifetime, and parity decisions that `T-WP4` does not own.
- The cost is material (`571,200` B per shadow, before any access kernel), and
  the access kernel must be measured against the existing unit-iteration
  strategies (ADR 0015) before it is worth shipping.
- Runtime bases remain rejected. `epd_of_db` gives a base value without
  reopening that decision.

### 5. Future work

If WP11 is revived, the ticket must:

1. own the shadow allocation and document its `K` and bounds contract;
2. decide whether the shadow is a module-level singleton or per-call;
3. ship a runtime access kernel with measured static/executed cost; and
4. keep `epd_of_db` as the only constructor for the base value.

Until then, this ADR is a design record only.

## Consequences

- The rejection is documented with the exact arithmetic, so revisiting it
  starts from a known design rather than a blank page.
- No public container, no array surface, and no new HIR/LIR node ships in this
  ticket set.
- ADR 0029's "base value, not runtime-indexed container" boundary has a
  concrete cross-reference.
