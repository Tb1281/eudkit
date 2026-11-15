# 16-bit wire string ids resolve through relocation `mode 6`

Status: accepted (2026-11-15)

## Context

`units.dat` `nameString` is a 16-bit field: one entry per unit type at
`0x660260 + 2 * t` (`schema/layouts/dat-trgunit.json`). The value stored there
is **not** an address and **not** the 0-based module string index. It is the
1-based STRx id, seeded as `template_count + index + 1`, the same expression
that assigns `ctrig.strid` for a display action.

Today the only writable form is numeric:

```py
unitsDat[UnitType.Marine].nameString.set(NAME.message_index)  # wrong id
```

`NAME.message_index` is the 0-based module string index. The table wants that
index plus the basemap's `template_count` plus one. The result compiles and
silently points the unit type at some other string: a footgun with no
diagnostic.

Two properties of the field constrain the fix:

- **`template_count` is an assembly property.** It is known only after the
  basemap string table is loaded (`save_tbl`), not in either frontend.
- **Two entries share every dword.** `nameString` has stride 2, so an even
  entry lives in lane 0 (`shift 0`, mask `0x0000FFFF`) and an odd entry in
  lane 2 (`shift 16`, mask `0xFFFF0000`). A whole-dword write would clobber
  the adjacent unit type.

## Decision

A 16-bit table field is written from a `FixedString`/`LazyFixedString`
reference through a new HIR relocation mode, `mode == 6`:

```py
unitsDat[UnitType.Marine].nameString.set_string(NAME)
```

```ts
unitsDat[UnitType.Marine].nameString.setString(NAME);
```

`DatFieldHandle.set_string`/`setString` rejects a non-16-bit field with
`EUDSL-BOUNDS` and an operand without an integer `message_index` with
`EUDSL-TYPE`. On success it emits exactly one preserved zero-condition
trigger whose single action is the lane-masked `SetDeathsX` shape:

```text
field_offset = 20              (HIR value field)
target_string = message_index  (0-based module index)
target_offset = 0 | 16         (a BIT shift for mode 6 only)
mode = 6
```

Lowering resolves the id with one shared helper used by both the condition and
action string-relocation loops:

```rust
let id = module_string_base + string_index + 1;  // must fit 16 bits
value = ((id as u32) & 0xFFFF) << shift;
```

This is deliberately the **same expression** that seeds `ctrig.strid`; there is
one definition of the id formula in the lowerer, not a second derivation.
`mode 6` must not call `module_string_addr`: the synthetic `0x2000_0000`
address must never leak into an id field.

### Why not a frontend accessor

A `FixedString.string_id` property would be a lie before assembly. The
frontend cannot know `template_count`; a value computed there would either be
wrong or require the frontend to model basemap string recycling. Relocation at
assembly is the only point where the id is both known and checkable.

### Why not a whole-dword write

`nameString[t]` and `nameString[t + 1]` share a dword. The mask plus the
pre-shift is mandatory; writing the whole dword would rename the neighbouring
unit type as a side effect.

### Overflow policy

An id above `0xFFFF` is a loud `EUDSL-BOUNDS` at assembly
(`LowerError::StringIdOutOfRange`), never silent 16-bit truncation. A shift
outside `0..=31` is rejected the same way. This contrasts with
`MemFieldHandle.act_set`, which masks a constant into its lane without a range
check (the carve-out framed in ADR 0019 §5): here the value is not a
user-written constant but a compiler-derived id, so exceeding the field's
domain is a compiler bug or an impossible string table, and both deserve a
build failure.

### Rejected alternative

Writing `message_index` and documenting "add `template_count` yourself" was
rejected. It is unverifiable at build time, cannot detect the missing `+ 1`,
and preserves exactly the silent-footgun behaviour this change removes.

## Scope

- `unitsDat[t].nameString` — **this decision**.
- `unitsDat[t].rank` (base `6700496`, size 1) already has a constant-writable
  `set(k)`; no id is involved, so it needs no new surface.
- `CUnit.rankIncrease` (+142) and `CUnit.killCount` (+143) are dynamic-EPD
  fields on a selected unit. `MemFieldHandle` still raises `EUDSL-TARGET`;
  unlocking those is the dynamic-EPD target work tracked by ADR 0029 §4, not
  this decision.
- Rank *text* lives in `stat_txt.tbl`, which has no TBL model; it is out of
  scope.
- The id-vs-address distinction with the existing string relocations is
  cross-referenced from ADR 0011 rather than redefining its formatting rules.

No `schema/` change, no new `EUDSL-*` code, and no change to modes 1/4: the
helper is a pure extraction, so existing string/blob relocation output stays
byte-identical.

## Consequences

- Renaming a unit type is a typed write: `set_string(fs)` / `setString(fs)`.
- The frontend never needs `template_count`; the lowerer owns the id formula.
- The 16-code diagnostic contract in `docs/diagnostics.md` is unchanged.
  `EUDSL-BOUNDS` covers the non-16-bit field and the assembly-time overflow;
  `EUDSL-TYPE` covers a bad operand.
