//! Gate satisfiability and dead-body folding (T-WP13, ADR 0031).
//!
//! These tests drive the public optimizer seam: a `when` whose condition list
//! is contradictory folds to `HirCond::never()` and disappears (or promotes
//! its `else` arm), while `once`/`while` liveness gates are left alone.

use eudkit_hir::{
    HirAct, HirBlob, HirBlobReloc, HirCond, HirDocument, HirModule, HirOnce, HirReloc, HirStmt,
    HirTrigger, HirVarDecl, HirVarTransfer, HirWhen, HirWhile, hir_to_json,
};
use eudkit_opt::folds::FoldKind;
use eudkit_opt::{optimize_module, optimize_module_with_folds};

const AT_LEAST: u8 = 0;
const AT_MOST: u8 = 1;
const EXACTLY: u8 = 10;

fn when(conds: Vec<HirCond>, body: Vec<HirStmt>) -> HirModule {
    module_with_stmt(HirStmt::When(HirWhen::new(conds, body)))
}

fn when_else(conds: Vec<HirCond>, body: Vec<HirStmt>, else_body: Vec<HirStmt>) -> HirModule {
    module_with_stmt(HirStmt::When(HirWhen::with_else(conds, body, else_body)))
}

fn module_with_stmt(stmt: HirStmt) -> HirModule {
    let mut module = HirModule::new();
    module.stmts = vec![stmt];
    module
}

fn module_with_stmts(stmts: Vec<HirStmt>) -> HirModule {
    let mut module = HirModule::new();
    module.stmts = stmts;
    module
}

fn act(value: u32) -> HirStmt {
    HirStmt::Act(HirAct::set_deaths(0, 7, value, 0))
}

fn body() -> Vec<HirStmt> {
    vec![act(1)]
}

fn deaths(comparison: u8, amount: u32) -> HirCond {
    HirCond::deaths(0, comparison, amount, 0)
}

fn optimized(module: &HirModule) -> Vec<HirStmt> {
    optimize_module(module).expect("module is valid HIR").stmts
}

fn is_flattened(stmts: &[HirStmt]) -> bool {
    stmts.is_empty()
}

/// Independent truth definition for one condition on a target whose value is
/// `value`: `AtLeast` is `>=`, `AtMost` is `<=`, `Exactly` is `==`.
fn holds(comparison: u8, amount: u32, value: u32) -> bool {
    match comparison {
        AT_LEAST => value >= amount,
        AT_MOST => value <= amount,
        EXACTLY => value == amount,
        _ => unreachable!("test only uses the three comparison codes"),
    }
}

fn satisfiable(existing: u8, a: u32, incoming: u8, b: u32) -> bool {
    // Amounts in the table are 3/5/7; scanning a little wider is still an
    // independent check of "does any target value satisfy both conditions?".
    (0..=20).any(|value| holds(existing, a, value) && holds(incoming, b, value))
}

fn has_condition(conds: &[HirCond], comparison: u8, amount: u32) -> bool {
    conds
        .iter()
        .any(|cond| cond.condtype == 15 && cond.comparison == comparison && cond.amount == amount)
}

fn has_never(conds: &[HirCond]) -> bool {
    conds.iter().any(|cond| cond.condtype == 23)
}

#[test]
fn at_least_5_and_at_most_3_is_never() {
    let module = when(vec![deaths(AT_LEAST, 5), deaths(AT_MOST, 3)], body());
    assert!(
        is_flattened(&optimized(&module)),
        "an unsatisfiable gate must drop its body"
    );

    let (optimized_module, log) = optimize_module_with_folds(&module).expect("valid HIR");
    assert!(optimized_module.stmts.is_empty());
    let kinds: Vec<FoldKind> = log.events().iter().map(|event| event.kind).collect();
    assert_eq!(
        kinds,
        vec![FoldKind::CondFold, FoldKind::GateDrop],
        "the contradiction must be reported as a fold, then a drop: {:?}",
        log.events()
    );
    assert_eq!(log.events()[0].site, "when#0");
    assert_eq!(log.events()[0].after, "Never");
    assert_eq!(log.events()[1].after, "dropped");
}

