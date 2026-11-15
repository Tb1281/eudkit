//! End-to-end CLI tests: both frontends, dump-ir, synth, and frozen plans.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit::Frontend;
use eudkit::plan::{BuildPlan, PlanEntry};
use eudkit_map::AssemblyOptions;
use serde_json::Value;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn basemap() -> PathBuf {
    repo_root().join("tests/outputmap/basemap/basemap.scx")
}

fn eudkit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(args)
        .current_dir(repo_root())
        .env("EUDKIT_ROOT", repo_root())
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

fn write_ts_with_preamble(dir: &Path, name: &str, preamble: &str, body: &str) -> PathBuf {
    let root = repo_root().display();
    let source = format!(
        "{preamble}import {{ Var, buildModule, when }} from \"{root}/ts/index.ts\";\n\
         export const module = buildModule(() => {{\n{body}\n}});\n"
    );
    let path = dir.join(name);
    fs::write(&path, source).unwrap();
    path
}

fn write_ts(dir: &Path, name: &str, body: &str) -> PathBuf {
    write_ts_with_preamble(dir, name, "", body)
}

fn write_py_with_preamble(dir: &Path, name: &str, preamble: &str, body: &str) -> PathBuf {
    let source = format!(
        "{preamble}from py import Var, build_module, when\n\
         def prog():\n{body}\n\
         module = build_module(prog)\n"
    );
    let path = dir.join(name);
    fs::write(&path, source).unwrap();
    path
}

fn write_py(dir: &Path, name: &str, body: &str) -> PathBuf {
    write_py_with_preamble(dir, name, "", body)
}

fn demo_body() -> &'static str {
    "    const x = Var(0);\n    when(x.eq(0), () => {\n      x.set(7);\n    });"
}

fn demo_py_body() -> &'static str {
    "    x = Var(0)\n    when(x.eq(0), lambda: x.set(7))"
}

const ENTRY_SURFACES: [&str; 4] = ["synth", "dump-ir", "compile", "plan"];

/// Run one CLI entry surface against `source`. Plan mode freezes a plan
/// around the same source so the failure travels through `compile --plan`.
fn run_entry_surface(surface: &str, dir: &Path, source: &Path, frontend: Frontend) -> Output {
    match surface {
        "synth" => eudkit(&["synth", source.to_str().unwrap()]),
        "dump-ir" => eudkit(&["dump-ir", source.to_str().unwrap(), "--json"]),
        "compile" => {
            let out = dir.join(format!(
                "{}-surface.scx",
                source.file_stem().unwrap().to_str().unwrap()
            ));
            eudkit(&[
                "compile",
                source.to_str().unwrap(),
                "--basemap",
                basemap().to_str().unwrap(),
                "--out",
                out.to_str().unwrap(),
            ])
        }
        "plan" => {
            let stem = source.file_stem().unwrap().to_str().unwrap();
            let plan_path = dir.join(format!("{stem}-surface.plan.json"));
            let out = dir.join(format!("{stem}-surface-plan.scx"));
            let entry = PlanEntry::freeze(stem, source, frontend, &basemap(), &out, None).unwrap();
            fs::write(&plan_path, BuildPlan::new(vec![entry]).to_json().unwrap()).unwrap();
            let out_dir = dir.join(format!("{stem}-surface-plan-out"));
            eudkit(&[
                "compile",
                "--plan",
                plan_path.to_str().unwrap(),
                "--out-dir",
                out_dir.to_str().unwrap(),
            ])
        }
        other => panic!("unknown entry surface {other}"),
    }
}

fn assert_entry_diagnostic(output: &Output, source: &Path, expected: &[&str], surface: &str) {
    assert!(
        !output.status.success(),
        "{surface}: entry source unexpectedly compiled\nstdout:\n{}",
        stdout(output)
    );
    let err = stderr(output);
    let canonical = fs::canonicalize(source).unwrap();
    let mut needles = vec!["E-ENTRY", canonical.to_str().unwrap()];
    needles.extend_from_slice(expected);
    for needle in needles {
        assert!(
            err.contains(needle),
            "{surface}: expected {needle:?} in stderr, got:\n{err}"
        );
    }
}

#[test]
fn dump_ir_text_reports_static_cost_storage_and_packing_density() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let out = eudkit(&["dump-ir", source.to_str().unwrap(), "--frontend", "ts"]);
    assert_ok(&out);
    let text = stdout(&out);
    assert!(text.contains("Static cost: 1T static"), "{text}");
    assert!(text.contains("Cost stage: pre-assembly"), "{text}");
    assert!(text.contains("Executed cost: 1T executed"), "{text}");
    assert!(text.contains("TriggerPage slot(s)"), "{text}");
    assert!(text.contains("dword cell(s)"), "{text}");
    assert!(text.contains("1/16C"), "{text}");
    assert!(text.contains("1/64A"), "{text}");
    assert!(text.contains("Allocations:"), "{text}");
    assert!(text.contains("var[0]"), "{text}");
}

#[test]
fn dump_ir_json_reports_the_same_pipeline_for_python() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_py(dir.path(), "demo.py", demo_py_body());
    let out = eudkit(&[
        "dump-ir",
        source.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&out);
    let report: Value = serde_json::from_str(&stdout(&out)).unwrap();
    assert_eq!(report["static_cost"]["triggers"], 1);
    assert_eq!(report["static_cost"]["conditions"], 1);
    assert_eq!(report["static_cost"]["actions"], 1);
    assert_eq!(
        report["pre_assembly"], true,
        "dump-ir costs must be labelled pre-assembly"
    );
    assert_eq!(report["executed_cost"]["triggers"], 1);
    assert_eq!(report["executed_cost"]["actions"], 1);
    assert_eq!(report["storage"]["total_vars"], 1);
    assert_eq!(report["storage"]["dword_cells"], 1);
    assert_eq!(report["packing"][0]["conditions"], 1);
    assert_eq!(report["packing"][0]["actions"], 1);
}

#[test]
fn dump_ir_accepts_a_synthesized_hir_file() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let hir_path = dir.path().join("demo.hir.json");
    let synth = eudkit(&[
        "synth",
        source.to_str().unwrap(),
        "--out",
        hir_path.to_str().unwrap(),
    ]);
    assert_ok(&synth);

    let out = eudkit(&["dump-ir", hir_path.to_str().unwrap(), "--json"]);
    assert_ok(&out);
    let report: Value = serde_json::from_str(&stdout(&out)).unwrap();
    assert_eq!(report["static_cost"]["triggers"], 1);
}

#[test]
fn synth_emits_validated_hir_for_both_frontends() {
    let dir = tempfile::tempdir().unwrap();
    let ts = write_ts(dir.path(), "demo.ts", demo_body());
    let py = write_py(dir.path(), "demo.py", demo_py_body());

    let ts_out = eudkit(&["synth", ts.to_str().unwrap()]);
    assert_ok(&ts_out);
    let ts_hir: Value = serde_json::from_str(&stdout(&ts_out)).unwrap();
    assert_eq!(ts_hir["version"], "3.2");
    assert!(!ts_hir["module"]["stmts"].as_array().unwrap().is_empty());

    let py_out = eudkit(&["synth", py.to_str().unwrap(), "--compact"]);
    assert_ok(&py_out);
    let py_hir: Value = serde_json::from_str(&stdout(&py_out)).unwrap();
    assert_eq!(
        ts_hir["module"], py_hir["module"],
        "TS and Py must synthesize identical HIR"
    );
}

#[test]
fn compile_writes_a_readable_scx_for_both_frontends() {
    let dir = tempfile::tempdir().unwrap();
    let ts = write_ts(dir.path(), "demo.ts", demo_body());
    let py = write_py(dir.path(), "demo.py", demo_py_body());

    for (source, name) in [(&ts, "ts.scx"), (&py, "py.scx")] {
        let out_path = dir.path().join(name);
        let out = eudkit(&[
            "compile",
            source.to_str().unwrap(),
            "--basemap",
            basemap().to_str().unwrap(),
            "--out",
            out_path.to_str().unwrap(),
        ]);
        assert_ok(&out);
        // 1 program trigger + 35 once-per-tick gate triggers.
        assert!(stdout(&out).contains("36T static"), "{}", stdout(&out));
        let info = eudkit_map::mapinfo::extract_map_info(&out_path).unwrap();
        assert!(info.strings_b64.len() > 100);
        assert!(fs::metadata(&out_path).unwrap().len() > 10_000);
    }
}

#[test]
fn compile_and_plan_report_the_gate_inclusive_static_cost() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());

    // dump-ir stays pre-assembly: one program trigger, explicitly labelled.
    let dumped = eudkit(&["dump-ir", source.to_str().unwrap(), "--json"]);
    assert_ok(&dumped);
    let report: Value = serde_json::from_str(&stdout(&dumped)).unwrap();
    assert_eq!(report["static_cost"]["triggers"], 1);
    assert_eq!(report["pre_assembly"], true);

    // compile and --plan-out use the assembled payload: 1 + 35 gate
    // triggers. The regression in eudkit-map proves this number equals
    // the trigger count encoded into the written STRx payload.
    let out_path = dir.path().join("demo.scx");
    let plan_path = dir.path().join("plan.json");
    let compiled = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
    ]);
    assert_ok(&compiled);
    assert!(
        stdout(&compiled).contains("36T static"),
        "{}",
        stdout(&compiled)
    );
    let plan: Value = serde_json::from_str(&fs::read_to_string(&plan_path).unwrap()).unwrap();
    assert_eq!(plan["entries"][0]["expected_static_cost"]["triggers"], 36);

    // Rebuilding from the plan reports and enforces the same contract.
    let rebuilt = dir.path().join("rebuilt");
    let second = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        rebuilt.to_str().unwrap(),
    ]);
    assert_ok(&second);
    assert!(
        stdout(&second).contains("36T static"),
        "{}",
        stdout(&second)
    );
}

