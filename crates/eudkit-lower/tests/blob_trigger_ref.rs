//! R1 regression: a trigger referenced only by a blob relocation must stay a
//! standalone `ExecTrigger` instead of being folded into the surrounding
//! variable-batch basic block.
//!
//! The unpack predicate in `lower_hir_stmts` folds any preserved,
//! unconditional, flag-`4` trigger whose id `TriggerRefs::from_module` does not
//! report. That walker must therefore see `module.blobs[].relocs`, or the
//! folded trigger no longer exists as an exec item and the blob relocation
//! fails to resolve.
//!
//! Sanity check (recorded here, not encoded as `#[should_panic]`): removing the
//! blob scan from `TriggerRefs::from_module` makes this test fail at
//! `lower_document` with `LowerError::UnknownTriggerId(TRIGGER_ID)`, because
//! the trigger was folded into the basic block and has no exec item for the
//! blob relocation to target.

use eudkit_hir::{
    HirAct, HirBlob, HirBlobReloc, HirDocument, HirModule, HirStmt, HirTrigger, HirVarDecl,
    HirVarTransfer,
};
use eudkit_lir::LirItem;
use eudkit_lower::lower_document;

/// Fixed id for the trigger the blob points at.
const TRIGGER_ID: usize = 0xB10B;

/// Distinctive action payload; identifies the retained `ExecTrigger` in LIR,
/// which does not serialize the internal `hir_id`.
const SENTINEL_VALUE: u32 = 0xB10B_0001;

fn blob_referenced_trigger_module() -> HirModule {
    let mut module = HirModule::new();
    // Pinned variables so the transfer is a page-firing `VarBatch`: that same
    // statement list then passes `has_var_batch` to the unpack predicate.
    module.vars = vec![HirVarDecl::pinned(0, 0), HirVarDecl::pinned(1, 1)];
    module.stmts = vec![
        HirStmt::VarTransfer(HirVarTransfer {
            dst_var: 0,
            src_var: 1,
            modifier: 7,
        }),
        HirStmt::Trigger(
            HirTrigger::new(
                TRIGGER_ID,
                Vec::new(),
                vec![HirAct::set_deaths(0, 7, SENTINEL_VALUE, 0)],
                true,
            )
            .expect("trigger is within the 16C/64A bounds"),
        ),
    ];
    // The only reference to TRIGGER_ID is this blob relocation.
    module.blobs =
        vec![HirBlob::zeroed(4).with_relocs(vec![HirBlobReloc::trigger(0, TRIGGER_ID, 0, 4)])];
    module
}

#[test]
fn blob_referenced_trigger_keeps_its_exec_item() {
    let lir = lower_document(&HirDocument::new(blob_referenced_trigger_module()))
        .expect("lowering must succeed once blob relocations are scanned");

    let target_item = lir
        .module
        .items
        .iter()
        .find_map(|item| match item {
            LirItem::Blob(blob) => blob.relocs.first().map(|reloc| reloc.target_item),
            LirItem::Trigger(_) => None,
        })
        .expect("the emitted blob must carry its trigger relocation");

    let retained = match &lir.module.items[target_item] {
        LirItem::Trigger(trig) => trig,
        LirItem::Blob(blob) => panic!(
            "blob reloc targets blob item {target_item} (size {}), not an ExecTrigger",
            blob.size
        ),
    };

    // The retained `ExecTrigger` was created from `HirTrigger { id: TRIGGER_ID }`
    // (its `hir_id` is not serialized into LIR), so identify it by the payload
    // that only that trigger carries.
    assert!(retained.preserved);
    assert_eq!(retained.flag, None, "flags 4 is the preserved default");
    assert!(retained.conds.is_empty());
    assert_eq!(
        retained.acts.len(),
        1,
        "the trigger's action was folded into the basic block instead of being retained"
    );
    assert_eq!(
        retained.acts[0].act.value, SENTINEL_VALUE,
        "blob reloc did not resolve to the TRIGGER_ID trigger"
    );

    // Sanity: the sentinel exists in exactly one exec trigger, so the item at
    // `target_item` really is the retained trigger rather than a page or batch.
    let sentinel_items: Vec<usize> = lir
        .module
        .items
        .iter()
        .enumerate()
        .filter_map(|(idx, item)| match item {
            LirItem::Trigger(trig)
                if trig.acts.iter().any(|act| act.act.value == SENTINEL_VALUE) =>
            {
                Some(idx)
            }
            _ => None,
        })
        .collect();
    assert_eq!(sentinel_items, vec![target_item]);
}
