# eudkit (Python DSL)

> **한국어 안내:** [py/README.ko.md](./README.ko.md) (English document is the SSOT).

Python implementation of the eudkit DSL. Mirrors `ts/` 1:1 at the canonical
vocabulary level. A small number of builder-seam behaviours differ by design;
see [ADR 0026](../docs/adr/0026-builder-seam-divergence.md).

## Install

```sh
pip install eudkit
```

The package contains the Python DSL only. For the `eudkit` compiler CLI and
`eudkit-lsp`, download a prebuilt standalone bundle from
[GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest) or build
from the repository root (see the [main README](../README.md)).

## Develop

Editable install using `uv` (recommended) or `pip`:

```sh
# From consumer project:
uv add --editable ./eudkit/py

# Or in an existing virtualenv:
pip install -e ./py
```

Build the Rust workspace first. The Python suite shells out to
`target/debug/eudkit` and `target/debug/eudkit-compile`, so it fails on a fresh
checkout until those binaries exist:

```sh
cargo build --workspace

# Run from the repository root:
python3 -m unittest discover -s py/tests -p "test_*.py" -t .
```

Use `unittest`, not `pytest`, and keep `-t .` so `from py import ...` resolves
to this checkout. The full build/test matrix and the `py`/`struct` shadowing
details are in
[CONTRIBUTING.md#build-and-test](../CONTRIBUTING.md#build-and-test).

## Package Layout & Build

The package manifest lives directly in `py/` ([`pyproject.toml`](./pyproject.toml)):

- `packages = ["eudkit"]`
- `package-dir = {"eudkit" = "."}`
- `package-data = {"eudkit" = ["schema/layouts/*.json", "schema/stock-rows.json"]}`

The DSL frontend modules live directly in `py/` and are exposed under package
`eudkit`. The 31 modules group by area:

- **Core:** `hir.py`, `var.py`, `temporal.py`, `trigger.py`, `lazy_binding.py`
- **Memory and views:** `view.py`, `backing.py`, `location.py`, `player.py`,
  `units.py`, `scstate.py`
- **Data structures and I/O:** `array.py`, `queue.py`, `string.py`, `struct.py`,
  `blockio.py`, `capacity.py`
- **Conditions and math:** `conditions.py`, `mathf.py`, `random.py`
- **DAT:** `dat.py`, `datadumper.py`
- **Stock catalog:** `stock_rows_built.py`, `stock_rows_generated.py`
- **Map effects and audio:** `bgm.py`, `camera.py`, `cp.py`,
  `noaircollision.py`, `ogg.py`, `soundlooper.py`, `unlimiter.py`

There is no `mem.py`; memory views (`MemView`, `CUnit`, `CSprite`,
`MemFieldHandle`, `MemCond`) live in `view.py`. Tests are isolated under
`py/tests/`.

The package ships both `schema/layouts/` (JSON layouts loaded at runtime by
`MemView` such as `CUnit` and `CSprite`) and `schema/stock-rows.json` (the
stock/extension spec loaded at import time by `stock_rows_built.py`).

## Consume

### Hello World: in-repo vs installed

Inside this checkout the package directory is `py/`, so the import name is
`py`. This form runs unmodified through the compiler from the repository root
(the CLI puts the repository root on `sys.path`); running the file directly
needs `PYTHONPATH=.`:

```python
# hello.py — in-repo form (run from the repository root)
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

After `pip install -e ./py`, the import name is `eudkit`. This form works when
the `python3` process the compiler invokes has the editable install on its
path (for example, an activated virtualenv):

```python
# hello.py — installed form (after `pip install -e ./py`)
from eudkit import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

Run either file through `./target/debug/eudkit synth hello.py` (after
`cargo build --workspace`) to emit HIR. `synth` stops at HIR; `eudkit compile`
lowers that HIR and assembles a `.scx`. Both forms are the same program; only
the import name differs.

The canonical vocabulary is available under either name:

```python
from eudkit import (
    Var,
    Trigger,
    when,
    once,
    until,
    every,
    after,
    with_cp,
    memcpy_epd,
    memset_epd,
    CUnit,
    CSprite,
    FixedArray,
    FixedString,
    Queue,
    build_module,
)
```

In addition to the canonical vocabulary shared 1:1 with TypeScript, the Python frontend provides:

- Context managers: `with when(...):`
- Operator overloading on `Var`: `+=`, `-=`, `==`, `!=`, `<`, `<=`, `>`, `>=`
- Idiomatic `snake_case` aliases: `on_rising`, `on_falling`, `with_cp`, `memcpy_epd`, `memset_epd`, `while_loop`
- `FixedString` methods `write_from`, `print_at`, `display_at` (aliases of `writeFrom`, `printAt`, `displayAt`), not module-level exports
- Direct property access on views: `u.hp.set(1000)`

### Runtime conditions are not Python bools

`when(conds, body)` runs `body` once, at build time; every comparison
(`v.eq(1)`, `v == 1`, a field read, `Never()`) is a HIR record describing an
in-game test, never a build-time answer. A Python `if`/`while` on one would
silently pick a branch while `buildModule` runs, so it raises
`EUDSL-COND-TRUTHIFY` (ADR 0020). Write the runtime branch instead:

| write this instead of | Python `if`/`while` does | sanctioned spelling |
|---|---|---|
| `if c: A` | build-time branch | `when([c], A)` or `with when([c]):` |
| `if c: A else: B` | `else` is dead | `when([c], A, B)` or `with when([c]) as w: …; w.otherwise(B)` |
| multi-way integer choice | hand-written `when` chain | `dispatch(value, [(k, body), …], fallback)` |
| `while c: body` | build hang | `while_loop([c], body)` |
| `a and b` | drops one cond | pass both: `when([a, b], body)` or `cond_all(a, b)` |
| `a or b` | drops one cond | `cond_any(a, b)` in one `when` |
| `not c`, `x if c else y`, `assert c` | silent | `cond_not(c)` / move the choice into a `when` |

`cond_all(...)`, `cond_any(...)`, and `cond_not(...)` are the sanctioned
boolean surface for conditions (the camelCase aliases are the same objects).
Same-kind nesting flattens before planning, `cond_all(x)` / `cond_any(x)`
return `x`, `cond_all()` is true, `cond_any()` is `Never()`, and negation uses
a direct leaf complement when one exists (otherwise one shared flag gate).
They compose anywhere a condition is accepted, including raw `Trigger.conds`;
combinators that need a flag must be built inside `build_module`. See
[ADR 0028](../docs/adr/0028-logical-condition-combinators.md).

The third argument (or `w.otherwise(fn)` / `w.else_(fn)`) queues a runtime
else arm. It runs iff **not all** conds hold, so `when([a, b], A, B)` runs `B`
when `a` fails or `b` fails. `when(False, A, B)` runs `B` unconditionally;
`when(True, A, B)` / `when([], A, B)` never run `B`. Only `when` has an else
arm.

`dispatch(value, [(k, body), …], fallback)` is the multi-way integer surface.
Its keys are compile-time plain integers (normalized to 32 bits and unique), so
the search shape is fixed at build time. A runtime value with `N <= 9` cases
lowers to a nested `when(..., else_body)` chain; `N >= 10` lowers to a balanced
`HirWhen` search tree (`ge(mid)` internal nodes, `eq(key)` leaves) for an
`O(log N)` search path. A plain-integer `value` folds at build time: only the
selected arm, the fallback, or nothing is emitted, with no `when` node. A miss
without a fallback is a no-op.

A runtime condition is never a Python bool — using one in `if`/`while`/`assert`
raises `EUDSL-COND-TRUTHIFY`
([ADR 0020](../docs/adr/0020-build-time-vs-runtime-conditions.md)).

## Entry

The source file passed to `eudkit synth`, `eudkit dump-ir`, or
`eudkit compile` is the root source. It must assign exactly one compiled
module to `module` at module scope:

```python
# In-repo form; installed consumers use `from eudkit import ...`.
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

`build_module` is the HIR construction entry: it runs the body once at compile
time and returns the `CompiledModule`. It is not a capture hook. Helpers may
build or export their own modules, but only the root source's `module` is
read by the CLI. See
[ADR 0012](../docs/adr/0012-explicit-entry-module-export.md).

## Stock trigger catalog

The DSL exports helpers for all 22 StarEdit stock conditions and all 57 stock
actions. Each helper emits the documented CHK condition or action type and
uses the stock parameter order and default flags:

```python
# In-repo form; installed consumers use `from eudkit import ...`.
from py import (
    Anywhere,
    AtLeast,
    Bring,
    CreateUnit,
    P1,
    Trigger,
    UnitType,
    build_module,
)


def prog() -> None:
    Trigger(
        conds=[Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere)],
        acts=[CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)],
        preserved=True,
    )