#[test]
fn single_player_and_no_tick_gate_compile_with_35_fewer_triggers_and_execute_payload() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());

    // 1. Standard compilation has 36 triggers (1 program + 35 gate triggers).
    let default_out = dir.path().join("default.scx");
    let default_compiled = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        default_out.to_str().unwrap(),
    ]);
    assert_ok(&default_compiled);
    assert!(
        stdout(&default_compiled).contains("36T static"),
        "{}",
        stdout(&default_compiled)
    );

    // 2. With --single-player: reports 1T static (35 fewer triggers).
    let sp_out = dir.path().join("sp.scx");
    let sp_compiled = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        sp_out.to_str().unwrap(),
        "--single-player",
    ]);
    assert_ok(&sp_compiled);
    assert!(
        stdout(&sp_compiled).contains("1T static"),
        "{}",
        stdout(&sp_compiled)
    );
    assert!(sp_out.is_file());

    // 3. With --no-tick-gate: reports 1T static (35 fewer triggers).
    let no_gate_out = dir.path().join("no_gate.scx");
    let no_gate_compiled = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        no_gate_out.to_str().unwrap(),
        "--no-tick-gate",
    ]);
    assert_ok(&no_gate_compiled);
    assert!(
        stdout(&no_gate_compiled).contains("1T static"),
        "{}",
        stdout(&no_gate_compiled)
    );
    assert!(no_gate_out.is_file());

    // 4. Output map verification: verify the STRx payload has 35 fewer triggers (35 * 2408 bytes).
    let extract_strx_len = |path: &Path| -> usize {
        let mut ar = stormlib::Archive::open(path, stormlib::OpenArchiveFlags::empty()).unwrap();
        let chk = ar
            .open_file("staredit\\scenario.chk")
            .unwrap()
            .read_all()
            .unwrap();
        let mut off = 0;
        while off + 8 <= chk.len() {
            let name = &chk[off..off + 4];
            let ln = i32::from_le_bytes(chk[off + 4..off + 8].try_into().unwrap()) as usize;
            if name == b"STRx" {
                return ln;
            }
            off += 8 + ln;
        }
        panic!("STRx section not found");
    };

    let default_strx_len = extract_strx_len(&default_out);
    let sp_strx_len = extract_strx_len(&sp_out);
    let no_gate_strx_len = extract_strx_len(&no_gate_out);

    assert_eq!(sp_strx_len, no_gate_strx_len);
    assert_eq!(default_strx_len - sp_strx_len, 35 * 2408);

    // 5. Verify payload executes correctly when single-player / no-tick-gate is used.
    let prepared = eudkit::prepare_source(
        &source,
        Some(eudkit::frontend::Frontend::Ts),
        Some(&basemap()),
    )
    .unwrap();
    let cost = prepared.assembled_static_cost_with_options(AssemblyOptions {
        enable_tick_gate: false,
    });
    assert_eq!(cost.triggers, 1);
    let mut sim = eudkit_lir::init_simulation(&prepared.lir).unwrap();
    sim.step_loop();
    assert_eq!(sim.last_executed_cost.triggers, 1);
}

#[test]
fn single_player_plan_round_trips_at_the_ungated_static_cost() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let plan_path = dir.path().join("plan.json");
    let out_path = dir.path().join("demo.scx");

    // Freeze a plan from a single-player build: the ungated cost is 1T.
    let first = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
        "--single-player",
    ]);
    assert_ok(&first);
    assert!(stdout(&first).contains("1T static"), "{}", stdout(&first));
    let plan: Value = serde_json::from_str(&fs::read_to_string(&plan_path).unwrap()).unwrap();
    assert_eq!(
        plan["entries"][0]["expected_static_cost"]["triggers"], 1,
        "a single-player plan must freeze the ungated assembled cost"
    );

    // Rebuilding with the same option honors the plan's ungated cost.
    let out_dir = dir.path().join("rebuilt");
    let second = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        out_dir.to_str().unwrap(),
        "--no-tick-gate",
    ]);
    assert_ok(&second);
    assert!(stdout(&second).contains("1T static"), "{}", stdout(&second));
    assert!(out_dir.join("demo.scx").is_file());

    // Plans do not persist the option: a gated rebuild mismatches loudly
    // instead of silently writing a map with the wrong cost.
    let gated_dir = dir.path().join("gated");
    let third = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        gated_dir.to_str().unwrap(),
    ]);
    assert!(!third.status.success());
    assert!(stderr(&third).contains("E-COST-GATE"), "{}", stderr(&third));
    assert!(
        !gated_dir.join("demo.scx").is_file(),
        "a cost-gated build must not write a map"
    );
}

#[test]
fn frozen_plan_round_trips_and_rejects_a_changed_basemap() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let plan_path = dir.path().join("plan.json");
    let out_path = dir.path().join("demo.scx");

    let first = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
    ]);
    assert_ok(&first);
    assert!(out_path.is_file());

    let plan: Value = serde_json::from_str(&fs::read_to_string(&plan_path).unwrap()).unwrap();
    assert_eq!(plan["version"], "1.0");
    assert_eq!(plan["entries"][0]["name"], "demo");
    assert_eq!(
        plan["entries"][0]["expected_static_cost"]["triggers"], 36,
        "frozen plans must store the gate-inclusive assembled cost"
    );
    let planned_source = plan["entries"][0]["source"].as_str().unwrap();
    assert!(
        !Path::new(planned_source).is_absolute(),
        "plans should be portable relative to the plan file: {planned_source}"
    );

    let out_dir = dir.path().join("rebuilt");
    let second = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        out_dir.to_str().unwrap(),
    ]);
    assert_ok(&second);
    assert!(out_dir.join("demo.scx").is_file());

    let tampered = dir.path().join("tampered.scx");
    let mut bytes = fs::read(basemap()).unwrap();
    let last = bytes.len() - 1;
    bytes[last] ^= 0xff;
    fs::write(&tampered, bytes).unwrap();
    let mismatch = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--basemap",
        tampered.to_str().unwrap(),
        "--out-dir",
        out_dir.to_str().unwrap(),
    ]);
    assert!(!mismatch.status.success());
    assert!(
        stderr(&mismatch).contains("E-PLAN-FINGERPRINT"),
        "{}",
        stderr(&mismatch)
    );
}

#[test]
fn plan_cost_gate_fails_on_a_drifted_static_cost() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let plan_path = dir.path().join("plan.json");
    let out_path = dir.path().join("demo.scx");

    let first = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
    ]);
    assert_ok(&first);

    let mut plan: Value = serde_json::from_str(&fs::read_to_string(&plan_path).unwrap()).unwrap();
    // The old pre-assembly cost (1) must no longer pass the gate: the
    // contract is the gate-inclusive assembled payload (1 + 35).
    plan["entries"][0]["expected_static_cost"]["triggers"] = Value::from(1);
    fs::write(&plan_path, serde_json::to_string_pretty(&plan).unwrap()).unwrap();

    let out_dir = dir.path().join("checked");
    let gated = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        out_dir.to_str().unwrap(),
    ]);
    assert!(!gated.status.success());
    assert!(stderr(&gated).contains("E-COST-GATE"), "{}", stderr(&gated));
    assert!(
        !out_dir.join("demo.scx").is_file(),
        "a cost-gated build must not write a map"
    );

    let skipped = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        out_dir.to_str().unwrap(),
        "--no-cost-gate",
    ]);
    assert_ok(&skipped);
    assert!(out_dir.join("demo.scx").is_file());
}

#[test]
fn single_source_compile_accepts_cost_gate_flags_without_effect() {
    // Preserved quirk, pinned end to end: --no-cost-gate and
    // --check-cost are accepted in single-source compile, where the
    // plan gate does not exist, and change nothing about the build.
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    for flag in ["--no-cost-gate", "--check-cost"] {
        let out_path = dir.path().join(format!("{flag}.scx"));
        let out = eudkit(&[
            "compile",
            source.to_str().unwrap(),
            "--basemap",
            basemap().to_str().unwrap(),
            "--out",
            out_path.to_str().unwrap(),
            flag,
        ]);
        assert_ok(&out);
        assert!(
            out_path.is_file(),
            "{flag} must not break single-source compile"
        );
    }
}

#[test]
fn plan_mode_rejects_combined_source_and_single_source_flags() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let plan_path = dir.path().join("plan.json");
    let out_path = dir.path().join("demo.scx");
    let first = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
    ]);
    assert_ok(&first);

    let cases: &[(&[&str], &str)] = &[
        (
            &["compile", "demo.ts", "--plan", "plan.json"],
            "compile: use either a source file or --plan, not both",
        ),
        (
            &["compile", "--plan", "plan.json", "--out", "x.scx"],
            "compile: --out is not valid with --plan (use --out-dir)",
        ),
        (
            &["compile", "--plan", "plan.json", "--plan-out", "p.json"],
            "compile: --plan-out cannot be combined with --plan",
        ),
        (
            &["compile", "--plan", "plan.json", "--frontend", "ts"],
            "compile: --frontend is set per plan entry; omit it with --plan",
        ),
    ];
    for (args, message) in cases {
        // Rewrite the relative paths to the temp dir.
        let resolved: Vec<String> = args
            .iter()
            .map(|a| match *a {
                "demo.ts" => source.to_str().unwrap().to_string(),
                "plan.json" => plan_path.to_str().unwrap().to_string(),
                other => other.to_string(),
            })
            .collect();
        let refs: Vec<&str> = resolved.iter().map(|s| s.as_str()).collect();
        let out = eudkit(&refs);
        assert!(!out.status.success(), "{args:?} must be rejected");
        assert!(
            stderr(&out).contains(message),
            "{args:?}: expected '{message}' in stderr, got: {}",
            stderr(&out)
        );
    }
}

#[test]
fn unknown_frontend_and_missing_basemap_fail_loudly() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("demo.txt");
    fs::write(&source, "not a module").unwrap();
    let out = eudkit(&["dump-ir", source.to_str().unwrap()]);
    assert!(!out.status.success());
    assert!(stderr(&out).contains("E-FRONTEND"), "{}", stderr(&out));

    let ts = write_ts(dir.path(), "demo.ts", demo_body());
    let out = eudkit(&["compile", ts.to_str().unwrap()]);
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("missing --basemap"),
        "{}",
        stderr(&out)
    );
}

