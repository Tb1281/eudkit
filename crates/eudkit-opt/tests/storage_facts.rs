//! Agreement and regression tests for the shared scalar-storage facts.
//!
//! `CellMap` (the scheduler's view) and `analyze_storage` (the final layout)
//! must agree on which variables are page-backed. A disagreement is observable
//! through `optimize_module`: an act touching a page-machinery field
//! (`.dest`/`.modifier`/`.mask`) closes an open transfer run, while the same
//! act on a cell-backed variable is merely a value read+write and may be
//! hoisted ahead of the firing.
//!
//! Before the R3 fix, `CellMap` omitted `HirCallArg::Var` arguments and
//! subroutine `params`/`result`, so these probes were misclassified as scalar
//! and the run stayed open. The probes below exercise the public seam only.

use std::collections::BTreeSet;

use eudkit_hir::{
    HirAct, HirAfter, HirCall, HirCallArg, HirCond, HirEdge, HirEdgeDirection, HirEvery, HirFunc,
    HirModule, HirOnce, HirReloc, HirStmt, HirTrigger, HirUntil, HirVarDecl, HirVarStore,
    HirVarTransfer, HirWhen, HirWhile,
};
use eudkit_lir::{DEST_FIELD_OFFSET, MASK_FIELD_OFFSET, MODIFIER_DWORD_OFFSET};
use eudkit_opt::cell::{StorageClass, analyze_storage};
use eudkit_opt::optimize_module;

/// Untagged action value that identifies the machinery probe.
const PROBE_VALUE: u32 = 0x00C0_FFEE;

// Dedicated probe-run variables. They are declared but never named by the
// fixture's own statements, so a probe subject is never a member of the run.
const PROBE_DST_A: usize = 40;
const PROBE_SRC_A: usize = 41;
const PROBE_DST_B: usize = 42;
const PROBE_SRC_B: usize = 43;

fn transfer(dst_var: usize, src_var: usize, modifier: u8) -> HirVarTransfer {
    HirVarTransfer {
        dst_var,
        src_var,
        modifier,
    }
}

fn var_decl(id: usize) -> HirVarDecl {
    HirVarDecl::new(id, 500)
}

/// The variable page-slot fields a transfer setup act patches, other than the
/// plain value word: `.dest`, the dword holding `.modifier`, and `.mask`.
/// Naming any of these is a machinery access when `subject` is page-backed; on
/// a cell-backed scalar it is just a value access on its one dword.
const MACHINERY_TARGET_OFFSETS: [usize; 3] = [
    DEST_FIELD_OFFSET,
    MODIFIER_DWORD_OFFSET,
    MASK_FIELD_OFFSET,
];

/// An act whose relocation names a page-machinery field of `subject`.
///
/// `field_offset = DEST_FIELD_OFFSET` with a non-value `target_offset` is how
/// lowering patches a transfer's setup action: not a plain value read/write,
/// so it is machinery exactly when `subject` is page-backed.
fn machinery_probe(subject: usize, target_offset: usize) -> HirAct {
    let mut act = HirAct::set_deaths(0, 7, PROBE_VALUE, 0);
    act.relocs.push(HirReloc::var(
        DEST_FIELD_OFFSET,
        subject,
        target_offset,
        1,
    ));
    act
}

fn probe_suffix(subject: usize, target_offset: usize) -> Vec<HirStmt> {
    vec![
        HirStmt::VarTransfer(transfer(PROBE_DST_A, PROBE_SRC_A, 7)),
        HirStmt::Act(machinery_probe(subject, target_offset)),
        HirStmt::VarTransfer(transfer(PROBE_DST_B, PROBE_SRC_B, 7)),
    ]
}

fn is_probe_act(act: &HirAct, subject: usize, target_offset: usize) -> bool {
    act.value == PROBE_VALUE
        && act.relocs.iter().any(|reloc| {
            reloc.target_var == Some(subject)
                && reloc.field_offset == DEST_FIELD_OFFSET
                && reloc.target_offset == target_offset
        })
}

fn stmt_has_probe_act(stmt: &HirStmt, subject: usize, target_offset: usize) -> bool {
    match stmt {
        HirStmt::Act(act) => is_probe_act(act, subject, target_offset),
        HirStmt::Trigger(trigger) => trigger
            .acts
            .iter()
            .any(|act| is_probe_act(act, subject, target_offset)),
        _ => false,
    }
}

fn batch_has(stmt: &HirStmt, dst_var: usize, src_var: usize) -> bool {
    matches!(
        stmt,
        HirStmt::VarBatch(batch)
            if batch
                .transfers
                .iter()
                .any(|t| t.dst_var == dst_var && t.src_var == src_var)
    )
}

fn position(is_match: impl Fn(&HirStmt) -> bool, stmts: &[HirStmt], what: &str) -> usize {
    stmts
        .iter()
        .position(is_match)
        .unwrap_or_else(|| panic!("{what} did not survive optimization"))
}

