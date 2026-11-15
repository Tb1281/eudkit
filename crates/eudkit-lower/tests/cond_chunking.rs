//! ADR 0027: `>16` condition auto-chunking at the lowering/LIR seam.
//!
//! A raw `Trigger (16C, 64A)` stays capped at 16 conditions with
//! `EUDSL-BOUNDS`. Structured control flow (`when`, `once`, `whileLoop`, and
//! the opt-desugared `until` / `onRising` / `onFalling`) must instead split
//! its condition list into 16-condition chunks so a 17/32/33-condition gate
//! lowers and assembles cleanly.
//!
//! These tests pin the single-gate LIR shape at `<= 16` and exercise the
//! chunked path at 17/32/33 through lowering, assembly, and runtime.

use eudkit_hir::{
    HirAct, HirCond, HirDocument, HirEdge, HirEdgeDirection, HirError, HirModule, HirOnce, HirStmt,
    HirTrigger, HirUntil, HirVarDecl, HirWhen, HirWhile,
};
use eudkit_lir::{LirDocument, SimulationState, init_simulation, lir2payload, LirItem};
use eudkit_lower::{lower_document, lower_optimized_module};
use eudkit_opt::cell::analyze_storage;
use eudkit_opt::optimize_module;

/// Distinct conditions, keyed by amount and unit. Varying `unit` keeps opt's
/// condition-fusion pass from collapsing the list below `n`.
fn conds(n: usize) -> Vec<HirCond> {
    (0..n)
        .map(|i| HirCond::deaths(0, 10, (i + 1) as u32, i as u16))
        .collect()
}

fn body() -> Vec<HirStmt> {
    vec![HirStmt::Act(HirAct::set_deaths(0, 7, 1, 0))]
}

fn module_with(stmt: HirStmt, vars: Vec<HirVarDecl>) -> HirModule {
    let mut module = HirModule::new();
    module.vars = vars;
    module.stmts = vec![stmt];
    module
}

/// Every structured form from the ADR, at one condition count.
fn forms(n: usize) -> Vec<(&'static str, HirModule)> {
    vec![
        (
            "when",
            module_with(HirStmt::When(HirWhen::new(conds(n), body())), vec![]),
        ),
        (
            "when+else",
            module_with(
                HirStmt::When(HirWhen::with_else(conds(n), body(), body())),
                vec![],
            ),
        ),
        (
            "when(empty)+else",
            module_with(
                HirStmt::When(HirWhen::with_else(conds(n), Vec::new(), body())),
                vec![],
            ),
        ),
        (
            "once",
            module_with(HirStmt::Once(HirOnce::new(conds(n), body())), vec![]),
        ),
        (
            "until",
            module_with(
                HirStmt::Until(HirUntil::new(conds(n), body(), 0)),
                vec![HirVarDecl::new(0, 1)],
            ),
        ),
        (
            "whileLoop",
            module_with(HirStmt::While(HirWhile::new(conds(n), body())), vec![]),
        ),
        (
            "onRising",
            module_with(
                HirStmt::Edge(HirEdge::new(
                    HirEdgeDirection::Rising,
                    conds(n),
                    body(),
                    0,
                    1,
                )),
                vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)],
            ),
        ),
        (
            "onFalling",
            module_with(
                HirStmt::Edge(HirEdge::new(
                    HirEdgeDirection::Falling,
                    conds(n),
                    body(),
                    0,
                    1,
                )),
                vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)],
            ),
        ),
    ]
}

fn assert_every_trigger_chunked(lir: &LirDocument, label: &str, n: usize) {
    let mut total = 0usize;
    let mut condition_bearing = 0usize;
    for (idx, item) in lir.module.items.iter().enumerate() {
        if let LirItem::Trigger(trig) = item {
            assert!(
                trig.conds.len() <= eudkit_lir::COND_COUNT,
                "{label} n={n}: LIR item {idx} carries {} conditions",
                trig.conds.len()
            );
            total += trig.conds.len();
            condition_bearing += usize::from(!trig.conds.is_empty());
        }
    }
    assert!(
        total >= n,
        "{label} n={n}: all {n} conditions must stay represented across chunks; found {total}"
    );
    let expected_gates = n.div_ceil(eudkit_lir::COND_COUNT);
    assert!(
        condition_bearing >= expected_gates,
        "{label} n={n}: expected at least {expected_gates} condition-bearing triggers, saw {condition_bearing}"
    );
}

#[test]
fn lowering_and_lir_assembly_chunk_all_structured_forms_at_17_32_33() {
    for n in [17usize, 32, 33] {
        for (label, module) in forms(n) {
            let document = HirDocument::new(module);
            let lir = lower_document(&document)
                .unwrap_or_else(|err| panic!("{label} n={n}: lowering failed: {err}"));
            assert_every_trigger_chunked(&lir, label, n);
            lir2payload(&lir).unwrap_or_else(|err| {
                panic!("{label} n={n}: LIR assembly failed with TooManyConditions: {err}")
            });
        }
    }
}

