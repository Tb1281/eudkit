//! T-ABS-VIEW-POOL: simulator proof for `CUnitView`, the raw bit helpers, and
//! `ObjectPool`.
//!
//! The engine semantics live in the simulator, so this suite is the only place
//! the field masks, location packing, pointer links, sprite lane, and free-list
//! order are exercised against seeded memory. Every runtime fixture is emitted
//! from both frontends, asserted byte-identical, and executed to the same
//! values and trigger counts. The cost test pins every §5 ceiling through
//! `dump-ir --json --loops 1`, including the untouched 20T reader goldens.

use std::path::Path;
use std::process::{Command, Output};

use eudkit::{Frontend, capture_source, prepare_source};
use eudkit_lir::{ExecutedCost, init_simulation};
use serde_json::Value;

mod common;
use common::{repo_root, tempdir};

/// `EPD(0x59CCA8)`, the first CUnit slot.
const CUNIT_BASE_EPD: u32 = 19_025;
/// CUnit slots are 84 dwords (336 bytes) apart.
const CUNIT_STRIDE_EPDS: u32 = 84;
/// `0x59CCA8`, the first CUnit slot as a byte pointer.
const CUNIT_BASE_BYTES: u32 = 0x59_CCA8;
/// `EPD(0x58DC4C)`, the first scenario location record.
const LOCATION_EPD: u32 = 3_642;
/// Each location record is five dwords; dwords 0 and 1 are left/top.
const LOCATION_STRIDE_EPDS: u32 = 5;
/// Sentinel returned by `ObjectPool.allocate` when exhausted.
const POOL_NULL: u32 = 0xFFFF_FFFF;
/// First external EPD used as a result marker.
const MARKER_EPD: u32 = 9_000;

fn py_imports() -> &'static str {
    "from py import Var, build_module, when\n\
     from py.hir import AddrReloc, emitVarStore\n\
     from py.queue import ObjectPool\n\
     from py.view import (\n\
     \x20   CUnitView, in_cunit_pool, read_bit, read_bit_var, split_dword,\n\
     \x20   split_word, write_bit, write_mask,\n\
     )"
}

fn ts_imports() -> String {
    let root = repo_root().display();
    format!(
        "import {{ Var, buildModule, when, CUnitView, ObjectPool, AddrReloc, \
         emitVarStore, inCunitPool, readBit, readBitVar, splitDword, splitWord, \
         writeBit, writeMask }} from \"{root}/ts/index.ts\";"
    )
}

fn marker_addr(index: u32) -> u32 {
    eudkit_lir::EPD_BASE + (MARKER_EPD + index) * 4
}

fn py_marker(name: &str, index: u32) -> String {
    format!("    emitVarStore({name}.id, AddrReloc({}))\n", marker_addr(index))
}

fn ts_marker(name: &str, index: u32) -> String {
    format!(
        "  emitVarStore({name}.id, AddrReloc({}));\n",
        marker_addr(index)
    )
}

struct Observation {
    vars: Vec<u32>,
    memory: Vec<u32>,
    cost: ExecutedCost,
}

fn observe(
    frontend: Frontend,
    path: &Path,
    seeds: &[(u32, u32)],
    memory: &[u32],
    var_count: usize,
) -> Observation {
    let prepared = prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let var_ids: Vec<usize> = prepared
        .doc
        .module
        .vars
        .iter()
        .map(|var| var.id)
        .take(var_count)
        .collect();
    assert_eq!(
        var_ids.len(),
        var_count,
        "{}: fewer variables than expected",
        path.display()
    );

    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    let cost = sim.step_loop();

    Observation {
        vars: var_ids
            .iter()
            .map(|id| common::read_var(&prepared, &sim, *id))
            .collect(),
        memory: memory.iter().map(|epd| sim.read_epd(*epd)).collect(),
        cost,
    }
}

