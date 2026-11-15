# eudkit

`eudkit` is a compiler toolchain and unified TypeScript/Python DSL for authoring StarCraft: Remastered EUD maps around the native `Trigger (16C, 64A)` execution and storage model.

## Execution & Trigger Model

**eudkit**:
The unified toolchain, compiler CLI (`eudkit compile`, `eudkit dump-ir`), and crate family (`eudkit-hir`, `eudkit-opt`, `eudkit-lower`, `eudkit-lir`, `eudkit-map`, `eudkit-lsp`).
_Avoid_: `eudc`, `eudc-*`, `eudplib`.

**buildModule**:
The HIR construction entry: a function that runs once at compile time and returns a `CompiledModule`. It is not the CLI capture hook; the root source hands its result to the CLI as `module`.
_Avoid_: `onPluginStart`, `beforeTriggerExec`, `afterTriggerExec`, capture hook.

**module (root source entry)**:
The single compiled module the root source exports (TypeScript
`export const module = buildModule(...)`) or assigns at module scope (Python
`module = build_module(prog)`). `synth` / `dump-ir` / `compile` read only this root-namespace entry;
helper modules may build or export their own modules but cannot provide it
([ADR 0012](./docs/adr/0012-explicit-entry-module-export.md)).
_Avoid_: last module, capture hook, any exported module.

**build-time macro**:
A TypeScript or Python function (including `buildModule` and callbacks passed to `when`/`once`) that
executes once at compile time to construct the High-level Intermediate Representation (HIR) trigger
tree. Build-time macros never run inside StarCraft, have no in-game call stack or local frames, and
cannot be called at game time. Repetition and control flow in-game occur strictly through trigger
loops walking emitted triggers.
_Avoid_: runtime function call, in-game stack frame.

**build-time condition**:
A Python or TypeScript value evaluated while `buildModule` runs — a plain `bool`, a loop counter, a comparison of constants — which may drive a build-time `if`/`while`. It decides which HIR is emitted and does not describe an in-game test (contrast **runtime condition**).
_Avoid_: runtime cond, in-game bool.

**runtime condition**:
A HIR condition record describing an in-game test (`v.eq(1)`, a field read, `Never()`). In py it is
a `HirCond`; using one as a Python `bool` (in `if`/`while`/`assert`/`and`/`or`/`not`) raises
`EUDSL-COND-TRUTHIFY` ([ADR 0020](./docs/adr/0020-build-time-vs-runtime-conditions.md)). Branch
in-game with `when`/`once`/`while_loop` instead, and combine conditions with a **condition
combinator** rather than the language's boolean operators.
_Avoid_: "cond truthiness", trigger bool.

**condition combinator**:
`condAll`/`cond_all`, `condAny`/`cond_any`, or `condNot`/`cond_not`: a build-time constructor that
composes runtime conditions while keeping the runtime surface (ordinary condition entries).
`condAll` splices its operands into the consuming condition list; `condAny` and an opaque `condNot`
lower through one shared flag gate. Same-kind nesting flattens before planning, `condAll(x)` and
`condAny(x)` are identities, `condAll()` is true, and `condAny()` is `Never()`
([ADR 0028](./docs/adr/0028-logical-condition-combinators.md)).
_Avoid_: runtime boolean node, JS/Python `&&`/`||`/`!`, truthified condition.

**GatePlan**:
The boolean plan tree a **condition combinator** resolves to before HIR emission: `lit` (one direct
condition), `and` (children spliced into the caller's condition list), `or` (one shared flag plus a
clause `when` per branch), and `not_opaque` (a flag initialized true and cleared when the inner
condition holds). A `PlanContext` maps each plan to one flag so a `whileLoop`'s entry and tail reuse
the same cell; a malformed or cyclic plan is `EUDSL-TYPE`.
_Avoid_: condition list, runtime OR node, `gateTerms`.

**gate prelude**:
The preserved reset trigger plus clause/set `when`s emitted immediately before the trigger that
consumes a flag-backed condition. It owns the extra triggers of `condAny` and opaque `condNot`,
must be emitted inside a builder (`EUDSL-SCOPE` otherwise), and a raw `Trigger` counts planned
condition slots before emitting it so an over-16 `condAll` splice fails with `EUDSL-BOUNDS` without
leaving a flag or prelude behind.
_Avoid_: hoisted gate, flag setup (when naming the whole ordered prelude).

**trigger loop**:
One pass of the game engine over the active trigger list, evaluating every non-disabled trigger once; the fundamental time unit of all temporal constructs.
_Avoid_: `frame` (when referring to trigger evaluation cadence), `tick`.

**frame**:
One rendered game frame, which coincides with a trigger loop only when EUD turbo is active.

**trigger (T)**:
The cost unit representing one StarCraft trigger evaluation or emitted trigger node (`1T`). Always qualify as either **static cost** or **executed cost**.
_Avoid_: bare `T` without a static/executed qualifier, "trigger count".

**static cost**:
The number of triggers a construct emits into the built map (`NT static`).
_Avoid_: "map trigger count", bare `T`.

**executed cost**:
The number of trigger evaluations per trigger loop at game time (`NT executed` or `NT/loop`).
_Avoid_: "dynamic cost", bare `T`.

**Trigger**:
The first-class domain primitive representing an addressable StarCraft trigger (`16C, 64A`) with 16 condition slots, 64 action slots, execution flags, and `prev`/`next` links. Serves as both the unit of batched execution and the backing store for `TriggerPage`s and `TriggerStruct`s.
_Avoid_: `RawTrigger`, `vartrigger`.

**classic trigger**:
A trigger in the basemap's `TRIG` section encoded as a 2400-byte record (16 conditions x 20 B + 64
actions x 32 B + player/flag fields), as distinct from the 2408-byte `Trigger (16C, 64A)` payload
record. The game evaluates a classic trigger only when its disabled bit (mask `0x8` in the flag
dword at byte 2368) is clear.
_Avoid_: template trigger (when the distinct encoding matters), StarEdit trigger.

**authored classic trigger**:
A classic trigger present in the basemap before compilation, as distinct from one the injector synthesizes during assembly. Its enabled/disabled flag is the map author's state and survives compilation unchanged.
_Avoid_: user trigger, template trigger.

**synthetic injector trigger**:
A classic trigger added by the injector during assembly rather than authored in the basemap. Its enabled/disabled state is injector scaffolding, not map-author state.
_Avoid_: keep trigger, preserved trigger.

**stock row spec**:
The machine-readable `schema/stock-rows.json` that is the single source of truth for the 79 stock
rows (22 conditions + 57 actions) and the 13 eudkit extension helper rows, plus the `UnitProperty`
record row. It fixes each row's id, CHK type, class, default flags, ordered parameters with CHK
fields, validators and encodings, and documentation; the checked-in TypeScript module, import-time
Python helpers, docs tables, LSP vocabulary, and Python export manifest all derive from it. Adding a
row is a spec edit plus regeneration.
_Avoid_: catalog, trigger catalog, row table, master list.

**stock trigger helper**:
A DSL function for one of the 22 StarEdit stock conditions or 57 stock actions. Every helper is one
row in the **stock row spec**, which fixes its CHK type, field placement, default flags, parameter
order, and validators; the body is generated from that row unless it is a **special row**. It
accepts stock constants for every parameter, except the documented **extension target forms** of
`Deaths` and `SetDeaths` (`player` plus `amount`/`value`), whose field-reference/pointer targets
relocate those CHK fields. Other dynamic values use raw builders or **eudkit extension helpers**.
_Avoid_: dynamic stock helper, generic action builder.

**extension target form**:
A parameter position on a stock row that accepts a `TriggerFieldRef` or pointer in addition to a
stock constant. Only `Deaths` (`player`, `amount`) and `SetDeaths` (`player`, `value`) provide one;
the target relocates the condition/action field to the referenced trigger slot at assembly time. The
row stays `stock`; the target form is classed `eudkit extension`.
_Avoid_: dynamic stock helper (for the whole row), raw builder.

**special row**:
A **stock row spec** row whose helper body stays hand-written because the data emitter cannot
express it: `Deaths` and `SetDeaths` (extension target forms), `CreateUnitWithProperties` (UPRP
record parameter), `SetStringDword` (string-table relocation), and `UnitProperty` (UPRP record
builder). At most five rows may be special; the freshness tests fail on a sixth.
_Avoid_: catalog entry, custom row, exception row, manual row.

**stock vocabulary**:
The numeric constants shared by the stock helpers and the CHK tables: `Anywhere = 64`, `All = 0`, player/force values (`0..11`, `13..21`, `26`), comparisons, and resource/score/switch/order/alliance enums.
_Avoid_: bare enum (when the value is a raw numeric constant), eudplib constant.

**eudkit extension helper**:
A helper that reuses a stock condition/action wire form for behavior outside the StarEdit rows, such
as masked memory (`DeathsX`, `SetDeathsX`), fixed-address memory (`Memory`, `SetMemory`), pointer
writes (`SetNextPtr`), or pre-resolved strings (`DisplayTextIndex`). Marked `eudkit extension` in
the **stock row spec**; not a stock row. Distinct from an **extension target form**, which is a
parameter form on a stock `Deaths`/`SetDeaths` row.
_Avoid_: stock helper (for `Memory*` / `SetMemory*`), custom action (when it emits stock type 45).

