# Lazy global declarations (handle + per-builder binding)

Status: accepted (2026-11-04)

## Context

Value and container declarations (`Var`, `FixedArray`, `FixedString`,
`Queue`, `Deque`, `Stack`, `UnitGroup`, `TriggerStruct` instances) could only
be created inside `buildModule`, because the eager constructors allocate ids
from the active builder's id space, and outside a builder there is no id
space: `allocVarSlot` threw `EUDSL-SCOPE`. Cramming every declaration inside
`buildModule` is inconvenient, and cross-file sharing required threading
values through parameters instead of importing a module-scope declaration.

The fixed points:

- Scope is all value/container types. Statements and control flow (`when` /
  `once` / `until` / `edge` / `every` / `after` / `whileLoop`, `Trigger`,
  `TriggerPage`) stay inside `buildModule`: declarations global, statements
  inside.
- The entry point is always `buildModule(fn)`. Cross-file import is in scope
  (a sub module's global used as `sub.globalVar` inside the main
  `buildModule`).
- At the SC:R engine level there is no declaration, only addresses — once
  reserved memory is global within that game. `buildModule` scoping is
  compiler bookkeeping, not a runtime boundary. The unit of "global" is the
  game/map (one compilation), not the process.
- Lazy first use beats adoption at `buildModule` start because adoption has a
  pending-list leak hazard: a test that declares without building leaks its
  globals into the next test's module.
- Per-module allocation is required: tests build many modules per process, so
  a global used under a second builder must resolve separately per builder.
- Existing constructors stay eager. Eager outside-builder allocation stays
  impossible; lazy APIs are a separate constructor path, not a weakening of
  the var-scope fix.
- Py `Var` has `__iadd__`, so `sub.globalVar += 1` works in Py, but TS cannot
  overload `+=`, so TS needs `sub.globalVar.iadd(1)`.
- Constraints carried forward: byte-identity for existing inside-`buildModule`
  programs; Layer 2 pinned-rank equality; StoragePlan only (ids are
  identities, not ranks); path-complete restore untouched; `control.rs`
  untouched unless a need is proven; LIR unchanged; English only.

Making `Var()` itself lazy by default was tested and rejected. Allocation
order is part of language semantics:

1. **Expressiveness**: eager `Var(...)` is required for non-constant
   initialization (`Var(x + 1)`, `Var(other_var)`, `v.copy()`) and
   unreferenced slot reservations. `lazy*` cannot safely support non-constant
   initialization without conditional scope-shift hazards.
2. **Allocation fragility**: first-use order is fragile — adding or reordering
   an early condition check silently reshuffles slot allocations across the
   whole map and can push structs across `TriggerPage` boundaries. Declaration
   order inside `buildModule` is stable and author-controlled.
3. **Byte-identity as proof**: output differences were the diagnostic proof
   that first-use order silently perturbs compiled layouts.

Consequently, `Var()` stays eager and lazy ships as an explicit opt-in
surface.

## Decision

1. **Handle + per-builder binding.** A global declaration is a handle: an
   object constructible anywhere (no builder needed) that validates only
   build-time arguments (bounds, types) and stores pure metadata. On first
   resolving use under a builder, the handle creates its binding — a fully
   eager instance created under that builder — and delegates to it. Bindings
   live and die with their builder; handles live forever (module scope) and
   hold one binding per builder, cached handle-side and weak-keyed by builder
   object (TS `WeakMap`, Py `WeakKeyDictionary`), with a single-record
   post-build fallback: a handle resolved under exactly one builder returns
   its cached record after `buildModule` returns; resolved under several
   builders it throws as ambiguous; never resolved it throws `EUDSL-SCOPE`.
   Pure reads (`init`, `bitmask`, length, capacity, size, schema data) never
   resolve.
2. **`lazy*` naming, not `global*`.** Every var is already engine-global, so
   `global*` misleads and overpromises cross-map sharing. Py mirrors use the
   same names plus the existing snake_case alias convention (`lazy_var`
   aliases `lazyVar`).
3. **First-use order is the allocation order.** Within one builder, bindings
   are created in first-use order. Allocation ids, page ranks, string-table
   indices, and flusher registration order all follow first-use order.
   Declaration order is documentation only: users needing a specific rank
   order touch globals in order at the top of the `buildModule` body. A
   container's binding is created atomically, with no other allocation
   interleaving inside it. If first use sits inside a conditional scope, the
   binding's allocations still happen at compile time, but any statements the
   binding's constructor emits land inside that scope.