/// Emit, compare, simulate, and compare one fixture on both frontends.
fn run_pair(
    stem: &str,
    py_body: &str,
    ts_body: &str,
    seeds: &[(u32, u32)],
    memory: &[u32],
    var_count: usize,
) -> (Observation, Observation) {
    let dir = tempdir("view-pool-");
    let py = common::write_py(dir.path(), &format!("{stem}.py"), py_imports(), py_body);
    let ts = common::write_ts(dir.path(), &format!("{stem}.ts"), &ts_imports(), ts_body);

    let py_hir = capture_source(&py, Frontend::Py).expect("capture Python HIR");
    let ts_hir = capture_source(&ts, Frontend::Ts).expect("capture TS HIR");
    assert_eq!(
        py_hir, ts_hir,
        "{stem}: frontends must emit byte-identical HIR"
    );

    let py_obs = observe(Frontend::Py, &py, seeds, memory, var_count);
    let ts_obs = observe(Frontend::Ts, &ts, seeds, memory, var_count);
    assert_eq!(py_obs.vars, ts_obs.vars, "{stem}: variables disagree");
    assert_eq!(py_obs.memory, ts_obs.memory, "{stem}: memory disagrees");
    assert_eq!(
        py_obs.cost.triggers, ts_obs.cost.triggers,
        "{stem}: executed triggers disagree"
    );
    assert_eq!(
        py_obs.cost.actions, ts_obs.cost.actions,
        "{stem}: executed actions disagree"
    );
    (py_obs, ts_obs)
}

#[test]
fn give_to_writes_only_the_owner_byte() {
    let base = CUNIT_BASE_EPD;
    let (py, _) = run_pair(
        "owner",
        &format!("    u = CUnitView({base})\n    u.give_to(3)\n"),
        &format!("  new CUnitView({base}).giveTo(3);\n"),
        &[
            (base + 18, 0x1122_3344),
            (base + 19, 0xAABB_CCDD),
            (base + 20, 0x5566_7788),
        ],
        &[base + 18, base + 19, base + 20],
        0,
    );
    // Byte 76 is the low lane of dword 19; bytes 75/77/78 stay put.
    assert_eq!(py.memory, vec![0x1122_3344, 0xAABB_CC03, 0x5566_7788]);
}

#[test]
fn die_and_remove_clear_and_mark_the_right_bytes() {
    let first = CUNIT_BASE_EPD;
    let second = CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS;
    let (py, _) = run_pair(
        "order-remove",
        &format!("    CUnitView({first}).die()\n    CUnitView({second}).remove()\n"),
        &format!(
            "  new CUnitView({first}).die();\n  new CUnitView({second}).remove();\n"
        ),
        &[
            (first + 19, 0x1122_3344),
            (first + 36, 0xDEAD_BEEF),
            (second + 19, 0x5566_7788),
            (second + 36, 0xDEAD_BEEF),
        ],
        &[first + 19, first + 36, second + 19, second + 36],
        0,
    );
    assert_eq!(
        py.memory,
        vec![0x1122_0044, 0xDEAD_BEEF, 0x5566_0088, 0x04AD_BEEF]
    );
    assert_eq!(py.memory[3] >> 24, 4, "userActionFlags byte 147 must be 4");
}

#[test]
fn set_invincible_preserves_other_status_bits_and_is_burrowed_reads_bit_four() {
    let first = CUNIT_BASE_EPD;
    let second = CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS;
    let (py, _) = run_pair(
        "flags",
        &format!(
            "    CUnitView({first}).set_invincible()\n\
             \x20   b0 = CUnitView({first}).is_burrowed()\n\
             \x20   b1 = CUnitView({second}).is_burrowed()\n"
        ),
        &format!(
            "  new CUnitView({first}).setInvincible();\n\
             \x20 const b0 = new CUnitView({first}).isBurrowed();\n\
             \x20 const b1 = new CUnitView({second}).isBurrowed();\n"
        ),
        &[(first + 55, 0x0000_0011), (second + 55, 0)],
        &[first + 55],
        2,
    );
    assert_eq!(py.memory[0], 0x0400_0011);
    assert_eq!(py.vars, vec![1, 0]);
}

