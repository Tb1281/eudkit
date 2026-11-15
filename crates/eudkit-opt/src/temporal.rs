//! Temporal control flow optimization passes:
//! 1. Desugar `Until` into the latch shape (C11-02): a trip `when` plus a body
//!    `when`, or only the trip for an unconditional gate (issue #72).
//! 2. Conjoin nested `when(c1, () => when(c2, body))` blocks up to 16
//!    conditions, including condition CSE and comparison fusion.
//! 3. Merge consecutive identically-gated `when`s and `once`s whose bodies are
//!    flat (`Act | VarTransfer | VarBatch | VarStore`), generalizing the #90
//!    dependency guard to any flat body (`is_guarded_flat_body` replaces the
//!    per-kind leaf/transfer guards; `body_writes_only_paged_vars` also
//!    admits transfers and store-free batches, and #112 adds precise
//!    [`StorageKey`] address keys for external-state gates — see the step-3
//!    comment). Cycle 3 #02 adds a backward path for `when` (`A; X; B` →
//!    `X; A;B` when `X` is an unconditional act run that provably commutes
//!    with the leaf earlier body `A` and the gate's storage).
//! 4. Pack leaf `when` and `once` blocks into `Trigger (16C, 64A)`.
//! 5. Coalesce adjacent transfer runs.

use std::cmp::{max, min};

use eudkit_hir::{
    HirAct, HirAfter, HirCond, HirEdge, HirEdgeDirection, HirError, HirEvery, HirModule, HirReloc,
    HirStmt, HirTrigger, HirUntil, HirVarStore, HirWhen, TriggerRefs,
};
#[cfg(test)]
use eudkit_hir::{HirOnce, HirWhile};
use eudkit_lir::{
    ACT_COUNT, COND_COUNT, COND_PLAYER_FIELD_OFFSET, DEST_FIELD_OFFSET, VALUE_FIELD_OFFSET,
};

use crate::facts::{ActEffects, FULL_MASK, MOD_SET_TO};
use crate::scheduler::{BlockKind, BlockScheduler, CellMap};

/// Desugar `HirUntil` into exactly today's DSL shape (C11-02): one latch
/// var (init 1); `when(latch==1){ when(conds){ latch.set(0) } }`
/// plus `when(latch==1){ body }`.
///
/// The two whens are emitted trip-first, byte-identical to the
/// DSL-desugar baseline — except for an unconditional gate (no conds, or
/// all-`Always`, which emits no real test), where only the trip-when is
/// emitted and the body is dropped. The trip fires on the very first
/// loop, so under the exclusive semantics (user decision 2026-11-11,
/// issue #72) the body never runs; emitting the body-when would let a
/// flat leaf-act body ride along in the merged trip+body trigger and
/// run once. The latch id is still used verbatim by the trip, so the
/// C11-02 reserved-id invariant holds. See ADR 0006's amendments.
///
/// The latch id comes from the node itself: the DSL reserved it at
/// node-emission time, in exactly the old allocation order. This
/// function never mints ids (see the `HirUntil::latch_var` docs for why
/// opt minting cannot preserve rank order).
///
/// The returned statements are RAW (not yet optimized): the caller runs
/// them through `optimize_stmts` so they receive exactly the
/// flatten/fuse/merge/pack processing the DSL-emitted whens receive. The
/// latch condition and reset act replicate the DSL encodings bit for bit:
/// `latch.eq(1)` is Deaths/Exactly/1 with a mode-1 value-field reloc, and
/// `latch.set(0)` is SetDeathsX/SetTo/0 with a full mask and a mode-1
/// value-field reloc.
fn desugar_until(u: HirUntil) -> Vec<HirStmt> {
    let latch = u.latch_var;
    let mut latch_cond = HirCond::deaths(0, 10, 1, 0);
    latch_cond.relocs = vec![HirReloc::var(4, latch, 20, 1)];
    let mut reset = HirAct::set_deathsx(0, 7, 0, 0, 0xFFFF_FFFF);
    reset.relocs = vec![HirReloc::var(16, latch, 20, 1)];
    // An unconditional gate has nothing to wait for: the trip fires on the
    // very first loop. `Always()` (condtype 22) is unconditional too — it
    // reaches here through `ConstFixedArrayValue._cmp` and masked-`Var`
    // folds, and `_normalize_conds` wraps it as `[Always]`.
    //
    // Exclusive semantics (user decision 2026-11-11, issue #72): the body
    // never runs, so only the trip-when is emitted. A body-when here
    // would be a second statement that re-checks `[latch==1]` and never
    // fires — except for a flat leaf-act body, whose acts would pack
    // with the reset into ONE trigger and run once anyway.
    let unconditional = u.conds.is_empty() || u.conds.iter().all(|c| c.condtype == 22);
    let trip_when = HirStmt::When(HirWhen::new(
        vec![latch_cond.clone()],
        vec![HirStmt::When(HirWhen::new(
            u.conds,
            vec![HirStmt::Act(reset)],
        ))],
    ));
    if unconditional {
        vec![trip_when]
    } else {
        // Every real gate keeps the trip-first order, byte-identical.
        let body_when = HirStmt::When(HirWhen::new(vec![latch_cond], u.body));
        vec![trip_when, body_when]
    }
}

/// Fuse comparison bounds and deduplicate common conditions.
pub fn fuse_and_dedup_conditions(conds: &[HirCond]) -> Vec<HirCond> {
    if conds.len() <= 1 {
        return conds.to_vec();
    }

    let mut out: Vec<HirCond> = Vec::new();

    for cond in conds {
        // Skip Always (22) if we already have or will have other conditions
        if cond.condtype == 22 && conds.iter().any(|c| c.condtype != 22) {
            continue;
        }

        // Check if exact duplicate already in out
        if out.contains(cond) {
            continue;
        }

        // Try to fuse with existing conditions on the same target
        // Target match: Deaths/DeathsX (15), same player, unit, mask (locid), eudx, relocs
        let mut fused = false;
        if cond.condtype == 15 {
            for existing in out.iter_mut() {
                if existing.condtype == 15
                    && existing.player == cond.player
                    && existing.unit == cond.unit
                    && existing.locid == cond.locid
                    && existing.eudx == cond.eudx
                    && existing.relocs == cond.relocs
                {
                    // Case 1: Both AtLeast (0)
                    if existing.comparison == 0 && cond.comparison == 0 {
                        existing.amount = max(existing.amount, cond.amount);
                        fused = true;
                        break;
                    }
                    // Case 2: Both AtMost (1)
                    if existing.comparison == 1 && cond.comparison == 1 {
                        existing.amount = min(existing.amount, cond.amount);
                        fused = true;
                        break;
                    }
                    // Case 3: One AtLeast and One AtMost with same amount -> Exactly (10)
                    if existing.comparison == 0 && cond.comparison == 1 && existing.amount == cond.amount {
                        existing.comparison = 10;
                        fused = true;
                        break;
                    }
                    if existing.comparison == 1 && cond.comparison == 0 && existing.amount == cond.amount {
                        existing.comparison = 10;
                        fused = true;
                        break;
                    }
                    // Case 4: Existing Exactly (10), new AtLeast or AtMost is redundant
                    if existing.comparison == 10 {
                        if (cond.comparison == 0 && existing.amount >= cond.amount)
                            || (cond.comparison == 1 && existing.amount <= cond.amount)
                        {
                            fused = true;
                            break;
                        }
                    }
                    // Case 5: New Exactly (10), existing AtLeast or AtMost is redundant
                    if cond.comparison == 10 {
                        if (existing.comparison == 0 && cond.amount >= existing.amount)
                            || (existing.comparison == 1 && cond.amount <= existing.amount)
                        {
                            existing.comparison = 10;
                            existing.amount = cond.amount;
                            fused = true;
                            break;
                        }
                    }
                }
            }
        }

        if !fused {
            out.push(cond.clone());
        }
    }

    if out.is_empty() && conds.iter().any(|c| c.condtype == 22) {
        out.push(HirCond::always());
    }

    out
}

/// SetDeaths modifier codes for the counted-form desugar (C11-04).
const MOD_ADD: u8 = 8;
const MOD_SUB: u8 = 9;
/// Deaths comparison codes used by the counted-form desugar.
const CMP_AT_LEAST: u8 = 0;
const CMP_EXACTLY: u8 = 10;

/// C11-04: desugar `every(n)` to exactly today's DSL shape — one
/// unconditional counter increment plus a fire `when` that resets the
/// counter and runs the body. The counter id comes from the node's
/// `counter_var` field (allocated by the DSL at emission time, in the
/// old order); opt never mints var ids.
fn desugar_every(e: HirEvery) -> Vec<HirStmt> {
    let cnt = e.counter_var;
    let mut fire_body = vec![HirStmt::Act(HirAct::set_var_const(
        cnt, MOD_SET_TO, 0, FULL_MASK,
    ))];
    fire_body.extend(e.body);
    vec![
        HirStmt::Act(HirAct::set_var_const(cnt, MOD_ADD, 1, FULL_MASK)),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(cnt, CMP_AT_LEAST, e.n)],
            fire_body,
        )),
    ]
}

/// C11-04: desugar `after(n)` to exactly today's DSL shape — the 3-`when`
/// arm/decrement/fire sequence, using the node's `counter_var` verbatim.
fn desugar_after(a: HirAfter) -> Vec<HirStmt> {
    let cnt = a.counter_var;
    let mut fire_body = vec![HirStmt::Act(HirAct::set_var_const(
        cnt, MOD_SET_TO, 0, FULL_MASK,
    ))];
    fire_body.extend(a.body);
    vec![
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(cnt, CMP_EXACTLY, 1)],
            fire_body,
        )),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(cnt, CMP_AT_LEAST, 2)],
            vec![HirStmt::Act(HirAct::set_var_const(
                cnt, MOD_SUB, 1, FULL_MASK,
            ))],
        )),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(cnt, CMP_EXACTLY, 0)],
            vec![HirStmt::Act(HirAct::set_var_const(
                cnt, MOD_SET_TO, a.n, FULL_MASK,
            ))],
        )),
    ]
}

fn is_leaf_body(stmts: &[HirStmt]) -> bool {
    stmts.iter().all(|s| matches!(s, HirStmt::Act(_)))
}

/// Whether every statement in `stmts` is *flat*: a leaf act, a variable
/// transfer, a coalesced transfer/store batch, or a variable store.
///
/// A flat body has no control flow of its own, so the scheduler can reorder
/// its members without crossing a nested gate. Nested temporal nodes
/// (`when`/`once`/`While`/`until`/...) are exactly the non-leaf boundary a
/// sibling merge may not cross; a leaf-act run is the strict subset with only
/// `Act` statements.
fn is_flat_body(stmts: &[HirStmt]) -> bool {
    stmts.iter().all(|s| {
        matches!(
            s,
            HirStmt::Act(_) | HirStmt::VarTransfer(_) | HirStmt::VarBatch(_) | HirStmt::VarStore(_)
        )
    })
}

/// Variable ids a condition list reads: every `relocs[].target_var`, deduped.
///
/// Conditions without relocs (e.g. a stock `Deaths`) read no variable, so
/// they contribute nothing. Used by the sibling-`when` transfer merge guard
/// (ADR 0001 §3): a body may only join an identically-gated sibling when no
/// transfer destination is in this read set, so the second gate would always
/// see the same values the first one did.
fn cond_read_vars(conds: &[HirCond]) -> Vec<usize> {
    let mut out: Vec<usize> = Vec::new();
    for cond in conds {
        for reloc in &cond.relocs {
            if let Some(id) = reloc.target_var {
                if !out.contains(&id) {
                    out.push(id);
                }
            }
        }
    }
    out
}

/// Whether `stmts` is a flat body none of whose *variable* destinations is in
/// `read_vars` — the body side of the sibling-`when` merge guard (#90,
/// generalized to any flat body by #98).
///
/// Acts name a variable through a `relocs[].target_var`; an act without one
/// writes a constant, absolute, trigger or string address, so it cannot change
/// a variable the shared gate reads. Transfers and batch transfers name their
/// written variable through `dst_var`. A store names no read-set variable at
/// all: its destination is an absolute cell or trigger field, it writes no
/// paged variable the shared gate could read, and its own source read stays in
/// program order because a `VarStore` is one statement, not a destination.
/// A batch's `stores` are stores in the same sense and are likewise ignored
/// here (only the batch's transfers are checked). An empty body is admitted
/// (`is_guarded_flat_body(&[], …) == true`): it contributes nothing, so
/// merging it changes no order.
/// Any non-flat statement rejects the run (the caller already requires a flat
/// body).
fn is_guarded_flat_body(stmts: &[HirStmt], read_vars: &[usize]) -> bool {
    stmts.iter().all(|stmt| match stmt {
        HirStmt::Act(a) => a
            .relocs
            .iter()
            .all(|reloc| reloc.target_var.is_none_or(|v| !read_vars.contains(&v))),
        HirStmt::VarTransfer(t) => !read_vars.contains(&t.dst_var),
        HirStmt::VarBatch(b) => b.transfers.iter().all(|t| !read_vars.contains(&t.dst_var)),
        HirStmt::VarStore(_) => true,
        _ => false,
    })
}

/// Whether `cond` reads its storage through a paged variable: condtype 15
/// (Deaths/DeathsX/Memory) names its cell through a `player` reloc at
/// [`COND_PLAYER_FIELD_OFFSET`]; condtypes 22/23 (Always/Never) read no storage
/// at all. Every other cond kind (e.g. 11/Switch) names an absolute location
/// and reads no variable.
///
/// A masked `DeathsX` read keeps `eudx = EUDX_MAGIC` and `locid = mask`; the
/// field-4 reloc with a `target_var` is the discriminator that covers both the
/// plain and the masked form, so this predicate must not test `eudx`/`locid`.
fn cond_keyed_by_paged_var(cond: &HirCond) -> bool {
    if cond.condtype == 22 || cond.condtype == 23 {
        return true;
    }
    cond.relocs
        .iter()
        .any(|reloc| reloc.field_offset == COND_PLAYER_FIELD_OFFSET && reloc.target_var.is_some())
}

/// Whether `act` writes paged-variable storage: a var reloc at
/// [`DEST_FIELD_OFFSET`]. An act without one writes a constant, absolute,
/// trigger or string address.
fn act_writes_paged_var(act: &HirAct) -> bool {
    act.relocs
        .iter()
        .any(|reloc| reloc.field_offset == DEST_FIELD_OFFSET && reloc.target_var.is_some())
}

/// A non-empty flat run in which every statement writes paged variable
/// storage: an act with a `dest`-field var reloc, or a transfer / store-free
/// batch (transfers write paged variables by definition).
///
/// A body of only paged-var writes cannot change storage named by an absolute
/// address, which is exactly the storage class an external-state gate
/// (`Memory`/`Deaths`/`Switch`) reads, so the sibling merge is safe against
/// such a gate. A `VarStore` writes an absolute cell or a trigger field, so it
/// is rejected here: this is the paged-only fast path (clause (ii)).
/// Absolute writes under an external gate are admitted instead by
/// clause (iii), which compares their precise `StorageKey`s.
fn body_writes_only_paged_vars(stmts: &[HirStmt]) -> bool {
    if stmts.is_empty() {
        return false;
    }
    stmts.iter().all(|stmt| match stmt {
        HirStmt::Act(a) => act_writes_paged_var(a),
        HirStmt::VarTransfer(_) => true,
        HirStmt::VarBatch(b) => b.stores.is_empty(),
        _ => false,
    })
}

/// A storage identity named by a gate condition or a body write (#112).
///
/// `Epd` holds the raw HIR field value, so all three absolute-address sources
/// are directly comparable: `Memory(FLAG).player == SetMemory(FLAG).dest ==
/// eudkit_lir::epd_u32(FLAG)`. `Unknown` means the construct is not modelled
/// (dynamic base, string/blob/trigger reloc, value reloc on an act whose write
/// key is derived from its static `value`, unaligned store address); it always
/// fails disjointness, so the merge falls back to today's answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StorageKey {
    Var(usize),
    Epd(u32),
    Switch(u16),
    Unknown,
}