#[test]
fn missing_module_diagnostic_is_shared_by_every_entry_surface() {
    let dir = tempfile::tempdir().unwrap();
    let root = repo_root().display();

    let ts = dir.path().join("missing_everywhere.ts");
    fs::write(
        &ts,
        format!(
            "import {{ buildModule }} from \"{root}/ts/index.ts\";\n\
             const not_exported = buildModule(() => {{}});\n"
        ),
    )
    .unwrap();
    let py = dir.path().join("missing_everywhere.py");
    fs::write(
        &py,
        "from py import build_module\nnot_module = build_module(lambda: None)\n",
    )
    .unwrap();

    for (source, frontend, expected) in [
        (
            &ts,
            Frontend::Ts,
            &[
                "must export a compiled module named \"module\"",
                "buildModule(...)",
            ][..],
        ),
        (
            &py,
            Frontend::Py,
            &[
                "must assign a compiled module named \"module\"",
                "build_module(...)",
            ][..],
        ),
    ] {
        for surface in ENTRY_SURFACES {
            let out = run_entry_surface(surface, dir.path(), source, frontend);
            assert_entry_diagnostic(&out, source, expected, surface);
        }
    }
}

#[test]
fn wrong_module_value_is_described_on_every_entry_surface() {
    let dir = tempfile::tempdir().unwrap();

    let ts = dir.path().join("wrong_number.ts");
    fs::write(&ts, "export const module = 42;\n").unwrap();
    let py = dir.path().join("wrong_number.py");
    fs::write(&py, "module = 42\n").unwrap();

    for (source, frontend, expected) in [
        (
            &ts,
            Frontend::Ts,
            &[
                "exports \"module\" but it is number (42)",
                "expected a compiled module from buildModule(...)",
            ][..],
        ),
        (
            &py,
            Frontend::Py,
            &[
                "assigns \"module\" but it is int (42)",
                "expected a compiled module from build_module(...)",
            ][..],
        ),
    ] {
        for surface in ENTRY_SURFACES {
            let out = run_entry_surface(surface, dir.path(), source, frontend);
            assert_entry_diagnostic(&out, source, expected, surface);
        }
    }
}

#[test]
fn wrong_module_value_reports_each_documented_shape() {
    let dir = tempfile::tempdir().unwrap();

    let cases: Vec<(&str, String, &[&str])> = vec![
        (
            "wrong_function.ts",
            "export function module() {}\n".to_string(),
            &["exports \"module\" but it is function"][..],
        ),
        (
            "wrong_object.ts",
            "export const module = { answer: 42 };\n".to_string(),
            &["exports \"module\" but it is object without toJson()"][..],
        ),
        (
            "wrong_null.ts",
            "export const module = null;\n".to_string(),
            &["exports \"module\" but it is null"][..],
        ),
        (
            "wrong_undefined.ts",
            "export const module = undefined;\n".to_string(),
            &["exports \"module\" but it is undefined"][..],
        ),
        (
            "wrong_string.ts",
            "export const module = \"hello\";\n".to_string(),
            &["exports \"module\" but it is string (\"hello\")"][..],
        ),
        (
            "wrong_boolean.ts",
            "export const module = false;\n".to_string(),
            &["exports \"module\" but it is boolean (false)"][..],
        ),
        (
            "wrong_function.py",
            "def module():\n    pass\n".to_string(),
            &["assigns \"module\" but it is function"][..],
        ),
        (
            "wrong_object.py",
            "module = {\"answer\": 42}\n".to_string(),
            &["assigns \"module\" but it is dict without to_json()"][..],
        ),
        (
            "wrong_none.py",
            "module = None\n".to_string(),
            &["assigns \"module\" but it is None"][..],
        ),
        (
            "wrong_bool.py",
            "module = True\n".to_string(),
            &["assigns \"module\" but it is bool (True)"][..],
        ),
        (
            "wrong_float.py",
            "module = 1.5\n".to_string(),
            &["assigns \"module\" but it is float (1.5)"][..],
        ),
        (
            "wrong_string.py",
            "module = \"hello\"\n".to_string(),
            &["assigns \"module\" but it is str ('hello')"][..],
        ),
        (
            "wrong_list.py",
            "module = [1, 2]\n".to_string(),
            &["assigns \"module\" but it is list without to_json()"][..],
        ),
        (
            "wrong_tuple.py",
            "module = (1, 2)\n".to_string(),
            &["assigns \"module\" but it is tuple without to_json()"][..],
        ),
        (
            "wrong_callable.py",
            "class NotAModule:\n    pass\nmodule = NotAModule\n".to_string(),
            &["assigns \"module\" but it is type callable"][..],
        ),
    ];

    for (name, body, expected) in cases {
        let source = dir.path().join(name);
        fs::write(&source, body).unwrap();
        let out = eudkit(&["synth", source.to_str().unwrap()]);
        assert_entry_diagnostic(&out, &source, expected, "synth");
    }
}

#[test]
fn callable_entry_with_to_json_is_rejected_on_every_entry_surface() {
    let dir = tempfile::tempdir().unwrap();

    let ts = dir.path().join("wrong_function_with_to_json.ts");
    fs::write(
        &ts,
        "export function module() {}\nmodule.toJson = () => \"{}\";\n",
    )
    .unwrap();

    let py_function = dir.path().join("wrong_function_with_to_json.py");
    fs::write(
        &py_function,
        "def module():\n    pass\nmodule.to_json = lambda: \"{}\"\n",
    )
    .unwrap();

    let py_callable = dir.path().join("wrong_callable_with_to_json.py");
    fs::write(
        &py_callable,
        concat!(
            "class CallableModule:\n",
            "    def __call__(self):\n",
            "        pass\n",
            "    def to_json(self):\n",
            "        return \"{}\"\n",
            "module = CallableModule()\n",
        ),
    )
    .unwrap();

    for (source, frontend, expected) in [
        (
            &ts,
            Frontend::Ts,
            &[
                "exports \"module\" but it is function",
                "expected a compiled module from buildModule(...)",
            ][..],
        ),
        (
            &py_function,
            Frontend::Py,
            &[
                "assigns \"module\" but it is function",
                "expected a compiled module from build_module(...)",
            ][..],
        ),
        (
            &py_callable,
            Frontend::Py,
            &[
                "assigns \"module\" but it is CallableModule callable",
                "expected a compiled module from build_module(...)",
            ][..],
        ),
    ] {
        for surface in ENTRY_SURFACES {
            let out = run_entry_surface(surface, dir.path(), source, frontend);
            assert_entry_diagnostic(&out, source, expected, surface);
        }
    }
}

#[test]
fn helper_module_exports_do_not_satisfy_the_root_entry() {
    let dir = tempfile::tempdir().unwrap();

    // The helpers build a different module from the roots, so an accidentally
    // captured helper would change the HIR and fail the equality assertions.
    let ts_helper_body = "    const helperVar = Var(0);\n    \
                          when(helperVar.eq(0), () => { helperVar.set(99); });";
    write_ts(dir.path(), "ts_entry_helper.ts", ts_helper_body);
    let ts_missing = dir.path().join("ts_helper_only.ts");
    fs::write(&ts_missing, "import \"./ts_entry_helper.ts\";\n").unwrap();
    let ts_with_root_module = write_ts_with_preamble(
        dir.path(),
        "ts_helper_and_root.ts",
        "import \"./ts_entry_helper.ts\";\n",
        demo_body(),
    );
    let ts_root_only = write_ts(dir.path(), "ts_root_only.ts", demo_body());

    let py_helper_body = "    helper_var = Var(0)\n    \
                          when(helper_var.eq(0), lambda: helper_var.set(99))";
    write_py(dir.path(), "py_entry_helper.py", py_helper_body);
    let py_missing = dir.path().join("py_helper_only.py");
    fs::write(
        &py_missing,
        "import os, sys\n\
         sys.path.insert(0, os.path.dirname(__file__))\n\
         import py_entry_helper\n",
    )
    .unwrap();
    let py_with_root_module = write_py_with_preamble(
        dir.path(),
        "py_helper_and_root.py",
        "import os, sys\n\
         sys.path.insert(0, os.path.dirname(__file__))\n\
         import py_entry_helper\n",
        demo_py_body(),
    );
    let py_root_only = write_py(dir.path(), "py_root_only.py", demo_py_body());

    let out = eudkit(&["synth", ts_missing.to_str().unwrap()]);
    assert_entry_diagnostic(
        &out,
        &ts_missing,
        &["must export a compiled module named \"module\""],
        "synth",
    );
    let out = eudkit(&["synth", py_missing.to_str().unwrap()]);
    assert_entry_diagnostic(
        &out,
        &py_missing,
        &["must assign a compiled module named \"module\""],
        "synth",
    );

    let out = eudkit(&["synth", ts_with_root_module.to_str().unwrap()]);
    assert_ok(&out);
    let with_helper = stdout(&out);
    let out = eudkit(&["synth", ts_root_only.to_str().unwrap()]);
    assert_ok(&out);
    assert_eq!(
        with_helper,
        stdout(&out),
        "TS must compile only the root's module export, not a helper's"
    );

    let out = eudkit(&["synth", py_with_root_module.to_str().unwrap()]);
    assert_ok(&out);
    let with_helper = stdout(&out);
    let out = eudkit(&["synth", py_root_only.to_str().unwrap()]);
    assert_ok(&out);
    assert_eq!(
        with_helper,
        stdout(&out),
        "Python must compile only the root's module export, not a helper's"
    );
}

