//! Regression for issue #40: authored basemap classic triggers keep their
//! enabled/disabled flags in the compiled map.
//!
//! Stage 1 toggles bit 8 on every old trigger. eudplib's stage 3 flips it
//! back at runtime, but eudkit has no stage 3. Without dispatch wiring in the
//! payload, nothing restores the bit, so the tail must preserve the author's
//! flag dwords instead of toggling them.
//!
//! The tests go through the public `assemble_map` seam with a basemap built
//! from the committed fixture, then read the emitted CHK through
//! `TailInputs::debug_emit_chk` (same harness as `mrgn_no_obfuscation.rs`).

use eudkit_map::payload_init::STR_SECTION_BASE;
use eudkit_map::tick_gate::append_tick_gate;
use eudkit_map::{
    AssemblyOptions, TailInputs, assemble_map, chk, mpq, rng,
};
use std::path::{Path, PathBuf};

fn basemap() -> &'static Path {
    Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/outputmap/basemap/basemap.scx"
    ))
}

fn lir_fixture() -> String {
    r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#.to_string()
}

/// A structurally normal classic trigger: `Always` + `SetDeaths(0, SetTo, 1,
/// 0)`, player 0, with the given flag dword.
fn authored_trigger(flag: u32) -> Vec<u8> {
    let mut t = vec![0u8; 2400];
    t[15] = 22; // cond[0].condtype = Always
    t[320 + 16..320 + 20].copy_from_slice(&0u32.to_le_bytes()); // player
    t[320 + 20..320 + 24].copy_from_slice(&1u32.to_le_bytes()); // amount
    t[320 + 26] = 45; // acttype = SetDeaths
    t[320 + 27] = 7; // modifier = SetTo
    t[320 + 28] = 20; // action flags
    t[2368..2372].copy_from_slice(&flag.to_le_bytes());
    t[2372] = 1; // player 0 executes
    t
}

/// Copy the fixture basemap, replace its TRIG with `triggers`, and write a
/// valid `.scx` with the production MPQ writer. Returns the scratch dir (to
/// keep the file alive) and the basemap path.
fn basemap_with_trig(triggers: &[Vec<u8>]) -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("classic-basemap.scx");

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
    let trig = triggers.concat();
    chkt.setsection(*b"TRIG", trig);
    let mut save_rng = rng::CpRandom::seed_zero();
    let modified_chk = chkt.savechk(&mut save_rng, false);
    mpq::write_output_map(&path, basemap(), &modified_chk, None, &[]).unwrap();
    (dir, path)
}

fn assemble(basemap_path: &Path, dir: &Path) -> PathBuf {
    let out = dir.join("out.scx");
    let chk_out = dir.join("out.chk");
    let lir: eudkit_lir::LirDocument = serde_json::from_str(&lir_fixture()).unwrap();
    let summary = assemble_map(TailInputs {
        lir: &lir,
        basemap: basemap_path,
        out_path: &out,
        sector_size: None,
        mrgndata: None,
        draw_log_json: None,
        queued_files: vec![],
        debug_emit_chk: Some(&chk_out),
        no_fake_isom: false,
        assembly: AssemblyOptions::default(),
    })
    .expect("assemble_map must succeed for the fixture basemap");
    assert!(!summary.template_inlined, "no wiring means the keep path");
    assert!(out.is_file(), "assembly must write the output map");
    chk_out
}

/// Extract one section from a raw CHK byte blob.
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

fn output_section(chk_path: &Path, want: &[u8; 4]) -> Vec<u8> {
    let chk = std::fs::read(chk_path).expect("debug_emit_chk must write the CHK");
    chk_section(&chk, want)
        .unwrap_or_else(|| {
            panic!(
                "output CHK must carry a {} section",
                String::from_utf8_lossy(want)
            )
        })
        .to_vec()
}

fn flag(record: &[u8]) -> u32 {
    u32::from_le_bytes(record[2368..2372].try_into().unwrap())
}

