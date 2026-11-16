# Architecture Decision Records

Status values:

- `accepted (YYYY-MM-DD)` — accepted and never amended.
- `accepted (YYYY-MM-DD; amended YYYY-MM-DD[, YYYY-MM-DD…])` — amended after
  acceptance; every amendment date is listed.
- `accepted (YYYY-MM-DD; revised YYYY-MM-DD)` — corrected or renumbered
  without changing the decision.
- `accepted (YYYY-MM-DD; amends ADR NNNN)` / `supersedes …` — cross-record
  relationship.

Amendment notes at the top of an ADR use one header form,
`> **Amendment (YYYY-MM-DD[, context]):** …`; a longer amendment may instead
use `## Amendment (YYYY-MM-DD)`. `> **Cross-reference (YYYY-MM-DD[, context]):** …`
records a measurement that does not change a decision. An ADR records a
decision and its trade-off; read the linked file for the argument.

| ADR | Title | Status | Scope |
|---|---|---|---|
| [0001](./0001-trigger-16c-64a-architecture.md) | Trigger (16C, 64A) storage and execution | accepted (2026-11-04; amended 2026-11-10, 2026-11-11, 2026-11-13) | Trigger as execution-packing unit and mutable storage backing; four-layer pipeline and coalescing. |
| [0002](./0002-unified-ts-py-dsl.md) | Unified TypeScript and Python DSL surface | accepted (2026-11-04; amended 2026-11-14) | One canonical vocabulary and mirrored `ts/` and `py/` roots; `dispatch` added to the vocabulary. |
| [0003](./0003-capacity-aware-aligned-string-writes.md) | Capacity-aware aligned `FixedString` writes | accepted (2026-11-04; amended 2026-11-06) | Capacity proofs, aligned lanes, and scope-boundary padding. |
| [0004](./0004-triggerpage-transfer-parking-and-curpl-reset.md) | TriggerPage transfers, parking, and `curpl` reset | accepted (2026-11-04) | Transfer-run parking and the per-loop CurrentPlayer reset. |
| [0005](./0005-lazy-global-declarations.md) | Lazy global declarations | accepted (2026-11-04) | Lazy handles, per-builder binding, first-use order, and owner-file views. |
| [0006](./0006-hir-nodes-for-until-edge-and-counted-forms.md) | HIR nodes for `until`, `edge`, and counted forms | accepted (2026-11-04; amended 2026-11-10, 2026-11-11) | Temporal HIR promotion and opt desugaring, including unconditional-`until` semantics. |
| [0007](./0007-displayat-owns-the-display-cursor.md) | `displayAt` relative offset and cursor restore | accepted (2026-11-05; supersedes owning experiment) | Display-line cursor ownership and restore. |
| [0008](./0008-preserve-authored-classic-trigger-flags.md) | Preserve authored classic-trigger flags | accepted (2026-11-05) | Basemap trigger flags survive compilation. |
| [0009](./0009-typed-dat-accessors.md) | Typed DAT table accessors | accepted (2026-11-05) | Schema-derived typed DAT entries and field handles. |
| [0010](./0010-classic-chain-runner-boundary.md) | Classic-chain runner boundary and ordering | accepted (2026-11-05; amended by [ADR 0013](./0013-optional-tick-gate-for-single-chain-maps.md) for single-chain maps) | Begin/End runner pair and classic-first ordering. |
| [0011](./0011-dword-packed-hex-and-constant-offset-string-formatting.md) | Dword-packed hex and constant-offset string formatting | accepted (2026-11-06; amended 2026-11-06, 2026-11-10) | Direct hex accumulation, format-run alignment, and capacity accounting. |
| [0012](./0012-explicit-entry-module-export.md) | Explicit root-source entry module export | accepted (2026-11-06) | One root `module` entry; helper modules cannot provide it. |
| [0013](./0013-optional-tick-gate-for-single-chain-maps.md) | Optional tick gate for single-chain maps | accepted (2026-11-06; amends [ADR 0010](./0010-classic-chain-runner-boundary.md) for single-chain maps) | Opt-out gate; amends ADR 0010 decision 1. |
| [0014](./0014-decimal-format-default-and-fill-alignment-syntax.md) | Decimal default, format mini-language, and digit extraction | accepted (2026-11-06; renumbered from ADR 0012; amended 2026-11-07, 2026-11-11) | `{}` decimal default, fill/alignment grammar, direct digit testing. |
| [0015](./0015-unit-iteration-strategies.md) | Unit iteration strategies and loop primitives | accepted (2026-11-07; amended 2026-11-14) | Five planned iteration algorithms; decision 7 `UnitGroup` compact mode implemented. |
| [0016](./0016-stock-row-spec-single-source-of-truth.md) | Stock row spec as the single source of truth | accepted (2026-11-08) | One schema drives helpers, docs, LSP vocabulary, and exports. |
| [0017](./0017-field-value-space-comparisons.md) | Field value space governs comparisons | accepted (2026-11-09) | Constant folding by field value space and negative-constant rules. |
| [0018](./0018-masked-variable-value-space.md) | Masked variable value space | accepted (2026-11-09; amended 2026-11-14) | Raw-lane comparisons, fold rules, and the un-masked emitted constant; FE1 shares the kernel. |
| [0019](./0019-operand-domain-validation.md) | Operand domains are validated, not coerced | accepted (2026-11-09; amended 2026-11-10, 2026-11-11) | Eager `EUDSL-TYPE` operand checks and documented carve-outs. |
| [0020](./0020-build-time-vs-runtime-conditions.md) | Build-time branching on a runtime condition is an error | accepted (2026-11-10) | Runtime conditions cannot be truthified in `if`/`while`. |
| [0021](./0021-mpq-embedded-file-surface.md) | MPQ embedded-file surface in HIR modules | accepted (2026-11-09; revised 2026-11-10) | `embedFile` and the HIR `files` list; HIR 3.2. |
| [0022](./0022-value-field-offset-single-home.md) | One home for the value-field offset | accepted (2026-11-11) | `VALUE_FIELD_OFFSET` shared; frontends own their literals. |
| [0023](./0023-soundlooper-loop-player.md) | Soundlooper: a segmented loop player | accepted (2026-11-11; amended 2026-11-14) | Archive segment playback, deterministic ids, and the deliberate `when`-chain dispatch exception. |
| [0024](./0024-noaircollision-scrubber-helper.md) | noAirCollision: an unrolled memory scrubber | accepted (2026-11-12) | One-loop collision-table scrubber and its fixed 602-EPD stride. |
| [0025](./0025-multiway-dispatch.md) | Multi-way integer branching: `dispatch` | accepted (2026-11-14) | `dispatch` desugar: `when` chain for `N <= 9`, balanced tree for `N >= 10`. |
| [0026](./0026-builder-seam-divergence.md) | Builder-seam divergence is intentional | accepted (2026-11-14) | Py/TS builder seams differ by design and are pinned per language. |
| [0027](./0027-condition-chunking.md) | Auto-chunking structured gates above 16 conditions | accepted (2026-11-15) | `when`/`once`/`whileLoop` and their desugared forms split into 16C chunk gates; raw `Trigger` stays capped. |
| [0028](./0028-logical-condition-combinators.md) | Logical condition combinators: `condAll`, `condAny`, `condNot` | accepted (2026-11-15) | Build-time conjunction, disjunction, and negation; De Morgan/`complement()` rewrites, shared flag gates, and `GatePlan` planning. |
| [0029](./0029-runtime-epd-addressing.md) | Runtime EPD addressing: an EPD is a value, not an address | accepted (2026-11-15) | `EPD` runtime arithmetic and value-vs-address boundary. |
| [0030](./0030-runtime-indexed-payload-backing.md) | Runtime-indexed payload backing (deferred) | accepted (2026-11-15) | Build-time `ConstFixedArray` lookup vs runtime payload table strategy. |
| [0031](./0031-gate-satisfiability-and-dead-body-folding.md) | Gate satisfiability and dead-body folding | accepted (2026-11-15) | Compile-time elimination of unsatisfiable condition gates and dead bodies. |
| [0032](./0032-player-name-matching.md) | Player-name records and the layout-A nickname matcher | accepted (2026-11-16) | Exact NUL-terminated UTF-8 matching over `0x57EEE8`. |
| [0033](./0033-string-id-wire-resolution.md) | 16-bit wire string ids resolve through relocation `mode 6` | accepted (2026-11-16) | Canonical 16-bit shifted relocation for trigger string references. |
| [0034](./0034-signed-math-trig-and-binary-search.md) | Signed div/mod, table-driven trig, and binary search | accepted (2026-11-16) | Signed rounding modes over shared restoring ladder, Q16 trig payload tables, and `(hi-lo+1).bit_length()` binary search. |
| [0035](./0035-per-builder-lcg-prng.md) | Per-builder LCG PRNG with deterministic tick seeding | accepted (2026-11-16) | Per-builder LCG state, shift/add `dwrand`, `GAME_TICK_ADDR` seeding, and constant-bound `rand_range`. |
| [0036](./0036-player-state-pvariable-and-broadcasts.md) | Player state accessors, `PVariable`, and CP-preserving broadcasts | accepted (2026-11-16; amended 2026-11-16) | Arithmetic `player_exists`, `PlayerField` arrays, 8-slot `PVariable`, and CP-safe `for_each_player` broadcasts. |
| [0037](./0037-location-handles-and-geometry.md) | Location handles and build-time vector geometry | accepted (2026-11-16) | 5-dword `Location` handles, `(left, top)` `getloc_tl`, `memsetEPD` dynamic stores, and build-time `draw_*`. |
| [0038](./0038-tech-and-upgrade-state-tables.md) | Tech researched and upgrade level state tables | accepted (2026-11-16) | SC/BW split byte tables (`DeathsX`/`SetDeathsX`), runtime lane dispatch, and local player validation. |
| [0039](./0039-cunit-view-bit-helpers-and-object-pool.md) | `CUnitView`, bit/split helpers, flag tables, and `ObjectPool` | accepted (2026-11-16) | Typed `CUnitView` facade, Rule V1 bit helpers, flag namespaces, and deterministic LIFO `ObjectPool`. |
