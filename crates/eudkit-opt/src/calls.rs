//! Call-site rewrites that keep a shared subroutine at inlined executed cost.
//!
//! A subroutine call copies the callee's result slot into the call's `dst`
//! variable, and the caller then typically assigns that `dst` somewhere else
//! (`out.set(f(...))`). Both steps are variable-to-variable moves, and every
//! move is one `TriggerPage` firing (64 executed actions). Fusing the call
//! result into the final destination removes one firing per call, so a shared
//! body costs what the inlined copy it replaced cost.
//!
//! [`inline_single_call_funcs`] then decides whether a body is worth sharing at
//! all. One site always inlines (nothing is shared); several sites inline only
//! when duplicating the body packs into no more static triggers than sharing
//! it. A leaf-act body is unpacked into bare acts before insertion, so the
//! scheduler packs the copies across sites exactly like hand-written acts.
//! Large, condition-heavy bodies therefore stay shared while small kernels cost
//! no call machinery per site. The estimate is conservative where cross-copy
//! packing cannot be proven, which is the direction ADR 0001's
//! executed > static > bytes priority asks for.

use std::collections::{HashMap, HashSet};

use eudkit_hir::{
    HirCall, HirCallArg, HirCond, HirError, HirModule, HirReloc, HirStmt, TriggerRefs,
};
use eudkit_lir::DEST_FIELD_OFFSET;

use crate::facts::is_full_value_dest_write;

/// Rewrite `Call{dst: D}` immediately followed by `X := D` into
/// `Call{dst: X}` and drop the transfer.
///
/// The rewrite is only legal when `D` has no other read and is never written
/// by another statement: the call's result copy then lands in `X` at exactly
/// the point where the dropped transfer did (the call's return lands on the
/// next item, and an immediately-following transfer is that landing), so the
/// observable order of writes is unchanged.
pub fn fuse_call_results(module: &mut HirModule) {
    let mut uses = VarUses::default();
    for stmt in &module.stmts {
        uses.collect_stmt(stmt);
    }
    for func in &module.funcs {
        for stmt in &func.body {
            uses.collect_stmt(stmt);
        }
    }

    fuse_list(&mut module.stmts, &uses);
    for func in &mut module.funcs {
        fuse_list(&mut func.body, &uses);
    }
}

/// Whether `reloc` on `act` depends on the pre-existing value of its target.
///
/// A non-dest reloc (`reloc.field_offset != DEST_FIELD_OFFSET`) is an operand
/// read. A dest reloc into a non-value target field (a machinery patch) also
/// leaves the value word intact. Those cases are reads, as is any write that is
/// not a full value-word `SetTo`: a non-`SetDeaths`, an RMW modifier, a partial
/// bitmask, or a `SetDeathsX` with a zero mask (a 0-bit no-op in LIR).
fn reloc_reads_target(act: &eudkit_hir::HirAct, reloc: &HirReloc) -> bool {
    !is_full_value_dest_write(act, reloc)
}

fn collect_conds(uses: &mut VarUses, conds: &[eudkit_hir::HirCond]) {
    for c in conds {
        for r in &c.relocs {
            if let Some(id) = r.target_var {
                uses.read(id);
            }
        }
    }
}

#[derive(Default)]
struct VarUses {
    reads: HashMap<usize, usize>,
    writes: HashSet<usize>,
    call_dsts: HashMap<usize, usize>,
}

impl VarUses {
    fn read(&mut self, id: usize) {
        *self.reads.entry(id).or_insert(0) += 1;
    }

    fn write(&mut self, id: usize) {
        self.writes.insert(id);
    }

    fn collect_act(&mut self, act: &eudkit_hir::HirAct) {
        for r in &act.relocs {
            let Some(id) = r.target_var else {
                continue;
            };
            if r.field_offset == DEST_FIELD_OFFSET {
                self.write(id);
                if reloc_reads_target(act, r) {
                    self.read(id);
                }
            } else {
                self.read(id);
            }
        }
    }