#[test]
fn set_location_packs_left_and_top_into_pos() {
    let base = CUNIT_BASE_EPD;
    let record = LOCATION_EPD + 7 * LOCATION_STRIDE_EPDS;
    let (py, _) = run_pair(
        "location",
        &format!("    CUnitView({base}).set_location(7)\n"),
        &format!("  new CUnitView({base}).setLocation(7);\n"),
        &[(base + 10, 0xFFFF_FFFF), (record, 123), (record + 1, 456)],
        &[base + 10],
        0,
    );
    assert_eq!(py.memory[0], (456 << 16) | 123);
}

#[test]
fn links_read_the_slot_biased_pointer_and_reject_null() {
    let first = CUNIT_BASE_EPD;
    let second = CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS;
    let next_ptr = CUNIT_BASE_BYTES + 336 * 3;
    let prev_ptr = CUNIT_BASE_BYTES + 336 * 5;
    let (py, _) = run_pair(
        "links",
        &format!(
            "    n = CUnitView({first}).next_unit()\n\
             \x20   p = CUnitView({first}).prev_unit()\n\
             \x20   nul = CUnitView({second}).next_unit()\n\
             \x20   hit = Var(0)\n\
             \x20   when([*in_cunit_pool(nul), hit.eq(0)], lambda: hit.iadd(1))\n"
        ),
        &format!(
            "  const n = new CUnitView({first}).nextUnit();\n\
             \x20 const p = new CUnitView({first}).prevUnit();\n\
             \x20 const nul = new CUnitView({second}).nextUnit();\n\
             \x20 const hit = Var(0);\n\
             \x20 when([...inCunitPool(nul), hit.eq(0)], () => {{ hit.iadd(1); }});\n"
        ),
        &[(first + 1, next_ptr), (first, prev_ptr), (second + 1, 0)],
        &[],
        4,
    );
    assert_eq!(
        py.vars,
        vec![
            CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS * 3,
            CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS * 5,
            0xFFF9_D729,
            // `in_cunit_pool` rejects the sentinel through the upper bound, so
            // the gated body never runs (the `cunit_reader.rs` convention).
            0,
        ]
    );
}

#[test]
fn set_color_writes_the_sprite_player_lane_only() {
    let base = CUNIT_BASE_EPD;
    let sprite_epd = CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS * 7;
    let sprite_ptr = CUNIT_BASE_BYTES + 336 * 7;
    let (py, _) = run_pair(
        "color",
        &format!("    CUnitView({base}).set_color(2)\n"),
        &format!("  new CUnitView({base}).setColor(2);\n"),
        &[
            (base + 3, sprite_ptr),
            (sprite_epd + 1, 0x5566_7788),
            (sprite_epd + 2, 0x11AA_2233),
            (sprite_epd + 3, 0x99AA_BBCC),
        ],
        &[sprite_epd + 1, sprite_epd + 2, sprite_epd + 3],
        0,
    );
    // `bytes[10]` is lane 2 of the sprite's dword 2; its neighbours are intact.
    assert_eq!(
        py.memory,
        vec![0x5566_7788, 0x1102_2233, 0x99AA_BBCC]
    );
}

