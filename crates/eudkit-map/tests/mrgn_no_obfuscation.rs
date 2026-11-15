//! Regression tests for the stage-3 finalizer MRGN gap
//! (`notes/fup-stage3/spec.md`, ticket T2).
//!
//! Root cause (measured): the native `assemble_map` -> `initialize_payload`
//! obfuscated MRGN in the output CHK (`orig - pinned_mrgndata`), but the
//! native pipeline has NO stage-3 finalizer to restore it in game memory
//! (legacy `inj_finalizer.py:create_inject_finalizer` does). In-game
//! locations therefore stayed garbage forever, so CreateUnit at an
//! editor-placed location never appeared.
//!
//! The T1 fix is at the driver level: `pipeline.rs` passes
//! `mrgndata: None` (no obfuscation when no restore runs). These tests
//! pin the `assemble_map` seam that T1 relies on:
//!
//! * `mrgndata: None` -> output CHK MRGN is byte-identical to the
//!   basemap's MRGN (the fixed behaviour).
//! * `mrgndata: Some(pinned_mrgndata)` -> output CHK MRGN DIFFERS from
//!   the basemap's MRGN. This is the pre-T1 driver behaviour, kept here
//!   as a sensitivity proof: it demonstrates that the byte-equality
//!   assertion above CAN fail, i.e. the test is not vacuous. It also
//!   documents that the driver-level e2e check (compile through `eudc`
//!   with a basemap) fails on the pre-T1 build and passes on the fixed
//!   build — the seam test itself passes on both, because it passes
//!   `mrgndata` explicitly rather than going through the driver.
//!
//! NOTE on scope: this file intentionally does NOT go through the `eudc`
//! driver, so it cannot by itself prove T1 landed. The driver-level e2e
//! compile check (test-2.py + ENTRY-OK against test-base.scx) is the
//! coordinator-run verification recorded in the appendix to
//! docs/eud-ir/mrgn-no-obfuscation-note.md.
//!
//! The test reads the output CHK through `TailInputs::debug_emit_chk`
//! (the public Tier-1 harness hook) so no MPQ reader is needed here:
//! the emitted CHK is parsed with the same section walk the in-crate
//! tests use (4-byte name + i32 length + payload, in order).
//!
//! Expected MRGN bytes are constructed from the fixture, not copied from
//! the output under test: `tests/outputmap/basemap/basemap_strx.scx`
//! carries a 5100-byte MRGN (255 locations x 20 bytes) with exactly one
//! non-zero entry — 1-based location id 64 (0-based index 63):
//! left = 0, top = 0, right = 2048, bottom = 2048, string id = 3,
//! flags = 0; every other entry is all zeros. Those values were measured
//! from the fixture with mpqdump before this test was written, and the
//! single-non-zero-entry shape is asserted below so the test cannot pass
//! vacuously on an empty or all-zero section.

use eudkit_map::{AssemblyOptions, assemble_map, rng, TailInputs};
use std::path::{Path, PathBuf};

/// Basemap fixture: blank map plus one real location (id 64).
fn basemap_strx() -> &'static Path {
    Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/outputmap/basemap/basemap_strx.scx"
    ))
}

/// A real native-lowering LIR (string-less, as the native lowering
/// always emits); the payload content is irrelevant to MRGN handling,
/// which only depends on the `mrgndata` input.
fn lir_fixture() -> String {
    r#"{"version":"3.0","module":{"entry":0,"items":[{"kind":"trigger","size":2408,"prev":-1,"next":-2147483648,"prev_reloc":false,"next_reloc":false,"conds":[],"acts":[],"preserved":true}],"strings":[]}}"#.to_string()
}

