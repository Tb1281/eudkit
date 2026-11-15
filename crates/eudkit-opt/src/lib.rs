//! HIR 3.0 optimization and canonicalization passes for `eudkit`.
//!
//! `eudkit-opt`:
//! 1. Validates all `Trigger (16C, 64A)` bounds (`EUDSL-BOUNDS`) and variable references.
//! 2. Desugars and flattens temporal control flow, merges sibling `when`s, and
//!    bin-packs adjacent unconditional actions into `Trigger (16C, 64A)`.
//! 3. Schedules every statement list — the module root and every `when`/`else`,
//!    `once`, and `while` body, including the `until`/`edge`/`every`/`after`
//!    desugar expansions — with one recursive [`scheduler::BlockScheduler`].
//!    The scheduler coalesces `VarTransfer` and `VarStore` members (and existing
//!    `VarBatch`es) into one shared `TriggerPage` firing (`<= 63A` setup /
//!    `<= 64A` restore), packed by lowering, and orders surrounding acts into
//!    the setup or the restore continuation by their real read/write sets.
//! 4. Drops dead 0-act conditional gates after scheduling: an unreferenced
//!    empty `Trigger`, an empty `when`, and an empty `once`. Empty `while`
//!    loops are spin-waits and are kept.

/// Process-wide number of [`optimize_module`] calls.
///
/// Instrumentation for the C2c acceptance test (one optimize per compile);
/// compiled in only with the `count-calls` feature, which production builds
/// leave off.
#[cfg(feature = "count-calls")]
#[doc(hidden)]
pub static OPTIMIZE_MODULE_CALLS: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

use eudkit_hir::{HirDocument, HirError, HirFunc, HirModule, TriggerRefs};
use folds::FoldLog;

pub mod calls;
pub mod cell;
mod facts;
pub mod folds;
mod scheduler;
pub mod temporal;
pub mod transfer;

/// Validate and optimize a [`HirDocument`], recording every optimizer-visible
/// fold in the returned [`FoldLog`].
pub fn optimize_document_with_folds(
    doc: &HirDocument,
) -> Result<(HirDocument, FoldLog), HirError> {
    doc.validate()?;
    let mut log = FoldLog::enabled();
    let module = optimize_module_impl(&doc.module, &mut log)?;
    Ok((
        HirDocument {
            version: doc.version.clone(),
            module,
        },
        log,
    ))
}

/// Validate and optimize a [`HirDocument`].
pub fn optimize_document(doc: &HirDocument) -> Result<HirDocument, HirError> {
    doc.validate()?;
    Ok(HirDocument {
        version: doc.version.clone(),
        module: optimize_module(&doc.module)?,
    })
}

/// Validate and optimize a [`HirModule`], bin-packing runs of adjacent
/// unconditional actions into `Trigger (16C, 64A)` nodes and scheduling every
/// statement list with one block-wide rule.
///
/// [`temporal::optimize_stmts`] runs the desugar/flatten/merge/pack passes and
/// then [`scheduler::BlockScheduler`] coalesces each block's `VarTransfer`,
/// `VarStore`, and existing `VarBatch` members into shared `VarBatch`es (one
/// `TriggerPage` firing regardless of member count) while ordering the
/// surrounding acts into the batch's setup trigger or its restore
/// continuation. The same rule applies at the module root and in every
/// `when`/`else`, `once`, and `while` body.
pub fn optimize_module(module: &HirModule) -> Result<HirModule, HirError> {
    optimize_module_impl(module, &mut FoldLog::disabled())
}

/// Validate and optimize a [`HirModule`], recording every optimizer-visible
/// fold in the returned [`FoldLog`].
///
/// The produced HIR is byte-identical to [`optimize_module`] for the same
/// input; only the optional report differs.
pub fn optimize_module_with_folds(module: &HirModule) -> Result<(HirModule, FoldLog), HirError> {
    let mut log = FoldLog::enabled();
    let module = optimize_module_impl(module, &mut log)?;
    Ok((module, log))
}

