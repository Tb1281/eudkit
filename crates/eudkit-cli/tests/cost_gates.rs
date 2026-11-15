//! Comprehensive cost gate: one fixture module that exercises every primitive
//! family (packed triggers, structs, views, arrays, containers, strings, block
//! memory, CP context, and every temporal form) synthesized through both the
//! TypeScript and Python frontends, with pinned static/executed cost, storage,
//! packing, and assembled-map reports. The string coverage includes the ADR 0011
//! two-variable hex HUD write so the gate exercises dword-packed formatting.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit_map::AssemblyOptions;
use serde_json::{Value, json};

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn eudkit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(args)
        .current_dir(repo_root())
        .output()
        .expect("run eudkit")
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_ok(output: &Output) {
    assert!(
        output.status.success(),
        "eudkit failed\nstdout:\n{}\nstderr:\n{}",
        stdout(output),
        stderr(output)
    );
}

fn synth_report(fixture: &str) -> String {
    let path = fixtures_dir().join(fixture);
    let out = eudkit(&["synth", path.to_str().unwrap(), "--compact"]);
    assert_ok(&out);
    stdout(&out)
}

fn cost_report(fixture: &str) -> (String, Value) {
    let path = fixtures_dir().join(fixture);
    let out = eudkit(&["dump-ir", path.to_str().unwrap(), "--json", "--loops", "1"]);
    assert_ok(&out);
    let raw = stdout(&out);
    let report = serde_json::from_str(&raw).unwrap_or_else(|e| {
        panic!("{fixture}: dump-ir emitted invalid JSON: {e}\n{raw}");
    });
    (raw, report)
}

#[test]
fn cost_gate_fixtures_synthesize_byte_identical_hir() {
    let ts = synth_report("cost_gate.ts");
    let py = synth_report("cost_gate.py");

    assert_eq!(
        ts, py,
        "TypeScript and Python cost-gate fixtures must synthesize byte-identical HIR"
    );

    let hir: Value = serde_json::from_str(&ts).unwrap();
    assert_eq!(hir["version"], "3.2");
    assert!(
        !hir["module"]["stmts"].as_array().unwrap().is_empty(),
        "cost-gate fixture must emit statements"
    );
}

#[test]
fn cost_gate_arith_fixture_pins_the_102_site_costs_for_both_frontends() {
    // Ticket #102: one Var x Var `mul` site (shared `mul_shift_add` kernel)
    // and one Var / Var `div` plus one `mod` site (single-diff gated ladder)
    // in a dedicated fixture, so the long-frozen cost_gate.* goldens above
    // stay byte-identical. The aggregate is pinned per frontend and the two
    // frontends must agree byte for byte.
    //
    // Per-site standalone measurements (test_ticket102.py/.ts): mul 165T
    // (163T before cycle-2 chunking, 529T before #102), div 259T (291T before
    // cycle 4), mod 259T (291T before cycle 4). Cycle 4 hoists
    // `diff.set(den)` above the numerator-bit trigger so transfer coalescing
    // folds the independent transfers into one page firing per bit. The
    // chunk-16 early exit adds one gate trigger plus its restore trigger per
    // site, hence +2T/+1C/+2A here. Ticket #122 adds `rem.set(0)` +
    // `quot.set(0)` resets in `_divmod_ladder` for re-entrancy (both `div` and
    // `mod` sites), adding +2T/+0C/+6A static (+4,816 trigger bytes) and
    // +4T/+0C/+134A executed on loop 1.
    let ts = synth_report("cost_gate_arith.ts");
    let py = synth_report("cost_gate_arith.py");
    assert_eq!(
        ts, py,
        "TypeScript and Python #102 arith fixtures must synthesize byte-identical HIR"
    );

    let (ts_raw, ts_report) = cost_report("cost_gate_arith.ts");
    let (py_raw, py_report) = cost_report("cost_gate_arith.py");
    assert_eq!(
        ts_raw, py_raw,
        "TypeScript and Python #102 arith fixtures must produce byte-identical reports"
    );
    assert_eq!(ts_report, py_report);

    assert_eq!(
        ts_report["static_cost"],
        json!({
            "triggers": 679,
            "conditions": 225,
            "actions": 1746,
            "trigger_bytes": 1635032,
            "blob_bytes": 32,
            "total_bytes": 1635064,
        })
    );
    assert_eq!(ts_report["executed_cost"]["triggers"], 659);
    assert_eq!(ts_report["executed_cost"]["conditions"], 153);
    assert_eq!(ts_report["executed_cost"]["actions"], 11919);
    assert_eq!(
        ts_report["storage"],
        json!({
            "total_vars": 15,
            "trigger_page_slots": 7,
            "trigger_pages": 1,
            "dword_cells": 8,
            "bit_cells": 0,
            "cell_dwords": 8,
            "cell_bytes": 32,
        })
    );
}

