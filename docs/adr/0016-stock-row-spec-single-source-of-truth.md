# Stock row spec as the single source of truth and frozen per-row goldens

Status: accepted (2026-11-08)

## Context

Before this decision every stock trigger helper had a hand-written body in
each frontend (`ts/hir.ts` and `py/hir.py`): 22 StarEdit conditions, 57
StarEdit actions, and 13 eudkit extension helpers, 92 rows in total. Each
body fixed the CHK type, field placement, default flags, parameter order,
and validators. The reference tables in `docs/stock-triggers.md`, the LSP
vocabulary, and the Python export list were maintained separately, and
`tests/stock-trigger-catalog.json` with its mirrored TypeScript and Python
catalog tests acted as the oracle: each language was checked against the
same catalog, and cross-language parity was the drift guard.

That arrangement has two failure modes. The same row is written down in
several places (two helper bodies, docs, LSP vocabulary, exports), so a
change lands in one and drifts in another. And parity only proves that the
two frontends agree with each other; a mistake copied into both, or into the
shared catalog, passes. The catalog was also partial: it did not describe
the extension rows as rows, and its runner used subprocesses instead of the
frontend API.

[ADR 0009](./0009-typed-dat-accessors.md) had already settled the frontend
asymmetry for schema-derived surfaces: TypeScript gets a checked-in
generated module with a freshness test, while Python constructs its surface
at import with no second checked-in artifact.
[ADR 0006](./0006-hir-nodes-for-until-edge-and-counted-forms.md) makes HIR
changes versioned and exact-checked. The stock row work has the same shape:
one machine-readable schema, generated frontend surfaces, and a frozen
contract that does not move when an implementation moves.

## Decision

### 1. One row spec

`schema/stock-rows.json` is the single source of truth for all 92 helper
rows: 79 stock rows (22 conditions + 57 actions) and 13 `eudkit extension`
rows. It also carries `UnitProperty` as a record row; a record is not one of
the 92 trigger rows and is listed separately so that the five special rows
are visible in one place.

Each row records id, kind, CHK type (null for the record), class (`stock` or
`eudkit extension`), default flags, ordered parameters (name, HIR field, CHK
field, encoding, validator, optional default, optional extension target
form), documentation, and the `special` / `handWritten` / `specialReason`
flags. Counts and provenance assumptions live in the same file. The row spec
is metadata for the frontend helpers; it is not a new HIR node and it does
not describe lowering.

### 2. Helper generation is frontend-only and byte-identical

The generated helpers are the same helpers, field for field, as the
hand-written ones they replace. HIR stays 3.1 under
[ADR 0006](./0006-hir-nodes-for-until-edge-and-counted-forms.md): this work
adds no HIR node, no lowering path, and no version bump. Byte identity is
proven by the frozen goldens (decision 4) replayed through both frontends.

- **TypeScript.** `ts/scripts/generate-stock.ts` renders
  `ts/stock.generated.ts`, one metadata record plus one explicit declaration
  for each of the 88 non-special rows. The module is checked in, and
  `ts/tests/test_stock_codegen.ts` fails when the checked-in text differs
  from a fresh render of the spec.
- **Python.** `py/stock_rows_built.py` constructs the same 88 helpers from
  the spec when `py` is imported, preserving `__name__`, `__doc__`, and
  `__signature__`. No generated Python helper source is checked in.
  `py/stock_rows_generated.py` is only the export manifest;
  `scripts/generate_stock_surfaces.py --check` keeps it and the docs tables
  fresh. This is the [ADR 0009](./0009-typed-dat-accessors.md) asymmetry,
  applied to the stock rows.
- **Shared validation order.** Both frontends validate every parameter in
  the canonical `buildCond` / `buildAct` field order (the order the goldens
  pin) before they build or queue anything, so a late failure cannot leave a
  partial action or trigger behind.

The remaining surfaces derive from the same spec: the generated docs tables,
the Python export manifest, and the LSP vocabulary, which
`crates/eudkit-lsp/build.rs` regenerates on the next Rust build. Those
surfaces are editor and docs metadata, not HIR, so none of them weakens the
frontend-only, byte-identical claim.

### 3. At most five special rows

A **special row** is a row whose behaviour the data emitter cannot express.
At most five may be special, and each must carry a `specialReason`. Today
there are exactly five:

| Row | Reason |
| --- | --- |
| `Deaths`, `SetDeaths` | extension target forms (`PlayerOrFieldTarget` / `ValueOrPtrTarget`) |
| `CreateUnitWithProperties` | UPRP record parameter |
| `SetStringDword` | string-table relocation |
| `UnitProperty` | UPRP record builder; not a trigger row |

The other 88 rows are `handWritten: false` and must not be declared in any
checked-in frontend source. The TypeScript check scans every `ts/*.ts`
module except the generated module; the Python check scans every non-test
module under `py/`. Both fail on a sixth special row and on a non-special
body that reappears by hand.

### 4. The oracle is the frozen per-row goldens

`tests/stock-row-goldens.json` is the new oracle. It was captured from the
hand-written helpers **before** any emitter change, so
it records the existing contract rather than whatever the new emitter
happens to produce. For each of the 92 rows it fixes:

- one argument fixture;
- the exact HIR condition/action record, including JSON key order — the
  values that land in the raw CHK record's fields, under their HIR field
  names (`locid`, `player`, `amount`, `unit`, `comparison`, `condtype`,
  `restype`, `flags`, `eudx` for conditions; `mask`, `strid`, `wavid`,
  `time`, `dest`, `value`, `unit`, `acttype`, `modifier`, `flags`,
  `padding`, `eudx`, and relocations for actions);
- the first diagnostic for every validated parameter, plus an all-invalid
  case that pins validation order (code, message, and which parameter is
  reported first).

`UnitProperty` keeps its raw 20-byte UPRP record golden and its
integer-parameter diagnostics alongside the rows. Its boolean-flag
diagnostics are asserted per frontend instead, because the TypeScript and
Python messages are worded differently; freezing either wording would have
made one frontend wrong.

Both frontends replay the same goldens. Cross-implementation parity is no
longer the drift guard, and the partial test-only catalog and its mirrored
tests are retired (`tests/stock-trigger-catalog.json`,
`py/tests/stock_catalog_runner.py`, `py/tests/test_stock_catalog.py`,
`ts/tests/test_stock_catalog.ts`). The older `test_stock_triggers` suites
and `crates/eudkit-lower/tests/stock_triggers.rs` remain as
lowering/consumer spot checks over representative row groups; they are not
the row oracle.

### 5. Adding a row

Adding a row is a spec edit, a regeneration, a hand-authored golden, and a
counter sync across the tests that pin the counts and docs headings, not two
hand-written bodies plus mirrored tests. Steps 3 and 4 are manual, and step 4
is the residue of counters that the spec does not yet derive:

1. Add the row to `schema/stock-rows.json`, with `special` set only when the
   data emitter cannot express it.
2. Regenerate: `python3 scripts/generate_stock_surfaces.py` (docs tables,
   Python export manifest, and the packaged `py/schema/stock-rows.json` copy)
   and `cd ts && npm run generate:stock` (`ts/stock.generated.ts`). The LSP
   vocabulary regenerates on the next Rust build; Python needs no
   regeneration because it builds at import.
3. Hand-author the new row's entry in `tests/stock-row-goldens.json`, in spec
   order, as an HIR 3.1 record. There is no capture tool: nothing writes a
   golden from the helper. Both `ts/tests/test_stock_rows.ts` and
   `py/tests/test_stock_rows.py` replay the same file, so a missing or
   misordered row fails both suites.
4. Sync the hand-maintained counters and headings. `counts` is not derived
   from the sections (`grep -n counts scripts/generate_stock_surfaces.py
   ts/scripts/generate-stock.ts` finds nothing), and the docs headings are
   exact test keys, so every site below changes with the row:
   - `schema/stock-rows.json` `counts`: the `conditions`, `actions`,
     `extensions`, and `records` integers.
   - Both `test_stock_rows` suites: the section-length and `spec.counts.*`
     literals `22`/`57`/`13`, the stock-row total `79`, the
     `goldens.rows.length` total `92`, and the condition-type range
     expectation.
   - `docs/stock-triggers.md` headings `## Conditions (22)` and
     `## Actions (57)`. They sit outside the generated markers and are exact
     test keys, so a heading edit without the matching test edit raises
     `ValueError` in Python and fails the TypeScript table test.
   - `crates/eudkit-lsp/tests/stock_spec.rs`: the symbol count `92` and its
     message `"22 conditions + 57 actions + 13 extensions"`.
   - `ts/tests/test_stock_codegen.ts`: `generatedRows().length` `88` and its
     message `"88 of the 92 rows are data-driven"`.
   - Prose that restates the counts: `GLOSSARY.md` and this ADR's Decision
     intro. Not test-enforced, but stale after a row add.

