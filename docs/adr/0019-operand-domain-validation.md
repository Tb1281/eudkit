# Operand domains are validated, not coerced

Status: accepted (2026-11-09; amended 2026-11-10, 2026-11-11, 2026-11-15)

> **Amendments.** 2026-11-10 extended the guard set from field and `Var` writes
> to container element writes and the death-table / raw-memory constructors.
> 2026-11-11 added the remaining death-table operands (`epd-from-address`
> `addr`, `ptr` `src`, hand-written `SetStringDword` `value`/`mask`/`modifier`).
> 2026-11-15 widened `isqrt`'s `value` to the full operand domain on both
> frontends, closing the last documented math-helper divergence.

## Context

Both frontends coerced the operand of a write or a comparison rather than
validating it: TypeScript's `number` admits every non-integer and `MemView`
fields are `any`, so the compiler caught none of it. Against the simulator
(`CUnit(1000).hp` is EPD `1002`), TS `u.hp.set("abc" | undefined | NaN |
Infinity)` built and wrote `0`; `1.5`, `"5"`, and `true` were silently
converted; py raised raw `ValueError`/`TypeError`/`OverflowError` from `int()`
or accepted the same coercion. `Var` operands were no better: TS `v.eq(1.5)`
built HIR identical to `v.eq(1)` and py `v.eq(1.5)` built a `DeferredCond`
silently. Writing `0` where the author meant `"abc"` is wrong at build time;
the frontends should say so at the call, with one error code.

## Decision

### 1. Plain integers only

The guard set is every value that reaches memory through a DSL handle: a write
(`set`/`iadd`/`isub`, `+=`/`-=`, var init, struct field init, const array) or a
comparison (`eq`/`ne`/`ge`/`le`/`gt`/`lt`, on all four comparison surfaces:
`MemFieldHandle`, `TriggerFieldRef`, `Var`, and a const-array value).

| family | py site | ts site |
|---|---|---|
| `Var` writes (`set`/`iadd`/`isub`/`act_set`/`act_add`/`act_sub`) and comparisons (`eq`/`ne`/`ge`/`le`/`gt`/`lt`) | `py/var.py` | `ts/var.ts` |
| var init (`Var(x)`, `lazyVar(x)`) | via `Var.set` (`py/var.py`) — already loud | `VarImpl` constructor (`ts/var.ts`) |
| field writes (`TriggerFieldRef` + `MemFieldHandle` `act_*` and their `set`/`iadd`/`isub` aliases), comparisons, and dunders | `py/view.py` | `ts/view.ts` (no dunders: JS has no operator overloading) |
| field augmented assignment (`+=` / `-=`, both field surfaces) | `py/view.py` | n/a — JS has no operator overloading |
| struct field inits (`S(v=…)`) | `py/struct.py` | `ts/struct.ts` |
| const-array literals (`FixedArray.const([...])`) and const-array comparisons (`.get(k).eq(…)`) | `py/array.py` | `ts/array.ts` |
| container element writes (`Queue.push`/`append`, `Deque.append`/`appendleft`, `Stack.push` / `FixedArray` dynamic set) | `py/queue.py`, `py/array.py` | `ts/queue.ts`, `ts/array.ts` |
| death-table / raw-memory constructors (`Deaths`/`DeathsX`/`SetDeaths`/`SetDeathsX`, `Memory*`, `SetMemory*`) — destination (`addr`), value, and the `ptr` `src`; plus the hand-written `SetStringDword` `value`/`mask`/`modifier` | `py/hir.py` (the two resolve functions, `SetStringDword`), `py/stock_rows_built.py` (`epd-from-address`, `ptr`) | `ts/hir.ts` |
| the 9 condition + 12 action HIR record fields (the same constructors, every field) | `py/hir.py` (`_build_cond`/`_build_act`) | `ts/hir.ts` (`buildCond`/`buildAct`) |
| `MemCond`'s `comparison` / `size_or_mask` parameters | `py/view.py` | `ts/view.ts` |
| `Var.iand` / `Var.ior` | already loud | `ts/var.ts` fast path |
| math-helper operand (`isqrt` `value`) | `py/mathf.py` | `ts/mathf.ts` |

A plain integer is a Python `int` that is not `bool`, or a TypeScript `number`
with `Number.isInteger(number) === true`. `bool` / `float` / `NaN` / `Infinity`
/ `string` / `None` / `undefined` / `null` raise **`EUDSL-TYPE`** **eagerly at
the call**, before any act is queued, naming the operand.

**Math helper.** `isqrt` (`py/mathf.py` / `ts/mathf.ts`) writes no lane: its
`value` accepts `int | Var | ReadonlyVar | Expr` through `to_var` / `toVar`
(`ReadonlyVar` forwards without a copy; an `Expr` costs the one temp
`tmp.set(expr)` would allocate). `bool`/`boolean` are rejected so the diagnostic
names the full domain; folding, range checks, and `int`/`Var` HIR are unchanged.

