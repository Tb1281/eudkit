//! T-WP7: G-SIMLIST — simulator oracle for the unit-list walk.
//!
//! Three fabricated CUnits live outside the payload:
//!
//! * `A = 0x59CCA8` (EPD 19025)
//! * `B = 0x59CDF8` (EPD 19109)
//! * `C = 0x59CF48` (EPD 19193)
//!
//! `U` is walked forward through `next` (`+1`) from head EPD 161843; `D` is
//! walked backward through `prev` (`+0`) from tail EPD 161842. A forward walk
//! from the same D tail is empty, which is the direction bug this fixture
//! exists to catch. The Python and TypeScript frontends are run through the
//! same seeded simulator and must agree.

use std::path::{Path, PathBuf};
use std::process::Command;

use eudkit::Frontend;

mod common;
use common::{CP_EPD, CaseResult, repo_root, tempdir};

const U_HEAD: u32 = 161_843; // 0x628430
const D_TAIL: u32 = 161_842; // 0x62842C
const A_EPD: u32 = 19025;
const B_EPD: u32 = 19109;
const C_EPD: u32 = 19193;
const A_ADDR: u32 = 0x59CCA8;
const B_ADDR: u32 = 0x59CDF8;
const C_ADDR: u32 = 0x59CF48;

const PY_IMPORTS: &str = "from py import (\n\
         \x20   EPD_CURRENT_PLAYER,\n\
         \x20   SetDeaths,\n\
         \x20   SetTo,\n\
         \x20   Var,\n\
         \x20   build_module,\n\
         \x20   for_each_unit_list,\n\
         \x20   when,\n\
         \x20   while_loop,\n\
         )\n\
         from py.cp import CP_ADDR, read_current_player_register\n\
         from py.hir import AddrReloc, emitVarStore\n\
         from py.view import in_cunit_pool, read_cunit_pointer_saved";

const TS_IMPORTS: &str = "import {\n\
         \x20 EPD_CURRENT_PLAYER,\n\
         \x20 SetDeaths,\n\
         \x20 SetTo,\n\
         \x20 Var,\n\
         \x20 buildModule,\n\
         \x20 forEachUnitList,\n\
         \x20 when,\n\
         \x20 whileLoop,\n\
         } from \"{root}/ts/index.ts\";\n\
         import { CP_ADDR, readCurrentPlayerRegister } from \"{root}/ts/cp.ts\";\n\
         import { AddrReloc, emitVarStore } from \"{root}/ts/hir.ts\";\n\
         import { inCunitPool, readCunitPointerSaved } from \"{root}/ts/view.ts\";";

fn write_py(dir: &Path, name: &str, body: &str) -> PathBuf {
    common::write_py(dir, name, PY_IMPORTS, body)
}

fn write_ts(dir: &Path, name: &str, body: &str) -> PathBuf {
    let imports = TS_IMPORTS.replace("{root}", &repo_root_ts());
    common::write_ts(dir, name, &imports, body)
}

