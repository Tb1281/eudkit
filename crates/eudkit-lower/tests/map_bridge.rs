//! End-to-end coverage for the `eudkit-compile map` bridge (ADR 0013
//! decision 5): the bridge must carry the assembly option through to
//! `compile_hir_to_map_with_options`, omit the once-per-tick gate for both
//! opt-out spellings, and report the cost of the map it actually wrote.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit_lower::{
    AssemblyOptions, LowerError, compile_hir_to_map, compile_hir_to_map_with_options,
};
use eudkit_map::MapTailError;
use serde_json::Value;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn basemap() -> PathBuf {
    repo_root().join("tests/outputmap/basemap/basemap.scx")
}

/// Synthesize the ADR 0011 two-variable hex HUD through the TypeScript
/// frontend. The bridge consumes HIR, so this is the smallest fixture that
/// also pins the 83T program cost.
fn two_variable_hex_hir() -> String {
    let script = r#"
import { FixedString, Var, buildModule } from "./ts/index.ts";

const mod = buildModule(() => {
  const hud = new FixedString(32);
  const wave = Var(1);
  const hp = Var(100);
  hud.write("wave={:x} hp={:x}", wave, hp);
});
process.stdout.write(mod.toJson());
"#;
    let out = Command::new("node")
        .args(["--input-type=module", "-e", script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the TS fixture");
    assert!(
        out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).expect("HIR JSON is UTF-8")
}

fn run_map_bridge(hir_path: &Path, out_path: &Path, flags: &[&str]) -> (Value, Output) {
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit-compile"))
        .arg("map")
        .arg(hir_path)
        .arg(basemap())
        .arg(out_path)
        .args(flags)
        .current_dir(repo_root())
        .output()
        .expect("run eudkit-compile map");
    assert!(
        out.status.success(),
        "eudkit-compile map {flags:?} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let cost: Value = serde_json::from_slice(&out.stdout)
        .expect("map bridge must print the StaticCost JSON of the written map");
    (cost, out)
}

#[test]
fn map_bridge_omits_the_tick_gate_for_both_opt_out_spellings() {
    let dir = tempfile::tempdir().unwrap();
    let hir_path = dir.path().join("hud.hir.json");
    fs::write(&hir_path, two_variable_hex_hir()).unwrap();

    let gated_path = dir.path().join("gated.scx");
    let (gated, _) = run_map_bridge(&hir_path, &gated_path, &[]);
    assert_eq!(
        gated["triggers"], 118,
        "default bridge assembly must include the 35-trigger gate on top of 83T"
    );
    assert!(gated_path.is_file(), "default bridge assembly must write the map");

    let no_gate_path = dir.path().join("no_gate.scx");
    let (no_gate, _) = run_map_bridge(&hir_path, &no_gate_path, &["--no-tick-gate"]);
    assert_eq!(
        no_gate["triggers"], 83,
        "--no-tick-gate must assemble the program without the gate"
    );
    assert!(
        no_gate_path.is_file(),
        "--no-tick-gate bridge assembly must write the map"
    );

    let single_player_path = dir.path().join("single_player.scx");
    let (single_player, _) =
        run_map_bridge(&hir_path, &single_player_path, &["--single-player"]);
    assert_eq!(
        single_player["triggers"], 83,
        "--single-player must assemble the program without the gate"
    );
    assert!(
        single_player_path.is_file(),
        "--single-player bridge assembly must write the map"
    );

    assert_eq!(
        gated["triggers"].as_u64().unwrap() - no_gate["triggers"].as_u64().unwrap(),
        eudkit_map::tick_gate::GATE_TRIGGER_COUNT as u64,
        "the bridge opt-out must drop exactly the 35 gate triggers"
    );
}

#[test]
fn empty_program_entry_fails_on_both_compile_hir_to_map_paths() {
    // An empty HIR module lowers to `entry: 0` with no payload items, which
    // is exactly the dangling-entry shape. Both public map entry points must
    // surface the hard error: the gated default must not let the gate turn
    // the missing entry into `CHAIN_END_PTR`.
    let dir = tempfile::tempdir().unwrap();
    let hir_json = r#"{"version":"3.2","module":{"vars":[],"stmts":[],"strings":[]}}"#;

    for (name, result) in [
        (
            "gated default",
            compile_hir_to_map(hir_json, &basemap(), &dir.path().join("gated.scx")),
        ),
        (
            "ungated option",
            compile_hir_to_map_with_options(
                hir_json,
                &basemap(),
                &dir.path().join("ungated.scx"),
                AssemblyOptions {
                    enable_tick_gate: false,
                },
            ),
        ),
    ] {
        match result {
            Err(LowerError::Map(MapTailError::EntryOutOfRange { entry, item_count })) => {
                assert_eq!(entry, 0, "{name}: offending entry");
                assert_eq!(item_count, 0, "{name}: payload item count");
            }
            other => panic!("{name}: expected EntryOutOfRange, got {other:?}"),
        }
    }
    assert!(
        !dir.path().join("gated.scx").exists() && !dir.path().join("ungated.scx").exists(),
        "a rejected entry must not write a map on either path"
    );
}