4. **Const-only init.** Lazy vars and lazy struct fields accept const init
   only. Non-const init (another `Var`, an `Expr`) throws `EUDSL-TYPE`
   eagerly at the declaration call, not at first use: a deferred `.set()`
   whose first use is inside a `when` would land inside that scope, a silent
   runtime semantic change.
5. **Never-used globals emit nothing.** A lazy handle that is never used
   consumes no id, emits no decl, no string-table entry, and no statement.
   Programs or tests depending on unused-var decls must not convert those vars
   to lazy handles.
6. **Per-builder separate bindings, not fail-loud.** This is per-map
   semantics: the same handle in two `buildModule` calls compiles in both,
   with different ids, and neither module references the other's ids.
7. **Implementation shape.** The deferred mode sits directly on the eager
   types: an internal deferred flag on the `VarImpl`/`Var` constructors plus
   resolving getters for `id`/`pageIndex`/`slotIndex`/`mask`/`dest`/`value`/
   `modifier`, with the flag unset the path is byte-identical to eager
   behavior. `lazyVar` is a thin factory over that base (plus the `lazy_var`
   alias in Py). There is no `LazyVarImpl` subclass; docs describe the factory
   shape. `FixedArray`, `Queue`, `Deque`, `Stack`, and `UnitGroup` follow the
   same deferred-mode-plus-thin-factory shape, with mutators resolving before
   any eager work. `FixedString` uses a wrapper with explicit method
   delegation: the factory validates `size` with the eager `EUDSL-BOUNDS`
   messages and allocates nothing; `size` is a pure read, while
   `messageIndex`/`message_index` and every method resolve, and binding
   creation is one full eager construction so the string-table index and
   flusher registration happen in binding order.
8. **Single entry point.** `buildModule` is a single entry point, one per map,
   like Rust `main`. A nested `buildModule` call throws; sequential
   `buildModule` calls (tests building many modules) are separate
   compilations, each with its own builder. This supersedes the earlier
   innermost-builder clause.
9. **Rollout.** Each of Var, FixedArray, struct instances,
   Queue/Deque/Stack/UnitGroup, and FixedString ships with docs and
   verification before the next starts. Py ships the snake_case aliases
   (`lazy_fixed_array`, `lazy_queue`, `lazy_deque`, `lazy_stack`,
   `lazy_unit_group`, `lazy_fixed_string`).

## Consequences

- Scope symmetry:
  - **Module scope (outside `buildModule`)**: eager `Var()` is illegal and
    fails fast with fatal `EUDSL-SCOPE` (`"Var() must be created inside
    buildModule()"`). Lazy handles (`lazyVar`, `lazyFixedString`, ...) are the
    sole mechanism.
  - **In-module scope (inside `buildModule`)**: eager `Var()` is standard.
    Declaring `lazy*` inside `buildModule` emits a compile-time warning
    `EUDSL-WARN-SCOPE` (`"lazy* declared inside buildModule; use eager Var for
    in-module declarations"`). Every `lazy*` factory emits this one exact
    message through the shared `warnLazyInBuilder()`/`warn_lazy_in_builder()`
    helper; factories do not carry their own text.
  - **Rust analogy**: resembles `static X: LazyLock<...> =
    LazyLock::new(...)`, with two differences: eudkit lazy handles resolve
    per-builder at build time on first use (not a process-wide runtime
    singleton across all compilations), and they accept constant initializers
    only (not arbitrary runtime computation).
  - **Warning-listener ownership**: the warning machinery (`EudslWarning`,
    `onWarning`/`on_warning`, `emitWarning`/`emit_warning`, and the canonical
    `warnLazyInBuilder()`/`warn_lazy_in_builder()` helper) lives in `view`
    (`ts/view.ts`, `py/view.py`). When `EUDSL-WARN-PERF` shipped, the listener
    registry lived in `string`, but importing `string` from
    `var`/`array`/`struct`/`queue` to emit `EUDSL-WARN-SCOPE` created an
    import cycle. `view` already owns `EudslWarning` and is imported by every
    declaration module, so it is the lowest-level home for one process-wide
    listener registry. `string` keeps its warning re-exports only for
    backwards compatibility.
  - **Owner-file convention**: each lazy global lives in the file that owns
    its domain concept. The owner keeps the mutable handle private, exports a
    `ReadonlyVar` view (`asReadonly()`/`as_readonly()`), and exports mutator
    functions for every legal state change. Consumers import only the view and
    the mutators: they read, compare, format, and pass the view as a
    position/line argument, but have no mutation path.

    ```python
    # waves.py (owner)
    _wave = lazyVar(0)          # mutable handle stays private
    wave = as_readonly(_wave)   # exported read-only view

    def advance_wave():
        _wave.iadd(1)           # the only mutation path
    ```

    ```python
    # hud.py (consumer)
    from waves import advance_wave, wave

    hud.write("wave={:d}", wave)
    with when(wave.atLeast(10)):
        ...
    advance_wave()
    ```

    The view deliberately exposes no `value`/`dest`/`mask`/`modifier` field
    refs — those carry `set`/`iadd`/`isub`/`act_*` in both frontends — and no
    `copy()` that would return the mutable handle. It also has no `.read()`:
    the handle itself is the read value in conditions, expressions, and
    format/position arguments. Python expressions built from the view keep the
    view as their operand, so `Expr.lhs` cannot leak the owner's mutable
    `Var`.

    Enforcement differs honestly by frontend. TypeScript's `ReadonlyVar` is a
    compile-time interface: `asReadonly()` is a cast, the runtime object is
    still the owner's `VarImpl`, and untyped JavaScript can mutate it.
    Python's `as_readonly()` is a runtime wrapper: every direct mutator and
    in-place operator raises `EUDSL-READONLY` with a message pointing at the
    owner-exported mutator. Neither is a security boundary; `_var` and owner
    handles are private by convention. Single-file usage must use eager locals
    instead of lazy globals.
