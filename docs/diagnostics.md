# Diagnostics reference (`EUDSL-*`)

The **16** canonical `EUDSL-*` codes span the Python and TypeScript frontends,
the opt-in TypeScript truthiness linter (`EUDSL-TS-TRUTHIFY`), and the CLI plan
loader (`EUDSL-PLAN`). Fourteen are errors and two are warnings
(`EUDSL-WARN-*`). Three are frontend-specific (`EUDSL-COND-TRUTHIFY` and
`EUDSL-READONLY` are Python-only; `EUDSL-TS-TRUTHIFY` is an opt-in TypeScript
lint); the rest are shared. The warnings are printed as `warning: EUDSL-…` on
stderr and do not fail a build; pass `--no-warnings` to `synth`, `dump-ir`, or
`compile` to silence them.

The codes are emitted by the DSL frontends (`py/`, `ts/`) and by the CLI's
plan loader (`crates/eudkit-cli/src/plan.rs`). Every code below was checked
against those sources. Where an architecture decision governs the behaviour,
the ADR is linked; where no ADR exists, the owning source file is named.

| Code | Kind | One-line meaning | ADR / source |
|---|---|---|---|
| `EUDSL-ASCII` | error | Non-ASCII fill byte in a format spec | [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md) |
| `EUDSL-BOUNDS` | error | Index, capacity, or trigger slot count out of range | [ADR 0003](./adr/0003-capacity-aware-aligned-string-writes.md), [0009](./adr/0009-typed-dat-accessors.md), [0019](./adr/0019-operand-domain-validation.md) |
| `EUDSL-CMP` | error | Unknown `MemCond` comparison name | [ADR 0019](./adr/0019-operand-domain-validation.md) §6 |
| `EUDSL-COND-TRUTHIFY` | error | Python `if`/`while`/`assert`/`and`/`or`/`not` on a runtime condition | [ADR 0020](./adr/0020-build-time-vs-runtime-conditions.md) |
| `EUDSL-DIV-ZERO` | error | Constant division or modulo by zero | `py/var.py`, `ts/var.ts` |
| `EUDSL-FORMAT` | error | Malformed `FixedString` format string | [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md) |
| `EUDSL-PLAN` | error | Frozen-plan JSON version is not supported | [ADR 0013](./adr/0013-optional-tick-gate-for-single-chain-maps.md), `crates/eudkit-cli/src/plan.rs` |
| `EUDSL-RANGE` | error | Numeric value outside a documented range | [ADR 0019](./adr/0019-operand-domain-validation.md) |
| `EUDSL-READONLY` | error | Mutation through a Python `ReadonlyVar` | [ADR 0005](./adr/0005-lazy-global-declarations.md) |
| `EUDSL-SCOPE` | error | Declaration or DSL call outside `buildModule`, or an unresolvable lazy handle | [ADR 0005](./adr/0005-lazy-global-declarations.md), [0012](./adr/0012-explicit-entry-module-export.md) |
| `EUDSL-TARGET` | error | A target that must be a constant is dynamic | [ADR 0009](./adr/0009-typed-dat-accessors.md), [0004](./adr/0004-triggerpage-transfer-parking-and-curpl-reset.md) |
| `EUDSL-TS-TRUTHIFY` | error (opt-in lint) | TypeScript runtime condition used as a JS boolean | [ADR 0020](./adr/0020-build-time-vs-runtime-conditions.md) |
| `EUDSL-TYPE` | error | Operand outside its accepted domain | [ADR 0019](./adr/0019-operand-domain-validation.md) |
| `EUDSL-UNKNOWN-FIELD` | error | Unknown field name on a struct schema | `py/struct.py`, `ts/struct.ts` |
| `EUDSL-WARN-PERF` | warning | Two or more bare decimal placeholders in one format string | [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md) |
| `EUDSL-WARN-SCOPE` | warning | `lazy*` declaration inside `buildModule` | [ADR 0005](./adr/0005-lazy-global-declarations.md) |

---

## `EUDSL-ASCII`

**Meaning.** A format-spec fill byte is outside ASCII. The v1 string formatter
only emits ASCII bytes.

