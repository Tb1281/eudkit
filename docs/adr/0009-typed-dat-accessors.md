# Typed DAT table accessors

Status: accepted (2026-11-05)

## Context

`schema/layouts/dat-*.json` already describes the Brood War `units.dat`,
`weapons.dat`, `flingy.dat`, `techdata.dat`, and `upgrades.dat` tables: entry
counts, absolute field base addresses, strides, widths, byte aliases, and the
display-name -> index mappings. Before this decision the DSL could only reach
those tables through raw `EPD` arithmetic; the name mappings were unused, and
nothing checked a field name or an entry bound.

The tables are struct-of-arrays. Field `f` of entry `i` lives at
`base(f) + i * stride(f)`, and a field may start on any byte lane inside its
dword. A runtime entry index would therefore need a runtime lane select, which
the trigger model cannot express as one `SetDeathsX`; the accessor surface
must require a compile-time constant index.

## Decision

1. **Runtime metadata comes from the schemas.** `loadDatTable("dat-trgunit")`
   (TS) and `load_dat_table("dat-trgunit")` (Python) parse the same
   `schema/layouts/dat-*.json` files that the compiler ships. The preloaded
   tables are `flingyDat`, `techDat`, `unitsDat`, `upgradesDat`, `weaponsDat`
   in TS, and `flingy_dat`, `tech_dat`, `units_dat`, `upgrades_dat`,
   `weapons_dat` plus camelCase aliases in Python. `UnitType`,
   `WeaponType`, `FlingyType`, `TechType`, and `UpgradeType` are exported in
   both languages.

2. **Entry lookup is constant-index only.** `table.at(i)` and `table[i]` accept
   an integer in `0..count-1`. A non-integer (for example a `Var`) raises
   `EUDSL-TARGET`; an out-of-range index, unknown display name, or unknown
   field raises `EUDSL-BOUNDS`.

3. **Field handles reuse `MemFieldHandle`.** `DatFieldHandle` computes
   `address = base + index * stride`, folds it into
   `EPD(address)` plus `offset = (address - EPD_BASE) mod 4`, and constructs a
   normal `MemFieldHandle` at that EPD and byte lane. `.set`, `.iadd`,
   `.isub`, comparisons, and `.read` therefore emit exactly what a `MemView`
   at the same absolute address would emit. Field lookup resolves the
   canonical name, the snake_case spelling, and schema aliases
   (`turnRadius` / `turnSpeed`).

4. **Symbols are identifier-safe.** An exact display name maps to its
   PascalCase spelling (`Terran Marine` -> `TerranMarine`). A race-prefixed
   name also gets the prefix-stripped alias (`Terran Marine` -> `Marine`) only
   when that alias is unambiguous across the whole table; for example
   `Marker` is omitted because three races define one. Duplicate exact
   identifiers are an error, not a silent winner.

5. **The TypeScript surface is checked in; Python builds it at import.** The
   `dat.generated.ts` module carries field-name unions, typed entry interfaces
   (canonical names via `DatEntry`, plus snake_case and alias spellings), and
   the symbol const objects. It is rendered by
   `ts/scripts/generate-dat.ts` and a freshness test fails if the schema
   changed without `npm run generate:dat`. Addresses and entry counts are not
   copied into the generated file; the JSON stays the single source of truth
   for metadata. Python has no build step, so it derives the same symbol names
   and entry objects from the same JSON at import time using the same rules.

6. **Cross-language parity is tested at HIR level.** A Rust integration test
   runs the same DAT reads/writes through the TS and Python DSLs, requires
   byte-identical HIR JSON, lowers it, and checks the resulting raw action
   destinations/values/masks against independently computed addresses.

## Considered options

- **Runtime-only untyped proxies in both languages.** Rejected for
  TypeScript: it gives no editor completion or typo checking. Python has no
  equivalent static surface to generate, so it stays runtime-only by design.
- **Generate Python source as well.** Rejected: Python already loads the
  schema JSON at import for `CUnit`/`CSprite`; a second generated artifact
  would add a build step and another drift class without improving typing.
- **Copy field addresses and counts into `dat.generated.ts`.** Rejected: it
  duplicates the schema and lets the two sides drift. The generated file
  contains only what the type system needs.

## Consequences

- Adding or renaming a DAT field requires regenerating
  `ts/dat.generated.ts`; the freshness test fails until that happens.
- Dynamic DAT indexing is not supported by construction. Callers that need a
  runtime-selected entry must use `withCP`/raw EPD arithmetic and the byte-lane
  masks explicitly.
- DAT reads are raw. Fixed-point fields keep their storage scaling:
  `units.maxHp` for a Marine with 40 HP reads as `0x2800` (40 * 256), not 40.
- Ambiguous race-stripped aliases are intentionally absent from both the
  generated constants and the runtime `symbols` object. The exact
  race-qualified names remain available.
- Importing either DSL now reads the five DAT schemas in addition to the
  existing `CUnit`/`CSprite` layouts. This is the same import-time JSON class
  the DSL already relies on; no new build dependency is introduced.
