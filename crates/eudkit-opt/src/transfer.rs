//! Variable-source transfer legality and plan shape for `eudkit-opt`.
//!
//! This module owns the *decision* side of transfers: which consecutive
//! `HirStmt::VarTransfer` statements may share one page firing, and what a
//! single transfer looks like as a plan. It owns no addresses and no ranks:
//! every plan here is expressed in variable ids and modifiers only, and the
//! final page/slot of each variable comes from [`StoragePlan`] at lowering
//! time. Lowering (`eudkit-lower`) consumes these plans and builds the
//! setup/restore/page-chain actions. It does re-derive one neighboring
//! decision: whether an unconditional preserved flag-`4` `Trigger` folds into
//! the batch's basic block, through the shared
//! [`eudkit_hir::HirTrigger::is_unpackable`] predicate. It does not re-derive
//! transfer legality or plan shape, and this module never computes a page or
//! slot from a variable id.
//!
//! Guarantee: every variable-source assignment that reaches lowering is a
//! legal batch (no duplicate source, no RAW/WAR, only additive WAW, at most
//! 64 transfers) whose setup/page-fire/restore sequence is built once from
//! the same plan, so no consumer can silently apply a transfer twice, miss
//! a restore, or fire a page with a stale dest/modifier/mask.
//!
//! This pass is behaviour-preserving: it moves the legality rule and the
//! plan shape, it does not change them. In particular, an illegal
//! hand-written batch is still re-split by the optimizer (lowering re-runs
//! optimization), not rejected loudly; loud rejection of illegal batches
//! is deferred until hand-written misuse is observed in practice.

use std::collections::HashMap;

use eudkit_hir::{HirVarBatch, HirVarTransfer};
use eudkit_lir::ACT_COUNT;

/// Destination of a runtime variable-value transfer.
///
/// This is the plan-side destination only: it names *what* is written, not
/// where the page slot lives. The page/slot of a variable destination comes
/// from [`StoragePlan`] at lowering time, never from a variable id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferDest {
    /// Scalar variable slot (`dst_var`).
    Var(usize),
    /// Fixed 4-byte memory address (e.g. the `CurrentPlayer` slot).
    AbsAddr(u32),
    /// Byte offset inside a trigger (e.g. a condition's `player` field).
    TriggerField { trigger_id: usize, offset: usize },
}

impl TransferDest {
    /// Whether two destinations name the same 4-byte word.
    ///
    /// Destinations of different kinds are never considered the same word:
    /// a variable slot lives in the TriggerPage allocation, an absolute
    /// address is plain memory, and a trigger field is inside a specific
    /// trigger, so the plan alone cannot prove they alias.
    pub fn same_word(&self, other: &TransferDest) -> bool {
        match (self, other) {
            (TransferDest::Var(a), TransferDest::Var(b)) => a == b,
            (TransferDest::AbsAddr(a), TransferDest::AbsAddr(b)) => a == b,
            (
                TransferDest::TriggerField { trigger_id: a, offset: ao },
                TransferDest::TriggerField { trigger_id: b, offset: bo },
            ) => a == b && ao == bo,
            _ => false,
        }
    }
}

/// One variable-source assignment as lowering will execute it.
///
/// A plan is built once from the same inputs for every consumer: the
/// source variable, the destination kind, the modifier, and the masks.
/// The source mask is the mask the page slot must be restored to after the
/// page fires; the destination mask is the mask the page slot must carry
/// while the page fires. Lowering turns plans into setup/restore/page-chain
/// actions and never re-derives these fields.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferPlan {
    pub src_var: usize,
    pub dst: TransferDest,
    pub modifier: u8,
    pub src_mask: u32,
    pub dst_mask: u32,
}

impl TransferPlan {
    /// Whether the plan writes the source variable itself.
    ///
    /// Lowering skips the dest patch for a self-transfer — the slot already
    /// points at itself — while still applying modifier and mask patches.
    /// This rule lives here, with the plan shape, and lowering applies it;
    /// it is not re-derived at the act-construction site.
    pub fn is_self_transfer(&self) -> bool {
        matches!(self.dst, TransferDest::Var(dst) if dst == self.src_var)
    }
}