module = build_module(prog)
```

`CreateUnitWithProperties` is paired with the `UnitProperty` builder for the
20-byte UPRP record; slots are allocated and deduped at map assembly.
`Transmission` resolves its text to `strid` and its sound name (HIR `wav`) to
`wavid`; `PlayWAV` uses HIR `wav` only.
The stock catalog is stock StarEdit vocabulary; masked memory helpers such as
`DeathsX`, `Memory`, `SetDeathsX`, and `SetMemoryX` are marked **eudkit
extensions** because they reuse the stock wire form but are not StarEdit
rows. Stock helpers accept constants, except that `Deaths` and `SetDeaths`
also accept the documented extension target forms for `player` plus
`amount`/`value`; other dynamic values stay on the raw builders.

See [`docs/stock-triggers.md`](../docs/stock-triggers.md) for the full
support matrix, parameter order, default flags, and the stock-versus-
extension split.

Python builds every non-special stock and extension helper at import time
from `schema/stock-rows.json` in `py/stock_rows_built.py`. The generated Python
artifact that *is* checked in is `py/stock_rows_generated.py`, the ordered
export name list; regenerate it with
`python3 scripts/generate_stock_surfaces.py` (the script's `--check` mode fails
on drift). ADR 0009 records the deliberate TypeScript/Python asymmetry: TS
checks in `ts/stock.generated.ts`, Python builds the helper bodies at import
and checks in only the names. Each constructed helper keeps the spec's
`__name__`, `__doc__`, and `__signature__`, so introspection and keyword calls
still see the documented ordered parameters and defaults. Only the five special
rows (`Deaths`, `SetDeaths`, `CreateUnitWithProperties`, `SetStringDword`,
`UnitProperty`) are hand-written.

## DAT tables

The `dat-*.json` schemas power typed accessors for the native data tables:

```python
# In-repo form; installed consumers use `from eudkit import ...`.
from py import UnitType, WeaponType, build_module, units_dat, weapons_dat


