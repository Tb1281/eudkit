//! Block-wide statement scheduler for HIR 3.2.
//!
//! One recursive scheduler replaces the four partial passes that used to
//! decide this piecemeal: the root-only [`PendingRoot`] run (ticket #60), the
//! body-only `coalesce_transfer_runs` pass, the per-statement `VarStore`
//! lowering split, and the root-only act bin-packer. Every statement list —
//! the module root, every `when`/`else`, `once`, and `while` body, and every
//! desugared temporal expansion — is scheduled by [`schedule_block`] with the
//! same rules.
//!
//! # The schedule of a block
//!
//! A block is a sequence of *members* (`VarTransfer`, `VarStore`, or an
//! existing `VarBatch`) and *acts* (`HirStmt::Act`, or the acts of an
//! unconditional flag-`4` block). All members of an open run share one
//! `TriggerPage` firing: each patches its own source slot in the setup, the
//! page fires once, and the restore acts continue the chain. The cost is one
//! setup trigger, one page fire, and one restore trigger regardless of member
//! count, which is why coalescing `x=y+1; z=w+1` and a run of `VarStore`s
//! matters.
//!
//! Membership is decided on [`TransferPlan`]s, the same plan shape lowering
//! consumes, so a store and a transfer are policed by one legality rule
//! ([`transfer::can_coalesce_plan`]): distinct sources, no RAW, no WAR, only
//! additive WAW, and one destination word written once. A store's destination
//! is not a variable slot, so its only hazard surface is its source read and
//! the destination word's write-once rule.
//!
//! # Acts around a firing
//!
//! Acts that an open run can ignore stay *ahead* of the batch, exactly as the
//! pre-#96 root pass emitted them, so lowering folds them into the setup
//! trigger (`<= 64A` including the page-set-next act). An act that the run
//! cannot ignore — it reads or writes a variable the firing writes, or writes
//! a variable the firing reads — is emitted *after* the batch instead, where
//! lowering places it in the restore continuation. Both sides are packed by
//! lowering at `ACT_COUNT` per trigger.
//!
//! An act is independent of an open run when, for every scalar it names:
//! - it does not read or write a transfer destination (the firing changes it),
//! - it does not write a transfer or store source (the firing reads it), and
//! - it does not touch a page-machinery field of a member (`.dest`,
//!   `.modifier`, `.mask`) — only plain value-field accesses are reorderable.
//! A later member may only join a run whose pending after-acts do not touch
//! it: the after-acts run after this firing but before that member's source
//! would be read.

use std::collections::BTreeSet;
use std::mem;

use eudkit_hir::{
    HirAct, HirError, HirModule, HirStmt, HirTrigger, HirVarBatch, HirVarStore, HirVarTransfer,
};
use eudkit_lir::ACT_COUNT;

use crate::facts::{ActEffects, StorageFacts};
use crate::transfer::{TransferDest, TransferPlan, can_coalesce_plan, plan_var_store};

/// Which statement list is being scheduled.
///
/// The coalescing rules are identical for both kinds; only the *shape* of the
/// emitted act run differs: the module root packages its bare acts into
/// synthetic `Trigger` statements (the pre-#96 root bin-packer), while a
/// control-flow body leaves them as `HirStmt::Act` statements for lowering's
/// basic-block packer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum BlockKind {
    Root,
    Body,
}

/// The scheduler's view of the shared scalar-storage facts.
///
/// [`StorageFacts`] is the single pin home: declarations, transfer/store
/// sources, `Call` arguments, subroutine params/results, and every nested body
/// are walked exactly once there. The scheduler decides *membership and order*,
/// never page/slot layout; lowering owns the ranks, parking rules, and the
/// setup/restore act count.
///
/// This wrapper exposes two things to the ordering rules:
///
/// - the declared mask, so the plan shape built here is the same
///   [`TransferPlan`] lowering re-derives from the same declarations, and
/// - the shared facts themselves, which [`ActEffects::of`] queries for
///   page-backedness. Only a page-backed variable has the
///   `.dest`/`.modifier`/`.mask` machinery fields a firing patches; a
///   cell-backed scalar is one 4-byte word, so a field relocation to it is
///   just a value access.
///
/// Scheduling only regroups statements, so this matches the page-backed set
/// `analyze_storage` later derives from the scheduled module.
#[derive(Debug, Default, Clone)]
pub(crate) struct CellMap {
    facts: StorageFacts,
}