/// Derive the plans for one `HirVarBatch`, in batch order.
///
/// Masks come from the variable declarations: a mask unrecorded for a
/// variable defaults to full (`0xFFFF_FFFF`). Restore acts are built in
/// plan order, not reversed: in a legal batch every source is distinct
/// (rule 1 above), so each plan restores a different page slot and the
/// cross-plan order is unobservable. That reasoning is pinned here, not
/// left as an accident of the lowering loop.
pub fn plan_var_batch(
    batch: &HirVarBatch,
    var_masks: &HashMap<usize, u32>,
) -> Vec<TransferPlan> {
    batch
        .transfers
        .iter()
        .map(|t| TransferPlan {
            src_var: t.src_var,
            dst: TransferDest::Var(t.dst_var),
            modifier: t.modifier,
            src_mask: *var_masks.get(&t.src_var).unwrap_or(&0xFFFF_FFFF),
            dst_mask: *var_masks.get(&t.dst_var).unwrap_or(&0xFFFF_FFFF),
        })
        .collect()
}

/// Derive the single plan for a variable store.
///
/// A store is never batched with other transfers: its destination is not a
/// variable slot, so it cannot share a page firing's dest field with a
/// batch. It still travels through the same plan shape, with a full
/// destination mask: after the page fires, the source slot's mask is
/// restored to the source mask, and the store's own mask effect is the
/// full mask the page slot carried while firing.
pub fn plan_var_store(
    src_var: usize,
    dst: TransferDest,
    modifier: u8,
    src_mask: u32,
) -> TransferPlan {
    TransferPlan {
        src_var,
        dst,
        modifier,
        src_mask,
        dst_mask: 0xFFFF_FFFF,
    }
}

/// Whether `next` may join `batch` in one page firing.
///
/// The one legality authority for every batch member — transfers and stores
/// alike — operating on the [`TransferPlan`] shape lowering consumes. The
/// five rules, in order:
/// 1. One TriggerPage slot has only one `.dest` field per page firing, so
///    no source may appear twice in a batch.
/// 2. Read-After-Write (RAW) hazard: the next plan must not read a
///    variable written earlier in the batch.
/// 3. Write-After-Read (WAR) hazard: the next plan must not write a
///    variable read earlier in the batch.
/// 4. Write-After-Write (WAW) hazard: only commutative `Add` (8) plans
///    may share a destination word.
/// 5. At most 64 plans per batch (one TriggerPage capacity). Note the
///    batch limit counts plans, not actions: a batch's setup acts can be
///    up to three per plan and are chunked at lowering time, so "64
///    plans" is not a 64-action budget.
///
/// A store's destination is not a variable slot, so it only participates
/// through the source read (rules 1 and 2) and the destination-word rule
/// (rule 4); it has no WAR edge into a variable source.
pub fn can_coalesce_plan(batch: &[TransferPlan], next: &TransferPlan) -> bool {
    if batch.is_empty() {
        return true;
    }
    // 1. One TriggerPage slot has only one `.dest` field per page firing.
    if batch.iter().any(|p| p.src_var == next.src_var) {
        return false;
    }
    // 2. Read-After-Write (RAW) hazard: reading a variable modified earlier
    //    in the batch.
    if batch
        .iter()
        .any(|p| matches!(p.dst, TransferDest::Var(dst) if dst == next.src_var))
    {
        return false;
    }
    // 3. Write-After-Read (WAR) hazard: writing a variable read earlier in
    //    the batch. A store destination is not a variable, so only variable
    //    destinations have this edge.
    if let TransferDest::Var(dst) = next.dst {
        if batch.iter().any(|p| p.src_var == dst) {
            return false;
        }
    }
    // 4. Write-After-Write (WAW) hazard: only commutative `Add` (8) plans may
    //    share a destination word.
    if batch
        .iter()
        .any(|p| p.dst.same_word(&next.dst) && (p.modifier != 8 || next.modifier != 8))
    {
        return false;
    }
    // 5. Coalesce up to 64 plans in a single batch (one TriggerPage capacity).
    batch.len() < ACT_COUNT
}

