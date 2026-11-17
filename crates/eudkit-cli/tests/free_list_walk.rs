//! T-WP10-T2: simulator oracle for the tier-3 free-list walk.
//!
//! The free list is a plain forward chain from head EPD 161845 (`0x628438`)
//! through each node's `next` link (`ptr + 1`). Three fabricated CUnits live
//! outside the payload:
//!
//! * `A = 0x59CCA8` (EPD 19025)
//! * `B = 0x59CDF8` (EPD 19109)
//! * `C = 0x59CF48` (EPD 19193)
//!
//! The Python and TypeScript frontends are run through the same seeded
//! simulator and must agree on the count and the executed cost.

use std::path::{Path, PathBuf};

use eudkit::Frontend;

mod common;
use common::{CP_EPD, CaseResult, tempdir};

const FREE_HEAD: u32 = 161_845; // 0x628438
const A_EPD: u32 = 19025;
const B_EPD: u32 = 19109;
const C_EPD: u32 = 19193;
const A_ADDR: u32 = 0x59CCA8;
const B_ADDR: u32 = 0x59CDF8;
const C_ADDR: u32 = 0x59CF48;

const PY_IMPORTS: &str = "from py import BREAK, Var, build_module\n\
         from py.capacity import free_list_walk, free_slot_count";

const TS_IMPORTS: &str = "import { BREAK, Var, buildModule } from \"{root}/ts/index.ts\";\n\
         import { freeListWalk, freeSlotCount } from \"{root}/ts/capacity.ts\";";

fn write_py(dir: &Path, name: &str, body: &str) -> PathBuf {
    common::write_py(dir, name, PY_IMPORTS, body)
}

fn write_ts(dir: &Path, name: &str, body: &str) -> PathBuf {
    let imports = TS_IMPORTS.replace("{root}", &common::repo_root().display().to_string());
    common::write_ts(dir, name, &imports, body)
}

fn run_case(
    dir: &Path,
    label: &str,
    py_body: &str,
    ts_body: &str,
    seeds: &[(u32, u32)],
) -> (CaseResult, CaseResult) {
    let py_path = write_py(dir, &format!("{label}.py"), py_body);
    let ts_path = write_ts(dir, &format!("{label}.ts"), ts_body);
    common::run_case(label, &py_path, &ts_path, seeds)
}

fn free_chain() -> Vec<(u32, u32)> {
    vec![
        (FREE_HEAD, A_ADDR),
        (A_EPD + 1, B_ADDR),
        (B_EPD + 1, C_ADDR),
        (C_EPD + 1, 0),
    ]
}

fn cyclic_chain() -> Vec<(u32, u32)> {
    vec![
        (FREE_HEAD, A_ADDR),
        (A_EPD + 1, B_ADDR),
        (B_EPD + 1, C_ADDR),
        (C_EPD + 1, A_ADDR),
    ]
}

const PY_FREE_SLOT_COUNT: &str = "    free_slot_count()\n";
const TS_FREE_SLOT_COUNT: &str = "  freeSlotCount();\n";

const PY_BREAK: &str = "    count = Var(0)\n\
                        \x20   def body():\n\
                        \x20       count.iadd(1)\n\
                        \x20       return BREAK\n\
                        \x20   free_list_walk(body)\n";
const TS_BREAK: &str = "  const count = Var(0);\n\
                        \x20 freeListWalk(() => {\n\
                        \x20   count.iadd(1);\n\
                        \x20   return BREAK;\n\
                        \x20 });\n";

#[test]
fn three_node_free_chain_counts_three() {
    let dir = tempdir("free-list-three-");
    let (py, _ts) = run_case(
        dir.path(),
        "three",
        PY_FREE_SLOT_COUNT,
        TS_FREE_SLOT_COUNT,
        &free_chain(),
    );
    assert_eq!(py.count, 3);
}

#[test]
fn empty_list_runs_zero_iterations_and_restores_cp() {
    let dir = tempdir("free-list-empty-");
    let seeds = vec![(FREE_HEAD, 0), (CP_EPD, 0x1234_5678)];
    let py_path = write_py(dir.path(), "empty.py", PY_FREE_SLOT_COUNT);
    let ts_path = write_ts(dir.path(), "empty.ts", TS_FREE_SLOT_COUNT);
    let py = common::run_one(Frontend::Py, &py_path, &seeds);
    let ts = common::run_one(Frontend::Ts, &ts_path, &seeds);
    assert_eq!(py.count, 0);
    assert_eq!(ts.count, 0);
    assert_eq!(py.cp_value, 0x1234_5678, "py: restore must run");
    assert_eq!(ts.cp_value, 0x1234_5678, "ts: restore must run");
}

#[test]
fn a_cycle_stops_at_the_scan_bound() {
    let dir = tempdir("free-list-cycle-");
    let (py, ts) = run_case(
        dir.path(),
        "cycle",
        PY_FREE_SLOT_COUNT,
        TS_FREE_SLOT_COUNT,
        &cyclic_chain(),
    );
    assert_eq!(py.count, 1700, "a cycle must stop at CUNIT_FREE_SCAN_MAX");
    assert_eq!(ts.count, 1700);
}

#[test]
fn an_out_of_pool_pointer_stops_after_the_last_valid_node() {
    let dir = tempdir("free-list-out-of-pool-");
    let seeds = vec![(FREE_HEAD, A_ADDR), (A_EPD + 1, 0x1234)];
    let (py, _ts) = run_case(
        dir.path(),
        "out_of_pool",
        PY_FREE_SLOT_COUNT,
        TS_FREE_SLOT_COUNT,
        &seeds,
    );
    assert_eq!(py.count, 1);
}

#[test]
fn a_breaking_body_stops_early() {
    let dir = tempdir("free-list-break-");
    let (py, _ts) = run_case(dir.path(), "break", PY_BREAK, TS_BREAK, &free_chain());
    assert_eq!(py.count, 1);
}

#[test]
fn per_node_cost_is_pinned() {
    let dir = tempdir("free-list-cost-");
    let (zero, _) = run_case(
        dir.path(),
        "cost_empty",
        PY_FREE_SLOT_COUNT,
        TS_FREE_SLOT_COUNT,
        &[(FREE_HEAD, 0)],
    );
    let (three, _) = run_case(
        dir.path(),
        "cost_three",
        PY_FREE_SLOT_COUNT,
        TS_FREE_SLOT_COUNT,
        &free_chain(),
    );
    let per_node = (three.cost.triggers - zero.cost.triggers) / 3;
    // Pinned in ADR 0015 §9. The measured shape is one T more than T-WP7's
    // forward U walk because the tier-3 scan adds the `remaining` guard.
    assert_eq!(per_node, 37, "per-node executed triggers");
}