#[test]
fn gt16_when_survives_opt_and_reaches_lowering_cleanly() {
    let module = module_with(HirStmt::When(HirWhen::new(conds(17), body())), Vec::new());

    let optimized = optimize_module(&module).expect("17-cond when must optimize");
    assert!(
        optimized
            .stmts
            .iter()
            .any(|stmt| matches!(stmt, HirStmt::When(when) if when.conds.len() == 17)),
        "opt must keep a >16 when as a when (it cannot leaf-pack it into a 16C trigger)"
    );

    // Lower the optimized module directly instead of re-running opt through
    // `lower_document`; this is the shape the compile pipeline actually sees.
    let storage = analyze_storage(&optimized);
    let lir = lower_optimized_module(&optimized, &storage, &[])
        .expect("17-cond when must lower from the optimized module");
    assert_every_trigger_chunked(&lir, "when-after-opt", 17);
    lir2payload(&lir).expect("17-cond when must assemble after opt");
}

#[test]
fn once_with_empty_body_and_gt16_conditions_reaches_lowering_cleanly() {
    // `lower_document` runs opt, which drops an empty `once` before lowering.
    // Build the plan from the raw module directly so this exercises the
    // empty-body `lower_once` chunk path the ADR requires.
    let module = module_with(
        HirStmt::Once(HirOnce::new(conds(17), Vec::new())),
        Vec::new(),
    );
    let storage = analyze_storage(&module);
    let lir = lower_optimized_module(&module, &storage, &[])
        .expect("17-cond empty once must lower directly");
    assert_every_trigger_chunked(&lir, "empty-once", 17);
    lir2payload(&lir).expect("17-cond empty once must assemble");
}

#[test]
fn raw_trigger_above_sixteen_conditions_still_rejects_with_eudsl_bounds() {
    let err = HirTrigger::new(0, conds(17), Vec::new(), true).unwrap_err();
    assert_eq!(err, HirError::TooManyConditions(17));
    assert!(err.to_string().contains("EUDSL-BOUNDS"));
}

#[test]
fn sixteen_condition_when_lir_shape_is_unchanged() {
    // A non-leaf body keeps the outer gate as a structured `When` (a leaf
    // body would be packed into a raw 16C trigger by opt). Today this lowers
    // to exactly: gate(16C) -> packed body(1C) -> separate restore(0C).
    let inner = HirStmt::When(HirWhen::new(vec![HirCond::deaths(0, 10, 17, 99)], body()));
    let module = module_with(
        HirStmt::When(HirWhen::new(conds(16), vec![inner])),
        Vec::new(),
    );

    let lir = lower_document(&HirDocument::new(module)).expect("16-cond when must lower");
    assert_every_trigger_chunked(&lir, "when16-golden", 16);

    let cond_counts: Vec<usize> = lir
        .module
        .items
        .iter()
        .filter_map(|item| match item {
            LirItem::Trigger(trig) => Some(trig.conds.len()),
            LirItem::Blob(_) => None,
        })
        .collect();
    let act_counts: Vec<usize> = lir
        .module
        .items
        .iter()
        .filter_map(|item| match item {
            LirItem::Trigger(trig) => Some(trig.acts.len()),
            LirItem::Blob(_) => None,
        })
        .collect();
    assert_eq!(
        cond_counts,
        vec![16, 1, 0],
        "a <=16 when must keep today's gate/body/restore LIR shape"
    );
    assert_eq!(
        act_counts,
        vec![1, 1, 1],
        "a <=16 when must keep one action per gate/body/restore trigger"
    );
    lir2payload(&lir).expect("16-cond when must assemble");
}

/// Conditions keyed to distinct player EPDs so the LIR simulator can flip
/// them independently: condition `i` passes when its player EPD equals
/// `i + 1`. Players start at 20 to avoid the `CURRENT_PLAYER` sentinel (13).
fn cond_player(i: usize) -> u32 {
    (i + 20) as u32
}

fn player_conds(n: usize) -> Vec<HirCond> {
    (0..n)
        .map(|i| HirCond::deaths(cond_player(i), 10, (i + 1) as u32, i as u16))
        .collect()
}

/// Write one true/false frame for [`player_conds`].
fn set_truth(sim: &mut SimulationState, truth: &[bool]) {
    for (i, &passes) in truth.iter().enumerate() {
        let value = if passes { (i + 1) as u32 } else { 0 };
        sim.write_epd(cond_player(i), value);
    }
}