/// Whether two storage keys name provably different cells.
///
/// Symmetric. Different ids within one class are disjoint. `(Var, Epd)` and
/// `(Var, Switch)` are disjoint under the load-bearing invariant that an
/// absolute address cannot name paged storage: frontend output allocates paged
/// variables in `TriggerPage` slots and absolute writes from EPD/switch
/// addresses, while `Var.value`/`_resolve_epd_dest` keep a reloc placeholder
/// instead of emitting a numeric address. The same invariant already carries
/// the #60 root interleave and the sibling-merge paged fast path. Raw HIR that
/// violates it is outside the model, not a counterexample the merge could
/// detect; `Unknown` on either side always fails. `Epd` vs `Switch` is
/// deliberately **not** disjoint — switch state lives in game memory but this
/// repo models it only as a separate `switches: [bool; 256]` array, so
/// `SetMemory` aliasing a switch is unmodelled.
fn storage_keys_disjoint(a: &StorageKey, b: &StorageKey) -> bool {
    use StorageKey::{Epd, Switch, Unknown, Var};
    match (a, b) {
        (Unknown, _) | (_, Unknown) => false,
        (Epd(x), Epd(y)) => x != y,
        (Switch(x), Switch(y)) => x != y,
        (Var(x), Var(y)) => x != y,
        (Epd(_), Switch(_)) | (Switch(_), Epd(_)) => false,
        (Var(_), Epd(_)) | (Epd(_), Var(_)) => true,
        (Var(_), Switch(_)) | (Switch(_), Var(_)) => true,
    }
}

/// The storage key a gate condition reads, if it reads any.
///
/// `None` means the condition reads no storage (`Always`/`Never`); a reloc
/// outside the field-4 `player` slot, a non-const field-4 reloc, the dynamic
/// `CurrentPlayer` base, and every unmodelled cond kind yield `Unknown`.
fn gate_storage_key(cond: &HirCond) -> Option<StorageKey> {
    // Only condtype 15 keys its cell through the player field. Any reloc on
    // another field (the comparison input, e.g. `amount`) gives that value
    // non-constant provenance, and a reloc on a non-15 cond is not modelled.
    if cond
        .relocs
        .iter()
        .any(|reloc| cond.condtype != 15 || reloc.field_offset != COND_PLAYER_FIELD_OFFSET)
    {
        return Some(StorageKey::Unknown);
    }
    match cond.condtype {
        22 | 23 => None,
        15 => match cond
            .relocs
            .iter()
            .find(|reloc| reloc.field_offset == COND_PLAYER_FIELD_OFFSET)
        {
            Some(reloc) => match reloc.target_var {
                Some(var_id) => Some(StorageKey::Var(var_id)),
                None => Some(StorageKey::Unknown),
            },
            None if cond.player == 13 => Some(StorageKey::Unknown),
            None => Some(StorageKey::Epd(cond.player)),
        },
        11 => Some(StorageKey::Switch(u16::from(cond.restype))),
        _ => Some(StorageKey::Unknown),
    }
}

/// The union of the keys a gate's conditions read. `None` if any condition is
/// `Unknown`: the gate must be wholly modelled before it can admit a merge.
fn external_gate_keys(conds: &[HirCond]) -> Option<Vec<StorageKey>> {
    let mut keys = Vec::new();
    for cond in conds {
        match gate_storage_key(cond) {
            Some(StorageKey::Unknown) => return None,
            Some(key) => keys.push(key),
            None => {}
        }
    }
    Some(keys)
}

/// The key an act writes through its `dest` field.
fn act_storage_key(act: &HirAct) -> StorageKey {
    // `SetSwitch` (13) derives its switch id from the static `value` field.
    // A `value`-field reloc makes that id a runtime patch, so the fabricated
    // static id must not be used to prove two switches disjoint.
    if act.acttype == 13
        && act
            .relocs
            .iter()
            .any(|reloc| reloc.field_offset == VALUE_FIELD_OFFSET)
    {
        return StorageKey::Unknown;
    }
    if let Some(reloc) = act
        .relocs
        .iter()
        .find(|reloc| reloc.field_offset == DEST_FIELD_OFFSET)
    {
        return match reloc.target_var {
            Some(var_id) => StorageKey::Var(var_id),
            None => StorageKey::Unknown,
        };
    }
    match act.acttype {
        // SetDeaths/SetDeathsX/SetMemory and their masked variants all write
        // one dword; mask/eudx does not narrow the key (same dword, so the
        // comparison stays conservative).
        45 if act.dest == 13 => StorageKey::Unknown,
        45 => StorageKey::Epd(act.dest),
        // SetSwitch and its set/clear/toggle/random modifiers all write the
        // same switch id; the simulator reads the low byte.
        13 => StorageKey::Switch(u16::from((act.value & 0xFF) as u8)),
        _ => StorageKey::Unknown,
    }
}

/// The key a `VarStore` writes, or `Unknown` for a trigger field or an
/// unaligned address. #111 rejects unaligned absolute dwords at the frontend;
/// raw HIR can still carry one, so the key model defends here too.
fn var_store_storage_key(store: &HirVarStore) -> StorageKey {
    if store.dst_trigger.is_some() {
        return StorageKey::Unknown;
    }
    match store.dst_addr {
        Some(addr) if addr & 3 == 0 => StorageKey::Epd(eudkit_lir::epd_u32(addr)),
        _ => StorageKey::Unknown,
    }
}

/// Every key an earlier body writes. `None` if any write is `Unknown`; each
/// statement contributes one key per written cell (a batch contributes its
/// transfers and its stores).
fn external_body_keys(stmts: &[HirStmt]) -> Option<Vec<StorageKey>> {
    let mut keys = Vec::new();
    for stmt in stmts {
        match stmt {
            HirStmt::Act(act) => keys.push(act_storage_key(act)),
            HirStmt::VarTransfer(transfer) => keys.push(StorageKey::Var(transfer.dst_var)),
            HirStmt::VarBatch(batch) => {
                keys.extend(
                    batch
                        .transfers
                        .iter()
                        .map(|transfer| StorageKey::Var(transfer.dst_var)),
                );
                keys.extend(batch.stores.iter().map(var_store_storage_key));
            }
            HirStmt::VarStore(store) => keys.push(var_store_storage_key(store)),
            _ => return None,
        }
    }
    if keys.contains(&StorageKey::Unknown) {
        None
    } else {
        Some(keys)
    }
}

/// Whether every gate key is provably a different cell from every body-write
/// key. An empty side is vacuously disjoint (it names/reads no storage).
fn storage_key_sets_disjoint(gate: &[StorageKey], body: &[StorageKey]) -> bool {
    gate.iter().all(|gate_key| {
        body.iter()
            .all(|write| storage_keys_disjoint(gate_key, write))
    })
}

/// Every storage key a gate reads: the external keys of
/// [`external_gate_keys`] plus a [`StorageKey::Var`] for each paged variable
/// in [`cond_read_vars`] (deduped — a field-4 reloc already yields the `Var`
/// key). `None` if any condition is unmodelled.
fn gate_storage_keys(conds: &[HirCond]) -> Option<Vec<StorageKey>> {
    let mut keys = external_gate_keys(conds)?;
    for var in cond_read_vars(conds) {
        let key = StorageKey::Var(var);
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    Some(keys)
}

/// Classify an act for the backward gate-merge reorder proof (cycle 3 #02).
///
/// Unlike the scheduler's batch rule, this proof is key-aware: an absolute
/// `SetDeaths` destination is a `StorageKey::Epd`, so it can commute with a
/// paged gate. The act must still be wholly modelled:
/// - a reloc may only name the `dest` or `value` field (page machinery is not
///   reorderable),
/// - no trigger/string/blob reloc (the target is not a comparable cell),
/// - no unmodelled absolute read (a `value` reloc without a `target_var`),
/// - no non-empty [`ActEffects::machinery`] set, and
/// - a known [`act_storage_key`] write.
fn modelled_act(act: &HirAct, cells: &CellMap) -> Option<ActEffects> {
    for reloc in &act.relocs {
        if reloc.field_offset != DEST_FIELD_OFFSET && reloc.field_offset != VALUE_FIELD_OFFSET {
            return None;
        }
        if reloc.target_trigger.is_some()
            || reloc.target_string.is_some()
            || reloc.target_blob.is_some()
        {
            return None;
        }
        if reloc.field_offset != DEST_FIELD_OFFSET && reloc.target_var.is_none() {
            return None;
        }
    }
    let touches = ActEffects::of(act, cells.facts());
    if !touches.machinery.is_empty() {
        return None;
    }
    if matches!(act_storage_key(act), StorageKey::Unknown) {
        return None;
    }
    Some(touches)
}

/// Whether a write to `key` aliases anything `touches` reads (or writes).
fn write_key_aliases_touches(key: &StorageKey, touches: &ActEffects) -> bool {
    match key {
        StorageKey::Var(var) => touches.reads.contains(var) || touches.writes.contains(var),
        _ => false,
    }
}

/// Whether two modelled acts may swap without changing an observation: their
/// write keys are provably disjoint and neither write aliases the other's
/// read set.
fn modelled_acts_commute(
    a_key: &StorageKey,
    a: &ActEffects,
    b_key: &StorageKey,
    b: &ActEffects,
) -> bool {
    storage_keys_disjoint(a_key, b_key)
        && !write_key_aliases_touches(a_key, b)
        && !write_key_aliases_touches(b_key, a)
}

/// Whether `stmt` is a bare unconditional act for the backward merge run.
/// A `HirStmt::Trigger` qualifies only when [`HirTrigger::is_unpackable`]
/// holds: the one shared shape predicate (no conditions, no chain pointers,
/// preserved, flags `4`) *and* no external name refers to its id.
fn is_unconditional_act_stmt(stmt: &HirStmt, refs: &TriggerRefs) -> bool {
    match stmt {
        HirStmt::Act(_) => true,
        HirStmt::Trigger(trig) => trig.is_unpackable(refs),
        _ => false,
    }
}

/// The acts of an unconditional run statement, in order. A trigger whose id is
/// externally referenced is not part of the run; it is a barrier.
fn unconditional_act_blocks(stmt: &HirStmt, refs: &TriggerRefs) -> Option<Vec<HirAct>> {
    match stmt {
        HirStmt::Act(act) => Some(vec![act.clone()]),
        HirStmt::Trigger(trig) if trig.is_unpackable(refs) => Some(trig.acts.clone()),
        _ => None,
    }
}

/// Re-emit a drained run as bare acts so the normal act packer can join the
/// moved acts into one trigger.
///
/// An unconditional flag-`4` block is folded into its basic block by
/// lowering, so its acts behave like bare acts there; emitting them as
/// `HirStmt::Act` is exactly what makes two separated runs pack into one
/// `Trigger` (`5/3/5` → `2/1/5`). The run's relative order is preserved.
fn unpack_run(run: Vec<HirStmt>, refs: &TriggerRefs) -> Vec<HirStmt> {
    let mut out = Vec::with_capacity(run.len());
    for stmt in run {
        match stmt {
            HirStmt::Trigger(trig) if trig.is_unpackable(refs) => {
                out.extend(trig.acts.into_iter().map(HirStmt::Act));
            }
            other => out.push(other),
        }
    }
    out
}

/// Try the cycle-3 #02 backward merge for `when(G){A}; X; when(G){B}`:
/// drain the unconditional act run `X`, prove it commutes with `A` and the
/// gate, and rewrite to `X; when(G){A;B}`.
///
/// Returns `false` (leaving `merged` untouched) whenever any part of the
/// narrow proof fails. `w2`'s flat-body/empty-`else` precondition is the
/// caller's; this function re-checks it so the contract is local.
fn try_backward_merge(
    merged: &mut Vec<HirStmt>,
    w2: &HirWhen,
    cells: &CellMap,
    refs: &TriggerRefs,
    next_id: &mut usize,
) -> Result<bool, HirError> {
    if !w2.else_body.is_empty() || !is_flat_body(&w2.body) {
        return Ok(false);
    }
    let mut run_start = merged.len();
    while run_start > 0 && is_unconditional_act_stmt(&merged[run_start - 1], refs) {
        run_start -= 1;
    }
    // An empty run is the existing adjacent-merge path, not this one.
    if run_start == merged.len() {
        return Ok(false);
    }
    let Some(w1_idx) = run_start.checked_sub(1) else {
        return Ok(false);
    };
    let HirStmt::When(w1) = &merged[w1_idx] else {
        return Ok(false);
    };
    if !w1.else_body.is_empty()
        || !is_leaf_body(&w1.body)
        || !flat_siblings_mergeable(&w1.body, &w2.body, &w1.conds, &w2.conds)
    {
        return Ok(false);
    }
    let Some(gate_keys) = gate_storage_keys(&w1.conds) else {
        return Ok(false);
    };

    // Every earlier-body act must itself be modelled, because the run is
    // moved across it.
    let mut earlier: Vec<(ActEffects, StorageKey)> = Vec::new();
    for stmt in &w1.body {
        let HirStmt::Act(act) = stmt else {
            return Ok(false);
        };
        let Some(touches) = modelled_act(act, cells) else {
            return Ok(false);
        };
        earlier.push((touches, act_storage_key(act)));
    }

    // Every run act must commute with every earlier-body act and avoid every
    // gate storage key (external cell, switch, or paged gate variable).
    for stmt in &merged[run_start..] {
        let Some(acts) = unconditional_act_blocks(stmt, refs) else {
            return Ok(false);
        };
        for act in &acts {
            let Some(touches) = modelled_act(act, cells) else {
                return Ok(false);
            };
            let key = act_storage_key(act);
            if !gate_keys
                .iter()
                .all(|gate| storage_keys_disjoint(&key, gate))
            {
                return Ok(false);
            }
            for (earlier_touches, earlier_key) in &earlier {
                if !modelled_acts_commute(&key, &touches, earlier_key, earlier_touches) {
                    return Ok(false);
                }
            }
        }
    }

    // Proof holds: drain the run and the earlier sibling, merge, then re-append
    // the run ahead of the merged gate in its original relative order.
    let run = merged.split_off(w1_idx + 1);
    let Some(HirStmt::When(mut merged_when)) = merged.pop() else {
        return Err(HirError::OptimizerInvariant(
            "backward merge expected a When at w1_idx",
        ));
    };
    merged_when.body.extend(w2.body.clone());
    reschedule_merged_body(&mut merged_when.body, cells, next_id)?;
    merged.extend(unpack_run(run, refs));
    merged.push(HirStmt::When(merged_when));
    Ok(true)
}

/// Whether two identically-gated sibling bodies may join (#90 guard,
/// generalized from leaf acts / pure transfer runs to any flat body by #98;
/// shared by the `when` and `once` arms since #91).
///
/// Both bodies must be flat runs that avoid the gate's read set, the gate
/// condition lists must be identical, and the earlier body must be unable to
/// change the gate's storage. Three clauses admit that, and the third was
/// added by #112:
///
/// 1. every gate cond is keyed by a paged variable (vacuously true for a
///    0-cond gate, which reads nothing), or
/// 2. the earlier body writes only paged variables, or
/// 3. every external cell the earlier body writes is provably a different
///    dword from every cell the gate reads ([`StorageKey`]). Unknown keys on
///    either side fail, so clause 3 only ever admits a merge; it can never
///    withdraw one that clauses 1/2 already allowed (#90, #98, #112).
///
/// The 64-act cap applies only when both bodies are pure leaf-act runs, where
/// the merged body must still fit one `Trigger`. A body carrying a store or a
/// transfer is larger than 64 lowered acts by design; the caller re-schedules
/// the merged body through [`BlockScheduler`], which splits it into as many
/// triggers as it needs.
fn flat_siblings_mergeable(
    body1: &[HirStmt],
    body2: &[HirStmt],
    conds1: &[HirCond],
    conds2: &[HirCond],
) -> bool {
    let read_vars = cond_read_vars(conds2);
    let gate_reads_paged_vars = conds2.iter().all(cond_keyed_by_paged_var);
    let both_leaf = is_leaf_body(body1) && is_leaf_body(body2);
    let external_keys_ok = match (external_gate_keys(conds2), external_body_keys(body1)) {
        (Some(gate), Some(body)) => storage_key_sets_disjoint(&gate, &body),
        _ => false,
    };
    is_flat_body(body1)
        && is_flat_body(body2)
        && is_guarded_flat_body(body1, &read_vars)
        && is_guarded_flat_body(body2, &read_vars)
        && conds1 == conds2
        && (gate_reads_paged_vars
            || body_writes_only_paged_vars(body1)
            || external_keys_ok)
        && (!both_leaf || body1.len() + body2.len() <= ACT_COUNT)
}

fn extract_leaf_acts(stmts: &[HirStmt]) -> Vec<HirAct> {
    let mut acts = Vec::new();
    for s in stmts {
        if let HirStmt::Act(a) = s {
            acts.push(a.clone());
        }
    }
    acts
}

fn flatten_nested_when(mut w: HirWhen) -> HirWhen {
    while w.else_body.is_empty() && w.body.len() == 1 {
        if let HirStmt::When(inner) = &w.body[0] {
            if inner.else_body.is_empty() {
                let mut combined = w.conds.clone();
                combined.extend(inner.conds.clone());
                let fused = fuse_and_dedup_conditions(&combined);
                if fused.len() <= COND_COUNT {
                    w = HirWhen {
                        conds: fused,
                        body: inner.body.clone(),
                        else_body: Vec::new(),
                    };
                    continue;
                }
            }
        }
        break;
    }
    w
}

/// Re-coalesce the body of a just-merged sibling when the merge brought in a
/// store or transfer.
///
/// Two pure leaf-act runs concatenate into one act list the packer still
/// handles, so they stay as-is. Any other flat merge creates transfer
/// adjacency neither body had on its own (each body arrived here already
/// coalesced as its own statement list), so the merged body goes back through
/// the block scheduler — the same re-schedule the pre-#98 transfer branch ran.
fn reschedule_merged_body(
    body: &mut Vec<HirStmt>,
    cells: &CellMap,
    next_id: &mut usize,
) -> Result<(), HirError> {
    if is_leaf_body(body) {
        return Ok(());
    }
    let merged = std::mem::take(body);
    *body = BlockScheduler::new(cells, BlockKind::Body, next_id).schedule(&merged)?;
    Ok(())
}

/// The `When` arm's own treatment, shared with the edge desugar so
/// desugared gates receive exactly the treatment DSL-emitted `when`
/// nodes receive: flatten, fuse, recurse, flatten. Packing and merging
/// stay with the caller's full-list passes, so trigger-id consumption
/// and cross-statement merging match the baseline pipeline.
fn optimize_when_node(
    mut w: HirWhen,
    cells: &CellMap,
    refs: &TriggerRefs,
    next_id: &mut usize,
) -> Result<HirWhen, HirError> {
    w = flatten_nested_when(w);
    w.conds = fuse_and_dedup_conditions(&w.conds);
    w.body = optimize_stmts(w.body, BlockKind::Body, cells, refs, next_id)?;
    w.else_body = optimize_stmts(w.else_body, BlockKind::Body, cells, refs, next_id)?;
    w = flatten_nested_when(w);
    Ok(w)
}

/// Desugar one [`HirEdge`] into exactly today's DSL 5-statement shape.
///
/// Shared logic for both polarities; only the fire gate's comparison
/// pair differs (rising: curr==1 && prev==0; falling: curr==0 && prev==1).
/// The sample and update gates are identical. Order is load-bearing:
/// sample, then fire, then update `prev`. The reset is a plain top-level
/// act: the caller's bin-packing pass packs it exactly as the DSL builder
/// packs top-level acts (HIR trigger ids never reach compiled output).
/// Comparison `10` is Exactly; modifier `7` is SetTo. State-variable ids
/// come from the node itself (DSL-reserved at emission time); this
/// function never mints ids.
fn desugar_edge(e: &HirEdge) -> Vec<HirStmt> {
    let curr = e.curr_var;
    let prev = e.prev_var;
    let (fire_curr, fire_prev) = match e.direction {
        HirEdgeDirection::Rising => (1, 0),
        HirEdgeDirection::Falling => (0, 1),
    };
    vec![
        HirStmt::Act(HirAct::set_var_const(curr, 7, 0, 0xFFFF_FFFF)),
        HirStmt::When(HirWhen::new(
            e.conds.clone(),
            vec![HirStmt::Act(HirAct::set_var_const(curr, 7, 1, 0xFFFF_FFFF))],
        )),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(curr, 10, fire_curr)],
            vec![HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(prev, 10, fire_prev)],
                e.body.clone(),
            ))],
        )),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(curr, 10, 1)],
            vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 1, 0xFFFF_FFFF))],
        )),
        HirStmt::When(HirWhen::new(
            vec![HirCond::var_deaths(curr, 10, 0)],
            vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 0, 0xFFFF_FFFF))],
        )),
    ]
}