#[test]
fn cost_gate_reports_match_the_pinned_golden_for_both_frontends() {
    // ADR 0014's decimal default needed no golden update: cost_gate.ts and
    // cost_gate.py write only the static string "cost-gate" and contain no
    // dynamic decimal placeholder, so their static/executed costs are unchanged.
    let golden_path = fixtures_dir().join("cost_gate.expected.json");
    let golden_raw = fs::read_to_string(&golden_path)
        .unwrap_or_else(|e| panic!("read {}: {e}", golden_path.display()));
    let golden: Value = serde_json::from_str(&golden_raw).expect("golden report is valid JSON");

    // Pinned pipeline totals. The golden file also covers the full storage summary
    // and every packing entry; these explicit values keep the headline numbers
    // readable and catch accidental re-blessing. The ADR 0011 two-variable hex
    // HUD adds 83T static / 83T executed to the pre-existing fixture baseline;
    // ticket #10's constant-fill fold removes 6T static / 14T executed (the
    // fixture's `memsetEPD(3000, 0xAABBCCDD, 2)` no longer emits a runtime loop);
    // ticket #11 adds the memory-primitive probes (eq / ne / read / MemCond and
    // the variable-count block calls), taking the gate to 388T; ticket #16
    // expands the `.ne` probe's deferred marker to the 4T flag form, taking the
    // gate to 355T.
    //
    // Ticket #97 replaces every full-dword `read()` with one shared 32-bit-test
    // reader declared once per module, so the duplicated inline readers collapse
    // to a single body and each call site pays a call sequence instead of a
    // full inline copy. The fixture has three funcs with call counts 1, 2 and
    // 1; the rework then splices the two single-site bodies back in
    // (substituting arguments for parameters). Net static triggers 355 -> 320
    // and conditions 282 -> 250; static actions fall to 869 because the two
    // inlined bodies no longer need per-call field patching.
    // Executed cost: argument/result `TriggerPage` traffic is gone for the
    // inlined sites and call-result fusion removes the result copy, so the
    // gate is 435T / 332C / 2093A executed -- still under the all-inlined
    // 442T / 2412A baseline while keeping the static win (K > 1 sites share).
    //
    // Merging ticket #96 (one block-wide scheduler over transfer runs and
    // `VarStore`s) folds two more store triggers into the transfer batch's
    // setup firing; HIR is otherwise unchanged.
    //
    // C2-01 replaces the block-memory cursor variables with in-memory pointer
    // cursors (the CP register for the source, a scratch dword for the
    // destination), so each loop drops its cursor stores: static triggers
    // 320 -> 319, conditions 250 -> 252 and actions 869 -> 856, with storage
    // losing five scratch vars. Executed: 435T/332C/2093A -> 425T/334C/1683A.
    // Gating the constant-destination memset seed for a runtime-zero count
    // (C2-01 review F3) adds back one trigger/condition, so the shipped totals
    // are 320T/253C/856A and 426T/335C/1683A executed.
    assert_eq!(
        golden["static_cost"],
        json!({
            "triggers": 320,
            "conditions": 253,
            "actions": 856,
            "trigger_bytes": 770560,
            "blob_bytes": 164,
            "total_bytes": 770724,
        })
    );
    assert_eq!(
        golden["executed_cost"]["triggers"],
        Value::from(426),
        "executed cost is for the last of --loops 1"
    );
    assert_eq!(golden["executed_cost"]["conditions"], 335);
    assert_eq!(golden["executed_cost"]["actions"], 1683);
    assert_eq!(
        golden["executed_cost"]["visited_items"]
            .as_array()
            .unwrap()
            .len(),
        426
    );
    assert_eq!(
        golden["storage"],
        json!({
            "total_vars": 260,
            "trigger_page_slots": 205,
            "trigger_pages": 4,
            "dword_cells": 40,
            "bit_cells": 15,
            "cell_dwords": 41,
            "cell_bytes": 164,
        })
    );

    // The explicitly packed trigger must survive lowering as one full 16C x 64A
    // page rather than being split or unpacked into basic blocks.
    let packed: Vec<&Value> = golden["packing"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|entry| entry["conditions"] == 16)
        .collect();
    assert_eq!(packed.len(), 1, "exactly one 16-condition trigger expected");
    assert_eq!(packed[0]["actions"], 64);
    assert_eq!(packed[0]["condition_density"], 1.0);
    assert_eq!(packed[0]["action_density"], 1.0);

    let ts = cost_report("cost_gate.ts");
    let py = cost_report("cost_gate.py");

    assert_eq!(
        ts.0, py.0,
        "TypeScript and Python fixtures must produce byte-identical reports"
    );
    assert_eq!(ts.1, golden, "cost_gate.ts report drifted from golden");
    assert_eq!(py.1, golden, "cost_gate.py report drifted from golden");
}