#[test]
fn explicit_reexports_are_root_entries_but_helper_imports_stay_isolated() {
    let dir = tempfile::tempdir().unwrap();
    let ts_root_only = write_ts(dir.path(), "reexport_root_only.ts", demo_body());
    let py_root_only = write_py(dir.path(), "reexport_root_only.py", demo_py_body());

    let out = eudkit(&["synth", ts_root_only.to_str().unwrap()]);
    assert_ok(&out);
    let ts_root_hir = stdout(&out);
    let out = eudkit(&["synth", py_root_only.to_str().unwrap()]);
    assert_ok(&out);
    let py_root_hir = stdout(&out);

    // The helper modules build the same module as the root-only baselines.
    write_ts(dir.path(), "reexport_helper.ts", demo_body());
    write_py(dir.path(), "reexport_helper.py", demo_py_body());

    // TypeScript: both explicit re-export forms are root entries.
    for (name, body) in [
        (
            "ts_export_star.ts",
            "export * from \"./reexport_helper.ts\";\n",
        ),
        (
            "ts_export_named.ts",
            "export { module } from \"./reexport_helper.ts\";\n",
        ),
    ] {
        let source = dir.path().join(name);
        fs::write(&source, body).unwrap();
        let out = eudkit(&["synth", source.to_str().unwrap()]);
        assert_ok(&out);
        assert_eq!(
            stdout(&out),
            ts_root_hir,
            "{name}: an explicit root re-export must compile the helper's module"
        );
    }

    // TypeScript: a local import is not a root export, even when named
    // `module`; a side-effect import leaves the helper isolated too.
    for (name, body) in [
        (
            "ts_local_import.ts",
            "import { module } from \"./reexport_helper.ts\";\n",
        ),
        ("ts_plain_import.ts", "import \"./reexport_helper.ts\";\n"),
    ] {
        let source = dir.path().join(name);
        fs::write(&source, body).unwrap();
        let out = eudkit(&["synth", source.to_str().unwrap()]);
        assert_entry_diagnostic(
            &out,
            &source,
            &["must export a compiled module named \"module\""],
            name,
        );
    }

    // Python: module scope has no export/import distinction, so both explicit
    // assignments are root entries.
    for (name, body) in [
        (
            "py_from_import.py",
            "import os, sys\n\
             sys.path.insert(0, os.path.dirname(__file__))\n\
             from reexport_helper import module\n",
        ),
        (
            "py_assign_import.py",
            "import os, sys\n\
             sys.path.insert(0, os.path.dirname(__file__))\n\
             import reexport_helper\n\
             module = reexport_helper.module\n",
        ),
    ] {
        let source = dir.path().join(name);
        fs::write(&source, body).unwrap();
        let out = eudkit(&["synth", source.to_str().unwrap()]);
        assert_ok(&out);
        assert_eq!(
            stdout(&out),
            py_root_hir,
            "{name}: an explicit module-scope assignment must be the root entry"
        );
    }

    // Python: a plain `import helper` leaves the helper isolated.
    let py_plain_import = dir.path().join("py_plain_import.py");
    fs::write(
        &py_plain_import,
        "import os, sys\n\
         sys.path.insert(0, os.path.dirname(__file__))\n\
         import reexport_helper\n",
    )
    .unwrap();
    let out = eudkit(&["synth", py_plain_import.to_str().unwrap()]);
    assert_entry_diagnostic(
        &out,
        &py_plain_import,
        &["must assign a compiled module named \"module\""],
        "py_plain_import",
    );
}

#[test]
fn explicit_module_export_works_for_synth_and_compile() {
    let dir = tempfile::tempdir().unwrap();
    let ts = write_ts(dir.path(), "demo.ts", demo_body());
    let py = write_py(dir.path(), "demo.py", demo_py_body());

    for source in [&ts, &py] {
        let synth = eudkit(&["synth", source.to_str().unwrap()]);
        assert_ok(&synth);
        let hir: Value = serde_json::from_str(&stdout(&synth)).unwrap();
        assert_eq!(hir["version"], "3.2");

        let dumped = eudkit(&[
            "dump-ir",
            source.to_str().unwrap(),
            "--json",
            "--loops",
            "1",
        ]);
        assert_ok(&dumped);
        let report: Value = serde_json::from_str(&stdout(&dumped)).unwrap();
        assert_eq!(report["pre_assembly"], true);

        let out_path = dir
            .path()
            .join(source.with_extension("scx").file_name().unwrap());
        let compile = eudkit(&[
            "compile",
            source.to_str().unwrap(),
            "--basemap",
            basemap().to_str().unwrap(),
            "--out",
            out_path.to_str().unwrap(),
        ]);
        assert_ok(&compile);
        assert!(out_path.is_file());
    }
}

#[test]
fn stock_catalog_smoke_map_is_identical_across_frontends() {
    let smoke = repo_root().join("tests/outputmap/smoke");
    let silent = repo_root().join("tests/outputmap/basemap/basemap_silent.scx");
    let ts = smoke.join("stock-catalog.ts");
    let py = smoke.join("stock-catalog.py");

    let ts_hir = eudkit(&["synth", ts.to_str().unwrap()]);
    assert_ok(&ts_hir);
    let py_hir = eudkit(&["synth", py.to_str().unwrap()]);
    assert_ok(&py_hir);
    assert_eq!(
        stdout(&ts_hir),
        stdout(&py_hir),
        "stock smoke sources must emit identical HIR"
    );

    // `MoveLocation`/`CenterView` defer the rest of the trigger's actions to
    // the next cycle, so the setup marker must stay before them or the chat
    // log shows `STOCK BRING` first even though setup runs first.
    let hir: Value = serde_json::from_str(&stdout(&ts_hir)).unwrap();
    let setup = &hir["module"]["stmts"][0];
    assert_eq!(setup["kind"].as_str(), Some("once"));
    let actions = setup["body"].as_array().expect("setup actions");
    assert_eq!(actions.len(), 4, "setup trigger shape changed");
    assert_eq!(actions[0]["acttype"].as_u64(), Some(9), "DisplayText");
    assert_eq!(actions[0]["text"].as_str(), Some("STOCK SETUP"));
    assert_eq!(actions[1]["acttype"].as_u64(), Some(44), "CreateUnit");
    assert_eq!(actions[2]["acttype"].as_u64(), Some(38), "MoveLocation");
    assert_eq!(actions[3]["acttype"].as_u64(), Some(10), "CenterView");

    let dir = tempfile::tempdir().unwrap();
    let mut built = Vec::new();
    for source in [&ts, &py] {
        let out_path = dir
            .path()
            .join(source.with_extension("scx").file_name().unwrap());
        let compile = eudkit(&[
            "compile",
            source.to_str().unwrap(),
            "--basemap",
            silent.to_str().unwrap(),
            "--out",
            out_path.to_str().unwrap(),
        ]);
        assert_ok(&compile);
        built.push(fs::read(out_path).unwrap());
    }
    assert_eq!(
        built[0], built[1],
        "stock smoke maps must be byte-identical"
    );
}