**UPRP slot**:
One of 64 20-byte unit-property records in a CHK `UPRP` table, enabled by the matching `UPUS` byte.
A module `CreateUnitWithProperties` action references a 1-based slot index; assembly preserves
authored slots, dedupes identical records, takes the lowest free slot, and fails on overflow. The
record itself is built by `UnitProperty`.
_Avoid_: property index table, UPRS.

**runner boundary**:
The synthetic classic-trigger pair `TrigTriggerBegin` / `TrigTriggerEnd` that brackets the authored
classic chain. `Begin` runs after stage 1 and before the first authored trigger; `End` runs after
the last authored trigger and before the synthetic blank. Both are `AllPlayers`, preserved, and
reset `curpl` (`0x6509B0`) to player 0. The pair defines **classic-first ordering**
([ADR 0010](./docs/adr/0010-classic-chain-runner-boundary.md)).
_Avoid_: runner start/end (when naming the eudkit contract), stage-3 finalizer.

**classic-first ordering**:
The chain order
`stage 1 -> TrigTriggerBegin -> authored classic triggers -> TrigTriggerEnd -> synthetic blank -> payload`.
Authored classic triggers retain native per-chain cadence and run before the compiled payload on
every trigger loop. The first-loop order is part of the contract, not a divergence
([ADR 0010](./docs/adr/0010-classic-chain-runner-boundary.md)).
_Avoid_: payload-first (unless a future ADR supersedes 0010).