**Trigger.** `s.write("{:٤>4d}", v)` (Arabic-Indic digit as fill) or any fill
character with a code point above `0x7F`.

**Before**

```python
s.write("{:٤>4d}", v)
```

**After**

```python
s.write("{:\x1a>4d}", v)   # invisible StarCraft: Remastered pad byte
s.write("{:0>4d}", v)      # zero fill
s.write("{:>4d}", v)       # space fill (default)
```

**ADR.** [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md).

## `EUDSL-BOUNDS`

**Meaning.** A fixed capacity, index, field count, or slot count is outside
its legal range. It is a structural bound, not a type or value-range error.

**Trigger.**

- `FixedArray("dword", 0)`, `FixedString(0)`, negative sizes.
- A raw `Trigger` with more than 16 condition or 64 action entries.
  Structured forms (`when`, `once`, `until`, `whileLoop`, `onRising`,
  `onFalling`) are not subject to the 16-condition cap: lowering auto-chunks
  each block of up to 16 conditions into a separate gate (ADR 0027). The raw
  `Trigger` stays capped at 16 conditions and still reports this code.
- A DAT index outside `0..count-1`, or an unknown DAT field.
- A static string write longer than the `FixedString(size)` capacity.

**Before**

```python
arr = FixedArray("dword", 0)          # capacity must be positive
s = FixedString(3)
s.write("abcd")                        # 4 bytes + NUL do not fit
t = Trigger(conds=[Always()] * 17)     # maximum is 16
```

**After**

```python
arr = FixedArray("dword", 8)
s = FixedString(4)
s.write("abc")
t = Trigger(conds=[Always()])          # split the rest into another Trigger
```

If the conditions describe one logical gate, prefer a structured form
(`when`, `once`, `while_loop`/`whileLoop`): lowering splits it above 16
conditions, so the author never needs to hand-split a `Trigger`.

**ADR.** [ADR 0003](./adr/0003-capacity-aware-aligned-string-writes.md) (string
capacity), [ADR 0009](./adr/0009-typed-dat-accessors.md) (DAT entries),
[ADR 0019](./adr/0019-operand-domain-validation.md) (eager validation),
[ADR 0027](./adr/0027-condition-chunking.md) (structured-form condition
chunking), and ADR 0001 (16 conditions / 64 actions per trigger).

## `EUDSL-CMP`

**Meaning.** `MemCond` received a string comparison name it does not know.
Only `exactly`/`==`/`=`, `atleast`/`>=`, and `atmost`/`<=` are accepted;
anything else stays `EUDSL-CMP`.

**Trigger.** `MemCond(EPD(addr), ">", 1)` or `MemCond(epd, "less", 1)`.

**Before**

```python
MemCond(EPD(0x57F0F0), ">", 100)
```

**After**

```python
MemCond(EPD(0x57F0F0), ">=", 100)   # or use AtLeast directly
```

**ADR.** [ADR 0019](./adr/0019-operand-domain-validation.md) §6.

## `EUDSL-COND-TRUTHIFY`

**Meaning.** Python asked a runtime condition or value for a build-time
`bool`. Conditions describe an in-game test; truthifying one silently picks a
branch while `buildModule` runs, so the frontend raises instead.

**Trigger.** `if v.eq(1):`, `while v == 0:`, `assert cond`, `a and b`,
`a or b`, `not c`, and `x if c else y` where `c` is a `Var`, `Expr`,
`ReadonlyVar`, field read, or condition.

**Before**

```python
if minerals.eq(0):
    DisplayText("EMPTY")
else:
    DisplayText("OK")
```

**After**

```python
when([minerals.eq(0)], lambda: DisplayText("EMPTY"))
when([minerals.ne(0)], lambda: DisplayText("OK"))

# Or use the runtime else arm:
when([minerals.eq(0)], lambda: DisplayText("EMPTY"), lambda: DisplayText("OK"))
```

Use `while_loop([...], body)` for loops and `dispatch(value, cases, fallback)`
for multi-way integer choices.

**ADR.** [ADR 0020](./adr/0020-build-time-vs-runtime-conditions.md).

## `EUDSL-DIV-ZERO`