fn marker_body() -> Vec<HirStmt> {
    vec![HirStmt::Act(HirAct::set_deaths(0, 8, 1, 0))]
}

#[test]
fn all_false_chunked_when_costs_exactly_one_trigger() {
    for n in [17usize, 33] {
        let module = module_with(
            HirStmt::When(HirWhen::new(player_conds(n), marker_body())),
            Vec::new(),
        );
        let lir = lower_document(&HirDocument::new(module)).expect("chunked when must lower");
        assert_every_trigger_chunked(&lir, "when-1T", n);

        let mut sim = init_simulation(&lir).expect("1T when must simulate");
        set_truth(&mut sim, &vec![false; n]);
        let cost = sim.step_loop();
        assert_eq!(
            cost.triggers, 1,
            "n={n}: an all-false chunked gate must cost exactly 1T"
        );
        assert_eq!(
            cost.visited_items,
            vec![0],
            "n={n}: only g0 may be evaluated on an all-false pass"
        );
    }
}

#[test]
fn chunked_when_and_once_keep_tf_ft_tt_frame_order() {
    for n in [17usize, 33] {
        for (label, stmt) in [
            (
                "when",
                HirStmt::When(HirWhen::new(player_conds(n), marker_body())),
            ),
            (
                "once",
                HirStmt::Once(HirOnce::new(player_conds(n), marker_body())),
            ),
        ] {
            let lir = lower_document(&HirDocument::new(module_with(stmt, Vec::new())))
                .unwrap_or_else(|err| panic!("{label} n={n}: lower failed: {err}"));
            assert_every_trigger_chunked(&lir, label, n);
            let mut sim = init_simulation(&lir).expect("chunked frame-order sim");

            // T/F: chunk 0 passes, the last chunk fails.
            let mut tf = vec![true; n];
            tf[n - 1] = false;
            set_truth(&mut sim, &tf);
            let cost = sim.step_loop();
            assert_eq!(
                sim.read_epd(0),
                0,
                "{label} n={n}: T/F must not run the body"
            );
            assert_eq!(
                cost.visited_items.first(),
                Some(&0),
                "{label} n={n}: T/F must start at g0"
            );

            // F/T after T/F: a stale g0.next would misfire into the body.
            let mut ft = vec![true; n];
            ft[0] = false;
            set_truth(&mut sim, &ft);
            sim.step_loop();
            assert_eq!(
                sim.read_epd(0),
                0,
                "{label} n={n}: F/T after T/F must not run the body"
            );

            // T/T, then T/T again.
            set_truth(&mut sim, &vec![true; n]);
            sim.step_loop();
            assert_eq!(
                sim.read_epd(0),
                1,
                "{label} n={n}: first T/T must run the body once"
            );
            set_truth(&mut sim, &vec![true; n]);
            sim.step_loop();
            let expected = if label == "once" { 1 } else { 2 };
            assert_eq!(
                sim.read_epd(0),
                expected,
                "{label} n={n}: second T/T frame order"
            );
        }
    }
}

#[test]
fn chunked_while_runs_multiple_body_passes_before_chunk_1_fails() {
    let n = 33usize;
    let mut conds = player_conds(n);
    // Chunk 1's first condition is a countdown: its player EPD >= 1.
    conds[16] = HirCond::deaths(cond_player(16), 0, 1, 0);
    let module = module_with(
        HirStmt::While(HirWhile::new(
            conds,
            vec![
                HirStmt::Act(HirAct::set_deaths(0, 8, 1, 0)), // marker += 1
                HirStmt::Act(HirAct::set_deaths(cond_player(16), 9, 1, 0)), // countdown -= 1
            ],
        )),
        Vec::new(),
    );
    let lir = lower_document(&HirDocument::new(module)).expect("chunked while must lower");
    assert_every_trigger_chunked(&lir, "while-multi", n);

    let body_item = lir
        .module
        .items
        .iter()
        .position(|item| match item {
            LirItem::Trigger(trig) => trig
                .acts
                .iter()
                .any(|act| act.act.acttype == 45 && act.act.dest == cond_player(16)),
            LirItem::Blob(_) => false,
        })
        .expect("while body trigger must contain the countdown action");

    let mut sim = init_simulation(&lir).expect("chunked while must simulate");
    set_truth(&mut sim, &vec![true; n]);
    sim.write_epd(cond_player(16), 2); // two remaining body passes
    let cost = sim.step_loop();
    assert_eq!(sim.read_epd(0), 2, "the while body must execute twice");
    assert_eq!(
        cost.visited_items
            .iter()
            .filter(|&&idx| idx == body_item)
            .count(),
        2,
        "the body trigger must run in both passes before chunk 1 fails"
    );
    assert_eq!(
        sim.read_epd(cond_player(16)),
        0,
        "the chunk-1 countdown must reach zero"
    );
}