/// Recursively optimize statement list:
/// 1. Desugar `Until` nodes into the latch shape (C11-02), using each node's
///    DSL-reserved latch id verbatim (no minting): a trip `when` plus a body
///    `when`, or only the trip for an unconditional gate (issue #72).
/// 2. Conjoin nested `When` blocks.
/// 3. Merge consecutive identically-gated `When` and `Once` blocks.
/// 4. Pack leaf `When` and `Once` blocks into `HirStmt::Trigger`.
/// 5. Schedule the resulting block with [`BlockScheduler`], which coalesces
///    transfer/store runs, packs adjacent acts, and orders acts around the
///    batch — the one rule for the module root and every body alike.
///
/// The desugar/merge/pack half lives in [`optimize_list`] so the `Until`
/// desugar expansion can be pre-processed without scheduling it twice: the
/// expansion is scheduled once, as part of the block it is spliced into.
///
/// `refs` is the module-wide trigger-reference set: a trigger it names is a
/// barrier to the backward merge and is never unpacked. `next_id` is the
/// trigger-id namespace, consumed for packed triggers.
pub(crate) fn optimize_stmts(
    stmts: Vec<HirStmt>,
    kind: BlockKind,
    cells: &CellMap,
    refs: &TriggerRefs,
    next_id: &mut usize,
) -> Result<Vec<HirStmt>, HirError> {
    let list = optimize_list(stmts, cells, refs, next_id)?;
    BlockScheduler::new(cells, kind, next_id).schedule(&list)
}

/// The pre-scheduling half of [`optimize_stmts`]: desugar, flatten, merge,
/// and pack, recursing into nested bodies (each of which is its own block and
/// is scheduled on the way back up).
fn optimize_list(
    stmts: Vec<HirStmt>,
    cells: &CellMap,
    refs: &TriggerRefs,
    next_id: &mut usize,
) -> Result<Vec<HirStmt>, HirError> {
    let mut processed = Vec::with_capacity(stmts.len());

    for stmt in stmts {
        match stmt {
            HirStmt::When(w) => {
                processed.push(HirStmt::When(optimize_when_node(w, cells, refs, next_id)?));
            }
            HirStmt::Once(mut o) => {
                o.conds = fuse_and_dedup_conditions(&o.conds);
                o.body = optimize_stmts(o.body, BlockKind::Body, cells, refs, next_id)?;
                processed.push(HirStmt::Once(o));
            }
            HirStmt::While(mut wh) => {
                wh.conds = fuse_and_dedup_conditions(&wh.conds);
                wh.body = optimize_stmts(wh.body, BlockKind::Body, cells, refs, next_id)?;
                processed.push(HirStmt::While(wh));
            }
            // C11-02: desugar to the raw 2-when shape, then run the whens
            // through the list passes so they receive exactly the processing
            // the DSL-emitted whens receive. The body is NOT pre-optimized
            // here: it is optimized as part of the desugared whens, which
            // also recurses into nested Until nodes (desugar order matches
            // DSL authoring order: outer latch first, then inner latches).
            // The expansion is not scheduled here: the caller schedules the
            // block it is spliced into.
            HirStmt::Until(u) => {
                let raw = desugar_until(u);
                let desugared = optimize_list(raw, cells, refs, next_id)?;
                processed.extend(desugared);
            }
            HirStmt::Edge(mut e) => {
                // Recurse first so nested edges desugar before the outer
                // expansion is built (nesting, design Q8); the expansion's
                // gates then get the same flatten/fuse treatment as
                // DSL-emitted whens. Packing and merging run on the full
                // list afterwards, as in the baseline pipeline.
                e.body = optimize_stmts(e.body, BlockKind::Body, cells, refs, next_id)?;
                for stmt in desugar_edge(&e) {
                    match stmt {
                        HirStmt::When(w) => {
                            processed.push(HirStmt::When(optimize_when_node(
                                w, cells, refs, next_id,
                            )?));
                        }
                        other => processed.push(other),
                    }
                }
            }
            HirStmt::Every(mut e) => {
                // Body first (recursion), then desugar. The produced
                // whens flow through merge/pack below exactly like the
                // DSL-emitted whens did. Flatten/fuse on the produced
                // whens is a no-op (single cond; body starts with an act
                // or has length >= 2), so skipping the When-arm pass is
                // safe; the byte-identity tests pin this.
                e.body = optimize_stmts(e.body, BlockKind::Body, cells, refs, next_id)?;
                let mut desugared = desugar_every(e);
                let iadd = match desugared.remove(0) {
                    HirStmt::Act(a) => a,
                    other => unreachable!("desugar_every head is an act: {other:?}"),
                };
                let fire = desugared.remove(0);
                // Merge-backward: the DSL flush packs a preceding
                // top-level act run into a Trigger stmt before the node,
                // while the old DSL packed the increment into that same
                // run. Rejoining the increment with that run reproduces
                // the old packing exactly. Merge only into a trigger with
                // `unpack_shape()` — unlinked (`prev`/`next` absent),
                // `flags == 4`, unconditional, preserved — and room:
                // merging into a conditional trigger would gate the
                // increment, merging into a self-disabling trigger would
                // run it once only, merging into a chained or non-flag-4
                // trigger would misresolve its packed encoding, and
                // merging into a full trigger would overflow it.
                // Otherwise the increment stays a bare act and becomes
                // its own trigger downstream, as a fresh act run would.
                let can_merge = matches!(
                    processed.last(),
                    Some(HirStmt::Trigger(t)) if t.unpack_shape() && t.acts.len() < ACT_COUNT
                );
                if can_merge {
                    if let Some(HirStmt::Trigger(t)) = processed.last_mut() {
                        t.acts.push(iadd);
                    }
                } else {
                    processed.push(HirStmt::Act(iadd));
                }
                processed.push(fire);
            }
            HirStmt::After(mut a) => {
                // After desugars to whens only (no bare act), so no
                // merge-backward applies; the produced whens flow through
                // merge/pack below exactly like the DSL-emitted ones.
                a.body = optimize_stmts(a.body, BlockKind::Body, cells, refs, next_id)?;
                processed.extend(desugar_after(a));
            }
            other => processed.push(other),
        }
    }

    // Step 3: Merge consecutive identically-gated sibling `when` and `once`
    // blocks whose bodies are *flat* (`Act | VarTransfer | VarBatch |
    // VarStore`) — #98 generalized this from leaf-act runs and pure transfer
    // runs. Same-kind only — a `once` never merges into a `when` (their
    // cadences differ: each `once` self-disables independently).
    //
    // The merge is dependency-guarded: the merged group evaluates the shared
    // gate once, so no earlier body may change a variable that gate reads (the
    // later body would then run against a stale gate — a RAW hazard). Every
    // variable destination in either body — an act's reloc target, a transfer
    // or batch transfer destination — must avoid the gate's read set; stores
    // impose no such constraint (their destination is an absolute cell or
    // trigger field, not a paged variable the gate could read, and their source
    // read keeps program order). The merged body is then re-coalesced by the
    // block scheduler (ADR 0001 §3, GLOSSARY "sibling `when`/`once` merge").
    //
    // The reloc read set alone is not enough: a `Memory`/`Deaths`/`Switch` gate
    // names its location by an absolute address and carries no relocs at all,
    // so the guard would pass vacuously and an earlier body writing that cell
    // would be treated as harmless. Three clauses close that hole, and any one
    // suffices:
    //   (i) every gate cond is keyed by a paged variable (vacuously true for
    //       a 0-cond gate, which reads nothing), or
    //  (ii) the earlier body, as accumulated by this loop, writes only paged
    //       variables. Absolute (non-reloc) addresses cannot name a paged
    //       variable's storage, so such a body cannot change an
    //       external-state gate's storage. Transfers, and batches' transfers,
    //       write paged storage; a batch carrying `stores` is not paged-only,
    //       and a `VarStore` does not write paged storage at all.
    // (iii) #112: every external cell the accumulated earlier body writes is
    //       provably a different dword from every cell the gate reads
    //       (`StorageKey`). `Epd` keys compare the raw HIR field value, so a
    //       `Memory(FLAG)` gate and a store at `TABLE` are disjoint; equal
    //       keys, an `Epd`-vs-`Switch` pair, and any `Unknown` write fall back
    //       to today's answer. The clause only admits, so it never withdraws
    //       the paged-gate merges clauses (i)/(ii) already allowed.
    // The accumulated earlier body is re-checked on every iteration, so a
    // third sibling cannot ride along after an external write landed in the
    // merged body.
    //
    // The 64-act `Trigger` cap applies only to a pure leaf-act pair (see
    // `flat_siblings_mergeable`): a merged store/transfer body is larger than
    // 64 lowered acts by design and the scheduler splits it into as many
    // triggers as it needs. A `VarStore` with `dst_trigger` under a paged gate
    // is safe but reads as surprising: a paged gate reads only paged storage,
    // so it cannot observe a trigger field the store writes.
    let mut merged: Vec<HirStmt> = Vec::with_capacity(processed.len());
    for stmt in processed {
        let can_merge = match &stmt {
            HirStmt::When(w2) if w2.else_body.is_empty() && is_flat_body(&w2.body) => {
                let mut joined = false;
                if let Some(HirStmt::When(w1)) = merged.last_mut() {
                    if w1.else_body.is_empty()
                        && flat_siblings_mergeable(&w1.body, &w2.body, &w1.conds, &w2.conds)
                    {
                        w1.body.extend(w2.body.clone());
                        joined = true;
                    }
                }
                if joined {
                    if let Some(HirStmt::When(w1)) = merged.last_mut() {
                        reschedule_merged_body(&mut w1.body, cells, next_id)?;
                    }
                } else {
                    // Cycle 3 #02: `when(G){A}; X; when(G){B}` → `X; when(G){A;B}`
                    // when the unconditional run `X` provably commutes with `A`
                    // and the gate's storage.
                    joined = try_backward_merge(&mut merged, w2, cells, refs, next_id)?;
                }
                joined
            }
            HirStmt::Once(o2) if is_flat_body(&o2.body) => {
                let mut joined = false;
                if let Some(HirStmt::Once(o1)) = merged.last_mut() {
                    if flat_siblings_mergeable(&o1.body, &o2.body, &o1.conds, &o2.conds) {
                        o1.body.extend(o2.body.clone());
                        joined = true;
                    }
                }
                if joined {
                    if let Some(HirStmt::Once(o1)) = merged.last_mut() {
                        reschedule_merged_body(&mut o1.body, cells, next_id)?;
                    }
                }
                joined
            }
            _ => false,
        };

        if !can_merge {
            merged.push(stmt);
        }
    }

    // Step 4: Pack leaf When (preserved: true) and Once (preserved: false) blocks into Trigger (16C, 64A)
    let mut packed: Vec<HirStmt> = Vec::with_capacity(merged.len());
    for stmt in merged {
        match stmt {
            HirStmt::When(w) => {
                if w.else_body.is_empty()
                    && w.conds.len() <= COND_COUNT
                    && is_leaf_body(&w.body)
                    && w.body.len() <= ACT_COUNT
                {
                    let acts = extract_leaf_acts(&w.body);
                    let trig_id = *next_id;
                    *next_id += 1;
                    let trig = HirTrigger::new(trig_id, w.conds, acts, true)?;
                    packed.push(HirStmt::Trigger(trig));
                } else {
                    packed.push(HirStmt::When(w));
                }
            }
            HirStmt::Once(o) => {
                if o.conds.len() <= COND_COUNT
                    && is_leaf_body(&o.body)
                    && o.body.len() <= ACT_COUNT
                {
                    let acts = extract_leaf_acts(&o.body);
                    let trig_id = *next_id;
                    *next_id += 1;
                    // Once leaf emits with preserved: false (flags: 0), permanently self-disabling on first execution
                    let trig = HirTrigger::new(trig_id, o.conds, acts, false)?;
                    packed.push(HirStmt::Trigger(trig));
                } else {
                    packed.push(HirStmt::Once(o));
                }
            }
            other => packed.push(other),
        }
    }

    Ok(packed)
}

