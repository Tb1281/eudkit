# eudkit

`eudkit` is a compiler toolchain and unified TypeScript/Python DSL for authoring StarCraft: Remastered EUD maps around the native `Trigger (16C, 64A)` execution and storage model.

> Canonical domain definitions only: one to three sentences each plus _Avoid_ synonyms and anti-patterns.
> Design rationale and detailed algorithms live in the linked ADRs and module docs.

## Execution & Trigger Model

**eudkit**:
The unified toolchain, compiler CLI (`eudkit compile`, `eudkit dump-ir`), and crate family (`eudkit-hir`, `eudkit-opt`, `eudkit-lower`, `eudkit-lir`, `eudkit-map`, `eudkit-lsp`).
_Avoid_: `eudc`, `eudc-*`, `eudplib`.

**buildModule**:
The HIR construction entry: a compile-time function returning a `CompiledModule` that the root source hands to the CLI as `module`.
_Avoid_: `onPluginStart`, `beforeTriggerExec`, `afterTriggerExec`, capture hook.

**module (root source entry)**:
The single compiled module the root source exports (TypeScript `export const module = buildModule(...)`) or assigns at module scope (Python `module = build_module(prog)`). `synth` / `dump-ir` / `compile` read only this root-namespace entry, so helper modules cannot provide it ([ADR 0012](./docs/adr/0012-explicit-entry-module-export.md)).
_Avoid_: last module, capture hook, any exported module.

**build-time macro**:
A TypeScript or Python function (including `buildModule` and callbacks passed to `when`/`once`) that runs once at compile time to construct the HIR trigger tree. It never runs in game and has no in-game call stack or local frames; in-game repetition happens through trigger loops over emitted triggers.
_Avoid_: runtime function call, in-game stack frame.

**build-time vs runtime condition**:
A build-time condition is a Python or TypeScript value evaluated while `buildModule` runs, such as a `bool`, loop counter, or comparison of constants, that drives a build-time `if`/`while` and decides which HIR is emitted. A runtime condition is a HIR record describing an in-game test (`v.eq(1)`, a field read, `Never()`); using one as a Python `bool` in `if`/`while`/`assert`/`and`/`or`/`not` raises `EUDSL-COND-TRUTHIFY` ([ADR 0020](./docs/adr/0020-build-time-vs-runtime-conditions.md)), so branch with `when`/`once`/`while_loop` and combine conditions with a **condition combinator**.
_Avoid_: runtime cond, in-game bool, "cond truthiness", trigger bool.

**condition combinator**:
`condAll`/`cond_all`, `condAny`/`cond_any`, or `condNot`/`cond_not`: a build-time constructor that composes runtime conditions into ordinary condition entries. `condAll` splices its operands, `condAny` and an opaque `condNot` lower through one shared flag gate, same-kind nesting flattens, `condAll(x)` and `condAny(x)` are identities, `condAll()` is true, and `condAny()` is `Never()` ([ADR 0028](./docs/adr/0028-logical-condition-combinators.md)).
_Avoid_: runtime boolean node, JS/Python `&&`/`||`/`!`, truthified condition.