impl CellMap {
    /// Collect the storage facts of `module` with the shared walk.
    pub(crate) fn from_module(module: &HirModule) -> Self {
        Self {
            facts: StorageFacts::from_module(module),
        }
    }

    /// The facts available from a bare statement list: no declarations, so
    /// no pinned storage and every declared mask treated as full.
    ///
    /// Only the unit tests that drive [`super::temporal::optimize_stmts`]
    /// without a module need this; production code always goes through
    /// [`CellMap::from_module`].
    #[cfg(test)]
    pub(crate) fn from_stmts(stmts: &[HirStmt]) -> Self {
        Self {
            facts: StorageFacts::from_stmts(stmts),
        }
    }

    /// Declared mask of `var_id`; unrecorded means full, as lowering assumes.
    pub(crate) fn mask(&self, var_id: usize) -> u32 {
        self.facts.mask(var_id)
    }

    pub(crate) fn facts(&self) -> &StorageFacts {
        &self.facts
    }
}

/// The open run of members waiting for one `TriggerPage` firing.
#[derive(Debug, Default)]
struct PendingBatch {
    plans: Vec<TransferPlan>,
    transfers: Vec<HirVarTransfer>,
    stores: Vec<HirVarStore>,
    /// Acts that must run after the firing.
    after: Vec<HirAct>,
    srcs: BTreeSet<usize>,
    dsts: BTreeSet<usize>,
}

impl PendingBatch {
    fn is_empty(&self) -> bool {
        self.plans.is_empty()
    }
}

/// Recursive block scheduler. See the module docs for the rules.
pub(crate) struct BlockScheduler<'a> {
    cells: &'a CellMap,
    kind: BlockKind,
    next_id: &'a mut usize,
    out: Vec<HirStmt>,
    /// Acts that stay ahead of the open run.
    pre: Vec<HirAct>,
    batch: PendingBatch,
}

impl<'a> BlockScheduler<'a> {
    pub(crate) fn new(cells: &'a CellMap, kind: BlockKind, next_id: &'a mut usize) -> Self {
        Self {
            cells,
            kind,
            next_id,
            out: Vec::new(),
            pre: Vec::new(),
            batch: PendingBatch::default(),
        }
    }

    /// Schedule one statement list.
    pub(crate) fn schedule(mut self, stmts: &[HirStmt]) -> Result<Vec<HirStmt>, HirError> {
        for stmt in stmts {
            match stmt {
                HirStmt::VarTransfer(t) => self.push_transfer(t)?,
                HirStmt::VarStore(s) => self.push_store(s)?,
                HirStmt::VarBatch(b) => {
                    for t in &b.transfers {
                        self.push_transfer(t)?;
                    }
                    for s in &b.stores {
                        self.push_store(s)?;
                    }
                }
                HirStmt::Act(act) => self.push_act(act)?,
                HirStmt::Trigger(trig) => self.push_trigger(trig)?,
                other => {
                    self.flush()?;
                    self.out.push(other.clone());
                }
            }
        }
        self.flush()?;
        Ok(mem::take(&mut self.out))
    }

    fn push_transfer(&mut self, t: &HirVarTransfer) -> Result<(), HirError> {
        let plan = TransferPlan {
            src_var: t.src_var,
            dst: TransferDest::Var(t.dst_var),
            modifier: t.modifier,
            src_mask: self.cells.mask(t.src_var),
            dst_mask: self.cells.mask(t.dst_var),
        };
        self.push_plan(plan, Some(t), None)
    }