Changing an existing row's emitted HIR is different: it requires an explicit
golden update and an HIR version decision under
[ADR 0006](./0006-hir-nodes-for-until-edge-and-counted-forms.md). The goldens are
never auto-rewritten.

## Consequences

- A row is defined once. Docs tables, the LSP vocabulary, the Python export
  manifest, and both helper surfaces are generated from the same file, so a
  new row cannot land in one surface and drift in another.
- Drift detection is stronger and cheaper: the freshness tests compare
  generated output with the spec, and the goldens compare emitted HIR with
  the frozen contract. A wrong change is caught even when both frontends
  agree on it.
- The 88 generated rows share one emitter per frontend. A fix applies
  everywhere at once, and a regression can too; the emitter and its
  validator order deserve the scrutiny that the hand-written bodies used to
  take.
- The five special rows are the only per-row escape hatch, and the cap makes
  them auditable. A sixth row that genuinely cannot be expressed means
  changing the emitter or revisiting this ADR, not silently adding another
  body.
- Diagnostics and validation order are now part of the frozen contract.
  Renaming a label, changing a message, or reordering validation requires a
  golden update, which is deliberate friction.
- The spec is a build/import dependency: TypeScript reads it at build and
  test time, Python at import time. The schema must ship with the package,
  exactly as the DAT and CUnit/CSprite layouts already do.
- HIR stays 3.1. No map output, cost pin, or lower-level golden changes.
- Python pays an import-time construction cost and has no checked-in source
  to inspect. For a generated helper, `__name__`, `__doc__`, and the ordered
  parameter names and defaults survive: `inspect.signature` reports
  `(comparison, time)` for `hir.CountdownTimer`. Type annotations and the
  defining module do not: `hir.CountdownTimer.__module__` is
  `py.stock_rows_built`, not `py.hir`. The hand-written special rows keep both
  their annotations and their `py.hir` module; `inspect.signature(hir.Deaths)`
  still reports `(player: 'PlayerOrFieldTarget', comparison: 'int',
  amount: 'ValueOrPtrTarget', unit: 'int' = 0) -> 'dict[str, Any]'`.

## Rejected alternatives

- **Keep the hand-written bodies and rely on mirrored parity tests.**
  Rejected: two independent bodies plus a shared catalog multiply the places
  a row must change, and parity proves only that the frontends agree, not
  that either matches the CHK contract. A bug copied into both passes.
- **Generate both languages' helper source and check both in.** Rejected:
  Python has no build step, and ADR 0009 already established import-time
  construction as its schema-derived surface. A second generated Python
  artifact would add a drift class without adding typing.
- **Move row generation into Rust or generate at HIR level.** Rejected:
  these are frontend DSL constructors, and the Rust layer never sees a
  "row". A Rust path would either duplicate the frontend constructors or
  change lowering; changing emitted HIR would force a version bump under
  [ADR 0006](./0006-hir-nodes-for-until-edge-and-counted-forms.md), which
  makes that a deliberate, separately justified event.
- **Encode the special rows in the data emitter too.** Rejected: their
  behaviour is not data — it is field relocation, UPRP record assembly, and
  string relocation. Pushing them into the emitter would grow a second,
  implicit mini-language.
- **Keep the test-only catalog as a second oracle.** Rejected: a second
  source of truth is the problem, not the solution, and the catalog was
  incomplete.
- **Capture goldens from the generated emitters after they land.**
  Rejected: an emitter bug would be frozen as truth. The goldens were
  captured from the old hand-written helpers first, and then the emitters
  had to reproduce them byte for byte.
- **Keep cross-implementation parity as the primary drift guard.**
  Rejected: parity is a lower-level spot check on representative programs,
  not a per-row contract, and it cannot catch a mirrored mistake.
