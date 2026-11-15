# Operand domains are validated, not coerced

Status: accepted (2026-11-09; amended 2026-11-10, 2026-11-11)

## Context

Both frontends coerce the operand of a write or a comparison instead of
validating it. TypeScript's `number` admits every non-integer and `MemView`
fields are `any`, so the compiler catches none of it. Measured before this change
against the native simulator (`CUnit(1000).hp` is EPD `1002`):

| written operand | TS `u.hp.set(x)` | py `u.hp.set(x)` |
|---|---|---|
| `"abc"` | builds, writes **0** | `ValueError` (raw, from `int()`) |
| `undefined` / `None` | builds, writes **0** | `TypeError` (raw, from `int()`) |
| `true` / `True` | builds, writes **1** | builds, writes **1** |
| `1.5` | builds, writes **1** | builds, writes **1** |
| `"5"` | builds, writes **5** | builds, writes **5** |
| `NaN` | builds, writes **0** | `ValueError` (raw, from `int()`) |
| `Infinity` | builds, writes **0** | `OverflowError` (raw, from `int()`) |

`Var` operands are no better: TS `v.eq(1.5)` builds HIR **identical to
`v.eq(1)`**, `v.eq(NaN)` builds `eq(0)`, and `v.eq("abc")` / `v.set(true)`
build a `DeferredCond` or no-op that only dies later with a raw
`TypeError`. py `v.set(1.5)` raises a bare, unlabelled
`TypeError: Unsupported Var.set operand: 1.5`, and `v.eq(1.5)` builds a
`DeferredCond` silently, failing only at materialisation.

A program that writes `0` where it meant `"abc"` is wrong at build time; the
frontends should say so at the call, with one error code on both.

## Decision

### 1. Plain integers only

Every value the guard set validates is listed in the table below. The test is
"does this value reach memory through a DSL handle?" — a write
(`set`/`iadd`/`isub`, `+=`/`-=`, var init, struct field init, const array) or a
comparison (`eq`/`ne`/`ge`/`le`/`gt`/`lt`, on all four comparison surfaces:
`MemFieldHandle`, `TriggerFieldRef`, `Var`, and a const-array value). Every
value that reaches a lane is now validated, including the death-table seam
closed in cycle 16: the two resolve functions every `Deaths`-table and
raw-memory constructor funnels through, the 9 condition + 12 action record
fields behind them, and `MemCond`'s own two DSL parameters. Cycle 17 then
closed the last three operands that bypassed those guards: the
`epd-from-address` `addr` (guarded *before* `EPD()` folds it into a lane), the
`ptr` `src` (guarded before `.next` is read), and the hand-written
`SetStringDword` rows' `value`/`mask`/`modifier` (guarded before masking). §6
lists every deliberate carve-out with its measured divergence, and no entry
there puts an unguarded operand into a lane. Cycle 14 wrote the first sentence
of this paragraph as "the domain is every value that reaches memory"; measured,
that was false at the death-table seam, and cycle 16 closed it rather than
renamed it. Cycle 16's claim that it had closed *every* lane-writing path was
in turn false at the three sites above until cycle 17 fixed them.

