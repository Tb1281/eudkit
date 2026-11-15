//! Golden tests for `dump-ir --explain-folds`.
//!
//! Default text/JSON must stay byte-identical; the fold report is an opt-in
//! section and never changes the simulated costs.

use std::fs;
use std::path::Path;
use std::process::{Command, Output};

use serde_json::Value;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn fixture_dir() -> std::path::PathBuf {
    repo_root().join("crates/eudkit-cli/tests/fixtures")
}

const FIXTURE: &str = "crates/eudkit-cli/tests/fixtures/explain_folds.hir.json";

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

fn golden(name: &str) -> String {
    fs::read_to_string(fixture_dir().join(name))
        .unwrap_or_else(|e| panic!("read golden {name}: {e}"))
}

fn dump_json(args: &[&str]) -> String {
    let mut full = vec!["dump-ir", FIXTURE];
    full.extend_from_slice(args);
    let output = eudkit(&full);
    assert_ok(&output);
    stdout(&output)
}

#[test]
fn default_json_has_no_folds_key() {
    let text = dump_json(&["--json"]);
    assert_eq!(text, golden("explain_folds.default.json"));

    let value: Value = serde_json::from_str(&text).unwrap();
    assert!(
        value.get("folds").is_none(),
        "default JSON must omit folds, got:\n{text}"
    );
}

#[test]
fn explain_json_matches_golden_and_keeps_costs() {
    let text = dump_json(&["--json", "--explain-folds"]);
    assert_eq!(text, golden("explain_folds.explain.json"));

    let explained: Value = serde_json::from_str(&text).unwrap();
    let folds = explained["folds"].as_array().expect("folds array");
    assert!(!folds.is_empty());
    for entry in folds {
        for key in ["kind", "site", "before", "after"] {
            assert!(
                entry[key].as_str().is_some_and(|s| !s.is_empty()),
                "fold entry missing {key}: {entry}"
            );
        }
    }

    let default: Value = serde_json::from_str(&golden("explain_folds.default.json")).unwrap();
    for key in ["static_cost", "executed_cost", "storage", "packing"] {
        assert_eq!(
            default[key], explained[key],
            "{key} changed with --explain-folds"
        );
    }
}

#[test]
fn explain_text_matches_golden() {
    let output = eudkit(&["dump-ir", FIXTURE, "--explain-folds"]);
    assert_ok(&output);
    let text = stdout(&output);
    assert_eq!(text, golden("explain_folds.explain.txt"));
    assert!(text.contains("Folds: 6 event(s)"), "{text}");
    assert!(text.contains("[cond-fold] when#0:"), "{text}");
    assert!(text.contains("[gate-drop] when#6:"), "{text}");
}

#[test]
fn explain_folds_with_loops_two_reports_same_folds() {
    let text = dump_json(&["--json", "--explain-folds", "--loops", "2"]);
    let value: Value = serde_json::from_str(&text).unwrap();

    let expected: Value = serde_json::from_str(&golden("explain_folds.explain.json")).unwrap();
    assert_eq!(value["folds"], expected["folds"]);
    assert!(
        value["executed_cost"].get("visited_items").is_some(),
        "--loops 2 must still execute: {text}"
    );
}