#[test]
fn cost_gate_assembled_map_costs_match_the_pinned_golden_for_both_frontends() {
    let golden_path = fixtures_dir().join("cost_gate.assembled.expected.json");
    let golden_raw = fs::read_to_string(&golden_path)
        .unwrap_or_else(|e| panic!("read {}: {e}", golden_path.display()));
    let golden: Value = serde_json::from_str(&golden_raw).expect("assembled golden is valid JSON");

    let ts = eudkit::prepare_source(
        &fixtures_dir().join("cost_gate.ts"),
        Some(eudkit::frontend::Frontend::Ts),
        None,
    )
    .expect("prepare cost_gate.ts");
    let py = eudkit::prepare_source(
        &fixtures_dir().join("cost_gate.py"),
        Some(eudkit::frontend::Frontend::Py),
        None,
    )
    .expect("prepare cost_gate.py");

    let ungated = AssemblyOptions {
        enable_tick_gate: false,
    };
    let gated = AssemblyOptions::default();
    let ts_ungated = ts.assembled_static_cost_with_options(ungated);
    let ts_gated = ts.assembled_static_cost_with_options(gated);
    let py_ungated = py.assembled_static_cost_with_options(ungated);
    let py_gated = py.assembled_static_cost_with_options(gated);

    assert_eq!(
        ts_ungated, py_ungated,
        "TypeScript and Python no-gate assembled costs must match"
    );
    assert_eq!(
        ts_gated, py_gated,
        "TypeScript and Python gated assembled costs must match"
    );
    assert_eq!(
        ts_ungated,
        ts.static_cost(),
        "no-gate assembly must leave the program cost unchanged"
    );
    assert_eq!(
        ts_gated.triggers,
        ts_ungated.triggers + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
    );
    assert_eq!(
        ts_gated.conditions,
        ts_ungated.conditions + eudkit_map::tick_gate::GATE_CONDITION_COUNT
    );
    assert_eq!(
        ts_gated.actions,
        ts_ungated.actions + eudkit_map::tick_gate::GATE_ACTION_COUNT
    );

    // Headline assembled-map numbers: the 35-trigger tick gate takes the
    // 320T fixture payload to 355T by default; --no-tick-gate leaves it at
    // 320T (355T before #97's shared reader, 307T before #96's scheduler).
    assert_eq!(
        ts_ungated.triggers, 320,
        "ungated assembled cost must stay 320T"
    );
    assert_eq!(ts_gated.triggers, 355, "gated assembled cost must be 355T");
    assert_eq!(serde_json::to_value(ts_ungated).unwrap(), golden["ungated"]);
    assert_eq!(serde_json::to_value(ts_gated).unwrap(), golden["gated"]);
}

#[test]
fn cost_gate_compiled_maps_match_the_pinned_gated_and_ungated_costs() {
    let golden_path = fixtures_dir().join("cost_gate.assembled.expected.json");
    let golden_raw = fs::read_to_string(&golden_path)
        .unwrap_or_else(|e| panic!("read {}: {e}", golden_path.display()));
    let golden: Value = serde_json::from_str(&golden_raw).expect("assembled golden is valid JSON");

    let dir = tempfile::tempdir().unwrap();
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let mut gated_maps = Vec::new();
    let mut ungated_maps = Vec::new();

    for (frontend, extension) in [
        (eudkit::frontend::Frontend::Ts, "ts"),
        (eudkit::frontend::Frontend::Py, "py"),
    ] {
        let source = fixtures_dir().join(format!("cost_gate.{extension}"));

        let prepared = eudkit::prepare_source(&source, Some(frontend), Some(&basemap))
            .expect("prepare cost gate");

        let gated_path = dir.path().join(format!("cost_gate_gated.{extension}.scx"));
        let gated = eudkit::compile_prepared_with_options(
            &prepared,
            &gated_path,
            AssemblyOptions::default(),
        )
        .expect("compile gated cost gate");
        assert_eq!(
            serde_json::to_value(gated).unwrap(),
            golden["gated"],
            "gated {extension} compiled map cost drifted from golden"
        );
        gated_maps.push(fs::read(&gated_path).unwrap());

        let ungated_path = dir
            .path()
            .join(format!("cost_gate_ungated.{extension}.scx"));
        let ungated = eudkit::compile_prepared_with_options(
            &prepared,
            &ungated_path,
            AssemblyOptions {
                enable_tick_gate: false,
            },
        )
        .expect("compile ungated cost gate");
        assert_eq!(
            serde_json::to_value(ungated).unwrap(),
            golden["ungated"],
            "ungated {extension} compiled map cost drifted from golden"
        );
        ungated_maps.push(fs::read(&ungated_path).unwrap());
    }

    assert_eq!(
        gated_maps[0], gated_maps[1],
        "TypeScript and Python gated cost-gate maps must be byte-identical"
    );
    assert_eq!(
        ungated_maps[0], ungated_maps[1],
        "TypeScript and Python ungated cost-gate maps must be byte-identical"
    );
}