**GatePlan**:
The boolean plan tree a **condition combinator** resolves to before HIR emission: `lit` (one direct condition), `and` (children spliced into the caller's condition list), `or` (one shared flag plus a clause `when` per branch), and `not_opaque` (a flag cleared when the inner condition holds). One plan maps to one flag cell so a loop's entry and tail reuse it, and a malformed or cyclic plan is `EUDSL-TYPE` ([ADR 0028](./docs/adr/0028-logical-condition-combinators.md)).
_Avoid_: condition list, runtime OR node, `gateTerms`.

**gate prelude**:
The preserved reset trigger plus clause/set `when`s emitted immediately before the trigger that consumes a flag-backed condition; it owns the extra triggers of `condAny` and opaque `condNot` and must be emitted inside a builder (`EUDSL-SCOPE` otherwise). A raw `Trigger` counts planned condition slots before emitting it, so an over-16 `condAll` splice fails with `EUDSL-BOUNDS` without leaving a flag or prelude behind ([ADR 0028](./docs/adr/0028-logical-condition-combinators.md)).
_Avoid_: hoisted gate, flag setup (when naming the whole ordered prelude).

**trigger loop / frame**:
One pass of the game engine over the active trigger list, evaluating every non-disabled trigger once; the fundamental time unit of all temporal constructs. A frame is one rendered game frame, which coincides with a trigger loop only when EUD turbo is active: the trigger-execution timer at `0x6509A0` counts down once per frame and triggers run when it reaches 0, so writing 0 there (`turbo()`) makes the loop run once per frame, while 1 gives the traditional hyper rate of once per two frames ([EUD Book: TriggerExecutionTimer](https://armoha.github.io/eud-book/offsets/TriggerExecutionTimer(Hypertriggers).html), [StarEdit: hyper triggers](http://staredit.net/topic/17533/1/)).
_Avoid_: `frame` (when referring to trigger evaluation cadence), `tick`.

**trigger (T)**:
The cost unit representing one StarCraft trigger evaluation or emitted trigger node (`1T`); always qualify it as **static cost** or **executed cost**.
_Avoid_: bare `T` without a static/executed qualifier, "trigger count".

**static cost / executed cost**:
The number of triggers a construct emits into the built map (`NT static`) versus the number of trigger evaluations per trigger loop at game time (`NT executed` or `NT/loop`).
_Avoid_: "map trigger count", "dynamic cost", bare `T`.

**Trigger**:
The first-class domain primitive for an addressable StarCraft trigger (`16C, 64A`) with 16 condition slots, 64 action slots, execution flags, and `prev`/`next` links. It is both the unit of batched execution and the backing store for `TriggerPage`s and `TriggerStruct`s.
_Avoid_: `RawTrigger`, `vartrigger`.

**classic trigger**:
A trigger in the basemap's `TRIG` section encoded as a 2400-byte record, as distinct from the 2408-byte `Trigger (16C, 64A)` payload record; the game evaluates it only when its disabled bit is clear.
_Avoid_: template trigger (when the distinct encoding matters), StarEdit trigger.

**authored vs synthetic classic trigger**:
An authored classic trigger is present in the basemap before compilation, and its enabled/disabled state is map-author state that survives compilation unchanged. A synthetic injector trigger is added by the injector during assembly, and its enabled/disabled state is injector scaffolding rather than map-author state.
_Avoid_: user trigger, template trigger, keep trigger, preserved trigger.

**stock row spec**:
The machine-readable `schema/stock-rows.json` that is the single source of truth for the 79 stock rows (22 conditions + 57 actions), the 13 eudkit extension helper rows, and the `UnitProperty` record row. The checked-in TypeScript module, Python helpers, docs tables, LSP vocabulary, and Python export manifest all derive from it ([ADR 0016](./docs/adr/0016-stock-row-spec-single-source-of-truth.md)).
_Avoid_: catalog, trigger catalog, row table, master list.

**stock trigger helper / extension target form**:
A DSL function for one of the 22 stock conditions or 57 stock actions, generated from the **stock row spec** unless it is a **special row**; it accepts stock constants except at documented extension target forms. Only `Deaths` (`player`, `amount`) and `SetDeaths` (`player`, `value`) provide one, accepting a `TriggerFieldRef` or pointer that relocates that field at assembly time; the row stays `stock`, and the target form is classed `eudkit extension`.
_Avoid_: dynamic stock helper, generic action builder, raw builder.

**special row**:
A **stock row spec** row whose helper body stays hand-written because the data emitter cannot express it: `Deaths` and `SetDeaths` (extension target forms), `CreateUnitWithProperties` (UPRP record parameter), `SetStringDword` (string-table relocation), and `UnitProperty` (UPRP record builder); at most five rows may be special.
_Avoid_: catalog entry, custom row, exception row, manual row.

**stock vocabulary**:
The numeric constants shared by stock helpers and CHK tables: `Anywhere = 64`, `All = 0`, player/force values (`0..11`, `13..21`, `26`), comparisons, and resource/score/switch/order/alliance enums.
_Avoid_: bare enum (when the value is a raw numeric constant), eudplib constant.

**eudkit extension helper**:
A helper that reuses a stock condition/action wire form for behavior outside the StarEdit rows, such as masked memory (`DeathsX`, `SetDeathsX`), fixed-address memory (`Memory`, `SetMemory`), pointer writes (`SetNextPtr`), or pre-resolved strings (`DisplayTextIndex`). It is marked `eudkit extension` in the **stock row spec** and is not a stock row; this is distinct from an **extension target form**, which is a parameter on a stock row.
_Avoid_: stock helper (for `Memory*` / `SetMemory*`), custom action (when it emits stock type 45).

**UPRP slot**:
One of 64 20-byte unit-property records in a CHK `UPRP` table, enabled by the matching `UPUS` byte; `CreateUnitWithProperties` references a 1-based slot index, and assembly preserves authored slots, dedupes identical records, and takes the lowest free slot.
_Avoid_: property index table, UPRS.

**runner boundary**:
The synthetic classic-trigger pair `TrigTriggerBegin` / `TrigTriggerEnd` that brackets the authored classic chain after stage 1 and before the synthetic blank; both are `AllPlayers`, preserved, and reset `curpl` (`0x6509B0`) to player 0 ([ADR 0010](./docs/adr/0010-classic-chain-runner-boundary.md)).
_Avoid_: runner start/end (when naming the eudkit contract), stage-3 finalizer.

**classic-first ordering**:
The chain order `stage 1 -> TrigTriggerBegin -> authored classic triggers -> TrigTriggerEnd -> synthetic blank -> payload`; authored classic triggers retain native per-chain cadence and run before the compiled payload on every trigger loop ([ADR 0010](./docs/adr/0010-classic-chain-runner-boundary.md)).
_Avoid_: payload-first (unless a future ADR supersedes 0010).

**tick gate**:
The assembly-time prelude that makes the compiled payload run once per game tick across all active player chains rather than once per chain per trigger loop; it gates only the payload, and single-chain maps may opt out ([ADR 0013](./docs/adr/0013-optional-tick-gate-for-single-chain-maps.md)).
_Avoid_: finalizer (eudplib's stage-3 bundle), per-player gate.

## Structs & Memory Views

**TriggerStruct**:
An allocated, mutable struct whose fields occupy individual `SetDeathsX` action slots (`0..63`) of a `Trigger`, giving hardware-masked field widths (`byte`, `word`, `dword`), power-of-2 field addressing, and `1T` whole-struct execution.
_Avoid_: `EUDStruct`, var-backed struct.

**MemView**:
A zero-storage typed layout schema of byte, word, and dword offsets over an `EPD` or `CP` cursor (such as `CUnit` or `CSprite`); it owns no backing memory and accesses fields directly as properties (`u.hp`).
_Avoid_: `Ptr` struct, `.field("...")` string lookup.

**CUnit**:
The built-in `MemView` over one unit's CUnit record, addressed by a unit `EPD` or a bound `CP` cursor; fields are read and written by name (`u.hp`), not by manual pointer arithmetic.
_Avoid_: unit struct, unit record pointer, `Ptr`.

**CondView / ActView**:
Built-in `MemView`s over a `Trigger`'s 20-byte condition slot (`5 EPDs`, `trig.cond(0..15)`) and 32-byte action slot (`8 EPDs`, `trig.act(0..63)`, exposing mask, destination, value, modifier, action type, flags, and EUD-X fields).
_Avoid_: raw condition or action offset arithmetic, `_varact`.

**value-field offset**:
The byte offset `20` of the `value` field inside one 32-byte `CTrigAction` record, spelled `VALUE_FIELD_OFFSET` in `eudkit-lir`; every other offset names a different action field, so `HirReloc` carries the raw offset ([ADR 0022](./docs/adr/0022-value-field-offset-single-home.md)).
_Avoid_: an `ACTION_`-prefixed name for the value field, bare `20` for the offset, "the HIR offset constant".

**relocation descriptor**:
A typed destination shape for `emitVarStore`, `AddrReloc(addr)` or `TriggerFieldReloc(trigger_id, offset=0)`, exported by both frontends. It normalizes to the legacy target object at the seam, so the HIR wire schema and emitted action bytes are unchanged; legacy mapping forms remain accepted for backward compatibility.
_Avoid_: relocation mapping, stringly-typed store target, a new wire field.

**DAT table**:
A native StarCraft data table described by `schema/layouts/dat-*.json` (`units.dat`, `weapons.dat`, `flingy.dat`, `techdata.dat`, `upgrades.dat`), structured as arrays of records so every field access is constant-indexed ([ADR 0009](./docs/adr/0009-typed-dat-accessors.md)).
_Avoid_: `UnitType` when referring to the table itself, C array.

**DAT entry / DAT symbol**:
One row of a DAT table, addressed by a constant index (`unitsDat[UnitType.Marine]`), whose field access returns a **DAT field handle**. A DAT symbol is an identifier-safe PascalCase name for that index, generated from schema display names, with a race-stripped alias (`Marine`) when unambiguous across the table ([ADR 0009](./docs/adr/0009-typed-dat-accessors.md)).
_Avoid_: DAT struct, record pointer, enum member, magic index, hard-coded `0`.

**DAT field handle**:
A `MemFieldHandle` bound to one field of one DAT entry, with its absolute address folded into an `EPD` plus byte lane at construction time so `.set`, `.iadd`, `.isub`, comparisons, and `.read` match a `MemView` at the same address.
_Avoid_: raw `SetDeathsX` at a DAT address, separate write path.

**field value space**:
The set of bit patterns a field can hold in field units, `(raw & bitMask) >> shift`; every comparison obeys it, so an out-of-space constant folds by direction instead of being silently masked or shifted. Write the width's all-ones constant (`0xFFFF`) to mean all ones; a masked `Var` uses raw lane units instead ([ADR 0017](./docs/adr/0017-field-value-space-comparisons.md), [ADR 0018](./docs/adr/0018-masked-variable-value-space.md)).
_Avoid_: field range, nominal width, "unsigned width max", mask value space.

**masked variable value space**:
The set of bit patterns a masked `Var` can hold in **raw lane units**, where the compared value is `raw & bitMask`, `shift` is not applied, and the maximum is `bitMask & 0xFFFFFFFF`. Constants fold by the field rule with `shift = 0`, a full-mask `Var` is unaffected, and the emitted condition carries the normalised constant rather than `c & bitMask` ([ADR 0018](./docs/adr/0018-masked-variable-value-space.md)).
_Avoid_: field units for a `Var`, shift-adjusted `Var` comparison, "the constant is masked before it is emitted".

**operand domain**:
The set of values a write or comparison operand may take: a plain integer (Python `int` excluding `bool`; TypeScript `number` with `Number.isInteger(number) === true`) plus the runtime forms the surface accepts, typically `Var`/`ReadonlyVar`/`Expr`. Values outside it — `bool`, non-integral numbers, `NaN`, `Infinity`, strings, and nullish values — are `EUDSL-TYPE` at the call, and magnitude is not part of the domain because integers normalise to 32 bits ([ADR 0019](./docs/adr/0019-operand-domain-validation.md)). Death-table constructors also accept relocation operands, which relocate a field instead of becoming a number.
_Avoid_: "coerced operand", "int()-compatible value", float-as-int.

## Storage & Containers

**semantic container / fixed capacity**:
A user-facing collection or value type (`TriggerStruct`, `FixedArray`, `Queue`, `Deque`, `Stack`, `UnitGroup`, `FixedString`) whose capacity is chosen at build time and never grows or reallocates at game time.
_Avoid_: `EUDVArray`, `EUDStructArray`, `StringBuffer`, `DBString`, `Db`, dynamic array, growable, runtime allocator.

**backing / backing module**:
Where a container's or variable's state resides: a `TriggerPage`, a `Cell`, a read-only `payload` blob, or an `STR` table entry. The backing module is the page/slot indexing arithmetic in `py/backing.py` / `ts/backing.ts` — carry-free EPD weights, EPD decomposition, element masks, and `PageSlotEPD` — not a storage-residency choice.
_Avoid_: "storage type", `strategy` parameter, residency, allocation.

**TriggerPage**:
A `Trigger` (`16C, 64A`) used as a 64-slot mutable storage page whose action slots hold up to 64 struct fields, array elements, or VProc variables. Inactive slots keep the self-targeted action invariant (`act[k].dest == EPD(act[k].value)` at rest), so firing the page is a no-op on untouched slots ([ADR 0004](./docs/adr/0004-triggerpage-transfer-parking-and-curpl-reset.md)).
_Avoid_: `VarBuffer`, `XVarBuffer`, 72-byte `vartrigger` slot.

**FixedArray**:
A fixed-capacity indexable container whose elements are scalars or `TriggerStruct` fields, chosen when the array size is known at build time; `FixedArray.const` is its read-only, build-time-table form.
_Avoid_: `EUDArray`, `EUDVArray`, `VarBuffer`, dynamic array.

**Cell**:
A non-trigger mutable scalar storage unit (4-byte dword or 1-bit boolean) automatically selected for scalars that never require `VProc` trigger chaining.
_Avoid_: `EUDLightVariable`, `EUDLightBool` as user-facing types.

**Var**:
A user-facing mutable handle to a Deaths-table scalar storage slot (`Cell` or `TriggerPage` slot); it must be created inside `buildModule`, and instantiating outside a builder throws fatal `EUDSL-SCOPE`. Binding assignment (`v2 = v1`) aliases the same backing slot, while `Var(v1)` or `v1.copy()` creates an independent copy at runtime.
_Avoid_: value-copy assignment, pointer wrapper, instantiating outside `buildModule`.

**in-place mutation**:
An imperative method (`v.set`, `v.iadd`, `v.isub`, `v.imul`, `v.idiv`, `v.imod`) that mutates a `Var` in place and returns `void`/`None` to prevent accidental assignment expressions. Declarative action builders (`v.act_set`, `v.act_add`, `v.act_sub`) instead construct `HirAct` records for raw trigger action arrays.
_Avoid_: chaining on imperative mutations.

**payload**:
The read-only backing that stores constant tables (`FixedArray.const`) as packed data rather than in `TriggerPage` action slots; `lir2payload` starts each item on a dword boundary (`align4`), and the 0–3 byte inter-item pad counts toward `total_bytes` but not `blob_bytes` (`StaticCost` in `crates/eudkit-lir/src/lib.rs`).
_Avoid_: legacy writable `EUDArray`, `Db`.

**UTF-8 string bytes**:
The canonical RFC 3629 byte sequence a `FixedString` stores in its STR entry; literal content in any language (Korean, Japanese, Chinese, emoji, Latin) lowers to these bytes, so `é` takes 2 bytes, `한` 3, and `🙂` 4. An embedded NUL and a lone UTF-16 surrogate are `EUDSL-TYPE`.
_Avoid_: UTF-16 storage, code-point capacity, system codepage, ANSI text.

**STR-backed / FixedString**:
A `FixedString` is a fixed-capacity text buffer whose backing is an entry of the map string table, giving it the STR identity required by `DisplayText` and `PlayWAV`; `FixedString(size)` reserves `size` content bytes plus the NUL at offset `size`. Capacity counts UTF-8 bytes rather than characters, and text that does not fit is a build-time error, never a silent truncation or split code point.
_Avoid_: `StringBuffer`, `DBString`, `Db`, mutable byte array, character capacity.

**DisplayText**:
The stock action helper that shows text to CurrentPlayer rather than every player; its text must carry STR identity, so it accepts a `FixedString` or a literal, and `DisplayTextIndex` takes a pre-resolved string id.
_Avoid_: broadcast message, chat to all players, `StringBuffer`.

**STRx**:
The 32-bit offset StarCraft: Remastered extended string table section in `scenario.chk`, replacing the 16-bit 64 KB-limited classic `STR ` section.
_Avoid_: `STR` (when referring to the Remastered 4-byte offset string table).

**module string index / wire string id**:
The module string index is the 0-based index of a string in the module's string table (`FixedString.message_index`), allocated in first-use order. The wire string id is the 1-based value a 16-bit table field such as `unitsDat[t].nameString` stores, `template_count + module string index + 1`, resolved through relocation `mode == 6`, with ids above `0xFFFF` a loud `EUDSL-BOUNDS` ([ADR 0033](./docs/adr/0033-string-id-wire-resolution.md)).
_Avoid_: string id (for the 0-based index), local string number, `message_index` (for the table value), frontend string id, `strid`.

**string-table lane**:
The 16-bit half of the 32-bit dword holding two adjacent string ids: an even entry is lane 0 (`shift 0`, mask `0x0000FFFF`) and an odd entry is lane 2 (`shift 16`, mask `0xFFFF0000`); a lane write must apply that mask or it clobbers the neighbouring entry.
_Avoid_: whole-dword name write, byte offset (for the lane bit shift).

**control character**:
An in-game text formatting byte (`0x01`–`0x1F`) in a StarCraft string specifying text color, alignment, or layout.
_Avoid_: color tag (when referring to wire byte encoding), escape code.

**aligned lane / STRING_PAD_BYTE**:
The aligned lane is the compile-time byte lane (`offset & 3`) of the next `FixedString` content byte when it can be proven; scope boundaries pad a partial tail to lane 0, while fixed-width placeholders fold constant offsets without auto-padding. `STRING_PAD_BYTE` is the zero-width `0x1A` byte used for that padding, appended after the complete UTF-8 content and never inside a code point ([ADR 0003](./docs/adr/0003-capacity-aware-aligned-string-writes.md)).
_Avoid_: "alignment slack", "+4 tail allowance", mid-string auto-padding, NUL padding, character padding.

**format spec / direct digit extraction**:
A format spec inside a `FixedString` interpolation placeholder follows `[[fill]align]width[type]`; bare `{}` defaults to decimal, types are `d` (width 1–10) and `x` (width 8), and fixed-width decimal overflows wrap modulo `10^W`. Direct digit extraction is the decimal strategy that tests binary-weighted powers of ten with direct 1-test conditions and subtract-and-accumulate actions instead of runtime division loops ([ADR 0014](./docs/adr/0014-decimal-format-default-and-fill-alignment-syntax.md)).
_Avoid_: "printf string", "format string syntax", "division ladder", "mod ladder".

**divmod / signed divmod family**:
`a.divmod(b)` / `divmod(a, b)` returns `(quot, rem)` as a pair of fresh `Var`s from one restoring ladder, while `div` and `mod` stay single-result APIs over the same helper. The signed family (`div_towards_zero`/`mod_towards_zero`, `div_floor`/`mod_floor`, `div_euclid`/`mod_euclid`) lowers sign-magnitude fixups over that ladder ([ADR 0034](./docs/adr/0034-signed-math-trig-and-binary-search.md)).
_Avoid_: "divide twice", "two-ladder divmod", using `divmod` when only one result is needed, signed `f_div` with tuple out-params.

**unlimiter**:
A core helper that replaces the game's four grow-only lists (bullets, sprites, images, orders) with lists backed by module data blobs, so the engine can allocate past its vanilla caps. `unlimiter()` arms all four with prebuilt node tables and must be called after the module's other statements; `connect_dlist` / `connectDList` builds one list.
_Avoid_: unlimiter plugin, plugin settings manifest, `onPluginStart` hook, rebuilding the lists with raw `SetMemory` payload pointers.

**data dumper (dataDumper / data_dumper)**:
`data_dumper(input_data, out_offsets, flags=())` / `dataDumper(inputData, outOffsets, flags = [])`: a shipped core helper that places a raw byte payload in a module data blob and makes target addresses learn about it. It supports `SetMemory`, `unpatchable`, and `copy` modes, registers and emits in one call, and runs as a `once` block on the first trigger loop.
_Avoid_: dataDumper plugin, `require`/`stat_txt` injection mode, unpatch bookkeeping, one blob per target.

## Temporal & Memory Primitives

**when / once / until / temporal form**:
Loop-cadence control blocks: `when` runs every trigger loop its condition holds, `once` runs on the first loop it holds and permanently self-disables, and `until` runs every loop while false and permanently disables when true. Together with `while`, `edge`, `every`, and `after` they form the seven temporal forms; all are HIR nodes, and `until`, `edge`, `every`, and `after` desugar in opt ([ADR 0006](./docs/adr/0006-hir-nodes-for-until-edge-and-counted-forms.md)). An unconditional `until` gate emits only the reset trip-when, so the body never runs.
_Avoid_: `if`/`while` for in-game cadence, timing helper, scheduler.

**dispatch**:
A frontend desugar for a one-of-N integer choice with compile-time plain-integer keys; `N <= 9` lowers to a nested `when` chain, and `N >= 10` lowers to a balanced search tree for logarithmic lookup ([ADR 0025](./docs/adr/0025-multiway-dispatch.md)). It is not a HIR node and not a dense jump table.
_Avoid_: `switch` (TypeScript reserved word and StarCraft `Switch*` collision), `match`, jump table.

**onRising / onFalling**:
Edge-triggered blocks that run on the trigger loop where `cond` transitions `false -> true` (`onRising`) or `true -> false` (`onFalling`); loop 1 counts as rising from initial 0. Each edge desugars to a reset act, a sample gate, a nested fire gate, and two previous-value updates ([ADR 0006](./docs/adr/0006-hir-nodes-for-until-edge-and-counted-forms.md)).
_Avoid_: level trigger, one-shot block.

**every / after / counted form**:
Counted-loop cadences driven by an explicit loop counter: `every(n)` runs every `n` trigger loops, and `after(n)` arms an `n`-loop countdown when reached and fires once at 0. Both desugar from HIR nodes carrying the count and a DSL-reserved counter variable, and both are distinct from `whileLoop` intra-frame repetition ([ADR 0006](./docs/adr/0006-hir-nodes-for-until-edge-and-counted-forms.md)).
_Avoid_: timer block, scheduled callback, frame loop.

**sibling `when`/`once` merge**:
An `eudkit-opt` fusion of adjacent `when` blocks, or adjacent `once` blocks, with identical condition lists into one conditional group, so the shared gate is evaluated once. Kinds never mix, bodies must be flat and free of read-after-write hazards, and gates naming absolute storage admit only provably disjoint external writes ([ADR 0001](./docs/adr/0001-trigger-16c-64a-architecture.md)).
_Avoid_: when fusion, gate dedup (condition CSE is a separate pass).

**root act/transfer interleave**:
A root action does not split a pending transfer run: `eudkit-opt` parks it while a `VarTransfer` run is open and emits it ahead of the batch, matching the grouped form's cost. An action carrying a `relocs[].target_var` may touch paged storage, so it keeps source order and ends the run; the rule is module-root only ([ADR 0001](./docs/adr/0001-trigger-16c-64a-architecture.md)).
_Avoid_: act fusion, act reorder pass.

**desugar / path-complete restore**:
Desugar replaces a high-level form with lower-level forms before lowering; each temporal node expands to `when` blocks plus DSL-reserved state Vars in opt, recursing into bodies at every depth ([ADR 0006](./docs/adr/0006-hir-nodes-for-until-edge-and-counted-forms.md)). Path-complete restore is the invariant that every gate's `next` pointer is restored on every path from body entry to join, which desugared temporal output inherits by emitting only `when` blocks.
_Avoid_: runtime expansion, HIR rewrite, partial restore, best-effort restore.

**whileLoop**:
The intra-frame runtime loop (`while_loop` in Python) that repeats its body while conditions hold within one trigger loop, with no counter; it is the sanctioned spelling of `while cond:`, whereas a language `while` on a runtime condition loops at build time and hangs ([ADR 0020](./docs/adr/0020-build-time-vs-runtime-conditions.md)).
_Avoid_: bare `while` (build-time loop), frame loop.

**HIR version**:
The `version` field of HIR JSON, exact-checked by readers; `3.2` added the embedded-file `files` list, and `3.1` added the `until`/`edge`/`every`/`after` kinds ([ADR 0021](./docs/adr/0021-mpq-embedded-file-surface.md)).
_Avoid_: schema version (for the HIR document version).

**EPD**:
The Deaths-table dword index of a 4-byte-aligned memory address, `EPD(addr) = (addr - 0x58A364) / 4`.
_Avoid_: byte address, offset.

**runtime EPD / payload blob EPD**:
A runtime EPD is an EPD whose numeric value is known only when the map payload is assembled, such as a module blob's payload address; `epd_of_db` materializes one into a fresh `Var` through a mode-1 blob relocation ([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md)). A payload blob EPD is the runtime EPD of one dword of a `Db`/`DbRef` payload, `EPD(payload_addr + byteOffset)` with `byteOffset % 4 == 0`, and `epd_of_db` is its only public constructor; both are scalar values, not runtime bases or indexed containers ([ADR 0030](./docs/adr/0030-runtime-indexed-payload-backing.md)).
_Avoid_: dynamic pointer, runtime address, blob address (for the EPD), blob pointer, mode-4 value.

**EPD-bearing value**:
A `Var` whose 32-bit value is an EPD rather than a byte address, count, or boolean; `epd_of_db` returns one, and `read_epd_pointer` also produces one. It can seed CP, index a payload dword, or be compared as an EPD, and the unit stays EPD end to end.
_Avoid_: pointer variable (when it holds an EPD), address-valued `Var`.

**CP (CurrentPlayer)**:
The CurrentPlayer address (`0x6509B0`) used as a pointer register, enabling `Deaths(13, ...)` to read or write relative to the bound `EPD`; text actions also use it as a player-index display target, so text for the local human must first set it to the user player id (`0x512684`). `with_cp` binds a constant EPD with absolute hops and restores cursor-only, while `with_epd` saves the ambient CP and restores it unconditionally with LIFO nesting ([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md) §7).
_Avoid_: player index (when naming the pointer register), global player state.

**EPD-pointer read**:
`read_epd_pointer(epd)` / `readEpdPointer(epd)` reads the dword at `epd` as a byte address, drops bits 0–1, and subtracts `EPD_BASE / 4` to return the pointed-to `EPD`. It composes the caller-reset `read_epd` bit-test read, binds no CP, and does not preserve a non-zero ambient CP.
_Avoid_: `f_epdread_epd` (when naming the eudkit helper), dereference (when the result is an EPD).

**MemCond**:
A single-condition in-place memory comparison against an `EPD` without materializing the value into a variable (`1T` when the `EPD` is constant).
_Avoid_: condition materialization, temp variable.

**memcpyEPD / memsetEPD**:
Block memory copy (`memcpyEPD(dstEPD, srcEPD, count)`) and fill (`memsetEPD(dstEPD, value, count)`) over `count` dwords; constant counts fold at build time under a static byte budget, while non-folded calls use a check-first `while_loop` that re-stores `count` on every execution. `memcpyEPD` seeds CurrentPlayer from the source and advances it per read, and both primitives land each dword through one scratch pointer.
_Avoid_: unbounded unroll, per-call runtime decoder.

**isqrt**:
Runtime integer square root `isqrt(value, value_bits=32, fixed_bits=0)`, returning a `Var` for `floor(sqrt(value) * 2**fixed_bits)`; its step count is fixed by the declared widths, and a constant folds at build time. `value` accepts `int`, `Var`, `ReadonlyVar`, and `Expr`, and the caller owns the `value < 2**value_bits` bound ([ADR 0019](./docs/adr/0019-operand-domain-validation.md)). Used by **camera_follow**.
_Avoid_: `math.sqrt`, floating-point root, "divide by two until it converges".

**camera_follow (cameraFollow)**:
The per-loop camera-follow helper from euddraft's `cammove.py` that moves a scratch location onto a P9 Map Revealer inside the target location, eases the camera box toward it with fixed-point smoothing, and recenters the view. It runs when multiplayer mode is active and the caller's switch is set, writes `CurrentPlayer` from the user player id before `CenterView` and restores `0` afterward ([ADR 0004](./docs/adr/0004-triggerpage-transfer-parking-and-curpl-reset.md)), and resets `lastTick` when the switch clears so re-enabling snaps. `inertia == 0` is a build-time branch that omits smoothing, and the helper returns `CameraFollowState` (`last_tick`, `screen_x`).
_Avoid_: camera lock, view lerp, "paused-game no-op" (the gate is multiplayer mode).

## Unit Iteration

**Status:** `list walk`, **tracked group** (`UnitGroup.for_each`, unrolled default), **compact mode**, and the **runtime-loop API** are current ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decision 7); `slot scan`, `density crossover`, and `new-unit snapshot` are planned, not implemented.

**slot scan / density crossover**:
A slot scan walks all 1699 fixed CUnit slots and checks liveness per slot, with roughly 4T static plus the body and roughly 3T × 1699 executed per invocation regardless of live count. The density crossover is the live-unit count (roughly 130–150) at which a list walk's linear cost overtakes that fixed cost; both are planned, not implemented ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md)).
_Avoid_: unit array scan, `EUDLoopUnit2` (when naming the strategy), "1700 static triggers", break-even threshold, magic unit count.

**list walk**:
A unit-iteration strategy that follows `nextUnit` pointers from a list head; the reference loop costs roughly 35–40T executed per live node, which is per-node loop overhead rather than the pointer read alone. The implemented bits-0–19 CUnit pointer reader costs 20T executed per read ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §9), and `for_each_unit_list` / `forEachUnitList` covers `U`/`T`/`S`/`D`; slot-scan and new-unit siblings remain planned.
_Avoid_: pointer chase, linked-list loop.

**new-unit snapshot**:
A one-time startup table aligned with the 1699 CUnit free-list slots, used to detect units created after initialization; it costs about 558 KiB per call site unless shared. Planned, not implemented ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md)).
_Avoid_: shadow array, seen bitmap.

