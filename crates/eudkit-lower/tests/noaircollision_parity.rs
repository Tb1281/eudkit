//! Cross-frontend parity and runtime proof for the air-collision scrubber
//! (issue #119, port of euddraft `plugins/noAirCollision.py`).
//!
//! Three things are pinned here:
//!
//! * the Python and TypeScript frontends emit byte-identical HIR for the same
//!   `noAirCollision` program, and that HIR lowers;
//! * the emitted zeroing triggers are the source's 115 pointers with
//!   `64, 64, ..., 64, 15` actions (7,311 slots), and the patch pass rewrites
//!   every one of those action destinations to `P, P + 1, ... P + 7,310`;
//! * the first trigger loop already zeroes all 7,311 dwords of the table, and
//!   not one dword outside it, so the patch pass runs before the zeroing
//!   triggers exactly as `onPluginStart` runs before `beforeTriggerExec`.
//!
//! The runtime proof runs in the simulator's default live-read mode, like
//! `unlimiter_parity.rs`: its snapshot model does not model the `while_loop`
//! control-flow rewrite (a bare `while_loop` runs zero iterations there), and
//! the loop scaffolding is shared eudkit machinery rather than part of this
//! helper.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{SimulationState, epd_u32, init_simulation};
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

const PROGRAM_TS: &str = r#"
import { buildModule, noAirCollision } from "./ts/index.ts";
const mod = buildModule(() => { noAirCollision(); });
process.stdout.write(mod.toJson());
"#;

const PROGRAM_PY: &str = r#"
import sys
from py import build_module, noAirCollision
def prog():
    noAirCollision()
sys.stdout.write(build_module(prog).to_json())
"#;

/// `f_epdread_epd(EPD(0x6D5CD8))` reads this dword; the test seeds it.
const REPH_EPD_ADDR: u32 = 0x006D_5CD8;
/// Byte address the seeded pointer holds; its EPD is the table base `P`.
const TABLE_ADDR: u32 = 0x0100_0000;
const TABLE_DWORDS: u32 = 7_311;
/// Marker the test writes into every table dword before the loop runs.
const MARKER: u32 = 0xDEAD_BEEF;

fn simulate(hir: &str) -> SimulationState {
    let doc = hir_from_json(hir).expect("HIR must parse");
    let lir = lower_document(&doc).expect("HIR must lower");
    let mut sim = init_simulation(&lir).expect("simulation must initialize");
    sim.write_epd(epd_u32(REPH_EPD_ADDR), TABLE_ADDR);
    // The dword before the table and two after it are markers too: the pass
    // must stop exactly at the source's 7,311 slots.
    for dword in 0..TABLE_DWORDS + 2 {
        sim.write_epd(epd_u32(TABLE_ADDR).wrapping_add(dword), MARKER);
    }
    sim.write_epd(epd_u32(TABLE_ADDR).wrapping_sub(1), MARKER);
    sim.step_loop();
    sim
}

#[test]
fn py_and_ts_emit_byte_identical_noaircollision_hir() {
    let ts = run_node(PROGRAM_TS);
    let py = run_python(PROGRAM_PY);
    assert_eq!(ts, py, "noAirCollision HIR mismatch");
    let doc = hir_from_json(&ts).expect("HIR must parse");
    lower_document(&doc).expect("noAirCollision HIR must lower");
}

#[test]
fn patch_pass_zeroes_exactly_the_table_every_loop() {
    let hir = run_python(PROGRAM_PY);
    let sim = simulate(&hir);
    let base = epd_u32(TABLE_ADDR);

    for dword in 0..TABLE_DWORDS {
        assert_eq!(
            sim.read_epd(base.wrapping_add(dword)),
            0,
            "table dword {dword} must be zeroed on the first loop"
        );
    }
    assert_eq!(
        sim.read_epd(base.wrapping_sub(1)),
        MARKER,
        "the dword before the table must be untouched"
    );
    for dword in TABLE_DWORDS..TABLE_DWORDS + 2 {
        assert_eq!(
            sim.read_epd(base.wrapping_add(dword)),
            MARKER,
            "the dword after the table must be untouched"
        );
    }
}

/// The 115 zeroing triggers, as `(action count, destinations)` in item order.
fn zeroing_triggers(sim: &SimulationState) -> Vec<(usize, Vec<u32>)> {
    let base = epd_u32(TABLE_ADDR);
    let mut found = Vec::new();
    for item in 0..sim.payload.addrs.len() {
        if sim.payload.addrs[item] as usize + 2_408 > sim.payload.data.len() {
            continue;
        }
        let trig = sim.raw_trigger(item);
        let slots: Vec<u32> = trig
            .acts
            .iter()
            .filter(|act| {
                act.acttype == 45
                    && act.modifier == 7
                    && act.value == 0
                    && act.dest >= base
                    && act.dest < base + TABLE_DWORDS
            })
            .map(|act| act.dest)
            .collect();
        if !slots.is_empty() {
            found.push((slots.len(), slots));
        }
    }
    found
}

#[test]
fn every_zeroing_action_is_patched_to_consecutive_epds() {
    let hir = run_python(PROGRAM_PY);
    let sim = simulate(&hir);
    let base = epd_u32(TABLE_ADDR);

    let triggers = zeroing_triggers(&sim);
    let counts: Vec<usize> = triggers.iter().map(|(count, _)| *count).collect();
    assert_eq!(counts.len(), 115, "the source emits 115 zeroing triggers");
    assert_eq!(
        counts,
        std::iter::repeat_n(64, 114).chain([15]).collect::<Vec<_>>(),
        "114 triggers with 64 actions and a last one with 15"
    );

    let dests: Vec<u32> = triggers.into_iter().flat_map(|(_, slots)| slots).collect();
    assert_eq!(dests.len(), TABLE_DWORDS as usize);
    for (index, dest) in dests.iter().enumerate() {
        assert_eq!(
            *dest,
            base + index as u32,
            "action {index} must be patched to P + {index}"
        );
    }
}

#[test]
fn rendered_zeroing_actions_keep_the_source_shape() {
    let hir = run_python(PROGRAM_PY);
    let sim = simulate(&hir);
    let base = epd_u32(TABLE_ADDR);

    for item in 0..sim.payload.addrs.len() {
        if sim.payload.addrs[item] as usize + 2_408 > sim.payload.data.len() {
            continue;
        }
        let trig = sim.raw_trigger(item);
        let first = trig.acts[0];
        if !(first.acttype == 45 && first.dest >= base && first.dest < base + TABLE_DWORDS) {
            continue;
        }
        assert!(
            trig.conds.iter().all(|cond| cond.condtype == 0),
            "the source's zeroing triggers carry no conditions"
        );
        for act in trig.acts.iter().take_while(|act| act.acttype != 0) {
            assert_eq!(act.acttype, 45, "every zeroing action is a SetMemory");
            assert_eq!(act.modifier, 7, "every zeroing action is `SetTo`");
            assert_eq!(act.value, 0, "every zeroing action writes 0");
        }
    }
}