    fn push_store(&mut self, s: &HirVarStore) -> Result<(), HirError> {
        let dst = match (s.dst_addr, s.dst_trigger) {
            (Some(addr), None) => TransferDest::AbsAddr(addr),
            (None, Some(trigger_id)) => TransferDest::TriggerField {
                trigger_id,
                offset: s.dst_offset,
            },
            _ => return Err(HirError::InvalidVarStore),
        };
        let plan = plan_var_store(s.src_var, dst, s.modifier, self.cells.mask(s.src_var));
        self.push_plan(plan, None, Some(s))
    }

    fn push_plan(
        &mut self,
        plan: TransferPlan,
        transfer: Option<&HirVarTransfer>,
        store: Option<&HirVarStore>,
    ) -> Result<(), HirError> {
        if !self.plan_joins(&plan) {
            self.flush()?;
        }
        self.batch.plans.push(plan);
        if let Some(t) = transfer {
            self.batch.transfers.push(t.clone());
        }
        if let Some(s) = store {
            self.batch.stores.push(s.clone());
        }
        self.batch.srcs.insert(plan.src_var);
        if let TransferDest::Var(dst) = plan.dst {
            self.batch.dsts.insert(dst);
        }
        Ok(())
    }

    /// Whether `plan` may join the open run.
    ///
    /// The plan must be legal with every member
    /// ([`transfer::can_coalesce_plan`]) and must not be observable by any
    /// act already queued to run after the firing: such an act runs before
    /// this member's source would be read, so it must not read or write the
    /// member's destination and must not write its source.
    fn plan_joins(&self, plan: &TransferPlan) -> bool {
        if self.batch.is_empty() {
            return true;
        }
        if !can_coalesce_plan(&self.batch.plans, plan) {
            return false;
        }
        // A store's destination is an address the scheduler cannot compare
        // against an opaque destination write, so a pending opaque act keeps
        // the store out of this firing.
        if plan_is_store(plan)
            && self
                .batch
                .after
                .iter()
                .any(|a| ActEffects::of(a, self.cells.facts()).opaque_dest)
        {
            return false;
        }
        self.batch.after.iter().all(|a| {
            let touches = ActEffects::of(a, self.cells.facts());
            if touches.writes.contains(&plan.src_var) {
                return false;
            }
            match plan.dst {
                TransferDest::Var(dst) => {
                    !touches.reads.contains(&dst)
                        && !touches.writes.contains(&dst)
                        && !touches.machinery.contains(&dst)
                }
                _ => true,
            }
        })
    }

    fn push_act(&mut self, act: &HirAct) -> Result<(), HirError> {
        if self.batch.is_empty() {
            self.pre.push(act.clone());
            return Ok(());
        }
        let touches = ActEffects::of(act, self.cells.facts());
        // A page-machinery access (any field other than a plain value
        // read/write) can change what a page firing writes, and a page fires
        // every slot on it — not just the members' slots. The scheduler does
        // not model pages, so such an act always closes the run and keeps
        // program order.
        if !touches.machinery.is_empty() {
            self.flush()?;
            self.pre.push(act.clone());
            return Ok(());
        }
        if self.act_can_precede_firing(&touches) {
            self.pre.push(act.clone());
        } else {
            self.batch.after.push(act.clone());
        }
        Ok(())
    }

    /// Whether an act may be hoisted ahead of the open firing.
    ///
    /// Two independent conditions, because the hoist crosses both the firing
    /// and every act already queued behind it:
    /// - it cannot observe or change anything the firing does
    ///   ([`act_is_agnostic`]), and
    /// - it commutes with every pending after-act, whose program position it
    ///   would otherwise swap with.
    fn act_can_precede_firing(&self, touches: &ActEffects) -> bool {
        if !act_is_agnostic(&self.batch, touches) {
            return false;
        }
        self.batch.after.iter().all(|queued| {
            let queued = ActEffects::of(queued, self.cells.facts());
            act_touches_commute(touches, &queued)
        })
    }