#[test]
fn object_pool_allocates_in_lifo_order_then_returns_null() {
    let mut py_body = String::from("    pool = ObjectPool(4)\n");
    let mut ts_body = String::from("  const pool = new ObjectPool(4);\n");
    for index in 0..5 {
        py_body.push_str(&format!("    a{index} = pool.allocate()\n"));
        py_body.push_str(&py_marker(&format!("a{index}"), index));
        ts_body.push_str(&format!("  const a{index} = pool.allocate();\n"));
        ts_body.push_str(&ts_marker(&format!("a{index}"), index));
    }
    py_body.push_str("    pool.release(1)\n");
    py_body.push_str("    a5 = pool.allocate()\n");
    py_body.push_str(&py_marker("a5", 5));
    ts_body.push_str("  pool.release(1);\n");
    ts_body.push_str("  const a5 = pool.allocate();\n");
    ts_body.push_str(&ts_marker("a5", 5));

    let markers: Vec<u32> = (0..6).map(|index| MARKER_EPD + index).collect();
    let (py, _) = run_pair(
        "pool",
        &py_body,
        &ts_body,
        &[],
        &markers,
        0,
    );
    assert_eq!(py.memory, vec![0, 1, 2, 3, POOL_NULL, 1]);
}

#[test]
fn whole_fixture_matches_and_executes_identically() {
    let dir = tempdir("view-pool-whole-");
    let base = CUNIT_BASE_EPD;
    let sprite_epd = CUNIT_BASE_EPD + CUNIT_STRIDE_EPDS * 7;
    let sprite_ptr = CUNIT_BASE_BYTES + 336 * 7;
    let record = LOCATION_EPD + 7 * LOCATION_STRIDE_EPDS;

    let py_body = format!(
        "    hit = Var(0)\n\
         \x20   u = CUnitView({base})\n\
         \x20   u.give_to(3)\n\
         \x20   u.die()\n\
         \x20   u.remove()\n\
         \x20   u.set_invincible()\n\
         \x20   u.set_location(7)\n\
         \x20   u.set_color(2)\n\
         \x20   n = u.next_unit()\n\
         \x20   b = u.is_burrowed()\n\
         \x20   pool = ObjectPool(4)\n\
         \x20   slot = pool.allocate()\n\
         \x20   when(b.eq(1), lambda: hit.iadd(1))\n"
    );
    let ts_body = format!(
        "  const hit = Var(0);\n\
         \x20 const u = new CUnitView({base});\n\
         \x20 u.giveTo(3);\n\
         \x20 u.die();\n\
         \x20 u.remove();\n\
         \x20 u.setInvincible();\n\
         \x20 u.setLocation(7);\n\
         \x20 u.setColor(2);\n\
         \x20 const n = u.nextUnit();\n\
         \x20 const b = u.isBurrowed();\n\
         \x20 const pool = new ObjectPool(4);\n\
         \x20 const slot = pool.allocate();\n\
         \x20 when(b.eq(1), () => {{ hit.iadd(1); }});\n"
    );

    let py = common::write_py(dir.path(), "whole.py", py_imports(), &py_body);
    let ts = common::write_ts(dir.path(), "whole.ts", &ts_imports(), &ts_body);

    let py_hir = capture_source(&py, Frontend::Py).expect("capture Python HIR");
    let ts_hir = capture_source(&ts, Frontend::Ts).expect("capture TS HIR");
    assert_eq!(py_hir, ts_hir, "whole fixture: frontends must agree");

    let seeds = [
        (base + 3, sprite_ptr),
        (base + 55, 0x10),
        (sprite_epd + 2, 0x00AA_0000),
        (record, 123),
        (record + 1, 456),
    ];
    let (py_case, ts_case) = common::run_case("whole", &py, &ts, &seeds);
    assert_eq!(py_case.count, 1, "the burrowed hit must fire exactly once");
    assert_eq!(py_case.cost.triggers, ts_case.cost.triggers);
    assert_eq!(py_case.cost.conditions, ts_case.cost.conditions);
    assert_eq!(py_case.cost.actions, ts_case.cost.actions);
}

fn eudkit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(args)
        .env("EUDKIT_ROOT", repo_root())
        .output()
        .expect("run eudkit")
}

