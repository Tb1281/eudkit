# Trigger (16C, 64A) Storage and Execution Architecture

Status: accepted (2026-11-04; amended 2026-11-10, 2026-11-11, 2026-11-13)

> **Amendment (2026-11-10, Cycles 20–21):** decision 5's
> transfer-coalescing bullet (§3) was extended to every statement list (#67)
> and then to adjacent sibling `when` blocks whose bodies are pure transfers
> under a dependency guard (#69).
>
> **Amendment (2026-11-11, Cycle 22):** decision 5's root transfer run is no
> longer split by root actions that cannot observe it (#60).
>
> **Amendment (2026-11-11, #79):** decision 5's sibling-`when` leaf-act merge
> now carries the same dependency guard as the transfer merge: a body act
> that writes a variable the shared gate reads keeps the siblings apart, so
> the later body never runs against a stale gate.
>
> **Amendment (2026-11-13, Cycle 28/Cycle 3):** decision 5's `Func` inlining
> rule replaces the `sites == 1` rule recorded in ADR 0011's Consequences
> (ticket #97). One site still always inlines; several sites inline only when
> duplicating the body packs into no more static triggers than sharing it
> (`INLINE_EXTRA_TRIGGERS = 0`), with a leaf-act body unpacked into bare acts so
> the scheduler re-packs the copies. This gate is deliberately static-first: a
> duplication that would add a static trigger stays shared even when it saves
> executed triggers, because the copy multiplies static size at every site.
> Executed cost is the tie-break that makes a static tie worth taking, not a
> licence to grow the static program; the rule is therefore an explicit
> exception to decision 5's executed > static > bytes priority, not an
> application of it.
>
> **Cross-reference (2026-11-14, R4):** dead-gate removal
> (`temporal::drop_dead_gates`) runs after scheduling and does not re-schedule
> the module, so root-act packing remains root-only. R4 recorded the gap in
> `crates/eudkit-opt/tests/dead_gate_measure.rs`: acts split by a removed dead
> gate stay in two scheduled root triggers, so the current shape costs 1T
> static / 2,408 B more than the hand-coalesced shape. C3 (moving root packing
> into lowering so the two shapes coalesce) remains deferred; no decision
> change.

## Context

In earlier trigger-slot designs, every mutable variable, writable array element, and struct field occupied an isolated 72-byte `vartrigger` slot (`18 EPDs`) in a `VarBuffer` or `XVarBuffer`. That layout used only 1 of a StarCraft trigger's 64 action slots, forced dynamic array and struct indexing to multiply by `18` (costing a 32T shift-add chain per access), required `2K` triggers to copy a `K`-field struct, and required `O(capacity)` destination-patch actions at every `EUDQueue`/`EUDDeque` read site. Meanwhile, earlier statement-by-statement lowering only packed adjacent statements when each lowered to a single 0-condition trigger, leaving `iadd.const` (`2T`), sequenced variable copies (`2T` each), and leaf `when` blocks (`2–4T`) unpacked.

## Decision

`eudkit` makes the full StarCraft **`Trigger` (`16C, 64A`)** (`2,408` bytes = `602 EPDs`; 16 conditions at `20B = 5 EPDs` each; 64 actions at `32B = 8 EPDs` each) both the primary execution-packing unit and the canonical mutable storage backing:

1. **Self-Targeted `64A` `TriggerPage` Backing**:
   - Mutable `TriggerStruct`s, writable `FixedArray`s, and VProc-capable scalar variables are stored in the 64 `SetDeathsX` action slots (`0..63`) of `TriggerPage`s, retiring 72-byte `VarBuffer`/`XVarBuffer` slots.
   - At rest, every action slot `k` maintains the **Self-Targeted Action Invariant**: `act[k].dest = EPD(act[k].value)` and `modifier = SetTo`. Firing a `TriggerPage` is a no-op on inactive slots, allowing a single `1T` trigger firing to read multiple variables/fields from the same page simultaneously or copy an entire `≤ 64`-field `TriggerStruct` in `1T`–`2T`.
   - Non-VProc scalars (write-only, address-only, or boolean) are automatically demoted by `eudkit-opt` to 4-byte or 1-bit `Cell`s.

2. **Carry-Free Bit-Weighted FixedArray & Page Addressing**:
   - Because a `TriggerPage` holds `64 = 2^6` action slots and each action slot is `32 bytes = 8 = 2^3` EPDs, any element index `i` decomposes without carry into `slot = i & 63` (stride `8 EPDs`) and `page = i >> 6` (stride `602 EPDs`):
     $$\text{EPD}(i) = \text{base\_epd} + \sum_{b=0}^{5} \text{bit}_b(i) \cdot (8 \ll b) + \sum_{b=6}^{K-1} \text{bit}_b(i) \cdot (602 \ll (b - 6))$$
   - Dynamic indexing costs `ceil(log2(N))` bit-test triggers instead of a 32T `* 18` multiply chain.

3. **`TriggerStruct` vs `MemView` Separation & Per-Action Hardware Masks**:
   - **`TriggerStruct`** allocates 1 action slot per field (`0..63`) inside a `TriggerPage`, using each 32-byte action struct's independent `+0` `mask` and `+30` `eudx` (`0x4353`) for `0T` `byte`/`word`/bitfield masking.
   - **`MemView`** (`CUnit`, `CSprite`, `CondView`, `ActView`) is a zero-storage offset schema over an `EPD` or `CP` cursor.
   - **`Trigger`** is exposed as a first-class domain primitive with `trig.cond(0..15)` (`CondView`), `trig.act(0..63)` (`ActView`), `.prev`, `.next`, `.flags`, and `.fire()`.

4. **Fixed-Outbox Ring Buffers (`Queue` / `Deque`)**:
   - Ring-buffer slot triggers bind `act[0..K-1].dest` statically at build time to a chain-owned `out_box` (`O(1)` read sites with 0 slot-destination patches) and support multi-field `TriggerStruct` elements across Actions `0..K-1`.

5. **4-Layer Compiler Pipeline (`eudkit-hir` → `eudkit-opt` → `eudkit-lower` → `eudkit-lir` + `eudkit-map`)**:
   - Replaces monolithic single-stage lowering with focused `eudkit-*` passes that lower in-place constant ops (`x += c`) to 1 action, coalesce adjacent variable assignments into shared `64A` `NonSeqCompute`/`VProc` batches, and emit leaf `when` blocks (`≤ 16C`, `≤ 64A`) as a single inline `Trigger (16C, 64A)` (`1T`).
   - Transfer coalescing applies to **every statement list** — the module root and every `when`/`else`/`once`/`while` body and every `until`/`edge`/`every`/`after` desugar expansion — not only the module root. Act bin-packing stays top-level (plus packed leaf `when` bodies), and a `VarStore` always ends a transfer run.
   - At the module root a transfer run stays open across root actions that cannot observe it: a bare `HirStmt::Act` or an unconditional preserved `Trigger` block (the `can_unpack_into_basic_block` shape — no conditions, no chain pointers, flags `4`) is emitted ahead of the batch, which is the grouped-source order lowering already folds into the batch's setup trigger. An action carrying any `relocs[].target_var` may read or write a paged slot, so it keeps source order and ends the run (ticket #60).
   - Adjacent sibling `when` blocks — or adjacent sibling `once` blocks — with identical condition lists merge into one conditional group, so the shared gate is evaluated once for the merged group instead of once per block. The kinds never mix (`once`+`when` stays unmerged: each `once` self-disables independently, so the group would follow the wrong cadence). Every body must be *flat* (`Act | VarTransfer | VarBatch | VarStore`; nested `when`/`once`/`While` rejects) and carry a dependency guard: no body act reloc target or transfer/batch destination may be a variable the gate conditions read (`conds[].relocs[].target_var`), so the later body always re-observes an earlier write while the merged group evaluates the gate once. Stores need no read-set admission — their destination is an absolute cell or trigger field rather than a paged variable the gate could read, and their source read keeps program order — and a merged store/transfer body is re-scheduled by the block scheduler, which splits it into as many `Trigger`s as it needs. Only a pure leaf-act pair is bounded by the `64`-action cap (an act names a variable only through a `relocs[].target_var`, so constant/absolute writes never block a paged-var gate); the pre-#98 pure-transfer shape is the store-free special case of the same rule (#91, #98). The reloc read set alone cannot prove the merge safe when the gate names storage by an absolute address: `Memory`/`Deaths` (condtype `15`, `player` = EPD) and `Switch` (condtype `11`, `restype` = switch id) carry no relocs, so an earlier act writing that cell would look harmless. Absolute (non-reloc) addresses cannot name a paged variable's storage — the assumption already load-bearing for the #60 root interleave rule and for `Var.value`/`_resolve_epd_dest` emitting a reloc placeholder instead of a numeric address — so the merge also requires either every gate cond to be keyed by a paged variable (a field-4 `player` reloc with a `target_var`, covering plain and masked reads; vacuously true for a 0-cond gate) or the earlier body to write only paged variables (#90). A body that writes only paged vars cannot change the storage an external-state gate reads; transfers, and batches' transfers, write paged storage — a batch carrying `stores` is not paged-only — while a `VarStore` does not. Phase 2 (#112) also admits a body under an external gate when every external cell it writes is provably a different dword from every cell the gate reads, keyed by a private `StorageKey` (`Var`, raw-EPD `Epd` — the same value the lowerer uses — and `Switch`). Unknown writes (dynamic `CurrentPlayer`, string/blob/trigger relocs, unaligned `dst_addr`) fall back to the pre-#112 answer, so the clause only ever admits a merge; equal keys stay split, `Epd`-vs-`Switch` is never disjoint because switch state is not modelled as an address, and `Var` is disjoint from any external key by the "an absolute address cannot name paged storage" invariant.
   - Cycle 28 (Cycle 3) #02: an identical-gate `when` pair separated by an unconditional act run merges backward — `when(G){A}; X; when(G){B}` becomes `X; when(G){A;B}` when `X` is a contiguous run of bare `Act`s or `is_unconditional_block`-shaped `Trigger`s and every act in `X` provably commutes with the leaf earlier body `A` and with the gate's storage. The proof uses `scheduler::ActTouches` for reads and `temporal::StorageKey` for write-key disjointness (including `Epd`-vs-`Var` and distinct `Epd`/`Switch` keys). A `machinery` field, unknown write key, reloc that is not a `dest`/`value` or that targets a trigger/string/blob, conditional trigger, store/transfer, nested temporal node, or chain pointer is a barrier. `once` stays adjacent-only: a one-shot's cadence needs its own proof.
   - Cycle 28 (Cycle 3) #03: after `temporal::optimize_stmts` has scheduled the module root and every function body, remove conditional gates with no observable effect: a `Trigger` with no actions, no `prev`/`next`, and no incoming reference (another trigger's `prev`/`next`, an `HirReloc::target_trigger`, or a `HirVarStore::dst_trigger`) anywhere in the module; a `when` whose body and `else_body` are both empty; and a `once` with an empty body. An empty `while` is excluded — it is a spin-wait on external state and can hang — and trigger ids are never renumbered. Removing these after scheduling leaves user-authored scope boundaries (for example the `when(Always, None)` marker the `FixedString` boundary probe in `test_ticket9` writes between a static write and a dynamic append) and the scheduler's batching untouched. Dropping a marker can additionally let adjacent act runs pack into one `Trigger` during lowering: the `FixedString` boundary probe goes `29T/3C/332A`, `69,832 B` → `27T/2C/332A`, `65,016 B`.
   - Subroutine (`Func`) bodies are inlined at **every** call site when the function has one call site, or when duplicating the body at all of its sites packs into no more static triggers than emitting it once (`INLINE_EXTRA_TRIGGERS = 0`; a swap that adds a static trigger is rejected even when it saves executed ones — this gate is static-first, see the Cycle 28 amendment). A leaf-act body is unpacked into bare acts before insertion so the scheduler packs the copies across sites. Multi-site inlining is all-or-nothing; a body containing any other statement kind (`Once`/`While`/`Until`/`Edge`/`Every`/`After`/`Call`/`VarTransfer`/`VarBatch`/`VarStore`), a `preserved: false` trigger, a chain pointer, a trigger relocation, or an externally referenced trigger id stays shared.

## Consequences

- **Optimization priority**: Strictly prioritizes `executed cost (NT/loop)` > `static cost (NT static)` > byte footprint, except for whole-body duplication (decision 5's `Func` inlining), which is gated on static cost first so a body cannot multiply the static program to save executed triggers.
- **Standalone toolchain**: `eudkit` uses clean `3.0` HIR/LIR schemas and zero legacy `eudplib` Python/PyO3 code.