    fn push_trigger(&mut self, trig: &HirTrigger) -> Result<(), HirError> {
        // An unconditional preserved flag-`4` block is folded into the
        // current basic block by lowering, so its acts behave exactly like
        // bare acts there. A block with any page-variable relocation stays
        // opaque so its trigger id and statement survive untouched. With a
        // store in the open run, an act addressing anything but a known
        // variable is also opaque: an absolute destination could alias the
        // store's destination, and hoisting would reorder the two writes.
        let opaque_dest_conflict = !self.batch.stores.is_empty()
            && trig
                .acts
                .iter()
                .any(|a| ActEffects::of(a, self.cells.facts()).opaque_dest);
        let unpackable = trig.unpack_shape()
            && !trig.acts.iter().any(act_may_touch_paged_var)
            && !opaque_dest_conflict
            && self.batch.after.is_empty();
        if unpackable {
            // Lowering folds the block's acts into the same setup trigger as
            // the batch, so the open run survives it.
            self.flush_acts()?;
        } else {
            self.flush()?;
        }
        self.out.push(HirStmt::Trigger(trig.clone()));
        Ok(())
    }

    /// Emit the pending acts only; an open run stays open.
    fn flush_acts(&mut self) -> Result<(), HirError> {
        if self.pre.is_empty() {
            return Ok(());
        }
        match self.kind {
            BlockKind::Root => {
                for chunk in self.pre.chunks(ACT_COUNT) {
                    let id = *self.next_id;
                    *self.next_id += 1;
                    let trig = HirTrigger::new(id, Vec::new(), chunk.to_vec(), true)?;
                    self.out.push(HirStmt::Trigger(trig));
                }
            }
            BlockKind::Body => {
                for act in self.pre.drain(..) {
                    self.out.push(HirStmt::Act(act));
                }
            }
        }
        self.pre.clear();
        Ok(())
    }

    /// Emit the pending acts, then the open run and its after-acts.
    fn flush(&mut self) -> Result<(), HirError> {
        self.flush_acts()?;
        if self.batch.is_empty() {
            return Ok(());
        }
        let batch = mem::take(&mut self.batch);
        self.out.push(HirStmt::VarBatch(HirVarBatch {
            transfers: batch.transfers,
            stores: batch.stores,
        }));
        for act in batch.after {
            self.out.push(HirStmt::Act(act));
        }
        Ok(())
    }
}

/// Whether `plan`'s destination is a store destination (not a variable slot).
fn plan_is_store(plan: &TransferPlan) -> bool {
    !matches!(plan.dst, TransferDest::Var(_))
}

/// Whether `act` can run either side of the open firing without changing the
/// result: it must not observe a variable the firing writes and must not
/// change a variable the firing reads.
fn act_is_agnostic(batch: &PendingBatch, touches: &ActEffects) -> bool {
    if !batch.stores.is_empty() && touches.opaque_dest {
        return false;
    }
    if touches.reads.iter().any(|v| batch.dsts.contains(v)) {
        return false;
    }
    touches
        .writes
        .iter()
        .all(|v| !batch.dsts.contains(v) && !batch.srcs.contains(v))
}

/// Whether swapping `a` and `b` cannot change the result.
///
/// This is the second half of the hoist test: an act pulled ahead of a firing
/// also crosses every act already queued behind that firing, so their effects
/// must not interact. An opaque destination (an absolute address or a trigger
/// field) cannot be compared, and a machinery field is state the scheduler
/// does not model, so either one forbids the swap. Otherwise the two commute
/// exactly when their reads and writes are disjoint.
fn act_touches_commute(a: &ActEffects, b: &ActEffects) -> bool {
    if a.opaque_dest || b.opaque_dest {
        return false;
    }
    let clashes = |left: &ActEffects, right: &ActEffects| {
        left.writes
            .iter()
            .any(|v| right.reads.contains(v) || right.writes.contains(v))
            || left.machinery.iter().any(|v| {
                right.reads.contains(v) || right.writes.contains(v) || right.machinery.contains(v)
            })
            || right.machinery.iter().any(|v| left.reads.contains(v))
    };
    !clashes(a, b) && !clashes(b, a)
}

/// Whether `act` may read or write a page-backed variable slot, the coarse
/// gate an unconditional block has to pass before its acts can be reordered
/// like bare acts.
fn act_may_touch_paged_var(act: &HirAct) -> bool {
    act.relocs.iter().any(|reloc| reloc.target_var.is_some())
}