#[test]
fn explicit_never_is_preserved_as_written() {
    // A frontend constant false (`condAny()`, a statically false field read) is
    // not a contradiction this pass derived. It must keep its existing shape so
    // the frontend cost goldens do not move.
    let lone = optimized(&when(vec![HirCond::never()], body()));
    assert_eq!(lone.len(), 1, "an explicit Never gate stays a gate");
    match &lone[0] {
        HirStmt::Trigger(trigger) => {
            assert_eq!(trigger.conds, vec![HirCond::never()]);
        }
        other => panic!("expected the packed Never gate, got {other:?}"),
    }

    let mixed = optimized(&when(vec![HirCond::never(), deaths(AT_LEAST, 5)], body()));
    match &mixed[0] {
        HirStmt::Trigger(trigger) => {
            assert_eq!(
                trigger.conds.len(),
                2,
                "Never stays next to the other bound"
            );
            assert!(has_never(&trigger.conds));
            assert!(has_condition(&trigger.conds, AT_LEAST, 5));
        }
        other => panic!("expected the packed mixed gate, got {other:?}"),
    }
}

#[test]
fn full_comparison_truth_table() {
    let comparisons = [AT_LEAST, AT_MOST, EXACTLY];
    let amounts = [3, 5, 7];

    for &existing in &comparisons {
        for &incoming in &comparisons {
            for &a in &amounts {
                for &b in &amounts {
                    let module = when(vec![deaths(existing, a), deaths(incoming, b)], body());
                    let dropped = is_flattened(&optimized(&module));
                    let expected_drop = !satisfiable(existing, a, incoming, b);
                    assert_eq!(
                        dropped, expected_drop,
                        "existing {existing} {a} + incoming {incoming} {b}: \
                         expected satisfiable={}, got dropped={dropped}",
                        !expected_drop
                    );
                }
            }
        }
    }
}

/// A contradiction is only a contradiction on the *same* target. Different
/// player/unit/mask/eudx/relocation fields must keep both conditions.
#[test]
fn different_targets_never_fold_to_never() {
    let cases: Vec<(&str, HirCond, HirCond)> = vec![
        ("player", deaths(AT_LEAST, 5), {
            let mut cond = deaths(AT_MOST, 3);
            cond.player = 1;
            cond
        }),
        ("unit", deaths(AT_LEAST, 5), {
            let mut cond = deaths(AT_MOST, 3);
            cond.unit = 1;
            cond
        }),
        ("locid", deaths(AT_LEAST, 5), {
            let mut cond = deaths(AT_MOST, 3);
            cond.locid = 0x00ff_00ff;
            cond
        }),
        ("eudx", deaths(AT_LEAST, 5), {
            let mut cond = deaths(AT_MOST, 3);
            cond.eudx = 0x7fff;
            cond
        }),
        (
            "relocs",
            {
                let mut cond = deaths(AT_LEAST, 5);
                cond.relocs = vec![HirReloc::var(4, 0, 20, 1)];
                cond
            },
            {
                let mut cond = deaths(AT_MOST, 3);
                cond.relocs = vec![HirReloc::var(4, 1, 20, 1)];
                cond
            },
        ),
    ];

    for (field, existing, incoming) in cases {
        let mut module = when(vec![existing, incoming], body());
        module.vars = vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)];
        assert!(
            !is_flattened(&optimized(&module)),
            "a different {field} must not fold to Never"
        );
    }
}

#[test]
fn never_and_else_promotes_else() {
    let module = when_else(
        vec![deaths(AT_LEAST, 5), deaths(AT_MOST, 3)],
        body(),
        vec![act(2)],
    );
    let expected = module_with_stmts(vec![act(2)]);

    assert_eq!(
        optimized(&module),
        optimized(&expected),
        "a Never gate with an else arm must become the else body"
    );

    let (_, log) = optimize_module_with_folds(&module).expect("valid HIR");
    let kinds: Vec<FoldKind> = log.events().iter().map(|event| event.kind).collect();
    assert_eq!(kinds, vec![FoldKind::CondFold, FoldKind::GateDrop]);
    assert_eq!(log.events()[1].after, "else-promoted");
}

#[test]
fn always_opens_then_and_drops_else() {
    let module = when_else(vec![HirCond::always()], body(), vec![act(2)]);
    let expected = module_with_stmts(body());

    assert_eq!(
        optimized(&module),
        optimized(&expected),
        "an Always gate must open and drop its else arm"
    );

    let (_, log) = optimize_module_with_folds(&module).expect("valid HIR");
    let kinds: Vec<FoldKind> = log.events().iter().map(|event| event.kind).collect();
    assert_eq!(kinds, vec![FoldKind::GateOpen]);
    assert_eq!(log.events()[0].after, "opened");
}

