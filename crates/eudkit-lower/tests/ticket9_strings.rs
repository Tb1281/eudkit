//! Ticket #9: module string resolution through HIR -> lowering -> map STRx.

use std::path::Path;

use eudkit_hir::{
    HirAct, HirCond, HirDocument, HirModule, HirReloc, HirStmt, HirStringInit, HirTrigger,
    HirVarDecl, hir_to_json,
};
use eudkit_lir::epd_u32;
use eudkit_lower::{
    LowerError, STR_TABLE_BASE, compile_hir_to_map, lower_document, lower_module_with_string_addrs,
    lower_module_with_template_strings,
};
use eudkit_map::mapinfo::extract_map_info;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn string_module() -> HirModule {
    let mut addr = HirVarDecl::pinned(0, 0);
    addr.init_string = Some(HirStringInit {
        index: 0,
        offset: 4,
    });

    // A display action using module string 1.
    let mut display = HirAct::set_deaths(0, 7, 0, 0);
    display.str_index = Some(1);
    display.relocs.push(HirReloc::string(16, 1, 0, 1));

    // A buffer write using module string 0 plus byte offset 4.
    let mut buffer = HirAct::set_deaths(0, 7, 0, 0);
    buffer.relocs.push(HirReloc::string(16, 0, 4, 1));

    let trig = HirTrigger::new(0, vec![HirCond::always()], vec![display, buffer], true).unwrap();
    HirModule {
        vars: vec![addr],
        stmts: vec![HirStmt::Trigger(trig)],
        strings: vec!["module-buffer".to_string(), "module-display".to_string()],
        files: Vec::new(),
        funcs: Vec::new(),
        blobs: Vec::new(),
    }
}

fn find_str_indexed_act(lir: &eudkit_lir::LirDocument, strid: i64) -> (usize, usize) {
    for (item_idx, item) in lir.module.items.iter().enumerate() {
        if let eudkit_lir::LirItem::Trigger(trig) = item {
            for (act_idx, act) in trig.acts.iter().enumerate() {
                if act.act.strid as i64 == strid {
                    return (item_idx, act_idx);
                }
            }
        }
    }
    panic!("no action with strid {strid}");
}

fn find_string_reloc_act(lir: &eudkit_lir::LirDocument, want: i64) -> (usize, usize) {
    for (item_idx, item) in lir.module.items.iter().enumerate() {
        if let eudkit_lir::LirItem::Trigger(trig) = item {
            for (act_idx, act) in trig.acts.iter().enumerate() {
                if act.act.dest as i64 == want {
                    return (item_idx, act_idx);
                }
            }
        }
    }
    panic!("no action with resolved string EPD {want:#x}");
}

fn page_slot_value(lir: &eudkit_lir::LirDocument) -> i64 {
    match lir.module.items.last().expect("lowered items") {
        eudkit_lir::LirItem::Trigger(trig) => trig.acts[0].act.value as i64,
        eudkit_lir::LirItem::Blob(_) => panic!("page must be the last item"),
    }
}

#[test]
fn lowering_uses_synthetic_string_addresses_without_a_basemap() {
    let module = string_module();
    let lir = lower_document(&HirDocument::new(module)).unwrap();

    let decoded = eudkit_map::decode_strings(&lir.module.strings).unwrap();
    assert_eq!(
        decoded,
        vec![b"module-buffer".to_vec(), b"module-display".to_vec()]
    );

    // Module string 1 (display) gets template_count 0 + index 1 + 1 = id 2.
    let (item_idx, act_idx) = find_str_indexed_act(&lir, 2);
    let eudkit_lir::LirItem::Trigger(trig) = &lir.module.items[item_idx] else {
        unreachable!()
    };
    assert_eq!(trig.acts[act_idx].act.acttype, 45);

    // Buffer string 0 at byte offset 4 resolves to the synthetic base + 4.
    let base_epd = epd_u32(0x2000_0000);
    find_string_reloc_act(&lir, base_epd as i64 + 1);

    // The page-pinned address variable carries the synthetic string EPD.
    assert_eq!(page_slot_value(&lir), base_epd as i64 + 1);
}

#[test]
fn lowering_folds_the_real_str_offset_table() {
    let module = string_module();
    let addrs = [0x40u32, 0x80u32];
    let lir = lower_module_with_string_addrs(&module, &[], &addrs).unwrap();

    let buffer_addr = STR_TABLE_BASE + addrs[0] + 4;
    assert_eq!(page_slot_value(&lir), epd_u32(buffer_addr) as i64);
    find_string_reloc_act(&lir, epd_u32(buffer_addr) as i64);
}