#[test]
fn string_format_benchmark_two_variable_hex_regression_adr_0011() {
    let dir = tempfile::tempdir().unwrap();
    let root = repo_root().display();

    // Two-variable fixed-width hex HUD counter:
    // Fixed-width hex placeholders fold directly at their compile-time byte
    // lanes without mid-string alignment padding. Constant-offset and dword
    // folding (ADR 0011) slashes static cost from ~1,670T to 83T static.
    let ts_source_content = format!(
        "import {{ FixedString, Var, buildModule }} from \"{root}/ts/index.ts\";\n\
         export const module = buildModule(() => {{\n\
             const hud = new FixedString(32);\n\
             const wave = Var(1);\n\
             const hp = Var(100);\n\
             hud.write(\"wave={{:x}} hp={{:x}}\", wave, hp);\n\
         }});\n"
    );
    let ts_path = dir.path().join("hud.ts");
    fs::write(&ts_path, &ts_source_content).unwrap();

    let py_source_content = "\
from py import FixedString, Var, build_module
def prog():
    hud = FixedString(32)
    wave = Var(1)
    hp = Var(100)
    hud.write(\"wave={:x} hp={:x}\", wave, hp)
module = build_module(prog)
";
    let py_path = dir.path().join("hud.py");
    fs::write(&py_path, py_source_content).unwrap();

    // 1. Synthesize HIR and assert TypeScript and Python emit byte-identical IR.
    let ts_synth = eudkit(&["synth", ts_path.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_synth);
    let py_synth = eudkit(&["synth", py_path.to_str().unwrap(), "--compact"]);
    assert_ok(&py_synth);
    assert_eq!(
        stdout(&ts_synth),
        stdout(&py_synth),
        "TS and Python string formatting must synthesize byte-identical HIR"
    );

    // 2. Pre-assembly dump-ir report verification:
    // Confirms exact static trigger count: 83T static (80 conditions, 171 actions).
    // Each 32-bit hex variable accumulates directly into the target string dwords
    // using 32 DeathsX bit-test triggers + 8 nibble adjustment triggers
    // (40T per variable = 80T); literal and terminator writes make up the
    // remaining 3T. The ~1,670T Context number is a different `printAt`
    // program, so this 83T is not a percentage reduction from it.
    let ts_dump = eudkit(&[
        "dump-ir",
        ts_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&ts_dump);
    let ts_report: Value = serde_json::from_str(&stdout(&ts_dump)).unwrap();

    let py_dump = eudkit(&[
        "dump-ir",
        py_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&py_dump);
    let py_report: Value = serde_json::from_str(&stdout(&py_dump)).unwrap();

    assert_eq!(
        ts_report, py_report,
        "dump-ir reports must match across frontends"
    );
    let static_triggers = ts_report["static_cost"]["triggers"]
        .as_u64()
        .expect("static trigger count");
    assert!(
        static_triggers < 100,
        "ADR 0011 target: two-variable static cost must stay below 100T, got {static_triggers}T"
    );
    assert_eq!(
        static_triggers, 83,
        "exact static trigger count must be confirmed at 83T for the write benchmark (ADR 0011 Consequences)"
    );
    assert_eq!(ts_report["static_cost"]["conditions"], 80);
    assert_eq!(ts_report["static_cost"]["actions"], 171);
    assert_eq!(ts_report["pre_assembly"], true);
    let executed_triggers = ts_report["executed_cost"]["triggers"]
        .as_u64()
        .expect("executed trigger count");
    assert!(
        executed_triggers < 100,
        "ADR 0011 target: two-variable executed cost must stay below 100T (~90T), got {executed_triggers}T"
    );
    assert_eq!(
        executed_triggers, 83,
        "executed cost dropped with the static path"
    );
    assert_eq!(ts_report["executed_cost"]["conditions"], 80);
    assert_eq!(ts_report["executed_cost"]["actions"], 35);

    // 3. Post-assembly compile (with standard 35-trigger tick gate): 83 + 35 = 118T static.
    let out_scx = dir.path().join("hud.scx");
    let compile_out = eudkit(&[
        "compile",
        ts_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_scx.to_str().unwrap(),
    ]);
    assert_ok(&compile_out);
    assert!(
        stdout(&compile_out).contains("118T static"),
        "compiled payload with tick gate must report 118T static: {}",
        stdout(&compile_out)
    );
    assert!(out_scx.is_file());

    // 4. Post-assembly compile with --no-tick-gate: exact 83T static.
    let no_gate_scx = dir.path().join("hud_no_gate.scx");
    let no_gate_compile = eudkit(&[
        "compile",
        ts_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        no_gate_scx.to_str().unwrap(),
        "--no-tick-gate",
    ]);
    assert_ok(&no_gate_compile);
    assert!(
        stdout(&no_gate_compile).contains("83T static"),
        "compiled payload without tick gate must match pre-assembly 83T static: {}",
        stdout(&no_gate_compile)
    );
    assert!(no_gate_scx.is_file());

    // 5. Post-assembly compile with --single-player for Python: exact 83T static.
    let sp_scx = dir.path().join("hud_sp.scx");
    let sp_compile = eudkit(&[
        "compile",
        py_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        sp_scx.to_str().unwrap(),
        "--single-player",
    ]);
    assert_ok(&sp_compile);
    assert!(
        stdout(&sp_compile).contains("83T static"),
        "single-player compiled payload must report 83T static: {}",
        stdout(&sp_compile)
    );
    assert!(sp_scx.is_file());

    // 6. In-memory execution simulation: verify assembled costs and the
    // formatted string contents. The gated assembled cost is 83 + 35 = 118T.
    let prepared =
        eudkit::prepare_source(&ts_path, Some(eudkit::frontend::Frontend::Ts), None).unwrap();
    assert_eq!(
        prepared
            .assembled_static_cost_with_options(AssemblyOptions::default())
            .triggers,
        118
    );
    let cost = prepared.assembled_static_cost_with_options(AssemblyOptions {
        enable_tick_gate: false,
    });
    assert_eq!(cost.triggers, 83);
    let mut sim = eudkit_lir::init_simulation(&prepared.lir).unwrap();
    sim.step_loop();
    assert_eq!(sim.last_executed_cost.triggers, 83);

    // Read the string buffer dwords from external_epds.
    let epd_base = (0x2000_0000u32.wrapping_sub(0x58A364)) / 4;
    let mut rendered_bytes = Vec::new();
    for d in 0..7 {
        let val = sim.external_epds.get(&(epd_base + d)).copied().unwrap_or(0);
        rendered_bytes.extend_from_slice(&val.to_le_bytes());
    }
    let rendered = String::from_utf8_lossy(&rendered_bytes);
    assert!(
        rendered.starts_with("wave=00000001 hp=00000064"),
        "simulated string write must render expected ASCII characters: {rendered:?}"
    );
}

#[test]
fn string_format_benchmark_two_variable_decimal_regression_adr_0014() {
    let dir = tempfile::tempdir().unwrap();
    let smoke = repo_root().join("tests/outputmap/smoke");
    let ts_path = smoke.join("sample.ts");
    let py_path = smoke.join("sample.py");

    // Two-variable fixed-width decimal HUD sample (ADR 0014, issue #81).
    // The committed tests/outputmap/smoke/sample.{ts,py} sources are the
    // benchmark fixture, so this test also keeps the sample compilable.
    //
    // Fixed-width format "{:\x1a>4d}" uses the direct 4-test binary digit
    // extraction engine ($8, 4, 2, 1 \times 10^k$) with compile-time byte lane
    // pre-shifting and constant-offset folding.
    //
    // Re-baselined by issue #98 (2026-11-07): the two-variable sample measures
    // 100T static / 105T executed pre-assembly. Issue #89's 146T / 175T
    // baseline included per-digit scratch-trigger pointer patching; issue #98
    // writes each digit byte straight into its compile-time string dword with
    // a masked SetStringDword action, so the fixed-width path has no varstores,
    // no runtime pos.shr(2), and no dynamic 'when' lane dispatch.

    // 1. Synthesize HIR and assert TypeScript and Python emit byte-identical IR.
    let ts_synth = eudkit(&["synth", ts_path.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_synth);
    let py_synth = eudkit(&["synth", py_path.to_str().unwrap(), "--compact"]);
    assert_ok(&py_synth);
    assert_eq!(
        stdout(&ts_synth),
        stdout(&py_synth),
        "TS and Python fixed-width decimal string formatting must synthesize byte-identical HIR"
    );

    // Verify constant-offset folding in synthesized HIR:
    // Zero dynamic 'when' statements and zero pos.shr(2) shift ladders are emitted.
    let hir: Value = serde_json::from_str(&stdout(&ts_synth)).unwrap();
    let stmts = hir["module"]["stmts"].as_array().unwrap();
    assert!(
        !stmts.iter().any(|s| s["kind"] == "when"),
        "constant-offset fixed decimal formatting must avoid dynamic when blocks"
    );
    let varstores: Vec<_> = stmts.iter().filter(|s| s["kind"] == "varstore").collect();
    assert_eq!(
        varstores.len(),
        0,
        "fixed-width decimal digit writes must not patch scratch-trigger fields"
    );

    // 2. Pre-assembly dump-ir report verification:
    // Pinned re-baseline from issue #98: 100T static (88 conditions, 220
    // actions) and 105T executed (87 conditions, 435 actions). Exact equality
    // makes any regression fail instead of hiding under a loose bound.
    let ts_dump = eudkit(&[
        "dump-ir",
        ts_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&ts_dump);
    let ts_report: Value = serde_json::from_str(&stdout(&ts_dump)).unwrap();

    let py_dump = eudkit(&[
        "dump-ir",
        py_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&py_dump);
    let py_report: Value = serde_json::from_str(&stdout(&py_dump)).unwrap();

    assert_eq!(
        ts_report, py_report,
        "dump-ir reports must match across frontends"
    );
    let static_triggers = ts_report["static_cost"]["triggers"].as_u64().unwrap();
    assert_eq!(
        static_triggers, 100,
        "two-variable fixed-width decimal static cost must stay at the re-baselined 100T (ADR 0014, #98)"
    );
    assert_eq!(ts_report["static_cost"]["conditions"], 88);
    assert_eq!(ts_report["static_cost"]["actions"], 220);
    assert_eq!(ts_report["pre_assembly"], true);
    let executed_triggers = ts_report["executed_cost"]["triggers"].as_u64().unwrap();
    assert_eq!(
        executed_triggers, 105,
        "two-variable fixed-width decimal executed cost must stay at the re-baselined 105T (ADR 0014, #98)"
    );
    assert_eq!(ts_report["executed_cost"]["conditions"], 87);
    assert_eq!(ts_report["executed_cost"]["actions"], 435);

    // 3. Post-assembly compile (with standard 35-trigger tick gate): 100 + 35 = 135T static.
    let out_scx = dir.path().join("hud.scx");
    let compile_out = eudkit(&[
        "compile",
        ts_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_scx.to_str().unwrap(),
    ]);
    assert_ok(&compile_out);
    assert!(
        stdout(&compile_out).contains("135T static"),
        "compiled payload with tick gate must report 135T static: {}",
        stdout(&compile_out)
    );
    assert!(out_scx.is_file());

    // 4. Post-assembly compile with --no-tick-gate: exact 100T static.
    let no_gate_scx = dir.path().join("hud_no_gate.scx");
    let no_gate_compile = eudkit(&[
        "compile",
        ts_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        no_gate_scx.to_str().unwrap(),
        "--no-tick-gate",
    ]);
    assert_ok(&no_gate_compile);
    assert!(
        stdout(&no_gate_compile).contains("100T static"),
        "compiled payload without tick gate must match pre-assembly 100T static: {}",
        stdout(&no_gate_compile)
    );
    assert!(no_gate_scx.is_file());

    // 5. Post-assembly compile with --single-player for Python: exact 100T static.
    let sp_scx = dir.path().join("hud_sp.scx");
    let sp_compile = eudkit(&[
        "compile",
        py_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        sp_scx.to_str().unwrap(),
        "--single-player",
    ]);
    assert_ok(&sp_compile);
    assert!(
        stdout(&sp_compile).contains("100T static"),
        "single-player compiled payload must report 100T static: {}",
        stdout(&sp_compile)
    );
    assert!(sp_scx.is_file());

    // 6. In-memory execution simulation: verify the formatted string contents.
    let prepared =
        eudkit::prepare_source(&ts_path, Some(eudkit::frontend::Frontend::Ts), None).unwrap();
    let cost = prepared.assembled_static_cost_with_options(AssemblyOptions {
        enable_tick_gate: false,
    });
    assert_eq!(cost.triggers, 100);
    let mut sim = eudkit_lir::init_simulation(&prepared.lir).unwrap();
    sim.step_loop();
    assert_eq!(sim.last_executed_cost.triggers, 105);
    assert_eq!(sim.last_executed_cost.actions, 435);

    // Read the string buffer dwords from external_epds.
    let epd_base = (0x2000_0000u32.wrapping_sub(0x58A364)) / 4;
    let mut rendered_bytes = Vec::new();
    for d in 0..7 {
        let val = sim.external_epds.get(&(epd_base + d)).copied().unwrap_or(0);
        rendered_bytes.extend_from_slice(&val.to_le_bytes());
    }
    let rendered = String::from_utf8_lossy(&rendered_bytes);
    assert!(
        rendered.starts_with("wave=\x1a\x1a\x1a5 hp=\x1a100"),
        "simulated string write must render expected ASCII characters: {rendered:?}"
    );
}

#[test]
fn string_format_benchmark_bare_decimal_regression_adr_0014() {
    let dir = tempfile::tempdir().unwrap();
    let root = repo_root().display();

    // Bare decimal format "{}" (ADR 0014):
    // Uses direct 4-test binary extraction across all 10 powers of 10 ($10^9 \dots 10^0$).
    // Leading zero suppression runs dynamically and appends digits through the
    // CurrentPlayer-cursor byte-lane writer (issue #87, ADR 0004).
    //
    // Re-baselined by issue #87 (2026-11-11): 688T static (492 conditions,
    // 841 actions) and 105T executed (90 conditions, 238 actions) per variable
    // at 2 digits. The previous #89 pin was 2421T/279T: the writer used to
    // derive `pos & 3` and `pos >> 2` per digit and patch a scratch trigger's
    // dest/value fields with emitVarStore. The CP-cursor writer seeds
    // CurrentPlayer once per run with `base + (pos >> 2)`, selects the lane
    // with one masked equality, writes constant bytes plus constant bit adds,
    // advances the cursor on lane wraps, and restores curpl to 0.
    // Static cost is value-independent (the same 687T covers 1, 2 and 10
    // digits); the executed figure moves with the value (94T/103T/193T).
    let ts_source_content = format!(
        "import {{ FixedString, Var, buildModule }} from \"{root}/ts/index.ts\";\n\
         export const module = buildModule(() => {{\n\
             const hud = new FixedString(32);\n\
             const wave = Var(42);\n\
             hud.write(\"{{}}\", wave);\n\
         }});\n"
    );
    let ts_path = dir.path().join("bare.ts");
    fs::write(&ts_path, &ts_source_content).unwrap();

    let py_source_content = "\
from py import FixedString, Var, build_module
def prog():
    hud = FixedString(32)
    wave = Var(42)
    hud.write(\"{}\", wave)
module = build_module(prog)
";
    let py_path = dir.path().join("bare.py");
    fs::write(&py_path, py_source_content).unwrap();

    // 1. Synthesize HIR and assert TypeScript and Python emit byte-identical IR.
    let ts_synth = eudkit(&["synth", ts_path.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_synth);
    let py_synth = eudkit(&["synth", py_path.to_str().unwrap(), "--compact"]);
    assert_ok(&py_synth);
    assert_eq!(
        stdout(&ts_synth),
        stdout(&py_synth),
        "TS and Python bare decimal formatting must synthesize byte-identical HIR"
    );

    // 2. Pre-assembly dump-ir report verification. This is the cost truth: the
    // old ~50–60T target was a HIR node estimate, not the measured trigger cost.
    // Exact equality pins the #89 re-baseline and fails on any regression.
    let ts_dump = eudkit(&[
        "dump-ir",
        ts_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&ts_dump);
    let ts_report: Value = serde_json::from_str(&stdout(&ts_dump)).unwrap();

    let py_dump = eudkit(&[
        "dump-ir",
        py_path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert_ok(&py_dump);
    let py_report: Value = serde_json::from_str(&stdout(&py_dump)).unwrap();

    assert_eq!(
        ts_report, py_report,
        "dump-ir reports must match across frontends"
    );
    let static_triggers = ts_report["static_cost"]["triggers"].as_u64().unwrap();
    // #97 rework: the variable-width decimal kernel is one func body per
    // module, but a body with a single call site shares nothing, so opt
    // splices it back in (substituting the call's arguments for the callee's
    // parameters instead of binding them). A lone bare `{}` therefore costs
    // exactly the inlined baseline again: 687T/492C/840A static and
    // 103T/90C/173A executed. K sites still cost O(K + M): each extra site
    // keeps its call and pays ~10T static plus the shared body's
    // `TriggerPage` data movement at runtime.
    assert_eq!(
        static_triggers, 687,
        "bare decimal static cost must stay at the inlined baseline 687T (ADR 0014, #87; \
         #97 inlines a single-site func body; #96 folds one writer act \
         into the batch setup trigger)"
    );
    assert_eq!(ts_report["static_cost"]["conditions"], 492);
    assert_eq!(
        ts_report["static_cost"]["actions"], 840,
        "#97 inlines a single-site func body; #96 folds one writer act into the batch setup trigger"
    );
    assert_eq!(ts_report["pre_assembly"], true);
    let executed_triggers = ts_report["executed_cost"]["triggers"].as_u64().unwrap();
    assert_eq!(
        executed_triggers, 103,
        "bare decimal executed cost must stay at the inlined baseline 103T (ADR 0014, #87; \
         #97 inlines a single-site func body; #96 shares one firing per store run)"
    );
    assert_eq!(ts_report["executed_cost"]["conditions"], 90);
    assert_eq!(
        ts_report["executed_cost"]["actions"], 173,
        "#97 inlines a single-site func body; #96 folds the writer's per-store actions into the shared firing"
    );

    // 3. Verify dynamic zero-suppression structure remains in HIR. The
    // extraction test count itself is intentionally not asserted here; the
    // dump-ir static cost above is the benchmark.
    let hir: Value = serde_json::from_str(&stdout(&ts_synth)).unwrap();
    let stmts = hir["module"]["stmts"].as_array().unwrap();
    let whens: Vec<_> = stmts.iter().filter(|s| s["kind"] == "when").collect();
    assert!(
        !whens.is_empty(),
        "bare decimal must use dynamic when blocks for variable-width zero suppression"
    );

    // 4. In-memory execution simulation: verify bare decimal renders without leading zeros.
    let prepared =
        eudkit::prepare_source(&ts_path, Some(eudkit::frontend::Frontend::Ts), None).unwrap();
    let mut sim = eudkit_lir::init_simulation(&prepared.lir).unwrap();
    sim.step_loop();

    let epd_base = (0x2000_0000u32.wrapping_sub(0x58A364)) / 4;
    let mut rendered_bytes = Vec::new();
    for d in 0..4 {
        let val = sim.external_epds.get(&(epd_base + d)).copied().unwrap_or(0);
        rendered_bytes.extend_from_slice(&val.to_le_bytes());
    }
    let rendered = String::from_utf8_lossy(&rendered_bytes);
    assert!(
        rendered.starts_with("42\0"),
        "simulated bare decimal write must render '42': {rendered:?}"
    );
}

fn write_warning_ts(dir: &Path, name: &str, writes: usize) -> PathBuf {
    let root = repo_root().display();
    let preamble = format!("import {{ FixedString }} from \"{root}/ts/index.ts\";\n");
    let mut body = String::from(
        "    const s = new FixedString(64);\n    const a = Var(1);\n    const b = Var(2);\n",
    );
    for _ in 0..writes {
        body.push_str("    s.write(\"A={} B={}\", a, b);\n");
    }
    write_ts_with_preamble(dir, name, &preamble, &body)
}

fn write_warning_py(dir: &Path, name: &str, writes: usize) -> PathBuf {
    let preamble = "from py import FixedString\n";
    let mut body = String::from("    s = FixedString(64)\n    a = Var(1)\n    b = Var(2)\n");
    for _ in 0..writes {
        body.push_str("    s.write(\"A={} B={}\", a, b)\n");
    }
    write_py_with_preamble(dir, name, preamble, &body)
}

#[test]
fn frontend_warnings_surface_on_synth_and_keep_stdout_pure() {
    let dir = tempfile::tempdir().unwrap();
    let ts = write_warning_ts(dir.path(), "warn.ts", 1);
    let py = write_warning_py(dir.path(), "warn.py", 1);

    let ts_out = eudkit(&["synth", ts.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_out);
    let py_out = eudkit(&["synth", py.to_str().unwrap(), "--compact"]);
    assert_ok(&py_out);

    let ts_warnings = stderr(&ts_out);
    let py_warnings = stderr(&py_out);
    assert_eq!(
        ts_warnings, py_warnings,
        "TS and Python must surface identical warning lines"
    );
    assert_eq!(
        ts_warnings
            .lines()
            .filter(|line| line.starts_with("warning: EUDSL-WARN-PERF:"))
            .count(),
        1,
        "expected exactly one PERF warning, got:\n{ts_warnings}"
    );
    assert!(
        !ts_warnings.contains("__EUDKIT_WARN__"),
        "internal marker must not leak: {ts_warnings}"
    );

    let ts_hir: Value = serde_json::from_str(&stdout(&ts_out)).unwrap();
    let py_hir: Value = serde_json::from_str(&stdout(&py_out)).unwrap();
    assert_eq!(ts_hir["version"], "3.2");
    assert_eq!(ts_hir["module"], py_hir["module"]);

    let quiet = eudkit(&["synth", ts.to_str().unwrap(), "--compact", "--no-warnings"]);
    assert_ok(&quiet);
    assert!(
        stderr(&quiet).is_empty(),
        "--no-warnings must suppress frontend warnings, got:\n{}",
        stderr(&quiet)
    );
}

#[test]
fn scope_warnings_surface_for_both_frontends() {
    let dir = tempfile::tempdir().unwrap();
    let root = repo_root().display();
    let ts_preamble = format!("import {{ lazyVar }} from \"{root}/ts/index.ts\";\n");
    let ts = write_ts_with_preamble(dir.path(), "scope.ts", &ts_preamble, "    lazyVar(0);");

    let py_preamble = "from py import lazyVar\n";
    let py = write_py_with_preamble(dir.path(), "scope.py", py_preamble, "    lazyVar(0)");

    let ts_out = eudkit(&["synth", ts.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_out);
    let py_out = eudkit(&["synth", py.to_str().unwrap(), "--compact"]);
    assert_ok(&py_out);

    assert_eq!(
        stderr(&ts_out),
        stderr(&py_out),
        "TS and Python must surface identical EUDSL-WARN-SCOPE lines"
    );
    assert_eq!(
        stderr(&ts_out)
            .lines()
            .filter(|line| line.starts_with("warning: EUDSL-WARN-SCOPE:"))
            .count(),
        1,
        "expected exactly one SCOPE warning, got:\n{}",
        stderr(&ts_out)
    );
    assert!(
        !stderr(&ts_out).contains("__EUDKIT_WARN__"),
        "internal marker must not leak: {}",
        stderr(&ts_out)
    );
}

#[test]
fn duplicate_frontend_warnings_are_deduped() {
    let dir = tempfile::tempdir().unwrap();
    let ts = write_warning_ts(dir.path(), "warn.ts", 3);
    let py = write_warning_py(dir.path(), "warn.py", 3);

    for source in [&ts, &py] {
        let out = eudkit(&["synth", source.to_str().unwrap(), "--compact"]);
        assert_ok(&out);
        assert_eq!(
            stderr(&out)
                .lines()
                .filter(|line| line.starts_with("warning: EUDSL-WARN-PERF:"))
                .count(),
            1,
            "identical warnings must collapse to one line:\n{}",
            stderr(&out)
        );
    }
}

#[test]
fn frontend_warnings_surface_for_dump_ir_compile_and_plan() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_warning_ts(dir.path(), "warn.ts", 1);

    let dumped = eudkit(&["dump-ir", source.to_str().unwrap(), "--json"]);
    assert_ok(&dumped);
    let report: Value = serde_json::from_str(&stdout(&dumped)).unwrap();
    assert_eq!(report["pre_assembly"], true);
    assert!(
        stderr(&dumped).contains("warning: EUDSL-WARN-PERF:"),
        "dump-ir must surface warnings:\n{}",
        stderr(&dumped)
    );

    let out_path = dir.path().join("warn.scx");
    let compiled = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
    ]);
    assert_ok(&compiled);
    assert!(
        stdout(&compiled).contains("T static"),
        "{}",
        stdout(&compiled)
    );
    assert!(
        stderr(&compiled).contains("warning: EUDSL-WARN-PERF:"),
        "compile must surface warnings:\n{}",
        stderr(&compiled)
    );

    let plan_path = dir.path().join("warn.plan.json");
    let plan_out = dir.path().join("plan-out");
    let entry = PlanEntry::freeze(
        "warn-entry",
        &source,
        Frontend::Ts,
        &basemap(),
        dir.path().join("plan-warn.scx"),
        None,
    )
    .unwrap();
    fs::write(&plan_path, BuildPlan::new(vec![entry]).to_json().unwrap()).unwrap();
    let planned = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        plan_out.to_str().unwrap(),
    ]);
    assert_ok(&planned);
    assert!(
        stderr(&planned).contains("warning: [warn-entry] EUDSL-WARN-PERF:"),
        "plan warnings must carry the entry name:\n{}",
        stderr(&planned)
    );

    let quiet = eudkit(&[
        "compile",
        "--plan",
        plan_path.to_str().unwrap(),
        "--out-dir",
        plan_out.to_str().unwrap(),
        "--no-warnings",
    ]);
    assert_ok(&quiet);
    assert!(
        stderr(&quiet).is_empty(),
        "--no-warnings must suppress plan warnings, got:\n{}",
        stderr(&quiet)
    );
}

#[test]
fn frontend_warnings_render_before_failure_stderr() {
    let dir = tempfile::tempdir().unwrap();
    let root = repo_root().display();
    let ts_source = format!(
        "import {{ FixedString, Var, buildModule }} from \"{root}/ts/index.ts\";\n\
         export const module = buildModule(() => {{\n\
           const s = new FixedString(64);\n\
           const a = Var(1);\n\
           const b = Var(2);\n\
           s.write(\"A={{}} B={{}}\", a, b);\n\
           throw new Error(\"boom\");\n\
         }});\n"
    );
    let ts_path = dir.path().join("boom.ts");
    fs::write(&ts_path, ts_source).unwrap();

    let py_source = format!(
        r#"import sys
sys.path.insert(0, "{root}")
from py import FixedString, Var, build_module
def prog():
    s = FixedString(64)
    a = Var(1)
    b = Var(2)
    s.write("A={{}} B={{}}", a, b)
    raise RuntimeError("boom")
module = build_module(prog)
"#
    );
    let py_path = dir.path().join("boom.py");
    fs::write(&py_path, py_source).unwrap();

    for source in [&ts_path, &py_path] {
        let out = eudkit(&["synth", source.to_str().unwrap()]);
        assert!(
            !out.status.success(),
            "failure test source unexpectedly succeeded:\nstdout:\n{}\nstderr:\n{}",
            stdout(&out),
            stderr(&out)
        );
        let err = stderr(&out);
        let warning_idx = err
            .find("warning: EUDSL-WARN-PERF:")
            .unwrap_or_else(|| panic!("expected rendered warning before failure:\n{err}"));
        let error_idx = err
            .find("E-FRONTEND")
            .unwrap_or_else(|| panic!("expected E-FRONTEND in stderr:\n{err}"));
        assert!(
            warning_idx < error_idx,
            "warning must be rendered before the frontend error:\n{err}"
        );
        assert!(
            !err.contains("__EUDKIT_WARN__"),
            "internal marker must not leak into failure output:\n{err}"
        );
    }
}

#[test]
fn compile_embeds_declared_files_into_the_output_mpq() {
    let dir = tempfile::tempdir().unwrap();
    let audio = b"RIFF-embedded-bgm-payload\0with-binary\x01\x02".to_vec();
    fs::write(dir.path().join("audio.wav"), &audio).unwrap();

    // The source lives in a temp dir while the CLI runs from the repo root, so
    // a successful build proves relative paths resolve against the compiling
    // source's directory, not the process CWD.
    let source = format!(
        "import {{ SetMemory, SetTo, Trigger, buildModule, embedFile }} from \"{root}/ts/index.ts\";\n\
         export const module = buildModule(() => {{\n\
           embedFile(\"bgm\", \"audio.wav\");\n\
           embedFile(\"data/bin\", \"audio.wav\");\n\
           new Trigger({{ conds: [], acts: [SetMemory(0x58a36c, SetTo, 1)] }});\n\
         }});\n",
        root = repo_root().display()
    );
    let source_path = dir.path().join("embed.ts");
    fs::write(&source_path, source).unwrap();

    let out_path = dir.path().join("embed.scx");
    let out = eudkit(&[
        "compile",
        source_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
    ]);
    assert_ok(&out);
    assert!(out_path.is_file());

    let mut ar = stormlib::Archive::open(&out_path, stormlib::OpenArchiveFlags::empty()).unwrap();
    for archived in ["bgm", "data/bin"] {
        let mut file = ar
            .open_file(archived)
            .unwrap_or_else(|err| panic!("{archived} missing from output MPQ: {err}"));
        assert_eq!(
            file.read_all().unwrap(),
            audio,
            "{archived} must carry the source bytes verbatim"
        );
    }
}

/// Issue #25 review B1: a module whose *only* string use is the BGM helper must
/// still compile against a basemap.
///
/// A basemap ships 1024 template strings. The lowerer seeds them from the
/// module's action strings, and `PlayWAV` interns its archive name into that
/// same table; when the predicate only looked at `DisplayText` text, a
/// wav-only module left the table unseeded and `build_ordered` rejected the
/// build with "LIR strings (1 ids) shorter than template table (1024 ids)".
/// Both frontends and the helper itself are exercised here, and the archived
/// track is byte-checked so the acceptance criterion ("the file lands in the
/// MPQ as `bgm`") is proven for a module that actually uses the helper.
#[test]
fn compile_plays_bgm_from_a_basemap_with_no_other_strings() {
    let dir = tempfile::tempdir().unwrap();
    let audio = b"RIFF-bgm-only-module\0binary\x01\x02\xff".to_vec();
    fs::write(dir.path().join("track.wav"), &audio).unwrap();

    let ts_source = format!(
        r#"import {{ buildModule, playBgm }} from "{root}/ts/index.ts";
export const module = buildModule(() => {{
  playBgm("track.wav", 1.5);
}});
"#,
        root = repo_root().display()
    );
    let ts_path = dir.path().join("bgm_only.ts");
    fs::write(&ts_path, ts_source).unwrap();

    let py_source = r#"from py import build_module, play_bgm


def prog():
    play_bgm("track.wav", 1.5)


module = build_module(prog)
"#;
    let py_path = dir.path().join("bgm_only.py");
    fs::write(&py_path, py_source).unwrap();

    for source in [&ts_path, &py_path] {
        let out_path = dir
            .path()
            .join(source.with_extension("scx").file_name().unwrap());
        let out = eudkit(&[
            "compile",
            source.to_str().unwrap(),
            "--basemap",
            basemap().to_str().unwrap(),
            "--out",
            out_path.to_str().unwrap(),
        ]);
        assert!(
            out.status.success(),
            "{} must compile against a basemap:\nstdout:\n{}\nstderr:\n{}",
            source.display(),
            stdout(&out),
            stderr(&out)
        );
        assert!(out_path.is_file());

        let mut ar =
            stormlib::Archive::open(&out_path, stormlib::OpenArchiveFlags::empty()).unwrap();
        let mut track = ar
            .open_file("bgm")
            .unwrap_or_else(|err| panic!("bgm missing from {}: {err}", out_path.display()));
        assert_eq!(
            track.read_all().unwrap(),
            audio,
            "the embedded track must reach the MPQ verbatim"
        );
    }
}

#[test]
fn per_command_help_is_focused_and_describes_flags() {
    let compile = eudkit(&["compile", "--help"]);
    assert_ok(&compile);
    let text = stdout(&compile);
    assert!(
        text.contains("eudkit compile <source.ts|source.py|module.hir.json>"),
        "{text}"
    );
    assert!(text.contains("--basemap"), "{text}");
    assert!(
        text.contains("deprecated no-op; cost gate is on by default"),
        "{text}"
    );
    assert!(text.contains("plan builds only"), "{text}");
    assert!(!text.contains("eudkit synth <"), "{text}");
    assert!(!text.contains("eudkit dump-ir <"), "{text}");

    let dump_ir = eudkit(&["dump-ir", "--help"]);
    assert_ok(&dump_ir);
    let text = stdout(&dump_ir);
    assert!(
        text.contains("eudkit dump-ir <source.ts|source.py|module.hir.json>"),
        "{text}"
    );
    assert!(text.contains("--loops"), "{text}");
    assert!(!text.contains("--basemap"), "{text}");
    assert!(!text.contains("eudkit compile <"), "{text}");

    let synth = eudkit(&["synth", "--help"]);
    assert_ok(&synth);
    let text = stdout(&synth);
    assert!(
        text.contains("eudkit synth <source.ts|source.py>"),
        "{text}"
    );
    assert!(text.contains("--compact"), "{text}");
    assert!(!text.contains("--basemap"), "{text}");
    assert!(!text.contains("eudkit compile <"), "{text}");
}

#[test]
fn unknown_option_errors_point_at_the_command_help() {
    for command in ["compile", "dump-ir", "synth"] {
        let out = eudkit(&[command, "demo.ts", "--bogus"]);
        assert!(!out.status.success(), "{command} --bogus must fail");
        let err = stderr(&out);
        assert!(err.contains("Unknown option '--bogus'"), "{err}");
        assert!(
            err.contains(&format!("Run 'eudkit {command} --help' for usage.")),
            "{err}"
        );
    }
}

#[test]
fn compile_accepts_a_synthesized_hir_json_without_a_frontend() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let hir_path = dir.path().join("demo.hir.json");
    let synth = eudkit(&[
        "synth",
        source.to_str().unwrap(),
        "--out",
        hir_path.to_str().unwrap(),
    ]);
    assert_ok(&synth);

    let out_path = dir.path().join("from_hir.scx");
    let out = eudkit(&[
        "compile",
        hir_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
    ]);
    assert_ok(&out);
    assert!(out_path.is_file());
    assert!(stdout(&out).contains("36T static"), "{}", stdout(&out));
}

fn init_workspace() -> tempfile::TempDir {
    let target = repo_root().join("target");
    fs::create_dir_all(&target).unwrap();
    tempfile::tempdir_in(&target).unwrap()
}

#[test]
fn doctor_json_reports_the_checkout_and_toolchain() {
    let out = eudkit(&["doctor", "--json"]);
    assert_ok(&out);
    let report: Value = serde_json::from_str(&stdout(&out)).unwrap();
    assert_eq!(report["eudkit_version"], env!("CARGO_PKG_VERSION"));
    assert!(
        report["eudkit_root"].as_str().is_some(),
        "EUDKIT_ROOT must be reported: {report}"
    );
    assert_eq!(report["runtime"], "checkout");
    assert!(
        report["checkout_root"].as_str().is_some(),
        "the checkout root must resolve: {report}"
    );
    assert_eq!(report["node"]["found"], true, "{report}");
    assert_eq!(report["node"]["minimum"], "22.6.0");
    assert_eq!(report["python"]["found"], true, "{report}");
    assert_eq!(report["python"]["minimum"], "3.11.0");
    assert!(report["ts_package"]["found"].is_boolean(), "{report}");
    assert!(report["python_package"]["found"].is_boolean(), "{report}");

    let text = eudkit(&["doctor"]);
    assert_ok(&text);
    let text = stdout(&text);
    assert!(text.contains("Runtime: checkout"), "{text}");
    assert!(text.contains("node:"), "{text}");
    assert!(text.contains("python3:"), "{text}");
}

#[test]
fn doctor_with_invalid_eudkit_root_reports_problems() {
    let dir = tempfile::tempdir().unwrap();
    let invalid = dir.path().join("not-a-checkout");
    fs::create_dir_all(&invalid).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["doctor", "--json"])
        .current_dir(dir.path())
        .env("EUDKIT_ROOT", &invalid)
        .output()
        .expect("run eudkit");
    assert_ok(&out);
    let report: Value = serde_json::from_str(&stdout(&out)).unwrap();
    assert_eq!(report["runtime"], "invalid EUDKIT_ROOT", "{report}");
    assert_eq!(report["checkout_root"], Value::Null, "{report}");
    assert_eq!(report["ok"], false, "{report}");
    let runtime_error = report["runtime_error"]
        .as_str()
        .expect("invalid EUDKIT_ROOT must explain the runtime failure");
    assert!(runtime_error.contains("E-RUNTIME"), "{report}");

    let text = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["doctor"])
        .current_dir(dir.path())
        .env("EUDKIT_ROOT", &invalid)
        .output()
        .expect("run eudkit");
    assert_ok(&text);
    let text = stdout(&text);
    assert!(text.contains("Runtime: invalid EUDKIT_ROOT"), "{text}");
    assert!(text.contains("E-RUNTIME"), "{text}");
    assert!(text.contains("problems found"), "{text}");
}

#[test]
fn init_ts_starter_compiles_inside_the_checkout() {
    let dir = init_workspace();
    let out = eudkit(&["init", dir.path().to_str().unwrap(), "--ts"]);
    assert_ok(&out);
    let progress = stdout(&out);
    assert!(progress.contains("eudkit compile"), "{progress}");

    let source = dir.path().join("main.ts");
    let basemap = dir.path().join("basemap.scx");
    let body = fs::read_to_string(&source).unwrap();
    assert!(body.contains("ts/index.ts"), "{body}");
    assert!(body.contains("once([], () =>"), "{body}");
    assert!(body.contains("Hello from eudkit!"), "{body}");
    assert_eq!(
        fs::read(&basemap).unwrap(),
        fs::read(repo_root().join("tests/outputmap/basemap/basemap_silent.scx")).unwrap(),
        "init must embed the silent basemap"
    );

    let compiled = dir.path().join("main.scx");
    let compile = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap.to_str().unwrap(),
        "--out",
        compiled.to_str().unwrap(),
    ]);
    assert_ok(&compile);
    assert!(compiled.is_file());
}

#[test]
fn init_py_starter_compiles_inside_the_checkout() {
    let dir = init_workspace();
    let out = eudkit(&["init", dir.path().to_str().unwrap(), "--py"]);
    assert_ok(&out);

    let source = dir.path().join("main.py");
    let basemap = dir.path().join("basemap.scx");
    let body = fs::read_to_string(&source).unwrap();
    assert!(body.contains("from py import"), "{body}");
    assert!(body.contains("once([], lambda:"), "{body}");
    assert!(body.contains("Hello from eudkit!"), "{body}");
    assert!(body.contains("module = build_module(prog)"), "{body}");

    let compiled = dir.path().join("main.scx");
    let compile = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap.to_str().unwrap(),
        "--out",
        compiled.to_str().unwrap(),
    ]);
    assert_ok(&compile);
    assert!(compiled.is_file());
}

#[test]
fn init_outside_a_checkout_targets_the_installed_package() {
    let ts_dir = tempfile::tempdir().unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["init", ts_dir.path().to_str().unwrap()])
        .current_dir(ts_dir.path())
        .env_remove("EUDKIT_ROOT")
        .env("EUDKIT_NO_MANIFEST_FALLBACK", "1")
        .output()
        .expect("run eudkit");
    assert_ok(&out);
    let body = fs::read_to_string(ts_dir.path().join("main.ts")).unwrap();
    assert!(body.contains("from \"eudkit\""), "{body}");
    assert!(!body.contains("ts/index.ts"), "{body}");

    let py_dir = tempfile::tempdir().unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["init", py_dir.path().to_str().unwrap(), "--py"])
        .current_dir(py_dir.path())
        .env_remove("EUDKIT_ROOT")
        .env("EUDKIT_NO_MANIFEST_FALLBACK", "1")
        .output()
        .expect("run eudkit");
    assert_ok(&out);
    let body = fs::read_to_string(py_dir.path().join("main.py")).unwrap();
    assert!(body.contains("from eudkit import"), "{body}");
    assert!(!body.contains("from py import"), "{body}");
}

