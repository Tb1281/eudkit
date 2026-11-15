//! Regression for issue #41: a basemap carrying compiled inline-code
//! triggers must load and assemble. The pure path cannot `exec` the
//! embedded source, so the whole template TRIG is preserved verbatim
//! instead of failing with `InlineCodeMagic`.

use std::path::Path;

use eudkit_map::{AssemblyOptions, TailInputs, assemble_map, chk, mpq, rng};

fn basemap() -> &'static Path {
    Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/outputmap/basemap/basemap.scx"
    ))
}

/// A valid compiled inline-code trigger: magic at byte 20, nonzero player
/// code at 24, and all of `GetInlineCodePlayerList`'s structural bytes
/// zero (effplayers, cond[0].condtype at 15, act[0].acttype at 346).
fn magic_trigger() -> Vec<u8> {
    let mut trigger = vec![0u8; 2400];
    trigger[20..24].copy_from_slice(&0x1097_8D4A_u32.to_le_bytes());
    trigger[24..28].copy_from_slice(&1u32.to_le_bytes());
    trigger
}

fn lir_fixture() -> String {
    r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#.to_string()
}

#[test]
fn assemble_map_accepts_a_basemap_with_compiled_inline_code() {
    let dir = tempfile::tempdir().unwrap();
    let inline_basemap = dir.path().join("inline-basemap.scx");

    // Build a basemap whose TRIG carries the compiled inline-code trigger,
    // reusing the production MPQ writer so the fixture is a valid map.
    let mut archive =
        stormlib::Archive::open(basemap(), stormlib::OpenArchiveFlags::empty()).unwrap();
    let raw = archive
        .open_file("staredit\\scenario.chk")
        .unwrap()
        .read_all()
        .unwrap();
    drop(archive);

    let mut chkt = chk::Chk::new();
    assert!(chkt.loadchk(&raw), "fixture CHK must load");
    let mut trig = chkt.getsection(b"TRIG").unwrap_or_default().to_vec();
    let magic = magic_trigger();
    trig.extend_from_slice(&magic);
    chkt.setsection(*b"TRIG", trig);
    let mut save_rng = rng::CpRandom::seed_zero();
    let modified_chk = chkt.savechk(&mut save_rng, false);
    mpq::write_output_map(&inline_basemap, basemap(), &modified_chk, None, &[]).unwrap();

    let out = dir.path().join("out.scx");
    let chk_out = dir.path().join("out.chk");
    let lir: eudkit_lir::LirDocument = serde_json::from_str(&lir_fixture()).unwrap();
    let summary = assemble_map(TailInputs {
        lir: &lir,
        basemap: &inline_basemap,
        out_path: &out,
        sector_size: None,
        mrgndata: None,
        draw_log_json: None,
        queued_files: vec![],
        debug_emit_chk: Some(&chk_out),
        no_fake_isom: false,
        assembly: AssemblyOptions::default(),
    })
    .expect("a basemap with compiled inline-code triggers must assemble");

    assert!(
        !summary.template_inlined,
        "an opaque inline-code template must take the keep path"
    );
    assert!(out.is_file(), "assembly must write the output map");

    // The magic payload survives: `initialize_payload` toggles only the
    // disabled flag, never the trigger's inline-code marker bytes.
    let produced = std::fs::read(&chk_out).unwrap();
    let mut produced_chkt = chk::Chk::new();
    assert!(produced_chkt.loadchk(&produced));
    let produced_trig = produced_chkt.getsection(b"TRIG").unwrap();
    assert!(
        produced_trig.chunks_exact(2400).any(|chunk| {
            u32::from_le_bytes(chunk[20..24].try_into().unwrap()) == 0x1097_8D4A
                && u32::from_le_bytes(chunk[24..28].try_into().unwrap()) == 1
        }),
        "the compiled inline-code trigger must be preserved in the output TRIG"
    );
}
