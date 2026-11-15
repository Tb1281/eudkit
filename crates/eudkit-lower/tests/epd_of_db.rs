//! `epd_of_db`: a mode-1 relocation into an action value field materializes
//! the payload blob's EPD, not the blob's first four bytes.
//!
//! The Python/TypeScript helper emits exactly one preserved, zero-condition
//! trigger whose action value field carries a mode-1 `HirReloc` targeting the
//! blob (ADR 0029). This test builds that HIR shape directly, lowers it, and
//! simulates one trigger loop against the native LIR simulator:
//!
//! * the `Var`'s value field is the action destination, so executing the
//!   action stores the relocated value into the variable;
//! * that value must equal `epd_u32(payload_addr)` for the emitted blob item.
//!
//! A mode-4 reloc would store the byte address instead, and a wrong target
//! item would store some other item's EPD, so the assertion pins the whole
//! mode-1 blob path rather than the emitted literal alone.

use eudkit_hir::{
    HirAct, HirBlob, HirDocument, HirModule, HirReloc, HirStmt, HirTrigger, HirVarDecl,
};
use eudkit_lir::{epd_u32, init_simulation, LirItem};
use eudkit_lower::lower_document;
use eudkit_opt::cell::{analyze_storage, StorageClass};
use eudkit_opt::optimize_module;

const VAR_ID: usize = 0;
const BLOB_ID: usize = 0;
const SET_TO: u8 = 7;
const DEST_FIELD_OFFSET: usize = 16;
const VALUE_FIELD_OFFSET: usize = 20;
const VAR_VALUE_SLOT_OFFSET: usize = 20;

/// The emitted shape of `epd_of_db(Db(16))`: one zero-init `Var`, one
/// preserved zero-condition trigger, one action whose value field relocates
/// to the blob payload EPD (mode 1) and whose destination is that `Var`'s
/// value slot.
fn epd_of_db_module() -> HirModule {
    let mut module = HirModule::new();
    // Pin the variable to a TriggerPage so the simulator observes the full
    // 32-bit EPD. Cell demotion is a storage-size decision for variables whose
    // value domain the optimizer can prove; this test is about the mode-1
    // relocation value, not that plan.
    module.vars = vec![HirVarDecl::pinned(VAR_ID, 0)];
    module.blobs = vec![HirBlob::zeroed(16)];

    let mut act = HirAct::set_deaths(0, SET_TO, 0, 0);
    act.relocs = vec![
        HirReloc::var(DEST_FIELD_OFFSET, VAR_ID, VAR_VALUE_SLOT_OFFSET, 1),
        HirReloc::blob(VALUE_FIELD_OFFSET, BLOB_ID, 0, 1),
    ];

    module.stmts = vec![HirStmt::Trigger(
        HirTrigger::new(0, Vec::new(), vec![act], true)
            .expect("one action is within the 16C/64A bounds"),
    )];
    module
}

#[test]
fn mode_1_blob_reloc_stores_the_payload_epd_in_the_var() {
    let module = epd_of_db_module();
    let trigger = match &module.stmts[0] {
        HirStmt::Trigger(trigger) => trigger,
        other => panic!("expected a trigger statement, got {other:?}"),
    };
    assert_eq!(trigger.conds.len(), 0, "the seed trigger has no conditions");
    assert_eq!(trigger.acts.len(), 1, "the seed trigger has one action");

    let doc = HirDocument::new(module);
    let lir = lower_document(&doc).expect("epd_of_db HIR must lower");

    // Mirror the storage plan lowering used, so the variable can be read back
    // from the simulated page/cell.
    let opt = optimize_module(&doc.module).expect("opt must accept the module");
    let plan = analyze_storage(&opt);
    let num_pages = plan.trigger_pages;
    // Item order: exec triggers, trigger pages, an optional demoted Cell blob,
    // then the HIR blobs.
    let num_exec = lir.module.items.len()
        - num_pages
        - usize::from(plan.cell_dwords > 0)
        - opt.blobs.len();

    let mut sim = init_simulation(&lir).expect("simulation must initialize");
    sim.step_loop();

    let actual = match plan
        .storage(VAR_ID)
        .expect("the declared variable must have a storage class")
    {
        StorageClass::TriggerPage {
            page_index,
            slot_index,
        } => sim.read_page_slot_value(num_exec + page_index, slot_index),
        StorageClass::DwordCell { dword_index } => {
            let addr = sim.payload.addrs[num_exec + num_pages] + (dword_index * 4) as u32;
            sim.read_epd(epd_u32(addr))
        }
        StorageClass::BitCell {
            dword_index,
            bit_index,
        } => {
            let addr = sim.payload.addrs[num_exec + num_pages] + (dword_index * 4) as u32;
            (sim.read_epd(epd_u32(addr)) >> bit_index) & 1
        }
    };

    let blob_item = lir
        .module
        .items
        .iter()
        .position(|item| matches!(item, LirItem::Blob(_)))
        .expect("the module blob must be emitted");
    let payload_addr = sim.payload.addrs[blob_item];

    assert_eq!(
        actual,
        epd_u32(payload_addr),
        "the var must hold (payload_addr - EPD_BASE) / 4, not the blob bytes or the byte address"
    );
    // The EPD is a dword index: encode it back to confirm it targets the blob
    // payload's first dword.
    assert_eq!(
        eudkit_lir::EPD_BASE.wrapping_add(actual.wrapping_mul(4)),
        payload_addr
    );
}