#[test]
fn init_under_tmp_with_an_explicit_checkout_emits_compiling_checkout_imports() {
    let dir = tempfile::tempdir().unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["init", dir.path().to_str().unwrap(), "--ts"])
        .current_dir(repo_root())
        .env("EUDKIT_ROOT", repo_root())
        .output()
        .expect("run eudkit");
    assert_ok(&out);

    let source = dir.path().join("main.ts");
    let basemap = dir.path().join("basemap.scx");
    let body = fs::read_to_string(&source).unwrap();
    assert!(body.contains("ts/index.ts"), "{body}");
    assert!(!body.contains("from \"eudkit\""), "{body}");

    let compiled = dir.path().join("main.scx");
    let compile = eudkit(&[
        "compile",
        source.to_str().unwrap(),
        "--basemap",
        basemap.to_str().unwrap(),
        "--out",
        compiled.to_str().unwrap(),
    ]);
    assert_ok(&compile);
    assert!(compiled.is_file());
}

#[test]
fn init_refuses_to_overwrite_an_existing_starter() {
    let dir = init_workspace();
    let first = eudkit(&["init", dir.path().to_str().unwrap(), "--ts"]);
    assert_ok(&first);
    let source = dir.path().join("main.ts");
    let before = fs::read(&source).unwrap();

    let second = eudkit(&["init", dir.path().to_str().unwrap(), "--ts"]);
    assert!(!second.status.success(), "init must refuse to overwrite");
    let err = stderr(&second);
    assert!(err.contains("refusing to overwrite"), "{err}");
    assert!(err.contains("main.ts"), "{err}");
    assert_eq!(
        fs::read(&source).unwrap(),
        before,
        "starter must be untouched"
    );
}