#[test]
fn empty_gate_with_else_opens_and_drops_else() {
    let module = when_else(Vec::new(), body(), vec![act(2)]);
    let expected = module_with_stmts(body());
    assert_eq!(optimized(&module), optimized(&expected));
}

#[test]
fn once_and_while_are_not_folded() {
    let conds = vec![deaths(AT_LEAST, 5), deaths(AT_MOST, 3)];
    let mut module = module_with_stmts(vec![
        HirStmt::Once(HirOnce::new(conds.clone(), vec![transfer(0, 1)])),
        HirStmt::While(HirWhile::new(conds.clone(), vec![transfer(0, 1)])),
    ]);
    module.vars = vec![HirVarDecl::new(0, 0), HirVarDecl::new(1, 0)];

    let stmts = optimized(&module);
    let mut saw_once = false;
    let mut saw_while = false;
    for stmt in &stmts {
        match stmt {
            HirStmt::Once(once) => {
                saw_once = true;
                assert!(
                    has_condition(&once.conds, AT_LEAST, 5)
                        && has_condition(&once.conds, AT_MOST, 3),
                    "the contradictory once conditions must stay intact: {:?}",
                    once.conds
                );
                assert!(!has_never(&once.conds));
                assert!(!once.body.is_empty(), "the once body must survive");
            }
            HirStmt::While(while_loop) => {
                saw_while = true;
                assert!(
                    has_condition(&while_loop.conds, AT_LEAST, 5)
                        && has_condition(&while_loop.conds, AT_MOST, 3),
                    "the contradictory while conditions must stay intact: {:?}",
                    while_loop.conds
                );
                assert!(!has_never(&while_loop.conds));
                assert!(!while_loop.body.is_empty(), "the while body must survive");
            }
            _ => {}
        }
    }
    assert!(saw_once, "a once node must survive: {stmts:?}");
    assert!(saw_while, "a while node must survive: {stmts:?}");
}

fn transfer(dst_var: usize, src_var: usize) -> HirStmt {
    HirStmt::VarTransfer(HirVarTransfer {
        dst_var,
        src_var,
        modifier: 7,
    })
}

/// ADR 0027 folds the *full* condition list before lowering chunks it. A
/// contradiction inside the first 16 conditions and one that spans the chunk
/// boundary must both drop the whole gate.
#[test]
fn chunked_contradiction_drops_the_whole_gate() {
    // Chunk 0: the pair is at indices 0 and 1.
    let mut first_chunk = distinct_conds(15);
    first_chunk.insert(0, deaths_on_unit(AT_LEAST, 5, 99));
    first_chunk.insert(1, deaths_on_unit(AT_MOST, 3, 99));
    assert_eq!(first_chunk.len(), 17);
    assert!(
        is_flattened(&optimized(&when(first_chunk, body()))),
        "a contradiction in chunk 0 must drop the whole 17-cond gate"
    );

    // Chunk boundary: AtLeast is the last cond of chunk 0 (index 15) and
    // AtMost is the first cond of chunk 1 (index 16).
    let mut spanning = distinct_conds(15);
    spanning.push(deaths_on_unit(AT_LEAST, 5, 99));
    spanning.push(deaths_on_unit(AT_MOST, 3, 99));
    assert_eq!(spanning.len(), 17);
    assert!(
        is_flattened(&optimized(&when(spanning, body()))),
        "a contradiction spanning chunks must drop the whole 17-cond gate"
    );

    // Chunk 1 only: 18 conditions put the pair entirely in the second chunk.
    let mut second_chunk = distinct_conds(16);
    second_chunk.push(deaths_on_unit(AT_LEAST, 5, 99));
    second_chunk.push(deaths_on_unit(AT_MOST, 3, 99));
    assert_eq!(second_chunk.len(), 18);
    assert!(
        is_flattened(&optimized(&when(second_chunk, body()))),
        "a contradiction contained in chunk 1 must drop the whole gate"
    );
}

/// Distinct conditions keyed by unit so the fold pass cannot collapse them.
fn distinct_conds(n: usize) -> Vec<HirCond> {
    (0..n)
        .map(|i| HirCond::deaths(0, AT_LEAST, (i + 1) as u32, i as u16))
        .collect()
}