- The first-use ordering rules above are normative and user-facing.
- Migration note: decl-count / `total_vars`-dependent programs must not
  convert counted vars to lazy handles.
- Handles are safe to carry across builders; binding-derived objects (an
  element var, a struct field, an out-box) are builder-scoped and must not
  cross builders. This is a documented rule with no runtime enforcement (the
  same hazard class as eager vars); a debug-mode assertion may be added later
  only if cross-builder carrying bugs are observed.
- Per-map address note: the same handle resolves to different addresses in
  different maps. That is expected, not a bug.
- Flusher registration: exactly once per binding, flush order = binding order.
- Verification bar per phase: full expanded lock unedited; byte-identity
  corpus (lazy program == same eager program at same first-use order, full
  `dump-ir` output compared, not just totals); differential probes
  (out-of-order both directions, conditional first use with const init,
  non-const init eager throw, never-used emits nothing, two-builder different
  ids, cross-file use, nested-`buildModule` throw); no golden change.
- No `control.rs`, opt, lower, map, CLI, or LSP changes. No public `resolve()`
  / `isResolved` APIs. No LIR/schema change.

## Rejected alternatives

- **Adoption (collect globals at `buildModule` start, then allocate in
  declaration order).** Rejected: the pending-list leak hazard — a
  declaration without a following build leaks into the next module — plus
  waste (never-used globals would allocate) and process-global registry state.
  Revive only if first-use order proves unusable in practice and a
  leak-proof, per-module-scoped adoption design exists. Both conditions
  required.
- **`global*` naming.** Rejected: implies eager vars are not global (they are,
  within a game) and overpromises cross-map sharing. Revive only if the
  vocabulary match is preferred over mechanism honesty; then `lazy*` becomes
  the alias, never both as primaries.
- **Deferred `.set()` for non-const init.** Rejected: the conditional
  first-use scope shift is a silent semantic change. Revive only with a real
  use case plus a root-hoisting hook designed as its own change with seam
  tests.
- **Bind-once + fail-loud (one binding, second builder throws).** Rejected:
  breaks multi-module tests. Revive only as an opt-in strict mode if
  cross-builder aliasing bugs appear in practice.
- **Builder-side registry method (builder owns the handle cache).** Rejected:
  a seam change; the cache lives in handles (weak-keyed), not in the builder.
- **FixedString subclassing (promote `private` state to internal).**
  Rejected: the shipped wrapper + method delegation needs no state promotion.
  Revive for FixedString only if delegation boilerplate proves
  unmaintainable, and only after an eager byte-identity proof.
- **All-at-once rollout.** Rejected: blast radius (5 types × 2 mirrors); a
  FixedString problem would stall Var value.
