# One home for the `CTrigAction` field offsets: `VALUE_FIELD_OFFSET`

Status: accepted (2026-11-11)

## Context

The raw value-field offset (`+20`) needs one named home instead of scattered
literals. The tree was checked for an `ACTION_`-prefixed constant and for a
leak into `eudkit-hir`; neither describes the current tree. The check found:

1. **The `ACTION_`-prefixed spelling does not exist, and the real constant
   does.** That spelling has zero occurrences in the tree. The
   canonical spelling is `VALUE_FIELD_OFFSET = 20` in
   `crates/eudkit-lir/src/lib.rs`, beside `DEST_FIELD_OFFSET = 16`,
   `MASK_FIELD_OFFSET = 0` and `MODIFIER_DWORD_OFFSET = 24` — all byte offsets
   inside one 32-byte `CTrigAction` record. The optimizer's predicate
   `eudkit_opt::cell::is_value_field_offset` in `crates/eudkit-opt/src/cell.rs`
   compares against the constant, not a literal, and lowering reads it for its
   bit-cell validation in `crates/eudkit-lower/src/lib.rs`. Both the constant
   and the predicate are pinned by tests in `eudkit-lir` and `eudkit-opt::cell`.

2. **The raw offset in HIR is deliberate.** `HirReloc.target_offset`
   (`crates/eudkit-hir/src/lib.rs`) is an offset *inside the relocated
   record*, so its vocabulary is the record layout; naming it after the record
   kind would add a second synonym for a value HIR does not otherwise reason
   about. The relocation *records* are authored by the
   frontends, which emit HIR JSON carrying `"field_offset": 16|20`. Those
   literals are the actual leak. The Rust side has two production literals —
   `HirAct::set_var_const` (`crates/eudkit-hir/src/lib.rs`, the builder the
   tests and lowering share) and `opt::temporal::desugar_until`
   (`crates/eudkit-opt/src/temporal.rs`); the remaining Rust hits are test
   fixtures.

The frontends already name the record layout once: `ACT_FIELD_OFFSETS`
(`py/stock_rows_built.py`, `ts/stock-spec.ts`) carries `dest: 16` and
`value: 20`. What remains are residual literal sites that read or write the
layout directly:

| frontend | literal | role |
|---|---|---|
| `py/trigger.py` | `16`, `20` | classic chain runner pointer patch |
| `py/array.py` | `16`, `20` | raw-EPD reset relocs |
| `py/queue.py` | `16`, `20` | raw-EPD reset relocs |
| `py/hir.py` | `16` | `SetStringDword` value-field reloc |
| `py/cp.py` | `20` | CP-cursor slot read (`field_offset` already named) |
| `ts/trigger.ts` | `16`, `20` | classic chain runner pointer patch |
| `ts/array.ts` | `16`, `20` | raw-EPD reset relocs |
| `ts/queue.ts` | `16`, `20` | raw-EPD reset relocs |
| `ts/hir.ts` | `16` | `SetStringDword` value-field reloc |
| `ts/cp.ts` | `20` | CP-cursor slot read (`field_offset` already named) |

## Decision

1. **The name is `VALUE_FIELD_OFFSET`.** No `ACTION_`-prefixed variant
   exists, and this ADR is the record that none will be introduced. The
   record-kind qualifier is redundant: the constant lives in
   `eudkit-lir`, whose offset constants are all `CTrigAction` / `RawTrigger`
   fields, and the neighbouring constants are named for the field (`dest`,
   `mask`, `modifier`), not the record.
2. **`eudkit-lir` is the single Rust home.** `eudkit-hir` gains no offset
   constant, `HirReloc`'s `field_offset` and `target_offset` stay raw, and the
   optimizer keeps branching on `is_value_field_offset` rather than on a
   literal.
3. **The leak origin is the frontends.** The raw `16` / `20` values that reach
   HIR JSON are authored in `py/` and `ts/`; the Rust crates only consume them
   (plus the two production sites above). Any centralization work belongs to
   the frontends' `ACT_FIELD_OFFSETS` dictionary, not to a new HIR symbol.

## Consequences

- This record is documentation-only: no behavior, cost, HIR version, or
  golden change. The cost gates and the `dump-ir` fixtures must stay
  byte-identical.
- The residual frontend literals in the table above are recorded, not
  removed here. Migrating them to `ACT_FIELD_OFFSETS` is behavior-neutral and
  may be folded into any later frontend change that already touches the line;
  it is not a prerequisite for anything.
- A later report of another name for this offset, or one that attributes the
  literals to `eudkit-hir`, should be corrected against this ADR rather than
  re-investigated.