/// MRGN entry layout: rect 4 x u32LE, string id u16LE at +16, flags
/// u16LE at +18.
fn expected_mrgn() -> Vec<u8> {
    let mut mrgn = vec![0u8; 5100];
    // 1-based location id 64 == 0-based index 63.
    let e = 63 * 20;
    mrgn[e..e + 4].copy_from_slice(&0u32.to_le_bytes()); // left
    mrgn[e + 4..e + 8].copy_from_slice(&0u32.to_le_bytes()); // top
    mrgn[e + 8..e + 12].copy_from_slice(&2048u32.to_le_bytes()); // right
    mrgn[e + 12..e + 16].copy_from_slice(&2048u32.to_le_bytes()); // bottom
    mrgn[e + 16..e + 18].copy_from_slice(&3u16.to_le_bytes()); // string id
    mrgn[e + 18..e + 20].copy_from_slice(&0u16.to_le_bytes()); // flags
    // Non-vacuity guard on the expectation itself: exactly one entry
    // may be non-zero, and it must be entry 63.
    let nonzero: Vec<usize> = (0..255)
        .filter(|&i| mrgn[i * 20..(i + 1) * 20].iter().any(|&b| b != 0))
        .collect();
    assert_eq!(
        nonzero,
        vec![63],
        "expected MRGN must have exactly one non-zero entry (id 64)"
    );
    mrgn
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

/// Scratch dir under the system temp dir (no tempfile dev-dep here;
/// integration tests only see the crate's public API + serde_json).
fn scratch_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "eudc-map-mrgn-{tag}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch dir must be creatable");
    dir
}

fn assemble_with_mrgndata(
    lir_json: &str,
    mrgndata: Option<&[u8]>,
    out: &Path,
    chk_out: &Path,
) {
    let lir: eudkit_lir::LirDocument = serde_json::from_str(lir_json).unwrap();
    assemble_map(TailInputs {
        lir: &lir,
        basemap: basemap_strx(),
        out_path: out,
        sector_size: None,
        mrgndata,
        draw_log_json: None,
        queued_files: vec![],
        debug_emit_chk: Some(chk_out),
        no_fake_isom: false,
        assembly: AssemblyOptions::default(),
    })
    .expect("assemble_map must succeed for the fixture basemap");
}

fn output_mrgn(chk_path: &Path) -> Vec<u8> {
    let chk = std::fs::read(chk_path).expect("debug_emit_chk must write the CHK");
    chk_section(&chk, b"MRGN")
        .expect("output CHK must carry an MRGN section")
        .to_vec()
}

#[test]
fn mrgn_none_preserves_basemap_mrgn_byte_for_byte() {
    let lir = lir_fixture();
    let dir = scratch_dir("none");
    let out = dir.join("out.scx");
    let chk_out = dir.join("out.chk");
    assemble_with_mrgndata(&lir, None, &out, &chk_out);
    assert!(out.is_file(), "assemble_map must emit the output map");

    let got = output_mrgn(&chk_out);
    let expected = expected_mrgn();
    assert_eq!(
        got.len(),
        5100,
        "output MRGN must stay 255 locations x 20 bytes"
    );
    assert_eq!(
        got, expected,
        "with mrgndata: None the output MRGN must equal the basemap MRGN \
         byte-for-byte (location 64 intact, all other entries zero)"
    );
    // Readability spot-check for the location the bug report is about;
    // subsumed by the full-section byte-equality above, kept only so a
    // reader can see location id 64 at a glance.
    let loc64 = &got[63 * 20..64 * 20];
    assert_eq!(
        loc64,
        &expected[63 * 20..64 * 20],
        "location id 64 must survive assembly unchanged"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn mrgn_some_pinned_obfuscates_proving_the_assertion_is_not_vacuous() {
    // Pre-T1 driver behaviour: the driver passed
    // `Some(&pinned_mrgndata())`. The same assertion that passes with
    // `None` MUST fail with `Some` — if it did not, the test would be
    // incapable of catching the regression. This test pins the
    // differing side rather than asserting equality, so it passes on
    // both pre- and post-T1 trees and never depends on the old
    // behaviour being the shipped one.
    let lir = lir_fixture();
    let dir = scratch_dir("some");
    let out = dir.join("out.scx");
    let chk_out = dir.join("out.chk");
    let pinned = rng::pinned_mrgndata();
    assemble_with_mrgndata(&lir, Some(&pinned), &out, &chk_out);

    let got = output_mrgn(&chk_out);
    let expected = expected_mrgn();
    assert_eq!(got.len(), 5100, "obfuscated MRGN keeps its length");
    assert_ne!(
        got, expected,
        "with mrgndata: Some(..) the output MRGN must differ from the \
         basemap MRGN — otherwise this regression test could not detect \
         the pre-T1 obfuscation"
    );
    // The corruption must reach the one real location, not just padding:
    // entry 63 is the only non-zero entry in the fixture, and the
    // obfuscation arithmetic (orig - obfus, wrapping) changes it.
    assert_ne!(
        &got[63 * 20..64 * 20],
        &expected[63 * 20..64 * 20],
        "obfuscation must corrupt location id 64 itself"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