/// Whether the probe act closed the run (`Some(true)`) or was hoisted ahead of
/// a still-coalesced run (`Some(false)`).
fn run_closed_around_probe(
    optimized: &HirModule,
    subject: usize,
    target_offset: usize,
) -> Option<bool> {
    let act = position(
        |stmt| stmt_has_probe_act(stmt, subject, target_offset),
        &optimized.stmts,
        "probe act",
    );
    let first = position(
        |stmt| batch_has(stmt, PROBE_DST_A, PROBE_SRC_A),
        &optimized.stmts,
        "first probe transfer",
    );
    let second = position(
        |stmt| batch_has(stmt, PROBE_DST_B, PROBE_SRC_B),
        &optimized.stmts,
        "second probe transfer",
    );
    match (first == second, act < first, first < act && act < second) {
        // Not page-backed: the act is a scalar value touch, so it hoists
        // ahead of the one run both transfers share.
        (true, true, _) => Some(false),
        // Page-backed: the machinery touch closes the first run, and the
        // second transfer opens a new one behind the act.
        (false, _, true) => Some(true),
        _ => panic!(
            "probe act was neither hoisted ahead of a shared run nor placed \
             between two split runs (act={act}, first={first}, second={second})"
        ),
    }
}

fn probe_module(module: &HirModule, subject: usize, target_offset: usize) -> HirModule {
    let mut probed = module.clone();
    let mut stmts = probe_suffix(subject, target_offset);
    stmts.extend(probed.stmts);
    probed.stmts = stmts;
    probed
}

/// The call-site fixture: `subject` is page-backed only through a `Call` arg
/// or a subroutine param/result.
fn call_site_module() -> HirModule {
    let mut vars = vec![
        HirVarDecl::new(0, 500), // call argument
        HirVarDecl::new(1, 500), // subroutine parameter (unread by the body)
        HirVarDecl::new(2, 500), // subroutine result (unwritten by the body)
        HirVarDecl::new(3, 500), // first call destination
        HirVarDecl::new(4, 500), // second call destination
        HirVarDecl::new(5, 500), // body-local transfer source
        HirVarDecl::new(6, 500), // body-local transfer destination
    ];
    for id in [PROBE_DST_A, PROBE_SRC_A, PROBE_DST_B, PROBE_SRC_B] {
        vars.push(var_decl(id));
    }
    let func = HirFunc::new(
        0,
        vec![1],
        Some(2),
        // A transfer body is outside the multi-site inline whitelist, so two
        // call sites keep the subroutine shared and the pins observable.
        vec![HirStmt::VarTransfer(transfer(6, 5, 7))],
    );
    let stmts = vec![
        HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(0)], Some(3))),
        HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(0)], Some(4))),
    ];
    HirModule {
        vars,
        stmts,
        funcs: vec![func],
        strings: Vec::new(),
        files: Vec::new(),
        blobs: Vec::new(),
    }
}

#[test]
fn call_argument_pin_closes_the_transfer_run_on_a_machinery_touch() {
    let module = call_site_module();
    for target_offset in MACHINERY_TARGET_OFFSETS {
        let optimized = optimize_module(&probe_module(&module, 0, target_offset))
            .expect("call fixture optimizes");
        assert_eq!(
            run_closed_around_probe(&optimized, 0, target_offset),
            Some(true),
            "a Call argument is page-backed, so a field touch at +{target_offset} is machinery and must close the run"
        );
    }
}

#[test]
fn subroutine_params_and_results_close_the_transfer_run_on_a_machinery_touch() {
    let module = call_site_module();
    for subject in [1, 2] {
        for target_offset in MACHINERY_TARGET_OFFSETS {
            let optimized = optimize_module(&probe_module(&module, subject, target_offset))
                .expect("call fixture optimizes");
            assert_eq!(
                run_closed_around_probe(&optimized, subject, target_offset),
                Some(true),
                "subroutine param/result var {subject} is page-backed; a field touch at +{target_offset} must close the run"
            );
        }
    }
}

// ---- Agreement: CellMap's page-backed set == analyze_storage's pin set ----

