//! The stock/extension vocabulary is generated from `schema/stock-rows.json`.
//!
//! These tests pin the generator's public behaviour (which the build script
//! runs) and the checked vocabulary it produces, including the loud failures
//! for a missing/unparseable spec and for a stale curated doc-overlay key.

#[path = "../build/stock_vocabulary.rs"]
mod stock_vocabulary;

use std::path::PathBuf;

use stock_vocabulary::{generate, generate_with_overlay, load_rows, render_symbols};

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|path| path.parent())
        .expect("crate lives in <workspace>/crates/eudkit-lsp")
        .to_path_buf()
}

fn spec_path() -> PathBuf {
    workspace_root().join("schema").join("stock-rows.json")
}

#[test]
fn vocabulary_generates_every_stock_and_extension_row_in_spec_order() {
    let rows = load_rows(&spec_path()).expect("the checked-in spec parses");
    let symbols = render_symbols(&rows).expect("the spec renders");

    let expected: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    let actual: Vec<&str> = symbols.iter().map(|symbol| symbol.name.as_str()).collect();
    assert_eq!(actual, expected, "vocabulary follows spec row order");

    assert_eq!(
        symbols.len(),
        92,
        "22 conditions + 57 actions + 13 extensions"
    );
    for symbol in &symbols {
        assert!(!symbol.doc.is_empty(), "{} needs a doc", symbol.name);
    }

    let countdown = symbols
        .iter()
        .find(|symbol| symbol.name == "CountdownTimer")
        .unwrap();
    assert_eq!(countdown.detail, "stock condition 1");
    assert!(countdown.doc.contains("`comparison, time`"));
    assert!(countdown.doc.contains("condition flags stay 0"));

    let set_deaths = symbols
        .iter()
        .find(|symbol| symbol.name == "SetDeaths")
        .unwrap();
    assert_eq!(set_deaths.detail, "stock action 45");
    assert!(set_deaths.doc.contains("default flags 20"));

    let memory = symbols
        .iter()
        .find(|symbol| symbol.name == "Memory")
        .unwrap();
    assert_eq!(memory.detail, "eudkit extension");
    assert!(memory.doc.contains("not a StarEdit stock row"));

    assert!(
        symbols.iter().all(|symbol| symbol.name != "UnitProperty"),
        "the UnitProperty record is not a trigger helper"
    );
}

#[test]
fn checked_vocabulary_embeds_the_generated_rows_verbatim() {
    let rows = load_rows(&spec_path()).expect("the checked-in spec parses");
    let symbols = render_symbols(&rows).expect("the spec renders");

    let generated = &eudkit_lsp::VOCABULARY[eudkit_lsp::VOCABULARY.len() - symbols.len()..];
    for (want, got) in symbols.iter().zip(generated) {
        assert_eq!(got.name, want.name);
        assert_eq!(got.detail, want.detail);
        assert_eq!(got.doc, want.doc, "{} doc drifted from the spec", want.name);
    }
}

#[test]
fn curated_overlay_applies_to_its_row_and_rejects_stale_keys() {
    let rows = load_rows(&spec_path()).expect("the checked-in spec parses");
    let overlay = &[("DisplayText", "Curated note.")];
    let symbols = generate_with_overlay(&rows, overlay).expect("the overlay names a spec row");
    let display_text = symbols
        .iter()
        .find(|symbol| symbol.name == "DisplayText")
        .unwrap();
    assert!(display_text.doc.ends_with("Curated note."));

    let error = generate_with_overlay(&rows, &[("NoSuchRow", "Stale.")])
        .expect_err("a stale overlay key must fail the build");
    assert!(
        error.contains("doc overlay key NoSuchRow"),
        "unexpected error: {error}"
    );
}

#[test]
fn adding_a_stock_row_needs_only_the_spec() {
    let mut rows = load_rows(&spec_path()).expect("the checked-in spec parses");
    rows.push(stock_vocabulary::SpecRow {
        id: "SyntheticProbe".to_string(),
        section: "conditions".to_string(),
        chk_type: Some(99),
        default_flags: Some(0),
        doc: "Synthetic probe condition (condition 99).".to_string(),
        params: vec![stock_vocabulary::SpecParam {
            name: "comparison".to_string(),
            default: None,
            target_form: false,
        }],
    });
    let symbols = render_symbols(&rows).expect("the extended spec renders");
    let probe = symbols
        .iter()
        .find(|symbol| symbol.name == "SyntheticProbe")
        .expect("the new row must become an LSP symbol");
    assert_eq!(probe.detail, "stock condition 99");
    assert!(probe.doc.contains("`comparison`"));
    assert!(probe.doc.contains("Synthetic probe condition"));
}

#[test]
fn a_missing_spec_fails_loudly() {
    let error = generate(&workspace_root().join("schema").join("does-not-exist.json"))
        .expect_err("a missing spec must fail the build");
    assert!(error.contains("cannot read"), "unexpected error: {error}");
}

#[test]
fn an_unparseable_spec_fails_loudly() {
    let dir = std::env::temp_dir().join(format!("eudkit-lsp-spec-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("stock-rows.json");
    std::fs::write(&path, "{ this is not json").unwrap();

    let error = generate(&path).expect_err("an unparseable spec must fail the build");
    assert!(error.contains("cannot parse"), "unexpected error: {error}");
    std::fs::remove_dir_all(&dir).ok();
}