fn optimize_module_impl(module: &HirModule, log: &mut FoldLog) -> Result<HirModule, HirError> {
    #[cfg(feature = "count-calls")]
    OPTIMIZE_MODULE_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    module.validate()?;

    // Fold a call's result copy into the assignment that consumes it before
    // scheduling: the call's landing *is* that assignment, so fusing them
    // costs one value move instead of two (one `TriggerPage` firing each).
    let mut module = module.clone();
    calls::fuse_call_results(&mut module);
    // A body with a single call site shares nothing, so it pays the call/return
    // machinery for free. Splice it back in before scheduling.
    calls::inline_single_call_funcs(&mut module)?;
    let module = &module;

    // One module-wide reference set for the temporal passes: a trigger id the
    // module names from a chain pointer, relocation, store, or blob relocation
    // is a barrier to the backward merge and may not be dropped.
    let refs = TriggerRefs::from_module(module);

    // Subroutine bodies are statement lists too, so they share the synthetic
    // id space with the module root. `next_id` covers every nested statement
    // list, not just root `Trigger`s: packing a nested leaf `when`/`once`
    // (e.g. a block-memory seed inside a loop) mints an id from this counter,
    // so a nested user trigger with a higher id must still advance the
    // namespace.
    let mut next_synth_id = refs.next_id();

    // Storage facts for the scheduler come from the shared facts walk (the
    // single pin home for declarations, transfer/store sources, call
    // arguments, subroutine params/results, and every nested body). Scheduling
    // only regroups statements, so this is the same page-backed set
    // `analyze_storage` builds later from the scheduled module.
    let cells = scheduler::CellMap::from_module(module);

    // Candidate 11 desugar state vars are DSL-reserved (carried on the
    // nodes, e.g. HirUntil::latch_var); opt never mints var ids, so the
    // module's declarations pass through unchanged.
    let stmts = temporal::optimize_stmts(
        module.stmts.clone(),
        scheduler::BlockKind::Root,
        &cells,
        &refs,
        &mut next_synth_id,
        log,
    )?;

    // A subroutine body is a body block: the scheduler keeps its acts as bare
    // `HirStmt::Act`s for lowering's basic-block packer and batches its
    // transfers/stores the same way it batches a `when` body. One shared body
    // therefore costs exactly what the inlined copy it replaces cost.
    let mut funcs = Vec::with_capacity(module.funcs.len());
    for func in &module.funcs {
        let body = temporal::optimize_stmts(
            func.body.clone(),
            scheduler::BlockKind::Body,
            &cells,
            &refs,
            &mut next_synth_id,
            log,
        )?;
        funcs.push(HirFunc {
            id: func.id,
            params: func.params.clone(),
            result: func.result,
            body,
        });
    }

    let mut optimized = HirModule {
        vars: module.vars.clone(),
        stmts,
        funcs,
        strings: module.strings.clone(),
        files: module.files.clone(),
        blobs: module.blobs.clone(),
    };
    // Post-schedule dead-gate removal: a 0-act conditional gate only burns a
    // condition evaluation. Running this last does not re-schedule, so the
    // frontend's statement order and the scheduler's batching are preserved;
    // surviving trigger ids are not renumbered. Removing a scope boundary can
    // still expose adjacent act runs, which lowering may then pack into fewer
    // triggers, so downstream static counts are not promised byte-identical.
    temporal::drop_dead_triggers(&mut optimized, log);
    Ok(optimized)
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::{
        HirAct, HirCond, HirReloc, HirStmt, HirTrigger, HirVarBatch, HirVarDecl, HirVarStore,
        HirVarTransfer, HirWhen, HirWhile,
    };

    fn transfer(dst_var: usize, src_var: usize, modifier: u8) -> HirVarTransfer {
        HirVarTransfer {
            dst_var,
            src_var,
            modifier,
        }
    }



    #[test]
    fn bin_packs_up_to_64_adjacent_unconditional_actions_per_trigger() {
        let mut stmts = Vec::new();
        for i in 0..130 {
            stmts.push(HirStmt::Act(HirAct::set_deaths(0, 7, i, 0)));
        }
        let module = HirModule {
            vars: Vec::new(),
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        // 130 actions -> 64 + 64 + 2 = 3 Triggers
        assert_eq!(optimized.stmts.len(), 3);
        let counts: Vec<usize> = optimized
            .stmts
            .iter()
            .map(|s| match s {
                HirStmt::Trigger(t) => t.acts.len(),
                _ => panic!("all top-level Acts must be bin-packed into Triggers"),
            })
            .collect();
        assert_eq!(counts, vec![64, 64, 2]);
    }

    #[test]
    fn preserves_explicit_triggers_and_flushes_around_them() {
        let explicit = HirTrigger::new(
            0,
            vec![HirCond::always()],
            vec![HirAct::set_deaths(1, 7, 99, 0)],
            true,
        )
        .unwrap();
        let module = HirModule {
            vars: Vec::new(),
            stmts: vec![
                HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0)),
                HirStmt::Act(HirAct::set_deaths(0, 7, 2, 0)),
                HirStmt::Trigger(explicit.clone()),
                HirStmt::Act(HirAct::set_deaths(0, 7, 3, 0)),
            ],

            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        assert_eq!(optimized.stmts.len(), 3);
        match &optimized.stmts[1] {
            HirStmt::Trigger(t) => assert_eq!(t, &explicit),
            _ => panic!("expected explicit trigger at index 1"),
        }
    }

    fn when_with_body(body: Vec<HirStmt>) -> HirModule {
        HirModule {
            vars: (0..8).map(|id| HirVarDecl::new(id, 0)).collect(),
            stmts: vec![HirStmt::When(HirWhen::new(vec![HirCond::always()], body))],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        }
    }

    fn body_of_only_when(module: &HirModule) -> &[HirStmt] {
        match &module.stmts[0] {
            HirStmt::When(w) => &w.body,
            other => panic!("expected a When, got {other:?}"),
        }
    }

    #[test]
    fn coalesces_adjacent_transfers_inside_a_when_body() {
        // Two independent copies in one body: one shared VarBatch, not two
        // bare transfers (the bug this ticket fixes).
        let module = when_with_body(vec![
            HirStmt::VarTransfer(transfer(2, 0, 7)),
            HirStmt::VarTransfer(transfer(3, 1, 7)),
        ]);
        let optimized = optimize_module(&module).unwrap();
        let body = body_of_only_when(&optimized);
        assert_eq!(body.len(), 1);
        match &body[0] {
            HirStmt::VarBatch(b) => assert_eq!(b.transfers.len(), 2),
            other => panic!("expected one VarBatch inside the body, got {other:?}"),
        }
    }

    #[test]
    fn hazards_split_runs_and_stores_join_independent_members_inside_a_when_body() {
        // RAW hazard: d.set(s); src2.set(d) -> two single-transfer batches.
        let raw = optimize_module(&when_with_body(vec![
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            HirStmt::VarTransfer(transfer(2, 1, 7)),
        ]))
        .unwrap();
        let raw_body = body_of_only_when(&raw);
        assert_eq!(raw_body.len(), 2);
        assert!(raw_body
            .iter()
            .all(|s| matches!(s, HirStmt::VarBatch(b) if b.transfers.len() == 1)));

        // A store keeps its program position by its source read (RAW) but no
        // longer ends the run: an independent transfer after it shares the
        // firing, so the pair costs one setup/page/restore instead of two.
        let stored = optimize_module(&when_with_body(vec![
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            HirStmt::VarStore(HirVarStore::to_addr(1, 0x640B58, 7)),
            HirStmt::VarTransfer(transfer(2, 3, 7)),
        ]))
        .unwrap();
        let stored_body = body_of_only_when(&stored);
        assert_eq!(stored_body.len(), 2);
        assert!(
            matches!(&stored_body[0], HirStmt::VarBatch(b) if b.transfers.len() == 1 && b.stores.is_empty())
        );
        match &stored_body[1] {
            HirStmt::VarBatch(b) => {
                assert_eq!(b.transfers, vec![transfer(2, 3, 7)]);
                assert_eq!(
                    b.stores,
                    vec![HirVarStore::to_addr(1, 0x640B58, 7)],
                    "the store rides the same firing as the independent transfer"
                );
            }
            other => panic!("expected the store to coalesce into a VarBatch, got {other:?}"),
        }
    }

    #[test]
    fn body_coalescing_does_not_change_packed_leaf_when_trigger_ids() {
        // A transfer-bodied When that cannot join its neighbours (its gate
        // differs, so the #98 flat-body merge does not apply) is not a leaf,
        // cannot be packed into a Trigger, and must not consume a trigger id,
        // so the two packed leaf Whens around it keep ids 0 and 1 exactly as
        // without the fix.
        let leaf = || HirStmt::When(HirWhen::new(vec![HirCond::always()], vec![
            HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0)),
        ]));
        let module = HirModule {
            vars: (0..8).map(|id| HirVarDecl::new(id, 0)).collect(),
            stmts: vec![
                leaf(),
                HirStmt::When(HirWhen::new(
                    vec![HirCond::deaths(0, 10, 1, 0)],
                    vec![
                        HirStmt::VarTransfer(transfer(1, 0, 7)),
                        HirStmt::VarTransfer(transfer(2, 1, 7)),
                    ],
                )),
                leaf(),
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };
        let optimized = optimize_module(&module).unwrap();
        assert_eq!(optimized.stmts.len(), 3);
        match (&optimized.stmts[0], &optimized.stmts[2]) {
            (HirStmt::Trigger(a), HirStmt::Trigger(b)) => {
                assert_eq!(a.id, 0);
                assert_eq!(b.id, 1);
            }
            other => panic!("expected packed leaf whens on both sides, got {other:?}"),
        }
        // The middle When survives unpacked and its RAW run stays split.
        match &optimized.stmts[1] {
            HirStmt::When(w) => assert_eq!(w.body.len(), 2),
            other => panic!("expected the transfer-bodied When in the middle, got {other:?}"),
        }
    }

    #[test]
    fn synthetic_ids_skip_all_nested_trigger_ids() {
        // A user trigger nested under a While holds id 7. Packing the earlier
        // leaf When mints the first synthetic id; it must be 8, not 0, or
        // lowering's `id -> index` map aliases it with the nested trigger.
        let nested = HirStmt::While(HirWhile::new(
            vec![HirCond::deaths(0, 10, 1, 0)],
            vec![HirStmt::When(HirWhen::new(
                vec![HirCond::always()],
                vec![HirStmt::Trigger(
                    HirTrigger::new(
                        7,
                        vec![HirCond::always()],
                        vec![HirAct::set_deaths(1, 7, 99, 0)],
                        true,
                    )
                    .unwrap(),
                )],
            ))],
        ));
        let module = HirModule {
            vars: (0..8).map(|id| HirVarDecl::new(id, 0)).collect(),
            stmts: vec![
                HirStmt::When(HirWhen::new(
                    vec![HirCond::always()],
                    vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))],
                )),
                nested,
            ],
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        };

        fn collect_ids(stmts: &[HirStmt], out: &mut Vec<usize>) {
            for stmt in stmts {
                match stmt {
                    HirStmt::Trigger(t) => out.push(t.id),
                    HirStmt::When(w) => {
                        collect_ids(&w.body, out);
                        collect_ids(&w.else_body, out);
                    }
                    HirStmt::Once(o) => collect_ids(&o.body, out),
                    HirStmt::While(w) => collect_ids(&w.body, out),
                    HirStmt::Until(u) => collect_ids(&u.body, out),
                    HirStmt::Edge(e) => collect_ids(&e.body, out),
                    HirStmt::Every(e) => collect_ids(&e.body, out),
                    HirStmt::After(a) => collect_ids(&a.body, out),
                    HirStmt::Act(_)
                    | HirStmt::VarTransfer(_)
                    | HirStmt::VarBatch(_)
                    | HirStmt::VarStore(_)
                    | HirStmt::Call(_) => {}
                }
            }
        }

        let optimized = optimize_module(&module).unwrap();
        let mut ids = Vec::new();
        collect_ids(&optimized.stmts, &mut ids);
        ids.sort_unstable();
        assert_eq!(ids, vec![7, 8], "nested id 7 is preserved; packing starts at 8");
    }

    // ---- Ticket #60: root act/transfer interleave must not split the run ----

    fn root_act(value: u32) -> HirAct {
        HirAct::set_deaths(0, 7, value, 0)
    }

    /// An unconditional preserved block, the shape both frontends emit for a
    /// root-level action (`eudkit-lower`'s `can_unpack_into_basic_block` shape).
    fn root_block(id: usize, acts: Vec<HirAct>) -> HirStmt {
        HirStmt::Trigger(HirTrigger::new(id, Vec::new(), acts, true).expect("legal Trigger"))
    }

    fn root_module(stmts: Vec<HirStmt>) -> HirModule {
        HirModule {
            vars: (0..4).map(|id| HirVarDecl::new(id, id as u32 * 10)).collect(),
            stmts,
            strings: Vec::new(),
            files: Vec::new(),
            blobs: Vec::new(),
            funcs: Vec::new(),
        }
    }

    /// The two copies used by the #60 probe: `1 = 0`, `3 = 2`.
    fn probe_transfers() -> [HirStmt; 2] {
        [
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            HirStmt::VarTransfer(transfer(3, 2, 7)),
        ]
    }

    fn batches(module: &HirModule) -> Vec<&HirVarBatch> {
        module
            .stmts
            .iter()
            .filter_map(|s| match s {
                HirStmt::VarBatch(b) => Some(b),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn interleaved_root_blocks_keep_one_transfer_batch() {
        // `act, act, x.set(y), w.set(z)` vs `act, x.set(y), act, w.set(z)`: the
        // second block used to flush the run, so one batch became two.
        let [t1, t2] = probe_transfers();
        let grouped = optimize_module(&root_module(vec![
            root_block(0, vec![root_act(1), root_act(2)]),
            t1,
            t2,
        ]))
        .unwrap();
        let interleaved = optimize_module(&root_module(vec![
            root_block(0, vec![root_act(1)]),
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            root_block(1, vec![root_act(2)]),
            HirStmt::VarTransfer(transfer(3, 2, 7)),
        ]))
        .unwrap();

        let grouped_batches = batches(&grouped);
        let interleaved_batches = batches(&interleaved);
        assert_eq!(grouped_batches.len(), 1);
        assert_eq!(interleaved_batches.len(), 1);
        assert_eq!(interleaved_batches[0].transfers.len(), 2);
        assert_eq!(interleaved_batches[0], grouped_batches[0]);
        // Both blocks survive ahead of the batch (trigger ids are not
        // re-minted: opt cannot know whether another trigger references them).
        assert!(matches!(&interleaved.stmts[0], HirStmt::Trigger(t) if t.id == 0));
        assert!(matches!(&interleaved.stmts[1], HirStmt::Trigger(t) if t.id == 1));
        assert!(matches!(&interleaved.stmts[2], HirStmt::VarBatch(_)));
    }

    #[test]
    fn interleaved_bare_root_acts_optimize_byte_identically_to_the_grouped_shape() {
        // `HirStmt::Act` is the other spelling of an unconditional root action;
        // it has no id, so interleaved acts bin-pack into the same Trigger.
        let [t1, t2] = probe_transfers();
        let grouped = optimize_module(&root_module(vec![
            HirStmt::Act(root_act(1)),
            HirStmt::Act(root_act(2)),
            t1,
            t2,
        ]))
        .unwrap();
        let interleaved = optimize_module(&root_module(vec![
            HirStmt::Act(root_act(1)),
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            HirStmt::Act(root_act(2)),
            HirStmt::VarTransfer(transfer(3, 2, 7)),
        ]))
        .unwrap();
        assert_eq!(interleaved, grouped);
        assert_eq!(batches(&interleaved).len(), 1);
        assert_eq!(batches(&interleaved)[0].transfers.len(), 2);
    }

    #[test]
    fn an_interleaved_act_that_names_a_variable_still_ends_the_transfer_run() {
        // The reorder is only legal for acts that cannot touch a paged variable
        // slot: an action carrying `relocs[].target_var` may read or write one.
        let mut dependent = root_act(2);
        dependent.relocs.push(HirReloc::var(20, 0, 20, 1));
        let optimized = optimize_module(&root_module(vec![
            root_block(0, vec![root_act(1)]),
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            root_block(1, vec![dependent]),
            HirStmt::VarTransfer(transfer(3, 2, 7)),
        ]))
        .unwrap();
        let split = batches(&optimized);
        assert_eq!(split.len(), 2, "a dependent act must keep source order");
        assert!(split.iter().all(|batch| batch.transfers.len() == 1));
        assert!(matches!(&optimized.stmts[1], HirStmt::VarBatch(_)));
        assert!(matches!(&optimized.stmts[2], HirStmt::Trigger(t) if t.id == 1));
        assert!(matches!(&optimized.stmts[3], HirStmt::VarBatch(_)));
    }

    #[test]
    fn an_interleaved_conditional_trigger_still_ends_the_transfer_run() {
        // Only the `can_unpack_into_basic_block` shape is safe to move: a
        // gated or chain-owned trigger may observe the transfer's effect.
        let gated = HirStmt::Trigger(
            HirTrigger::new(
                1,
                vec![HirCond::always()],
                vec![root_act(2)],
                true,
            )
            .expect("legal gated Trigger"),
        );
        let optimized = optimize_module(&root_module(vec![
            root_block(0, vec![root_act(1)]),
            HirStmt::VarTransfer(transfer(1, 0, 7)),
            gated,
            HirStmt::VarTransfer(transfer(3, 2, 7)),
        ]))
        .unwrap();
        assert_eq!(batches(&optimized).len(), 2);
        assert!(matches!(&optimized.stmts[2], HirStmt::Trigger(t) if t.id == 1));
    }
}
