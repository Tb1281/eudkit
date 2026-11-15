# Lazy global declarations (handle + per-builder binding)

Status: accepted (2026-11-04)

## Context

Value and container declarations (`Var`, `FixedArray`, `FixedString`,
`Queue`, `Deque`, `Stack`, `UnitGroup`, `TriggerStruct` instances)
could only be created inside `buildModule`, because the eager
constructors allocate ids from the active builder's id space, and
outside a builder there is no id space: `allocVarSlot` threw
`EUDSL-SCOPE` outside `buildModule` (the var-scope fix). Cramming
every declaration inside `buildModule` is inconvenient, and cross-file
sharing required threading values through parameters instead of
importing a module-scope declaration.

The fixed points for this work:

- F1. Scope is ALL value/container types. Statements and control
  flow (`when` / `once` / `until` / `edge` / `every` / `after` /
  `whileLoop`, `Trigger`, `TriggerPage`) stay INSIDE `buildModule`.
  Rule: "declarations global, statements inside".
- F2. The entry point is ALWAYS `buildModule(fn)`. Cross-file import
  is in scope (a sub module's global used as `sub.globalVar` inside
  the main `buildModule`).
- F3. Engine position: at the SC:R engine level there is no
  declaration, only addresses — once-reserved memory IS global within
  that game. `buildModule` scoping is compiler bookkeeping, not a
  runtime boundary. The unit of "global" is the GAME/MAP (one
  compilation), not the process. Docs state "declaration global,
  allocation per-map".