fn repo_root_ts() -> String {
    common::repo_root().display().to_string()
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

fn u_chain() -> Vec<(u32, u32)> {
    vec![
        (U_HEAD, A_ADDR),
        (A_EPD + 1, B_ADDR),
        (B_EPD + 1, C_ADDR),
        (C_EPD + 1, 0),
    ]
}

fn d_chain() -> Vec<(u32, u32)> {
    vec![
        (D_TAIL, C_ADDR),
        (C_EPD, B_ADDR),
        (B_EPD, A_ADDR),
        (A_EPD, 0),
    ]
}

fn py_count(list_name: &str, extra: &str) -> String {
    format!(
        "    count = Var(0)\n\
         \x20   def body(unit, loop):\n\
         \x20       count.iadd(1)\n{extra}\n\
         \x20   for_each_unit_list(\"{list_name}\", body)\n"
    )
}

fn ts_count(list_name: &str, extra: &str) -> String {
    format!(
        "  const count = Var(0);\n\
         \x20 forEachUnitList(\"{list_name}\", (unit, loop) => {{\n\
         \x20   count.iadd(1);\n{extra}\n\
         \x20 }});\n"
    )
}

#[test]
fn u_walk_is_forward_three_nodes() {
    let dir = tempdir("unit-list-u-");
    let (py, _ts) = run_case(
        dir.path(),
        "u_forward",
        &py_count("U", ""),
        &ts_count("U", ""),
        &u_chain(),
    );
    assert_eq!(py.count, 3);
}

#[test]
fn d_walk_is_backward_three_nodes() {
    let dir = tempdir("unit-list-d-");
    let (py, _ts) = run_case(
        dir.path(),
        "d_backward",
        &py_count("D", ""),
        &ts_count("D", ""),
        &d_chain(),
    );
    assert_eq!(py.count, 3);
}

#[test]
fn forward_walk_from_the_stale_d_head_is_empty() {
    // `0x59CC98` (EPD 19021) is the old forward D head; on Remastered it is
    // not a usable head and a forward walk from it is empty. The backward walk
    // from tail `0x62842C` finds all three nodes, which pins the direction
    // claim: a forward D implementation silently misses every death.
    let dir = tempdir("unit-list-d-forward-");
    let py_body = "    count = Var(0)\n\
                   \x20   saved = read_current_player_register()\n\
                   \x20   ptr = Var(0)\n\
                   \x20   ptr.set(read_cunit_pointer_saved(saved, 19021))\n\
                   \x20   done = Var(0)\n\
                   \x20   def iteration():\n\
                   \x20       count.iadd(1)\n\
                   \x20       nxt = read_cunit_pointer_saved(saved, ptr.add(1))\n\
                   \x20       when([done.eq(0)], lambda: ptr.set(nxt))\n\
                   \x20   while_loop([*in_cunit_pool(ptr), done.eq(0)], iteration)\n\
                   \x20   emitVarStore(saved.id, AddrReloc(CP_ADDR))\n";
    let ts_body = "  const count = Var(0);\n\
                   \x20 const saved = readCurrentPlayerRegister();\n\
                   \x20 const ptr = Var(0);\n\
                   \x20 ptr.set(readCunitPointerSaved(saved, 19021));\n\
                   \x20 const done = Var(0);\n\
                   \x20 whileLoop([...inCunitPool(ptr), done.eq(0)], () => {\n\
                   \x20   count.iadd(1);\n\
                   \x20   const nxt = readCunitPointerSaved(saved, ptr.add(1));\n\
                   \x20   when([done.eq(0)], () => ptr.set(nxt));\n\
                   \x20 });\n\
                   \x20 emitVarStore(saved.id, AddrReloc(CP_ADDR));\n";
    let (py, _ts) = run_case(dir.path(), "d_forward", py_body, ts_body, &d_chain());
    assert_eq!(py.count, 0);
}

#[test]
fn empty_list_runs_zero_iterations_and_restores_cp() {
    let dir = tempdir("unit-list-empty-");
    let seeds = vec![(U_HEAD, 0), (CP_EPD, 0x1234_5678)];
    let py_path = write_py(dir.path(), "empty.py", &py_count("U", ""));
    let ts_path = write_ts(dir.path(), "empty.ts", &ts_count("U", ""));
    let py = common::run_one(Frontend::Py, &py_path, &seeds);
    let ts = common::run_one(Frontend::Ts, &ts_path, &seeds);
    assert_eq!(py.count, 0);
    assert_eq!(ts.count, 0);
    assert_eq!(py.cp_value, 0x1234_5678, "py: restore must run");
    assert_eq!(ts.cp_value, 0x1234_5678, "ts: restore must run");
}

#[test]
fn break_gated_on_the_first_node_runs_once() {
    let dir = tempdir("unit-list-break-");
    let py = py_count("U", "        when([unit.eq(19025)], loop.break_)");
    let ts = ts_count("U", "    when([unit.eq(19025)], () => loop.break_());");
    let (py, _ts) = run_case(dir.path(), "break_first", &py, &ts, &u_chain());
    assert_eq!(py.count, 1);
}

#[test]
fn reentrant_reads_the_successor_before_a_body_frees_the_node() {
    let dir = tempdir("unit-list-reentrant-");
    let free_py = "        when([unit.eq(19025)], lambda: SetDeaths(19026, SetTo, 0, 0))";
    let free_ts = "    when([unit.eq(19025)], () => SetDeaths(19026, SetTo, 0, 0));";

    let pre_py = format!(
        "    count = Var(0)\n\
         \x20   def body(unit, loop):\n\
         \x20       count.iadd(1)\n{free_py}\n\
         \x20   for_each_unit_list(\"U\", body, reentrant=True)\n"
    );
    let pre_ts = format!(
        "  const count = Var(0);\n\
         \x20 forEachUnitList(\"U\", (unit, loop) => {{\n\
         \x20   count.iadd(1);\n{free_ts}\n\
         \x20 }}, {{ reentrant: true }});\n"
    );
    let (pre, _) = run_case(dir.path(), "reentrant_pre", &pre_py, &pre_ts, &u_chain());
    assert_eq!(pre.count, 3, "pre-read must keep the successor in hand");

    let post_py = format!(
        "    count = Var(0)\n\
         \x20   def body(unit, loop):\n\
         \x20       count.iadd(1)\n{free_py}\n\
         \x20   for_each_unit_list(\"U\", body)\n"
    );
    let post_ts = format!(
        "  const count = Var(0);\n\
         \x20 forEachUnitList(\"U\", (unit, loop) => {{\n\
         \x20   count.iadd(1);\n{free_ts}\n\
         \x20 }});\n"
    );
    let (post, _) = run_case(dir.path(), "reentrant_post", &post_py, &post_ts, &u_chain());
    assert_eq!(post.count, 1, "post-read sees the freed link");
}

#[test]
fn per_node_cost_is_pinned_for_u_and_d() {
    let dir = tempdir("unit-list-cost-");
    let (u0, _) = run_case(
        dir.path(),
        "u_cost_empty",
        &py_count("U", ""),
        &ts_count("U", ""),
        &[(U_HEAD, 0)],
    );
    let (u3, _) = run_case(
        dir.path(),
        "u_cost_three",
        &py_count("U", ""),
        &ts_count("U", ""),
        &u_chain(),
    );
    let (d0, _) = run_case(
        dir.path(),
        "d_cost_empty",
        &py_count("D", ""),
        &ts_count("D", ""),
        &[(D_TAIL, 0)],
    );
    let (d3, _) = run_case(
        dir.path(),
        "d_cost_three",
        &py_count("D", ""),
        &ts_count("D", ""),
        &d_chain(),
    );

    let u_per_node = (u3.cost.triggers - u0.cost.triggers) / 3;
    let d_per_node = (d3.cost.triggers - d0.cost.triggers) / 3;
    // U pays two extra triggers per node to materialise `ptr + 1`; D reads
    // `prev` at `ptr + 0`. Both stay inside ADR 0015's ~35–40T per-node row.
    assert_eq!(u_per_node, 36, "U per-node executed triggers");
    assert_eq!(d_per_node, 34, "D per-node executed triggers");
}

#[test]
fn frontends_agree_on_the_emitted_walk() {
    let dir = tempdir("unit-list-parity-");
    for (label, list_name) in [("parity_u", "U"), ("parity_d", "D")] {
        let py_path = write_py(dir.path(), &format!("{label}.py"), &py_count(list_name, ""));
        let ts_path = write_ts(dir.path(), &format!("{label}.ts"), &ts_count(list_name, ""));
        let synth = |path: &Path| {
            Command::new(env!("CARGO_BIN_EXE_eudkit"))
                .args(["synth", path.to_str().unwrap(), "--compact"])
                .env("EUDKIT_ROOT", repo_root())
                .output()
                .expect("run eudkit synth")
        };
        let py = synth(&py_path);
        let ts = synth(&ts_path);
        assert!(
            py.status.success(),
            "{label}: py synth failed: {}",
            String::from_utf8_lossy(&py.stderr)
        );
        assert!(
            ts.status.success(),
            "{label}: ts synth failed: {}",
            String::from_utf8_lossy(&ts.stderr)
        );
        assert_eq!(
            py.stdout, ts.stdout,
            "{label}: Py and TS must emit byte-identical HIR"
        );
    }
}
