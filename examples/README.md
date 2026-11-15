# Migration Examples (`eudplib` → `eudkit`)

> **한국어 안내:** [examples/README.ko.md](./README.ko.md) (English document is the SSOT).

> New to eudkit? Start with the progressive beginner examples in
> [`quickstart/`](./quickstart/README.md) and the
> [zero-to-playable-map walkthrough](../docs/quickstart.md), then come back here
> once those four examples make sense.

Side-by-side runnable examples showing how the same memory read/write, cursor,
table, struct/array, and block-I/O intent is written in **`eudplib`**,
**`eudkit` Python**, and **`eudkit` TypeScript**.

Every `py/` and `ts/` pair is verified to synthesize with zero warnings and
produce **byte-identical HIR and `.scx` output**.

## Start here

- For the in-game verification workflow and expected screen output, see
  [`../tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md).
- These are migration examples, not a tutorial. Run them through
  `eudkit synth` / `eudkit compile`; running `python3 examples/py/01_direct_memory.py`
  directly can resolve the PyPI `py` package instead of this checkout.

## Directory Layout

- `examples/eudplib/` — Classic `eudplib` baselines (`from eudplib import *`, exact parameter orders verified against `eudplib`).
- `examples/py/` — `eudkit` Python DSL (`from py import ...`, `module = build_module(...)`). External packages installed via `pip install -e ./py` import `from eudkit import ...`.
- `examples/ts/` — `eudkit` TypeScript DSL (`from "../../ts/index.ts"`, `export const module = buildModule(...)`). External npm consumers of `"eudkit"` import `from "eudkit"`.

## Memory Read/Write Decision Matrix

| Intent | `eudplib` | `eudkit` (recommended API) | Effect on `CP` (`0x6509B0`) |
|---|---|---|---|
| **Conditional comparison at a fixed address** | `Memory`, `MemoryX`, `f_dwread_epd` then `EUDIf` | `MemCond`, `MemView`/`CUnit` comparison (`1C`), `Memory`/`MemoryX` | Untouched (safe) |
| **Writing a constant to a fixed address** | `SetMemory`, `SetMemoryX`, `f_dwwrite_epd` | `MemFieldHandle.set(c)`, `SetMemory`/`SetMemoryXEPD` (`1A`) | Untouched (safe) |
| **Reading a fixed-address value (`Var`)** | `f_dwread_epd`, `f_wread_epd`, `f_bread_epd`, `f_maskread_epd` | `MemFieldHandle(...).read()` / `CUnit(epd).field.read()` (`DeathsX` bit test) | **Untouched (`DisplayText` safe)** |
| **Multiple accesses over consecutive offsets** | `f_setcurpl(epd)` + `f_*_cp` + `f_setcurpl2cpcache()` | `withCP(constEpd, cp => ...)` (`cp.at`, `cp.view(CUnit)`, absolute `moveTo`) | **Caution:** restores `baseEpd` on exit (not `0`). Call `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` before displaying. |
| **`.dat` tables** | `TrgUnit(...)`, `Weapon(...)`, `Tech(...)`, `Upgrade[p]` | `unitsDat[UnitType.TerranMarine]`, `weaponsDat`, `upgradesDat`, `techDat`, `flingyDat` (constant indices only) | Untouched (inherits from `MemFieldHandle`) |
| **Iterating a unit group** | `UnitGroup(cap)` + `for u in g.cploop` | `UnitGroup(cap, { compact: true, autoPrune: true })` + `forEach` | **`emit_cp_sweep` saves and restores the existing CP automatically** |
| **Indexable arrays/structs of your own** | `EUDArray`, `EUDVArray`, `EUDStruct` (`selftype`) | `FixedArray` (`PageSlotEPD` 64A `TriggerPage`), `FixedArray.const`, `TriggerStruct` | A dynamic `FixedArray.get` (`length >= 64`) resets CP to `0` after use |
| **Block copy/init/injection** | `f_repmovsd_epd`, `f_memcpy`, `Db`, `dataDumper` | `memcpyEPD`, `memsetEPD` (static folding up to 512 chunks), `dataDumper` | `memcpyEPD` moves `CP` to `srcEPD + count - 1` (restore `0` if needed) |
| **Reading/writing a runtime variable (`Var`) EPD** | `f_dwread_epd(var_epd)`, `f_dwwrite_epd(var_epd, var_val)` | Read: `memcpyEPD(SCRATCH_EPD, var_epd, 1)` or `emitVarStore(..., CP_ADDR)` + `CPCursor(0)`<br>Write: `Trigger` + `emitVarStore` + `trig.fire()` / a `FixedArray`-owned slot | The `memcpyEPD` / `CPCursor(0)` paths must restore `CP = 0` |

`SCRATCH_EPD` is a local constant declared by the examples (for example
`examples/py/08_dynamic_epd_access.py` and `examples/ts/07_block_io_and_data.ts`),
not a library export; pick any scratch dword for your own map. `CP_ADDR`
(`0x6509B0`) is re-exported from both `py` (`from py import CP_ADDR`) and
`ts/index.ts` (`import { CP_ADDR } from "../../ts/index.ts"`).

## Key Migration Gotchas (`eudplib` → `eudkit`)

1. **`withCP` does not restore `CP = 0` automatically (`03_cp_cursor`)**: `CPCursor.restore()` only re-anchors to `baseEpd` when the block exits. `DisplayText` / `FixedString.displayAt` display to the current `CurrentPlayer`, so before displaying to P1 call `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)`, or use `MemFieldHandle` / `CUnit(epd)` directly for a single constant EPD.
2. **Field writes and comparisons `.set(val)` accept constants only (`02`, `04`, `08`)**: `MemFieldHandle.set(var)` or `u.hp = var` raises an `EUDSL-TYPE` error. To write a runtime `Var` value into memory, use `emitVarStore` + a self-modifying `Trigger.fire()`, or `memcpyEPD` / `FixedArray`.
3. **Value space rules (`02`, `06`)**: `MemFieldHandle` / `DatFieldHandle` use shifted **field units** (ADR 0017), while `TriggerStruct` masked fields (`byte(8)`, `bit(3, 5)`) compare in **raw lane units (shift=0)** (ADR 0018). Comparing a sub-word field against `-1` normalises to `0xFFFFFFFF` and folds to `Never()`, so use `0xFF` / `0xFFFF`.
4. **Do not pass a runtime `Var` EPD directly to `SetMemoryEPD` (`08`)**: `SetMemoryEPD(var_epd, ...)` targets the `TriggerPage` slot of the `var_epd` variable itself (`mode: 1` reloc), not the address the variable *points to*.
5. **No build-time `if`/`while` (ADR 0020)**: putting a `Cond`/`Var`/`MemFieldHandle` into a Python `if`/`while`/`assert` raises `EUDSL-COND-TRUTHIFY`. Always use `when()`, `once()`, `whileLoop()`, or `dispatch()`.

## Topic Index

| # | `eudplib` | `eudkit` Python | `eudkit` TypeScript | Focus |
|---|---|---|---|---|
| 01 | [`01_direct_memory.py`](./eudplib/01_direct_memory.py) | [`01_direct_memory.py`](./py/01_direct_memory.py) | [`01_direct_memory.ts`](./ts/01_direct_memory.ts) | `MemCond`, `SetMemory`, CP-safe `MemFieldHandle.read()` |
| 02 | [`02_subdword_masks.py`](./eudplib/02_subdword_masks.py) | [`02_subdword_masks.py`](./py/02_subdword_masks.py) | [`02_subdword_masks.ts`](./ts/02_subdword_masks.ts) | Byte/word/bitmask (`FieldSpec`, `SetMemoryXEPD`, `TriggerStruct`) |
| 03 | [`03_cp_cursor.py`](./eudplib/03_cp_cursor.py) | [`03_cp_cursor.py`](./py/03_cp_cursor.py) | [`03_cp_cursor.ts`](./ts/03_cp_cursor.ts) | `withCP`, `CPCursor.at/view/moveTo`, `CP=0` reset vs direct `CUnit` |
| 04 | [`04_cunit_and_groups.py`](./eudplib/04_cunit_and_groups.py) | [`04_cunit_and_groups.py`](./py/04_cunit_and_groups.py) | [`04_cunit_and_groups.ts`](./ts/04_cunit_and_groups.ts) | `CUnit`, `CSprite`, `UnitGroup(compact=True, autoPrune=True)` |
| 05 | [`05_dat_tables.py`](./eudplib/05_dat_tables.py) | [`05_dat_tables.py`](./py/05_dat_tables.py) | [`05_dat_tables.ts`](./ts/05_dat_tables.ts) | `unitsDat`, `weaponsDat`, `techDat`, `upgradesDat` |
| 06 | [`06_arrays_and_structs.py`](./eudplib/06_arrays_and_structs.py) | [`06_arrays_and_structs.py`](./py/06_arrays_and_structs.py) | [`06_arrays_and_structs.ts`](./ts/06_arrays_and_structs.ts) | `FixedArray`, `FixedArray.const`, `TriggerStruct`, `Trigger.fire()` |
| 07 | [`07_block_io_and_data.py`](./eudplib/07_block_io_and_data.py) | [`07_block_io_and_data.py`](./py/07_block_io_and_data.py) | [`07_block_io_and_data.ts`](./ts/07_block_io_and_data.ts) | `memcpyEPD`, `memsetEPD`, `Db`, `dataDumper` |
| 08 | [`08_dynamic_epd_access.py`](./eudplib/08_dynamic_epd_access.py) | [`08_dynamic_epd_access.py`](./py/08_dynamic_epd_access.py) | [`08_dynamic_epd_access.ts`](./ts/08_dynamic_epd_access.ts) | Runtime `Var` EPD read (`memcpyEPD`/`CPCursor(0)`) & write (`emitVarStore` + `fire()`) |

## Verify All Examples

Build the workspace first. `test_examples.py` asserts that
`target/debug/eudkit` exists and then compiles with it:

```sh
cargo build --workspace
```

Then synthesize, compare HIR + `.scx` parity across Py/TS, and verify the
`eudplib` baselines:

```sh
# From the repository root:
PYTHONPATH=. python3 -m unittest py/tests/test_examples.py

# Or compile any single example to a playable .scx:
./target/debug/eudkit compile examples/ts/01_direct_memory.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_direct_memory.scx
```

The `eudplib` baseline import checks are skipped when no sibling `eudplib`
checkout exists; all Py/TS parity checks still run.