- F4. Mechanism preference: LAZY on first use over
  adoption-at-`buildModule`-start, because adoption has a pending-list
  LEAK hazard (a test that declares without building would leak its
  globals into the next test's module).
- F5. Per-module allocation is REQUIRED: tests build many modules per
  process, so a global used under a second builder must resolve
  separately per builder.
- F6. Existing constructors stay EAGER (new API only). Eager
  outside-builder allocation stays impossible; lazy APIs are a
  separate constructor path, not a weakening of the var-scope fix.
- F7. Mirror asymmetry: Py `Var` HAS `__iadd__`, so
  `sub.globalVar += 1` works in Py, but TS CANNOT overload `+=`, so
  TS needs `sub.globalVar.iadd(1)`.
- F8. Constraints carried forward: byte-identity for existing
  inside-`buildModule` programs; Layer 2 pinned-rank equality;
  StoragePlan only (ids are identities, not ranks); path-complete
  restore untouched; `control.rs` untouched unless a need is proven;
  LIR unchanged; English only.

A parallel experiment (the `lazy-test` branch) tested making `Var()`
itself lazy by default. The experiment proved that allocation order is
fundamentally part of language semantics:
1. **Expressiveness**: Eager `Var(...)` is required to express
   non-constant initialization (`Var(x + 1)`, `Var(other_var)`,
   `v.copy()`) and unreferenced slot reservations. `lazy*` cannot safely
   support non-constant initialization without conditional scope-shift
   hazards (D4).
2. **Allocation fragility**: First-use order is fragile — adding or
   reordering an early condition check silently reshuffles slot
   allocations across the entire map and can push structs across
   `TriggerPage` boundaries. In contrast, declaration order inside
   `buildModule` is stable and author-controlled.
3. **Byte-identity as verification proof**: Output differences on the
   `lazy-test` branch were the diagnostic proof that first-use order
   silently perturbs compiled layouts, rather than the primary
   motivation.

Consequently, `Var()` stays eager and lazy ships as an explicit opt-in
surface (outcome (a) of the design's §9). The experiment's branch is
reference only and never merges.

## Decision

1. **Handle + per-builder binding.** A global declaration is a HANDLE:
   an object constructible anywhere (no builder needed) that
   validates only build-time arguments (bounds, types) and stores
   pure metadata. On first resolving use under a builder, the handle
   creates its BINDING — a fully eager instance created under that
   builder — and delegates to it. Bindings live and die with their
   builder; handles live forever (module scope) and hold one binding
   per builder, cached handle-side weak-keyed by builder object (TS
   `WeakMap`, Py `WeakKeyDictionary`, with a single-record
   post-build fallback: a handle resolved under exactly one builder
   returns its cached record after `buildModule` returns; resolved
   under several builders it throws as ambiguous; never resolved it
   throws `EUDSL-SCOPE`). Pure reads (`init`, `bitmask`, length,
   capacity, size, schema data) never resolve.
2. **`lazy*` naming, not `global*`.** Every var is already
   engine-global (F3), so `global*` misleads and overpromises
   cross-map sharing. Py mirrors use the same names plus the
   existing snake_case alias convention (`lazy_var` aliases
   `lazyVar`).
3. **First-use order is THE allocation order (R1).** Within one
   builder, bindings are created in first-use order. Allocation ids,
   page ranks, string-table indices, and flusher registration order
   all follow first-use order (R2: declaration order is
   documentation only — users needing a specific rank order touch
   globals in order at the top of the `buildModule` body; R3: a
   container's binding is created atomically, no other allocation
   interleaves inside it; R4: if first use sits inside a conditional
   scope, the binding's allocations still happen at compile time,
   but any STATEMENTS the binding's constructor emits land inside
   that scope).
4. **Const-only init (D4).** Lazy vars and lazy struct fields accept
   const init only. Non-const init (another `Var`, an `Expr`) throws
   `EUDSL-TYPE` EAGERLY at the declaration call, not at first use:
   a deferred `.set()` whose first use is inside a `when` would land
   INSIDE that scope, a silent runtime semantic change.
5. **Never-used globals emit nothing (D5).** A lazy handle that is
   never used consumes no id, emits no decl, no string-table entry,
   and no statement. Programs or tests depending on unused-var decls
   must not convert those vars to lazy handles.
6. **Per-builder separate bindings, not fail-loud (D3).** This IS
   per-map semantics (F3): the same handle in two `buildModule`
   calls compiles in both, with different ids, and neither module
   references the other's ids.
7. **Implementation shape (shipped).** P1-01 put the deferred mode
   directly on the eager types: an INTERNAL deferred flag on the
   `VarImpl`/`Var` constructors plus resolving getters for
   `id`/`pageIndex`/`slotIndex`/`mask`/`dest`/`value`/`modifier`,
   with the flag unset the path is byte-identical to current eager
   behavior. P1-02 ships `lazyVar` as a THIN FACTORY over that base
   (plus the `lazy_var` alias in Py). There is no `LazyVarImpl`
   subclass; docs describe the factory shape.
8. **Single entry point.** `buildModule` is a single entry point,
   one per map, like Rust `main`. A nested `buildModule` call
   throws; sequential `buildModule` calls (tests building many
   modules) are separate compilations, each with its own builder.
   This supersedes the design draft's innermost-builder clause.
9. **Rollout (D11).** P1 Var (shipped here); P2 FixedArray + Struct
   instances (Struct shipped in P2-02; FixedArray in P2-01); P3
   Queue/Deque/Stack/UnitGroup (shipped in P3-01); P4 FixedString
   (shipped in P4-01). Each phase ships with docs + verification (§8 of
   the design) before the next starts. P2+P3 may merge only if P2 lands
   with zero findings (a P2-evaluation decision, not this ADR). The
   `D*`/`R*` labels cited below are the design draft's decision and rule
   labels, distinct from this ADR's enumerated decisions.
   - P2-01 `lazyFixedArray` (shipped): deferred-mode `FixedArrayImpl`
     base + thin factory (P1-01 pattern, no subclass, no wrapper);
     Py `set_all` gains `__len__` on `FixedArrayImpl` so an array
     source works (eager `set_all` with an array source previously
     raised `TypeError`, untested).
   - P3-01 `lazyQueue`/`lazyDeque`/`lazyStack`/`lazyUnitGroup`
     (shipped): deferred mode directly on the eager `Queue`/`Deque`/
     `Stack`/`UnitGroup` classes + thin factories
     (P1-01 pattern, no subclass, no wrapper). Factories validate
     capacity/element type only and allocate nothing; eager binding
     creation stays atomic (R3), with UnitGroup mutators resolving
     before any eager work. Pure reads (`capacity`; UnitGroup
     `autoPrune`/`unlimiter`) never resolve; every other read resolves
     and throws `EUDSL-SCOPE` outside a builder. Py ships
     `lazy_queue`/`lazy_deque`/`lazy_stack`/`lazy_unit_group` aliases.
   - P4-01 `lazyFixedString` (shipped): wrapper + explicit method
     delegation (D8), not a subclass and not a deferred flag on the
     eager `FixedString`. The factory validates `size` with the eager
     `EUDSL-BOUNDS` messages and allocates nothing; `size` is a pure
     read, while `messageIndex`/`message_index` and every method
     resolve. Binding creation is one full eager `FixedString`
     construction, so the string-table index and flusher registration
     happen in binding order (D9). Py ships the `lazy_fixed_string`
     alias.

## Consequences

- Scope symmetry:
  - **Module scope (outside `buildModule`)**: Eager `Var()` is illegal
    and fails fast with fatal `EUDSL-SCOPE` (`"Var() must be created inside buildModule()"`).
    Lazy handles (`lazyVar`, `lazyFixedString`, ...) are the sole mechanism.
  - **In-module scope (inside `buildModule`)**: Eager `Var()` is standard.
    Declaring `lazy*` inside `buildModule` emits a compile-time warning
    `EUDSL-WARN-SCOPE` (`"lazy* declared inside buildModule; use eager Var for in-module declarations"`).
    Every `lazy*` factory emits this one exact message through the shared
    `warnLazyInBuilder()`/`warn_lazy_in_builder()` helper; factories do not
    carry their own text.
  - **Rust analogy**: Resembles `static X: LazyLock<...> = LazyLock::new(...)`,
    with two documented differences:
    1. eudkit lazy handles resolve per-builder at build time on first use
       (not a process-wide runtime singleton across all compilations).
    2. eudkit lazy handles accept constant initializers only (not arbitrary
       runtime computation).
  - **Warning-listener ownership**: The warning machinery
    (`EudslWarning`, `onWarning`/`on_warning`, `emitWarning`/
    `emit_warning`, and the canonical `warnLazyInBuilder()`/
    `warn_lazy_in_builder()` helper) lives in `view` (`ts/view.ts`,
    `py/view.py`). This is intentional. When `EUDSL-WARN-PERF` shipped,
    the listener registry lived in `string`, but `string` is itself a
    lazy-factory module: importing it from `var`/`array`/`struct`/`queue`
    to emit `EUDSL-WARN-SCOPE` created an import cycle. `view` already
    owns `EudslWarning` and is imported by every declaration module, so
    it is the lowest-level home for one process-wide listener registry.
    `string` keeps its warning re-exports only for backwards
    compatibility.
  - **Owner-file convention**: Each lazy global lives in the file that
    owns its domain concept. The owner keeps the mutable handle private,
    exports a `ReadonlyVar` view (`asReadonly()`/`as_readonly()`), and
    exports mutator functions for every legal state change. Consumers
    import only the view and the mutators: they read, compare, format,
    and pass the view as a position/line argument, but have no mutation
    path.

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

    The view deliberately exposes no `value`/`dest`/`mask`/`modifier`
    field refs — those carry `set`/`iadd`/`isub`/`act_*` in both
    frontends — and no `copy()` that would return the mutable handle.
    It also has no `.read()`: the handle itself is the read value in
    conditions, expressions, and format/position arguments. Python
    expressions built from the view keep the view as their operand, so
    `Expr.lhs` cannot leak the owner's mutable `Var`.

    Enforcement differs honestly by frontend. TypeScript's `ReadonlyVar`
    is a compile-time interface: `asReadonly()` is a cast, the runtime
    object is still the owner's `VarImpl`, and untyped JavaScript can
    mutate it. Python's `as_readonly()` is a runtime wrapper: every
    direct mutator and in-place operator raises `EUDSL-READONLY` with a
    message pointing at the owner-exported mutator. Neither is a
    security boundary; `_var` and owner handles are private by
    convention.
    Single-file usage must use eager locals instead of lazy globals.
- R1–R4 ordering rules (above) are normative and user-facing.
- D5 migration note: decl-count / `total_vars`-dependent programs
  must not convert counted vars to lazy handles.
- D10 rule: handles are safe to carry across builders;
  binding-derived objects (an element var, a struct field, an
  out-box) are builder-scoped and must not cross builders. This is
  a DOCUMENTED rule with NO runtime enforcement (same hazard class
  as eager vars today); a debug-mode assertion may be added later
  only if cross-builder carrying bugs are observed.
- Per-map address note: the same handle resolves to different
  addresses in different maps. That is expected, not a bug.
- Flusher registration (P4): exactly once per binding, flush order
  = binding order (D9).
- Verification bar per phase (design §8): full expanded lock
  UNEDITED; byte-identity corpus (lazy program == same eager program
  at same first-use order, full `dump-ir` output compared, not just
  totals); differential probes (out-of-order both directions,
  conditional first use with const init, non-const init eager throw,
  never-used emits nothing, two-builder different ids, cross-file
  use, nested-`buildModule` throw); no golden change in any phase.
- No `control.rs`, opt, lower, map, CLI, or LSP changes in P1. No
  public `resolve()` / `isResolved` APIs. No LIR/schema change.

## Rejected alternatives

- **Adoption (collect globals at `buildModule` start, then allocate
  in declaration order).** Rejected: the pending-list LEAK hazard
  (F4) — a declaration without a following build leaks into the next
  module — plus waste (never-used globals would allocate) and
  process-global registry state. Revive ONLY if first-use order
  proves unusable in practice AND a leak-proof, per-module-scoped
  adoption design exists. Both conditions required.
- **`global*` naming.** Rejected: implies eager vars are not global
  (F3 says they are, within a game) and overpromises cross-map
  sharing. Revive only if the user explicitly prefers vocabulary
  match over mechanism honesty; then `lazy*` becomes the alias, never
  both as primaries.
- **Deferred `.set()` for non-const init.** Rejected: the conditional
  first-use scope shift is a silent semantic change (D4). Revive
  ONLY with a real use case plus a root-hoisting hook designed as
  its own ticket with seam tests.
- **Bind-once + fail-loud (one binding, second builder throws).**
  Rejected: breaks multi-module tests (F5). Revive only as an opt-in
  strict mode if cross-builder aliasing bugs appear in practice.
- **Builder-side registry method (builder owns the handle cache).**
  Rejected: a seam change; the cache lives in handles (weak-keyed),
  not in the builder.
- **FixedString subclassing (promote `private` state to internal).**
  Rejected for P1–P3. P4-01 ships a wrapper + method delegation (D8).
  Revive for FixedString only if delegation boilerplate proves
  unmaintainable, and only after an eager byte-identity proof.
- **All-at-once rollout.** Rejected: blast radius (5 types × 2
  mirrors); a FixedString problem would stall Var value.

## Design deviations corrected

- The design draft's §4.3 sketch showed a `LazyVarImpl` subclass
  shape. The shipped P1-01 mapping puts the deferred mode directly
  on `VarImpl`/`Var` with resolving getters, and P1-02 is a thin
  factory; the subclass does not exist. This ADR describes the
  shipped shape.
- The design draft's innermost-builder clause (a handle used inside
  an inner `buildModule` binds to the inner builder) is superseded
  by the single-entry-point rule: nested `buildModule` throws.
