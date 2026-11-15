//! T-ABS-RAND: runtime parity and pinned cost gates for the per-builder LCG.
//!
//! The reference is a four-line LCG in this test, not another eudkit helper.
//! Each frontend writes its draws into external EPD markers, so the assertions
//! cover both the emitted gated adds and the simulator's execution. The
//! `randomize` case seeds the real game-tick EPD before stepping.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit::{Frontend, capture_source, prepare_source};
use eudkit_lir::{EPD_BASE, SimulationState, epd_u32, init_simulation};
use serde_json::Value;

mod common;

// Far past any realistic payload length: `write_epd` spills to `external_epds`
// only when the address is outside `payload.data`, and the 64-draw range case
// alone emits ~21 MB of trigger records.
const MARKER_EPD: u32 = 8_000_000;
const GAME_TICK_ADDR: u32 = 0x0057_F23C;
const MULT: u32 = 1_103_515_245;
const INC: u32 = 12_345;

const PY_IMPORTS: &str = "from py import Var, build_module\n\
         from py.hir import AddrReloc, emitVarStore\n\
         from py.random import srand, rand, dwrand, get_seed, rand_range, randomize";

fn ts_imports() -> String {
    let root = common::repo_root().display();
    format!(
        "import {{ AddrReloc, Var, buildModule, emitVarStore }} from \"{root}/ts/index.ts\";\n\
         import {{ srand, rand, dwrand, getSeed, randRange, randomize }} from \"{root}/ts/random.ts\";"
    )
}

fn next_state(state: u32) -> u32 {
    state.wrapping_mul(MULT).wrapping_add(INC)
}

fn marker_addr(epd: u32) -> u32 {
    EPD_BASE.wrapping_add(epd.wrapping_mul(4))
}

fn marker(sim: &SimulationState, index: u32) -> u32 {
    sim.external_epds
        .get(&(MARKER_EPD.wrapping_add(index)))
        .copied()
        .unwrap_or(0)
}

fn write_pair(dir: &Path, stem: &str, py_body: &str, ts_body: &str) -> (PathBuf, PathBuf) {
    let py = common::write_py(dir, &format!("{stem}.py"), PY_IMPORTS, py_body);
    let ts = common::write_ts(dir, &format!("{stem}.ts"), &ts_imports(), ts_body);
    (py, ts)
}

fn run(frontend: Frontend, path: &Path, seeds: &[(u32, u32)]) -> SimulationState {
    let prepared = prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    sim.step_loop();
    sim
}

fn rand_bodies(seed: u32, count: u32) -> (String, String) {
    let mut py = format!("    srand({seed})\n");
    let mut ts = format!("  srand({seed});\n");
    for index in 0..count {
        let addr = marker_addr(MARKER_EPD.wrapping_add(index));
        py.push_str(&format!(
            "    r{index} = rand()\n    emitVarStore(r{index}.id, AddrReloc({addr}))\n"
        ));
        ts.push_str(&format!(
            "  const r{index} = rand();\n  emitVarStore(r{index}.id, AddrReloc({addr}));\n"
        ));
    }
    (py, ts)
}

#[test]
fn rand_sequence_matches_the_reference_and_both_frontends_agree() {
    let dir = common::tempdir("random-seq-");
    for seed in [0, 1, 12_345, 0xFFFF_FFFF] {
        let (py_body, ts_body) = rand_bodies(seed, 5);
        let (py_path, ts_path) =
            write_pair(dir.path(), &format!("seq_{seed}"), &py_body, &ts_body);

        let py_hir = capture_source(&py_path, Frontend::Py).expect("capture Python HIR");
        let ts_hir = capture_source(&ts_path, Frontend::Ts).expect("capture TS HIR");
        assert_eq!(
            py_hir, ts_hir,
            "seed {seed}: frontends must emit byte-identical HIR"
        );

        // The ticket asks for `common::run_case`; it also pins executed cost.
        common::run_case(
            &format!("rand seed {seed}"),
            &py_path,
            &ts_path,
            &[],
        );

        let py_sim = run(Frontend::Py, &py_path, &[]);
        let ts_sim = run(Frontend::Ts, &ts_path, &[]);
        let mut state = seed;
        for index in 0..5 {
            state = next_state(state);
            let want = state >> 16;
            assert_eq!(marker(&py_sim, index), want, "seed {seed} draw {index} (Py)");
            assert_eq!(marker(&ts_sim, index), want, "seed {seed} draw {index} (TS)");
        }
    }
}