fn deaths_on_unit(comparison: u8, amount: u32, unit: u16) -> HirCond {
    HirCond::deaths(0, comparison, amount, unit)
}

/// A `Never` body that declares a trigger an external blob relocation names
/// must be kept: dropping it would delete the referenced definition.
#[test]
fn referenced_body_is_kept() {
    let referenced = HirStmt::Trigger(
        HirTrigger::new(7, vec![HirCond::always()], Vec::new(), true).expect("legal trigger"),
    );
    let mut module = when(
        vec![deaths(AT_LEAST, 5), deaths(AT_MOST, 3)],
        vec![referenced],
    );
    module.blobs = vec![HirBlob::zeroed(8).with_relocs(vec![HirBlobReloc::trigger(0, 7, 0, 4)])];

    let (optimized_module, log) = optimize_module_with_folds(&module).expect("valid HIR");
    assert_eq!(
        optimized_module.stmts.len(),
        1,
        "the Never-gated body must survive: {:?}",
        optimized_module.stmts
    );
    match &optimized_module.stmts[0] {
        HirStmt::When(when) => {
            assert_eq!(when.conds, vec![HirCond::never()]);
            assert!(matches!(
                when.body.as_slice(),
                [HirStmt::Trigger(trigger)] if trigger.id == 7
            ));
        }
        other => panic!("expected the kept when, got {other:?}"),
    }

    let drop = log
        .events()
        .iter()
        .find(|event| event.kind == FoldKind::GateDrop)
        .expect("a referenced gate still records the attempted drop");
    assert_eq!(drop.after, "kept(referenced)");
}

#[test]
fn satisfiable_fixture_is_byte_identical() {
    let mut module = module_with_stmts(vec![
        HirStmt::When(HirWhen::new(
            vec![deaths(AT_LEAST, 5), deaths(AT_MOST, 7)],
            body(),
        )),
        HirStmt::When(HirWhen::new(vec![HirCond::always()], vec![act(2)])),
        HirStmt::When(HirWhen::new(Vec::new(), vec![act(3)])),
    ]);
    module.vars = Vec::new();

    let optimized = optimize_module(&module).expect("valid HIR");
    let json = hir_to_json(&HirDocument::new(optimized)).expect("serialize optimized HIR");
    assert_eq!(
        json, EXPECTED_SATISFIABLE_JSON,
        "satisfiable fixtures must remain byte-identical"
    );
}

const EXPECTED_SATISFIABLE_JSON: &str = "{\"version\":\"3.2\",\"module\":{\"stmts\":[{\"kind\":\"trigger\",\"id\":0,\"conds\":[{\"locid\":0,\"player\":0,\"amount\":5,\"unit\":0,\"comparison\":0,\"condtype\":15,\"restype\":0,\"flags\":0,\"eudx\":0},{\"locid\":0,\"player\":0,\"amount\":7,\"unit\":0,\"comparison\":1,\"condtype\":15,\"restype\":0,\"flags\":0,\"eudx\":0}],\"acts\":[{\"mask\":0,\"strid\":0,\"wavid\":0,\"time\":0,\"dest\":0,\"value\":1,\"unit\":0,\"acttype\":45,\"modifier\":7,\"flags\":20,\"padding\":0,\"eudx\":0}],\"preserved\":true,\"flags\":4},{\"kind\":\"trigger\",\"id\":1,\"conds\":[{\"locid\":0,\"player\":0,\"amount\":0,\"unit\":0,\"comparison\":0,\"condtype\":22,\"restype\":0,\"flags\":0,\"eudx\":0}],\"acts\":[{\"mask\":0,\"strid\":0,\"wavid\":0,\"time\":0,\"dest\":0,\"value\":2,\"unit\":0,\"acttype\":45,\"modifier\":7,\"flags\":20,\"padding\":0,\"eudx\":0}],\"preserved\":true,\"flags\":4},{\"kind\":\"trigger\",\"id\":2,\"conds\":[],\"acts\":[{\"mask\":0,\"strid\":0,\"wavid\":0,\"time\":0,\"dest\":0,\"value\":3,\"unit\":0,\"acttype\":45,\"modifier\":7,\"flags\":20,\"padding\":0,\"eudx\":0}],\"preserved\":true,\"flags\":4}]}}";