| family | py site | ts site | closed by |
|---|---|---|---|
| `Var` writes (`set`/`iadd`/`isub`/`act_set`/`act_add`/`act_sub`) | `py/var.py` | `ts/var.ts` | cycle 13 |
| `Var` comparisons (`eq`/`ne`/`ge`/`le`/`gt`/`lt`) | `py/var.py` | `ts/var.ts` | cycle 13 |
| var init (`Var(x)`, `lazyVar(x)`) | via `Var.set` (`py/var.py`) — already loud | `VarImpl` constructor (`ts/var.ts`) | cycle 14 (ts) |
| field writes (`TriggerFieldRef` + `MemFieldHandle` `act_*` and their `set`/`iadd`/`isub` aliases) | `py/view.py` | `ts/view.ts` | cycle 13 |
| field augmented assignment (`+=` / `-=`, both field surfaces) | `py/view.py` | n/a — JS has no operator overloading | cycle 15 (py only) |
| field comparisons (the six bodies × both field surfaces) | `py/view.py` | `ts/view.ts` | cycle 14 |
| field dunders (`==`/`!=`/`<`/`<=`/`>`/`>=`) | `py/view.py` | n/a — JS has no operator overloading | cycle 14 (py only) |
| struct field inits (`S(v=…)`) | `py/struct.py` | `ts/struct.ts` | cycle 14 |
| const-array literals (`FixedArray.const([...])`) and const-array comparisons (`.get(k).eq(…)`) | `py/array.py` | `ts/array.ts` | cycle 14 |
| container element writes (`Queue.push`/`append`, `Deque.append`/`appendleft`, `Stack.push` / `FixedArray` dynamic set) | `py/queue.py`, `py/array.py` | `ts/queue.ts`, `ts/array.ts` | cycle 16 |
| death-table / raw-memory constructors (`Deaths`/`DeathsX`/`SetDeaths`/`SetDeathsX`, `Memory*`, `SetMemory*`) — destination (`addr`), value, and the `ptr` `src`; plus the hand-written `SetStringDword` `value`/`mask`/`modifier` | `py/hir.py` (the two resolve functions, `SetStringDword`), `py/stock_rows_built.py` (`epd-from-address`, `ptr`) | `ts/hir.ts` | cycle 16–17 |
| the 9 condition + 12 action HIR record fields (the same constructors, every field) | `py/hir.py` (`_build_cond`/`_build_act`) | `ts/hir.ts` (`buildCond`/`buildAct`) | cycle 16 |
| `MemCond`'s `comparison` / `size_or_mask` parameters | `py/view.py` | `ts/view.ts` | cycle 16 |
| `Var.iand` / `Var.ior` | already loud | `ts/var.ts` fast path | cycle 14 (ts only) |

A plain integer is a Python `int` that is not `bool`, or a TypeScript `number`
with `Number.isInteger(number) === true`. `bool` / `float` / `NaN` /
`Infinity` / `string` / `None` / `undefined` / `null` are rejected with
**`EUDSL-TYPE`**, raised **eagerly at the call** — before any act is queued —
with the operand name in the message.

**Messages are not byte-pinned across frontends.** A test asserts the error
`code` plus one stable substring, never the whole sentence. The shapes in use
after cycle 17:

| family | shape | py example | ts example |
|---|---|---|---|
| field write / field init / const-array value | `<what> must be a plain integer, got <value>` | `field write must be a plain integer, got a float` | `field write must be a plain integer, got 1.5` |
| field augmented assignment (`+=` / `-=`) | `field write must be a plain integer, got <value>` | `field write must be a plain integer, got a float` | n/a — TS has no operator overloading |
| field comparison / const-array comparison | `field comparison must be a plain integer, got <value>` | `field comparison must be a plain integer, got 'abc'` | `field comparison must be a plain integer, got "abc"` |
| death-table destination / value (cycle 16, the two resolve functions) | `memory destination` / `memory value must be a plain integer, got <value>` | `memory value must be a plain integer, got a float` | `memory value must be a plain integer, got 1.5` |
| `epd-from-address` `addr` / `ptr` `src` (cycle 17, guarded before `EPD()` / `.next`) | `memory address` / `src must be a plain integer, got <value>` | `memory address must be a plain integer, got a float` | `src must be a plain integer, got null` |
| hand-written `SetStringDword` field (cycle 17, guarded before masking) | `SetStringDword <field> must be a plain integer, got <value>` | `SetStringDword mask must be a plain integer, got a float` | `SetStringDword modifier must be a plain integer, got true` |
| HIR record field (cycle 16, `_build_cond` / `buildCond`) | `condition field <name> must be a plain integer; got <value>` (`action field <name>` for actions) | `condition field comparison must be a plain integer; got a float` | `action field modifier must be a plain integer; got 1.5` |
| `MemCond` parameter (cycle 16) | `MemCond <param> must be a plain integer, got <value>` | `MemCond comparison must be a plain integer, got a float` | `MemCond size_or_mask must be a plain integer, got 1.5` |
| struct field init, **numeric** | `struct field init must be a plain integer, got <value>` | `struct field init must be a plain integer, got a float` | `struct field init must be a plain integer, got 1.5` |
| struct field init, **non-number** | ts routes the value through `Var.set`; py rejects it at the struct guard | `struct field init must be a plain integer, got 'abc'` | `Var.set operand must be an integer constant, a Var, or an Expr; got "abc"` |
| `Var` write | `Var.<op> operand must be an integer constant, a Var, or an Expr; got <value>` | `Var.set operand must be an integer constant, a Var, or an Expr; got a float` | `Var.set operand must be a plain integer, got 1.5` |
| `Var` comparison | `Var.<op> operand must be an integer constant, a Var, or an Expr; got <value>` | `Var.eq operand must be an integer constant, a Var, or an Expr; got a float` | `Var.eq operand must be a plain integer, got 1.5` |