**Meaning.** A constant division or modulo with a zero divisor was folded at
build time. A zero *runtime* divisor keeps the documented `(0, a)`/no-op
behaviour; only the build-time constant is rejected.

**Trigger.** Materializing an expression with a constant zero divisor:
`v.idiv(0)`, `v.imod(0)`, `v.divmod(0)`, or `v.set(v // 0)` (a bare
`v.div(0)` is a lazy `Expr` and raises only when it is materialized).

**Before**

```python
v.idiv(0)
```

**After**

```python
q = v.div(2)
# or guard the runtime case with when()/dispatch()
```

**ADR.** None; the guard lives in `py/var.py` and `ts/var.ts`.

## `EUDSL-FORMAT`

**Meaning.** A `FixedString` format string is malformed. The parser supports
`[[fill]align]width[type]` only: `>` alignment, decimal `d`, and 8-digit hex
`x`. Left/centre alignment, missing widths, widths outside `1..10`, lone
braces, and unknown types are errors.

**Trigger.** `s.write("{:d", v)`, `s.write("{:<4d}", v)`,
`s.write("{:>d}", v)`, `s.write("{:4x}", v)`, `s.write("bad }")`.

**Before**

```python
s.write("{:d", v)
s.write("{:<4d}", v)
```

**After**

```python
s.write("{:d}", v)
s.write("{:>4d}", v)
s.write("{:\x1a>4d}", v)   # fixed-width decimal, preferred for HUDs
s.write("{{literal braces}}")
```

**ADR.** [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md).

## `EUDSL-PLAN`

**Meaning.** The CLI's frozen-plan loader found a plan JSON whose `version`
does not match `PLAN_VERSION` (`1.0` in `crates/eudkit-cli/src/plan.rs`).

**Trigger.** `eudkit compile --plan old-plan.json` where the plan was written
by an incompatible CLI version.

**Before**

```json
{ "version": "0.9", "...": "..." }
```

**After**

Regenerate the plan with the current CLI (`--plan-out`) or update the plan's
`version` and schema together. Do not hand-edit only the version.

**ADR.** [ADR 0013](./adr/0013-optional-tick-gate-for-single-chain-maps.md)
describes frozen-plan builds; the version contract is in
`crates/eudkit-cli/src/plan.rs`.

## `EUDSL-RANGE`

**Meaning.** A numeric value is the right type but outside its documented
range. Unlike `EUDSL-TYPE`, the operand is an integer; the bound is the
problem.

**Trigger.**

- `every(0, body)` / `after(-1, body)` (count must be positive).
- `soundlooper` bar/goto/count outside `0..999`.
- `memsetEPD` constant count above `0xFFFFFFFF`.
- `dataDumper` with an empty payload.
- `isqrt` result overflow (`ceil(value_bits / 2) + fixed_bits > 32`).
- Camera-follow target/location/switch/owner/inertia bounds.

**Before**

```python
every(0, body)
```

**After**

```python
every(60, body)   # every 60 trigger loops
```

**ADR.** [ADR 0019](./adr/0019-operand-domain-validation.md) for the
validate-don't-coerce rule; concrete bounds live in each surface
(`py/temporal.py`, `py/soundlooper.py`, `py/blockio.py`, `py/mathf.py`,
`py/camera.py`, and their TS mirrors).

## `EUDSL-READONLY`

**Meaning.** Python code tried to mutate a `ReadonlyVar`, the owner-file view
exported by `asReadonly`/`as_readonly`. TypeScript removes mutators at the type
level; Python enforces at runtime.

**Trigger.** `view.set(1)`, `view.iadd(1)`, or any exposed in-place mutator
on a `ReadonlyVar`. (`copy()` and the raw field handles are omitted entirely,
so they raise `AttributeError` instead.)

**Before**

```python
wave.set(wave.plus(1))   # wave is a ReadonlyVar
```

**After**

```python
# In the owning module, keep the mutable Var private and export a mutator:
def advance_wave() -> None:
    _wave.iadd(1)
```

Consumers call `advance_wave()` instead of mutating the view.

**ADR.** [ADR 0005](./adr/0005-lazy-global-declarations.md).

## `EUDSL-SCOPE`