    fn collect_stmt(&mut self, stmt: &HirStmt) {
        match stmt {
            HirStmt::Trigger(t) => {
                collect_conds(self, &t.conds);
                for a in &t.acts {
                    self.collect_act(a);
                }
            }
            HirStmt::Act(a) => self.collect_act(a),
            HirStmt::VarTransfer(t) => {
                self.read(t.src_var);
                if t.modifier != 7 {
                    self.read(t.dst_var);
                }
                self.write(t.dst_var);
            }
            HirStmt::VarBatch(b) => {
                for t in &b.transfers {
                    self.read(t.src_var);
                    if t.modifier != 7 {
                        self.read(t.dst_var);
                    }
                    self.write(t.dst_var);
                }
                for s in &b.stores {
                    self.read(s.src_var);
                }
            }
            HirStmt::VarStore(s) => self.read(s.src_var),
            HirStmt::Call(c) => {
                for arg in &c.args {
                    if let eudkit_hir::HirCallArg::Var { id } = arg {
                        self.read(*id);
                    }
                }
                if let Some(dst) = c.dst {
                    *self.call_dsts.entry(dst).or_insert(0) += 1;
                }
            }
            HirStmt::When(w) => {
                collect_conds(self, &w.conds);
                for s in &w.body {
                    self.collect_stmt(s);
                }
                for s in &w.else_body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::Once(o) => {
                collect_conds(self, &o.conds);
                for s in &o.body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::While(w) => {
                collect_conds(self, &w.conds);
                for s in &w.body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::Until(u) => {
                collect_conds(self, &u.conds);
                self.read(u.latch_var);
                for s in &u.body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::Edge(e) => {
                collect_conds(self, &e.conds);
                self.read(e.curr_var);
                self.read(e.prev_var);
                for s in &e.body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::Every(e) => {
                self.read(e.counter_var);
                for s in &e.body {
                    self.collect_stmt(s);
                }
            }
            HirStmt::After(a) => {
                self.read(a.counter_var);
                for s in &a.body {
                    self.collect_stmt(s);
                }
            }
        }
    }

    fn single_read(&self, id: usize) -> bool {
        self.reads.get(&id).copied() == Some(1)
            && !self.writes.contains(&id)
            && self.call_dsts.get(&id).copied() == Some(1)
    }
}

fn fuse_list(stmts: &mut Vec<HirStmt>, uses: &VarUses) {
    // Recurse first so nested blocks see the same use counts (a nested block's
    // rewrite cannot change another block's counts: it only removes a read of
    // a var that then becomes unread).
    for stmt in stmts.iter_mut() {
        match stmt {
            HirStmt::When(w) => {
                fuse_list(&mut w.body, uses);
                fuse_list(&mut w.else_body, uses);
            }
            HirStmt::Once(o) => fuse_list(&mut o.body, uses),
            HirStmt::While(w) => fuse_list(&mut w.body, uses),
            HirStmt::Until(u) => fuse_list(&mut u.body, uses),
            HirStmt::Edge(e) => fuse_list(&mut e.body, uses),
            HirStmt::Every(e) => fuse_list(&mut e.body, uses),
            HirStmt::After(a) => fuse_list(&mut a.body, uses),
            _ => {}
        }
    }

    let mut i = 0;
    while i + 1 < stmts.len() {
        let fusible = match (&stmts[i], &stmts[i + 1]) {
            (HirStmt::Call(call), HirStmt::VarTransfer(transfer)) => {
                fuse_pair(call, transfer, uses)
            }
            _ => None,
        };
        match fusible {
            Some(incoming) => {
                if let HirStmt::Call(call) = &mut stmts[i] {
                    call.dst = Some(incoming);
                }
                stmts.remove(i + 1);
            }
            None => i += 1,
        }
    }
}

fn fuse_pair(call: &HirCall, transfer: &eudkit_hir::HirVarTransfer, uses: &VarUses) -> Option<usize> {
    let dst = call.dst?;
    if transfer.src_var != dst || transfer.modifier != 7 {
        return None;
    }
    if transfer.dst_var == dst || !uses.single_read(dst) {
        return None;
    }
    Some(transfer.dst_var)
}

/// Extra static triggers a multi-site inline may cost over the shared shape.
///
/// The default `0` means a body is duplicated only when the copies pack into
/// no more static triggers than sharing. Setting it to `1` would admit the two
/// measured `+1T` cases without re-deriving the estimator:
///
/// - a 200-act leaf run at two sites (`ceil(400 / 64) == 7` copies against a
///   shared `ceil(200 / 64) + 2 == 6`),
/// - a 100-act leaf run at four sites (`ceil(400 / 64) == 7` against `2 + 4`).
///
/// Both save executed triggers but add one 2,408-byte trigger, so ADR 0001's
/// executed > static > bytes priority keeps them shared until a profile says
/// the trade is wanted.
const INLINE_EXTRA_TRIGGERS: usize = 0;

/// Total acts of a leaf-act run: every statement is a bare `Act` or an
/// unpackable `Trigger`. `None` for any other shape.
fn leaf_act_count(stmts: &[HirStmt], refs: &TriggerRefs) -> Option<usize> {
    let mut total = 0;
    for stmt in stmts {
        match stmt {
            HirStmt::Act(_) => total += 1,
            HirStmt::Trigger(trig) if trig.is_unpackable(refs) => {
                total += trig.acts.len();
            }
            _ => return None,
        }
    }
    Some(total)
}

/// Static triggers a retained copy of `stmts` occupies.
///
/// The leaf-act run is the only shape whose copies can pack together, so it is
/// estimated as one run. Everything else counts each contiguous bare-`Act`
/// run, each retained `Trigger`, and each `When` the way the scheduler will
/// emit them; a `When` that cannot pack into one `Trigger` pays its gate plus
/// its body. That over-counts any cross-copy packing the estimator cannot
/// prove, which is the conservative direction under ADR 0001.
fn body_triggers(stmts: &[HirStmt], refs: &TriggerRefs) -> usize {
    if let Some(acts) = leaf_act_count(stmts, refs) {
        return acts.div_ceil(eudkit_lir::ACT_COUNT);
    }
    let mut total = 0;
    let mut run: usize = 0;
    for stmt in stmts {
        match stmt {
            HirStmt::Act(_) => run += 1,
            HirStmt::Trigger(_) => {
                total += run.div_ceil(eudkit_lir::ACT_COUNT);
                run = 0;
                total += 1;
            }
            HirStmt::When(w) => {
                total += run.div_ceil(eudkit_lir::ACT_COUNT);
                run = 0;
                let packs = w.else_body.is_empty()
                    && w.conds.len() <= eudkit_lir::COND_COUNT
                    && w.body.len() <= eudkit_lir::ACT_COUNT
                    && w.body.iter().all(|s| matches!(s, HirStmt::Act(_)));
                total += if packs {
                    1
                } else {
                    1 + body_triggers(&w.body, refs)
                };
            }
            // The multi-site shape whitelist admits nothing else.
            _ => {}
        }
    }
    total + run.div_ceil(eudkit_lir::ACT_COUNT)
}

/// The shape whitelist for duplicating a body.
///
/// Multi-site inlining may only copy `Act`, retained `Trigger`, and `When`
/// nodes. Latch/counter/chain state, transfers, stores, and batching never
/// enter the duplication decision: those bodies stay shared, which is the
/// conservative direction. A retained trigger must keep the same
/// `flags == 4` shape the unpacking path assumes, carry no chain pointer,
/// relocation, or externally named id, and a `When` must have no else arm and
/// recursively satisfy the same whitelist.
fn multi_site_shape_ok(stmts: &[HirStmt], refs: &TriggerRefs) -> bool {
    fn relocs_ok(relocs: &[eudkit_hir::HirReloc]) -> bool {
        relocs.iter().all(|reloc| reloc.target_trigger.is_none())
    }
    fn conds_ok(conds: &[eudkit_hir::HirCond]) -> bool {
        conds.iter().all(|cond| relocs_ok(&cond.relocs))
    }
    stmts.iter().all(|stmt| match stmt {
        HirStmt::Act(act) => relocs_ok(&act.relocs),
        HirStmt::Trigger(trig) => {
            trig.preserved
                && trig.flags == 4
                && trig.prev.is_none()
                && trig.next.is_none()
                && !refs.contains(trig.id)
                && conds_ok(&trig.conds)
                && trig.acts.iter().all(|act| relocs_ok(&act.relocs))
        }
        HirStmt::When(w) => {
            w.else_body.is_empty() && conds_ok(&w.conds) && multi_site_shape_ok(&w.body, refs)
        }
        _ => false,
    })
}

/// Whether every call to `func_id` is in this statement list and every
/// statement between them is the call itself or a bare `Act`.
///
/// Only then do the copies pack into one contiguous act run; a `Trigger`
/// statement (even an unconditional flag-`4` one) calls the scheduler's
/// `flush_acts` and ends the pending run.
fn all_sites_in_one_adjacent_run(stmts: &[HirStmt], func_id: usize, total: usize) -> bool {
    let positions: Vec<usize> = stmts
        .iter()
        .enumerate()
        .filter_map(|(index, stmt)| match stmt {
            HirStmt::Call(call) if call.func == func_id => Some(index),
            _ => None,
        })
        .collect();
    if positions.len() == total {
        let (Some(&first), Some(&last)) = (positions.first(), positions.last()) else {
            return false;
        };
        return stmts[first..=last].iter().all(|stmt| match stmt {
            HirStmt::Act(_) => true,
            HirStmt::Call(call) => call.func == func_id,
            _ => false,
        });
    }
    stmts.iter().any(|stmt| match stmt {
        HirStmt::When(w) => {
            all_sites_in_one_adjacent_run(&w.body, func_id, total)
                || all_sites_in_one_adjacent_run(&w.else_body, func_id, total)
        }
        HirStmt::Once(o) => all_sites_in_one_adjacent_run(&o.body, func_id, total),
        HirStmt::While(w) => all_sites_in_one_adjacent_run(&w.body, func_id, total),
        HirStmt::Until(u) => all_sites_in_one_adjacent_run(&u.body, func_id, total),
        HirStmt::Edge(e) => all_sites_in_one_adjacent_run(&e.body, func_id, total),
        HirStmt::Every(e) => all_sites_in_one_adjacent_run(&e.body, func_id, total),
        HirStmt::After(a) => all_sites_in_one_adjacent_run(&a.body, func_id, total),
        _ => false,
    })
}

fn collect_calls(stmts: &[HirStmt], func_id: usize, out: &mut Vec<HirCall>) {
    for stmt in stmts {
        match stmt {
            HirStmt::Call(call) if call.func == func_id => out.push(call.clone()),
            HirStmt::When(w) => {
                collect_calls(&w.body, func_id, out);
                collect_calls(&w.else_body, func_id, out);
            }
            HirStmt::Once(o) => collect_calls(&o.body, func_id, out),
            HirStmt::While(w) => collect_calls(&w.body, func_id, out),
            HirStmt::Until(u) => collect_calls(&u.body, func_id, out),
            HirStmt::Edge(e) => collect_calls(&e.body, func_id, out),
            HirStmt::Every(e) => collect_calls(&e.body, func_id, out),
            HirStmt::After(a) => collect_calls(&a.body, func_id, out),
            _ => {}
        }
    }
}

/// Re-emit every unpackable body trigger as bare acts so the scheduler packs
/// the copies across sites.
fn unpack_leaf_acts(stmts: Vec<HirStmt>, refs: &TriggerRefs) -> Vec<HirStmt> {
    let mut out = Vec::with_capacity(stmts.len());
    for stmt in stmts {
        match stmt {
            // `leaf_act_count` admitted this body, so every trigger here is an
            // unpackable flag-`4` block. Use the one HIR predicate the
            // temporal merge and lowering also use, so the flattening paths
            // cannot drift.
            HirStmt::Trigger(trig) if trig.is_unpackable(refs) => {
                out.extend(trig.acts.into_iter().map(HirStmt::Act));
            }
            other => out.push(other),
        }
    }
    out
}

/// Mint a fresh id for every retained `HirTrigger` in a later copy, anywhere
/// in the body (including inside retained `When` bodies).
///
/// `multi_site_shape_ok` admits only `Act`, `Trigger`, and `When`, so only
/// those arms can carry a retained id here; keeping dead temporal arms would
/// hide that invariant.
fn remint_trigger_ids(stmts: &mut [HirStmt], next_id: &mut usize) {
    for stmt in stmts {
        match stmt {
            HirStmt::Trigger(trig) => {
                trig.id = *next_id;
                *next_id += 1;
            }
            HirStmt::When(w) => {
                remint_trigger_ids(&mut w.body, next_id);
                remint_trigger_ids(&mut w.else_body, next_id);
            }
            // The whitelist admits no other statement that owns a trigger id.
            _ => {}
        }
    }
}

/// Collect every retained trigger id in `stmts` into `ids`, returning an
/// error if a duplicate appears. Called after reminting to turn a would-be
/// lowering misresolve into a compile error.
fn collect_unique_trigger_ids(
    stmts: &[HirStmt],
    ids: &mut HashSet<usize>,
) -> Result<(), HirError> {
    for stmt in stmts {
        match stmt {
            HirStmt::Trigger(trig) => {
                if !ids.insert(trig.id) {
                    return Err(HirError::OptimizerInvariant(
                        "multi-site inline reminted a duplicate trigger id",
                    ));
                }
            }
            HirStmt::When(w) => {
                collect_unique_trigger_ids(&w.body, ids)?;
                collect_unique_trigger_ids(&w.else_body, ids)?;
            }
            _ => {}
        }
    }
    Ok(())
}

/// Replace every remaining call to `func_id` with the next pre-computed body,
/// in the same depth-first order [`collect_calls`] used.
fn replace_all_sites(
    stmts: Vec<HirStmt>,
    func_id: usize,
    bodies: &mut impl Iterator<Item = Vec<HirStmt>>,
) -> Result<(Vec<HirStmt>, usize), HirError> {
    let mut out = Vec::with_capacity(stmts.len());
    let mut replaced = 0;
    for stmt in stmts {
        match stmt {
            HirStmt::Call(call) if call.func == func_id => {
                let Some(body) = bodies.next() else {
                    return Err(HirError::OptimizerInvariant(
                        "collected call sites and inlined bodies disagree",
                    ));
                };
                out.extend(body);
                replaced += 1;
            }
            HirStmt::When(mut w) => {
                let (body, n) = replace_all_sites(w.body, func_id, bodies)?;
                w.body = body;
                replaced += n;
                let (else_body, n) = replace_all_sites(w.else_body, func_id, bodies)?;
                w.else_body = else_body;
                replaced += n;
                out.push(HirStmt::When(w));
            }
            HirStmt::Once(mut o) => {
                let (body, n) = replace_all_sites(o.body, func_id, bodies)?;
                o.body = body;
                replaced += n;
                out.push(HirStmt::Once(o));
            }
            HirStmt::While(mut w) => {
                let (body, n) = replace_all_sites(w.body, func_id, bodies)?;
                w.body = body;
                replaced += n;
                out.push(HirStmt::While(w));
            }
            HirStmt::Until(mut u) => {
                let (body, n) = replace_all_sites(u.body, func_id, bodies)?;
                u.body = body;
                replaced += n;
                out.push(HirStmt::Until(u));
            }
            HirStmt::Edge(mut e) => {
                let (body, n) = replace_all_sites(e.body, func_id, bodies)?;
                e.body = body;
                replaced += n;
                out.push(HirStmt::Edge(e));
            }
            HirStmt::Every(mut e) => {
                let (body, n) = replace_all_sites(e.body, func_id, bodies)?;
                e.body = body;
                replaced += n;
                out.push(HirStmt::Every(e));
            }
            HirStmt::After(mut a) => {
                let (body, n) = replace_all_sites(a.body, func_id, bodies)?;
                a.body = body;
                replaced += n;
                out.push(HirStmt::After(a));
            }
            other => out.push(other),
        }
    }
    Ok((out, replaced))
}

enum InlinePlan {
    /// Today's rule: one site, so the body shares nothing.
    Single(Vec<HirStmt>),
    /// The cost model duplicated a body at every site, all or nothing.
    Multi(Vec<Vec<HirStmt>>),
}

/// Splice a subroutine body into its call sites.
///
/// A single-site body could run inline, so it always pays for the jump
/// machinery without sharing anything; it is always inlined, exactly as
/// before. A body with several sites is inlined only when duplicating it
/// packs into no more static triggers than sharing it (plus
/// [`INLINE_EXTRA_TRIGGERS`]) — the cost model in [`body_triggers`] and
/// [`all_sites_in_one_adjacent_run`]. That keeps a small body's `TriggerPage`
/// argument batches and result copies out of the hot path while leaving large,
/// condition-heavy bodies (the shared full-dword reader) shared.
///
/// Parameters are substituted rather than bound: at a resolved site the call's
/// argument *is* the callee's parameter, so reading the caller's variable
/// directly reproduces the value a bind would have copied. A parameter the
/// body *writes* is only substituted when the call's destination is the same
/// variable it was bound to, so the write-back completes in place.
///
/// Multi-site inlining is all-or-nothing per function: if any site cannot be
/// substituted (a constant argument, a written parameter whose destination
/// differs, an argument the body writes) or the body fails the
/// [`multi_site_shape_ok`] whitelist, every call stays. A leaf-act body is
/// unpacked so the scheduler re-packs the copies; later copies of a retained
/// trigger re-mint their ids from one allocator seeded above the module-wide
/// maximum.
pub fn inline_single_call_funcs(module: &mut HirModule) -> Result<(), HirError> {
    let mut next_trigger_id = TriggerRefs::from_module(module).next_id();
    loop {
        let mut sites: HashMap<usize, usize> = HashMap::new();
        count_calls(&module.stmts, &mut sites);
        for func in &module.funcs {
            count_calls(&func.body, &mut sites);
        }
        let refs = TriggerRefs::from_module(module);
        let mut plan: Option<(usize, InlinePlan)> = None;
        // Keep today's single-site priority and declaration order: a body with
        // one site shares nothing, so it is always spliced first.
        for func in module.funcs.iter() {
            if sites.get(&func.id).copied() != Some(1) {
                continue;
            }
            let Some(call) = find_call(&module.stmts, func.id)
                .or_else(|| {
                    module
                        .funcs
                        .iter()
                        .find_map(|f| find_call(&f.body, func.id))
                })
                .cloned()
            else {
                continue;
            };
            if let Some(body) = inline_body(func, &call) {
                plan = Some((func.id, InlinePlan::Single(body)));
                break;
            }
        }
        if plan.is_none() {
            for func in module.funcs.iter() {
                let Some(site_count) = sites.get(&func.id).copied().filter(|n| *n >= 2) else {
                    continue;
                };
                if let Some(bodies) = multi_site_bodies(module, func, site_count, &refs) {
                    plan = Some((func.id, InlinePlan::Multi(bodies)));
                    break;
                }
            }
        }
        let Some((func_id, plan)) = plan else {
            break;
        };
        let replaced = apply_plan(module, func_id, plan, &mut next_trigger_id)?;
        if replaced == 0 {
            break;
        }
        module.funcs.retain(|f| f.id != func_id);
    }
    Ok(())
}

/// Compute every call site's inlined body when the cost model selects
/// duplication. `None` keeps the function shared.
fn multi_site_bodies(
    module: &HirModule,
    func: &eudkit_hir::HirFunc,
    site_count: usize,
    refs: &TriggerRefs,
) -> Option<Vec<Vec<HirStmt>>> {
    if !multi_site_shape_ok(&func.body, refs) {
        return None;
    }
    let retained = body_triggers(&func.body, refs);
    let shared = retained + site_count;
    let adjacent = all_sites_in_one_adjacent_run(&module.stmts, func.id, site_count)
        || module
            .funcs
            .iter()
            .filter(|f| f.id != func.id)
            .any(|f| all_sites_in_one_adjacent_run(&f.body, func.id, site_count));
    let inline = match leaf_act_count(&func.body, refs) {
        // Only adjacent sites with nothing but bare acts between them pack
        // across copies; a separator trigger ends the pending act run.
        Some(acts) if adjacent => site_count.saturating_mul(acts).div_ceil(eudkit_lir::ACT_COUNT),
        _ => site_count.saturating_mul(retained),
    };
    if inline > shared + INLINE_EXTRA_TRIGGERS {
        return None;
    }

    let mut calls = Vec::new();
    collect_calls(&module.stmts, func.id, &mut calls);
    for other in &module.funcs {
        if other.id != func.id {
            collect_calls(&other.body, func.id, &mut calls);
        }
    }
    if calls.len() != site_count {
        return None;
    }

    let leaf = leaf_act_count(&func.body, refs).is_some();
    let mut bodies = Vec::with_capacity(calls.len());
    for call in &calls {
        let body = inline_body(func, call)?;
        // The whitelist has no `VarTransfer`, so a trailing one is the
        // appended unmodified-parameter return: it adds a value move the
        // static estimate does not count.
        if matches!(body.last(), Some(HirStmt::VarTransfer(_))) {
            return None;
        }
        bodies.push(if leaf { unpack_leaf_acts(body, refs) } else { body });
    }
    Some(bodies)
}

fn apply_plan(
    module: &mut HirModule,
    func_id: usize,
    plan: InlinePlan,
    next_trigger_id: &mut usize,
) -> Result<usize, HirError> {
    match plan {
        InlinePlan::Single(body) => {
            let inlined = replace_call(&mut module.stmts, func_id, &body) || {
                let mut hit = false;
                for f in &mut module.funcs {
                    if f.id != func_id && replace_call(&mut f.body, func_id, &body) {
                        hit = true;
                        break;
                    }
                }
                hit
            };
            Ok(usize::from(inlined))
        }
        InlinePlan::Multi(mut bodies) => {
            // The first copy keeps the body's ids; every later copy re-mints
            // its retained triggers so two `HirTrigger`s never share an id.
            for body in bodies.iter_mut().skip(1) {
                remint_trigger_ids(body, next_trigger_id);
            }
            let mut ids = HashSet::new();
            for body in &bodies {
                collect_unique_trigger_ids(body, &mut ids)?;
            }
            let site_count = bodies.len();
            let mut iter = bodies.into_iter();
            let (root, mut replaced) =
                replace_all_sites(std::mem::take(&mut module.stmts), func_id, &mut iter)?;
            module.stmts = root;
            for func in &mut module.funcs {
                if func.id == func_id {
                    continue;
                }
                let (body, n) =
                    replace_all_sites(std::mem::take(&mut func.body), func_id, &mut iter)?;
                func.body = body;
                replaced += n;
            }
            if replaced != site_count {
                return Err(HirError::OptimizerInvariant(
                    "every collected call site is replaced or none is",
                ));
            }
            Ok(replaced)
        }
    }
}

/// Whether `reloc` on `act` reads a tracked variable that this list already wrote.
fn alias_read_is_stale(
    act: &eudkit_hir::HirAct,
    reloc: &HirReloc,
    tracked: &HashSet<usize>,
    written: &HashSet<usize>,
) -> bool {
    reloc_reads_target(act, reloc)
        && reloc
            .target_var
            .is_some_and(|id| tracked.contains(&id) && written.contains(&id))
}

/// Check one act: reads see the values before its own write, so every read is
/// tested against the list's write history before its `dest` field is added.
fn alias_check_act(
    act: &eudkit_hir::HirAct,
    tracked: &HashSet<usize>,
    written: &mut HashSet<usize>,
) -> bool {
    if act
        .relocs
        .iter()
        .any(|reloc| alias_read_is_stale(act, reloc, tracked, written))
    {
        return true;
    }
    for reloc in &act.relocs {
        if reloc.field_offset == DEST_FIELD_OFFSET {
            if let Some(id) = reloc.target_var {
                if tracked.contains(&id) {
                    written.insert(id);
                }
            }
        }
    }
    false
}

/// Conditions only read their reloc targets.
fn alias_check_conds(
    conds: &[HirCond],
    tracked: &HashSet<usize>,
    written: &HashSet<usize>,
) -> bool {
    conds.iter().any(|cond| {
        cond.relocs.iter().any(|reloc| {
            reloc
                .target_var
                .is_some_and(|id| tracked.contains(&id) && written.contains(&id))
        })
    })
}

/// Summary read/write sets for a statement list, used for loop bodies where a
/// later iteration may observe an earlier iteration's write.
fn collect_tracked_uses(
    stmts: &[HirStmt],
    tracked: &HashSet<usize>,
    reads: &mut HashSet<usize>,
    writes: &mut HashSet<usize>,
) {
    fn cond_relocs(
        relocs: &[HirReloc],
        tracked: &HashSet<usize>,
        reads: &mut HashSet<usize>,
    ) {
        for reloc in relocs {
            if let Some(id) = reloc.target_var.filter(|id| tracked.contains(id)) {
                reads.insert(id);
            }
        }
    }
    fn act_relocs(
        act: &eudkit_hir::HirAct,
        tracked: &HashSet<usize>,
        reads: &mut HashSet<usize>,
        writes: &mut HashSet<usize>,
    ) {
        for reloc in &act.relocs {
            let Some(id) = reloc.target_var.filter(|id| tracked.contains(id)) else {
                continue;
            };
            if reloc.field_offset == DEST_FIELD_OFFSET {
                writes.insert(id);
                if reloc_reads_target(act, reloc) {
                    reads.insert(id);
                }
            } else {
                reads.insert(id);
            }
        }
    }
    for stmt in stmts {
        match stmt {
            HirStmt::Trigger(trig) => {
                for cond in &trig.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                for act in &trig.acts {
                    act_relocs(act, tracked, reads, writes);
                }
            }
            HirStmt::Act(act) => act_relocs(act, tracked, reads, writes),
            HirStmt::VarTransfer(transfer) => {
                if tracked.contains(&transfer.src_var) {
                    reads.insert(transfer.src_var);
                }
                if tracked.contains(&transfer.dst_var) {
                    if transfer.modifier != 7 {
                        reads.insert(transfer.dst_var);
                    }
                    writes.insert(transfer.dst_var);
                }
            }
            HirStmt::VarBatch(batch) => {
                for transfer in &batch.transfers {
                    if tracked.contains(&transfer.src_var) {
                        reads.insert(transfer.src_var);
                    }
                    if tracked.contains(&transfer.dst_var) {
                        if transfer.modifier != 7 {
                            reads.insert(transfer.dst_var);
                        }
                        writes.insert(transfer.dst_var);
                    }
                }
                for store in &batch.stores {
                    if tracked.contains(&store.src_var) {
                        reads.insert(store.src_var);
                    }
                }
            }
            HirStmt::VarStore(store) => {
                if tracked.contains(&store.src_var) {
                    reads.insert(store.src_var);
                }
            }
            HirStmt::Call(call) => {
                for arg in &call.args {
                    if let HirCallArg::Var { id } = arg {
                        if tracked.contains(id) {
                            reads.insert(*id);
                        }
                    }
                }
                if let Some(id) = call.dst.filter(|id| tracked.contains(id)) {
                    writes.insert(id);
                }
            }
            HirStmt::When(w) => {
                for cond in &w.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                collect_tracked_uses(&w.body, tracked, reads, writes);
                collect_tracked_uses(&w.else_body, tracked, reads, writes);
            }
            HirStmt::Once(o) => {
                for cond in &o.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                collect_tracked_uses(&o.body, tracked, reads, writes);
            }
            HirStmt::While(w) => {
                for cond in &w.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                collect_tracked_uses(&w.body, tracked, reads, writes);
            }
            HirStmt::Until(u) => {
                for cond in &u.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                if tracked.contains(&u.latch_var) {
                    reads.insert(u.latch_var);
                }
                collect_tracked_uses(&u.body, tracked, reads, writes);
            }
            HirStmt::Edge(e) => {
                for cond in &e.conds {
                    cond_relocs(&cond.relocs, tracked, reads);
                }
                for id in [e.curr_var, e.prev_var] {
                    if tracked.contains(&id) {
                        reads.insert(id);
                    }
                }
                collect_tracked_uses(&e.body, tracked, reads, writes);
            }
            HirStmt::Every(e) => {
                if tracked.contains(&e.counter_var) {
                    reads.insert(e.counter_var);
                }
                collect_tracked_uses(&e.body, tracked, reads, writes);
            }
            HirStmt::After(a) => {
                if tracked.contains(&a.counter_var) {
                    reads.insert(a.counter_var);
                }
                collect_tracked_uses(&a.body, tracked, reads, writes);
            }
        }
    }
}

/// Check an ordered statement list for a stale read: a tracked variable written
/// earlier in the list is read after that write. Nested branches are checked
/// with a cloned history and their possible writes are merged afterwards;
/// loop bodies additionally reject any tracked variable they both read and
/// write, because a later iteration may see the earlier iteration's value.
fn alias_check_list(
    stmts: &[HirStmt],
    tracked: &HashSet<usize>,
    written: &mut HashSet<usize>,
) -> bool {
    for stmt in stmts {
        match stmt {
            HirStmt::Trigger(trig) => {
                if alias_check_conds(&trig.conds, tracked, written) {
                    return true;
                }
                for act in &trig.acts {
                    if alias_check_act(act, tracked, written) {
                        return true;
                    }
                }
            }
            HirStmt::Act(act) => {
                if alias_check_act(act, tracked, written) {
                    return true;
                }
            }
            HirStmt::VarTransfer(transfer) => {
                if tracked.contains(&transfer.src_var) && written.contains(&transfer.src_var) {
                    return true;
                }
                if tracked.contains(&transfer.dst_var)
                    && transfer.modifier != 7
                    && written.contains(&transfer.dst_var)
                {
                    return true;
                }
                if tracked.contains(&transfer.dst_var) {
                    written.insert(transfer.dst_var);
                }
            }
            HirStmt::VarBatch(batch) => {
                for transfer in &batch.transfers {
                    if tracked.contains(&transfer.src_var) && written.contains(&transfer.src_var) {
                        return true;
                    }
                    if tracked.contains(&transfer.dst_var)
                        && transfer.modifier != 7
                        && written.contains(&transfer.dst_var)
                    {
                        return true;
                    }
                    if tracked.contains(&transfer.dst_var) {
                        written.insert(transfer.dst_var);
                    }
                }
                for store in &batch.stores {
                    if tracked.contains(&store.src_var) && written.contains(&store.src_var) {
                        return true;
                    }
                }
            }
            HirStmt::VarStore(store) => {
                if tracked.contains(&store.src_var) && written.contains(&store.src_var) {
                    return true;
                }
            }
            HirStmt::Call(call) => {
                for arg in &call.args {
                    if let HirCallArg::Var { id } = arg {
                        if tracked.contains(id) && written.contains(id) {
                            return true;
                        }
                    }
                }
                if let Some(id) = call.dst.filter(|id| tracked.contains(id)) {
                    written.insert(id);
                }
            }
            HirStmt::When(w) => {
                if alias_check_conds(&w.conds, tracked, written) {
                    return true;
                }
                let mut body_written = written.clone();
                if alias_check_list(&w.body, tracked, &mut body_written) {
                    return true;
                }
                let mut else_written = written.clone();
                if alias_check_list(&w.else_body, tracked, &mut else_written) {
                    return true;
                }
                // Either arm may have run.
                written.extend(body_written);
                written.extend(else_written);
            }
            HirStmt::Once(o) => {
                if alias_check_conds(&o.conds, tracked, written) {
                    return true;
                }
                let mut body_written = written.clone();
                if alias_check_list(&o.body, tracked, &mut body_written) {
                    return true;
                }
                written.extend(body_written);
            }
            HirStmt::While(w) => {
                if alias_check_conds(&w.conds, tracked, written) {
                    return true;
                }
                if alias_check_loop(&w.body, tracked, written) {
                    return true;
                }
            }
            HirStmt::Until(u) => {
                if alias_check_conds(&u.conds, tracked, written) {
                    return true;
                }
                if tracked.contains(&u.latch_var) && written.contains(&u.latch_var) {
                    return true;
                }
                if alias_check_loop(&u.body, tracked, written) {
                    return true;
                }
            }
            HirStmt::Edge(e) => {
                if alias_check_conds(&e.conds, tracked, written) {
                    return true;
                }
                for id in [e.curr_var, e.prev_var] {
                    if tracked.contains(&id) && written.contains(&id) {
                        return true;
                    }
                }
                if alias_check_loop(&e.body, tracked, written) {
                    return true;
                }
            }
            HirStmt::Every(e) => {
                if tracked.contains(&e.counter_var) && written.contains(&e.counter_var) {
                    return true;
                }
                if alias_check_loop(&e.body, tracked, written) {
                    return true;
                }
            }
            HirStmt::After(a) => {
                if tracked.contains(&a.counter_var) && written.contains(&a.counter_var) {
                    return true;
                }
                if alias_check_loop(&a.body, tracked, written) {
                    return true;
                }
            }
        }
    }
    false
}

fn alias_check_loop(
    body: &[HirStmt],
    tracked: &HashSet<usize>,
    written: &mut HashSet<usize>,
) -> bool {
    let mut body_written = written.clone();
    if alias_check_list(body, tracked, &mut body_written) {
        return true;
    }
    let mut reads = HashSet::new();
    let mut writes = HashSet::new();
    collect_tracked_uses(body, tracked, &mut reads, &mut writes);
    if reads.iter().any(|id| writes.contains(id)) {
        return true;
    }
    written.extend(body_written);
    false
}

/// Reject a substitution that would make a caller variable's own write visible
/// to a later read inside the inlined body (`x = f(x)` where `f` writes its
/// result before reading its parameter). Checked on the parameter-substituted
/// body, because the pre-substitution `VarUses` guard cannot see the
/// result→`dst` rewrite aliasing an argument with the call's destination.
///
/// Only parameters that map to `dst` can observe that write. A body-local
/// result is renamed to `dst` first; that is conservative (a read of the
/// body's own accumulating result also looks like a read of `dst`) and keeps
/// the check on the existing ordered list walk.
fn substitution_aliases_an_argument(
    body: &[HirStmt],
    func: &eudkit_hir::HirFunc,
    call: &HirCall,
    map: &HashMap<usize, usize>,
) -> bool {
    let Some(dst) = call.dst else {
        return false;
    };
    // A parameter that is also the result writes back in place; reads of its
    // own write are the body's value, not a stale caller value.
    let tracked: HashSet<usize> = func
        .params
        .iter()
        .filter(|param| func.result != Some(**param))
        .filter_map(|param| map.get(param).copied().filter(|id| *id == dst))
        .collect();
    if tracked.is_empty() {
        return false;
    }
    let mut body = body.to_vec();
    if let Some(result) = func.result.filter(|r| !func.params.contains(r) && *r != dst) {
        // The call's destination collects the body-local result: every write of
        // that result will land in `dst`.
        for stmt in &mut body {
            substitute_var(stmt, result, dst);
        }
    }
    alias_check_list(&body, &tracked, &mut HashSet::new())
}

/// Whether `stmts` reads or partially modifies `var` before unconditionally
/// overwriting `var` via a full-mask `SetTo` write. Used before rewriting a
/// body-local `result` directly to the call's `dst`.
fn body_reads_before_overwriting(stmts: &[HirStmt], var: usize) -> bool {
    fn act_reads_before_overwrite(act: &eudkit_hir::HirAct, var: usize) -> Option<bool> {
        let mut touched = false;
        for reloc in &act.relocs {
            if reloc.target_var != Some(var) {
                continue;
            }
            touched = true;
            if reloc_reads_target(act, reloc) {
                return Some(true);
            }
        }
        touched.then_some(false)
    }

    for stmt in stmts {
        match stmt {
            HirStmt::Act(act) => {
                if let Some(reads) = act_reads_before_overwrite(act, var) {
                    return reads;
                }
            }
            HirStmt::Trigger(trig) if trig.unpack_shape() => {
                for act in &trig.acts {
                    if let Some(reads) = act_reads_before_overwrite(act, var) {
                        return reads;
                    }
                }
            }
            HirStmt::VarTransfer(transfer) => {
                if transfer.src_var == var || (transfer.dst_var == var && transfer.modifier != 7) {
                    return true;
                }
                if transfer.dst_var == var && transfer.modifier == 7 {
                    return false;
                }
            }
            other => {
                let mut u = VarUses::default();
                u.collect_stmt(other);
                if u.reads.contains_key(&var)
                    || u.writes.contains(&var)
                    || u.call_dsts.contains_key(&var)
                {
                    return true;
                }
            }
        }
    }
    false
}

/// Rewrite a single-site call into the body it would have entered, or `None`
/// when the parameter substitution cannot reproduce the call exactly.
fn inline_body(func: &eudkit_hir::HirFunc, call: &HirCall) -> Option<Vec<HirStmt>> {
    let uses = analyze(&func.body);
    let mut map: HashMap<usize, usize> = HashMap::new();
    let mut written_args: HashSet<usize> = HashSet::new();
    for (param, arg) in func.params.iter().zip(call.args.iter()) {
        let HirCallArg::Var { id } = arg else {
            return None;
        };
        if uses.writes.contains(id) || uses.call_dsts.contains_key(id) {
            // Splitting the body's own var from the caller's would be required:
            // a substituted write would clobber a value the body may still read.
            return None;
        }
        if uses.writes.contains(param) || uses.call_dsts.contains_key(param) {
            // A written parameter is only safe as an in-place write-back: the
            // call's destination must be the very variable it was bound to, and
            // no other parameter may alias it.
            if func.result != Some(*param) || call.dst != Some(*id) {
                return None;
            }
            written_args.insert(*id);
        }
        map.insert(*param, *id);
    }
    // The substituted variable must not otherwise appear in the body: a
    // write-back would then be visible to the body itself.
    for id in written_args {
        if uses.reads.contains_key(&id) || uses.call_dsts.contains_key(&id) {
            return None;
        }
        if func.params.iter().filter(|p| map.get(p) == Some(&id)).count() > 1 {
            return None;
        }
    }

    let mut body = func.body.clone();
    for stmt in &mut body {
        substitute_map(stmt, &map);
    }
    // The `VarUses` guard above inspects the pre-substitution body, so it
    // cannot see the result→`dst` rewrite aliasing the destination with an
    // argument. Check the ordered body the inline would actually emit.
    if substitution_aliases_an_argument(&body, func, call, &map) {
        return None;
    }
    match (func.result, call.dst) {
        (Some(result), Some(dst)) => {
            // A write-back parameter keeps its caller-side variable; a
            // body-local result is rewritten to the call destination.
            let written_back = func.params.contains(&result);
            let result = map.get(&result).copied().unwrap_or(result);
            if result != dst {
                if written_back {
                    // A written-back parameter already landed in its argument
                    // variable; hand it to the call's destination explicitly.
                    body.push(HirStmt::VarTransfer(eudkit_hir::HirVarTransfer {
                        dst_var: dst,
                        src_var: result,
                        modifier: 7,
                    }));
                } else if body_reads_before_overwriting(&body, result) {
                    // Rewriting `result -> dst` directly would read `dst`'s
                    // pre-call value instead of `result`'s initial value.
                    return None;
                } else {
                    // A body-local result (the common read kernel): let the
                    // accumulating statements write the call's destination.
                    for stmt in &mut body {
                        substitute_var(stmt, result, dst);
                    }
                }
            }
        }
        (Some(result), None) => {
            // A result nobody reads: drop the dead accumulator writes by
            // leaving them alone (identical to the call's result landing).
            let _ = result;
        }
        _ => {}
    }
    Some(body)
}

/// Reads and writes of one statement list, used to decide whether a parameter
/// may be substituted by the call's argument.
fn analyze(stmts: &[HirStmt]) -> VarUses {
    let mut uses = VarUses::default();
    for stmt in stmts {
        uses.collect_stmt(stmt);
    }
    uses
}

fn find_call<'a>(stmts: &'a [HirStmt], func_id: usize) -> Option<&'a HirCall> {
    for stmt in stmts {
        let found = match stmt {
            HirStmt::Call(c) if c.func == func_id => return Some(c),
            HirStmt::When(w) => find_call(&w.body, func_id)
                .or_else(|| find_call(&w.else_body, func_id)),
            HirStmt::Once(o) => find_call(&o.body, func_id),
            HirStmt::While(w) => find_call(&w.body, func_id),
            HirStmt::Until(u) => find_call(&u.body, func_id),
            HirStmt::Edge(e) => find_call(&e.body, func_id),
            HirStmt::Every(e) => find_call(&e.body, func_id),
            HirStmt::After(a) => find_call(&a.body, func_id),
            _ => None,
        };
        if found.is_some() {
            return found;
        }
    }
    None
}

fn count_calls(stmts: &[HirStmt], sites: &mut HashMap<usize, usize>) {
    for stmt in stmts {
        match stmt {
            HirStmt::Call(c) => *sites.entry(c.func).or_insert(0) += 1,
            HirStmt::When(w) => {
                count_calls(&w.body, sites);
                count_calls(&w.else_body, sites);
            }
            HirStmt::Once(o) => count_calls(&o.body, sites),
            HirStmt::While(w) => count_calls(&w.body, sites),
            HirStmt::Until(u) => count_calls(&u.body, sites),
            HirStmt::Edge(e) => count_calls(&e.body, sites),
            HirStmt::Every(e) => count_calls(&e.body, sites),
            HirStmt::After(a) => count_calls(&a.body, sites),
            _ => {}
        }
    }
}

fn replace_call(stmts: &mut Vec<HirStmt>, func_id: usize, body: &[HirStmt]) -> bool {
    let mut i = 0;
    while i < stmts.len() {
        let hit = matches!(&stmts[i], HirStmt::Call(c) if c.func == func_id);
        if hit {
            stmts.splice(i..=i, body.iter().cloned());
            return true;
        }
        let nested = match &mut stmts[i] {
            HirStmt::When(w) => {
                replace_call(&mut w.body, func_id, body)
                    || replace_call(&mut w.else_body, func_id, body)
            }
            HirStmt::Once(o) => replace_call(&mut o.body, func_id, body),
            HirStmt::While(w) => replace_call(&mut w.body, func_id, body),
            HirStmt::Until(u) => replace_call(&mut u.body, func_id, body),
            HirStmt::Edge(e) => replace_call(&mut e.body, func_id, body),
            HirStmt::Every(e) => replace_call(&mut e.body, func_id, body),
            HirStmt::After(a) => replace_call(&mut a.body, func_id, body),
            _ => false,
        };
        if nested {
            return true;
        }
        i += 1;
    }
    false
}

fn substitute_map(stmt: &mut HirStmt, map: &HashMap<usize, usize>) {
    for (from, to) in map {
        substitute_var(stmt, *from, *to);
    }
}

fn substitute_var(stmt: &mut HirStmt, from: usize, to: usize) {
    fn relocs(relocs: &mut Vec<eudkit_hir::HirReloc>, from: usize, to: usize) {
        for r in relocs {
            if r.target_var == Some(from) {
                r.target_var = Some(to);
            }
        }
    }
    fn conds(conds: &mut [eudkit_hir::HirCond], from: usize, to: usize) {
        for c in conds {
            relocs(&mut c.relocs, from, to);
        }
    }
    match stmt {
        HirStmt::Trigger(t) => {
            conds(&mut t.conds, from, to);
            for a in &mut t.acts {
                relocs(&mut a.relocs, from, to);
            }
        }
        HirStmt::Act(a) => relocs(&mut a.relocs, from, to),
        HirStmt::VarTransfer(t) => {
            if t.dst_var == from {
                t.dst_var = to;
            }
            if t.src_var == from {
                t.src_var = to;
            }
        }
        HirStmt::VarBatch(b) => {
            for t in &mut b.transfers {
                if t.dst_var == from {
                    t.dst_var = to;
                }
                if t.src_var == from {
                    t.src_var = to;
                }
            }
            for s in &mut b.stores {
                if s.src_var == from {
                    s.src_var = to;
                }
            }
        }
        HirStmt::VarStore(s) => {
            if s.src_var == from {
                s.src_var = to;
            }
        }
        HirStmt::Call(c) => {
            if c.dst == Some(from) {
                c.dst = Some(to);
            }
            for arg in &mut c.args {
                if let eudkit_hir::HirCallArg::Var { id } = arg {
                    if *id == from {
                        *id = to;
                    }
                }
            }
        }
        HirStmt::When(w) => {
            conds(&mut w.conds, from, to);
            for s in &mut w.body {
                substitute_var(s, from, to);
            }
            for s in &mut w.else_body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::Once(o) => {
            conds(&mut o.conds, from, to);
            for s in &mut o.body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::While(w) => {
            conds(&mut w.conds, from, to);
            for s in &mut w.body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::Until(u) => {
            conds(&mut u.conds, from, to);
            if u.latch_var == from {
                u.latch_var = to;
            }
            for s in &mut u.body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::Edge(e) => {
            conds(&mut e.conds, from, to);
            if e.curr_var == from {
                e.curr_var = to;
            }
            if e.prev_var == from {
                e.prev_var = to;
            }
            for s in &mut e.body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::Every(e) => {
            if e.counter_var == from {
                e.counter_var = to;
            }
            for s in &mut e.body {
                substitute_var(s, from, to);
            }
        }
        HirStmt::After(a) => {
            if a.counter_var == from {
                a.counter_var = to;
            }
            for s in &mut a.body {
                substitute_var(s, from, to);
            }
        }
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{
        HirAct, HirCond, HirFunc, HirOnce, HirReloc, HirTrigger, HirTriggerPtr, HirVarDecl,
        HirVarStore, HirVarTransfer,
    };
    use eudkit_lir::{EUDX_MAGIC, VALUE_FIELD_OFFSET};

    fn test_module(funcs: Vec<HirFunc>, stmts: Vec<HirStmt>) -> HirModule {
        HirModule {
            vars: (0..8).map(|id| HirVarDecl::new(id, 0)).collect(),
            stmts,
            funcs,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        }
    }

    fn read_act(var: usize) -> HirStmt {
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.relocs.push(HirReloc::var(
            VALUE_FIELD_OFFSET,
            var,
            VALUE_FIELD_OFFSET,
            7,
        ));
        HirStmt::Act(act)
    }

    /// An act that writes `var`'s value word through its destination field.
    fn write_act(var: usize) -> HirStmt {
        let mut act = HirAct::set_deaths(0, 7, 99, 0);
        act.relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, var, VALUE_FIELD_OFFSET, 7));
        HirStmt::Act(act)
    }

    /// An act that patches `var`'s machinery field instead of its value word.
    fn machinery_dest_act(var: usize) -> HirAct {
        let mut act = HirAct::set_deaths(0, 7, 0, 0);
        act.relocs.push(HirReloc::var(DEST_FIELD_OFFSET, var, 0, 7));
        act
    }

    /// A `SetDeathsX` dest write with `mask == 0`, a 0-bit no-op in LIR.
    fn eudx_zero_mask_dest_act(var: usize) -> HirAct {
        let mut act = HirAct::set_deaths(0, 7, 1, 0);
        act.eudx = EUDX_MAGIC;
        act.mask = 0;
        act.relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, var, VALUE_FIELD_OFFSET, 7));
        act
    }

    /// A module with one function `(param) -> result` and one call site.
    fn single_site_module(func_id: usize, body: Vec<HirStmt>, arg: usize, dst: usize) -> HirModule {
        test_module(
            vec![HirFunc::new(func_id, vec![1], Some(2), body)],
            vec![HirStmt::Call(HirCall::new(
                func_id,
                vec![HirCallArg::var(arg)],
                Some(dst),
            ))],
        )
    }

    fn transfer(dst_var: usize, src_var: usize) -> HirStmt {
        HirStmt::VarTransfer(HirVarTransfer {
            dst_var,
            src_var,
            modifier: 7,
        })
    }

    fn call_ids(stmts: &[HirStmt]) -> Vec<usize> {
        stmts
            .iter()
            .filter_map(|s| match s {
                HirStmt::Call(c) => Some(c.func),
                _ => None,
            })
            .collect()
    }

    /// A bare, reloc-free act, the shape a frontend body is unpacked into.
    fn bare_act(value: u32) -> HirStmt {
        HirStmt::Act(HirAct::set_deaths(0, 7, value, 0))
    }

    /// An unconditional preserved flag-`4` block, the shape the frontend
    /// flushes a function body's acts into before opt runs.
    fn unconditional_block(id: usize, acts: Vec<HirAct>) -> HirStmt {
        HirStmt::Trigger(HirTrigger::new(id, Vec::new(), acts, true).expect("legal Trigger"))
    }

    /// A conditional preserved block: retained as a node, never unpackable.
    fn conditional_block(id: usize, act: HirAct) -> HirStmt {
        HirStmt::Trigger(
            HirTrigger::new(id, vec![HirCond::always()], vec![act], true)
                .expect("legal Trigger"),
        )
    }

    fn body_of_n_acts(n: usize) -> Vec<HirStmt> {
        (0..n as u32).map(bare_act).collect()
    }

    #[test]
    fn fuses_a_call_result_into_the_transfer_that_consumes_it() {
        let mut module = test_module(
            Vec::new(),
            vec![
                HirStmt::Call(HirCall::new(9, Vec::new(), Some(4))),
                transfer(7, 4),
            ],
        );
        fuse_call_results(&mut module);
        assert_eq!(module.stmts.len(), 1, "the result copy is gone");
        match &module.stmts[0] {
            HirStmt::Call(c) => assert_eq!(c.dst, Some(7)),
            other => panic!("expected the fused call, got {other:?}"),
        }
    }

    #[test]
    fn substitutes_parameter_reads_at_a_single_call_site() {
        let module = test_module(
            vec![HirFunc::new(10, vec![1], Some(3), vec![read_act(1)])],
            vec![HirStmt::Call(HirCall::new(
                10,
                vec![HirCallArg::var(2)],
                Some(4),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty(), "the single-site body is inlined");
        assert!(call_ids(&module.stmts).is_empty());
        match &module.stmts[0] {
            HirStmt::Act(a) => {
                assert_eq!(a.relocs[0].target_var, Some(2), "reads the argument");
            }
            other => panic!("expected the inlined body, got {other:?}"),
        }
    }

    #[test]
    fn a_written_result_parameter_writes_back_in_place() {
        let module = test_module(
            vec![HirFunc::new(11, vec![1], Some(1), vec![transfer(1, 6)])],
            vec![HirStmt::Call(HirCall::new(
                11,
                vec![HirCallArg::var(2)],
                Some(2),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty());
        assert_eq!(module.stmts.len(), 1, "no extra result transfer");
        match &module.stmts[0] {
            HirStmt::VarTransfer(t) => {
                assert_eq!((t.dst_var, t.src_var), (2, 6), "writes the argument");
            }
            other => panic!("expected the inlined write-back, got {other:?}"),
        }
    }

    #[test]
    fn a_written_parameter_keeps_the_call_when_the_destination_differs() {
        let module = test_module(
            vec![HirFunc::new(11, vec![1], Some(1), vec![transfer(1, 6)])],
            vec![HirStmt::Call(HirCall::new(
                11,
                vec![HirCallArg::var(2)],
                Some(5),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "the write-back cannot be in place");
        assert_eq!(call_ids(&module.stmts), vec![11]);
    }

    #[test]
    fn a_written_parameter_keeps_the_call_when_the_body_also_reads_the_argument() {
        let module = test_module(
            vec![HirFunc::new(
                11,
                vec![1],
                Some(1),
                vec![transfer(1, 6), read_act(2)],
            )],
            vec![HirStmt::Call(HirCall::new(
                11,
                vec![HirCallArg::var(2)],
                Some(2),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "the body would observe its own write");
        assert_eq!(call_ids(&module.stmts), vec![11]);
    }

    #[test]
    fn a_small_body_at_several_call_sites_inlines() {
        let module = test_module(
            vec![HirFunc::new(12, vec![1], Some(3), vec![read_act(1)])],
            vec![
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(2)], Some(4))),
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(5)], Some(6))),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty(), "a one-act body at two sites inlines");
        assert!(call_ids(&module.stmts).is_empty());
        assert_eq!(module.stmts.len(), 2);
        let targets: Vec<Option<usize>> = module
            .stmts
            .iter()
            .map(|s| match s {
                HirStmt::Act(a) => a.relocs[0].target_var,
                other => panic!("expected the substituted act, got {other:?}"),
            })
            .collect();
        assert_eq!(targets, vec![Some(2), Some(5)], "each site binds its argument");
    }

    #[test]
    fn a_leaf_run_is_unpacked_before_insertion() {
        let block = unconditional_block(
            50,
            vec![HirAct::set_deaths(0, 7, 1, 0), HirAct::set_deaths(0, 7, 2, 0)],
        );
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![block])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty(), "the two-act body inlines");
        assert!(
            module.stmts.iter().all(|s| matches!(s, HirStmt::Act(_))),
            "every unpackable body trigger becomes bare acts: {:?}",
            module.stmts
        );
        assert_eq!(module.stmts.len(), 4, "both copies' acts are bare");
    }

    #[test]
    fn a_large_body_with_several_call_sites_stays_shared() {
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, body_of_n_acts(200))],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "a 200-act body stays shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_constant_argument_keeps_a_multi_site_call_shared() {
        let module = test_module(
            vec![HirFunc::new(13, vec![1], Some(3), vec![read_act(1)])],
            vec![
                HirStmt::Call(HirCall::new(13, vec![HirCallArg::var(2)], Some(4))),
                HirStmt::Call(HirCall::new(13, vec![HirCallArg::constant(9)], Some(4))),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "all-or-nothing keeps every site");
        assert_eq!(call_ids(&module.stmts), vec![13, 13]);
    }

    #[test]
    fn multi_site_trigger_ids_are_unique() {
        let block = conditional_block(7, HirAct::set_deaths(0, 7, 1, 0));
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![block])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        let ids: Vec<usize> = module
            .stmts
            .iter()
            .filter_map(|s| match s {
                HirStmt::Trigger(t) => Some(t.id),
                _ => None,
            })
            .collect();
        assert_eq!(ids.len(), 2, "both retained copies are present");
        let unique: HashSet<usize> = ids.iter().copied().collect();
        assert_eq!(unique.len(), ids.len(), "retained ids must be re-minted");
        module.validate().expect("the inlined module is valid");
    }

    #[test]
    fn a_multi_site_body_with_once_stays_shared() {
        let once = HirStmt::Once(HirOnce::new(
            vec![HirCond::always()],
            vec![bare_act(1)],
        ));
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![once])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1);
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_multi_site_body_with_a_trigger_reference_stays_shared() {
        let mut chained = match conditional_block(7, HirAct::set_deaths(0, 7, 1, 0)) {
            HirStmt::Trigger(t) => t,
            other => panic!("expected a Trigger, got {other:?}"),
        };
        chained.next = Some(HirTriggerPtr::Trigger { id: 8, offset: 0 });
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![HirStmt::Trigger(chained)])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "a chain pointer keeps the body shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_destination_bound_argument_keeps_a_multi_site_call_shared() {
        // `x = f(x)` where the body writes its result before reading its
        // parameter: substituting the parameter with `x` would let the read
        // observe the result write once the result lands in `x`.
        let module = test_module(
            vec![HirFunc::new(
                12,
                vec![1],
                Some(2),
                vec![write_act(2), read_act(1)],
            )],
            vec![
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(3)], Some(3))),
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(3)], Some(3))),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "a stale argument read keeps it shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_destination_bound_argument_inlines_when_the_read_comes_first() {
        // The same aliasing shape, but reading the parameter before the result
        // write is the original call's order and inlines unchanged.
        let module = test_module(
            vec![HirFunc::new(
                12,
                vec![1],
                Some(2),
                vec![read_act(1), write_act(2)],
            )],
            vec![
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(3)], Some(3))),
                HirStmt::Call(HirCall::new(12, vec![HirCallArg::var(3)], Some(3))),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty(), "the ordered body inlines");
        assert!(call_ids(&module.stmts).is_empty());
        let targets: Vec<Option<usize>> = module
            .stmts
            .iter()
            .map(|s| match s {
                HirStmt::Act(a) => a.relocs[0].target_var,
                other => panic!("expected a substituted act, got {other:?}"),
            })
            .collect();
        assert_eq!(targets, vec![Some(3), Some(3), Some(3), Some(3)]);
    }