#[test]
fn invalid_eudkit_root_fails_loudly() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let invalid = dir.path().join("not-a-checkout");
    fs::create_dir_all(&invalid).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["synth", source.to_str().unwrap()])
        .current_dir(dir.path())
        .env("EUDKIT_ROOT", &invalid)
        .output()
        .expect("run eudkit");
    assert!(!out.status.success());
    let err = stderr(&out);
    assert!(err.contains("E-RUNTIME"), "{err}");
    assert!(err.contains("EUDKIT_ROOT"), "{err}");
    assert!(err.contains("ts/index.ts"), "{err}");
    assert!(err.contains("py/__init__.py"), "{err}");
}

#[test]
fn no_manifest_fallback_outside_a_checkout_fails_with_guidance() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let empty_bin = dir.path().join("empty-bin");
    fs::create_dir_all(&empty_bin).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(["synth", source.to_str().unwrap()])
        .current_dir(dir.path())
        .env_remove("EUDKIT_ROOT")
        .env("EUDKIT_NO_MANIFEST_FALLBACK", "1")
        .env("PATH", &empty_bin)
        .output()
        .expect("run eudkit");
    assert!(!out.status.success());
    let err = stderr(&out);
    assert!(err.contains("E-RUNTIME"), "{err}");
    assert!(
        err.contains("cannot locate the eudkit DSL runtime"),
        "{err}"
    );
    assert!(err.contains("Set EUDKIT_ROOT"), "{err}");
}