def prog() -> None:
    units_dat[UnitType.Marine].maxHp.set(40)
    units_dat[UnitType.Marine].max_hp.iadd(10)
    weapons_dat[WeaponType.GaussRifle].damage.set(6)


module = build_module(prog)
```

Entries are constant-indexed (`table.at(i)` / `table[i]`); passing a `Var`
throws `EUDSL-TARGET`, and an out-of-range index or unknown field throws
`EUDSL-BOUNDS`. Field names accept the schema spelling, snake_case
(`max_hp`), or a schema alias.

## Core Concepts for Beginners

- **`Var`** — a mutable scalar handle with a build-time initial value. The
  optimizer places it on a `TriggerPage` or demotes it to a dword/bit cell.
  `v2 = v1` aliases the same slot; `Var(v1)` or `v1.copy()` makes an
  independent copy. Create eager `Var`s inside `build_module`; `lazyVar`
  declares one at module scope and binds it to each builder on first use.
- **`FixedString` + `DisplayText`** — `FixedString(size)` is a STR-backed text
  buffer (`write`, `append`, `writeFrom`, `print`, `display`, and the
  `printAt`/`displayAt` chat-line methods). `DisplayText(text)` emits the stock
  action that shows `text` to CurrentPlayer (`0x6509B0`), not to every player;
  its second argument `always_display` defaults to `4`.
- **`CUnit`** — a zero-storage `MemView` over a unit EPD:
  `u = CUnit(epd)`, then `u.hp.set(1000)` or `u.hp.read()`. Field writes and
  comparisons use field units (ADR 0017); reads do not disturb CurrentPlayer.
- **`FixedArray`** — a fixed-capacity array backed by page-aligned
  `TriggerPage` slots. Constant indices are pure address math; dynamic
  indices use carry-free `PageSlotEPD` weights. `FixedArray.const([...])` is a
  zero-page build-time table.
- **Temporal blocks** — `when(conds, body)` runs every loop while the conds
  hold; `once` runs once and disables; `until` runs while the conds are false;
  `every(n, body)` and `after(n, body)` are counted forms; `on_rising` /
  `on_falling` are edge forms; `while_loop` is the structured intra-frame
  loop; `dispatch` is the multi-way integer surface.

## See also

- [Quickstart](../docs/quickstart.md)
- [Quickstart example](../examples/quickstart/README.md)
- [Migration examples](../examples/README.md)
- [Diagnostics reference](../docs/diagnostics.md)
- [Language server (`eudkit-lsp`)](../docs/lsp.md)
- [Glossary](../GLOSSARY.md)
- [In-game smoke maps](../tests/outputmap/smoke/README.md)
