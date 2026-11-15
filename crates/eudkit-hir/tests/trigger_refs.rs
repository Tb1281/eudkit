//! `TriggerRefs` pins every place a module names a trigger explicitly.
//!
//! The optimizer, call inliner, and lowering all refuse to fold, merge, or
//! renumber a trigger that one of these forms names, so a reference form that
//! one caller forgot would desynchronize their decisions. These tests cover
//! each form plus each control body, and pin the synthetic-id seed
//! (`next_id`) to the maximum *declared* trigger id: reference-only ids do not
//! advance it.

use eudkit_hir::{
    HirAct, HirAfter, HirBlob, HirBlobReloc, HirCall, HirCond, HirEdge, HirEdgeDirection, HirEvery,
    HirFunc, HirModule, HirOnce, HirReloc, HirStmt, HirTrigger, HirTriggerPtr, HirUntil,
    HirVarBatch, HirVarStore, HirVarTransfer, HirWhen, HirWhile, TriggerRefs,
};

/// An empty trigger carrying `id`; only its id matters to the walker.
fn trigger(id: usize) -> HirStmt {
    HirStmt::Trigger(
        HirTrigger::new(id, Vec::new(), Vec::new(), true)
            .expect("empty trigger is within the 16C/64A bounds"),
    )
}

/// An `Always` condition whose value field points at `trigger_id`.
fn cond_ref(trigger_id: usize) -> HirCond {
    let mut cond = HirCond::always();
    cond.relocs = vec![HirReloc::trigger(4, trigger_id, 0, 1)];
    cond
}

/// A plain action whose value field points at `trigger_id`.
fn act_ref(trigger_id: usize) -> HirAct {
    let mut act = HirAct::set_deaths(0, 7, 1, 0);
    act.relocs = vec![HirReloc::trigger(4, trigger_id, 0, 1)];
    act
}

#[test]
fn chain_pointers_are_references_but_the_trigger_itself_is_not() {
    let mut chain = HirTrigger::new(1, Vec::new(), Vec::new(), true).unwrap();
    chain.prev = Some(HirTriggerPtr::Trigger { id: 2, offset: 0 });
    chain.next = Some(HirTriggerPtr::Trigger { id: 3, offset: 4 });

    let refs = TriggerRefs::from_stmts(&[HirStmt::Trigger(chain)]);

    assert!(refs.contains(2), "prev pointer target is a reference");
    assert!(refs.contains(3), "next pointer target is a reference");
    assert!(
        !refs.contains(1),
        "declaring a trigger does not reference its own id"
    );
    assert_eq!(
        refs.next_id(),
        2,
        "a trigger's own id sets the max, not the ids its pointers name"
    );
}

#[test]
fn constant_chain_pointers_are_not_references() {
    let mut trig = HirTrigger::new(4, Vec::new(), Vec::new(), true).unwrap();
    trig.prev = Some(HirTriggerPtr::Const { value: -1 });
    trig.next = Some(HirTriggerPtr::Const { value: 0 });

    let refs = TriggerRefs::from_stmts(&[HirStmt::Trigger(trig)]);

    assert!(
        !refs.contains(0),
        "a literal chain value is not a trigger id"
    );
    assert_eq!(refs.next_id(), 5);
}

#[test]
fn trigger_condition_and_action_relocations_are_references() {
    let trig = HirTrigger::new(5, vec![cond_ref(21)], vec![act_ref(22)], true).unwrap();

    let refs = TriggerRefs::from_stmts(&[HirStmt::Trigger(trig)]);

    assert!(refs.contains(21), "condition reloc target is a reference");
    assert!(refs.contains(22), "action reloc target is a reference");
    assert!(!refs.contains(5));
    assert_eq!(refs.next_id(), 6);
}

#[test]
fn standalone_action_relocations_are_references() {
    let refs = TriggerRefs::from_stmts(&[HirStmt::Act(act_ref(23))]);

    assert!(refs.contains(23));
    assert_eq!(refs.next_id(), 0, "a bare act declares no trigger");
}

#[test]
fn var_store_and_batched_store_destinations_are_references() {
    let stmts = vec![
        HirStmt::VarStore(HirVarStore::to_trigger(0, 30, 4, 7)),
        HirStmt::VarBatch(HirVarBatch {
            transfers: Vec::new(),
            stores: vec![HirVarStore::to_trigger(1, 31, 8, 7)],
        }),
        // A fixed-address store and a transfer name no trigger.
        HirStmt::VarStore(HirVarStore::to_addr(0, 0x006D_0F38, 7)),
        HirStmt::VarTransfer(HirVarTransfer {
            dst_var: 0,
            src_var: 1,
            modifier: 7,
        }),
    ];

    let refs = TriggerRefs::from_stmts(&stmts);

    assert!(refs.contains(30), "VarStore.dst_trigger is a reference");
    assert!(
        refs.contains(31),
        "VarBatch member dst_trigger is a reference"
    );
    assert_eq!(refs.next_id(), 0, "stores declare no triggers");
}

/// A trigger declaring `id` whose condition names `reference`. The reference
/// is visible only if the walker descends into the enclosing control body.
fn body_trigger(id: usize, reference: usize) -> HirStmt {
    HirStmt::Trigger(HirTrigger::new(id, vec![cond_ref(reference)], Vec::new(), true).unwrap())
}

