//! Measurement fixture for the C3 question: does removing a dead root gate
//! expose an act run that the *current* pipeline still pays for?
//!
//! `drop_dead_triggers` removes an empty unconditional `when` after scheduling
//! but deliberately does not re-schedule. The acts before and after the gate
//! were packed into separate root trigger statements while the gate was still
//! present, and lowering does not merge adjacent root `Trigger` statements when
//! the block has no variable batch. C3 (moving root packing into lowering) is
//! the proposed fix.
//!
//! Observed on the R4 tree (2026-11-14):
//!
//! | shape | scheduled root triggers | acts |
//! |---|---|---|
//! | current (acts, dead gate, acts) | 2 | 4 |
//! | hand-built coalesced (what C3 would emit) | 1 | 4 |
//!
//! **Derived delta: 1 scheduled trigger, i.e. one 2,408-byte `RawTrigger`
//! record, per adjacent act-run pair split by a removed dead gate.** That is a
//! nonzero static win, so it is recorded here as input to a future C3 ticket.
//! It is *not* a claim that the inversion is worth its golden churn: this
//! fixture is one boundary and one trigger, and the ticket's acceptance
//! explicitly leaves C3 out of scope. If a future change makes the two shapes
//! equal, this test fails and the observed counts above must be re-taken
//! before any inversion work starts.
//!
//! The byte figure is derived at this seam, not read from emitted LIR: each
//! scheduled root `Trigger` statement becomes exactly one
//! `RAW_TRIGGER_BYTE_SIZE`-byte record because the scheduler chunks at
//! `ACT_COUNT` (64) and this fixture's runs are far below that. The LIR byte
//! total is not available here because `eudkit-opt` does not depend on
//! `eudkit-lower`.

use eudkit_hir::{HirAct, HirModule, HirStmt, HirWhen};
use eudkit_lir::RAW_TRIGGER_BYTE_SIZE;
use eudkit_opt::optimize_module;

/// The four acts that surround the dead gate. Distinct action values keep the
/// fixture readable and give the scheduler no batching work (there are no
/// transfers or stores, so every act is a bare root act).
fn surrounding_acts() -> [HirAct; 4] {
    [
        HirAct::set_deaths(0, 7, 0x11, 0),
        HirAct::set_deaths(0, 7, 0x12, 0),
        HirAct::set_deaths(0, 7, 0x21, 0),
        HirAct::set_deaths(0, 7, 0x22, 0),
    ]
}

fn module_with(stmts: Vec<HirStmt>) -> HirModule {
    HirModule {
        vars: Vec::new(),
        stmts,
        funcs: Vec::new(),
        strings: Vec::new(),
        files: Vec::new(),
        blobs: Vec::new(),
    }
}

/// `acts 0,1; dead gate; acts 2,3`.
///
/// The gate is an empty, unconditional `when`, which `drop_dead_triggers`
/// removes (both arms empty). `optimize_module` still schedules the acts on
/// either side as two separate runs.
fn current_shape() -> HirModule {
    let acts = surrounding_acts();
    module_with(vec![
        HirStmt::Act(acts[0].clone()),
        HirStmt::Act(acts[1].clone()),
        HirStmt::When(HirWhen::new(Vec::new(), Vec::new())),
        HirStmt::Act(acts[2].clone()),
        HirStmt::Act(acts[3].clone()),
    ])
}

/// The same acts with no gate: what C3's coalesced packing would produce.
fn coalesced_shape() -> HirModule {
    module_with(surrounding_acts().into_iter().map(HirStmt::Act).collect())
}

/// Count the scheduled root trigger records and their acts.
///
/// The fixture is built so the scheduled root contains only trigger records
/// (the dead gate is gone, and there are no transfers, stores, calls, or
/// control flow left). Any other statement is a fixture bug, not a shape the
/// measurement can compare.
fn scheduled_root(stmts: &[HirStmt]) -> (usize, usize) {
    let mut triggers = 0;
    let mut acts = 0;
    for stmt in stmts {
        match stmt {
            HirStmt::Trigger(trig) => {
                triggers += 1;
                acts += trig.acts.len();
            }
            other => panic!("fixture scheduled to a non-trigger root statement: {other:?}"),
        }
    }
    (triggers, acts)
}

#[test]
fn one_dead_gate_costs_one_scheduled_trigger_and_one_raw_trigger_record() {
    let current_input = current_shape();
    assert!(
        matches!(
            current_input.stmts.get(2),
            Some(HirStmt::When(when)) if when.conds.is_empty()
                && when.body.is_empty()
                && when.else_body.is_empty()
        ),
        "the fixture must contain the empty unconditional when that drop_dead_triggers removes"
    );

    let current = optimize_module(&current_input).expect("current shape is valid HIR");
    let coalesced = optimize_module(&coalesced_shape()).expect("coalesced shape is valid HIR");

    let (current_t, current_acts) = scheduled_root(&current.stmts);
    let (coalesced_t, coalesced_acts) = scheduled_root(&coalesced.stmts);

    // Both shapes are valid and carry the same program: the only difference is
    // where the root act runs were split by the now-removed gate.
    assert_eq!(current_acts, 4);
    assert_eq!(coalesced_acts, 4);

    // The observed counts pinned by the module docs.
    assert_eq!(current_t, 2, "acts before/after the dead gate stay split");
    assert_eq!(coalesced_t, 1, "one run of four acts packs into one trigger");

    let delta_t = current_t - coalesced_t;
    let delta_bytes = delta_t * RAW_TRIGGER_BYTE_SIZE;
    assert_eq!(delta_t, 1, "the C3 delta is one scheduled trigger");
    assert_eq!(
        delta_bytes, 2_408,
        "one scheduled root trigger is one derived 2,408-byte RawTrigger record"
    );
}