**Meaning.** A declaration or DSL operation is not in a valid builder scope.
Eager declarations (`Var`, `FixedString`, `FixedArray`, struct instances,
temporal blocks) must be created inside `build_module`; a module has exactly
one root builder; a lazy handle must resolve inside one builder.

**Trigger.**

- `Var(0)` at module scope without `lazyVar`.
- A nested `build_module` call.
- Using a `lazy*` handle that was never resolved, or one resolved under
  multiple builders after the build.
- `soundlooper.player()` emitted twice, or `FixedString` constructed outside
  a builder.

**Before**

```python
v = Var(0)   # module scope: no active builder


def prog() -> None:
    when([v.eq(0)], lambda: DisplayText("READY"))


module = build_module(prog)
```

**After**

```python
def prog() -> None:
    v = Var(0)   # eager local inside the builder
    when([v.eq(0)], lambda: DisplayText("READY"))


module = build_module(prog)
```

For cross-file values, declare `lazyVar(0)` at module scope and use it inside
exactly one builder (the owner-file convention).

**ADR.** [ADR 0005](./adr/0005-lazy-global-declarations.md),
[ADR 0012](./adr/0012-explicit-entry-module-export.md),
[ADR 0021](./adr/0021-mpq-embedded-file-surface.md).

## `EUDSL-TARGET`

**Meaning.** A target that must be known at build time was dynamic. DAT table
entries require a compile-time constant integer index; a `MemView`/
`MemFieldHandle` field on a dynamic `Var` EPD requires an explicit
`withCP`/`with_cp` scope.

**Trigger.** `unitsDat[v]` (a `Var` index), or a `MemFieldHandle`
read/write whose `base_epd` is a runtime value rather than a constant int or
a `CPCursor`, outside a `with_cp` scope.

**Before**

```python
entry = unitsDat[unit_index]   # unit_index is a Var
```

**After**

```python
entry = unitsDat[UnitType.Marine]   # constant index
```

```python
with_cp(epd, lambda cp: cp.view(CUnit).hp.read())
```

**ADR.** [ADR 0009](./adr/0009-typed-dat-accessors.md) (constant DAT indices),
[ADR 0004](./adr/0004-triggerpage-transfer-parking-and-curpl-reset.md)
(CurrentPlayer scoping).

## `EUDSL-TS-TRUTHIFY`

**Meaning.** The opt-in TypeScript AST check found a runtime condition/value
used as a JavaScript boolean, or a field compared against `.initialValue`.
`tsc` cannot see these shapes, so `npm run typecheck` runs
`scripts/check-truthiness.ts` as an extra pass. This code never comes from
`eudkit synth`; it is an editor/CI guard.

**Trigger.** `if (v.eq(1))`, `while (u.hp)`,
`if (u.hp == u.hp.initialValue)`.

**Before**

```ts
if (minerals.eq(0)) {
  DisplayText("EMPTY");
}
```

**After**

```ts
when([minerals.eq(0)], () => DisplayText("EMPTY"));
whileLoop([minerals.eq(0)], () => DisplayText("EMPTY"));
```

**ADR.** [ADR 0020](./adr/0020-build-time-vs-runtime-conditions.md) (the
TypeScript mirror of `EUDSL-COND-TRUTHIFY`).

## `EUDSL-TYPE`

**Meaning.** An operand is outside the domain the surface accepts. The
frontends validate eagerly instead of coercing: `bool`, non-integral
`float`/`number`, `string`, `None`/`null`/`undefined`, and integer-convertible
*handles* are rejected. Struct element writes must also match the element's
shape by position and arity.

**Trigger.**

- `u.hp.set("100")`, `v.set(True)`, `v.eq(1.5)`.
- `memcpyEPD(dst, readonly_var, 1)` (the block primitives reject a
  `ReadonlyVar`).
- `Queue(Point, 4).push(1)` where `Point` has two fields.

**Before**

```python
u.hp.set("100")
v.eq(1.5)
```

**After**

```python
u.hp.set(100)
v.eq(2)
```

**ADR.** [ADR 0019](./adr/0019-operand-domain-validation.md).

## `EUDSL-UNKNOWN-FIELD`

