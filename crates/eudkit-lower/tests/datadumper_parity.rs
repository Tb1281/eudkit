//! Cross-frontend parity and runtime proof for the data dumper core helper
//! (issue #114, port of eudplib `_add_datadumper` / `_f_datadumper` and
//! euddraft `plugins/dataDumper.py`).
//!
//! Three things are pinned here:
//!
//! * the Python and TypeScript frontends emit byte-identical HIR for the same
//!   `dataDumper` program in all three modes, and each HIR lowers;
//! * the default/`unpatchable` write stores the blob's payload address in the
//!   target dword, and the bytes are readable at that address in the
//!   simulated payload;
//! * the `copy` mode reads the destination's byte address from the target
//!   dword at run time, converts it to an EPD (eudplib's `f_epdread_epd`) and
//!   copies `ceil(len / 4)` dwords there, with the tail dword padded by
//!   in-blob zeros.
//!
//! The simulation proof also covers the `eudkit-compile sim` readback fix: a
//! module with trigger pages *and* HIR blobs used to slice past the payload
//! when reading variables back.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{SimulationState, epd_u32, simulate_trigger_loop};
use eudkit_lower::lower_document;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn run_node(script: &str) -> String {
    let out = Command::new("node")
        .args(["--input-type=module", "-e", script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the TS snippet");
    assert!(
        out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn run_python(script: &str) -> String {
    let out = Command::new("python3")
        .args(["-c", script])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run the Python snippet");
    assert!(
        out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

/// eudplib's own test copies a `RequireData` file to the address stored here.
const PTR: u32 = 0x58D740;
/// An external scratch address (`copy` writes the payload here at run time).
const DEST: u32 = 0x58E000;
/// The payload copied by `copy`; the last dword carries two padding zeros.
const PAYLOAD_DWORDS: [u32; 3] = [
    u32::from_le_bytes(*b"ABCD"),
    u32::from_le_bytes(*b"EFGH"),
    u32::from_le_bytes(*b"IJ\0\0"),
];

const PROGRAM_TS: &str = r#"
import { buildModule, dataDumper, SetMemory, SetTo } from "./ts/index.ts";
const payload = new TextEncoder().encode("ABCDEFGHIJ");
const mod = buildModule(() => {
  SetMemory(0x58d740, SetTo, 0x58e000);
  dataDumper(payload, [0x58d740], ["copy"]);
});
process.stdout.write(mod.toJson());
"#;

const PROGRAM_PY: &str = r#"
import sys
from py import SetMemory, SetTo, build_module, data_dumper
def prog():
    SetMemory(0x58D740, SetTo, 0x58E000)
    data_dumper(b"ABCDEFGHIJ", [0x58D740], flags=("copy",))
sys.stdout.write(build_module(prog).to_json())
"#;

const DEFAULT_TS: &str = r#"
import { buildModule, dataDumper } from "./ts/index.ts";
const mod = buildModule(() => {
  dataDumper(new TextEncoder().encode("ABCDEFGHIJ"), [0x58d740]);
});
process.stdout.write(mod.toJson());
"#;

const DEFAULT_PY: &str = r#"
import sys
from py import build_module, data_dumper
def prog():
    data_dumper(b"ABCDEFGHIJ", [0x58D740])
sys.stdout.write(build_module(prog).to_json())
"#;

const UNPATCHABLE_TS: &str = r#"
import { buildModule, dataDumper } from "./ts/index.ts";
const mod = buildModule(() => {
  dataDumper(new TextEncoder().encode("ABCDEFGHIJ"), [0x58d740], ["unpatchable"]);
});
process.stdout.write(mod.toJson());
"#;

const UNPATCHABLE_PY: &str = r#"
import sys
from py import build_module, data_dumper
def prog():
    data_dumper(b"ABCDEFGHIJ", [0x58D740], flags=("unpatchable",))
sys.stdout.write(build_module(prog).to_json())
"#;

const MULTI_TS: &str = r#"
import { buildModule, dataDumper } from "./ts/index.ts";
const mod = buildModule(() => {
  dataDumper(new TextEncoder().encode("ABCDEFGHIJ"), [0x58d740, 0x58d744]);
});
process.stdout.write(mod.toJson());
"#;

const MULTI_PY: &str = r#"
import sys
from py import build_module, data_dumper
def prog():
    data_dumper(b"ABCDEFGHIJ", [0x58D740, 0x58D744])
sys.stdout.write(build_module(prog).to_json())
"#;

fn simulate(hir: &str) -> SimulationState {
    let doc = hir_from_json(hir).expect("HIR must parse");
    let lir = lower_document(&doc).expect("HIR must lower");
    simulate_trigger_loop(&lir).expect("simulation must initialize and step")
}

fn read(sim: &SimulationState, addr: u32) -> u32 {
    sim.read_epd(epd_u32(addr))
}

fn assert_payload_at(sim: &SimulationState, addr: u32) {
    for (index, expected) in PAYLOAD_DWORDS.iter().enumerate() {
        assert_eq!(
            read(sim, addr + 4 * index as u32),
            *expected,
            "payload dword {index} at {addr:#x}"
        );
    }
}

fn assert_parity(ts_script: &str, py_script: &str, what: &str) -> String {
    let ts = run_node(ts_script);
    let py = run_python(py_script);
    assert_eq!(ts, py, "{what} HIR mismatch");
    lower_document(&hir_from_json(&ts).expect("HIR must parse"))
        .expect("HIR must lower");
    ts
}

#[test]
fn py_and_ts_emit_byte_identical_datadumper_hir() {
    assert_parity(DEFAULT_TS, DEFAULT_PY, "default dataDumper");
    assert_parity(UNPATCHABLE_TS, UNPATCHABLE_PY, "unpatchable dataDumper");
    assert_parity(MULTI_TS, MULTI_PY, "multi-target dataDumper");
    assert_parity(PROGRAM_TS, PROGRAM_PY, "copy dataDumper");
}

#[test]
fn default_mode_stores_the_blob_address_in_the_target() {
    let sim = simulate(&run_python(DEFAULT_PY));

    // The blob payload is the only item after the 2,408-byte trigger page.
    let blob_addr = read(&sim, PTR);
    assert_eq!(blob_addr, 2408, "blob payload address");
    assert_payload_at(&sim, blob_addr);
}

#[test]
fn unpatchable_mode_stores_the_blob_address_in_the_target() {
    let sim = simulate(&run_python(UNPATCHABLE_PY));

    let blob_addr = read(&sim, PTR);
    assert_eq!(blob_addr, 2408, "blob payload address");
    assert_payload_at(&sim, blob_addr);
}

#[test]
fn copy_mode_copies_the_padded_payload_to_the_destination() {
    let sim = simulate(&run_python(PROGRAM_PY));

    // The pre-seed is the only writer of the target dword; `copy` reads that
    // byte address and copies the payload to `DEST`.
    assert_eq!(read(&sim, PTR), DEST, "target still holds the destination");
    assert_payload_at(&sim, DEST);
}