#[test]
fn lowering_enables_sub_dword_string_relocations() {
    // Issue #101: a string relocation is a 4-byte window at a byte
    // offset, not a dword-indexed parameter slot. Offset 19 is not
    // dword aligned but its window (19..23) fits the 32-byte action
    // record, so lowering patches those exact bytes.
    let mut act = HirAct::set_deaths(0, 7, 0, 0);
    act.relocs.push(HirReloc::string(19, 0, 4, 1));
    let module = HirModule {
        vars: Vec::new(),
        stmts: vec![HirStmt::Act(act)],
        strings: vec!["buffer".to_string()],
        files: Vec::new(),
        blobs: Vec::new(),
        funcs: Vec::new(),
    };
    let lir = lower_document(&HirDocument::new(module)).unwrap();
    let trig = lir
        .module
        .items
        .iter()
        .find_map(|item| match item {
            eudkit_lir::LirItem::Trigger(t) if !t.acts.is_empty() => Some(t),
            _ => None,
        })
        .expect("lowered trigger");
    let bytes = trig.acts[0].act.encode();
    // Synthetic module string 0 at byte offset 4, mode 1 (EPD).
    let want = epd_u32(0x2000_0000 + 4);
    assert_eq!(&bytes[19..23], &want.to_le_bytes());
    // The unaligned window straddles the dest/value boundary, so the
    // typed record no longer matches a plain SetDeaths; that is the
    // point — it was patched at the requested bytes, nowhere else.
    assert_eq!(trig.acts[0].relocs.len(), 0);
}

#[test]
fn lowering_rejects_a_string_window_past_the_record() {
    // 29 + 4 > 32: the loud error instead of silently landing on a
    // different dword.
    let mut act = HirAct::set_deaths(0, 7, 0, 0);
    act.relocs.push(HirReloc::string(29, 0, 4, 1));
    let module = HirModule {
        vars: Vec::new(),
        stmts: vec![HirStmt::Act(act)],
        strings: vec!["buffer".to_string()],
        files: Vec::new(),
        blobs: Vec::new(),
        funcs: Vec::new(),
    };
    let err = lower_document(&HirDocument::new(module)).unwrap_err();
    assert!(
        matches!(
            err,
            LowerError::Hir(eudkit_hir::HirError::InvalidStringReloc(29, 32))
        ),
        "unexpected error: {err}"
    );
}

#[test]
fn lowering_refuses_a_missing_string_offset() {
    let module = string_module();
    let err = lower_module_with_string_addrs(&module, &[], &[0x40]).unwrap_err();
    assert!(
        matches!(err, LowerError::MissingStringOffset(1)),
        "unexpected error: {err}"
    );
}

#[test]
fn compile_resolves_module_strings_against_the_basemap_strx() {
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let info = extract_map_info(&basemap).unwrap();
    let template_count = info.strings_b64.len();
    assert!(
        template_count > 100,
        "basemap must have a real string table"
    );

    let module = string_module();
    let probe = lower_module_with_template_strings(&module, &info.strings_b64).unwrap();
    let addrs =
        eudkit_map::tbl::str_offsets_for_full_strings(&basemap, &probe.module.strings).unwrap();
    let expected_buffer = STR_TABLE_BASE + addrs[template_count] + 4;
    let expected_display = STR_TABLE_BASE + addrs[template_count + 1];

    let hir_json = hir_to_json(&HirDocument::new(module.clone())).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("strings.scx");
    compile_hir_to_map(&hir_json, &basemap, &out).unwrap();

    let lir = lower_module_with_string_addrs(&module, &info.strings_b64, &addrs).unwrap();
    find_string_reloc_act(&lir, epd_u32(expected_buffer) as i64);
    find_string_reloc_act(&lir, epd_u32(expected_display) as i64);
    find_str_indexed_act(&lir, (template_count + 2) as i64);

    let mut archive = stormlib::Archive::open(&out, stormlib::OpenArchiveFlags::empty()).unwrap();
    let chk = archive
        .open_file("staredit\\scenario.chk")
        .unwrap()
        .read_all()
        .unwrap();
    drop(archive);
    let strx = chk_section(&chk, b"STRx").expect("output STRx");
    let ids = parse_strx_ids(strx);
    assert_eq!(ids.len(), template_count + 2);
    assert_eq!(ids[template_count], b"module-buffer".to_vec());
    assert_eq!(ids[template_count + 1], b"module-display".to_vec());
}

fn chk_section<'a>(chk: &'a [u8], want: &[u8; 4]) -> Option<&'a [u8]> {
    let mut off = 0;
    while off + 8 <= chk.len() {
        let name = &chk[off..off + 4];
        let ln = i32::from_le_bytes(chk[off + 4..off + 8].try_into().unwrap());
        if ln < 0 || off + 8 + (ln as usize) > chk.len() {
            return None;
        }
        if name == want {
            return Some(&chk[off + 8..off + 8 + (ln as usize)]);
        }
        off += 8 + (ln as usize);
    }
    None
}

fn parse_strx_ids(section: &[u8]) -> Vec<Vec<u8>> {
    let n = u32::from_le_bytes(section[0..4].try_into().unwrap()) as usize;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let o = 4 + 4 * i;
        let off = u32::from_le_bytes(section[o..o + 4].try_into().unwrap()) as usize;
        let end = section[off..]
            .iter()
            .position(|&b| b == 0)
            .unwrap_or_else(|| panic!("STRx id {}: unterminated string", i + 1));
        out.push(section[off..off + end].to_vec());
    }
    out
}