**Meaning.** A `lazyStruct` init dict, or (in TypeScript) a read/write of a
field the `TriggerStruct` schema does not define. Python struct instances raise
a plain `AttributeError` for unknown field access; the canonical code is used
for `lazyStruct` in both frontends and for the TypeScript struct proxy. DAT
field names use `EUDSL-BOUNDS` instead.

**Trigger.** `lazyStruct(Point, {"z": 1})` for a schema with fields `x` and
`y`; in TypeScript, `p.z` or `p.z = 1` on `Point`.

**Before**

```python
p = lazyStruct(Point, {"z": 1})
```

**After**

```python
p = lazyStruct(Point, {"x": 1})
```

**ADR.** None; the struct schema is the source of truth
(`py/struct.py`, `ts/struct.ts`). DAT field-name validation is covered by
[ADR 0009](./adr/0009-typed-dat-accessors.md).

## `EUDSL-WARN-PERF`

**Meaning.** A format string interpolates two or more bare decimal `{}`
placeholders. Bare decimal is variable-width and costs roughly 688T static per
variable; a fixed-width decimal spec reduces that to roughly 51T static per
variable.

**Trigger.** `s.write("wave={} hp={}", wave, hp)`.

**Before**

```python
s.write("wave={} hp={}", wave, hp)
```

**After**

```python
s.write("wave={:\x1a>4d} hp={:\x1a>4d}", wave, hp)
```

**ADR.** [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md).

## `EUDSL-WARN-SCOPE`

**Meaning.** A `lazy*` factory was called inside `buildModule`. Lazy
declarations are meant to live at module scope; an in-builder value should be
eager instead. Both frontends emit the same message:

`EUDSL-WARN-SCOPE: lazy* declared inside buildModule; use eager Var for in-module declarations`

**Trigger.** `lazyVar(0)` or `lazyFixedArray("dword", 8)` inside the
`buildModule` body.

**Before**

```python
def prog() -> None:
    v = lazyVar(0)   # warning: lazy declaration inside the builder
```

**After**

```python
def prog() -> None:
    v = Var(0)       # eager local
```

**ADR.** [ADR 0005](./adr/0005-lazy-global-declarations.md).

## Condition combinators and diagnostic codes

`condAll`/`cond_all`, `condAny`/`cond_any`, and `condNot`/`cond_not`
([ADR 0028](./adr/0028-logical-condition-combinators.md)) are ordinary
conditions after build-time normalization, so they reuse the existing codes
rather than adding one:

| Shape | Code |
|---|---|
| `condNot()` or `condNot(a, b)` (wrong arity) | `EUDSL-TYPE` |
| `condAll(true)` or any non-condition operand | `EUDSL-TYPE` |
| `if condAny(...):` / `if condNot(...):` in Python | `EUDSL-COND-TRUTHIFY` |
| `if (condAny(...))` under the opt-in TypeScript lint | `EUDSL-TS-TRUTHIFY` |
| `Trigger(conds=[condAny(...)])` outside `build_module` | `EUDSL-SCOPE` |
| `Trigger(conds=[condAll(...)])` expanding past 16 entries | `EUDSL-BOUNDS` |

`condAll` splices its entries, so the 16-condition cap applies to the expanded
list; a raw `Trigger` above it still reports `EUDSL-BOUNDS`. The raw-trigger
path counts planned slots before emitting any prelude, so the bounds failure
leaves no flag, reset trigger, or set-when behind. `condAny` and `condNot` with
an opaque leaf need a prelude trigger (the shared flag gate), so they must run
inside a builder; the prelude cannot be hoisted out of a detached `Trigger`.
Structured forms remain exempt from the cap (ADR 0027), and the LSP's lexical
capacity check counts a combinator call as one entry, so an over-16 `condAll`
splice is reported by the build, not by the editor.

The opt-in TypeScript lint is syntactic: it recognizes a direct
`condAny(...)`/`cond_all(...)`/`condNot(...)`/`cond_not(...)` call in boolean
position, but it does not resolve aliases, namespace imports, or
`Boolean(...)` wrappers. Python's `EUDSL-COND-TRUTHIFY` is a runtime check on
the marker object and does catch those indirect forms.
