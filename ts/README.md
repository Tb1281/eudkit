# eudkit (TypeScript DSL)

TypeScript implementation of the eudkit DSL. Mirrors `py/` 1:1.

## Install

```sh
npm install eudkit
```

The package contains the compiled TypeScript DSL (`dist/`), its layout schemas,
and the MIT license. For the `eudkit` compiler CLI and `eudkit-lsp`, download a
prebuilt standalone bundle from
[GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest) or build
from the repository root (see the [main README](../README.md)). Building this
package from a checkout still requires `npm run build`; see below.

## Develop

Build the Rust workspace first. The test harness shells out to
`target/debug/eudkit` and `target/debug/eudkit-compile`, so `npm test` fails on
a fresh checkout until those binaries exist:

```sh
# From the repository root:
cargo build --workspace

# Then from ts/:
npm install        # dev tools only (typescript, @types/node); never shipped
npm run typecheck  # tsc --noEmit over sources + tests
npm run dev        # tsc --watch for continuous rebuild during local development
```

Tests run from `ts/` via Node's native test runner:

```sh
npm test           # runs node --test tests/test_*.ts
```

## Build (JS + .d.ts)

Run from `ts/` (there is no root `package.json`):

```sh
npm run build      # clean rebuild of dist/ from sources
```

The build is plain `tsc` (`tsconfig.build.json`): no bundler, no import
rewrites in sources. `rewriteRelativeImportExtensions` converts the
sources' `.ts` import specifiers to `.js` in the emitted JS, and emits a
`.d.ts` next to every module. Tests (`test_*.ts`) and fixtures
(`*_fixture.ts`) are excluded from the build.

`dist/` is build output and is gitignored (the repo `.gitignore` already
covers `dist/`); it is never committed. A fresh checkout builds with
`npm install && npm run build` (from `ts/`).

The build is type-checked: `npm run build` fails on any type error
instead of emitting a package whose declarations do not match the
sources. `npm run typecheck` runs the same check over the tests too,
without emitting.

## Consume

### Hello World: in-repo vs installed

Inside this checkout the package is `ts/`, and the compiler always loads the
raw TypeScript sources. A file two directories below the repository root (as
`examples/ts/` is) imports them with an explicit path and runs unmodified
through `eudkit synth`:

```ts
// hello.ts — in-repo form (from a file at examples/ts/hello.ts)
import { DisplayText, buildModule } from "../../ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

After `npm run build` from `ts/`, the package is importable by name. This is
the form external npm consumers use:

```ts
// hello.ts — installed form (after `npm run build` in ts/)
import { DisplayText, buildModule } from "eudkit";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

`npm run build` is a prerequisite: `main`/`exports` point at `dist/`, and
`dist/` is gitignored. The compiler invokes Node with
`--experimental-strip-types`, so Node **>= 22.6** is required. The same flag is
needed if you run a source file directly:
`node --experimental-strip-types hello.ts`.

`package.json` points at the build output:

- `main` / `exports` → `./dist/index.js` (plain node, no TS loader needed)
- `types` → `./dist/index.d.ts`

The package ships `dist/` plus `schema/layouts/` (the layout JSON the
`MemView` schemas load at runtime); the raw `.ts` sources are not shipped.
From `dist/`, the schema lookup resolves via the `dist/../schema/layouts`
fallback in `view.ts`, so no `dist/schema` duplication is needed.

## Entry

The source file passed to `eudkit synth`, `eudkit dump-ir`, or
`eudkit compile` is the root source. It must export exactly one compiled
module named `module`:

```ts
// In-repo form for a file at examples/ts/hello.ts.
// Installed consumers import from "eudkit" after `npm run build`.
import { DisplayText, buildModule } from "../../ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

`buildModule` is the HIR construction entry: it runs the body once at compile
time and returns the `CompiledModule`. It is not a capture hook. Helpers may
build or export their own modules, but only the root source's `module` is
read by the CLI. See
[ADR 0012](../docs/adr/0012-explicit-entry-module-export.md).

## Runtime conditions & core concepts

`when(conds, body)` builds the body once and runs it in-game while the
conditions hold. Comparisons and field reads (`v.eq(1)`, `u.hp`,
`Always()`, `Never()`) are HIR records, not JavaScript booleans. A plain
`if`/`while` on one silently tests the object at build time. `tsc` catches
`if (v == 1)` as TS2367, but an opt-in AST check catches the shapes it cannot:
`if (v.eq(1))`, `while (u.hp)`, and comparisons against `.initialValue`. Run it
with `npm run typecheck` (which runs `typecheck:truthiness`); it reports
`EUDSL-TS-TRUTHIFY` and never runs during `eudkit synth`. Write the runtime
branch instead:

| write this instead of | JS `if`/`while` does | sanctioned spelling |
|---|---|---|
| `if (c) A` | build-time branch | `when([c], A)` or `when(c, A)` |
| `if (c) A else B` | `else` is dead | `when([c], A, B)` |
| multi-way integer choice | hand-written `when` chain | `dispatch(value, [[k, body], …], fallback)` |
| `while (c) body` | build hang | `whileLoop([c], body)` |
| `a && b` | drops one cond | pass both: `when([a, b], body)` or `condAll(a, b)` |
| `a \|\| b` | drops one cond | `condAny(a, b)` in one `when` |
| `!c`, `c ? x : y`, `assert(c)` | silent | `condNot(c)` / move the choice into a `when` |

`condAll(...)`, `condAny(...)`, and `condNot(...)` are the sanctioned boolean
surface for conditions. Same-kind nesting flattens before planning,
`condAll(x)` / `condAny(x)` return `x`, `condAll()` is true, `condAny()` is
`Never()`, and negation uses a direct leaf complement when one exists
(otherwise one shared flag gate). They compose anywhere a condition is
accepted, including raw `Trigger.conds`; combinators that need a flag must be
built inside `buildModule`. See
[ADR 0028](../docs/adr/0028-logical-condition-combinators.md).

The core runtime vocabulary matches `py/`:

- **Temporal blocks** — `when(conds, body)` runs every loop while the conds
  hold; `once` runs once and disables; `until` runs while the conds are false;
  `every(n, body)` and `after(n, body)` are counted forms; `onRising` /
  `onFalling` are edge forms; `whileLoop` is the structured intra-frame loop;
  `dispatch` is the multi-way integer surface.
- **`Var`** — a mutable scalar handle with a build-time initial value. The
  optimizer places it on a `TriggerPage` or demotes it to a dword/bit cell.
  `const v2 = v1` aliases the same slot; `Var(v1)` or `v1.copy()` makes an
  independent copy. Create eager `Var`s inside `buildModule`; `lazyVar`
  declares one at module scope and binds it to each builder on first use.
- **`FixedString` + `DisplayText`** — `FixedString(size)` is a STR-backed text
  buffer (`write`, `append`, `writeFrom`, `print`, `display`, and the
  `printAt`/`displayAt` chat-line methods). `DisplayText(text)` emits the stock
  action that shows `text` to CurrentPlayer (`0x6509B0`), not to every player;
  its second argument `alwaysDisplay` defaults to `4`.
- **`CUnit`** — a zero-storage `MemView` over a unit EPD:
  `const u = CUnit(epd)`, then `u.hp.set(1000)` or `u.hp.read()`. Field writes
  and comparisons use field units (ADR 0017); reads do not disturb
  CurrentPlayer.
- **`FixedArray`** — a fixed-capacity array backed by page-aligned
  `TriggerPage` slots. Constant indices are pure address math; dynamic
  indices use carry-free `PageSlotEPD` weights. `FixedArray.const([...])` is a
  zero-page build-time table.

## Stock trigger catalog

The DSL exports helpers for all 22 StarEdit stock conditions and all 57 stock
actions. Each helper emits the documented CHK condition or action type and
uses the stock parameter order and default flags:

```ts
// In-repo form for a file at examples/ts/stock.ts.
// Installed consumers import from "eudkit" after `npm run build`.
import {
  Anywhere, AtLeast, Bring, CreateUnit, P1, Trigger, UnitType, buildModule,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  new Trigger({
    conds: [Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere)],
    acts: [CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)],
    preserved: true,
  });
});
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

Stock and extension helper bodies are generated from
`schema/stock-rows.json` into `ts/stock.generated.ts`; after editing the
spec, run `npm run generate:stock`. A freshness test fails on drift. Only
the five special rows (`Deaths`, `SetDeaths`, `CreateUnitWithProperties`,
`SetStringDword`, `UnitProperty`) stay hand-written.

## DAT tables

The `dat-*.json` schemas power typed accessors for the native data tables:

```ts
// In-repo form for a file at examples/ts/dat.ts.
// Installed consumers import from "eudkit" after `npm run build`.
import {
  UnitType, WeaponType, buildModule, unitsDat, weaponsDat,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  unitsDat[UnitType.Marine].maxHp.set(40);
  unitsDat[UnitType.Marine].maxHp.iadd(10);
  weaponsDat[WeaponType.GaussRifle].damage.set(6);
});
```

Entries are constant-indexed (`table.at(i)` / `table[i]`); passing a `Var`
throws `EUDSL-TARGET`, and an out-of-range index or unknown field throws
`EUDSL-BOUNDS`. Field names accept the schema spelling, snake_case
(`max_hp`), or a schema alias. `ts/dat.generated.ts` is checked in; after
editing a `schema/layouts/dat-*.json` field, run `npm run generate:dat`.

## See also

- [Quickstart](../docs/quickstart.md)
- [Quickstart example](../examples/quickstart/README.md)
- [Migration examples](../examples/README.md)
- [Diagnostics reference](../docs/diagnostics.md)
- [Language server (`eudkit-lsp`)](../docs/lsp.md)
- [Glossary](../GLOSSARY.md)
- [In-game smoke maps](../tests/outputmap/smoke/README.md)
