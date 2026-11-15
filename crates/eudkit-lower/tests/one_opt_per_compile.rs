//! C2c acceptance: a map compile runs `optimize_module` and `analyze_storage`
//! exactly once, for the result lowering actually uses.
//!
//! The counters are compiled in by eudkit-opt's `count-calls` feature, which
//! this crate's dev-dependencies enable. This file deliberately holds a single
//! test: the counters are process-wide, and cargo runs tests within one
//! binary on parallel threads.

use std::path::Path;
use std::sync::atomic::Ordering;

use eudkit_lower::compile_hir_to_map;

#[test]
fn map_compile_runs_the_opt_storage_pair_once() {
    let hir_json = r#"{
        "version": "3.2",
        "module": {
            "vars": [{"id": 0, "init": 1}, {"id": 1, "init": 0, "storage": "page"}],
            "stmts": [
                {"kind": "vartransfer", "dst_var": 0, "src_var": 1, "modifier": 7},
                {"kind": "trigger", "id": 0, "conds": [], "acts": [], "preserved": true, "flags": 4}
            ]
        }
    }"#;
    let basemap = Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/outputmap/basemap/basemap.scx"
    ));
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("out.scx");

    let opt_before = eudkit_opt::OPTIMIZE_MODULE_CALLS.load(Ordering::SeqCst);
    let storage_before = eudkit_opt::cell::ANALYZE_STORAGE_CALLS.load(Ordering::SeqCst);

    compile_hir_to_map(hir_json, basemap, &out).unwrap();

    let opt_calls = eudkit_opt::OPTIMIZE_MODULE_CALLS.load(Ordering::SeqCst) - opt_before;
    let storage_calls =
        eudkit_opt::cell::ANALYZE_STORAGE_CALLS.load(Ordering::SeqCst) - storage_before;
    assert_eq!(
        opt_calls, 1,
        "one compile must call optimize_module exactly once (was {opt_calls})"
    );
    assert_eq!(
        storage_calls, 1,
        "one compile must call analyze_storage exactly once (was {storage_calls})"
    );
    assert!(out.is_file(), "the compile must still write its map");
}