**tick gate**:
The assembly-time prelude that makes the compiled payload execute once per game tick across all
active player trigger chains, rather than once per chain per trigger loop. It gates the payload
only; classic triggers retain native per-chain cadence. On maps explicitly built for a single active
chain it may be omitted (`--single-player` / `--no-tick-gate`); the payload then runs at the chain's
native once-per-trigger-loop cadence
([ADR 0013](./docs/adr/0013-optional-tick-gate-for-single-chain-maps.md)).
_Avoid_: finalizer (eudplib's stage-3 bundle), per-player gate.

## Structs & Memory Views

**TriggerStruct**:
An allocated, mutable struct whose fields occupy individual `SetDeathsX` action slots (`0..63`) of a `Trigger`, providing hardware-masked field widths (`byte`, `word`, `dword`), power-of-2 field addressing, and `1T` whole-struct execution.
_Avoid_: `EUDStruct`, var-backed struct.

**MemView**:
A zero-storage typed layout schema of byte, word, and dword offsets over an `EPD` or `CP` cursor (such as `CUnit` or `CSprite`). It owns no backing memory and accesses fields directly as properties (`u.hp`).
_Avoid_: `Ptr` struct, `.field("...")` string lookup.

**CUnit**:
The built-in `MemView` over one unit's CUnit record, addressed by a unit `EPD` or through a bound `CP` cursor. Fields are read and written by name (`u.hp`), not by manual pointer arithmetic.
_Avoid_: unit struct, unit record pointer, `Ptr`.

**CondView**:
The built-in `MemView` over a `Trigger`'s 20-byte condition slot (`5 EPDs`), accessed via `trig.cond(0..15)`.
_Avoid_: raw condition offset arithmetic.

**ActView**:
The built-in `MemView` over a `Trigger`'s 32-byte action slot (`8 EPDs`), accessed via `trig.act(0..63)`, exposing `.mask`, `.dest`, `.value`, `.modifier`, `.acttype`, `.flags`, and `.eudx`.
_Avoid_: raw action offset arithmetic, `_varact`.

**value-field offset**:
The byte offset `20` of the `value` field inside one 32-byte `CTrigAction` record, spelled
`VALUE_FIELD_OFFSET` in `eudkit-lir` beside `DEST_FIELD_OFFSET` (`16`), `MASK_FIELD_OFFSET` (`0`)
and `MODIFIER_DWORD_OFFSET` (`24`). A `HirReloc` whose `target_offset` is the value-field offset is
a plain variable access, which is exactly what `eudkit_opt::cell::is_value_field_offset` tests;
every other offset names a different action field. `HirReloc` carries the raw offset on purpose, and
the raw `16` / `20` literals that reach HIR JSON are authored by the frontends and mirrored there in
`ACT_FIELD_OFFSETS` (`py/stock_rows_built.py`, `ts/stock-spec.ts`) — not in `eudkit-hir`
([ADR 0022](./docs/adr/0022-value-field-offset-single-home.md)).
_Avoid_: an `ACTION_`-prefixed name for the value field (review #3 invented one; no such symbol exists), bare `20` for the offset, "the HIR offset constant".

**relocation descriptor**:
A typed destination shape for `emitVarStore`, shipped in FE3 and exported by both frontends:
`AddrReloc(addr)` in `py/hir.py` / `ts/hir.ts`, or `TriggerFieldReloc(trigger_id, offset=0)` in
Python and `TriggerFieldReloc(triggerId, offset?)` in TypeScript. The descriptor normalises to the
existing legacy target object at the seam, so the HIR wire schema and emitted action bytes are
unchanged. All in-tree production modules and examples construct these descriptors, while the legacy
mapping forms (`{addr}` / `{trigger_id, offset}` in Python and `{addr}` / `{triggerId, offset}` in
TypeScript) remain accepted by the normalization seam for backward compatibility and existing test
fixtures.
_Avoid_: relocation mapping, stringly-typed store target, a new wire field.

**DAT table**:
A native StarCraft data table described by `schema/layouts/dat-*.json` (`units.dat`, `weapons.dat`, `flingy.dat`, `techdata.dat`, `upgrades.dat`). DAT tables are struct-of-arrays: field `f` of entry `i` lives at `base(f) + i * stride(f)`, so every access is constant-indexed.
_Avoid_: `UnitType` when referring to the table itself, C array.

**DAT entry**:
One row of a DAT table, addressed by a constant index (`unitsDat[UnitType.Marine]`, `units_dat[UnitType.Marine]`). Field access returns a `DatFieldHandle`.
_Avoid_: DAT struct, record pointer, `CUnit`.

**DAT symbol**:
An identifier-safe PascalCase name for a DAT entry index, generated from the schema display names. Exact names always map (`Terran Marine` -> `TerranMarine`); race-prefixed names also get a stripped alias (`Marine`) only when it is unambiguous across the table.
_Avoid_: enum member, magic index, hard-coded `0`.

**DAT field handle**:
A `MemFieldHandle` bound to one field of one DAT entry. Its absolute address is folded into an `EPD` plus byte lane at construction time, so `.set`, `.iadd`, `.isub`, comparisons, and `.read` match a `MemView` at the same address.
_Avoid_: raw `SetDeathsX` at a DAT address, separate write path.

**field value space**:
The set of bit patterns a field can hold, in field units: `(raw & bitMask) >> shift`. Every
comparison obeys it — `read`, `eq`, `ne`, `ge`, `le`, `gt`, `lt` — so a constant outside the space
folds by direction instead of being silently masked or shifted: `eq` never holds and `ne` always
holds, and the orderings fold `ge(c > max) -> Never`, `le(c > max) -> Always`,
`gt(c >= max) -> Never`, `lt(0) -> Never`, `lt(c > max) -> Always`. A *straddling* declaration (a
dword at byte 2, a word at byte 3) has `mask >> shift` as its largest value, not the nominal width;
`max = (bitMask & 0xFFFFFFFF) >> shift` is exact for every field. Constants are normalised to 32
bits before the test, so `word.eq(-1)` (`0xFFFFFFFF`) is `Never()`; write the width's all-ones
constant (`0xFFFF`) to mean all ones ([ADR 0017](./docs/adr/0017-field-value-space-comparisons.md),
field value space). This is a breaking change from pre-#21 behaviour. For a masked `Var` the same
rule applies in raw lane units (`shift = 0`); see **masked variable value space**
([ADR 0018](./docs/adr/0018-masked-variable-value-space.md)).
_Avoid_: field range, nominal width, "unsigned width max", mask value space.

**masked variable value space**:
The set of bit patterns a masked `Var` can hold, in **raw lane units**: the compared value is
`raw & bitMask` and the space is the bit patterns of `bitMask` as-is, so
`max = bitMask & 0xFFFFFFFF`. `shift` is a field-surface concept and is **not** applied to
struct-field `Var`s — `TriggerStruct` fields become `Var(init, explicit_mask=mask)` and drop the
`FieldSpec.shift` — which matches how `set`, `iadd`, and struct init already mask: `bit(3, 5)` has
mask `0xE0`, so `f.eq(1)` asks for `raw & 0xE0 == 1` and is `Never()`; name a representable value
(`0x20`) instead. Constants fold by the same rule as a field with `shift = 0`: `eq` never holds and
`ne` always holds for a constant with a bit outside the mask (the subset test), and the orderings
fold `ge(c > max) -> Never`, `le(c > max) -> Always`, `gt(c >= max) -> Never`, `lt(0) -> Never`,
`lt(c > max) -> Always` (the numeric test). Constants are normalised to 32 bits before the test, so
`Var(explicit_mask=0xFF).eq(-1)` is `Never()`; write the mask's all-ones pattern (`0xFF`) to mean
all ones. A full-mask (`0xFFFFFFFF`) `Var` is unaffected. The emitted condition carries the
normalised constant, never `c & bitMask`
([ADR 0018](./docs/adr/0018-masked-variable-value-space.md)). A comparison reads the left operand as
`raw & bitMask`; the `ne` middle-constant diff temp reads it the same way. A **variable** right
operand is compared against the **raw lane** by decision
([ADR 0018](./docs/adr/0018-masked-variable-value-space.md) §6).
_Avoid_: field units for a `Var`, shift-adjusted `Var` comparison, "the constant is masked before it is emitted".

**operand domain**:
The set of values a write or comparison operand may take: a plain integer (Python `int` that is not
`bool`; TypeScript `number` with `Number.isInteger(number) === true`), plus the runtime operand
forms the particular surface accepts. Most surfaces below accept `Var`/`ReadonlyVar`/`Expr`, but the
block-memory primitives are narrower: `memcpyEPD` / `memsetEPD` accept `Var` and `Expr`, not the
read-only view (TypeScript rejects it at the type level; Python at runtime). `isqrt` accepts
`int`, `Var`, `ReadonlyVar`, and `Expr` in both frontends; an `Expr` is materialized once through the
canonical `to_var` / `toVar`, and `bool`, non-integral numbers, and other values are
`EUDSL-TYPE`. That removes the last documented math-helper frontend divergence; it widens the
operand domain and does not add a new validation policy. The guarded surface: writes and comparisons on
both field surfaces and on `Var`, var init, struct field inits, const-array literals and
comparisons, container element writes on `Queue`/`Deque`/`Stack` and `FixedArray`'s dynamic set, the
death-table constructors (`Deaths`/`DeathsX`/`SetDeaths`/`SetDeathsX`, `Memory*`, `SetMemory*`)
including every one of their 9 condition + 12 action HIR record fields, their `epd-from-address`
`addr` and their `ptr` `src`, the hand-written `SetStringDword`'s `value`/`mask`/`modifier`,
`MemCond`'s `comparison`/`size_or_mask`, and (py only) the field comparison dunders and the
`+=`/`-=` augmented assignment; `Expr` construction is validated only at materialisation. A struct
element write matches the element's fields **by position, with matching arity** —
`Queue(Point, 4).push(Other(a=7, b=8))` builds, because the two fields are slotted positionally;
type identity is documented, not enforced (the struct schema in `py/struct.py` / `ts/struct.ts`),
and a scalar does not broadcast into a multi-field element (`shape must match the element`,
`py/queue.py:150` / `ts/queue.ts:144`). Everything else — `bool`, a non-integral `float`/`number`,
`NaN`, `Infinity`, `string`, `None`/`undefined`/`null` — is `EUDSL-TYPE`, raised eagerly at the call
with the operand named. The test is a plain `int`, not `__index__`: an integer-convertible *handle*
is not an operand, so `FixedArray.const([arr.get(0), 5])` and `hp.eq(arr.get(0))` are `EUDSL-TYPE`
(narrowed in cycle 14, recorded in [ADR 0019](./docs/adr/0019-operand-domain-validation.md) §5). A
relocation operand is the exception the death-table constructors keep: a `Var`, `TriggerFieldRef`,
or `Trigger` relocates a field instead of becoming a number. TS cannot distinguish `1.0` from `1`,
so py rejects float `1.0` while TS builds it; parity is over integer **values**, not literal
spellings ([ADR 0019](./docs/adr/0019-operand-domain-validation.md) §4). Magnitude is **not** part
of the domain: any integer is normalised to 32 bits, so `hp.set(0x100000000)` writes `0` and `-1`
stays legal ([ADR 0017](./docs/adr/0017-field-value-space-comparisons.md) §2). Python comparison
dunders keep the `NotImplemented` protocol ([ADR 0019](./docs/adr/0019-operand-domain-validation.md)
§1 for the site table, §6 for the API-shape carve-outs).
_Avoid_: "coerced operand", "int()-compatible value", float-as-int.

## Storage & Containers

**semantic container**:
A user-facing collection or value type (`TriggerStruct`, `FixedArray`, `Queue`, `Deque`, `Stack`, `UnitGroup`, `FixedString`) with fixed build-time capacity.
_Avoid_: `EUDVArray`, `EUDStructArray`, `StringBuffer`, `DBString`, `Db`.

**backing**:
Where a container's or variable's state resides: a `TriggerPage`, a `Cell`, a read-only `payload` blob, or an `STR` table entry.
_Avoid_: "storage type", `strategy` parameter.

**backing module**:
The page/slot indexing arithmetic in `py/backing.py` / `ts/backing.ts` — carry-free EPD weights, EPD decomposition, element masks, and `PageSlotEPD` — not a storage-residency choice. Keep the filename; the two senses are not interchangeable.
_Avoid_: "storage type", residency, allocation.

**TriggerPage**:
A `Trigger` (`16C, 64A`) used as a 64-slot mutable storage page whose action slots (`0..63`) hold up
to 64 struct fields, array elements, or VProc variables. Inactive slots maintain the **self-targeted
action invariant** (`act[k].dest == EPD(act[k].value)` at rest) so firing the page is a no-op on
untouched slots while executing active reads or copies in `1T`.
_Avoid_: `VarBuffer`, `XVarBuffer`, 72-byte `vartrigger` slot.

**FixedArray**:
A fixed-capacity indexable container whose elements are scalars or `TriggerStruct` fields, chosen when the array size is known at build time. `FixedArray.const` is the read-only, build-time-table form of the same surface.
_Avoid_: `EUDArray`, `EUDVArray`, `VarBuffer`, dynamic array.

**Cell**:
A non-trigger mutable scalar storage unit (4-byte dword or 1-bit boolean) automatically selected by the compiler for scalars that never require `VProc` trigger chaining.
_Avoid_: `EUDLightVariable`, `EUDLightBool` as user-facing types.

**Var**:
A user-facing mutable handle to a Deaths-table scalar storage slot (`Cell` or `TriggerPage` slot).
Must be created inside `buildModule`; instantiating outside a builder throws fatal `EUDSL-SCOPE` by
construction. Variable binding assignment (`v2 = v1`) creates a reference alias pointing to the
exact same backing slot. Creating an independent copy requires explicit construction via `Var(v1)`
or `v1.copy()`, which emits a runtime `SetDeaths` trigger (`1T executed`).
_Avoid_: value-copy assignment, pointer wrapper, instantiating outside `buildModule`.

**in-place mutation**:
An imperative method (`v.set`, `v.iadd`, `v.isub`, `v.imul`, `v.idiv`, `v.imod`) that mutates a
`Var` in place and returns `void` (TypeScript) / `None` (Python) to prevent accidental assignment
expressions (`v2 = v1.iadd(...)`). In Python, dunder in-place operators (`__iadd__`, `__isub__`,
`__imul__`, `__ifloordiv__`, `__iand__`, `__ior__`, `__ixor__`, `__ilshift__`, `__irshift__`) return
`self` to support language syntax (`v += 1`, `v &= x`, `v <<= 1`). Declarative action builders
(`v.act_set`, `v.act_add`, `v.act_sub`) construct `HirAct` records for use in raw trigger action
arrays (`Trigger(acts=[...])`).
_Avoid_: chaining on imperative mutations.

**payload**:
The read-only backing that stores constant tables (`FixedArray.const`) as packed data rather than in
`TriggerPage` action slots. `lir2payload` starts every payload item (trigger or `Db` blob) at
`align4(previous end)`, so every item base — and every mode-1 (EPD) relocation target built as
`base + offset` — is a dword address; the 0–3 B inter-item pad counts toward
`StaticCost.total_bytes` but not `blob_bytes` (cycle 26, #120,
[ADR 0019](./docs/adr/0019-operand-domain-validation.md) §1).
_Avoid_: legacy writable `EUDArray`, `Db`.

**UTF-8 string bytes**:
The canonical RFC 3629 byte sequence a `FixedString` stores in its STR entry.
Literal content in any language (Korean, Japanese, Chinese, emoji, Latin) is
lowered to these bytes, so `é` takes 2 bytes, `한` takes 3, and `🙂` takes 4.
An embedded NUL and a lone UTF-16 surrogate are `EUDSL-TYPE`: the first
terminates an `STRx` entry and the second is never valid UTF-8. The
format-spec `fill` is still one ASCII byte (ADR 0014).
_Avoid_: UTF-16 storage, code-point capacity, system codepage, ANSI text.

**STR-backed**:
The backing that stores text in an entry of the map string table, giving `FixedString` the STR identity required by `DisplayText` and `PlayWAV`.
_Avoid_: `StringBuffer`, `DBString`.

**FixedString**:
A fixed-capacity text buffer backed by the map string table, giving it the STR identity that `DisplayText` and `PlayWAV` require. `FixedString(size)` reserves `size` **content bytes** plus the NUL at offset `size`; capacity counts UTF-8 bytes, not characters. Text that does not fit is a build-time error, not a silent truncation and never a split code point.
_Avoid_: `StringBuffer`, `DBString`, `Db`, mutable byte array, character capacity.

**DisplayText**:
The stock action helper that shows text to CurrentPlayer, not to every player. Its text must carry STR identity, so it accepts a `FixedString` or a literal; use `DisplayTextIndex` for a pre-resolved string id.
_Avoid_: broadcast message, chat to all players, `StringBuffer`.

**STRx**:
The 32-bit offset StarCraft: Remastered extended string table section in `scenario.chk`, replacing the 16-bit 64KB-limited classic `STR ` section.
_Avoid_: `STR` (when referring to the Remastered 4-byte offset string table).

**module string index**:
The 0-based index of a string in the module's string table (`FixedString.message_index`), allocated
in first-use order. It is the frontend-side identity and is **not** the value a 16-bit table field
stores: a table field wants the 1-based wire id, which includes `template_count`.
_Avoid_: string id (for the 0-based index), local string number.

**wire string id**:
The 1-based id StarCraft stores in a 16-bit string field such as `unitsDat[t].nameString`:
`template_count + module string index + 1`. `template_count` is a basemap property known only when
the map is assembled, so `set_string` / `setString` resolves the id through relocation `mode == 6`
instead of accepting a precomputed number. An id above `0xFFFF` is a loud `EUDSL-BOUNDS` at
assembly, never a silent 16-bit truncation.
_Avoid_: `message_index` (for the table value), frontend string id, `strid`.

**string-table lane**:
The 16-bit half of the 32-bit dword holding two adjacent 16-bit string ids. An even entry is lane 0
(`shift 0`, mask `0x0000FFFF`); an odd entry is lane 2 (`shift 16`, mask `0xFFFF0000`). A lane write
must apply that mask — a whole-dword write clobbers the neighbouring entry.
_Avoid_: whole-dword name write, byte offset (for the lane bit shift).

**control character**:
An in-game text formatting byte (`0x01`–`0x1F`) in a StarCraft string specifying text color, alignment, or layout.
_Avoid_: color tag (when referring to wire byte encoding), escape code.

**aligned lane**:
The compile-time byte lane (`offset & 3`) of the next `FixedString` content byte when it can be
proven. Scope boundaries pad a partial tail with `0x1A` to restore lane 0, letting dynamic byte
writes bake their byte mask instead of selecting it with a runtime branch. Padding belongs strictly
to scope boundaries, never mid-format-string placeholders: fixed-width format placeholders fold
compile-time constant offsets across any byte lane directly without auto-padding.
_Avoid_: "alignment slack", "+4 tail allowance", mid-string auto-padding.

**STRING_PAD_BYTE**:
The zero-width `0x1A` byte that pads a `FixedString` tail to the next dword at a scope boundary
(ADR 0003). It is appended after the complete UTF-8 content and counted in bytes; it never appears
inside a code point, so the visible text is the content with the pad bytes removed.
_Avoid_: NUL padding, mid-sequence pad, character padding.

**format spec**:
A format specification inside a `FixedString` interpolation placeholder following the grammar
`[[fill]align]width[type]`
([ADR 0014](./docs/adr/0014-decimal-format-default-and-fill-alignment-syntax.md)). Bare `{}`
defaults to decimal (`{:d}`). Types are `d` (decimal, width 1–10) and `x` (lowercase hex, width 8).
Fill may be any ASCII byte; `\x1a` (invisible pad), `'0'`, and space `' '` are the canonical HUD
fills, with right alignment `>`. Fixed-width decimal overflows wrap modulo $10^W$.
_Avoid_: "printf string", "format string syntax" (when referring to the interpolation placeholder mini-language).

**direct digit extraction**:
Algorithm used to format decimal numbers by testing binary-weighted powers of 10 ($8, 4, 2, 1 \times 10^k$) with direct 1-test conditions and subtract-and-accumulate actions, bypassing runtime division loops and division ladders.
_Avoid_: "division ladder" (for the direct test engine), "mod ladder".

**divmod**:
`a.divmod(b)` (Python also the builtin `divmod(a, b)` via `__divmod__`) and the exported TypeScript
`divmod(a, b)`: return `(quot, rem)` / `[quot, rem]` as a pair of fresh `Var`s from one restoring
ladder. `div` and `mod` stay single-result APIs over the same ladder helper and copy only their
requested result, so their HIR is unchanged. A constant divisor reuses the constant-divisor ladder
and still returns both values; a runtime divisor zero yields `(0, dividend)`.
_Avoid_: "divide twice", "two-ladder divmod", using `divmod` when only one result is needed.

**fixed capacity**:
Every container's capacity is chosen at build time and never grows or reallocates at game time.
_Avoid_: "dynamic array", "growable", "runtime allocator".

**unlimiter**:
`unlimiter()` / `connect_dlist` / `connectDList` in `py/unlimiter.py` / `ts/unlimiter.ts`: a core
helper (not a plugin) ported from euddraft's `plugins/unlimiter.py`. It replaces the game's four
grow-only lists with lists backed by module data blobs (`Db`), so the engine can allocate past its
vanilla caps:

| list    | head     | tail     | node stride | elements |
| ------- | -------- | -------- | ----------- | -------- |
| bullets | 0x64EED8 | 0x64EEDC | 112         | 8192     |
| sprites | 0x63FE30 | 0x63FE34 | 36          | 65536    |
| images  | 0x57EB68 | 0x57EB70 | 64          | 65536    |
| orders  | 0x64B2E0 | 0x64B2E4 | 20          | 65536    |

`connectDList(elStart, elEnd, itemSize, itemN, isSprite)` builds one list: a `Db` of
`itemSize * (itemN + 2)` bytes (one padding element per side, because eudkit blob relocations are
non-negative offsets), a one-shot `while_loop` of `itemN` passes that writes each node's `prev`
(`+0`) / `next` (`+4`) and patches the write actions to advance one element per pass, then a tail
that points `elStart` at element 0, `elEnd` at element `itemN - 1`, and zeroes the two dangling
links. Sprite nodes also seed `+0x10` with `10000 + k`. `unlimiter()` arms the four lists above and
emits `SetMemory(0x64DEBC, SetTo, 40)` as a per-loop act, matching the source's `onPluginStart` +
`afterTriggerExec`; call it after the module's other statements so that write stays last. Arming is
intra-pass and evaluates 679,936 triggers over the four lists (one extra trigger per element over
the source's combined write+patch trigger), past `dump-ir`'s 100,000-evaluation simulation guard, so
inspect an armed module with `dump-ir --no-exec`; the four blobs are 8,782,288 bytes of payload, and
an unlimiter-only module is 22T / 4C / 64A.
_Avoid_: unlimiter plugin, plugin settings manifest, `onPluginStart` hook, rebuilding the lists with raw `SetMemory` payload pointers.

**data dumper (dataDumper / data_dumper)**:
`data_dumper(input_data, out_offsets, flags=())` in `py/datadumper.py`,
`dataDumper(inputData, outOffsets, flags = [])` in `ts/datadumper.ts`: a core helper (not a plugin)
ported from eudplib's `_add_datadumper` / `_f_datadumper` (`eudlib/utilf/datadumper.py`) and
euddraft's `plugins/dataDumper.py`. It puts a raw byte payload (a `stat_txt.tbl`, a `RequireData`
block) in a module data blob (`Db`) and makes `out_offsets` (positive 4-byte-aligned addresses, not
EPDs) learn about it. The default mode writes the blob's payload address into each target with
`SetMemory(out_offset, SetTo, db)`; `unpatchable` writes the same address (eudkit has no build-time
memory patching or patch stack, so eudplib's `f_dwpatch_epd`/`f_unpatchall` pair has no
counterpart); `copy` treats each target dword as the destination's *byte address* (the game's own
pointer, e.g. the `stat_txt.tbl` pointer at `0x58D740`), reads it at run time and converts it to an
EPD (`f_epdread_epd`: `(D - 0x58A364) / 4`, folded into the bit-test read plus one wrapping
subtraction), then copies the payload there with `memcpyEPD` (eudplib's `f_repmovsd_epd`),
`ceil(len / 4)` dwords. `copy` and `unpatchable` are mutually exclusive, as in the source's
`assert "copy" not in flags`. One call registers and emits (eudkit has no plugin settings phase),
and the emission is a `once` so it runs on the first trigger loop like the source's `onPluginStart`.
Deviations: one blob per call instead of one copy per target, the payload is zero-padded to a whole
dword so `copy`'s last rep-movsd dword is deterministic, an empty offset list returns
`None`/`undefined` (no blob), and an empty payload is `EUDSL-RANGE`. `memcpyEPD(dst, src, count)`
also accepts a `Db`/`DbRef` source: the cursor variable is seeded by a mode-1 blob relocation since
the payload address is only known at assembly time. Copy cost for a 10-byte payload is 77T / 64C /
150A static (31 triggers for the EPD read, the cursor seed, then the 44-byte copy loop), with the
blob dominant in map size.
_Avoid_: dataDumper plugin, `require`/`stat_txt` injection mode, unpatch bookkeeping, one blob per target.

## Temporal & Memory Primitives

**when / once / until**:
Loop-cadence control blocks: `when(cond, body)` runs every trigger loop `cond` holds;
`once(cond, body)` runs on the first loop `cond` holds and permanently self-disables;
`until(cond, body)` runs every loop while `cond` is false and permanently disables once `cond`
holds. `until` is a HIR node (`HirUntil`, carrying the DSL-reserved `latch_var`, init 1) since HIR
3.1; opt desugars it to `when(latch==1){ when(conds){ latch.set(0) } }` plus
`when(latch==1){ body }`. For an unconditional gate (`0` conds or all-`Always`) opt emits only the
reset trip-when, so the body never runs (exclusive semantics, issue #72).

**dispatch**:
A frontend desugar for a one-of-N integer choice with compile-time plain-integer keys (normalized to
32 bits and unique after normalization). A runtime value with `N <= 9` cases lowers to a nested
`when(..., else_body)` chain; `N >= 10` lowers to a balanced `HirWhen` search tree (`ge(mid)`
internal nodes, `eq(key)` leaves) for an `O(log N)` search path. A plain-integer value folds at
build time (only the selected arm, fallback, or nothing is emitted); an `Expr` value is materialized
once; the optional `fallback` runs only on a miss, and a miss without one is a no-op. It is not a
HIR node and not a dense jump table.
_Avoid_: `switch` (TypeScript reserved word and StarCraft `Switch*` collision), `match`, jump table.

**onRising / onFalling**:
Edge-triggered blocks that run on the trigger loop where `cond` transitions `false → true`
(`onRising`) or `true → false` (`onFalling`). An edge is a HIR node (`HirEdge`, carrying `direction`
plus the DSL-reserved `curr_var` / `prev_var`) since HIR 3.1; opt desugars it to a reset act, a
sample gate, a nested fire gate, and two `prev` updates. Loop 1 counts as rising from initial 0.

**every / after**:
Counted-loop cadences: `every(n, body)` runs every `n` trigger loops; `after(n, body)` arms an
`n`-loop countdown when reached and fires once at 0. Both are HIR nodes (`HirEvery` / `HirAfter`,
carrying `n` plus the DSL-reserved `counter_var`) since HIR 3.1; opt desugars `every` to a counter
increment plus a fire `when`, and `after` to a 3-`when` arm/decrement/fire sequence.

**temporal form**:
A named control-flow shape with time behaviour: `when`, `once`, `while`, `until`, `edge`, `every`, `after`. All seven are HIR nodes as of 3.1; the last four desugar in opt (see **desugar**).

**sibling `when`/`once` merge**:
`eudkit-opt` fuses adjacent `when` blocks — or adjacent `once` blocks — with identical condition
lists into one conditional group, so the shared gate is evaluated once for the merged group instead
of once per block. The kinds never mix (`once`+`when` stays unmerged: each `once` self-disables
independently, so the group would follow the wrong cadence). Bodies merge only when *flat* — `Act`,
`VarTransfer`, `VarBatch`, `VarStore`; any nested `when`/`once`/`While` rejects — and under a
dependency guard: no body may write a variable the gate conditions read
(`conds[].relocs[].target_var`), because the later gate would otherwise not re-observe the earlier
body's writes — a RAW hazard. Acts name a variable through a `relocs[].target_var`,
transfers/batches through `dst_var`; a `VarStore` names none (its destination is an absolute cell or
trigger field), so stores add no read-set constraint — a store's destination is absolute, its source
read keeps order. A gate that names storage by an absolute address instead (`Memory`/`Deaths`,
condtype `15`; `Switch`, condtype `11`) carries no relocs, so the merge also requires every gate
cond to be keyed by a paged variable (a field-4 `player` reloc with a `target_var`) or the earlier
body to write only paged variables — an act without a `target_var` writes non-paged storage, and
absolute addresses cannot name a paged variable's storage, so such a body cannot change the external
gate's cell (#90); transfers, and batches' transfers, write paged storage — a batch carrying
`stores` is not paged-only — and a `VarStore` does not. Phase 2 (#112) additionally admits a body
under an external gate when every external cell it writes is provably a different dword from every
cell the gate reads: a private `StorageKey` names paged variables (`Var`), raw EPD field values
(`Epd` — `Memory(FLAG).player == SetMemory(FLAG).dest == epd_u32(FLAG)`) and switch ids (`Switch`).
Unknown writes (the dynamic `CurrentPlayer` base, string/blob/trigger relocs, unaligned `dst_addr`)
fall back to today's answer, so this clause only ever admits a merge and never withdraws one; equal
keys keep the merge rejected. `Epd` vs `Switch` is deliberately never disjoint — switch state lives
in game memory but this repo models it only as a separate `switches` array, so `SetMemory` aliasing
a switch is unmodelled — while `Var` is disjoint from any external key by the existing "an absolute
address cannot name paged storage" invariant (#90, #98, #112). Only a pure leaf-act pair is bounded
by the `64`-action `Trigger` cap; any other merged body is larger by design and is re-coalesced by
the block scheduler under the transfer rules, so RAW/WAR/WAW/duplicate-source hazards still split it
and the merged group lands on the hand-written one-block cost (#69, #91, #98, #112).
_Avoid_: when fusion, gate dedup (condition CSE is a separate pass).

**root act/transfer interleave**:
A root action does not split a pending transfer run. `eudkit-opt` parks a root action while a
`VarTransfer` run is open and emits it ahead of the batch: a bare `HirStmt::Act`, or an
unconditional preserved `Trigger` block in the `can_unpack_into_basic_block` shape (no conditions,
no chain pointers, flags `4`). Lowering folds those actions into the batch's setup trigger, so
`act, x.set(y), act, w.set(z)` at the module root measures the same as the grouped
`act, act, x.set(y), w.set(z)`. The guard: an action carrying any `relocs[].target_var` may read or
write a paged variable slot, so it keeps source order and ends the run. Module root only; body
interleave is a separate follow-up needing the same act↔transfer dependency analysis.
_Avoid_: act fusion, act reorder pass.

**desugar**:
Replacing a high-level form with lower-level forms before lowering. Here: each temporal node expands
to `when` blocks plus DSL-reserved state Vars in `crates/eudkit-opt/src/temporal.rs`. The DSL
mirrors emit nodes only; they no longer desugar. Desugar recurses into bodies at every depth, so
flatten/fuse keeps working inside nested nodes.

**counted form**:
A time behaviour driven by an explicit loop counter: `every(n)` fires every `n` loops; `after(n)` fires once after `n` loops. Distinct from `whileLoop` (intra-frame repetition, no counter).

**whileLoop**:
The intra-frame runtime loop (`while_loop` in py): it repeats its body while the conditions hold
within one trigger loop, with no counter. It is the sanctioned spelling of `while cond:`; a
Python/JS `while` on a runtime condition loops at build time and hangs
([ADR 0020](./docs/adr/0020-build-time-vs-runtime-conditions.md)). Distinct from the **counted
form** (`every`/`after`), which is driven by an explicit loop counter.
_Avoid_: bare `while` (build-time loop), frame loop.

**path-complete restore**:
Every gate's `next` pointer is restored on every path from body entry to join. Desugared temporal output inherits it by construction: desugar produces only `when` blocks, which lower with path-complete restore, and `control.rs` is untouched by candidate 11 phase 1.

**HIR version**:
The `version` field of HIR JSON, exact-checked by readers. `3.2` since
[ADR 0021](./docs/adr/0021-mpq-embedded-file-surface.md) added the embedded-file `files` list (3.1
readers reject 3.2 documents); 3.1 added the `until` / `edge` / `every` / `after` kinds (3.0 readers
reject 3.1 documents). The LIR wire schema is separate and untouched by this work.

**EPD**:
The Deaths-table dword index of a 4-byte-aligned memory address, `EPD(addr) = (addr - 0x58A364) / 4`.

**runtime EPD**:
An EPD whose numeric value is only known when the map payload is assembled: a module blob's
payload address is placed by `lir2payload`, so an EPD into it cannot be a build-time constant.
`epd_of_db` materializes one into a fresh `Var` with a preserved zero-condition trigger and a
mode-1 blob relocation into the `Var`'s value field
([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md)). A runtime EPD is a scalar value; it is
not a runtime base or an indexed container
([ADR 0030](./docs/adr/0030-runtime-indexed-payload-backing.md)).
_Avoid_: dynamic pointer, runtime address (when the value is an EPD).

**EPD-bearing value**:
A `Var` whose 32-bit value is an EPD rather than a byte address, a count, or a boolean;
`epd_of_db` returns one and `read_epd_pointer` also produces one. It can seed CP
(`CurrentPlayer`), index a payload dword, or be compared like any other EPD. The value keeps the
EPD unit end to end; do not subtract `EPD_BASE / 4` twice.
_Avoid_: pointer variable (when it holds an EPD), address-valued `Var`.

**payload blob EPD**:
The runtime EPD of one dword of a `Db`/`DbRef` payload, `EPD(payload_addr + byteOffset)`, where
`byteOffset % 4 == 0`. It is assembly-time-only (the blob payload address is chosen by
`lir2payload`), and `epd_of_db` is the only public constructor: a preserved trigger whose action
value field carries a mode-1 relocation to the blob. Cross-reference **payload** and
**CP (CurrentPlayer)**; see [ADR 0029](./docs/adr/0029-runtime-epd-addressing.md).
_Avoid_: blob address (for the EPD), blob pointer, mode-4 value.

**CP (CurrentPlayer)**:
The CurrentPlayer address (`0x6509B0`) used as a pointer register, enabling `Deaths(13, ...)` to read
or write relative to the bound `EPD`. `DisplayText`/`DisplayTextIndex` use the same slot as a
player-index display target; the payload runner leaves it at 0 (P1), so text for the local human
must first set it to the user player id (`0x512684`).

The **ambient CP** is the value in the register immediately before a helper binds it; it is not
guaranteed to be 0 or 1, because a prior text write or `PageSlotEPD` read may have moved it.
`with_cp`/`withCP` binds a constant int EPD and emits **absolute** `SetTo base + k` hops; a
**relative hop** is the `Add k - currentOffset` delta that `with_epd`/`withEpd` must emit because
its runtime base is unknown at build time. The restore policies differ, stated once here: `with_cp`
is cursor-only (re-anchors on exit only when the build-time offset moved, so a read at offset 0 can
leave CP at 0); `with_epd` saves the ambient CP with the shared reader and restores it
**unconditionally** in a `finally`, including the exception and body-clobber paths, with nested
calls restoring LIFO ([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md) §7); and the
`PageSlotEPD` read resets CP to `0` (the `zero` policy,
[ADR 0004](./docs/adr/0004-triggerpage-transfer-parking-and-curpl-reset.md)).
`memcpyEPD`/`memsetEPD` still clobber at their end value.

A large dynamic `FixedArray.get` (`length >= 64`) uses the slot as scratch: the PageSlotEPD reader
seeds CP at the addressed element, reads it with 32 guarded bit tests, then resets CP to `0`. A
non-zero ambient CP is therefore not preserved across such a read unless the caller wraps it in
`with_epd`; the read is not a `with_cp` hop, and `CPCursor.restore()` only re-anchors a moved cursor.
The reader costs `35 + ceil(log2 N) + pages` triggers static and
`35 + ceil(log2 N)` executed (N=256: 47T static / 43T executed, 113,184 B, versus the unroll's 772T
/ 259T / 1,858,984 B); the guard costs `+32C`, 0T, 0 B.

**EPD-pointer read**:
`read_epd_pointer(epd)` / `readEpdPointer(epd)` (eudplib's `f_epdread_epd`): reads the dword at
`epd` as a byte address, drops bits 0–1 (addresses are dword-aligned), and subtracts `EPD_BASE / 4`
to return the pointed-to `EPD`. It composes the caller-reset `read_epd` bit-test read: the
accumulator reset is emitted at the read site, never inside the shared bit-test loop, so a caller's
queued CP move keeps its ordering. The EPD-pointer read itself binds no CP. The separate paged
CP-cursor reader follows the same caller-owned reset rule, and a non-zero ambient CP is not
preserved across that paged read (see **CP (CurrentPlayer)**).
_Avoid_: `f_epdread_epd` (when naming the eudkit helper), dereference (when the result is an EPD).

**MemCond**:
A single-condition in-place memory comparison against an `EPD` without materializing the value into a variable (`1T` when the `EPD` is constant).

**memcpyEPD / memsetEPD**:
Block memory copy (`memcpyEPD(dstEPD, srcEPD, count)`) and fill (`memsetEPD(dstEPD, value, count)`)
over `count` dwords. Constant counts fold at build time: `count == 0` emits nothing, a constant
`(dstEPD, value, count)` fill with `ceil(count/64) <= 512` (`MAX_FOLD_CHUNKS`, a static byte budget:
512 triggers ≈ 1.18 MiB) emits `ceil(count/64)` packed `SetDeaths` triggers, and a constant
`count == 1` copy emits the loop body once with no counter or `while_loop`. Fills above the cap keep
the runtime loop. Constant counts above `0xFFFFFFFF` are rejected with `EUDSL-RANGE`. Non-folded
calls use a check-first `while_loop` over a counter that is re-stored from `count` on every
execution (constant or runtime, so a fill inside a loop or a re-entered `while_loop` runs again).
`memcpyEPD` seeds CurrentPlayer to `srcEPD - 1` and advances it before every read, so a
`count`-dword copy ends at the observable CP value `srcEPD + count - 1`; a constant source emits one
direct `SetDeaths`, a runtime source stores the seed through a temp, and a `Db`/`DbRef` source seeds
a cursor variable through a mode-1 blob relocation because the payload EPD is only known at assembly
time. Both primitives land each dword through one scratch pointer whose `dest` field is the
destination cursor (patched from `dstEPD`, or from a `Var`'s slot) and whose `value` field carries
the fill value or copied dword. The destination and CP seed is gated on `count >= 1` for every
destination form (constant EPD or `Var`), so a runtime-zero call leaves the scratch `dest` field and
CP untouched and pays no seed action. Variable counts and variable fill values keep the runtime
loop.

**isqrt**:
Runtime integer square root (`py/mathf.py`, `ts/mathf.ts`):
`isqrt(value, value_bits=32, fixed_bits=0)` emits `floor(sqrt(value) * 2**fixed_bits)` and returns a
`Var`. It is a digit-by-digit restoring square root whose step count is fixed by the declared widths
(`ceil(value_bits / 2) + fixed_bits`, at most 32), so static cost is width-fixed rather than
value-dependent: one trigger per input bit (the two shift-in conditions of each integer step) plus a
recurrence group per step. The kernel carries `u = 2*root` beside `root`, so each step doubles both
accumulators, builds `trial = u + 1`, saturating-compares it against the remainder, and runs the
take gate; at the default `value_bits=32`, `fixed_bits=0` (`steps=16`) that is 3 setup triggers + 32
input-bit triggers + 11 recurrence triggers per step = **211T / 48C / 726A** static (48 = 32 bit
conditions + 16 take gates). Executed cost tracks the steps actually taken. `value` accepts
`int`, `Var`, `ReadonlyVar`, and `Expr` in both frontends (an `Expr` materializes once through
`to_var`/`toVar`); `bool`, non-integral numbers, and other values are `EUDSL-TYPE`. `value` must be
`< 2**value_bits` at game time (the caller owns the bound) and a constant folds at build time. Used
by **camera_follow** for `f_sqrt(fpQ² · (dx² + dy²))`.
_Avoid_: `math.sqrt`, floating-point root, "divide by two until it converges".

**camera_follow (cameraFollow)**:
The per-loop camera-follow helper ported from euddraft's `plugins/cammove.py` (`py/camera.py`,
`ts/camera.ts`). Every loop where `Memory(0x57F0B4, AtLeast, 1)` (`gbMultiPlayerMode`: any non-zero
mode is multiplayer — not a pause gate) and the caller's switch is set, it moves the scratch
location onto a Map Revealer (unit 101) owned by P9 (player 8) inside the target location, eases the
box toward that corner with the source's fixed-point smoothing (`fpQ = 256`, `inertia`,
`maxspeed * fpQ` cap), stores the box plus its right/bottom edges, centers the view, updates the
camera tile dwords, and re-derives the widescreen `screenX` width. It writes `CurrentPlayer`
(`0x6509B0`) from `dwUserPlayerId` (`0x512684`) before `CenterView` and restores `0` afterwards so
later `Deaths(CURRENT_PLAYER, …)` in the same loop is not poisoned
([ADR 0004](./docs/adr/0004-triggerpage-transfer-parking-and-curpl-reset.md) resets `curpl` once per
loop). A cleared switch resets `lastTick` to `-1` so re-enabling snaps instead of easing from a
stale tick; `inertia == 0` is a build-time branch that omits the smoothing kernel. Returns
`CameraFollowState` (`last_tick`, `screen_x`). Per-pass cost as a delta over identical seeding
scaffolding: `inertia=5` is 1,177T / 509C / 3,241A static (2,834,256 B); the first gated pass is
1,159T / 486C / 18,535A executed and the steady-state second pass is 1,121T / 454C / 18,463A.
`inertia=0` is 223 / 198 / 376 static (537,000 B); its first pass is 271 / 229 / 1,164 executed and
its second pass 233 / 197 / 1,092. A closed gate costs 3T / 2C / 0A executed because the chain jump
skips the body. The gate-scoped `once` cache for the local player id (`0x512684`) leaves the first
pass at the pre-cache cost and saves the 33T reader plus its call overhead on later open passes; it
adds 1T / 2,408 B static. The constant multiply in the smoothing kernel delegates to `Var.mul`, so
`(currentTick - lastTick) * fpQ` is one masked const-shift rather than a 24-trigger ladder. The
`run` body's shared path holds four coalesced transfer pairs, which is why body-wide transfer
coalescing moved `inertia=0` by 4 static triggers, 8 executed triggers, and 9,632B as well.
_Avoid_: camera lock, view lerp, "paused-game no-op" (the gate is multiplayer mode).

## Unit Iteration

**Status:** `list walk` (`for_each_unit_list` / `forEachUnitList` over `U`/`T`/`S`/`D`,
with the `Break` sentinel and the `reentrant` option) is current (`T-WP7`). `slot scan`,
`density crossover`, and `new-unit snapshot` are planned —
[ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decisions 1–6, not implemented.
**tracked group** (`UnitGroup.for_each`, unrolled default), **compact mode**, and the
**runtime-loop API** are current ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md)
decision 7). Each planned entry also says so inline.

**slot scan**:
A unit-iteration strategy that walks all 1699 fixed CUnit slots and checks liveness per slot; static
cost is ~4T plus the body once, and executed cost is ~3T × 1699 ≈ 5.1kT per invocation regardless of
live count. Planned — [ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decisions 1–6; not
implemented.
_Avoid_: unit array scan, `EUDLoopUnit2` (when naming the strategy), "1700 static triggers".

**list walk**:
A unit-iteration strategy that follows `nextUnit` pointers from a list head. The reference loop
costs ~35–40T executed per live node — per-node loop overhead, not the pointer read alone — and
scales with the walked node count. A pointer read costs 32T today and 20T with the bits-0–19
reader ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §8); do not read the 20T figure as
the whole per-node cost. Implemented for `U`/`T`/`S`/`D` by `for_each_unit_list` (`T-WP7`); the
slot-scan and new-unit siblings remain planned — decisions 1–6.
_Avoid_: pointer chase, linked-list loop.

**density crossover**:
The live-unit count (roughly 130–150, or ~85–100 if the 2T scan fusion lands) at which a list walk's
linear executed cost (~35–40T per live node) overtakes the slot scan's fixed ~5.1kT; below it the
walk is expected to win, above it the scan. Planned —
[ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decisions 1–6; not implemented.
_Avoid_: break-even threshold, magic unit count.

**new-unit snapshot**:
A one-time startup table aligned with the 1699 CUnit free-list slots, used to detect units created after initialization; it costs 570,868 B (`4 + 336 × 1699`, ~558 KiB) per call site unless shared. Planned — [ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decisions 1–6; not implemented.
_Avoid_: shadow array, seen bitmap.

**tracked group**:
A `UnitGroup` whose membership is explicitly added and removed; by default its `for_each` unrolls
the body per slot, giving static cost `capacity × (body_triggers + 1) + ceil(capacity/64)` triggers,
and it is intended for small tracked sets. `UnitGroup` and `for_each` are current
([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) decision 7); the `for_each_tracked`
wrapper is planned — decision 2, not implemented.
_Avoid_: unit registry, all-units group.

**compact mode**:
A `UnitGroup(compact=True)` mode where `for_each`, `autoPrune`, `add`, `remove`, and `clear` each
use a runtime CurrentPlayer cursor sweep instead of a per-slot unroll; it emits the body once.
Per-sweep costs are ~9T executed per scanned slot and ~34T executed per live entry, plus a ~79T
static `for_each` scaffold and 2,408 B per 64-slot page. Whole-group cost is the sum of its sweeps:
at capacity 1,000 the default `for_each` + `autoPrune` group is 176T static / 423,832 B (~424
B/entry) and 18,076T executed per top-level execution; adding top-level `add`/`remove`/`clear` is
307T static / 739,308 B (~739 B/entry) and 43,191T executed. A static-size optimization, not a speed
optimization.
_Avoid_: packed group, runtime group, compact table.

**runtime-loop API**:
A loop primitive that emits its body once and iterates at game time, as opposed to a compile-time unroll that duplicates the body per slot.
_Avoid_: unrolled loop, generator loop.

**free list**:
The engine's linked list of vacant CUnit slots, headed at `0x628438` (`CUNIT_FREE_HEAD`, EPD
161845) and tailed at `0x62843C` (`CUNIT_FREE_TAIL`). Its nodes use the CUnit `prev`/`next` links
(`+0`/`+4`) and the same 336-byte stride, so a forward walk follows `next` from the head. It is the
`F` list in the stock list family, not a game-unit list: it enumerates unallocated slots and is
reached through capacity predicates (`free_list_empty`, plus the planned `free_list_walk`) rather
than a unit-list walk.
_Avoid_: treating `F` as a unit list, `0x6283F0` (that is the unit count, not a list head).

**CCMU**:
"Can't Create More Units": the engine state in which no CUnit slot is available and a create order
fails (the classic error message's abbreviation). It is a capacity condition, not a player or a
command structure. `free_list_empty` is its exact 1-condition truth.
_Avoid_: `cannot` count, treating `0x6283F0` as CCMU truth.

**free_list_empty / freeListEmpty**:
The 1-condition, 0-read capacity predicate `Memory(0x628438, Exactly, 0)`: the free-list head is
null, so the engine has no free CUnit slot right now. It returns an ordinary condition (no wrapper
`Trigger`), so it composes into a 16-condition gate without spending a second trigger.
_Avoid_: free-slot count, `1700 - 0x6283F0`, inserting a gate silently.

**capacity tier (1/2/3)**:
The three ways to answer "is there room for another unit". Tier 1 is the exact 1-condition free-list
head test (`free_list_empty`, this wave). Tier 2 is a tracker-derived upper bound,
`1700 - (|U|+|T|+|S|+|D|)`, using the four list counters; it over-reports (subunits are included)
and is deferred because it needs the 5-list tracker (WP12, excluded). Tier 3 is the exact one-shot
free-list walk (`T-WP10-T2`): ~22–28T per free node, once at map start or on author request, never
per tick.
_Avoid_: calling the counter form tier 1, an exact count without the walk.

**advisory capacity check**:
A capacity signal that answers exactly the question it reads and no more. `free_list_empty` is
advisory, not authoritative: the allocator can still refuse a creation for reasons the head dword
cannot see (placement, sprite limits, a hijacked list). An empty list means "try", not "this will
succeed", and a non-empty list is not a slot count.
_Avoid_: capacity guarantee, authoritative free count.

**CUnit pool**:
The game's fixed array of 1699 CUnit slots starting at `0x59CCA8`, 336 bytes (84 dwords) apart;
slot starts therefore range over `S in [0x16732A, 0x18A052]` and map to EPDs `[19025, 161657]`.
`CUNIT_POOL_BASE`, `CUNIT_BASE_EPD`, `CUNIT_LAST_EPD`, `CUNIT_SLOT_COUNT`, and `CUNIT_SLOT_SIZE`
name it ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §8).
_Avoid_: unit array, CUnit heap, slot table (for the pool itself).

**shifted pointer space `S`**:
The dword-aligned byte address divided by four, `S = A >> 2`. For a CUnit slot start, `S` bits 0
and 1 are invariant (0 and 1) and bit 20 is always 1, so `S` bits 2-19 (18 tests) recover the slot
index; the reader seeds the invariant bits with `0x100002` and subtracts `0x1628D9` to land in EPD
space ([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §8).
_Avoid_: byte address, EPD (for `S`), unqualified pointer space.

**CP-relative read**:
A dword read whose condition target is `CURRENT_PLAYER`, so the engine reads the EPD currently
held in the CP register rather than a constant address. `read_cunit_pointer(Var)` binds CP through
`with_epd` and restores it; `read_cunit_pointer_saved` is the loop form that saves the ambient CP
once and advances it per node ([ADR 0029](./docs/adr/0029-runtime-epd-addressing.md) §5).
_Avoid_: indirect read, dynamic address read, CP hop.

**null-pointer sentinel (`0xFFF9D729`)**:
The value the CUnit pointer reader yields for a null pointer: `0x100002 + 0xFFE9D727`. It is
above `CUNIT_LAST_EPD` (161,657), so `in_cunit_pool`'s **upper** bound, not its lower bound,
terminates a list walk. Never use it as the reader's bias
([ADR 0015](./docs/adr/0015-unit-iteration-strategies.md) §8).
_Avoid_: null EPD, end-of-list marker (for the value).

**`in_cunit_pool`**:
`in_cunit_pool(epd)` / `inCunitPool(epd)`: the validity predicate for a reader result, returning
`[epd.ge(CUNIT_BASE_EPD), epd.le(CUNIT_LAST_EPD)]` as two conditions that splice into a loop gate
(`[*in_cunit_pool(ptr), done.eq(0)]`). It never raises: a map with a hijacked list stays
diagnosable, and the upper bound rejects the null sentinel.
_Avoid_: assert-in-pool, `EUDSL-BOUNDS` for a dead list, wrapping the check in a trigger.

**unit list (U/T/S/D)**:
One of the four global CUnit lists walked by `for_each_unit_list` / `forEachUnitList`:
`UNIT_LIST_U` (in use), `UNIT_LIST_T` (hidden), `UNIT_LIST_S` (revealers), and
`UNIT_LIST_D` (dying). `U`/`T`/`S` walk forward from `next` (`unit_epd + 1`) starting at
their head dwords `161843`/`161841`/`161844`; `D` walks **backward** from the tail
`161842` through `prev` (`unit_epd + 0`), because the old forward `D` head is not usable on
Remastered and a forward walk silently misses every death. The free list `F`
(`161845`/`161846`) is not a unit list and is rejected with `EUDSL-BOUNDS`.
_Avoid_: unit array, `EUDLoopUnit` (when naming the eudkit surface), forward `D` walk.

**reentrant walk**:
A `for_each_unit_list(..., reentrant=True)` / `forEachUnitList(..., { reentrant: true })`
walk that reads the successor link **before** running the body, so a body that frees the
current CUnit still reaches the next node. The default (`reentrant=False`) reads the link
**after** the body; if the body frees the node, the walk stops after one iteration because
its own link is gone. The flag changes read/body order only; it is a plain `bool`, and the
ambient CurrentPlayer save is still once per site.
_Avoid_: safe loop, pre-fetch flag, "retry the walk".

**`Break` sentinel**:
The named `BREAK` singleton (`Break()` in Python, `BREAK`/`Break` in TypeScript) returned
from a unit-list body to stop the walk after the current node. It is checked by identity
(`ret is BREAK` / `ret === BREAK`), never by truthiness: `True`, `1`, and every other value
continue. `loop.break_()` / `loop.breakLoop()` is the imperative form and emits one
`done.set(1)` at the build point, so callers gate it at runtime
(`when([...], loop.break_)`). There is no `continue` sentinel in v1.
_Avoid_: boolean break, truthy return, `Continue` sentinel.

## Player Names

**player name record**:
One of twelve 36-byte (9-dword) records at `0x57EEE8`, holding a player's nickname as UTF-8 bytes
plus its NUL. The table is below `EPD_BASE`, so `player_name_epd(p) = (EPD(0x57EEE8) + 9*p) & 0xFFFFFFFF`
is an ordinary wrapped EPD that the engine reads and writes; `player_name_addr(p)` is the byte
form. A name may encode to at most 35 bytes, leaving room for the NUL
([ADR 0032](./docs/adr/0032-player-name-matching.md) §1).
_Avoid_: player struct, name string table, `0x57F014` header.

**nickname matcher (layout A)**:
`match_names` / `matchNames`: a build-time unroll that emits one ordered `when` chain per player
and compares the name record dword by dword with `DeathsX`. Each `(player, name)` arm is one
trigger with `len(bytes)//4 + 1 <= 9` conditions; the final mask includes the NUL, so matching is
exact and `"Armo"` cannot match `"Armoha"`. Nickname bytes come from `encode_utf8` / `encodeUtf8`;
the runtime byte-reader walk (layout C) is deferred because it needs rejected `SetNextPtr` rewiring
([ADR 0032](./docs/adr/0032-player-name-matching.md) §2-§5).
_Avoid_: name dispatch (when naming the unroll), prefix match, substring match, layout C.

## Lazy Declarations

**Lazy declaration**:
A handle created without a builder (`lazyVar`, and the later `lazy*`
family) that is bound on first resolving use inside `buildModule`.
Lazy declarations live at module scope (outside `buildModule`); declaring
them inside `buildModule` triggers the compile-time warning
`EUDSL-WARN-SCOPE`:
`lazy* declared inside buildModule; use eager Var for in-module declarations`.
Every `lazy*` factory emits that one exact message in both frontends.
Declaration is global, allocation is per-map: the same handle
resolves to a different binding (and different addresses) in each
map it is used in.

Analogous to Rust's `static X: LazyLock<...> = LazyLock::new(...)`, with
two structural distinctions:
1. eudkit lazy handles resolve per-builder at build time on first use (not
   a process-wide runtime singleton across all compilations).
2. eudkit lazy handles accept constant initializers only (not arbitrary
   runtime computation).

Follows the **owner-file convention**: define each lazy global in the file
that owns the domain concept (e.g. `wave` in `waves.py`/`waves.ts`) and keep
its mutable handle private. The owner exports a `ReadonlyVar` view
(`asReadonly`/`as_readonly`) plus mutator functions (e.g. `advance_wave()`);
consumers import the view and mutators, never the raw handle. The view
exposes scalar reads, conditions, pure expressions, and format/position
arguments, but no `value`/`dest`/`mask`/`modifier` field refs and no
`copy()`. TypeScript removes mutators from the type only (runtime values are
still `VarImpl`); Python rejects every mutation at runtime with
`EUDSL-READONLY`. Single-file variables must use eager locals instead of lazy
globals.
_Avoid_: `global*` naming for lazy handles, declaring `lazy*` inside `buildModule`
(use eager `Var`/containers), god `globals.py` files, and direct `.set()`/`.iadd()`
mutations from external consumer files.

**Binding (per-builder)**:
The fully eager instance a lazy handle resolves to under one
builder. Bindings live and die with their builder; a handle holds
one binding per builder.
_Avoid_: describing a binding without its builder.

**deferred binding**:
The per-builder resolution state machine behind every lazy handle: eager fast path, active-builder
lookup, cached binding, the `instantiate` thunk, binding storage, resolution count, first-binding
capture, and the post-build zero/one/many `EUDSL-SCOPE` errors
([ADR 0005](./docs/adr/0005-lazy-global-declarations.md) decision 7). Shipped in FE5 as the shared
`LazyBinding` helper (`py/lazy_binding.py`, `ts/lazy-binding.ts`) for all eight lazy types — `Var`,
struct, `FixedArray`, `FixedString`, `Queue`, `Deque`, `Stack`, and `UnitGroup`; the state machine
is identical, only the instantiate thunk and the diagnostic label vary per type, and each type keeps
its `_resolve` / `resolve` entry point and exact diagnostic text. Distinct from **Lazy
declaration**, which names the user-facing handle.
_Avoid_: deferred declaration, per-map singleton, global binding.

**First-use order**:
The allocation order rule (R1): within one builder, bindings are
created in first-use order. Allocation ids, page ranks,
string-table indices, and flusher registration order all follow
first-use order.
_Avoid_: "declaration order" as the allocation order.

**Declaration order**:
Documentation only under lazy (R2): the order handles are declared
across files, or within a file, does NOT determine allocation order.
Users needing a specific rank order touch globals in order at the
top of the `buildModule` body.
_Avoid_: relying on declaration order for ranks or ids.

**Binding-derived object**:
An object obtained from a binding — an element var, a struct field,
an out-box. Builder-scoped: handles are safe to carry across
builders, binding-derived objects are not (D10, documented rule, no
runtime enforcement).
_Avoid_: using a binding-derived object in a second module.

## Build & Packaging

**embedded file**:
A build-time file recorded on the compiled module by `embedFile` /
`embed_file` and added to the output MPQ under its archived name — the
`MPQAddFile` equivalent. HIR stores only `{name, path}`; the CLI resolves a
relative path against the directory of the compiling source, so dumps stay
machine-independent. The same surface serves the periodic-BGM helper and the
future dataDumper port ([ADR 0021](./docs/adr/0021-mpq-embedded-file-surface.md)).
_Avoid_: MPQ asset, plugin file, resource, bundled blob.

**periodic BGM helper**:
`play_bgm` / `playBgm(path, length_seconds)`: a core helper (not a plugin)
that embeds the track as `bgm` and arms a preserved probe plus a `when` gate
that plays it for the local player on the first trigger loop and every
`length_seconds` thereafter. Scheduling uses the countdown clock at
`0x51CE8C` (`0xFFFFFFFF - dword` = elapsed ms) and eudplib's 42 ms
start-latency compensation.
_Avoid_: bgm plugin, music plugin, settings manifest, onPluginStart hook.

**soundlooper helper**:
The port of euddraft's `soundlooper`: `SoundLoops` scans
`<title>/<title>NNN.ogg` segments, reads each Ogg length, embeds the segments
under a five-character archive id, and `soundLooper().player()` plays them as
an intro / bar / bridge loop for the local player. Scheduling recomputes the
next clock threshold per segment ([ADR 0023](./docs/adr/0023-soundlooper-loop-player.md)); a shared dword array mirrors each
loop's progress. Per-bar dispatch uses a `when` chain ([ADR 0023](./docs/adr/0023-soundlooper-loop-player.md) §7) — a
deliberate exception to [ADR 0025](./docs/adr/0025-multiway-dispatch.md)'s `dispatch` preference. RT3 landed the
measurement in `scripts/bench_soundlooper_dispatch.py` and
`ts/scripts/bench_soundlooper_dispatch.ts`: the `dispatch` tree wins the
due-pass path from N >= 16 (17 → 6 conditions at N=16) but adds `2N - 2`
static triggers, about 4.8 KB per saved condition (~4.8 MB at N=1,000), so
production keeps the `when` chain pending a map-size budget. Segments use
`embedFile`, not the `Db` blob, because `PlayWAV` resolves an archived name
([ADR 0021](./docs/adr/0021-mpq-embedded-file-surface.md)).
_Avoid_: soundlooper plugin, sound settings, random archive ids.

**noAirCollision helper**:
The port of euddraft's `noAirCollision`: 115 forward triggers hold 7,311
`SetMemory` actions that zero the game's collision table (`0x6D5CD8` points at
it) on every trigger loop. A first-loop `once` block reads the table's base
EPD and rewrites every action destination to `P, P + 1, ... P + 7,310` by
walking the trigger records at the fixed 602-EPD stride ([ADR 0024](./docs/adr/0024-noaircollision-scrubber-helper.md)).
_Avoid_: air-collision plugin, dummy action, collision mask, hitbox helper.