fn dump_cost(path: &Path) -> Value {
    let out = eudkit(&[
        "dump-ir",
        path.to_str().unwrap(),
        "--json",
        "--loops",
        "1",
    ]);
    assert!(
        out.status.success(),
        "dump-ir failed for {}:\n{}",
        path.display(),
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("valid dump-ir JSON")
}

fn triggers(report: &Value, section: &str) -> u64 {
    report[section]["triggers"]
        .as_u64()
        .expect("trigger count")
}

fn cost_pair(stem: &str, py_body: &str, ts_body: &str) -> Value {
    let dir = tempdir("view-pool-cost-");
    let py = common::write_py(dir.path(), &format!("{stem}.py"), py_imports(), py_body);
    let ts = common::write_ts(dir.path(), &format!("{stem}.ts"), &ts_imports(), ts_body);

    let py_hir = capture_source(&py, Frontend::Py).expect("capture Python HIR");
    let ts_hir = capture_source(&ts, Frontend::Ts).expect("capture TS HIR");
    assert_eq!(py_hir, ts_hir, "{stem}: frontends must agree");

    let py_report = dump_cost(&py);
    let ts_report = dump_cost(&ts);
    assert_eq!(py_report, ts_report, "{stem}: dump-ir reports must match");
    assert_eq!(py_report["pre_assembly"], Value::Bool(true));
    py_report
}

fn assert_ceiling(
    stem: &str,
    py_body: &str,
    ts_body: &str,
    max_static: u64,
    max_exec: u64,
) {
    let report = cost_pair(stem, py_body, ts_body);
    let static_triggers = triggers(&report, "static_cost");
    let executed_triggers = triggers(&report, "executed_cost");
    assert!(
        static_triggers <= max_static,
        "{stem}: static triggers {static_triggers} > {max_static}"
    );
    assert!(
        executed_triggers <= max_exec,
        "{stem}: executed triggers {executed_triggers} > {max_exec}"
    );
}

#[test]
fn cost_gates_pin_every_section_five_row() {
    // split_* constant folds to two constant Vars and no triggers.
    assert_ceiling(
        "split-const",
        "    hi, lo = split_dword(0x12345678)\n    hi8, lo8 = split_word(0x1234)\n",
        "  const [hi, lo] = splitDword(0x12345678);\n  const [hi8, lo8] = splitWord(0x1234);\n",
        0,
        0,
    );

    // split_* runtime copies and masks once per half.
    assert_ceiling(
        "split-runtime",
        "    v = Var(0)\n    hi, lo = split_dword(v)\n    hi8, lo8 = split_word(v)\n",
        "  const v = Var(0);\n  const [hi, lo] = splitDword(v);\n  const [hi8, lo8] = splitWord(v);\n",
        48,
        48,
    );

    // read_bit contributes exactly one condition; the enclosing `when` owns
    // the single carrying trigger and the caller's `hit.iadd` the action. Rule
    // V1 keeps `read_bit` constant-address only.
    let report = cost_pair(
        "read-bit",
        "    hit = Var(0)\n    when(read_bit(0x58DC4C, 4), lambda: hit.iadd(1))\n",
        "  const hit = Var(0);\n  when(readBit(0x58dc4c, 4), () => { hit.iadd(1); });\n",
    );
    assert_eq!(report["static_cost"]["triggers"].as_u64(), Some(1));
    assert_eq!(report["static_cost"]["conditions"].as_u64(), Some(1));
    assert!(triggers(&report, "executed_cost") <= 1);

    assert_ceiling(
        "read-bit-var",
        "    c = read_bit_var(0x58DC4C, 4)\n",
        "  const c = readBitVar(0x58dc4c, 4);\n",
        2,
        2,
    );
    assert_ceiling(
        "write-bit",
        "    write_bit(0x58DC4C, 4, True)\n",
        "  writeBit(0x58dc4c, 4, true);\n",
        1,
        1,
    );
    assert_ceiling(
        "write-mask",
        "    write_mask(0x58DC4C, 0x1200, 0xFF00)\n",
        "  writeMask(0x58dc4c, 0x1200, 0xff00);\n",
        1,
        1,
    );

    // The 20T `read_cunit_pointer` goldens must not move.
    for (stem, py_call, ts_call) in [
        ("next", "next_unit", "nextUnit"),
        ("prev", "prev_unit", "prevUnit"),
    ] {
        let report = cost_pair(
            &format!("link-{stem}"),
            &format!("    CUnitView(19025).{py_call}()\n"),
            &format!("  new CUnitView(19025).{ts_call}();\n"),
        );
        assert_eq!(
            triggers(&report, "static_cost"),
            20,
            "{stem}: static reader golden moved"
        );
        assert_eq!(
            triggers(&report, "executed_cost"),
            20,
            "{stem}: executed reader golden moved"
        );
    }

    assert_ceiling(
        "sprite-epd",
        "    CUnitView(19025).sprite_epd()\n",
        "  new CUnitView(19025).spriteEpd();\n",
        36,
        36,
    );
    assert_ceiling(
        "burrowed-const",
        "    CUnitView(19025).is_burrowed()\n",
        "  new CUnitView(19025).isBurrowed();\n",
        2,
        2,
    );
    assert_ceiling(
        "burrowed-runtime",
        "    CUnitView(Var(19025)).is_burrowed()\n",
        "  new CUnitView(Var(19025)).isBurrowed();\n",
        80,
        70,
    );
    assert_ceiling(
        "give-const",
        "    CUnitView(19025).give_to(3)\n",
        "  new CUnitView(19025).giveTo(3);\n",
        1,
        1,
    );
    assert_ceiling(
        "die-const",
        "    CUnitView(19025).die()\n",
        "  new CUnitView(19025).die();\n",
        1,
        1,
    );

    // remove is two masked acts; even if a later packing change splits them,
    // the trigger count stays at or below the §5 ceiling of two.
    let report = cost_pair(
        "remove-const",
        "    CUnitView(19025).remove()\n",
        "  new CUnitView(19025).remove();\n",
    );
    assert!(triggers(&report, "static_cost") <= 2);
    assert_eq!(report["static_cost"]["actions"].as_u64(), Some(2));
    assert!(triggers(&report, "executed_cost") <= 2);

    assert_ceiling(
        "invincible-const",
        "    CUnitView(19025).set_invincible()\n",
        "  new CUnitView(19025).setInvincible();\n",
        1,
        1,
    );
    assert_ceiling(
        "masked-runtime",
        "    CUnitView(Var(19025)).give_to(2)\n",
        "  new CUnitView(Var(19025)).giveTo(2);\n",
        40,
        40,
    );
    assert_ceiling(
        "set-color",
        "    CUnitView(19025).set_color(2)\n",
        "  new CUnitView(19025).setColor(2);\n",
        90,
        90,
    );
    assert_ceiling(
        "set-location",
        "    CUnitView(19025).set_location(7)\n",
        "  new CUnitView(19025).setLocation(7);\n",
        200,
        180,
    );
    assert_ceiling(
        "set-location-runtime",
        "    CUnitView(Var(19025)).set_location(7)\n",
        "  new CUnitView(Var(19025)).setLocation(7);\n",
        200,
        180,
    );

    // Pool: init is ceil(capacity/64)+4, and allocate/release stay well under
    // their own §5 rows.
    assert_ceiling(
        "pool-init",
        "    ObjectPool(4)\n",
        "  new ObjectPool(4);\n",
        5,
        5,
    );
    assert_ceiling(
        "pool-allocate",
        "    pool = ObjectPool(4)\n    pool.allocate()\n",
        "  const pool = new ObjectPool(4);\n  pool.allocate();\n",
        60,
        60,
    );
    assert_ceiling(
        "pool-release",
        "    pool = ObjectPool(4)\n    pool.release(1)\n",
        "  const pool = new ObjectPool(4);\n  pool.release(1);\n",
        40,
        40,
    );
}