**tracked group / compact mode**:
A tracked group is a `UnitGroup` with explicitly added and removed membership whose default `for_each` unrolls the body per slot for small tracked sets. Compact mode uses a runtime CurrentPlayer cursor sweep instead, emitting the body once as a static-size optimization rather than a speed optimization ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md)).
_Avoid_: unit registry, all-units group, packed group, compact table.

**runtime-loop API**:
A loop primitive that emits its body once and iterates at game time, as opposed to a compile-time unroll that duplicates the body per slot.
_Avoid_: unrolled loop, generator loop.

**free list / CCMU**:
The free list is the engine's linked list of vacant CUnit slots, headed at `0x628438` (`CUNIT_FREE_HEAD`, EPD 161845) and tailed at `0x62843C` (`CUNIT_FREE_TAIL`), with nodes using the CUnit `prev`/`next` links at the 336-byte stride. CCMU ("Can't Create More Units") is the engine state where no CUnit slot is available and a create order fails, so it is a capacity condition rather than a player or command structure; `free_list_empty` is its exact 1-condition truth.
_Avoid_: treating `F` as a unit list, `0x6283F0` (that is the unit count, not a list head or CCMU truth), `cannot` count.

**free_list_empty / freeListEmpty**:
The 1-condition, 0-read capacity predicate `Memory(0x628438, Exactly, 0)`: the free-list head is null, so the engine has no free CUnit slot right now. It returns an ordinary condition rather than a wrapper `Trigger`, so it composes into a 16-condition gate.
_Avoid_: free-slot count, `1700 - 0x6283F0`, inserting a gate silently.