/// Drop conditional gates with no observable effect after scheduling.
///
/// A 0-act `Trigger` cannot change anything; its only effect is a condition
/// evaluation on every pass. The same is true of a `when` with both arms empty
/// and a `once` with an empty body. An empty `while` is **kept**: it is a
/// spin-wait on external state and can hang forever.
///
/// Removal runs after [`optimize_stmts`] and does not re-schedule the module,
/// so the statement order the scheduler produced is preserved. It cannot
/// promise byte-identical downstream packing, though: removing a user-authored
/// scope boundary (for example the `when(Always, None)` marker the
/// `FixedString` probe authors between a static write and a dynamic append)
/// can expose adjacent act runs that lowering then packs into fewer triggers.
/// Trigger ids are never renumbered, so surviving ids keep their gaps exactly
/// as other dead-code transformations leave them.
pub(crate) fn drop_dead_triggers(module: &mut HirModule) {
    let refs = TriggerRefs::from_module(module);
    module.stmts = drop_dead_gates(std::mem::take(&mut module.stmts), &refs);
    for func in &mut module.funcs {
        func.body = drop_dead_gates(std::mem::take(&mut func.body), &refs);
    }
}

fn drop_dead_gates(stmts: Vec<HirStmt>, refs: &TriggerRefs) -> Vec<HirStmt> {
    stmts
        .into_iter()
        .filter_map(|stmt| match stmt {
            HirStmt::Trigger(t) => {
                if t.acts.is_empty()
                    && t.prev.is_none()
                    && t.next.is_none()
                    && !refs.contains(t.id)
                {
                    None
                } else {
                    Some(HirStmt::Trigger(t))
                }
            }
            HirStmt::When(mut w) => {
                w.body = drop_dead_gates(w.body, refs);
                w.else_body = drop_dead_gates(w.else_body, refs);
                if w.body.is_empty() && w.else_body.is_empty() {
                    None
                } else {
                    Some(HirStmt::When(w))
                }
            }
            HirStmt::Once(mut o) => {
                o.body = drop_dead_gates(o.body, refs);
                if o.body.is_empty() {
                    None
                } else {
                    Some(HirStmt::Once(o))
                }
            }
            // A `while` with an empty body is a spin-wait on external state;
            // keep it even though it has no acts.
            HirStmt::While(mut w) => {
                w.body = drop_dead_gates(w.body, refs);
                Some(HirStmt::While(w))
            }
            HirStmt::Until(mut u) => {
                u.body = drop_dead_gates(u.body, refs);
                Some(HirStmt::Until(u))
            }
            HirStmt::Edge(mut e) => {
                e.body = drop_dead_gates(e.body, refs);
                Some(HirStmt::Edge(e))
            }
            HirStmt::Every(mut e) => {
                e.body = drop_dead_gates(e.body, refs);
                Some(HirStmt::Every(e))
            }
            HirStmt::After(mut a) => {
                a.body = drop_dead_gates(a.body, refs);
                Some(HirStmt::After(a))
            }
            other => Some(other),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{
        HirAfter, HirCond, HirEdge, HirEdgeDirection, HirEvery, HirModule, HirReloc, HirTriggerPtr,
        HirUntil, HirVarBatch, HirVarStore, HirVarTransfer,
    };
    use eudkit_lir::VALUE_FIELD_OFFSET;

    /// Drive [`optimize_stmts`] and schedule the result as a block body, the
    /// shape these list-level tests pin: root act packing belongs to
    /// [`optimize_module`], which the crate-level tests exercise.
    fn optimize_test_stmts(
        stmts: Vec<HirStmt>,
        next_id: &mut usize,
    ) -> Result<Vec<HirStmt>, HirError> {
        let cells = CellMap::from_stmts(&stmts);
        let refs = TriggerRefs::from_stmts(&stmts);
        optimize_stmts(stmts, BlockKind::Body, &cells, &refs, next_id)
    }

    /// Latch condition `latch == 1`, exactly as the DSL `active.eq(1)` encodes it:
    /// Deaths, Exactly, amount 1, with a mode-1 value-field reloc to the latch var.
    fn latch_cond(latch: usize) -> HirCond {
        let mut c = HirCond::deaths(0, 10, 1, 0);
        c.relocs = vec![HirReloc::var(4, latch, 20, 1)];
        c
    }

    /// Latch reset `latch.set(0)`, exactly as the DSL encodes it: SetDeathsX,
    /// SetTo, value 0, full mask, with a mode-1 value-field reloc to the latch var.
    fn latch_reset(latch: usize) -> HirAct {
        let mut a = HirAct::set_deathsx(0, 7, 0, 0, 0xFFFF_FFFF);
        a.relocs = vec![HirReloc::var(16, latch, 20, 1)];
        a
    }

    /// The hand-written old (DSL-desugar) shape for an until node, with an
    /// explicit latch id. Used to pin end-to-end equivalence: the opt desugar
    /// must produce exactly what this shape produces through the same pipeline.
    fn old_until_shape(latch: usize, conds: Vec<HirCond>, body: Vec<HirStmt>) -> Vec<HirStmt> {
        vec![
            HirStmt::When(HirWhen::new(
                vec![latch_cond(latch)],
                vec![HirStmt::When(HirWhen::new(
                    conds,
                    vec![HirStmt::Act(latch_reset(latch))],
                ))],
            )),
            HirStmt::When(HirWhen::new(vec![latch_cond(latch)], body)),
        ]
    }

    /// Reference shape for an unconditional gate: only the reset trip-when.
    /// The body is dropped, so the latch trips on the first loop and the
    /// body never runs (exclusive semantics, issue #72).
    fn trip_only_until_shape(latch: usize, conds: Vec<HirCond>) -> Vec<HirStmt> {
        vec![HirStmt::When(HirWhen::new(
            vec![latch_cond(latch)],
            vec![HirStmt::When(HirWhen::new(
                conds,
                vec![HirStmt::Act(latch_reset(latch))],
            ))],
        ))]
    }

    fn contains_until(stmts: &[HirStmt]) -> bool {
        stmts.iter().any(|s| match s {
            HirStmt::Until(_) => true,
            HirStmt::When(w) => contains_until(&w.body) || contains_until(&w.else_body),
            HirStmt::Once(o) => contains_until(&o.body),
            HirStmt::While(w) => contains_until(&w.body),
            HirStmt::Edge(e) => contains_until(&e.body),
            HirStmt::Every(e) => contains_until(&e.body),
            HirStmt::After(a) => contains_until(&a.body),
            _ => false,
        })
    }

    #[test]
    fn until_desugar_raw_shape_uses_reserved_latch_and_two_whens() {
        let latch = 10;
        let conds = vec![HirCond::deaths(0, 10, 42, 0)];
        let body = vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))];
        let stmts = desugar_until(HirUntil::new(conds.clone(), body.clone(), latch));
        assert_eq!(stmts, old_until_shape(latch, conds, body));
    }

    #[test]
    fn until_desugar_drops_body_for_unconditional_gate() {
        // Exclusive semantics (issue #72): an unconditional gate trips on
        // the first loop, so the body never runs and only the reset
        // trip-when is emitted. A real gate keeps the DSL-desugar
        // (trip-first) order byte-identically.
        let latch = 11;
        let body = vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))];

        let empty = desugar_until(HirUntil::new(vec![], body.clone(), latch));
        assert_eq!(empty, trip_only_until_shape(latch, vec![]));

        let always_conds = vec![HirCond::always()];
        let always = desugar_until(HirUntil::new(always_conds.clone(), body.clone(), latch));
        assert_eq!(always, trip_only_until_shape(latch, always_conds));

        let gated_conds = vec![HirCond::deaths(0, 10, 42, 0)];
        let gated = desugar_until(HirUntil::new(gated_conds.clone(), body.clone(), latch));
        assert_eq!(gated, old_until_shape(latch, gated_conds, body));
    }

    #[test]
    fn until_desugar_end_to_end_matches_handwritten_old_shape() {
        let latch = 5;
        let conds = vec![HirCond::deaths(0, 10, 42, 0)];
        let body = vec![HirStmt::Act(HirAct::set_deaths(1, 7, 9, 0))];
        let node = HirStmt::Until(HirUntil::new(conds.clone(), body.clone(), latch));
        let mut next_id = 0;
        let got = optimize_test_stmts(vec![node], &mut next_id).unwrap();
        let mut next_id2 = 0;
        let want = optimize_test_stmts(old_until_shape(latch, conds, body), &mut next_id2).unwrap();
        assert_eq!(got, want);
        assert!(!contains_until(&got));
    }

    #[test]
    fn until_desugar_recurses_and_nests() {
        // Until inside When, While, Once, and Until inside Until: every
        // node must desugar (none survives), and the Until-inside-Until
        // output must reference both reserved latch ids (outer 9, inner 7).
        // The outer Until is gated on purpose: an unconditional outer
        // gate drops its body (exclusive semantics, issue #72), so the
        // inner node would never appear.
        let leaf = || HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0));
        let inner = HirStmt::Until(HirUntil::new(vec![HirCond::always()], vec![leaf()], 7));
        let cases = vec![
            HirStmt::When(HirWhen::new(vec![HirCond::always()], vec![inner.clone()])),
            HirStmt::While(HirWhile::new(vec![HirCond::always()], vec![inner.clone()])),
            HirStmt::Once(HirOnce::new(vec![HirCond::always()], vec![inner.clone()])),
            HirStmt::Until(HirUntil::new(
                vec![HirCond::deaths(0, 10, 42, 0)],
                vec![inner],
                9,
            )),
        ];
        fn cond_targets(stmts: &[HirStmt], out: &mut std::collections::HashSet<usize>) {
            for s in stmts {
                match s {
                    HirStmt::When(w) => {
                        for c in &w.conds {
                            for r in &c.relocs {
                                if let Some(v) = r.target_var {
                                    out.insert(v);
                                }
                            }
                        }
                        cond_targets(&w.body, out);
                        cond_targets(&w.else_body, out);
                    }
                    HirStmt::Once(o) => cond_targets(&o.body, out),
                    HirStmt::While(w) => cond_targets(&w.body, out),
                    HirStmt::Trigger(t) => {
                        for c in &t.conds {
                            for r in &c.relocs {
                                if let Some(v) = r.target_var {
                                    out.insert(v);
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        for (i, case) in cases.into_iter().enumerate() {
            let mut next_id = 0;
            let out = optimize_test_stmts(vec![case], &mut next_id).unwrap();
            assert!(!contains_until(&out), "case {i} must not contain Until");
            let mut targets = std::collections::HashSet::new();
            cond_targets(&out, &mut targets);
            assert!(
                targets.contains(&7),
                "case {i} must reference inner latch 7"
            );
            if i == 3 {
                assert!(targets.contains(&9), "case 3 must reference outer latch 9");
            }
        }
    }

    #[test]
    fn until_with_empty_conds_desugars() {
        // Empty conds: the latch-off when flattens to a single
        // `when(latch == 1)` gate around the reset act. The body is
        // dropped (exclusive semantics, issue #72), so the trigger
        // holds only the reset act.
        let body_act = HirAct::set_deaths(0, 7, 1, 0);
        let node = HirStmt::Until(HirUntil::new(
            vec![],
            vec![HirStmt::Act(body_act)],
            7,
        ));
        let mut next_id = 0;
        let out = optimize_test_stmts(vec![node], &mut next_id).unwrap();
        assert!(!contains_until(&out));
        assert_eq!(out.len(), 1);
        match &out[0] {
            HirStmt::Trigger(t) => {
                assert_eq!(t.conds.len(), 1);
                assert_eq!(t.acts.len(), 1);
                assert_eq!(t.acts[0], latch_reset(7), "only the reset act survives");
                // The packed cond is the reserved latch's own cond, verbatim.
                assert_eq!(t.conds[0], latch_cond(7));
            }
            other => panic!("expected a packed Trigger, got {other:?}"),
        }
    }

    #[test]
    fn leaf_when_packs_into_single_1t_trigger() {
        let mut acts = Vec::new();
        for i in 0..10 {
            acts.push(HirStmt::Act(HirAct::set_deaths(0, 7, i, 0)));
        }
        let when = HirStmt::When(HirWhen::new(
            vec![HirCond::deaths(0, 10, 42, 0)],
            acts,
        ));
        let module = HirModule {
            funcs: Vec::new(),
            vars: Vec::new(),
            stmts: vec![when],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let mut next_id = 0;
        let opt = optimize_test_stmts(module.stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 1);
        if let HirStmt::Trigger(t) = &opt[0] {
            assert_eq!(t.conds.len(), 1);
            assert_eq!(t.acts.len(), 10);
            assert!(t.preserved);
        } else {
            panic!("Expected HirStmt::Trigger");
        }
    }

    #[test]
    fn leaf_once_packs_into_trigger_with_preserved_false() {
        let once = HirStmt::Once(HirOnce::new(
            vec![HirCond::always()],
            vec![HirStmt::Act(HirAct::set_deaths(0, 7, 100, 0))],
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![once], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1);
        if let HirStmt::Trigger(t) = &opt[0] {
            assert_eq!(t.conds.len(), 1);
            assert_eq!(t.acts.len(), 1);
            assert!(!t.preserved);
            assert_eq!(t.flags, 0);
        } else {
            panic!("Expected HirStmt::Trigger");
        }
    }



    #[test]
    fn nested_when_conjoins_conditions_up_to_16() {
        let inner = HirStmt::When(HirWhen::new(
            vec![HirCond::deaths(1, 10, 20, 0)],
            vec![HirStmt::Act(HirAct::set_deaths(2, 7, 30, 0))],
        ));
        let outer = HirStmt::When(HirWhen::new(
            vec![HirCond::deaths(0, 10, 10, 0)],
            vec![inner],
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![outer], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1);
        if let HirStmt::Trigger(t) = &opt[0] {
            assert_eq!(t.conds.len(), 2);
            assert_eq!(t.acts.len(), 1);
        } else {
            panic!("Expected conjoined HirStmt::Trigger");
        }
    }

    #[test]
    fn consecutive_when_merges_actions_up_to_64() {
        // The gate must be keyed by a paged variable: since ticket #90 an
        // absolute-address gate (stock `Deaths`) does not merge over leaf acts
        // that may write its storage, even when the acts write constants.
        let c = vec![var_read_cond(3)];
        let w1 = HirStmt::When(HirWhen::new(
            c.clone(),
            vec![
                HirStmt::Act(HirAct::set_deaths(1, 7, 10, 0)),
                HirStmt::Act(HirAct::set_deaths(2, 7, 20, 0)),
            ],
        ));
        let w2 = HirStmt::When(HirWhen::new(
            c.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(3, 7, 30, 0))],
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1);
        if let HirStmt::Trigger(t) = &opt[0] {
            assert_eq!(t.conds.len(), 1);
            assert_eq!(t.acts.len(), 3);
        } else {
            panic!("Expected merged HirStmt::Trigger");
        }
    }

    /// Gate `v == 1`, reading variable `v` through its value-field reloc.
    fn var_read_cond(v: usize) -> HirCond {
        let mut c = HirCond::deaths(0, 10, 1, 0);
        c.relocs = vec![HirReloc::var(4, v, 20, 1)];
        c
    }

    /// One leaf act writing variable `v` in its value-field reloc.
    fn var_write_act(v: usize, value: u32) -> HirStmt {
        HirStmt::Act(HirAct::set_var_const(v, 7, value, 0xFFFF_FFFF))
    }

    /// The sibling-`when` leaf-act merge is dependency-guarded: merging makes
    /// the group read its gate once, so an act that writes a variable the gate
    /// reads must keep the siblings apart. This pins the ticket #79 probe
    /// (`when(v==1){v.set(2)}` then `when(v==1){w.set(9)}`): the second body
    /// must not ride along on the first body's stale gate.
    #[test]
    fn leaf_when_merge_rejects_a_body_that_writes_the_gate_var() {
        let gate = vec![var_read_cond(3)];
        // First body falsifies the shared gate; the second writes another var.
        let w1 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(3, 2)]));
        let w2 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(4, 9)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 2, "hazardous siblings must stay unmerged: {opt:?}");
        match (&opt[0], &opt[1]) {
            (HirStmt::Trigger(t1), HirStmt::Trigger(t2)) => {
                assert_eq!(t1.acts.len(), 1);
                assert_eq!(t2.acts.len(), 1);
                assert_eq!(t1.acts[0].relocs[0].target_var, Some(3));
                assert_eq!(t2.acts[0].relocs[0].target_var, Some(4));
            }
            other => panic!("expected two packed Triggers, got {other:?}"),
        }

        // A later body writing the gate variable is rejected too: the guarded
        // group must not depend on which sibling carries the write.
        let w1 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(4, 9)]));
        let w2 = HirStmt::When(HirWhen::new(gate, vec![var_write_act(3, 2)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 2, "later gate-var write must also split: {opt:?}");
    }

    /// The merge-positive case: identical gates whose bodies write only
    /// variables the gate does not read still fuse into one trigger.
    #[test]
    fn leaf_when_merge_keeps_siblings_that_avoid_the_gate_vars() {
        let gate = vec![var_read_cond(3)];
        let w1 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(4, 9)]));
        let w2 = HirStmt::When(HirWhen::new(gate, vec![var_write_act(5, 7)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1, "safe siblings must still merge: {opt:?}");
        match &opt[0] {
            HirStmt::Trigger(t) => {
                assert_eq!(t.conds.len(), 1);
                assert_eq!(t.acts.len(), 2);
            }
            other => panic!("expected one merged Trigger, got {other:?}"),
        }
    }

    /// Ticket #90: an external-state gate (absolute address, no relocs) must
    /// not merge over an earlier leaf act that writes that storage. A body
    /// that only writes paged vars cannot touch it and still merges.
    #[test]
    fn leaf_when_merge_rejects_external_gate_over_an_absolute_write() {
        let gate = vec![HirCond::deaths(0, 10, 1, 0)]; // Memory/Deaths: absolute
        let w1 = HirStmt::When(HirWhen::new(
            gate.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(0, 7, 0, 0))],
        ));
        let w2 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(4, 7)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 2, "external-gate write hazard must split: {opt:?}");

        // Paged-only bodies are safe under the same external gate.
        let w1 = HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(4, 9)]));
        let w2 = HirStmt::When(HirWhen::new(gate, vec![var_write_act(5, 7)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1, "paged-only bodies still merge: {opt:?}");
    }

    /// A flat body accepts only acts, transfers, batches, and stores. A leaf
    /// body is the strict all-`Act` subset; every nested temporal node is the
    /// non-leaf boundary a sibling merge must not cross (#98).
    #[test]
    fn is_flat_body_accepts_only_flat_statements() {
        assert!(is_flat_body(&[]), "an empty body is vacuously flat");
        assert!(is_flat_body(&[var_write_act(4, 9)]));
        assert!(is_leaf_body(&[var_write_act(4, 9)]));
        assert!(is_flat_body(&[var_transfer(4)]));
        assert!(!is_leaf_body(&[var_transfer(4)]));
        assert!(is_flat_body(&[store_act(9, 0x600000)]));
        assert!(is_flat_body(&[
            var_write_act(4, 9),
            var_transfer(5),
            store_act(9, 0x600004),
        ]));
        for nested in [
            HirStmt::When(HirWhen::new(vec![HirCond::always()], vec![])),
            HirStmt::Once(HirOnce::new(vec![HirCond::always()], vec![])),
            HirStmt::While(HirWhile::new(vec![HirCond::always()], vec![])),
        ] {
            assert!(!is_flat_body(&[nested]), "nested control flow is not flat");
        }
    }

    /// The generalized body guard accepts flat statements whose *variable*
    /// destinations avoid the read set: an act names one through a
    /// `relocs[].target_var`, a transfer/batch through `dst_var`, and a store
    /// names none at all (its destination is an absolute cell or trigger
    /// field). Anything non-flat rejects.
    #[test]
    fn is_guarded_flat_body_checks_variable_destinations() {
        let reads = vec![3];
        let const_act = HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0));
        assert!(is_guarded_flat_body(&[const_act], &reads));
        assert!(is_guarded_flat_body(&[var_write_act(4, 9)], &reads));
        assert!(!is_guarded_flat_body(&[var_write_act(3, 9)], &reads));
        assert!(
            !is_guarded_flat_body(&[var_write_act(4, 9), var_write_act(3, 9)], &reads),
            "one act writing a gate variable rejects the run"
        );

        assert!(is_guarded_flat_body(&[var_transfer(4)], &reads));
        assert!(!is_guarded_flat_body(&[var_transfer(3)], &reads));

        let batch = |dsts: &[usize]| {
            HirStmt::VarBatch(HirVarBatch::transfers(
                dsts.iter()
                    .map(|&dst| HirVarTransfer {
                        dst_var: dst,
                        src_var: 9,
                        modifier: 7,
                    })
                    .collect(),
            ))
        };
        assert!(is_guarded_flat_body(&[batch(&[4, 5])], &reads));
        assert!(
            !is_guarded_flat_body(&[batch(&[4, 3])], &reads),
            "one destination in the read set must reject the batch"
        );

        // Stores are read-set neutral: their destination is not a variable.
        assert!(is_guarded_flat_body(&[store_act(9, 0x600000)], &reads));
        assert!(is_guarded_flat_body(&[store_act(3, 0x600004)], &reads));

        assert!(
            is_guarded_flat_body(&[], &reads),
            "an empty body writes nothing"
        );
        let nested = HirStmt::When(HirWhen::new(vec![HirCond::always()], vec![]));
        assert!(
            !is_guarded_flat_body(&[nested], &reads),
            "a non-flat statement never qualifies"
        );
    }

    /// The sibling-`when` transfer guard's read set: every reloc
    /// `target_var`, deduped in first-seen order. Conds without relocs
    /// contribute nothing; a reloc with no target var is ignored.
    #[test]
    fn cond_read_vars_collects_reloc_targets_deduped() {
        let stock = HirCond::deaths(0, 10, 42, 0);
        assert_eq!(cond_read_vars(&[]), Vec::<usize>::new());
        assert_eq!(cond_read_vars(&[stock]), Vec::<usize>::new());

        let mut reads3 = HirCond::deaths(0, 10, 1, 0);
        reads3.relocs = vec![HirReloc::var(4, 3, 20, 1)];
        assert_eq!(cond_read_vars(&[reads3.clone()]), vec![3]);

        let mut reads4 = HirCond::deaths(0, 10, 2, 0);
        reads4.relocs = vec![HirReloc::var(4, 4, 20, 1)];
        assert_eq!(
            cond_read_vars(&[reads3.clone(), reads4, reads3]),
            vec![3, 4],
            "dedup must keep first-seen order"
        );

        let mut no_target = HirReloc::var(4, 9, 20, 1);
        no_target.target_var = None;
        let mut ignored = HirCond::deaths(0, 10, 3, 0);
        ignored.relocs = vec![no_target];
        assert_eq!(cond_read_vars(&[ignored]), Vec::<usize>::new());
    }

    /// `cond_keyed_by_paged_var` accepts only gates that read paged-variable
    /// storage: a field-4 `player` reloc with a target var (plain or masked
    /// Deaths), or a condition that reads no storage at all (Always/Never).
    /// An absolute-address gate (Memory/Deaths/Switch) has no relocs and is
    /// rejected, which is the hole ticket #90 closes.
    #[test]
    fn cond_keyed_by_paged_var_distinguishes_external_gates() {
        let mut plain = HirCond::deaths(0, 10, 1, 0);
        plain.relocs = vec![HirReloc::var(4, 3, 20, 1)];
        assert!(cond_keyed_by_paged_var(&plain));

        // A masked read keeps locid/eudx; the field-4 reloc still decides.
        let mut masked = HirCond::deathsx(0, 10, 1, 0, 0xFF);
        masked.relocs = vec![HirReloc::var(4, 3, 20, 1)];
        assert!(
            cond_keyed_by_paged_var(&masked),
            "a masked var read must not be over-tightened"
        );

        // Absolute-address gates: Deaths/Memory (condtype 15, no relocs) and
        // Switch (condtype 11).
        assert!(!cond_keyed_by_paged_var(&HirCond::deaths(0, 10, 1, 0)));
        let mut switch = HirCond::deaths(0, 10, 1, 0);
        switch.condtype = 11;
        assert!(!cond_keyed_by_paged_var(&switch));

        // A reloc outside the player field does not key the gate.
        let mut wrong_field = HirCond::deaths(0, 10, 1, 0);
        wrong_field.relocs = vec![HirReloc::var(8, 3, 20, 1)];
        assert!(!cond_keyed_by_paged_var(&wrong_field));

        // Always/Never read no storage at all.
        assert!(cond_keyed_by_paged_var(&HirCond::always()));
        assert!(cond_keyed_by_paged_var(&HirCond::never()));
    }

    /// `act_writes_paged_var` keys on a var reloc at the action's `dest`
    /// field; a value-field reloc reads a variable without writing one.
    #[test]
    fn act_writes_paged_var_checks_dest_reloc() {
        assert!(act_writes_paged_var(&HirAct::set_var_const(
            3,
            7,
            1,
            0xFFFF_FFFF
        )));
        assert!(!act_writes_paged_var(&HirAct::set_deaths(0, 7, 1, 0)));
        let mut value_only = HirAct::set_deaths(0, 7, 1, 0);
        value_only.relocs = vec![HirReloc::var(20, 3, 20, 1)];
        assert!(
            !act_writes_paged_var(&value_only),
            "a value-field reloc is not a write"
        );
    }

    /// `body_writes_only_paged_vars` accepts a non-empty run in which every
    /// statement writes paged-variable storage: acts with a dest reloc, any
    /// transfer, and store-free batches. An empty body, a `VarStore`, an
    /// absolute write, or any nested node rejects.
    #[test]
    fn body_writes_only_paged_vars_requires_nonempty_all_var_writes() {
        assert!(body_writes_only_paged_vars(&[var_write_act(4, 9)]));
        assert!(body_writes_only_paged_vars(&[
            var_write_act(4, 9),
            var_write_act(5, 7)
        ]));
        assert!(!body_writes_only_paged_vars(&[]), "an empty body is rejected");
        assert!(!body_writes_only_paged_vars(&[HirStmt::Act(
            HirAct::set_deaths(0, 7, 1, 0)
        )]));
        assert!(!body_writes_only_paged_vars(&[
            var_write_act(4, 9),
            HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))
        ]));
        assert!(
            body_writes_only_paged_vars(&[var_transfer(4)]),
            "a transfer writes a paged variable by definition"
        );
        assert!(body_writes_only_paged_vars(&[HirStmt::VarBatch(
            HirVarBatch::transfers(vec![HirVarTransfer {
                dst_var: 4,
                src_var: 9,
                modifier: 7,
            }])
        )]));
        assert!(
            !body_writes_only_paged_vars(&[store_act(9, 0x600000)]),
            "a store writes an absolute cell, not a paged variable"
        );
        assert!(
            !body_writes_only_paged_vars(&[HirStmt::VarBatch(HirVarBatch {
                transfers: vec![HirVarTransfer {
                    dst_var: 4,
                    src_var: 9,
                    modifier: 7,
                }],
                stores: vec![HirVarStore::to_addr(9, 0x600000, 7)],
            })]),
            "a batch carrying a store is not paged-only"
        );
    }

    /// The `StorageKey` disjointness table (#112): equal keys within a class
    /// alias, different keys within a class are disjoint, `Var` is disjoint
    /// from every external class (the "an absolute address cannot name paged
    /// storage" invariant), `Epd` vs `Switch` is deliberately *not* disjoint
    /// (switch state is not modelled as an address), and `Unknown` anywhere
    /// falls back to today's answer.
    #[test]
    fn storage_key_disjointness_covers_every_class_pair() {
        assert!(storage_keys_disjoint(
            &StorageKey::Epd(1),
            &StorageKey::Epd(2)
        ));
        assert!(!storage_keys_disjoint(
            &StorageKey::Epd(1),
            &StorageKey::Epd(1)
        ));
        assert!(storage_keys_disjoint(
            &StorageKey::Switch(1),
            &StorageKey::Switch(2)
        ));
        assert!(!storage_keys_disjoint(
            &StorageKey::Switch(1),
            &StorageKey::Switch(1)
        ));
        assert!(storage_keys_disjoint(
            &StorageKey::Var(1),
            &StorageKey::Var(2)
        ));
        assert!(!storage_keys_disjoint(
            &StorageKey::Var(1),
            &StorageKey::Var(1)
        ));
        for external in [StorageKey::Epd(1), StorageKey::Switch(1)] {
            assert!(storage_keys_disjoint(&StorageKey::Var(1), &external));
            assert!(storage_keys_disjoint(&external, &StorageKey::Var(1)));
        }
        assert!(
            !storage_keys_disjoint(&StorageKey::Epd(1), &StorageKey::Switch(1)),
            "switch state is not modelled as an address: Epd vs Switch is unprovable"
        );
        assert!(!storage_keys_disjoint(
            &StorageKey::Switch(1),
            &StorageKey::Epd(1)
        ));
        for key in [
            StorageKey::Var(1),
            StorageKey::Epd(1),
            StorageKey::Switch(1),
        ] {
            assert!(!storage_keys_disjoint(&StorageKey::Unknown, &key));
            assert!(!storage_keys_disjoint(&key, &StorageKey::Unknown));
        }
    }

    /// Gate keys: a plain condtype-15 read names `Epd(player)`, a `CurrentPlayer`
    /// base and every non-const upstream input are unknown, a field-4 var reloc
    /// names the paged variable, and `Switch` names the switch id.
    #[test]
    fn external_gate_keys_name_cond_storage() {
        let flag = 0x57F0_B4;
        assert_eq!(
            external_gate_keys(&[HirCond::deaths(eudkit_lir::epd_u32(flag), 10, 1, 0)]),
            Some(vec![StorageKey::Epd(eudkit_lir::epd_u32(flag))]),
            "Memory(FLAG).player is the raw EPD dword"
        );
        assert_eq!(
            external_gate_keys(&[HirCond::always()]),
            Some(Vec::new()),
            "Always reads no storage"
        );

        let mut current_player = HirCond::deaths(13, 10, 1, 0);
        current_player.condtype = 15;
        assert_eq!(
            external_gate_keys(&[current_player]),
            None,
            "CurrentPlayer is a dynamic base"
        );

        let mut amount_reloc = HirCond::deaths(4, 10, 1, 0);
        amount_reloc.relocs = vec![HirReloc::var(8, 3, 20, 1)];
        assert_eq!(
            external_gate_keys(&[amount_reloc]),
            None,
            "a comparison-input reloc has non-constant provenance"
        );

        let mut other = HirCond::deaths(4, 10, 1, 0);
        other.condtype = 2;
        assert_eq!(external_gate_keys(&[other]), None);

        let mut var_gate = HirCond::deaths(0, 10, 1, 0);
        var_gate.relocs = vec![HirReloc::var(4, 7, 20, 1)];
        assert_eq!(
            external_gate_keys(&[var_gate]),
            Some(vec![StorageKey::Var(7)])
        );
        for reloc in [
            HirReloc::blob(4, 0, 0, 1),
            HirReloc::trigger(4, 1, 0, 1),
            HirReloc::string(4, 0, 0, 1),
        ] {
            let mut cond = HirCond::deaths(0, 10, 1, 0);
            cond.relocs = vec![reloc];
            assert_eq!(external_gate_keys(&[cond]), None);
        }

        let mut switch = HirCond::deaths(0, 10, 1, 0);
        switch.condtype = 11;
        switch.restype = 5;
        assert_eq!(
            external_gate_keys(&[switch]),
            Some(vec![StorageKey::Switch(5)])
        );
    }

    /// Act write keys: a `SetDeaths`-family write names `Epd(dest)`, a dynamic
    /// `CurrentPlayer` dest and every other act type are unknown, a dest-field
    /// var reloc names the paged variable, and a dest string/blob/trigger reloc
    /// is unprovable.
    #[test]
    fn external_body_keys_name_act_storage() {
        let table = 0x60_0000;
        assert_eq!(
            external_body_keys(&[HirStmt::Act(HirAct::set_deaths(
                eudkit_lir::epd_u32(table),
                7,
                1,
                0
            ))]),
            Some(vec![StorageKey::Epd(eudkit_lir::epd_u32(table))])
        );
        assert_eq!(
            external_body_keys(&[HirStmt::Act(HirAct::set_deaths(13, 7, 1, 0))]),
            None,
            "dest == CurrentPlayer is a dynamic base"
        );
        let mut other_act = HirAct::set_deaths(4, 7, 1, 0);
        other_act.acttype = 9;
        assert_eq!(
            external_body_keys(&[HirStmt::Act(other_act)]),
            None,
            "a non-SetDeaths act has no modelled write key"
        );

        assert_eq!(
            external_body_keys(&[var_write_act(4, 9)]),
            Some(vec![StorageKey::Var(4)])
        );
        for reloc in [
            HirReloc::string(16, 0, 0, 1),
            HirReloc::blob(16, 0, 0, 1),
            HirReloc::trigger(16, 1, 0, 1),
        ] {
            let mut act = HirAct::set_deaths(0, 7, 1, 0);
            act.relocs = vec![reloc];
            assert_eq!(external_body_keys(&[HirStmt::Act(act)]), None);
        }

        let mut switch = HirAct::set_deaths(0, 7, 0, 0);
        switch.acttype = 13;
        switch.value = 5;
        assert_eq!(
            external_body_keys(&[HirStmt::Act(switch)]),
            Some(vec![StorageKey::Switch(5)])
        );

        // SetSwitch with a value-field reloc patches the switch id at runtime,
        // so the static `value` is a placeholder, not a provable key.
        let mut patched_switch = HirAct::set_deaths(0, 7, 0, 0);
        patched_switch.acttype = 13;
        patched_switch.value = 5;
        patched_switch.relocs = vec![HirReloc::var(VALUE_FIELD_OFFSET, 9, 20, 4)];
        assert_eq!(
            external_body_keys(&[HirStmt::Act(patched_switch)]),
            None,
            "a value-relocated SetSwitch must not fabricate a switch id"
        );
    }

    /// Store write keys: an aligned absolute `dst_addr` names the same EPD the
    /// lowerer emits, while an unaligned address (raw HIR only after #111) and
    /// a trigger-field destination are unprovable.
    #[test]
    fn external_body_keys_name_store_storage() {
        let table = 0x60_0000;
        assert_eq!(
            external_body_keys(&[store_act(9, table)]),
            Some(vec![StorageKey::Epd(eudkit_lir::epd_u32(table))])
        );
        assert_eq!(
            external_body_keys(&[store_act(9, table + 2)]),
            None,
            "an unaligned dword address cannot name a comparable cell"
        );
        assert_eq!(
            external_body_keys(&[HirStmt::VarStore(HirVarStore::to_trigger(9, 1, 20, 7))]),
            None
        );
        assert_eq!(
            external_body_keys(&[var_transfer(4)]),
            Some(vec![StorageKey::Var(4)])
        );
        assert_eq!(
            external_body_keys(&[HirStmt::VarBatch(HirVarBatch {
                transfers: vec![HirVarTransfer {
                    dst_var: 4,
                    src_var: 9,
                    modifier: 7,
                }],
                stores: vec![HirVarStore::to_addr(9, table, 7)],
            })]),
            Some(vec![
                StorageKey::Var(4),
                StorageKey::Epd(eudkit_lir::epd_u32(table)),
            ])
        );
        assert_eq!(
            external_body_keys(&[
                store_act(9, table),
                HirStmt::VarStore(HirVarStore::to_trigger(9, 1, 20, 7)),
            ]),
            None,
            "one unknown write poisons the accumulated body"
        );
    }

    /// #112's positive case: an external gate over an act writing a *different*
    /// dword merges, while the existing same-cell pin above still splits.
    #[test]
    fn leaf_when_merge_admits_external_gate_over_a_distinct_address() {
        let gate = vec![HirCond::deaths(0, 10, 1, 0)];
        let w1 = HirStmt::When(HirWhen::new(
            gate.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(4, 7, 0, 0))],
        ));
        let w2 = HirStmt::When(HirWhen::new(gate, vec![var_write_act(4, 7)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2], &mut next_id).unwrap();
        assert_eq!(
            opt.len(),
            1,
            "a distinct external dword must admit the merge: {opt:?}"
        );
        match &opt[0] {
            HirStmt::Trigger(t) => {
                assert_eq!(t.conds.len(), 1);
                assert_eq!(t.acts.len(), 2);
            }
            other => panic!("expected one merged Trigger, got {other:?}"),
        }

        // The accumulated body is re-checked: after a distinct write and a
        // gate-cell write have merged, the third sibling must not ride along.
        let gate = vec![HirCond::deaths(0, 10, 1, 0)];
        let w1 = HirStmt::When(HirWhen::new(
            gate.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(4, 7, 0, 0))],
        ));
        let w2 = HirStmt::When(HirWhen::new(
            gate.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(0, 7, 0, 0))],
        ));
        let w3 = HirStmt::When(HirWhen::new(gate, vec![var_write_act(4, 9)]));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![w1, w2, w3], &mut next_id).unwrap();
        assert_eq!(
            opt.len(),
            2,
            "the third sibling must re-check the accumulated gate-cell write: {opt:?}"
        );
    }

    /// A leaf act that *reads* `var` through its value-field reloc and writes
    /// the absolute dword `dest`.
    fn var_read_act(var: usize, dest: u32) -> HirAct {
        let mut act = HirAct::set_deaths(dest, 7, 0, 0);
        act.relocs = vec![HirReloc::var(VALUE_FIELD_OFFSET, var, 20, 4)];
        act
    }

    /// Whether any trigger with `id` survives anywhere in the tree.
    fn has_trigger_id(stmts: &[HirStmt], id: usize) -> bool {
        stmts.iter().any(|stmt| match stmt {
            HirStmt::Trigger(trig) => trig.id == id,
            HirStmt::When(w) => {
                has_trigger_id(&w.body, id) || has_trigger_id(&w.else_body, id)
            }
            HirStmt::Once(o) => has_trigger_id(&o.body, id),
            HirStmt::While(w) => has_trigger_id(&w.body, id),
            HirStmt::Until(u) => has_trigger_id(&u.body, id),
            HirStmt::Edge(e) => has_trigger_id(&e.body, id),
            HirStmt::Every(e) => has_trigger_id(&e.body, id),
            HirStmt::After(a) => has_trigger_id(&a.body, id),
            _ => false,
        })
    }

    /// P1: a run trigger whose id an external relocation names must not be
    /// unpacked by the backward merge; unpacking would delete the only
    /// definition of that id and lowering would reject the module.
    #[test]
    fn keeps_a_referenced_trigger_inside_a_merged_run() {
        let gate = vec![var_read_cond(7)];
        let run = HirStmt::Trigger(
            HirTrigger::new(
                7,
                Vec::new(),
                vec![HirAct::set_deaths(9000, 7, 5, 0)],
                true,
            )
            .unwrap(),
        );
        let mut reference = HirAct::set_deaths(9000, 7, 1, 0);
        reference
            .relocs
            .push(HirReloc::trigger(VALUE_FIELD_OFFSET, 7, 0, 4));
        let stmts = vec![
            HirStmt::When(HirWhen::new(gate.clone(), vec![var_write_act(1, 9)])),
            run,
            HirStmt::When(HirWhen::new(gate, vec![var_write_act(2, 4)])),
            HirStmt::Act(reference),
        ];
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert!(
            has_trigger_id(&opt, 7),
            "a referenced run trigger must survive as a Trigger: {opt:?}"
        );
    }

    /// `when(gate){ first }; run...; when(gate){ second }` — the exact
    /// backward-merge shape (cycle 3 #02).
    fn backward_merge_case(
        gate: Vec<HirCond>,
        run: Vec<HirStmt>,
        first: Vec<HirStmt>,
        second: Vec<HirStmt>,
    ) -> Vec<HirStmt> {
        let mut stmts = vec![HirStmt::When(HirWhen::new(gate.clone(), first))];
        stmts.extend(run);
        stmts.push(HirStmt::When(HirWhen::new(gate, second)));
        stmts
    }

    /// Cycle 3 #02: `when(G){A}; X; when(G){B}` becomes `X; when(G){A;B}`,
    /// keeping the moved act ahead of the merged group and the body order
    /// `A;B` inside it.
    #[test]
    fn backward_merge_moves_a_bare_act_run_before_the_gate() {
        let gate = vec![var_read_cond(7)];
        let run = vec![HirStmt::Act(HirAct::set_deaths(9000, 7, 5, 0))];
        let stmts = backward_merge_case(
            gate,
            run.clone(),
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 2, "run then one merged gate: {opt:?}");
        assert_eq!(opt[0], run[0], "the run must move ahead of the gate");
        match &opt[1] {
            HirStmt::Trigger(t) => {
                assert_eq!(t.conds.len(), 1);
                assert_eq!(t.acts.len(), 2);
                assert_eq!(t.acts[0].relocs[0].target_var, Some(1));
                assert_eq!(t.acts[1].relocs[0].target_var, Some(2));
            }
            other => panic!("expected one merged Trigger, got {other:?}"),
        }
    }

    /// A moved act that writes the paged gate variable changes the second
    /// evaluation, which the merge drops. Reject.
    #[test]
    fn backward_merge_rejects_an_act_that_writes_the_gate_var() {
        let gate = vec![var_read_cond(7)];
        let stmts = backward_merge_case(
            gate,
            vec![var_write_act(7, 3)],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "gate-var write must keep siblings apart: {opt:?}");
    }

    /// RAW: the earlier body reads a variable the moved act writes.
    #[test]
    fn backward_merge_rejects_a_body_read_the_act_writes() {
        let gate = vec![var_read_cond(7)];
        let stmts = backward_merge_case(
            gate,
            vec![var_write_act(1, 5)],
            vec![HirStmt::Act(var_read_act(1, 9000))],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "moved write changes the earlier read: {opt:?}");
    }

    /// WAR: the earlier body writes a variable the moved act reads.
    #[test]
    fn backward_merge_rejects_an_act_read_the_body_writes() {
        let gate = vec![var_read_cond(7)];
        let stmts = backward_merge_case(
            gate,
            vec![HirStmt::Act(var_read_act(1, 9000))],
            vec![var_write_act(1, 5)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "moved read sees the pre-body value: {opt:?}");
    }

    /// A conditional intervening trigger may not run on every path; moving it
    /// before the gate would change when its condition is evaluated.
    #[test]
    fn backward_merge_rejects_a_conditional_trigger_barrier() {
        let gate = vec![var_read_cond(7)];
        let barrier = HirStmt::Trigger(
            HirTrigger::new(
                10,
                vec![HirCond::deaths(0, 10, 1, 0)],
                vec![HirAct::set_deaths(9000, 7, 5, 0)],
                true,
            )
            .unwrap(),
        );
        let stmts = backward_merge_case(
            gate,
            vec![barrier],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "a conditional trigger is a barrier: {opt:?}");
    }

    /// A store is not an unconditional bare act; reordering it over the gate
    /// needs the scheduler's batch proof, not the leaf-act one.
    #[test]
    fn backward_merge_rejects_a_var_store_barrier() {
        let gate = vec![var_read_cond(7)];
        let stmts = backward_merge_case(
            gate,
            vec![store_act(9, 0x600000)],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "a VarStore is a barrier: {opt:?}");
    }

    /// Anything the key-aware proof cannot model keeps today's unmerged shape:
    /// a non-SetDeaths act, and a dest/value reloc through a trigger, string,
    /// or blob.
    #[test]
    fn backward_merge_rejects_an_unmodelled_act() {
        let gate = vec![var_read_cond(7)];
        let mut non_set_deaths = HirAct::set_deaths(9000, 7, 5, 0);
        non_set_deaths.acttype = 9;
        let mut trigger_reloc = HirAct::set_deaths(0, 7, 0, 0);
        trigger_reloc.relocs = vec![HirReloc::trigger(DEST_FIELD_OFFSET, 1, 0, 1)];
        let mut string_reloc = HirAct::set_deaths(0, 7, 0, 0);
        string_reloc.relocs = vec![HirReloc::string(DEST_FIELD_OFFSET, 0, 0, 1)];
        let mut blob_reloc = HirAct::set_deaths(0, 7, 0, 0);
        blob_reloc.relocs = vec![HirReloc::blob(DEST_FIELD_OFFSET, 0, 0, 1)];
        // A value-field reloc with no target var is an unmodelled absolute
        // read. (A string/blob/trigger target would be rejected earlier by the
        // target-kind barrier, so this case must stay target-less to exercise
        // the `field != dest && target_var.is_none()` branch in `modelled_act`.)
        let mut absolute_value_read = HirAct::set_deaths(9000, 7, 5, 0);
        let mut targetless_value_reloc = HirReloc::var(VALUE_FIELD_OFFSET, 0, 0, 4);
        targetless_value_reloc.target_var = None;
        absolute_value_read.relocs = vec![targetless_value_reloc];

        for (label, act) in [
            ("non-SetDeaths", non_set_deaths),
            ("trigger reloc", trigger_reloc),
            ("string reloc", string_reloc),
            ("blob reloc", blob_reloc),
            ("absolute value read", absolute_value_read),
        ] {
            let stmts = backward_merge_case(
                gate.clone(),
                vec![HirStmt::Act(act)],
                vec![var_write_act(1, 9)],
                vec![var_write_act(2, 4)],
            );
            let mut next_id = 0;
            let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
            assert_eq!(opt.len(), 3, "{label} must be unmodelled: {opt:?}");
        }
    }

    /// An unconditional flag-`4` block is a bare act run, but one carrying a
    /// chain pointer is an opaque reference and a barrier.
    #[test]
    fn backward_merge_rejects_a_trigger_with_chain_pointers() {
        let gate = vec![var_read_cond(7)];
        let mut barrier = HirTrigger::new(
            10,
            vec![],
            vec![HirAct::set_deaths(9000, 7, 5, 0)],
            true,
        )
        .unwrap();
        barrier.prev = Some(HirTriggerPtr::Const { value: 0 });
        let stmts = backward_merge_case(
            gate,
            vec![HirStmt::Trigger(barrier)],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "chain pointers are not a bare act run: {opt:?}");
    }

    /// Ticket scope: the backward path is `when` only. A `once` self-disables,
    /// so moving acts across it needs its own cadence proof.
    #[test]
    fn backward_merge_does_not_apply_to_once() {
        let gate = vec![var_read_cond(7)];
        let stmts = vec![
            HirStmt::Once(HirOnce::new(gate.clone(), vec![var_write_act(1, 9)])),
            HirStmt::Act(HirAct::set_deaths(9000, 7, 5, 0)),
            HirStmt::Once(HirOnce::new(gate, vec![var_write_act(2, 4)])),
        ];
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "`once` stays adjacent-only: {opt:?}");
    }

    /// The moved act's write key must be disjoint from every gate key: a
    /// different external dword merges, the gate's own cell splits.
    #[test]
    fn backward_merge_keeps_a_disjoint_external_write() {
        let flag = 0x57F0B4;
        let flag_epd = eudkit_lir::epd_u32(flag);
        let gate = vec![HirCond::deaths(flag_epd, 10, 1, 0)];

        let merged = backward_merge_case(
            gate.clone(),
            vec![HirStmt::Act(HirAct::set_deaths(
                eudkit_lir::epd_u32(flag + 4),
                7,
                5,
                0,
            ))],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(merged, &mut next_id).unwrap();
        assert_eq!(opt.len(), 2, "a disjoint external write must merge: {opt:?}");

        let split = backward_merge_case(
            gate,
            vec![HirStmt::Act(HirAct::set_deaths(flag_epd, 7, 5, 0))],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(split, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "the gate's own cell must split: {opt:?}");
    }

    /// The backward path recurses into nested bodies like the adjacent one.
    #[test]
    fn backward_merge_inside_a_when_body() {
        let gate = vec![var_read_cond(7)];
        let inner = backward_merge_case(
            gate,
            vec![HirStmt::Act(HirAct::set_deaths(9000, 7, 5, 0))],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let outer = HirStmt::When(HirWhen::new(vec![HirCond::always()], inner));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![outer], &mut next_id).unwrap();
        assert_eq!(opt.len(), 1);
        match &opt[0] {
            HirStmt::When(w) => {
                assert_eq!(w.body.len(), 2, "inner merge must run in place: {w:?}");
                assert!(matches!(w.body[0], HirStmt::Act(_)));
                match &w.body[1] {
                    HirStmt::Trigger(t) => assert_eq!(t.acts.len(), 2),
                    other => panic!("expected a merged inner Trigger, got {other:?}"),
                }
            }
            other => panic!("expected the outer When, got {other:?}"),
        }
    }

    /// Two moved acts keep their relative order ahead of the merged gate.
    #[test]
    fn backward_merge_preserves_run_order() {
        let gate = vec![var_read_cond(7)];
        let stmts = backward_merge_case(
            gate,
            vec![
                HirStmt::Act(HirAct::set_deaths(9000, 7, 5, 0)),
                HirStmt::Act(HirAct::set_deaths(9004, 7, 6, 0)),
            ],
            vec![var_write_act(1, 9)],
            vec![var_write_act(2, 4)],
        );
        let mut next_id = 0;
        let opt = optimize_test_stmts(stmts, &mut next_id).unwrap();
        assert_eq!(opt.len(), 3, "two run acts then the merged gate: {opt:?}");
        assert_eq!(
            opt[0],
            HirStmt::Act(HirAct::set_deaths(9000, 7, 5, 0))
        );
        assert_eq!(
            opt[1],
            HirStmt::Act(HirAct::set_deaths(9004, 7, 6, 0))
        );
        match &opt[2] {
            HirStmt::Trigger(t) => {
                assert_eq!(t.acts[0].relocs[0].target_var, Some(1));
                assert_eq!(t.acts[1].relocs[0].target_var, Some(2));
            }
            other => panic!("expected the merged gate last, got {other:?}"),
        }
    }

    fn var_transfer(dst: usize) -> HirStmt {
        HirStmt::VarTransfer(HirVarTransfer {
            dst_var: dst,
            src_var: 9,
            modifier: 7,
        })
    }

    /// A `VarStore` writing to an absolute address: its source variable's
    /// value is read at the store, its destination names no paged variable.
    fn store_act(src: usize, addr: u32) -> HirStmt {
        HirStmt::VarStore(HirVarStore::to_addr(src, addr, 7))
    }

    // C11-03: edge desugar tests (TDD red first: no desugar exists yet).
    fn edge_body_act() -> HirStmt {
        HirStmt::Act(HirAct::set_deaths(1, 7, 99, 0))
    }

    /// The exact 5-statement shape today's DSL desugar emits for one edge
    /// node: reset, sample, fire (nested), update-prev-to-1, update-prev-to-0.
    /// Rising fires on curr==1 && prev==0; falling mirrors both polarities.
    fn expected_edge_expansion(
        direction: HirEdgeDirection,
        curr: usize,
        prev: usize,
    ) -> Vec<HirStmt> {
        let cond = HirCond::deaths(0, 10, 5, 0);
        let (fire_curr, fire_prev) = match direction {
            HirEdgeDirection::Rising => (1, 0),
            HirEdgeDirection::Falling => (0, 1),
        };
        vec![
            HirStmt::Act(HirAct::set_var_const(curr, 7, 0, 0xFFFF_FFFF)),
            HirStmt::When(HirWhen::new(
                vec![cond],
                vec![HirStmt::Act(HirAct::set_var_const(curr, 7, 1, 0xFFFF_FFFF))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, fire_curr)],
                vec![HirStmt::When(HirWhen::new(
                    vec![HirCond::var_deaths(prev, 10, fire_prev)],
                    vec![edge_body_act()],
                ))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, 1)],
                vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 1, 0xFFFF_FFFF))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, 0)],
                vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 0, 0xFFFF_FFFF))],
            )),
        ]
    }

    #[test]
    fn edge_desugar_rising_matches_baseline_shape() {
        let edge = HirEdge::new(
            HirEdgeDirection::Rising,
            vec![HirCond::deaths(0, 10, 5, 0)],
            vec![edge_body_act()],
            2,
            3,
        );
        assert_eq!(
            desugar_edge(&edge),
            expected_edge_expansion(HirEdgeDirection::Rising, 2, 3)
        );
    }

    #[test]
    fn edge_desugar_falling_shares_logic_except_polarity() {
        let edge = HirEdge::new(
            HirEdgeDirection::Falling,
            vec![HirCond::deaths(0, 10, 5, 0)],
            vec![edge_body_act()],
            4,
            5,
        );
        assert_eq!(
            desugar_edge(&edge),
            expected_edge_expansion(HirEdgeDirection::Falling, 4, 5)
        );
    }

    #[test]
    fn edge_through_optimize_stmts_matches_baseline_whens() {
        // The desugar path must be indistinguishable from today's
        // DSL-emitted whens after the full opt pipeline (flatten, fuse,
        // merge, pack): same statements, same trigger-id consumption.
        for direction in [HirEdgeDirection::Rising, HirEdgeDirection::Falling] {
            let cond = HirCond::deaths(0, 10, 5, 0);
            let edge = HirStmt::Edge(HirEdge::new(
                direction,
                vec![cond],
                vec![edge_body_act()],
                2,
                3,
            ));
            let baseline = expected_edge_expansion(direction, 2, 3);
            let mut next_id = 0;
            let via_edge = optimize_test_stmts(vec![edge], &mut next_id).unwrap();
            let mut next_id = 0;
            let via_baseline = optimize_test_stmts(baseline, &mut next_id).unwrap();
            assert_eq!(via_edge, via_baseline, "direction {direction:?} diverged");
        }
    }

    #[test]
    fn edge_desugar_recurses_into_nesting() {
        // Edge inside edge: no Edge node survives, and inner/outer state
        // vars stay distinct.
        let inner = HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Rising,
            vec![HirCond::deaths(0, 10, 5, 0)],
            vec![edge_body_act()],
            4,
            5,
        ));
        let outer = HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Falling,
            vec![HirCond::deaths(0, 10, 6, 0)],
            vec![inner],
            2,
            3,
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![outer], &mut next_id).unwrap();
        fn contains_edge(stmts: &[HirStmt]) -> bool {
            stmts.iter().any(|s| match s {
                HirStmt::Edge(_) => true,
                HirStmt::When(w) => contains_edge(&w.body) || contains_edge(&w.else_body),
                HirStmt::Once(o) => contains_edge(&o.body),
                HirStmt::While(w) => contains_edge(&w.body),
                _ => false,
            })
        }
        assert!(!contains_edge(&opt), "no Edge node may survive desugar");
        let mut targets = std::collections::HashSet::new();
        fn collect_cond_vars(stmts: &[HirStmt], out: &mut std::collections::HashSet<usize>) {
            for s in stmts {
                match s {
                    HirStmt::When(w) => {
                        for c in &w.conds {
                            for r in &c.relocs {
                                if let Some(v) = r.target_var {
                                    out.insert(v);
                                }
                            }
                        }
                        collect_cond_vars(&w.body, out);
                    }
                    HirStmt::Trigger(t) => {
                        for c in &t.conds {
                            for r in &c.relocs {
                                if let Some(v) = r.target_var {
                                    out.insert(v);
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        collect_cond_vars(&opt, &mut targets);
        for v in [2, 3, 4, 5] {
            assert!(targets.contains(&v), "state var {v} missing from output");
        }
    }

    #[test]
    fn condition_cse_and_comparison_fusion() {
        // Deaths(0, AtLeast, 5) && Deaths(0, AtLeast, 10) -> Deaths(0, AtLeast, 10)
        let c1 = HirCond::deaths(0, 0, 5, 0);
        let c2 = HirCond::deaths(0, 0, 10, 0);
        let fused1 = fuse_and_dedup_conditions(&[c1, c2]);
        assert_eq!(fused1.len(), 1);
        assert_eq!(fused1[0].comparison, 0);
        assert_eq!(fused1[0].amount, 10);

        // Deaths(0, AtLeast, 5) && Deaths(0, AtMost, 5) -> Exactly(5)
        let c3 = HirCond::deaths(0, 0, 5, 0);
        let c4 = HirCond::deaths(0, 1, 5, 0);
        let fused2 = fuse_and_dedup_conditions(&[c3, c4]);
        assert_eq!(fused2.len(), 1);
        assert_eq!(fused2[0].comparison, 10);
        assert_eq!(fused2[0].amount, 5);
    }

    /// C11-04: `Every` desugars to an unconditional counter increment
    /// plus a fire `when` (packed into a leaf `Trigger` by step 4),
    /// using the node's DSL-allocated `counter_var` verbatim in every
    /// act and cond.
    #[test]
    fn every_desugars_to_iadd_plus_fire_when() {
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 0;
        let opt = optimize_test_stmts(
            vec![HirStmt::Every(HirEvery::new(3, 2, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 2, "desugar must yield [iadd, fire trigger]");
        assert_eq!(
            opt[0],
            HirStmt::Act(HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF))
        );
        if let HirStmt::Trigger(t) = &opt[1] {
            assert_eq!(t.conds, vec![HirCond::var_deaths(2, 0, 3)]);
            assert_eq!(
                t.acts,
                vec![
                    HirAct::set_var_const(2, 7, 0, 0xFFFF_FFFF),
                    HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF),
                ]
            );
            assert!(t.preserved);
        } else {
            panic!(
                "expected the fire when packed into a Trigger, got {:?}",
                opt[1]
            );
        }
    }

    /// C11-04: `After` desugars to exactly the 3-`when`
    /// arm/decrement/fire sequence, each leaf `when` packed into a
    /// `Trigger`, using the node's `counter_var` verbatim.
    #[test]
    fn after_desugars_to_three_whens() {
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 0;
        let opt = optimize_test_stmts(
            vec![HirStmt::After(HirAfter::new(2, 1, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 3, "desugar must yield three triggers");
        let expect = [
            (
                HirCond::var_deaths(1, 10, 1),
                vec![
                    HirAct::set_var_const(1, 7, 0, 0xFFFF_FFFF),
                    HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF),
                ],
            ),
            (
                HirCond::var_deaths(1, 0, 2),
                vec![HirAct::set_var_const(1, 9, 1, 0xFFFF_FFFF)],
            ),
            (
                HirCond::var_deaths(1, 10, 0),
                vec![HirAct::set_var_const(1, 7, 2, 0xFFFF_FFFF)],
            ),
        ];
        for (i, (cond, acts)) in expect.iter().enumerate() {
            if let HirStmt::Trigger(t) = &opt[i] {
                assert_eq!(&t.conds, &vec![cond.clone()]);
                assert_eq!(&t.acts, acts);
                assert!(t.preserved);
            } else {
                panic!("expected a Trigger at {i}, got {:?}", opt[i]);
            }
        }
    }

    /// C11-04: a leaf `when` inside an `every` body is packed during
    /// body recursion, so the fire `when` stays a non-leaf `When` (its
    /// body starts with the counter reset, then the packed trigger).
    #[test]
    fn counted_desugar_optimizes_body_before_wrapping() {
        let leaf = HirStmt::When(HirWhen::new(
            vec![HirCond::always()],
            vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))],
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(
            vec![HirStmt::Every(HirEvery::new(3, 2, vec![leaf]))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 2);
        assert_eq!(
            opt[0],
            HirStmt::Act(HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF))
        );
        if let HirStmt::When(w) = &opt[1] {
            assert_eq!(w.conds, vec![HirCond::var_deaths(2, 0, 3)]);
            assert_eq!(w.body.len(), 2);
            assert_eq!(
                w.body[0],
                HirStmt::Act(HirAct::set_var_const(2, 7, 0, 0xFFFF_FFFF))
            );
            assert!(matches!(w.body[1], HirStmt::Trigger(_)));
        } else {
            panic!("expected a non-leaf fire When, got {:?}", opt[1]);
        }
    }

    /// C11-04: counted nodes nested inside `when` / `once` / `While`
    /// desugar via recursion — no `Every`/`After` node survives
    /// optimization at any depth.
    #[test]
    fn counted_nodes_nested_in_control_desugar() {
        fn has_counted(stmts: &[HirStmt]) -> bool {
            stmts.iter().any(|s| match s {
                HirStmt::Every(_) | HirStmt::After(_) => true,
                HirStmt::When(w) => has_counted(&w.body) || has_counted(&w.else_body),
                HirStmt::Once(o) => has_counted(&o.body),
                HirStmt::While(w) => has_counted(&w.body),
                _ => false,
            })
        }
        let leaf = || HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF));
        let nodes = vec![
            HirStmt::When(HirWhen::new(
                vec![HirCond::always()],
                vec![HirStmt::Every(HirEvery::new(2, 2, vec![leaf()]))],
            )),
            HirStmt::Once(HirOnce::new(
                vec![HirCond::always()],
                vec![HirStmt::After(HirAfter::new(2, 2, vec![leaf()]))],
            )),
            HirStmt::While(HirWhile::new(
                vec![HirCond::always()],
                vec![HirStmt::Every(HirEvery::new(2, 2, vec![leaf()]))],
            )),
        ];
        for node in nodes {
            let mut next_id = 0;
            let opt = optimize_test_stmts(vec![node], &mut next_id).unwrap();
            assert!(!has_counted(&opt), "no counted node must survive: {opt:?}");
        }
    }

    /// C11-04: a top-level `every` increment merges into a preceding
    /// unconditional preserved trigger — the DSL's own act-run packing
    /// — instead of splitting into its own trigger. The merged act uses
    /// the node's `counter_var` verbatim.
    #[test]
    fn every_iadd_merges_into_preceding_unconditional_trigger() {
        let frame_act = HirAct::set_var_const(0, 8, 1, 0xFFFF_FFFF);
        let run = HirStmt::Trigger(HirTrigger::new(0, vec![], vec![frame_act], true).unwrap());
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 1;
        let opt = optimize_test_stmts(
            vec![run, HirStmt::Every(HirEvery::new(3, 2, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 2, "merged run + fire trigger, got {opt:?}");
        if let HirStmt::Trigger(t) = &opt[0] {
            assert!(t.conds.is_empty());
            assert_eq!(
                t.acts,
                vec![
                    HirAct::set_var_const(0, 8, 1, 0xFFFF_FFFF),
                    HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF),
                ]
            );
        } else {
            panic!("expected the merged run trigger, got {:?}", opt[0]);
        }
        assert!(matches!(opt[1], HirStmt::Trigger(_)));
    }

    /// C11-04: no merge into a conditional trigger — the increment
    /// must stay a bare act (its own trigger downstream), exactly as a
    /// fresh act run after a conditional trigger.
    #[test]
    fn every_iadd_does_not_merge_into_conditional_trigger() {
        let cond_trig = HirStmt::Trigger(
            HirTrigger::new(
                0,
                vec![HirCond::always()],
                vec![HirAct::set_var_const(0, 8, 1, 0xFFFF_FFFF)],
                true,
            )
            .unwrap(),
        );
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 1;
        let opt = optimize_test_stmts(
            vec![cond_trig, HirStmt::Every(HirEvery::new(3, 2, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 3, "cond trigger + bare iadd + fire, got {opt:?}");
        assert_eq!(
            opt[1],
            HirStmt::Act(HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF))
        );
    }

    /// C11-04: no merge into a self-disabling (preserved=false)
    /// trigger — appending the increment there would run it once only,
    /// breaking the counter.
    #[test]
    fn every_iadd_does_not_merge_into_self_disabling_trigger() {
        let once_trig = HirStmt::Trigger(
            HirTrigger::new(
                0,
                vec![],
                vec![HirAct::set_var_const(0, 8, 1, 0xFFFF_FFFF)],
                false,
            )
            .unwrap(),
        );
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 1;
        let opt = optimize_test_stmts(
            vec![once_trig, HirStmt::Every(HirEvery::new(3, 2, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 3, "once trigger + bare iadd + fire, got {opt:?}");
        assert_eq!(
            opt[1],
            HirStmt::Act(HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF))
        );
    }

    /// C11-04: no merge into a full (64-act) trigger — the increment
    /// stays a bare act rather than overflowing the trigger.
    #[test]
    fn every_iadd_does_not_merge_into_full_trigger() {
        let mut acts = Vec::new();
        for i in 0..64 {
            acts.push(HirAct::set_var_const(0, 8, i, 0xFFFF_FFFF));
        }
        let full = HirStmt::Trigger(HirTrigger::new(0, vec![], acts, true).unwrap());
        let body = vec![HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF))];
        let mut next_id = 1;
        let opt = optimize_test_stmts(
            vec![full, HirStmt::Every(HirEvery::new(3, 2, body))],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(opt.len(), 3, "full trigger + bare iadd + fire, got {opt:?}");
        assert_eq!(
            opt[1],
            HirStmt::Act(HirAct::set_var_const(2, 8, 1, 0xFFFF_FFFF))
        );
    }

    /// A bare unconditional preserved flag-4 trigger with one act.
    fn bare_block() -> HirTrigger {
        HirTrigger::new(
            0,
            vec![],
            vec![HirAct::set_var_const(0, MOD_ADD, 1, FULL_MASK)],
            true,
        )
        .unwrap()
    }

    /// Optimize `trigger; every(3) { ... }` and assert the counter increment
    /// did not merge into the trigger.
    fn assert_every_iadd_stays_bare(trigger: HirTrigger, shape: &str) {
        let body = vec![HirStmt::Act(HirAct::set_var_const(
            1, MOD_SET_TO, 5, FULL_MASK,
        ))];
        let mut next_id = 1;
        let opt = optimize_test_stmts(
            vec![
                HirStmt::Trigger(trigger),
                HirStmt::Every(HirEvery::new(3, 2, body)),
            ],
            &mut next_id,
        )
        .unwrap();
        assert_eq!(
            opt.len(),
            3,
            "{shape} trigger + bare iadd + fire, got {opt:?}"
        );
        assert_eq!(
            opt[1],
            HirStmt::Act(HirAct::set_var_const(2, MOD_ADD, 1, FULL_MASK))
        );
    }

    /// A `prev` chain pointer means the trigger is not the bare unconditional
    /// flag-4 block shape; the every increment must stay a bare act.
    #[test]
    fn every_iadd_does_not_merge_into_a_prev_linked_trigger() {
        let mut linked = bare_block();
        linked.prev = Some(HirTriggerPtr::Const { value: 0 });
        assert_every_iadd_stays_bare(linked, "prev-linked");
    }

    /// A `next` chain pointer is likewise not the unpackable bare block shape.
    #[test]
    fn every_iadd_does_not_merge_into_a_next_linked_trigger() {
        let mut linked = bare_block();
        linked.next = Some(HirTriggerPtr::Const { value: 0 });
        assert_every_iadd_stays_bare(linked, "next-linked");
    }

    /// Any flag encoding other than the preserved flag-4 shape is not
    /// unpackable, so the increment cannot merge into it.
    #[test]
    fn every_iadd_does_not_merge_into_non_flag4_trigger() {
        let mut flags6 = bare_block();
        flags6.flags = 6;
        assert_every_iadd_stays_bare(flags6, "non-flag4");
    }

    /// The hand-written old (DSL-desugar) shape for an edge node, with
    /// explicit state ids. Mirrors `desugar_edge` statement for
    /// statement (reset act, sample gate, fire gate, prev updates).
    fn old_edge_shape(
        direction: HirEdgeDirection,
        conds: Vec<HirCond>,
        body: Vec<HirStmt>,
        curr: usize,
        prev: usize,
    ) -> Vec<HirStmt> {
        let (fire_curr, fire_prev) = match direction {
            HirEdgeDirection::Rising => (1, 0),
            HirEdgeDirection::Falling => (0, 1),
        };
        vec![
            HirStmt::Act(HirAct::set_var_const(curr, 7, 0, 0xFFFF_FFFF)),
            HirStmt::When(HirWhen::new(
                conds,
                vec![HirStmt::Act(HirAct::set_var_const(curr, 7, 1, 0xFFFF_FFFF))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, fire_curr)],
                vec![HirStmt::When(HirWhen::new(
                    vec![HirCond::var_deaths(prev, 10, fire_prev)],
                    body,
                ))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, 1)],
                vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 1, 0xFFFF_FFFF))],
            )),
            HirStmt::When(HirWhen::new(
                vec![HirCond::var_deaths(curr, 10, 0)],
                vec![HirStmt::Act(HirAct::set_var_const(prev, 7, 0, 0xFFFF_FFFF))],
            )),
        ]
    }

    /// True if any phase-1 temporal node survives anywhere in the
    /// statement tree (including inside when/once/While bodies and
    /// packed triggers' surroundings). A shallow desugar that does
    /// not recurse leaves inner nodes behind, so this scan is the
    /// recursion detector for the C11-05 nesting tests.
    fn has_temporal_node(stmts: &[HirStmt]) -> bool {
        stmts.iter().any(|s| match s {
            HirStmt::Until(_) | HirStmt::Edge(_) | HirStmt::Every(_) | HirStmt::After(_) => true,
            HirStmt::When(w) => has_temporal_node(&w.body) || has_temporal_node(&w.else_body),
            HirStmt::Once(o) => has_temporal_node(&o.body),
            HirStmt::While(w) => has_temporal_node(&w.body),
            _ => false,
        })
    }

    /// C11-05: cross-node nesting (until-in-edge, edge-in-until,
    /// every-in-after, after-in-every) fully desugars: no new node
    /// survives optimization at any depth. Edge-in-edge is
    /// additionally pinned by deep equality against the hand-built
    /// old shape.
    #[test]
    fn c11_05_cross_node_nesting_fully_desugars() {
        let leaf = || HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF));
        let cond = || vec![HirCond::always()];
        let cases = vec![
            HirStmt::Edge(HirEdge::new(
                HirEdgeDirection::Rising,
                cond(),
                vec![HirStmt::Until(HirUntil::new(cond(), vec![leaf()], 9))],
                2,
                3,
            )),
            HirStmt::Until(HirUntil::new(
                cond(),
                vec![HirStmt::Edge(HirEdge::new(
                    HirEdgeDirection::Rising,
                    cond(),
                    vec![leaf()],
                    2,
                    3,
                ))],
                9,
            )),
            HirStmt::After(HirAfter::new(
                2,
                5,
                vec![HirStmt::Every(HirEvery::new(2, 4, vec![leaf()]))],
            )),
            HirStmt::Every(HirEvery::new(
                3,
                5,
                vec![HirStmt::After(HirAfter::new(2, 4, vec![leaf()]))],
            )),
        ];
        for (i, case) in cases.into_iter().enumerate() {
            let mut next_id = 0;
            let opt = optimize_test_stmts(vec![case], &mut next_id).unwrap();
            assert!(
                !has_temporal_node(&opt),
                "case {i} must fully desugar: {opt:?}"
            );
        }
        // Edge-in-edge deep equality against the hand-built old shape.
        let inner = HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Rising,
            cond(),
            vec![leaf()],
            4,
            5,
        ));
        let outer = HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Falling,
            cond(),
            vec![inner],
            2,
            3,
        ));
        let mut next_id = 0;
        let got = optimize_test_stmts(vec![outer], &mut next_id).unwrap();
        let old_inner = old_edge_shape(HirEdgeDirection::Rising, cond(), vec![leaf()], 4, 5);
        let old_outer = old_edge_shape(HirEdgeDirection::Falling, cond(), old_inner, 2, 3);
        let mut next_id2 = 0;
        let want = optimize_test_stmts(old_outer, &mut next_id2).unwrap();
        assert_eq!(got, want);
        assert!(!has_temporal_node(&got));
    }

    /// C11-05: 3-deep nesting (every inside when inside until) fully
    /// desugars, and the leaf act survives into the output.
    #[test]
    fn c11_05_three_deep_nesting_fully_desugars() {
        fn contains_leaf_act(stmts: &[HirStmt], act: &HirAct) -> bool {
            stmts.iter().any(|s| match s {
                HirStmt::Act(a) => a == act,
                HirStmt::Trigger(t) => t.acts.contains(act),
                HirStmt::When(w) => {
                    contains_leaf_act(&w.body, act) || contains_leaf_act(&w.else_body, act)
                }
                HirStmt::Once(o) => contains_leaf_act(&o.body, act),
                HirStmt::While(w) => contains_leaf_act(&w.body, act),
                _ => false,
            })
        }
        let leaf = || HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF));
        // Gated on purpose: an unconditional outer gate drops its body
        // (exclusive semantics, issue #72), so the nested nodes could
        // never appear.
        let input = HirStmt::Until(HirUntil::new(
            vec![HirCond::deaths(0, 10, 42, 0)],
            vec![HirStmt::When(HirWhen::new(
                vec![HirCond::always()],
                vec![HirStmt::Every(HirEvery::new(2, 4, vec![leaf()]))],
            ))],
            9,
        ));
        let mut next_id = 0;
        let opt = optimize_test_stmts(vec![input], &mut next_id).unwrap();
        assert!(!has_temporal_node(&opt), "no node may survive: {opt:?}");
        assert!(
            contains_leaf_act(&opt, &HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF)),
            "leaf act must survive into the output: {opt:?}"
        );
    }

    /// C11-05 recursion proof: nested whens inside an until body must
    /// still flatten. If desugar did not recurse, the Until arm would
    /// push the raw expansion without optimizing it, so When nodes
    /// would survive in the output and the LIR would differ from the
    /// old program (whose plain whens opt flattens).
    ///
    /// A gated cond is used here on purpose: an unconditional gate
    /// drops the body (exclusive semantics, issue #72), so it cannot
    /// exercise flattening.
    #[test]
    fn c11_05_nested_when_inside_until_body_flattens() {
        let leaf = HirStmt::Act(HirAct::set_var_const(1, 7, 5, 0xFFFF_FFFF));
        let c1 = HirCond::deaths(0, 10, 1, 0);
        let c2 = HirCond::deaths(0, 10, 2, 0);
        let body = vec![HirStmt::When(HirWhen::new(
            vec![c1],
            vec![HirStmt::When(HirWhen::new(vec![c2], vec![leaf]))],
        ))];
        let gated = vec![HirCond::deaths(0, 10, 42, 0)];
        let node = HirStmt::Until(HirUntil::new(gated.clone(), body.clone(), 9));
        let mut next_id = 0;
        let got = optimize_test_stmts(vec![node], &mut next_id).unwrap();
        let mut next_id2 = 0;
        let want = optimize_test_stmts(
            old_until_shape(9, gated, body),
            &mut next_id2,
        )
        .unwrap();
        assert_eq!(got, want);
        fn has_when(stmts: &[HirStmt]) -> bool {
            stmts.iter().any(|s| match s {
                HirStmt::When(_) => true,
                HirStmt::Once(o) => has_when(&o.body),
                HirStmt::While(w) => has_when(&w.body),
                _ => false,
            })
        }
        assert!(!has_when(&got), "nested whens must flatten: {got:?}");
    }

    // ---- Cycle 3 #03: post-schedule dead 0-act gate removal ----

    fn gate_module(stmts: Vec<HirStmt>, funcs: Vec<eudkit_hir::HirFunc>) -> HirModule {
        HirModule {
            vars: Vec::new(),
            stmts,
            funcs,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        }
    }

    fn empty_trigger(id: usize) -> HirTrigger {
        HirTrigger::new(id, vec![HirCond::always()], Vec::new(), true).expect("legal empty trigger")
    }

    fn live_trigger(id: usize) -> HirTrigger {
        HirTrigger::new(id, Vec::new(), vec![HirAct::set_deaths(0, 7, 1, 0)], true)
            .expect("legal live trigger")
    }

    fn trigger_ids(stmts: &[HirStmt]) -> Vec<usize> {
        stmts
            .iter()
            .filter_map(|s| match s {
                HirStmt::Trigger(t) => Some(t.id),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn drops_an_unreferenced_empty_trigger() {
        let mut module = gate_module(
            vec![
                HirStmt::Trigger(empty_trigger(0)),
                HirStmt::Trigger(live_trigger(1)),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.stmts), vec![1]);
    }

    #[test]
    fn keeps_an_empty_trigger_with_prev_or_next() {
        let mut with_prev = empty_trigger(0);
        with_prev.prev = Some(HirTriggerPtr::Const { value: -1 });
        let mut with_next = empty_trigger(1);
        with_next.next = Some(HirTriggerPtr::Const { value: -1 });
        let mut module = gate_module(
            vec![HirStmt::Trigger(with_prev), HirStmt::Trigger(with_next)],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.stmts), vec![0, 1]);
    }

    #[test]
    fn keeps_an_empty_trigger_targeted_by_a_reloc() {
        let mut live = live_trigger(1);
        live.acts[0].relocs.push(HirReloc::trigger(16, 0, 0, 4));
        let mut module = gate_module(
            vec![HirStmt::Trigger(empty_trigger(0)), HirStmt::Trigger(live)],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.stmts), vec![0, 1]);
    }

    #[test]
    fn keeps_an_empty_trigger_targeted_by_a_blob_reloc() {
        let mut module = gate_module(
            vec![
                HirStmt::Trigger(empty_trigger(3)),
                HirStmt::Trigger(live_trigger(4)),
            ],
            Vec::new(),
        );
        module.blobs = vec![
            eudkit_hir::HirBlob::zeroed(4)
                .with_relocs(vec![eudkit_hir::HirBlobReloc::trigger(0, 3, 0, 4)]),
        ];
        drop_dead_triggers(&mut module);
        assert_eq!(
            trigger_ids(&module.stmts),
            vec![3, 4],
            "a blob relocation keeps the empty gate alive"
        );
    }

    #[test]
    fn keeps_an_empty_trigger_targeted_by_a_var_store() {
        let mut module = gate_module(
            vec![
                HirStmt::Trigger(empty_trigger(0)),
                HirStmt::VarStore(HirVarStore::to_trigger(1, 0, 16, 7)),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.stmts), vec![0]);
        assert!(matches!(&module.stmts[1], HirStmt::VarStore(s) if s.dst_trigger == Some(0)));
    }

    #[test]
    fn keeps_an_empty_trigger_referenced_from_another_body() {
        let mut live = live_trigger(1);
        live.acts[0].relocs.push(HirReloc::trigger(16, 5, 0, 4));
        let mut module = gate_module(
            vec![HirStmt::Trigger(live)],
            vec![eudkit_hir::HirFunc::new(
                0,
                Vec::new(),
                None,
                vec![HirStmt::Trigger(empty_trigger(5))],
            )],
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.funcs[0].body), vec![5]);
    }

    #[test]
    fn drops_an_empty_when_node() {
        let mut module = gate_module(
            vec![
                HirStmt::When(HirWhen::new(vec![HirCond::always()], Vec::new())),
                HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0)),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(module.stmts.len(), 1);
        assert!(matches!(&module.stmts[0], HirStmt::Act(_)));
    }

    #[test]
    fn drops_an_empty_once_node() {
        let mut module = gate_module(
            vec![
                HirStmt::Once(HirOnce::new(vec![HirCond::always()], Vec::new())),
                HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0)),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(module.stmts.len(), 1);
        assert!(matches!(&module.stmts[0], HirStmt::Act(_)));
    }

    #[test]
    fn does_not_drop_an_empty_while() {
        let mut module = gate_module(
            vec![HirStmt::While(HirWhile::new(
                vec![HirCond::always()],
                Vec::new(),
            ))],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert!(matches!(&module.stmts[0], HirStmt::While(w) if w.body.is_empty()));
    }

    #[test]
    fn drops_nested_empty_gates() {
        let mut module = gate_module(
            vec![
                HirStmt::When(HirWhen::with_else(
                    vec![HirCond::always()],
                    vec![
                        HirStmt::Trigger(empty_trigger(0)),
                        HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0)),
                    ],
                    vec![HirStmt::Once(HirOnce::new(
                        vec![HirCond::always()],
                        Vec::new(),
                    ))],
                )),
                HirStmt::When(HirWhen::new(
                    vec![HirCond::always()],
                    vec![HirStmt::When(HirWhen::new(
                        vec![HirCond::always()],
                        Vec::new(),
                    ))],
                )),
                HirStmt::Once(HirOnce::new(
                    vec![HirCond::always()],
                    vec![HirStmt::Trigger(empty_trigger(1))],
                )),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(module.stmts.len(), 1, "only the non-empty gate survives");
        match &module.stmts[0] {
            HirStmt::When(w) => {
                assert_eq!(w.body.len(), 1);
                assert!(matches!(&w.body[0], HirStmt::Act(_)));
                assert!(w.else_body.is_empty());
            }
            other => panic!("expected the surviving When, got {other:?}"),
        }
    }

    #[test]
    fn does_not_renumber_surviving_trigger_ids() {
        let mut module = gate_module(
            vec![
                HirStmt::Trigger(empty_trigger(0)),
                HirStmt::Trigger(live_trigger(5)),
                HirStmt::Trigger(empty_trigger(9)),
                HirStmt::Trigger(live_trigger(7)),
            ],
            Vec::new(),
        );
        drop_dead_triggers(&mut module);
        assert_eq!(trigger_ids(&module.stmts), vec![5, 7]);
    }
}