/// The runner pair's shared encoding (ADR 0010): no conditions, one
/// `SetMemory(0x6509B0, SetTo, 0)` action, enabled + preserved, AllPlayers.
fn assert_runner_trigger(record: &[u8]) {
    assert_eq!(record.len(), 2400);
    assert_eq!(&record[0..320], &[0u8; 320]);
    let a = &record[320..352];
    assert_eq!(
        u32::from_le_bytes(a[16..20].try_into().unwrap()),
        203155,
        "action player = EPD(CURPL)"
    );
    assert_eq!(u32::from_le_bytes(a[20..24].try_into().unwrap()), 0);
    assert_eq!(a[26], 45, "acttype: SetDeaths");
    assert_eq!(a[27], 7, "modifier: SetTo");
    assert_eq!(a[28], 20);
    assert_eq!(&record[352..2368], &[0u8; 2368 - 352]);
    assert_eq!(flag(record), 0x04, "enabled + preserved");
    assert_eq!(&record[2372..2389], &[0u8; 17]);
    assert_eq!(record[2389], 1, "peff[17] = AllPlayers");
    assert_eq!(&record[2390..2400], &[0u8; 10]);
}

fn blank_trigger_fixture() -> Vec<u8> {
    std::fs::read(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/blank_trigger_py.bin"
    ))
    .unwrap()
}

#[test]
fn authored_classic_triggers_keep_their_flags() {
    // One enabled (0x00) and one authored-disabled (0x08) classic trigger.
    let enabled = authored_trigger(0x00);
    let disabled = authored_trigger(0x08);
    let (dir, basemap_path) = basemap_with_trig(&[enabled.clone(), disabled.clone()]);
    let chk_out = assemble(&basemap_path, dir.path());

    let trig = output_section(&chk_out, b"TRIG");
    // 70 stage-1 triggers + runner begin + the two authored triggers +
    // runner end + the synthetic blank terminator.
    let base = 71;
    assert_eq!(trig.len(), (base + 4) * 2400);
    assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
    assert_eq!(
        &trig[base * 2400..(base + 1) * 2400],
        enabled.as_slice(),
        "the authored enabled trigger must survive byte-for-byte"
    );
    assert_eq!(
        &trig[(base + 1) * 2400..(base + 2) * 2400],
        disabled.as_slice(),
        "the authored disabled trigger must survive byte-for-byte"
    );
    assert_runner_trigger(&trig[(base + 2) * 2400..(base + 3) * 2400]);
    let last = &trig[(base + 3) * 2400..(base + 4) * 2400];
    assert_eq!(flag(last), 0x0C, "the synthetic blank still toggles");

    // Tick-gate interaction: the stage-1 chain target must still be
    // payload_offset + gate_entry_bytes, unaffected by the flag policy.
    let lir = lir_fixture();
    let mut doc = eudkit_lir::LirDocument::from_json(&lir).unwrap();
    let gate = append_tick_gate(&mut doc);
    assert!(
        gate.gate_byte_offset > 0,
        "the gate must sit after the program"
    );
    let payload = eudkit_lir::lir2payload(&doc).unwrap();
    let strx = output_section(&chk_out, b"STRx");
    let payload_offset = STR_SECTION_BASE + (strx.len() - payload.data.len()) as i64;
    // Stage-1 trigger 70 (0-based 69) writes the chain target into
    // CURPL-relative action 0.
    let stage1_last = &trig[69 * 2400..70 * 2400];
    let target = u32::from_le_bytes(stage1_last[320 + 20..320 + 24].try_into().unwrap());
    assert_eq!(
        target as i64,
        payload_offset + gate.gate_byte_offset as i64,
        "stage 1 must still enter the once-per-tick gate"
    );

    let _ = std::fs::remove_dir_all(dir.path());
}

#[test]
fn blank_basemap_output_has_runner_pair_and_blank() {
    // No authored triggers: the output is the bootstrap records, the runner
    // begin/end pair, and the synthetic blank terminator.
    let (dir, basemap_path) = basemap_with_trig(&[]);
    let chk_out = assemble(&basemap_path, dir.path());

    let trig = output_section(&chk_out, b"TRIG");
    assert_eq!(trig.len(), 73 * 2400);
    assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
    assert_runner_trigger(&trig[71 * 2400..72 * 2400]);
    let last = &trig[72 * 2400..73 * 2400];
    assert_eq!(flag(last), 0x0C);
    // Un-toggling the flag recovers the exact Python blank trigger.
    let mut untoggled = last.to_vec();
    untoggled[2368..2372].copy_from_slice(&(flag(last) ^ 8).to_le_bytes());
    assert_eq!(untoggled, blank_trigger_fixture());

    let _ = std::fs::remove_dir_all(dir.path());
}