**Messages are not byte-pinned across frontends.** Tests assert the error
`code` plus a stable substring (`must be a plain integer` or `integer constant,
a Var, or an Expr`), never the whole sentence; prefixes and examples vary. py
names the type (`got a float`), ts the value (`got 1.5`), and the HIR-record
shape uses a semicolon. py guards every non-plain-int struct init at
`py/struct.py:295-301`; ts routes non-numbers through `fieldVars[k].set(v)`
(`ts/struct.ts:250-256`), naming `Var.set`.

**Absolute dword addresses must be 4-byte aligned.** A whole-dword access names
`EPD_BASE + 4 * epd`, so an unaligned `addr` is rejected eagerly with
`EUDSL-BOUNDS`: `memory address … is not 4-byte aligned` (`Memory`/`MemoryX`/
`SetMemory`/`SetMemoryX`) or `VarStore address … is not 4-byte aligned`
(`emitVarStore`'s `{"addr": …}`). Guards sit in `_resolve_value` /
`resolveStockValue` *before* `EPD()` folds, and in `queue_var_store` /
`queueVarStore` before the node is queued. `payload_init::epd` (floor) is the
eudplib-compatible authority; `eudkit_lir::epd`/`epd_u32` agree on aligned
input. This is **not** a byte-lane guard: `DatFieldHandle` floors an unaligned
*field* address and masks the lane — do not guard `EPD()`. `SetStringDword`
rejects an unaligned `byte_offset`; the `Db`/`DbRef` offset is a multiple of 4
and `lir2payload` aligns every item. Trigger-field `VarStore` targets are
whole-dword: `queue_var_store` / `queueVarStore` require plain-integer
`trigger_id`/`offset` and reject `offset % 4 != 0`.

### 2. Magnitude, runtime operands, and `1.0`

Any integer is normalised to 32 bits, as ADR 0017 §2 specifies:
`0x100000000` and `-1` stay legal, so `hp.set(0x100000000)` builds and writes
`0`; the `EUDSL-RANGE` loud-vs-normalise question stays open. `Var` /
`ReadonlyVar` / `Expr` operands remain valid (`v.set(w)`, `v.eq(v.add(1))`); the
guard narrows only the *constant* domain, and `atLeast`/`atMost`/`exactly`
inherit it through `ge`/`le`/`eq`. `Number.isInteger(1.0)` is `true` and py
rejects float `1.0` as part of §3's break, so parity is over integer **values**,
not literal spellings: `hp.set(1)` builds on both, `hp.set(1.0)` builds on TS
and raises on py; use `1.5` for shared rejects.

### 3. Deliberate breaking change in py

py's field writes lose their `int()` coercion: `True` → `1`, `1.5` → `1`,
`"5"` → `5` were accepted and now raise `EUDSL-TYPE`. Comparisons lose it too
on both surfaces and through the six dunders. Augmented assignment loses it as
well: the `int()` sat in `__iadd__`/`__isub__` ahead of the guarded
`act_add`/`act_sub`, so `f += 1.5`, `f -= "5"`, and `f += True` built silently
wrong acts. `u.hp += 1` used to queue the act then raise from the write-back
because `MemViewInstance.__setattr__` treated the returned handle as a value; it
now builds one act, HIR-identical to `u.hp.act_add(1)`.

**Const-array narrowing.** `ConstFixedArrayValue` is integer-convertible
(`py/array.py:377,380`), so `FixedArray.const([arr.get(0), 5])` built `[3, 5]`
and `CUnit(1000).hp.eq(arr.get(0))` folded a runtime table read. Both now raise
`EUDSL-TYPE`. The guard is not widened to `operator.index()` / `__index__`:
`TriggerFieldRef` is `__index__`-able (`py/view.py:349-352`), so that would
re-admit field references as silent operands. The only check is
`_require_plain_int`. **Migration: write an integer**; no existing test passes a
bool, float, string, or `None` to these surfaces, so the suites do not move.

### 4. Carve-outs, recorded rather than hidden

These are not tightened; each is an API-shape decision, not a coercion hole.

- **Same-field assignment is a no-op, py**: `MemViewInstance.__setattr__`
  ignores a write-back of a handle for the *same* field (compared on
  `schema`/`spec`/`baseEpd`, since `__getattr__` allocates a fresh handle),
  mirroring `TriggerStructInstance.__setattr__` (`py/struct.py:192-209`). This
  makes `u.hp += 1` work; `u.hp = u.shield` and `u.hp = 1.5` still raise. The
  base check is identity plus plain-int equality, never `==` on a `Var` base
  (whose `__eq__` returns a `DeferredCond`).
- **`Expr` construction is unvalidated**: `v.add("abc")` builds; a bad leaf is
  caught at materialisation (py `_materialize_operand`, ts
  `materializeOperand`), inside the guard set.
- **`Var` dunders validate the rejected scalar domain**: `v == 1.5`, `v ==
  "abc"`, and `v == True` raise `EUDSL-TYPE`, matching `v.eq(1.5)`; `Expr` and
  `ReadonlyVar` inherit it. `None` and unrelated objects return
  `NotImplemented`, preserving `v == None` (`False`).
- **Field dunders, py**: `TriggerFieldRef`'s `==`/`!=` route `int`, `float`,
  `str`, and `bool` through the guarded bodies, so `t.cond(0).player == "abc"`
  raises (was `False`); `MemFieldHandle`'s six dunders delegate, and ordering
  dunders on both surfaces raise for non-plain-ints.
- **Field dunders, ts: not applicable.** `MemFieldHandle` has no `valueOf`, and
  `TriggerFieldRef`'s `valueOf()` / `Symbol.toPrimitive()` return
  `initialValue`, so `u.hp == 1.5` is a plain JS comparison.
- **Integer parameters (`explicit_mask` / `explicit_id` / `customMask`) are not
  guarded**: `Var(0, explicit_mask=NaN)` yields mask `0` on TS (`>>> 0`) while
  py raises a raw `TypeError`; `Var(0, explicit_id=1.5)` builds on py.

## Consequences

- py field writes, comparisons, and augmented assignments are a deliberate
  source break; `u.hp += 1` now builds one act, and `u.hp = u.hp` is a no-op.
  TypeScript throws at the call where it used to build a no-op, write `0`, or
  build `Exactly 0`.
- The death-table constructors are a deliberate source break on both
  frontends: `Deaths(1002, Exactly, 1.5)`, `SetDeaths(1002, SetTo, "5")`,
  `MemCond(1002, 1.5, 1)`, and `SetDeathsX(1002, Add, 1, 0, 1.5)` now raise
  `EUDSL-TYPE` instead of writing a truncated lane. Relocation forms are
  unchanged and pinned by `Deaths(1002, Exactly, Var(0))` (mode 4),
  `Deaths(Var(0), Exactly, 1)` (mode 1), `SetDeaths(t.act(0).value, SetTo, 1)`,
  and `SetNextPtr(t0, t0)`. The seam requires a plain integer *after* the
  relocation check; `MemCond`'s `comparison`/`size_or_mask` are guarded at the
  call, `epd-from-address` `addr` before `EPD()` folds it, and `ptr` `src`
  accepts only a `Trigger` or `TriggerFieldRef`.
- **Container element writes** are validated at the call, before the first
  trigger: a plain integer, a `Var`, or a struct of matching arity, else
  `EUDSL-TYPE`. A scalar no longer broadcasts into a multi-field element:
  `Queue(Point, 4).push(1)` raises `container element shape must match the
  element: expected 2 field(s), got 1`. Both directions are pinned.
- **Record fields are validated-then-masked** on both frontends: 9 condition
  fields (`locid`, `player`, `amount`, `unit`, `comparison`, `condtype`,
  `restype`, `flags`, `eudx`) and 12 action fields (`mask`, `strid`, `wavid`,
  `time`, `dest`, `value`, `unit`, `acttype`, `modifier`, `flags`, `padding`,
  `eudx`) raise `EUDSL-TYPE` naming the field before any `&`/`>>>`. The record
  builders are the single chokepoint for hand-written and generated rows;
  `text`/`wav`/`uprp`/`str_index`/`relocs` are not integer operands.
- Runtime operands are unchanged, so `Expr` leaves are validated only at
  materialisation. `isqrt`'s domain is unified on `int | Var | ReadonlyVar |
  Expr`, with no constant fold or HIR byte changed.
- No HIR moves: every existing test writes integers, so the suites, cost gate,
  and `eudkit synth --compact` stay byte-identical. The cost goldens remain
  `355 triggers / 442 executed / 855,004 B`, and the `cost_gate.{py,ts}`
  `--compact` synth hashes to
  `4d152c2bd9455c2c9f0d494f8bca1e2bed82b0db5fd08040c6089cc0fd3d502d` on both
  frontends.
- Out of scope: the `EUDSL-RANGE` decision (ADR 0017), the masked-`Var` aliasing
  primitive, the `DeferredCond`/`DeferredNe` protocol, and `EUDSL-WARN`.

## Rejected alternatives

- **(a) Keep coercing.** Rejected: `"abc"`/`NaN` become `0`, and the frontends
  disagree on the failure mode.
- **(b) Validate at materialisation.** Rejected: errors surface far from the
  offending line, after acts are queued.
- **(c) Accept `bool`/`"5"` because `int()` does, or integral floats in py.**
  Rejected: both are the coercion being removed, and `set(1.0)` building while
  `set(1.5)` raises is a worse trap.
- **(d) Reject on magnitude.** Rejected: ADR 0017 §2 pins 32-bit normalisation;
  the failure mode is the separate `EUDSL-RANGE` question.
- **(e) Leave field comparisons as a carve-out.** Rejected: the old parity
  claim held only for truncating rows — TS builds `Exactly 0` for `"abc"` /
  `NaN` / `undefined` / `null` while py raises.
- **(f) Widen the guard to `__index__` / `operator.index()`.** Rejected:
  `TriggerFieldRef` is `__index__`-able (`py/view.py:349-352`), so
  `f += other_field` would silently read a build-time `initialValue` into a
  lane — the exact defect class this ADR removes.