#[test]
fn dwrand_joins_two_draw_hiwords_and_leaves_the_second_state() {
    let dir = common::tempdir("random-dwrand-");
    let addr0 = marker_addr(MARKER_EPD);
    let addr1 = marker_addr(MARKER_EPD.wrapping_add(1));
    let py_body = format!(
        "    srand(0x12345678)\n\
         \x20   draw = dwrand()\n\
         \x20   emitVarStore(draw.id, AddrReloc({addr0}))\n\
         \x20   state = get_seed()\n\
         \x20   emitVarStore(state.id, AddrReloc({addr1}))\n"
    );
    let ts_body = format!(
        "  srand(0x12345678);\n\
         \x20 const draw = dwrand();\n\
         \x20 emitVarStore(draw.id, AddrReloc({addr0}));\n\
         \x20 const state = getSeed();\n\
         \x20 emitVarStore(state.id, AddrReloc({addr1}));\n"
    );
    let (py_path, ts_path) = write_pair(dir.path(), "dwrand", &py_body, &ts_body);

    let first = next_state(0x1234_5678);
    let second = next_state(first);
    let want = (first & 0xFFFF_0000) | (second >> 16);

    for (label, frontend, path) in [
        ("python", Frontend::Py, py_path),
        ("typescript", Frontend::Ts, ts_path),
    ] {
        let sim = run(frontend, &path, &[]);
        assert_eq!(marker(&sim, 0), want, "{label}: dwrand value");
        assert_eq!(marker(&sim, 1), second, "{label}: state after two draws");
    }
}

#[test]
fn rand_range_stays_in_bounds_and_the_fold_consumes_no_draw() {
    let dir = common::tempdir("random-range-");
    let mut py = String::from("    srand(123)\n");
    let mut ts = String::from("  srand(123);\n");
    for index in 0..64u32 {
        let addr = marker_addr(MARKER_EPD.wrapping_add(index));
        py.push_str(&format!(
            "    r{index} = rand_range(7, 19)\n    emitVarStore(r{index}.id, AddrReloc({addr}))\n"
        ));
        ts.push_str(&format!(
            "  const r{index} = randRange(7, 19);\n  emitVarStore(r{index}.id, AddrReloc({addr}));\n"
        ));
    }
    let (py_path, ts_path) = write_pair(dir.path(), "range", &py, &ts);
    let py_sim = run(Frontend::Py, &py_path, &[]);
    let ts_sim = run(Frontend::Ts, &ts_path, &[]);
    for index in 0..64 {
        let value = marker(&py_sim, index);
        assert!(
            (7..=19).contains(&value),
            "draw {index} out of range: {value}"
        );
        assert_eq!(
            value,
            marker(&ts_sim, index),
            "draw {index}: frontends disagree"
        );
    }

    let addr0 = marker_addr(MARKER_EPD);
    let addr1 = marker_addr(MARKER_EPD.wrapping_add(1));
    let py_fold = format!(
        "    srand(123)\n\
         \x20   value = rand_range(5, 5)\n\
         \x20   emitVarStore(value.id, AddrReloc({addr0}))\n\
         \x20   state = get_seed()\n\
         \x20   emitVarStore(state.id, AddrReloc({addr1}))\n"
    );
    let ts_fold = format!(
        "  srand(123);\n\
         \x20 const value = randRange(5, 5);\n\
         \x20 emitVarStore(value.id, AddrReloc({addr0}));\n\
         \x20 const state = getSeed();\n\
         \x20 emitVarStore(state.id, AddrReloc({addr1}));\n"
    );
    let (py_path, ts_path) = write_pair(dir.path(), "range_fold", &py_fold, &ts_fold);
    for (label, frontend, path) in [
        ("python", Frontend::Py, py_path),
        ("typescript", Frontend::Ts, ts_path),
    ] {
        let sim = run(frontend, &path, &[]);
        assert_eq!(marker(&sim, 0), 5, "{label}: folded value");
        assert_eq!(marker(&sim, 1), 123, "{label}: fold must not advance");
    }
}