**capacity tier (1/2/3)**:
The three ways to answer "is there room for another unit": tier 1 is the exact 1-condition free-list head test, tier 2 is a deferred tracker-derived upper bound `1700 - (|U|+|T|+|S|+|D|)` that over-reports, and tier 3 is the exact one-shot free-list walk at roughly 37T per free node, once at map start or on author request, never per tick ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §10).
_Avoid_: calling the counter form tier 1, an exact count without the walk.

**free-slot count / one-shot scan**:
`free_slot_count()` / `freeSlotCount()` returns the exact number of free CUnit nodes as a `Var`, sharing one call-site budget with `free_list_walk` per module; a second call is `EUDSL-BOUNDS`, while `free_list_empty()` does not count. `1699 - free_slot_count()` is the walk-derived upper bound and deliberately has no API name ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §10).
_Avoid_: per-frame free count, `1700 - 0x6283F0`, `slotsUpperBound` (not shipped), advisory once, automatic runtime gating.

**cycle guard**:
The `remaining` counter in the tier-3 walk, set to `CUNIT_FREE_SCAN_MAX` (1699) and decremented once per node; at zero the walk sets `done` and stops, so a cycle in a corrupted free list costs at most 1699 nodes instead of hanging the game. It is the third termination condition, beside a null pointer and an out-of-pool pointer, and never mutates game memory.
_Avoid_: sentinel node, list rewrite, infinite-loop watchdog.