#[test]
fn compile_hir_json_with_plan_out_requires_a_frontend() {
    let dir = tempfile::tempdir().unwrap();
    let source = write_ts(dir.path(), "demo.ts", demo_body());
    let hir_path = dir.path().join("demo.hir.json");
    let synth = eudkit(&[
        "synth",
        source.to_str().unwrap(),
        "--out",
        hir_path.to_str().unwrap(),
    ]);
    assert_ok(&synth);

    let plan_path = dir.path().join("from_hir.plan.json");
    let out_path = dir.path().join("from_hir.scx");
    let out = eudkit(&[
        "compile",
        hir_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
    ]);
    assert!(!out.status.success());
    let err = stderr(&out);
    assert!(
        err.contains("--plan-out requires --frontend ts|py or a .ts/.py source"),
        "{err}"
    );
    assert!(!plan_path.is_file(), "no plan may be written on rejection");

    // Supplying the frontend explicitly makes the HIR plan build valid.
    let planned = eudkit(&[
        "compile",
        hir_path.to_str().unwrap(),
        "--basemap",
        basemap().to_str().unwrap(),
        "--out",
        out_path.to_str().unwrap(),
        "--plan-out",
        plan_path.to_str().unwrap(),
        "--frontend",
        "ts",
    ]);
    assert_ok(&planned);
    assert!(plan_path.is_file());
}
