//! 16-bit wire string-id relocations (`mode == 6`).
//!
//! A table field such as `units.dat` `nameString` stores the 1-based STRx id
//! (`template_count + module_index + 1`), not an address. Only the lowerer
//! knows `template_count`, so the frontend records the 0-based module string
//! index and the lane bit shift; lowering resolves the id and writes it into
//! the field window.

use eudkit_hir::{HirAct, HirCond, HirDocument, HirModule, HirReloc, HirStmt, HirTrigger};
use eudkit_lir::{epd_u32, LirDocument, LirItem};
use eudkit_lower::{lower_document, lower_module_with_template_strings, LowerError};

/// `units.dat[n].nameString` EPD for entry 0.
const NAME_STRING_EPD: u32 = 219_071;
/// HIR action value-field byte offset.
const VALUE_FIELD_OFFSET: usize = 20;
/// HIR condition amount-field byte offset.
const AMOUNT_FIELD_OFFSET: usize = 8;

fn templates() -> Vec<String> {
    vec![
        eudkit_lir::b64_encode(b"template-0"),
        eudkit_lir::b64_encode(b"template-1"),
    ]
}

fn module_with_stmt(stmt: HirStmt) -> HirModule {
    HirModule {
        vars: Vec::new(),
        stmts: vec![stmt],
        strings: vec!["module-name".to_string()],
        files: Vec::new(),
        funcs: Vec::new(),
        blobs: Vec::new(),
    }
}

fn single_trigger(lir: &LirDocument) -> &eudkit_lir::TriggerItem {
    lir.module
        .items
        .iter()
        .find_map(|item| match item {
            LirItem::Trigger(trig) => Some(trig),
            LirItem::Blob(_) => None,
        })
        .expect("a lowered trigger")
}

#[test]
fn mode6_act_resolves_template_count_plus_index_plus_one() {
    // Two seeded template strings, then the module's one string at index 0:
    // id = 2 + 0 + 1 = 3. The lane shift is a bit shift, not a byte offset.
    for (shift, mask, want) in [
        (0usize, 0x0000_FFFFu32, 3u32),
        (16usize, 0xFFFF_0000u32, 3u32 << 16),
    ] {
        let mut act = HirAct::set_deathsx(NAME_STRING_EPD, 7, 0, 0, mask);
        act.relocs
            .push(HirReloc::string(VALUE_FIELD_OFFSET, 0, shift, 6));

        let lir =
            lower_module_with_template_strings(&module_with_stmt(HirStmt::Act(act)), &templates())
                .unwrap();
        let trig = single_trigger(&lir);

        assert_eq!(trig.acts[0].act.value, want);
        assert_eq!(trig.acts[0].act.mask, mask);
        // The id is a value, not a payload relocation.
        assert!(trig.acts[0].relocs.is_empty());
    }
}

#[test]
fn modes_1_and_4_keep_their_existing_address_forms() {
    let mut epd_act = HirAct::set_deaths(0, 7, 0, 0);
    epd_act
        .relocs
        .push(HirReloc::string(VALUE_FIELD_OFFSET, 0, 4, 1));
    let lir =
        lower_module_with_template_strings(&module_with_stmt(HirStmt::Act(epd_act)), &templates())
            .unwrap();
    assert_eq!(
        single_trigger(&lir).acts[0].act.value,
        epd_u32(0x2000_0000 + 4)
    );

    let mut addr_act = HirAct::set_deaths(0, 7, 0, 0);
    addr_act
        .relocs
        .push(HirReloc::string(VALUE_FIELD_OFFSET, 0, 4, 4));
    let lir =
        lower_module_with_template_strings(&module_with_stmt(HirStmt::Act(addr_act)), &templates())
            .unwrap();
    assert_eq!(single_trigger(&lir).acts[0].act.value, 0x2000_0000 + 4);
}

#[test]
fn mode6_condition_resolves_the_same_way() {
    let mut cond = HirCond::deathsx(NAME_STRING_EPD, 10, 0, 0, 0xFFFF_0000);
    cond.relocs
        .push(HirReloc::string(AMOUNT_FIELD_OFFSET, 0, 16, 6));
    let trig = HirTrigger::new(0, vec![cond], vec![HirAct::set_deaths(0, 7, 0, 0)], true).unwrap();

    let lir =
        lower_module_with_template_strings(&module_with_stmt(HirStmt::Trigger(trig)), &templates())
            .unwrap();
    let trig = single_trigger(&lir);

    assert_eq!(trig.conds[0].cond.amount, 3 << 16);
}

#[test]
fn mode6_overflow_is_a_loud_bounds_error() {
    let mut act = HirAct::set_deaths(NAME_STRING_EPD, 7, 0, 0);
    act.relocs
        .push(HirReloc::string(VALUE_FIELD_OFFSET, 0, 0, 6));

    // Seeders occupy 0xFFFF ids, so the module's first string is id 0x10000.
    let templates: Vec<String> = (0..0xFFFF)
        .map(|i| eudkit_lir::b64_encode(format!("t{i}").as_bytes()))
        .collect();
    let err = lower_module_with_template_strings(&module_with_stmt(HirStmt::Act(act)), &templates)
        .unwrap_err();

    assert!(
        matches!(err, LowerError::StringIdOutOfRange(0)),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().starts_with("EUDSL-BOUNDS:"),
        "unexpected message: {err}"
    );
}

#[test]
fn mode6_rejects_a_shift_past_the_id_width() {
    let mut act = HirAct::set_deaths(NAME_STRING_EPD, 7, 0, 0);
    act.relocs
        .push(HirReloc::string(VALUE_FIELD_OFFSET, 0, 24, 6));

    let err = lower_document(&HirDocument::new(module_with_stmt(HirStmt::Act(act)))).unwrap_err();

    assert!(
        matches!(err, LowerError::StringIdOutOfRange(0)),
        "unexpected error: {err}"
    );
}