**advisory capacity check**:
A capacity signal that answers exactly the question it reads and no more: `free_list_empty` is advisory, not authoritative, because the allocator can still refuse a creation for reasons the head dword cannot see. An empty list means "try", not "this will succeed", and a non-empty list is not a slot count.
_Avoid_: capacity guarantee, authoritative free count.

**CUnit pool / shifted pointer space `S`**:
The CUnit pool is the game's fixed array of 1699 CUnit slots starting at `0x59CCA8`, 336 bytes (84 dwords) apart, with slot starts in `S` over `[0x16732A, 0x18A052]` and EPDs over `[19025, 161657]`. The shifted pointer space `S = A >> 2` divides the dword-aligned byte address by four; for a slot start, bits 0 and 1 are invariant and bit 20 is always 1, so bits 2–19 recover the slot index ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §9). Note: this 1699-slot count is the repo's model (`py/view.py`, `ts/view.ts`); BWAPI accepts unit indices `0 <= unitIndex < 1700` ([BWAPI `Game`](https://bwapi.github.io/class_b_w_a_p_i_1_1_game.html)) and the euddraft wiki states 1700 units spanning `0x59CCA8`–`0x6283E7` ([euddraft wiki](https://github-wiki-see.page/m/phu54321/euddraft/wiki/05.-Selecting-a-pawn)), so slot 1699 must not be treated as free scratch (see `examples/py/07_block_io_and_data.py`).
_Avoid_: unit array, CUnit heap, slot table, byte address, EPD for `S`, unqualified pointer space.

**CP-relative read**:
A dword read whose condition target is `CURRENT_PLAYER`, so the engine reads the EPD currently held in the CP register rather than a constant address. `read_cunit_pointer(Var)` binds CP through `with_epd` and restores it, while `read_cunit_pointer_saved` is the loop form that saves the ambient CP once and advances it per node ([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md) §5).
_Avoid_: indirect read, dynamic address read, CP hop.

**null-pointer sentinel / `in_cunit_pool`**:
The null-pointer sentinel is the value the CUnit pointer reader yields for a null pointer, `0xFFF9D729`, which is above `CUNIT_LAST_EPD` and therefore rejected by the pool check's upper bound rather than its lower bound ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §9). `in_cunit_pool(epd)` / `inCunitPool(epd)` returns `[epd.ge(CUNIT_BASE_EPD), epd.le(CUNIT_LAST_EPD)]` as two conditions that splice into a loop gate, and it never raises, so a hijacked list stays diagnosable.
_Avoid_: null EPD, end-of-list marker, assert-in-pool, `EUDSL-BOUNDS` for a dead list, wrapping the check in a trigger.

**unit list (U/T/S/D)**:
One of the four global CUnit lists walked by `for_each_unit_list` / `forEachUnitList`: `UNIT_LIST_U` (in use), `UNIT_LIST_T` (hidden), `UNIT_LIST_S` (revealers), and `UNIT_LIST_D` (dying). `U`/`T`/`S` walk forward from `next`, while `D` walks backward from the tail through `prev` because a forward `D` walk silently misses every death; the free list `F` is not a unit list.
_Avoid_: unit array, `EUDLoopUnit` (when naming the eudkit surface), forward `D` walk.

**reentrant walk**:
A `for_each_unit_list(..., reentrant=True)` / `forEachUnitList(..., { reentrant: true })` walk that reads the successor link before running the body, so a body that frees the current CUnit still reaches the next node. The default reads the link after the body and stops after one iteration if the body frees that node.
_Avoid_: safe loop, pre-fetch flag, "retry the walk".

**`Break` sentinel**:
The named `BREAK` singleton (`Break()` in Python, `BREAK`/`Break` in TypeScript) returned from a unit-list body to stop the walk after the current node; it is checked by identity, never by truthiness. `loop.break_()` / `loop.breakLoop()` is the imperative form and emits one `done.set(1)`, so callers gate it at runtime.
_Avoid_: boolean break, truthy return, `Continue` sentinel.

## Player Names

**player name record / nickname matcher (layout A)**:
A player name record is one of twelve 36-byte (9-dword) records at `0x57EEE8`, holding a nickname as UTF-8 bytes plus its NUL; the table is below `EPD_BASE`, so `player_name_epd(p) = (EPD(0x57EEE8) + 9*p) & 0xFFFFFFFF` is an ordinary wrapped EPD, and a name encodes to at most 35 bytes. `match_names` / `matchNames` is a build-time unroll that emits one ordered `when` chain per player and compares the record dword by dword with `DeathsX`, with the final mask including the NUL so matching is exact ([ADR 0032](./docs/adr/0032-player-name-matching.md)).
_Avoid_: player struct, name string table, name dispatch, prefix match, substring match, layout C.

## Lazy Declarations

**Lazy declaration**:
A handle created without a builder (`lazyVar`, and the later `lazy*` family) that is bound on first resolving use inside `buildModule`; lazy declarations live at module scope. Declaring one inside `buildModule` triggers the compile-time warning `EUDSL-WARN-SCOPE`: `lazy* declared inside buildModule; use eager Var for in-module declarations`. Declaration is global but allocation is per map, so the same handle resolves to a different binding in each map ([ADR 0005](./docs/adr/0005-lazy-global-declarations.md)).
_Avoid_: `global*` naming for lazy handles, declaring `lazy*` inside `buildModule` (use eager `Var`/containers), god `globals.py` files, direct `.set()`/`.iadd()` mutations from external consumer files.

**Binding (per-builder) / binding-derived object**:
A binding is the fully eager instance a lazy handle resolves to under one builder; bindings live and die with their builder, and a handle holds one binding per builder. A binding-derived object — an element var, a struct field, or an out-box — is builder-scoped: handles may cross builders, but binding-derived objects may not (a documented rule with no runtime enforcement).
_Avoid_: describing a binding without its builder, using a binding-derived object in a second module.

**deferred binding**:
The per-builder resolution state machine behind every lazy handle: eager fast path, active-builder lookup, cached binding, `instantiate` thunk, binding storage, resolution count, first-binding capture, and post-build zero/one/many `EUDSL-SCOPE` errors ([ADR 0005](./docs/adr/0005-lazy-global-declarations.md) decision 7). It is the shared `LazyBinding` helper for all eight lazy types — `Var`, struct, `FixedArray`, `FixedString`, `Queue`, `Deque`, `Stack`, and `UnitGroup` — with per-type instantiate thunks and diagnostic labels.
_Avoid_: deferred declaration, per-map singleton, global binding.

**First-use order / declaration order**:
Within one builder, bindings are created in first-use order, and allocation ids, page ranks, string-table indices, and flusher registration order all follow it. Declaration order across or within files does not determine allocation order; to force a rank order, touch globals in order at the top of the `buildModule` body.
_Avoid_: "declaration order" as the allocation order, relying on declaration order for ranks or ids.

## Build & Packaging

**embedded file**:
A build-time file recorded on the compiled module by `embedFile` / `embed_file` and added to the output MPQ under its archived name, the `MPQAddFile` equivalent. HIR stores only `{name, path}`, and the CLI resolves a relative path against the directory of the compiling source; the same surface serves the periodic-BGM helper ([ADR 0021](./docs/adr/0021-mpq-embedded-file-surface.md)).
_Avoid_: MPQ asset, plugin file, resource, bundled blob.

**periodic BGM helper**:
`play_bgm` / `playBgm(path, length_seconds)`: a core helper that embeds the track as `bgm` and arms a preserved probe plus a `when` gate that plays it for the local player on the first trigger loop and every `length_seconds` thereafter.
_Avoid_: bgm plugin, music plugin, settings manifest, onPluginStart hook.

**soundlooper helper**:
A port of euddraft's `soundlooper`: `SoundLoops` scans `<title>/<title>NNN.ogg` segments, reads each Ogg length, embeds the segments under a five-character archive id, and `soundLooper().player()` plays them as an intro / bar / bridge loop for the local player. Per-bar dispatch uses a `when` chain ([ADR 0023](./docs/adr/0023-soundlooper-loop-player.md) §7), a deliberate exception to [ADR 0025](./docs/adr/0025-multiway-dispatch.md)'s `dispatch` preference, and segments use `embedFile` rather than a `Db` blob because `PlayWAV` resolves an archived name.
_Avoid_: soundlooper plugin, sound settings, random archive ids.

**noAirCollision helper**:
A port of euddraft's `noAirCollision`: 115 forward triggers hold 7,311 `SetMemory` actions that zero the game's collision table on every trigger loop. A first-loop `once` block reads the table's base EPD and rewrites every action destination across the fixed 602-EPD stride ([ADR 0024](./docs/adr/0024-noaircollision-scrubber-helper.md)).
_Avoid_: air-collision plugin, dummy action, collision mask, hitbox helper.

**Q16 trig payload table / exponentiation-by-squaring / binary classification search**:
The Q16 (`65536` scale) payload tables cache `atan2_256`/`atan2`, `lengthdir`, and `lengthdir_256` as `Db` blobs materialized once per builder from `ConstFixedArray` specs and indexed via `with_epd`. Exponentiation-by-squaring lowers compile-time-exponent `pow(base, exp, bits)` through MSB-first `Var.imul` sites, and `binary_min` / `binary_max` run `(hi - lo + 1).bit_length()` halving steps over monotone condition-only predicates ([ADR 0034](./docs/adr/0034-signed-math-trig-and-binary-search.md)).
_Avoid_: rebuilding trigonometric `Db` blobs per call site, runtime exponent loop, repeated multiply chain, linear scan, binary search over a materialized array.

**LCG state / `dwrand` word assembly**:
A per-builder private `Var` updated by `state = state * 1103515245 + 12345`, with a `HIWORD` draw for `rand` and two packed draws for `dwrand`, seeded deterministically via `GAME_TICK_ADDR` in `randomize()` ([ADR 0035](./docs/adr/0035-per-builder-lcg-prng.md)).
_Avoid_: process-global seed variables, `Switch` rewiring in `randomize`.

**player state accessor / `PVariable` / player broadcast loop**:
`Player(0..11)` (`PlayerField` views over SC:R player arrays), the 8-slot `PVariable` (`FixedArray(arrayDword, 8)`), `turbo()` (`0x6509A0 = 0`), arithmetic `player_exists(0..7)`, the `for_each_player` loop (`0..7` or unrolled subset without touching `CurrentPlayer`), and six local-user `*_all` broadcasts ([ADR 0036](./docs/adr/0036-player-state-pvariable-and-broadcasts.md)).
_Avoid_: per-player hand-written loops, player-index magic numbers.

**location handle / 5-dword location record / build-time geometry**:
`Location` wraps a 5-dword record at `0x58DC4C` (`left`, `top`, `right`, `bottom`, with dword 4 untouched), with build-time-clamped geometry (`draw_circle`, `draw_line`, `draw_polygon`, `draw_star`) and raw `0..255` integer ids across module boundaries ([ADR 0037](./docs/adr/0037-location-handles-and-geometry.md)).
_Avoid_: location id wrapper, runtime geometry computation.

**tech state slot / upgrade state slot / SC/BW stride split**:
Split SC/BW byte tables for tech and upgrade state (tech `0x58CF44`/24 vs `0x58F140`/20; upgrade `0x58D2B0`/46 vs `0x58F32C`/15) lowered to masked `DeathsX`/`SetDeathsX` and 4-lane `with_epd` dispatch ([ADR 0038](./docs/adr/0038-tech-and-upgrade-state-tables.md)).
_Avoid_: single-table assumption, per-race hard-coded strides.

**`CUnitView` / condition-EPD rule / `ObjectPool` / `POOL_NULL` sentinel**:
A typed facade over `CUnit` and `CSprite.player` taking raw location ids, Rule V1 forbidding runtime EPDs in trigger conditions, and a deterministic LIFO `ObjectPool` over `Stack` + `FixedArray` with `POOL_NULL = 0xFFFFFFFF` ([ADR 0039](./docs/adr/0039-cunit-view-bit-helpers-and-object-pool.md)).
_Avoid_: raw CUnit pointer surgery, runtime EPD in a condition, shared mutable pool slots.