#[test]
fn runner_pair_brackets_authored_chain() {
    // ADR 0010 ordering: stage 1 -> begin -> authored -> end -> blank.
    // One authored trigger keeps the indices easy to read.
    let authored = authored_trigger(0x00);
    let (dir, basemap_path) = basemap_with_trig(&[authored.clone()]);
    let chk_out = assemble(&basemap_path, dir.path());

    let trig = output_section(&chk_out, b"TRIG");
    assert_eq!(trig.len(), 74 * 2400);
    assert_runner_trigger(&trig[70 * 2400..71 * 2400]);
    assert_eq!(
        &trig[71 * 2400..72 * 2400],
        authored.as_slice(),
        "the authored trigger runs after begin and before end"
    );
    assert_runner_trigger(&trig[72 * 2400..73 * 2400]);
    assert_eq!(flag(&trig[73 * 2400..74 * 2400]), 0x0C);

    let _ = std::fs::remove_dir_all(dir.path());
}

#[test]
fn single_player_no_tick_gate_links_stage1_directly_to_entry_and_omits_35_triggers() {
    use eudkit_lir::{CTrigAction, LIR_VERSION, LirItem, LirModule, RAW_TRIGGER_BYTE_SIZE, TriggerItem};
    let enabled = authored_trigger(0x00);
    let (dir, basemap_path) = basemap_with_trig(&[enabled.clone()]);
    let out = dir.path().join("out_nogate.scx");
    let chk_out = dir.path().join("out_nogate.chk");

    let doc = eudkit_lir::LirDocument {
        version: LIR_VERSION.to_string(),
        module: LirModule {
            entry: 0,
            items: vec![LirItem::Trigger(TriggerItem {
                size: RAW_TRIGGER_BYTE_SIZE,
                prev: Some(-1),
                next: Some(eudkit_lir::CHAIN_END_PTR),
                prev_reloc: false,
                next_reloc: false,
                conds: vec![],
                acts: vec![CTrigAction::set_deaths(9000, 8, 1, 0).to_act_data()],
                preserved: true,
                flag: None,
            })],
            strings: vec![],
            str_addrs: vec![],
        },
    };
    let program_cost = eudkit_lir::static_cost(&doc);

    let summary = assemble_map(TailInputs {
        lir: &doc,
        basemap: &basemap_path,
        out_path: &out,
        sector_size: None,
        mrgndata: None,
        draw_log_json: None,
        queued_files: vec![],
        debug_emit_chk: Some(&chk_out),
        no_fake_isom: false,
        assembly: AssemblyOptions {
            enable_tick_gate: false,
        },
    })
    .expect("assemble_map without tick gate must succeed");

    assert_eq!(
        summary.static_cost.triggers, program_cost.triggers,
        "reported cost must equal program triggers without the 35 gate triggers"
    );

    let trig = output_section(&chk_out, b"TRIG");
    let strx = output_section(&chk_out, b"STRx");

    let payload = eudkit_lir::lir2payload(&doc).unwrap();
    let payload_offset = STR_SECTION_BASE + (strx.len() - payload.data.len()) as i64;

    // Stage-1 trigger 70 (0-based 69) writes the chain target into
    // CURPL-relative action 0.
    let stage1_last = &trig[69 * 2400..70 * 2400];
    let target = u32::from_le_bytes(stage1_last[320 + 20..320 + 24].try_into().unwrap());
    assert_eq!(
        target as i64,
        payload_offset,
        "stage 1 must link directly to the compiled payload's entry (offset 0)"
    );

    // Verify the payload executes correctly by simulating the program
    let mut sim = eudkit_lir::init_simulation(&doc).unwrap();
    sim.step_loop();
    assert_eq!(sim.last_executed_cost.triggers, program_cost.triggers);
    assert_eq!(sim.external_epds.get(&9000), Some(&1));

    let _ = std::fs::remove_dir_all(dir.path());
}