#[test]
fn randomize_folds_the_seeded_game_tick_into_the_state() {
    let dir = common::tempdir("random-tick-");
    let addr = marker_addr(MARKER_EPD);
    let py_body = format!(
        "    srand(0)\n\
         \x20   randomize()\n\
         \x20   state = get_seed()\n\
         \x20   emitVarStore(state.id, AddrReloc({addr}))\n"
    );
    let ts_body = format!(
        "  srand(0);\n\
         \x20 randomize();\n\
         \x20 const state = getSeed();\n\
         \x20 emitVarStore(state.id, AddrReloc({addr}));\n"
    );
    let (py_path, ts_path) = write_pair(dir.path(), "randomize", &py_body, &ts_body);

    for tick in [0u32, 1, 0xDEAD_BEEF] {
        let seeds = [(epd_u32(GAME_TICK_ADDR), tick)];
        let expected = next_state(tick);
        for (label, frontend, path) in [
            ("python", Frontend::Py, py_path.as_path()),
            ("typescript", Frontend::Ts, ts_path.as_path()),
        ] {
            let sim = run(frontend, path, &seeds);
            assert_eq!(
                marker(&sim, 0),
                expected,
                "{label}: tick {tick:#010X} state"
            );
            assert_eq!(
                marker(&sim, 0) >> 16,
                expected >> 16,
                "{label}: tick {tick:#010X} first draw"
            );
        }
    }
}

fn eudkit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(args)
        .current_dir(common::repo_root())
        .env("EUDKIT_ROOT", common::repo_root())
        .output()
        .expect("run eudkit")
}

fn cost_report(path: &Path) -> Value {
    let out = eudkit(&["dump-ir", path.to_str().unwrap(), "--json", "--loops", "1"]);
    assert!(
        out.status.success(),
        "{}: dump-ir failed: {}",
        path.display(),
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "{}: invalid dump-ir JSON: {err}",
            path.display()
        )
    })
}

#[test]
fn per_site_costs_are_pinned_and_under_the_ticket_ceilings() {
    let dir = common::tempdir("random-cost-");
    // (name, Python body, TS body, pinned static, pinned executed,
    //  static ceiling, executed ceiling)
    let sites: [(&str, &str, &str, u64, u64, u64, u64); 7] = [
        ("srand_const", "    srand(42)\n", "  srand(42);\n", 1, 1, 3, 3),
        (
            "srand_runtime",
            "    v = Var(0)\n    v.set(42)\n    srand(v)\n",
            "  const v = Var(0);\n  v.set(42);\n  srand(v);\n",
            3,
            3,
            3,
            3,
        ),
        (
            "get_seed",
            "    srand(0)\n    s = get_seed()\n",
            "  srand(0);\n  const s = getSeed();\n",
            1,
            1,
            3,
            3,
        ),
        (
            "rand",
            "    srand(0)\n    r = rand()\n",
            "  srand(0);\n  const r = rand();\n",
            52,
            52,
            200,
            180,
        ),
        (
            "dwrand",
            "    srand(0)\n    r = dwrand()\n",
            "  srand(0);\n  const r = dwrand();\n",
            103,
            104,
            400,
            350,
        ),
        (
            "randomize",
            "    srand(0)\n    randomize()\n",
            "  srand(0);\n  randomize();\n",
            70,
            71,
            240,
            220,
        ),
        (
            "rand_range",
            "    srand(0)\n    r = rand_range(7, 19)\n",
            "  srand(0);\n  const r = randRange(7, 19);\n",
            137,
            141,
            470,
            400,
        ),
    ];

    for (name, py_body, ts_body, static_pin, executed_pin, static_max, executed_max) in sites {
        let (py_path, ts_path) = write_pair(dir.path(), name, py_body, ts_body);
        let py = cost_report(&py_path);
        let ts = cost_report(&ts_path);
        assert_eq!(py, ts, "{name}: frontends must report identical costs");

        let static_triggers = py["static_cost"]["triggers"].as_u64().unwrap();
        let executed_triggers = py["executed_cost"]["triggers"].as_u64().unwrap();
        assert_eq!(static_triggers, static_pin, "{name}: pinned static");
        assert_eq!(executed_triggers, executed_pin, "{name}: pinned executed");
        assert!(
            static_triggers <= static_max,
            "{name}: static {static_triggers} exceeds ceiling {static_max}"
        );
        assert!(
            executed_triggers <= executed_max,
            "{name}: executed {executed_triggers} exceeds ceiling {executed_max}"
        );
    }
}