#[test]
fn every_control_body_and_condition_is_walked() {
    let stmts = vec![
        HirStmt::When(HirWhen::with_else(
            vec![cond_ref(101)],
            vec![body_trigger(102, 902)],
            vec![body_trigger(103, 903)],
        )),
        HirStmt::Once(HirOnce::new(
            vec![cond_ref(104)],
            vec![body_trigger(105, 905)],
        )),
        HirStmt::While(HirWhile::new(
            vec![cond_ref(106)],
            vec![body_trigger(107, 907)],
        )),
        HirStmt::Until(HirUntil::new(
            vec![cond_ref(108)],
            vec![body_trigger(109, 909)],
            0,
        )),
        HirStmt::Edge(HirEdge::new(
            HirEdgeDirection::Rising,
            vec![cond_ref(110)],
            vec![body_trigger(111, 911)],
            0,
            1,
        )),
        HirStmt::Every(HirEvery::new(2, 0, vec![body_trigger(112, 912)])),
        HirStmt::After(HirAfter::new(2, 0, vec![body_trigger(113, 913)])),
    ];

    let refs = TriggerRefs::from_stmts(&stmts);

    for id in [101, 104, 106, 108, 110] {
        assert!(
            refs.contains(id),
            "control condition target {id} is a reference"
        );
    }
    for id in [902, 903, 905, 907, 909, 911, 912, 913] {
        assert!(
            refs.contains(id),
            "reference {id} inside a control body must be found"
        );
    }
    assert_eq!(
        refs.next_id(),
        114,
        "max id comes from the highest nested trigger declaration"
    );
}

#[test]
fn function_bodies_are_walked() {
    let mut func_trigger = HirTrigger::new(200, vec![cond_ref(202)], Vec::new(), true).unwrap();
    func_trigger.prev = Some(HirTriggerPtr::Trigger { id: 201, offset: 0 });
    let module = HirModule {
        funcs: vec![HirFunc::new(
            9,
            Vec::new(),
            None,
            vec![HirStmt::Trigger(func_trigger)],
        )],
        ..HirModule::new()
    };

    let refs = TriggerRefs::from_module(&module);

    assert!(
        refs.contains(201),
        "a function-body chain pointer is a reference"
    );
    assert!(
        refs.contains(202),
        "a function-body cond reloc is a reference"
    );
    assert_eq!(
        refs.next_id(),
        201,
        "function-body triggers advance the synthetic-id seed"
    );
}

#[test]
fn blob_trigger_relocations_are_references_without_advancing_next_id() {
    let module = HirModule {
        stmts: vec![trigger(5)],
        blobs: vec![
            HirBlob::zeroed(8).with_relocs(vec![HirBlobReloc::trigger(0, 5000, 0, 4)]),
            // A blob-to-blob relocation names no trigger.
            HirBlob::zeroed(8).with_relocs(vec![HirBlobReloc::blob(4, 0, 0, 4)]),
        ],
        ..HirModule::new()
    };

    let refs = TriggerRefs::from_module(&module);

    assert!(
        refs.contains(5000),
        "a trigger named only by a blob relocation is still referenced"
    );
    assert_eq!(
        refs.next_id(),
        6,
        "blob relocations are references, not declarations: they do not advance the seed"
    );
}

#[test]
fn next_id_ignores_non_trigger_statements_and_nested_max_wins() {
    let mut reloc_only_act = act_ref(9999);
    reloc_only_act.value = 1;
    let module = HirModule {
        stmts: vec![
            HirStmt::Act(reloc_only_act),
            HirStmt::VarStore(HirVarStore::to_trigger(0, 9998, 4, 7)),
            HirStmt::VarTransfer(HirVarTransfer {
                dst_var: 0,
                src_var: 1,
                modifier: 7,
            }),
            HirStmt::Call(HirCall::new(7, Vec::new(), None)),
            HirStmt::When(HirWhen::new(
                vec![HirCond::always()],
                vec![HirStmt::Once(HirOnce::new(
                    vec![HirCond::always()],
                    vec![trigger(77)],
                ))],
            )),
        ],
        funcs: vec![HirFunc::new(7, Vec::new(), None, vec![trigger(88)])],
        ..HirModule::new()
    };

    let refs = TriggerRefs::from_module(&module);

    assert!(refs.contains(9999));
    assert!(refs.contains(9998));
    assert_eq!(
        refs.next_id(),
        89,
        "the highest declared trigger, nested or in a function, wins"
    );
}

#[test]
fn empty_lists_have_no_references_and_seed_zero() {
    let module_refs = TriggerRefs::from_module(&HirModule::new());
    let list_refs = TriggerRefs::from_stmts(&[]);

    assert!(!module_refs.contains(0));
    assert!(!list_refs.contains(0));
    assert_eq!(module_refs.next_id(), 0);
    assert_eq!(list_refs.next_id(), 0);
}

#[test]
fn from_stmts_sees_only_the_given_list() {
    let refs = TriggerRefs::from_stmts(&[HirStmt::Act(act_ref(42))]);

    assert!(refs.contains(42));
    assert_eq!(refs.next_id(), 0);
}
