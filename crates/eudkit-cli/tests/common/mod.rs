//! Shared harness for the CLI integration suites that run both frontends
//! through the seeded simulator.

#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};

use eudkit::{Frontend, PreparedProgram};
use eudkit_lir::{ExecutedCost, LirItem, SimulationState, init_simulation};
use eudkit_opt::cell::{StorageClass, bit_value};

/// `EPD(0x6509B0)`, the CurrentPlayer register dword.
pub const CP_EPD: u32 = 203_155;

pub fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

/// Write arbitrary source text into `dir` and return its path.
pub fn write_source(dir: &Path, name: &str, source: &str) -> PathBuf {
    let path = dir.join(name);
    fs::write(&path, source).unwrap();
    path
}

/// Write a Python module with the suite's import block and a `prog` body.
pub fn write_py(dir: &Path, name: &str, imports: &str, body: &str) -> PathBuf {
    let source = format!("{imports}\n\ndef prog():\n{body}\n\nmodule = build_module(prog)\n");
    write_source(dir, name, &source)
}

/// Write a TypeScript module with the suite's import block and builder body.
pub fn write_ts(dir: &Path, name: &str, imports: &str, body: &str) -> PathBuf {
    let source = format!("{imports}\n\nexport const module = buildModule(() => {{\n{body}\n}});\n");
    write_source(dir, name, &source)
}

/// Read one scalar variable's runtime value from its storage class.
///
/// Mirrors the `var_values` readback in `eudkit-compile sim`, so tests can
/// assert variables rather than side-channel memory writes.
pub fn read_var(prepared: &PreparedProgram, sim: &SimulationState, var_id: usize) -> u32 {
    let plan = &prepared.storage;
    let lir = &prepared.lir;
    let num_pages = plan.trigger_pages;
    let blob_items = lir
        .module
        .items
        .iter()
        .filter(|item| matches!(item, LirItem::Blob(_)))
        .count();
    let num_exec = lir
        .module
        .items
        .len()
        .saturating_sub(num_pages)
        .saturating_sub(blob_items);
    let cell_item = num_exec + num_pages;

    match plan.storage(var_id).expect("variable has storage") {
        StorageClass::TriggerPage {
            page_index,
            slot_index,
        } => sim.read_page_slot_value(num_exec + page_index, slot_index),
        StorageClass::DwordCell { dword_index } => {
            let addr = sim.payload.addrs[cell_item] + (dword_index * 4) as u32;
            sim.read_epd(eudkit_lir::epd_u32(addr))
        }
        StorageClass::BitCell {
            dword_index,
            bit_index,
        } => {
            let addr = sim.payload.addrs[cell_item] + (dword_index * 4) as u32;
            bit_value(bit_index, sim.read_epd(eudkit_lir::epd_u32(addr)))
        }
    }
}

pub struct CaseResult {
    pub count: u32,
    pub cost: ExecutedCost,
    pub cp_value: u32,
}

pub fn run_one(frontend: Frontend, path: &Path, seeds: &[(u32, u32)]) -> CaseResult {
    let prepared = eudkit::prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    let cost = sim.step_loop();
    let count = read_var(&prepared, &sim, 0);
    let cp_value = sim.read_epd(CP_EPD);
    CaseResult {
        count,
        cost,
        cp_value,
    }
}

/// Run both frontends and assert their counts and triggered-trigger costs agree.
pub fn run_case(
    label: &str,
    py_path: &Path,
    ts_path: &Path,
    seeds: &[(u32, u32)],
) -> (CaseResult, CaseResult) {
    let py = run_one(Frontend::Py, py_path, seeds);
    let ts = run_one(Frontend::Ts, ts_path, seeds);
    assert_eq!(py.count, ts.count, "{label}: Py/TS disagree on count");
    assert_eq!(
        py.cost.triggers, ts.cost.triggers,
        "{label}: Py/TS disagree on executed triggers"
    );
    (py, ts)
}

pub fn tempdir(prefix: &str) -> tempfile::TempDir {
    tempfile::Builder::new()
        .prefix(prefix)
        .tempdir_in(repo_root().join("target"))
        .unwrap()
}