The py `Var.set` / ts `Var.set` difference for the same input is **allowed and
recorded** (N4 resolution: documented, not aligned) — py names the type
(`got a float`), ts names the value (`got 1.5`). The stable substrings are
`must be a plain integer` and `integer constant, a Var, or an Expr`.

The two struct-init rows exist because the frontends guard different places
(cycle-15 measurement, correcting the single row cycle 14 wrote): py checks
every non-plain-int init at `py/struct.py:295-301` and names the struct, while
ts only runs `requirePlainInt` for `typeof val === "number"`
(`ts/struct.ts:199`) and applies everything else through `fieldVars[k].set(v)`
(`ts/struct.ts:250-256`), which names `Var.set`. Measured: ts `S({v: 1.5})` →
`struct field init must be a plain integer, got 1.5`; ts `S({v: "abc"})` →
`Var.set operand must be an integer constant, a Var, or an Expr; got "abc"`;
py names the struct shape for both.

**Absolute dword addresses must be 4-byte aligned (cycle 25, #111).** A
whole-dword read or write names one of `EPD_BASE + 4 * epd`, so an unaligned
`addr` is rejected eagerly at the frontend with `EUDSL-BOUNDS` and the message
`memory address 0x57f0b2 is not 4-byte aligned` (`epd-from-address` conditions
and actions: `Memory`/`MemoryX`/`SetMemory`/`SetMemoryX`) or `VarStore address
0x57f0b2 is not 4-byte aligned` (`emitVarStore`'s `{"addr": …}` target). The
guard sits in the two shared seams — `py/stock_rows_built.py` `_resolve_value`
and `ts/hir.ts` `resolveStockValue` guard *before* `EPD()` folds the address,
and `py/hir.py` `queue_var_store` / `ts/hir.ts` `queueVarStore` guard before
the HIR node is queued. Before the fix the conversions disagreed silently:
`Memory(0x57F0B2)` floored to `player: 4294955859` (the dword at `0x57F0B0`)
while `emitVarStore(…, {"addr": 0x57F0B2})` truncated to `4294955860` (the
dword at `0x57F0B4`), with no error in HIR or `dump-ir`. `payload_init::epd`
(floor) is the eudplib-compatible authority; `eudkit_lir::epd`/`epd_u32`
(truncating) agree with it exactly on aligned input, which is why alignment is
this seam's job and neither converter changes. This is **not** a byte-lane
guard: `DatFieldHandle` (`py/dat.py:160-170`, `ts/dat.ts:109-120`)
deliberately floors an unaligned *field* address and masks the byte lane,
which names a byte inside the containing dword rather than a whole-dword
access — do not fold that path into this check, and do not guard `EPD()`
itself, which would break every unaligned DAT field. `SetStringDword` is a
different case: it **rejects** an unaligned `byte_offset` eagerly
(`EUDSL-BOUNDS`, `py/hir.py:641-646` / `ts/hir.ts:944-949`), and it is the
only producer of `target_string` relocations, so the mode-1 string-reloc
path (`module_string_addr`, `crates/eudkit-lower/src/lib.rs:81-99`, adds the
offset raw) never sees an unaligned offset. The `Db`/`DbRef` path needs no
new guard either: its byte offset is already a non-negative multiple of 4
and `lir2payload` starts every payload item at `align4(previous end)`, so
the blob payload base is compiler-assigned and dword-aligned **by layout**
(cycle 26, #120) — the parity-correct fix was aligning that base, not
guarding correct code.

Trigger-field `VarStore` targets are whole-dword writes: `queue_var_store` /
`queueVarStore` require plain-integer `trigger_id` and `offset` (default `0`)
and reject `offset % 4 != 0` with `EUDSL-BOUNDS`; sub-dword field writes are
byte-lane operations and do not belong in this seam.

### 2. Magnitude is not part of the domain

Any integer is accepted and normalised to 32 bits, exactly as ADR 0017 §2
already specifies: `0x100000000` and `-1` stay legal, so
`hp.set(0x100000000)` still builds and writes `0`. The `EUDSL-RANGE`
loud-vs-normalise question for out-of-domain literals stays open where ADR
0017's Consequences left it; this ADR does not decide it, and the guards do
not reject on magnitude.

### 3. Runtime operands stay legal

`Var` / `ReadonlyVar` / `Expr` remain valid where they are valid today
(`v.set(w)`, `v.eq(w)`, `v.eq(v.add(1))`, `v.set(expr)`). The guard narrows
only the *constant* domain. `atLeast` / `atMost` / `exactly` inherit the rule
through `ge` / `le` / `eq`.

### 4. TS cannot distinguish `1.0` from `1`

TS cannot distinguish `1.0` from `1`: there is one `number` type and
`Number.isInteger(1.0)` is `true`. py **rejects** float `1.0` as part of §5's
break. The parity claim is therefore over integer **values**, not literal
spellings: `hp.set(1)` builds on both frontends, `hp.set(1.0)` builds on TS and
raises `EUDSL-TYPE` on py. Use a non-integral float (`1.5`) for shared reject
rows; the two operand-domain suites carry a **per-frontend** `1.0` row (py
expects `EUDSL-TYPE`, TS expects a build) and that row is a decision pin, not a
parity test. Do not read the matrices as cross-frontend parity for
float-valued integers.

### 5. Deliberate breaking change in py

py's field writes lose their `int()` coercion. `True` → `1`, `1.5` → `1`,
`"5"` → `5` were accepted before and are now `EUDSL-TYPE`.

Field **comparison** operands lose their `int()` coercion too. `hp.eq(1.5)` /
`hp.eq("5")` / `hp.eq(True)` built `eq(1)` / `eq(5)` / `eq(1)` before and are
now `EUDSL-TYPE`, on both field surfaces (`MemFieldHandle` and
`TriggerFieldRef`) and through the six comparison dunders.

Field **augmented assignment** loses its `int()` coercion too (cycle 15). The
`int()` sat in `__iadd__`/`__isub__` ahead of the already-guarded
`act_add`/`act_sub`. Measured before: `f = CUnit(1000).hp; f += 1.5` built
`SetDeathsX` `Add 1`, `f -= "5"` built `Subtract 5`, `f += True` built `Add 1`
(all three silently wrong values), and `f += float("nan")` was a raw
`ValueError`. All four are now `EUDSL-TYPE` on both field surfaces and both
spellings. The attribute form was broken even with a *valid* operand:
`u.hp += 1` queued the act and then raised `EUDSL-TYPE` from the attribute
write-back (253 chars of act against a 39-char control), because
`MemViewInstance.__setattr__` treated the handle `__iadd__` returned as a
value. It now builds exactly one act, HIR-identical to `u.hp.act_add(1)`.

**Const-array narrowing (N2).** `ConstFixedArrayValue` is integer-convertible
(`py/array.py:377,380`), so it used to pass as an operand. Measured before,
with `arr = FixedArray.const([3, 4])`: `FixedArray.const([arr.get(0), 5])`
built `[3, 5]` and `CUnit(1000).hp.eq(arr.get(0))` folded `amount: 3` — a
constant that came from a runtime table read. Both are now `EUDSL-TYPE`
(`const array value must be a plain integer` / `field comparison must be a
plain integer`). The guard is working as intended — a handle is not a
constant — but it is a narrowing, so it is recorded here rather than left to
be discovered.

The guard is deliberately **not** widened to `operator.index()` /
`__index__`. `TriggerFieldRef` implements `__index__` (`py/view.py:349-352`),
so an `__index__`-accepting operand check would re-admit field references as
silent operands — exactly the defect class this ADR removes. The only check
remains `_require_plain_int`: a Python `int` that is not `bool`.

**Migration: write an integer.** No test in `py/tests` or `ts/tests` passes a
bool, float, string, or `None` to a field write, a field comparison, or a `Var`
operand, so the suites and the cost gate do not move.

### 6. Carve-outs, recorded rather than hidden

These are **not** tightened in this cycle. Each is named with the divergence it
actually has, so the boundary is a decision, not an omission. §1's table is the
guard set, and **no entry here puts an unguarded operand into a lane**: every
entry below is an API-shape decision (a Python dunder protocol, an expression
constructor, a parameter that is not a lane operand), not a coercion hole.
Cycle 16 emptied this section of its two *lane-writing* entries (`MemCond`, the
raw HIR constructors), and cycle 17 closed the three lane-writing operands that
remained inside the death-table ticket's claimed scope (`epd-from-address`
`addr`, `ptr` `src`, `SetStringDword`'s fields); "§6 is empty" in the
cycle-16/17 scans means empty **of unguarded-lane entries** — the six decisions
below stay, because deleting them would delete a decision rather than a
defect.

- **Same-field assignment is a no-op, py** — `u.hp = u.hp` does not raise:
  `MemViewInstance.__setattr__` ignores a write-back of a handle for the *same*
  field, mirroring `TriggerStructInstance.__setattr__` (`py/struct.py:192-209`;
  measured, `t.v = t.v` emits nothing). `__getattr__` allocates a fresh
  `MemFieldHandle` per access, so the comparison is on
  `schema`/`spec`/`baseEpd`, not identity. This is what makes `u.hp += 1` work
  (the augmented assignment ends in a write-back of the handle `__iadd__`
  returned) and it is a decision, pinned by a test; `u.hp = u.shield` and
  `u.hp = 1.5` still raise `EUDSL-TYPE`. The base comparison is an identity
  check plus a **plain-int** equality check, never a `==` on two bases: a `Var`
  base's `__eq__` returns a `DeferredCond` (a runtime condition), so
  `CUnit(Var(0)); u.hp = u.hp` used to truth-test a condition while building
  and raised `EUDSL-COND-TRUTHIFY` — the wrong fix for the right guard
  (cycle-15 review S2). It is now the `CUnit(Var(0))` control.
- **`Expr` construction is unvalidated**: `v.add("abc")` builds an `Expr`; a
  bad leaf is caught when the expression is materialised (py
  `_materialize_operand`, ts `materializeOperand`), which is inside the guard
  set. Measured: `v.add("abc")` builds, `v.set(v.add("abc"))` raises
  `EUDSL-TYPE` at materialisation.
- **`Var` comparison dunders validate the rejected scalar domain**:
  `v == 1.5`, `v == "abc"`, and `v == True` raise `EUDSL-TYPE`, matching
  `v.eq(1.5)`; `Expr` and `ReadonlyVar` inherit the same rule, and the
  ordering dunders (`<`/`<=`/`>`/`>=`) delegate and therefore raise. `None`
  and unrelated objects still return `NotImplemented`, preserving `v == None`
  as an idiom (`False`) rather than an error. Measured: `v == 1.5` →
  `EUDSL-TYPE`, `v == None` → `False`.
- **Field dunders, py**: `TriggerFieldRef`'s `==`/`!=` route `int`, `float`,
  `str`, and `bool` through the guarded comparison bodies, so
  `t.cond(0).player == "abc"` raises `EUDSL-TYPE` (was `False`); only
  `== None` and other unrelated objects keep `NotImplemented` and stay
  `False`. A *numeric* operand that is not a plain integer still raises
  (`== 1.5` → `EUDSL-TYPE`, was `eq(1)`). `MemFieldHandle`'s six dunders
  delegate unconditionally, so `CUnit(1000).hp == "abc"` is `EUDSL-TYPE`
  (was a raw `ValueError`). The ordering dunders on both surfaces raise for
  every non-plain-int (measured before the fix: `t.cond(0).player >= "abc"`
  was a raw `ValueError`).
- **Field dunders, ts: not applicable.** TS has no operator overloading:
  `MemFieldHandle` has no `valueOf`, and `TriggerFieldRef`'s `valueOf()` /
  `Symbol.toPrimitive()` return `initialValue`, so `u.hp == 1.5` is a plain JS
  value comparison and is outside the operand domain. Measured: `u.hp == 1.5`
  is `false` and builds nothing. This is a frontend divergence recorded as
  such, not as parity.
- **Integer parameters (`explicit_mask` / `explicit_id` / `customMask`) are not
  guarded** — the separate "integer parameters" micro-audit. Measured:
  `Var(0, explicit_mask=NaN)` yields a mask of `0` on TS (`>>> 0`), while py
  raises a raw `TypeError`; `Var(0, explicit_id=1.5)` builds on py with id
  `1.5`. Deliberately left, recorded with the divergence.

## Consequences

- py field writes, field comparisons, and field augmented assignments are a
  deliberate source break: programs that passed `True`, `1.5`, or `"5"` now
  fail at build time with `EUDSL-TYPE` instead of writing `1`, `1`, or `5` (or
  comparing against them), and `f += 1.5` / `f -= "5"` no longer build
  `Add 1` / `Subtract 5`. `u.hp += 1`, which used to raise *after* emitting the
  act, now builds exactly one act; `u.hp = u.hp` is a no-op instead of an
  error.
- TypeScript throws at the call where it used to build a no-op, write `0`, or
  build `Exactly 0`; the error is `EUDSL-TYPE` with the operand named, not a
  raw `TypeError` from deep in materialisation.
- The death-table constructors are a deliberate source break on both
  frontends: `Deaths(1002, Exactly, 1.5)`, `SetDeaths(1002, SetTo, "5")`,
  `MemCond(1002, 1.5, 1)` and `SetDeathsX(1002, Add, 1, 0, 1.5)` now fail at
  build time with `EUDSL-TYPE` instead of writing a truncated lane. The
  relocation forms are unchanged and pinned by a control test:
  `Deaths(1002, Exactly, Var(0))` (mode 4), `Deaths(Var(0), Exactly, 1)`
  (mode 1), `SetDeaths(t.act(0).value, SetTo, 1)`, and `SetNextPtr(t0, t0)`.
- Runtime operands (`Var` / `ReadonlyVar` / `Expr`) are unchanged, so `Expr`
  leaves are still validated only at materialisation.
- §6 is the complete carve-out list, and it now has no lane-writing entry.
  Cycles 16 and 17 closed both halves of the operand-domain headline. **Ticket A**
  closed the queue/deque element write (`Queue.push`/`append`,
  `Deque.append`/`appendleft`, and the `FixedArray` element row behind
  `Stack.push`): the element is validated at the call, before the first trigger
  of the write — a plain integer, a `Var`, or a struct of matching arity, else
  `EUDSL-TYPE` with the operand named, and `EUDSL-TYPE` with
  `shape must match the element` for an arity mismatch. Measured before that
  fix, ts `new Queue("dword", 4).push(1.5)` built HIR **byte-identical to
  `push(1)`**, py raised a raw `AttributeError: 'float' object has no attribute
  'value'` from `pageSlotBitCond`, and a 3-field struct into a 2-field element
  silently dropped a field (`177174` against the correct `177139`).
  **Ticket B** closed the death-table seam: the two resolve functions every
  `Deaths`/`DeathsX`/`SetDeaths`/`SetDeathsX` and generated `Memory*`/
  `SetMemory*` row funnels through now require a plain integer *after* the
  relocation check (a `Var`/`TriggerFieldRef`/`Trigger` still relocates), and
  `MemCond`'s `comparison`/`size_or_mask` are guarded at the call so the error
  names the parameter the author wrote. Measured before: `Deaths(1002, Exactly,
  1.5)` built `amount: 1`, `Deaths(1.5, Exactly, 1)` built `player: 1`,
  `MemCond(1002, "==", 1.5)` built `amount: 1`, and ts `Deaths(1002, Exactly,
  "abc")` built `amount: 0`.
  **Cycle 17** closed the three operands that still bypassed those guards, all
  inside that ticket's claimed scope: the `epd-from-address` `addr` is now
  guarded *before* `EPD()` folds it (subject `memory address`), the `ptr` `src`
  accepts only a `Trigger` or a `TriggerFieldRef` and never reads `.next` before
  the type check (subject `src`), and the hand-written `SetStringDword` guards
  `value`/`mask`/`modifier` before masking. Measured before: ts
  `Memory(1.5 | NaN | Infinity | "abc" | true, Exactly, 1)` and
  `SetMemory(1.5, SetTo, 1)` built the wrong lane `player`/`dest: 4293515047`
  (a silent wrong memory write, accepted by `dump-ir`); py `Memory(True, …)`
  built the same lane while the other py rows raised a raw `TypeError`; py
  `SetNextPtr(1.5 | "abc" | None | True, t)` raised a raw `AttributeError` and
  ts named `undefined` instead of the operand; ts `SetStringDword(0, 0, 1.5)`
  built `"value": 1`, `SetStringDword(0, 0, 1, 1.5)` built `"mask": 1,
  "eudx": 17235` (a different act classification), and `modifier=true` built
  `modifier: 1`, with py `True` building silently in the same fields.
- **A scalar no longer broadcasts into a multi-field element** (review N1,
  recorded here because the decision previously lived only in a code comment):
  `Queue(Point, 4).push(1)` used to spread the scalar across every field of the
  struct slot and now raises `EUDSL-TYPE` with `container element shape must
  match the element: expected 2 field(s), got 1`. This is **deliberate**: a
  scalar is one value, so
  it only fits a scalar element; a multi-field element takes a struct of
  matching arity (matched by position, see the glossary). Both directions are
  pinned — the scalar-into-struct rejection and the scalar-into-scalar build —
  so re-broadcasting it later cannot pass silently. Behaviour is unchanged from
  cycle 16; cycle 17 only writes the decision down.
- **Record fields are now validated-then-masked**, on both frontends: 9
  condition fields (`locid`, `player`, `amount`, `unit`, `comparison`,
  `condtype`, `restype`, `flags`, `eudx`) and 12 action fields (`mask`, `strid`,
  `wavid`, `time`, `dest`, `value`, `unit`, `acttype`, `modifier`, `flags`,
  `padding`, `eudx`) raise `EUDSL-TYPE` naming the field before any `&`/`>>>`.
  Measured before: `Deaths(1002, 1.5, 1)` built `comparison: 1` on ts,
  `SetDeaths(1002, 1.5, …)` built `modifier: 1`, and `unit`/`mask` floats were
  silently truncated on ts while py raised a raw `TypeError`. The record
  builders are the **single chokepoint** every death-table constructor
  converges on, hand-written rows and generated stock rows alike; the old
  objection that guarding them "duplicates the call-site checks" was the wrong
  frame, because the call-site guards are per-constructor and the record guard
  is the backstop for the hand-written ones (the stock validators already
  reject non-ints with `EUDSL-BOUNDS`). The stock-row resolver therefore no
  longer masks `mask` / `u32-wrap` parameters itself — it passes them through
  so there is exactly one masking site, after the guard. `text`/`wav`/`uprp`/
  `str_index`/`relocs` are not integer operands and stay out.
- No HIR moves: every existing test writes integers, so the py and TS suites,
  the cost gate, and `eudkit synth --compact` stay byte-identical. Measured
  after the cycle-15 change: py `617 → 623` (six new tests, all green), ts
  `593 pass`; measured after the cycle-16 changes: py `623 → 628 → 644`
  (six + sixteen new tests), ts `593 → 598 → 612`, `cargo test --workspace`
  363 including cost gates `4/4`; and after cycle 17: py `644 → 648`, ts
  `612 → 616` (three reject matrices + one literal-record control on each
  frontend), the cost goldens still `355 triggers / 442 executed / 855,004 B`,
  and
  `eudkit synth crates/eudkit-cli/tests/fixtures/cost_gate.{py,ts} --compact`
  hashes to
  `4d152c2bd9455c2c9f0d494f8bca1e2bed82b0db5fd08040c6089cc0fd3d502d` on both
  frontends.
- Out of scope: the `EUDSL-RANGE` failure-mode decision (ADR 0017), the
  masked-`Var` aliasing primitive, the `DeferredCond` / `DeferredNe` protocol,
  and `EUDSL-WARN`.

## Rejected alternatives

- **(a) Keep coercing (`int()` / `>>> 0`) and accept everything.** Rejected:
  coercion is the defect — `"abc"` and `NaN` silently become `0` — and the two
  frontends disagree on the failure mode.
- **(b) Validate at materialisation instead of at the call.** Rejected:
  errors surface far from the offending line, after acts are queued; eager is
  the loud behaviour this decision buys.
- **(c) Accept `bool` as `0`/`1` and `"5"` as `5` because Python's `int()`
  does.** Rejected: a stringly-typed operand domain is exactly the coercion
  being removed, and `True` as `1` is an accident of the type system.
- **(d) Reject on magnitude.** Rejected: ADR 0017 §2 pins 32-bit
  normalisation; changing the failure mode of out-of-domain literals is the
  separate `EUDSL-RANGE` question and is not decided here.
- **(e) Leave field comparisons as a carve-out.** Rejected: the old
  justification claimed parity, but parity holds only for the truncating rows —
  TS builds `Exactly 0` for `"abc"` / `NaN` / `undefined` / `null` while py
  raises, which is exactly the silent wrong answer about memory this decision
  exists to remove. Cycle 14 closes them instead.
- **(f) Accept integral floats in py.** Rejected: it reintroduces the coercion
  this ADR removes and creates a worse trap — `set(1.0)` builds while
  `set(1.5)` raises, both from ordinary float arithmetic. py stays strict; the
  `1.0` rows are per-frontend decision pins, not parity tests (§4).
- **(g) Widen the operand guard to `__index__` / `operator.index()`.** Rejected:
  it would accept any integer-convertible *object*, and `TriggerFieldRef` is
  `__index__`-able (`py/view.py:349-352`) — so `f += other_field` would silently
  read a build-time `initialValue` and write it into a lane, which is the exact
  defect class this ADR removes. The domain is the plain `int`, not "anything
  that can be converted to one" (§5).
