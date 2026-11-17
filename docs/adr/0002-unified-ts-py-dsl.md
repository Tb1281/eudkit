# Unified TypeScript and Python DSL Surface

Status: accepted (2026-11-04; amended 2026-11-14)

> **Amendment (2026-11-14):** `dispatch` added to the canonical vocabulary
> (see [ADR 0025](./0025-multiway-dispatch.md)).

## Context

Historically, TypeScript and Python frontends often lived in asymmetric directories, used different naming conventions (`onRising`/`memcpyEPD` vs `on_rising`/`memcpy_epd`), and required verbose string lookups (`u.field("hp").set(1000)`) for struct and pointer fields. Every documentation page, glossary entry, and schema test had to account for two divergent surface styles.

## Decision

1. **Flat Root `ts/` and `py/` Layout**:
   - Both frontends live directly at the repository root in `ts/` and `py/` without intermediate `src/` or `eudkit/` subdirectories (`ts/<module>.ts` and `py/<module>.py`, with dedicated `py/pyproject.toml` and `ts/package.json`): `trigger`, `struct`, `view`, `array`, `queue`, `string`, `temporal`, `cp`, `hir`. (There is no `mem` module; the `memcpyEPD`/`memsetEPD` helpers live in `cp`.) Tests live in isolated `ts/tests/` and `py/tests/` subdirectories.
2. **Direct Property Access**:
   - `TriggerStruct`, `MemView` (`CUnit`, `CSprite`), `CondView`, and `ActView` expose fields as direct properties (`u.hp.set(1000)`, `trig.act(0).value.set(42)`) rather than `.field("...")` calls.
3. **Single Canonical Vocabulary**:
   - Both `dsl/ts` and `dsl/py` export identical canonical type names (`Trigger`, `TriggerStruct`, `MemView`, `CondView`, `ActView`, `FixedArray`, `Queue`, `Deque`, `Stack`, `UnitGroup`, `FixedString`, `CUnit`, `CSprite`) and identical function/method names (`when`, `once`, `until`, `every`, `after`, `dispatch`, `onRising`, `onFalling`, `withCP`, `withEpd`, `memcpyEPD`, `memsetEPD`, `.set()`, `.iadd()`, `.isub()`, `.eq()`, `.ne()`, `.lt()`, `.le()`, `.gt()`, `.ge()`, and on `FixedString`: `.write()` from offset `0`, `.append()` from `lastpos`, `.writeFrom(pos, ...)` from explicit byte offset `pos`, `.print()`, `.printAt()`, `.display()`, `.displayAt()`, `.clear()`).
   - `FixedArray` and `FixedString` name the two fixed-capacity containers. The `Fixed` prefix records that `size` is a build-time maximum and avoids shadowing the JavaScript globals `Array` and `String`, so `import { FixedArray, FixedString, Var } from "eudkit"` coexists with ordinary `Array.from(...)` and `String.fromCharCode(...)` calls. `Var` keeps its name because it does not collide with the lowercase `var` keyword, and the legacy `EUDArray` alias is removed rather than retained as a second name.
   - Python additionally provides idiomatic `with when(...):`, operator overloading (`+=`, `==`, `>=`), and `snake_case` aliases (`on_rising`, `with_cp`, `with_epd`, `memcpy_epd`, `write_from`, `print_at`, `display_at`) that desugar to the exact same HIR nodes.
   - Deliberate exception: declarative action constructors are the `Var`
     methods `act_set` / `act_add` / `act_sub` in both frontends, named for
     the HIR action they return. There is no TypeScript camelCase
     action-constructor surface; Python still provides its usual `snake_case`
     aliases where applicable.
4. **Symmetric Install Roots and Local Development Workflows**:
   - `py/` and `ts/` are symmetric independent install roots:
     - **Python (`py/`)**: Installable directly via `uv add -e ./eudkit/py` or `pip install -e py/` with `py/pyproject.toml` mapping `package-dir = {"eudkit" = "."}` and packaging `eudkit = ["schema/layouts/*.json", "schema/stock-rows.json"]`.
     - **TypeScript (`ts/`)**: Installable directly via `npm link ./eudkit/ts` or `npm install ./eudkit/ts` with `ts/package.json` exposing `./dist/index.js` and `./dist/index.d.ts`.
   - **Local TS Development Workflows**:
     - *Compiled + Watch (`npm link`)*: When developing across local projects, run `npm run dev` (`tsc -p tsconfig.build.json --watch`) inside `ts/` so edits to `ts/*.ts` continuously compile to `dist/index.js` and `dist/index.d.ts`.
     - *Zero-Build Live Source (`tsconfig` path mapping)*: Consumers using bundlers or TypeScript runners (`tsx`, `vite`, `esbuild`) can configure `paths: { "eudkit": ["../eudkit/ts/index.ts"] }` in consumer `tsconfig.json` to import raw TypeScript sources directly with zero build overhead.
   - **Schema Layout SSOT**:
     - Root `schema/layouts/*.json` is the canonical Single Source of Truth (SSOT).
     - Both `ts/schema/layouts/` and `py/schema/layouts/` are committed in git as tracked shipped package data, guarded by byte-identity packaging tests (`test_schema_packaging`).