/// Whether `next` may join `batch` in one page firing.
///
/// The legacy transfer-only spelling, expressed through the shared plan rule
/// so there is exactly one copy of the legality logic: every member becomes a
/// variable-destination plan (masks are irrelevant to legality).
pub fn can_coalesce_transfer(batch: &[HirVarTransfer], next: &HirVarTransfer) -> bool {
    let plans: Vec<TransferPlan> = batch
        .iter()
        .map(|t| TransferPlan {
            src_var: t.src_var,
            dst: TransferDest::Var(t.dst_var),
            modifier: t.modifier,
            src_mask: 0,
            dst_mask: 0,
        })
        .collect();
    let next = TransferPlan {
        src_var: next.src_var,
        dst: TransferDest::Var(next.dst_var),
        modifier: next.modifier,
        src_mask: 0,
        dst_mask: 0,
    };
    can_coalesce_plan(&plans, &next)
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{HirModule, HirStmt, HirVarBatch};
    use crate::optimize_module;

    fn transfer(dst_var: usize, src_var: usize, modifier: u8) -> HirVarTransfer {
        HirVarTransfer {
            dst_var,
            src_var,
            modifier,
        }
    }

    #[test]
    fn coalesces_consecutive_var_transfers_into_shared_var_batch() {
        // a.set(x); b.set(y); c.iadd(z);
        let vars: Vec<eudkit_hir::HirVarDecl> = (0..6)
            .map(|id| eudkit_hir::HirVarDecl::new(id, id as u32 * 10))
            .collect();
        let module = HirModule {
            funcs: Vec::new(),
            vars: vars.clone(),
            stmts: vec![
                HirStmt::VarTransfer(transfer(3, 0, 7)),
                HirStmt::VarTransfer(transfer(4, 1, 7)),
                HirStmt::VarTransfer(transfer(5, 2, 8)),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        assert_eq!(optimized.vars, vars);
        assert_eq!(optimized.stmts.len(), 1);
        assert_eq!(
            optimized.stmts[0],
            HirStmt::VarBatch(HirVarBatch::transfers(vec![
                transfer(3, 0, 7),
                transfer(4, 1, 7),
                transfer(5, 2, 8),
            ]))
        );
    }

    #[test]
    fn splits_var_batches_on_raw_war_duplicate_source_and_non_additive_waw_hazards() {
        let vars: Vec<eudkit_hir::HirVarDecl> = (0..6).map(|id| eudkit_hir::HirVarDecl::new(id, 0)).collect();
        // 1) RAW hazard: y.set(x); z.set(y); -> 2 batches
        let raw_mod = HirModule {
            funcs: Vec::new(),
            vars: vars.clone(),
            stmts: vec![
                HirStmt::VarTransfer(transfer(1, 0, 7)),
                HirStmt::VarTransfer(transfer(2, 1, 7)),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let opt_raw = optimize_module(&raw_mod).unwrap();
        assert_eq!(opt_raw.stmts.len(), 2);

        // 2) Duplicate source: a.set(x); b.set(x); -> 2 batches
        let dup_src = HirModule {
            funcs: Vec::new(),
            vars: vars.clone(),
            stmts: vec![
                HirStmt::VarTransfer(transfer(1, 0, 7)),
                HirStmt::VarTransfer(transfer(2, 0, 7)),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let opt_dup = optimize_module(&dup_src).unwrap();
        assert_eq!(opt_dup.stmts.len(), 2);

        // 3) Additive WAW: z.iadd(x); z.iadd(y); -> 1 coalesced batch!
        let add_waw = HirModule {
            funcs: Vec::new(),
            vars: vars.clone(),
            stmts: vec![
                HirStmt::VarTransfer(transfer(2, 0, 8)),
                HirStmt::VarTransfer(transfer(2, 1, 8)),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
        };
        let opt_add = optimize_module(&add_waw).unwrap();
        assert_eq!(opt_add.stmts.len(), 1);
    }

    #[test]
    fn coalesces_full_64_field_trigger_struct_copy_into_single_var_batch() {
        let vars: Vec<eudkit_hir::HirVarDecl> = (0..128).map(|id| eudkit_hir::HirVarDecl::new(id, id as u32)).collect();
        let stmts: Vec<HirStmt> = (0..64)
            .map(|k| HirStmt::VarTransfer(transfer(64 + k, k, 7)))
            .collect();
        let module = HirModule { funcs: Vec::new(), vars, stmts, strings: Vec::new(), files: Vec::new(), blobs: Vec::new() };
        let optimized = optimize_module(&module).unwrap();
        assert_eq!(optimized.stmts.len(), 1);
        match &optimized.stmts[0] {
            HirStmt::VarBatch(b) => assert_eq!(b.transfers.len(), 64),
            other => panic!("expected single 64-transfer VarBatch, got {other:?}"),
        }
    }

    // Direct legality matrix for `can_coalesce_transfer`: each rule is
    // pinned at the function, not only through `optimize_module`, so a
    // future refactor of the optimizer loop cannot silently drop a rule.
    #[test]
    fn legality_matrix_covers_all_five_rules_at_the_boundary() {
        // Rule 1: duplicate source.
        assert!(!can_coalesce_transfer(
            &[transfer(1, 0, 7)],
            &transfer(2, 0, 7)
        ));
        // Rule 2: RAW — next reads a variable written earlier.
        assert!(!can_coalesce_transfer(
            &[transfer(1, 0, 7)],
            &transfer(2, 1, 7)
        ));
        // Rule 3: WAR — next writes a variable read earlier.
        assert!(!can_coalesce_transfer(
            &[transfer(1, 0, 7)],
            &transfer(0, 2, 7)
        ));
        // Rule 4: non-additive WAW is illegal, additive-additive is legal,
        // and a mixed Add/SetTo pair sharing a dst is illegal.
        assert!(!can_coalesce_transfer(
            &[transfer(2, 0, 7)],
            &transfer(2, 1, 7)
        ));
        assert!(can_coalesce_transfer(
            &[transfer(2, 0, 8)],
            &transfer(2, 1, 8)
        ));
        assert!(!can_coalesce_transfer(
            &[transfer(2, 0, 8)],
            &transfer(2, 1, 7)
        ));
        // Rule 5: boundary — 63 transfers admit a 64th, 64 do not.
        let batch63: Vec<HirVarTransfer> = (0..63)
            .map(|k| transfer(100 + k, k, 7))
            .collect();
        assert!(can_coalesce_transfer(&batch63, &transfer(200, 63, 7)));
        let batch64: Vec<HirVarTransfer> = (0..64)
            .map(|k| transfer(100 + k, k, 7))
            .collect();
        assert!(!can_coalesce_transfer(&batch64, &transfer(200, 64, 7)));
        // Empty batch always admits.
        assert!(can_coalesce_transfer(&[], &transfer(1, 0, 7)));
    }

    #[test]
    fn self_transfer_coalescing_follows_the_same_hazard_rules() {
        // A self-transfer alone joins an empty batch.
        assert!(can_coalesce_transfer(&[], &transfer(0, 0, 7)));
        // A self-transfer after an unrelated transfer joins: no shared
        // source or destination with the earlier transfer.
        assert!(can_coalesce_transfer(
            &[transfer(1, 2, 7)],
            &transfer(0, 0, 7)
        ));
        // A self-transfer touching an earlier transfer's source or
        // destination is a RAW/WAR hazard and must split.
        assert!(!can_coalesce_transfer(
            &[transfer(1, 0, 7)],
            &transfer(0, 0, 7)
        ));
        assert!(!can_coalesce_transfer(
            &[transfer(0, 1, 7)],
            &transfer(0, 0, 7)
        ));
    }

    #[test]
    fn plan_derivation_uses_decl_masks_with_full_defaults_and_store_shape() {
        let mut masks = HashMap::new();
        masks.insert(0usize, 0x0000_FFFFu32);
        // src mask recorded, dst mask unrecorded -> full default.
        let batch = HirVarBatch::transfers(vec![transfer(1, 0, 7)]);
        let plans = plan_var_batch(&batch, &masks);
        assert_eq!(plans.len(), 1);
        assert_eq!(plans[0].src_var, 0);
        assert_eq!(plans[0].dst, TransferDest::Var(1));
        assert_eq!(plans[0].modifier, 7);
        assert_eq!(plans[0].src_mask, 0x0000_FFFF);
        assert_eq!(plans[0].dst_mask, 0xFFFF_FFFF);

        // A store is a single plan with a full destination mask, for both
        // destination kinds, and its source mask comes from the caller.
        let store_abs = plan_var_store(0, TransferDest::AbsAddr(0x58A364), 7, 0x0000_FFFF);
        assert_eq!(store_abs.dst, TransferDest::AbsAddr(0x58A364));
        assert_eq!(store_abs.src_mask, 0x0000_FFFF);
        assert_eq!(store_abs.dst_mask, 0xFFFF_FFFF);
        let store_field = plan_var_store(
            0,
            TransferDest::TriggerField {
                trigger_id: 3,
                offset: 16,
            },
            8,
            0xFFFF_FFFF,
        );
        assert_eq!(
            store_field.dst,
            TransferDest::TriggerField {
                trigger_id: 3,
                offset: 16,
            }
        );
        assert_eq!(store_field.modifier, 8);
        assert_eq!(store_field.dst_mask, 0xFFFF_FFFF);
    }

    #[test]
    fn self_transfer_skips_dest_patch_but_keeps_modifier_and_mask_patches() {
        let plan = TransferPlan {
            src_var: 0,
            dst: TransferDest::Var(0),
            modifier: 7,
            src_mask: 0xFFFF_FFFF,
            dst_mask: 0xFFFF_FFFF,
        };
        assert!(plan.is_self_transfer());
        let other = TransferPlan {
            dst: TransferDest::Var(1),
            ..plan
        };
        assert!(!other.is_self_transfer());
    }
}