/// One fixture exercising declarations, transfer/store sources, `Call` args,
/// subroutine params/results, and nested bodies. Ids 40..43 are reserved for
/// the probe run and are not subject vars.
fn agreement_fixture() -> HirModule {
    let mut vars = vec![
        HirVarDecl::pinned(0, 0), // declaration pin
        HirVarDecl::new(1, 500),  // root transfer source
        HirVarDecl::new(2, 500),  // root store source
        HirVarDecl::new(3, 500),  // Call argument
        HirVarDecl::new(4, 500),  // subroutine parameter
        HirVarDecl::new(5, 500),  // subroutine result
        HirVarDecl::new(6, 500),  // nested body transfer source
        HirVarDecl::new(7, 500),  // ordinary dword scalar
        HirVarDecl::new(8, 0),    // ordinary boolean scalar
        HirVarDecl::new(9, 500),  // root transfer destination
        HirVarDecl::new(10, 500), // nested body transfer destination
        HirVarDecl::new(11, 500), // func body transfer destination
        HirVarDecl::new(12, 500), // first call destination
        HirVarDecl::new(13, 500), // second call destination
        HirVarDecl::new(16, 500), // func body transfer source
        HirVarDecl::new(20, 500), // Once body transfer source
        HirVarDecl::new(21, 500), // While body transfer source
        HirVarDecl::new(22, 500), // Until body transfer source
        HirVarDecl::new(23, 500), // Edge body transfer source
        HirVarDecl::new(24, 500), // Every body transfer source
        HirVarDecl::new(25, 500), // After body transfer source
        HirVarDecl::new(30, 500), // Every counter
        HirVarDecl::new(31, 500), // After counter
        HirVarDecl::new(32, 500), // Until latch
        HirVarDecl::new(33, 500), // Edge current sample
        HirVarDecl::new(34, 500), // Edge previous sample
    ];
    for id in [PROBE_DST_A, PROBE_SRC_A, PROBE_DST_B, PROBE_SRC_B] {
        vars.push(var_decl(id));
    }
    let func = HirFunc::new(
        0,
        vec![4],
        Some(5),
        vec![HirStmt::VarTransfer(transfer(11, 16, 7))],
    );
    let stmts = vec![
        // A `Call` is not a batch member, so the scheduler flushes the probe
        // run before it; the fixture's own batching stays separate.
        HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(3)], Some(12))),
        HirStmt::Call(HirCall::new(0, vec![HirCallArg::var(3)], Some(13))),
        HirStmt::VarTransfer(transfer(9, 1, 7)),
        HirStmt::VarStore(HirVarStore::to_addr(2, 0x640B58, 7)),
        HirStmt::When(HirWhen::new(
            vec![HirCond::always()],
            vec![HirStmt::VarTransfer(transfer(10, 6, 7))],
        )),
        HirStmt::Act(HirAct::set_var_const(8, 7, 1, 0xFFFF_FFFF)),
        HirStmt::Trigger(
            HirTrigger::new(0, vec![HirCond::var_deaths(8, 10, 1)], vec![], true)
                .expect("legal trigger"),
        ),
        // Every control body gets its own transfer source so the test proves
        // the pin walk descends into that arm, not just into `When`.
        HirStmt::Once(HirOnce::new(
            vec![HirCond::always()],
            vec![HirStmt::VarTransfer(transfer(10, 20, 7))],
        )),
        HirStmt::While(HirWhile::new(
            vec![HirCond::always()],
            vec![HirStmt::VarTransfer(transfer(10, 21, 7))],
        )),
        HirStmt::Until(HirUntil::new(
            vec![HirCond::deaths(0, 10, 1, 0)],
            vec![HirStmt::VarTransfer(transfer(10, 22, 7))],
            32,
        )),
        HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Rising,
            vec![HirCond::always()],
            vec![HirStmt::VarTransfer(transfer(10, 23, 7))],
            33,
            34,
        )),
        HirStmt::Every(HirEvery::new(
            2,
            30,
            vec![HirStmt::VarTransfer(transfer(10, 24, 7))],
        )),
        HirStmt::After(HirAfter::new(
            2,
            31,
            vec![HirStmt::VarTransfer(transfer(10, 25, 7))],
        )),
    ];
    HirModule {
        vars,
        stmts,
        funcs: vec![func],
        strings: Vec::new(),
        files: Vec::new(),
        blobs: Vec::new(),
    }
}

fn page_backed_set(module: &HirModule) -> BTreeSet<usize> {
    let optimized = optimize_module(module).expect("fixture optimizes");
    let plan = analyze_storage(&optimized);
    optimized
        .vars
        .iter()
        .filter(|v| matches!(plan.storage(v.id), Some(StorageClass::TriggerPage { .. })))
        .map(|v| v.id)
        .collect()
}

#[test]
fn cell_map_page_backing_agrees_with_analyze_storage_for_every_fixture_var() {
    let fixture = agreement_fixture();
    let expected = page_backed_set(&fixture);
    // Every arm of the pin walk must actually be exercised, including one
    // distinct transfer source per nested control body.
    for id in [0, 1, 2, 3, 4, 5, 6, 16, 20, 21, 22, 23, 24, 25] {
        assert!(
            expected.contains(&id),
            "fixture var {id} must be page-backed by analyze_storage"
        );
    }
    for id in [7, 8, 9, 10, 11, 12, 13, 30, 31, 32, 33, 34] {
        assert!(
            !expected.contains(&id),
            "fixture var {id} must stay cell-backed"
        );
    }

    let subjects: Vec<usize> = fixture
        .vars
        .iter()
        .map(|v| v.id)
        .filter(|id| ![PROBE_DST_A, PROBE_SRC_A, PROBE_DST_B, PROBE_SRC_B].contains(id))
        .collect();
    for subject in subjects {
        for target_offset in MACHINERY_TARGET_OFFSETS {
            let optimized = optimize_module(&probe_module(&fixture, subject, target_offset))
                .expect("probed fixture optimizes");
            assert_eq!(
                run_closed_around_probe(&optimized, subject, target_offset),
                Some(expected.contains(&subject)),
                "CellMap page-backedness disagrees with analyze_storage for var {subject} at field +{target_offset}"
            );
        }
    }
}