    #[test]
    fn a_multi_site_body_with_a_preserved_false_trigger_stays_shared() {
        let block = HirStmt::Trigger(
            HirTrigger::new(7, Vec::new(), vec![HirAct::set_deaths(0, 7, 1, 0)], false)
                .expect("legal Trigger"),
        );
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![block])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "a self-disabling trigger stays shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_referenced_body_trigger_keeps_the_func_shared() {
        let block = unconditional_block(7, vec![HirAct::set_deaths(0, 7, 1, 0)]);
        let mut referencing = HirAct::set_deaths(0, 7, 0, 0);
        referencing.relocs.push(HirReloc::trigger(20, 7, 0, 4));
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![block])],
            vec![
                HirStmt::Act(referencing),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "an external trigger reference keeps it shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_parameter_result_with_a_different_destination_stays_shared() {
        // The body returns its unmodified parameter, so `inline_body` would
        // append a result `VarTransfer` when the call's destination differs.
        let module = test_module(
            vec![HirFunc::new(12, vec![1], Some(1), vec![read_act(1)])],
            vec![
                HirStmt::Call(HirCall::new(
                    12,
                    vec![HirCallArg::var(2)],
                    Some(2),
                )),
                HirStmt::Call(HirCall::new(
                    12,
                    vec![HirCallArg::var(5)],
                    Some(6),
                )),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "an appended result transfer rejects both sites");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_multi_site_body_with_a_var_store_stays_shared() {
        let store = HirStmt::VarStore(HirVarStore::to_addr(2, 0x640B58, 7));
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![store])],
            vec![
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
                HirStmt::Call(HirCall::new(12, Vec::new(), None)),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1, "a store body stays shared");
        assert_eq!(call_ids(&module.stmts), vec![12, 12]);
    }

    #[test]
    fn a_single_site_body_with_once_still_inlines() {
        let once = HirStmt::Once(HirOnce::new(
            vec![HirCond::always()],
            vec![bare_act(1)],
        ));
        let module = test_module(
            vec![HirFunc::new(12, Vec::new(), None, vec![once])],
            vec![HirStmt::Call(HirCall::new(12, Vec::new(), None))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert!(module.funcs.is_empty(), "single-site behavior is unchanged");
        assert!(call_ids(&module.stmts).is_empty());
    }

    #[test]
    fn a_constant_argument_keeps_the_call() {
        let module = test_module(
            vec![HirFunc::new(13, vec![1], Some(3), vec![read_act(1)])],
            vec![HirStmt::Call(HirCall::new(
                13,
                vec![HirCallArg::constant(9)],
                Some(4),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(module.funcs.len(), 1);
        assert_eq!(call_ids(&module.stmts), vec![13]);
    }

    #[test]
    fn a_parameter_mutated_by_act_dest_reloc_keeps_the_call() {
        let mut add_param = HirAct::set_deaths(0, 8, 5, 0);
        add_param
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 1, VALUE_FIELD_OFFSET, 7));
        let module = test_module(
            vec![HirFunc::new(
                14,
                vec![1],
                Some(2),
                vec![HirStmt::Act(add_param), write_act(2)],
            )],
            vec![HirStmt::Call(HirCall::new(
                14,
                vec![HirCallArg::var(3)],
                Some(4),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(
            module.funcs.len(),
            1,
            "mutating a non-result param via HirAct dest reloc must not clobber caller var"
        );
        assert_eq!(call_ids(&module.stmts), vec![14]);
    }

    #[test]
    fn a_body_local_result_with_rmw_before_overwrite_keeps_the_call() {
        let mut rmw_result = HirAct::set_deaths(0, 8, 5, 0);
        rmw_result
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 2, VALUE_FIELD_OFFSET, 7));
        let module = test_module(
            vec![HirFunc::new(
                15,
                vec![1],
                Some(2),
                vec![read_act(1), HirStmt::Act(rmw_result)],
            )],
            vec![HirStmt::Call(HirCall::new(
                15,
                vec![HirCallArg::var(3)],
                Some(4),
            ))],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(
            module.funcs.len(),
            1,
            "RMW on body-local result must not read dst's pre-call value"
        );
        assert_eq!(call_ids(&module.stmts), vec![15]);
    }

    #[test]
    fn a_destination_bound_argument_keeps_multi_site_when_rmw_touches_result_and_param() {
        // `x = f(x)` where `f(p)` writes `r` first (`SetTo`), then does `r += 5`,
        // then reads `p`: the `r += 5` act reads+writes `r` (aliased to `x`),
        // which must not bypass stale-read detection.
        let mut rmw_result = HirAct::set_deaths(0, 8, 5, 0);
        rmw_result
            .relocs
            .push(HirReloc::var(DEST_FIELD_OFFSET, 2, VALUE_FIELD_OFFSET, 7));
        let module = test_module(
            vec![HirFunc::new(
                16,
                vec![1],
                Some(2),
                vec![write_act(2), HirStmt::Act(rmw_result)],
            )],
            vec![
                HirStmt::Call(HirCall::new(16, vec![HirCallArg::var(3)], Some(3))),
                HirStmt::Call(HirCall::new(16, vec![HirCallArg::var(3)], Some(3))),
            ],
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(
            module.funcs.len(),
            1,
            "RMW after write_act on aliased destination keeps the call shared"
        );
        assert_eq!(call_ids(&module.stmts), vec![16, 16]);
    }

    /// A `DEST_FIELD_OFFSET` reloc that targets a machinery field (offset 0,
    /// not the value word) is not an overwrite of the target's value. A later
    /// value read therefore still happens before the result's value word is
    /// overwritten.
    #[test]
    fn machinery_dest_reloc_is_not_a_value_overwrite() {
        let body = vec![
            HirStmt::Act(machinery_dest_act(2)),
            read_act(2),
            write_act(2),
        ];

        assert!(
            body_reads_before_overwriting(&body, 2),
            "patching result's machinery field must not hide the later value read"
        );
    }

    /// `call(x) -> y` where the body patches machinery on its result before
    /// reading the result's value must keep the call: inline-aliasing
    /// `result -> dst` would read the caller's stale value.
    #[test]
    fn machinery_dest_reloc_before_value_read_keeps_call() {
        let module = single_site_module(
            20,
            vec![
                HirStmt::Act(machinery_dest_act(2)),
                read_act(2),
                write_act(2),
            ],
            3,
            4,
        );
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(
            module.funcs.len(),
            1,
            "a machinery dest patch is not a value overwrite, so the result is read first"
        );
        assert_eq!(call_ids(&module.stmts), vec![20]);
    }

    /// A `SetDeathsX` (`eudx == EUDX_MAGIC`) with `mask == 0` writes no bits:
    /// the target keeps its value, so the act reads it. Inline-aliasing the
    /// body-local result onto an argument must stay rejected.
    #[test]
    fn eudx_zero_mask_dest_write_reads_target_and_keeps_call() {
        let module = single_site_module(21, vec![HirStmt::Act(eudx_zero_mask_dest_act(2))], 3, 4);
        let mut module = module;
        inline_single_call_funcs(&mut module).expect("legal inline");
        assert_eq!(
            module.funcs.len(),
            1,
            "a 0-bit masked dest write reads the aliased argument's value"
        );
        assert_eq!(call_ids(&module.stmts), vec![21]);
    }
}
