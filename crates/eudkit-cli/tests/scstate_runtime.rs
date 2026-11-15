//! `T-ABS-SCDATA`: tech researched / upgrade level byte tables against the LIR
//! simulator.
//!
//! The four state bases sit outside the assembled payload, so the simulator
//! exposes them through `external_epds`. Seeding a dword with sentinel bytes in
//! the neighbour lanes proves the masked `SetDeathsX` write is byte-bounded;
//! the runtime-player fixtures seed a `Var` through `SetDeaths` so the four-arm
//! upgrade lane dispatch runs on real values. Both frontends go through the
//! shared capture/lowering pipeline and must emit byte-identical HIR.
//!
//! Costs are pinned from `dump-ir --json --loops 1`. The §5 constant rows are
//! measured on the bare call (the `tech_researched` condition is a detached HIR
//! node there; the frontend suites pin its one-condition shape). Runtime rows
//! are asserted against the §5 ceilings and the exact observed triple.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::Command;

use eudkit::{Frontend, PreparedProgram, capture_source, prepare_source};
use eudkit_lir::{ExecutedCost, SimulationState, epd_u32, init_simulation};

mod common;
use common::{CP_EPD, repo_root, tempdir};

const TECH_SC_BASE: u32 = 0x0058_CF44;
const TECH_BW_BASE: u32 = 0x0058_F140;
const UPGRADE_SC_BASE: u32 = 0x0058_D2B0;
const UPGRADE_BW_BASE: u32 = 0x0058_F32C;
const TECH_SC_STRIDE: u32 = 24;
const TECH_BW_STRIDE: u32 = 20;
const UPGRADE_SC_STRIDE: u32 = 46;
const UPGRADE_BW_STRIDE: u32 = 15;
const TECH_SC_COUNT: u32 = 24;
const UPGRADE_SC_COUNT: u32 = 46;

const PY_IMPORTS: &str = r#"from py import (
    SetDeaths,
    SetTo,
    Var,
    build_module,
    when,
)
from py.scstate import (
    set_tech_researched,
    set_upgrade_level,
    tech_researched,
    tech_researched_var,
    upgrade_level,
)"#;

const TS_IMPORTS: &str = r#"import {
  SetDeaths,
  SetTo,
  Var,
  buildModule,
  when,
} from "{root}/ts/index.ts";
import {
  setTechResearched,
  setUpgradeLevel,
  techResearched,
  techResearchedVar,
  upgradeLevel,
} from "{root}/ts/scstate.ts";"#;

fn repo_root_ts() -> String {
    repo_root().display().to_string()
}

fn write_py(dir: &Path, name: &str, body: &str) -> PathBuf {
    common::write_py(dir, name, PY_IMPORTS, body)
}

fn write_ts(dir: &Path, name: &str, body: &str) -> PathBuf {
    let imports = TS_IMPORTS.replace("{root}", &repo_root_ts());
    common::write_ts(dir, name, &imports, body)
}

fn indent_py(lines: &[&str]) -> String {
    lines.iter().map(|line| format!("    {line}\n")).collect()
}

fn indent_ts(lines: &[&str]) -> String {
    lines.iter().map(|line| format!("  {line}\n")).collect()
}

/// `(address & !3)` as an EPD index: the dword that holds the byte.
fn dword_epd(addr: u32) -> u32 {
    epd_u32(addr & !3)
}

fn lane_shift(addr: u32) -> u32 {
    8 * (addr & 3)
}

/// Replace one byte lane of `base`, leaving the other lanes untouched.
fn with_lane(base: u32, addr: u32, value: u32) -> u32 {
    let shift = lane_shift(addr);
    (base & !(0xFFu32 << shift)) | ((value & 0xFF) << shift)
}

fn lane_of(dword: u32, addr: u32) -> u32 {
    (dword >> lane_shift(addr)) & 0xFF
}

fn tech_addr(tech: u32, player: u32) -> u32 {
    if tech < TECH_SC_COUNT {
        TECH_SC_BASE + tech + TECH_SC_STRIDE * player
    } else {
        TECH_BW_BASE + (tech - TECH_SC_COUNT) + TECH_BW_STRIDE * player
    }
}

fn upgrade_addr(upgrade: u32, player: u32) -> u32 {
    if upgrade < UPGRADE_SC_COUNT {
        UPGRADE_SC_BASE + upgrade + UPGRADE_SC_STRIDE * player
    } else {
        UPGRADE_BW_BASE + (upgrade - UPGRADE_SC_COUNT) + UPGRADE_BW_STRIDE * player
    }
}

/// Run one frontend, keep the simulator, and return the executed cost.
fn run(
    frontend: Frontend,
    path: &Path,
    seeds: &[(u32, u32)],
) -> (PreparedProgram, SimulationState, ExecutedCost) {
    let prepared = prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    let cost = sim.step_loop();
    (prepared, sim, cost)
}

struct Pair {
    py: (PreparedProgram, SimulationState, ExecutedCost),
    ts: (PreparedProgram, SimulationState, ExecutedCost),
}

impl Pair {
    fn run(label: &str, py_path: &Path, ts_path: &Path, seeds: &[(u32, u32)]) -> Self {
        let py = run(Frontend::Py, py_path, seeds);
        let ts = run(Frontend::Ts, ts_path, seeds);
        assert_eq!(
            py.2.triggers, ts.2.triggers,
            "{label}: Py/TS disagree on executed triggers"
        );
        Self { py, ts }
    }

    fn dword(&self, _label: &str, addr: u32) -> (u32, u32) {
        let epd = dword_epd(addr);
        (
            self.py.1.read_epd(epd),
            self.ts.1.read_epd(epd),
        )
    }

    fn cp_values(&self) -> (u32, u32) {
        (self.py.1.read_epd(CP_EPD), self.ts.1.read_epd(CP_EPD))
    }
}

#[test]
fn constant_tech_round_trip_preserves_neighbour_lanes() {
    let dir = tempdir("scstate-tech-roundtrip-");
    let py_body = indent_py(&[
        "set_tech_researched(0, 0, True)",
        "out = Var(0)",
        "out.set(tech_researched_var(0, 0))",
    ]);
    let ts_body = indent_ts(&[
        "setTechResearched(0, 0, true);",
        "const out = Var(0);",
        "out.set(techResearchedVar(0, 0));",
    ]);
    let py_path = write_py(dir.path(), "roundtrip.py", &py_body);
    let ts_path = write_ts(dir.path(), "roundtrip.ts", &ts_body);

    let addr = tech_addr(0, 0);
    let seeds = vec![(dword_epd(addr), 0xAAAA_AA00), (CP_EPD, 0x1234)];
    let pair = Pair::run("tech-roundtrip", &py_path, &ts_path, &seeds);

    assert_eq!(
        common::read_var(&pair.py.0, &pair.py.1, 0),
        1,
        "py reads the written bit"
    );
    assert_eq!(
        common::read_var(&pair.ts.0, &pair.ts.1, 0),
        1,
        "ts reads the written bit"
    );
    let (py_dword, ts_dword) = pair.dword("tech-roundtrip", addr);
    assert_eq!(py_dword, 0xAAAA_AA01, "py writes only lane 0");
    assert_eq!(ts_dword, 0xAAAA_AA01, "ts writes only lane 0");
    assert_eq!(pair.cp_values(), (0x1234, 0x1234), "ambient CP restored");
}

#[test]
fn tech_split_touches_the_documented_sc_and_bw_dwords() {
    let dir = tempdir("scstate-tech-split-");
    let py_body = indent_py(&[
        "set_tech_researched(23, 2, True)",
        "set_tech_researched(24, 2, True)",
    ]);
    let ts_body = indent_ts(&[
        "setTechResearched(23, 2, true);",
        "setTechResearched(24, 2, true);",
    ]);
    let py_path = write_py(dir.path(), "split.py", &py_body);
    let ts_path = write_ts(dir.path(), "split.ts", &ts_body);

    let sc_addr = tech_addr(23, 2);
    let bw_addr = tech_addr(24, 2);
    assert_eq!(sc_addr, TECH_SC_BASE + 23 + 48);
    assert_eq!(bw_addr, TECH_BW_BASE + 0 + 40);
    let seeds = vec![
        (dword_epd(sc_addr), 0xA5A5_A5A5),
        (dword_epd(bw_addr), 0xA5A5_A5A5),
    ];
    let pair = Pair::run("tech-split", &py_path, &ts_path, &seeds);

    let (py_sc, ts_sc) = pair.dword("tech-split-sc", sc_addr);
    let (py_bw, ts_bw) = pair.dword("tech-split-bw", bw_addr);
    assert_eq!(py_sc, with_lane(0xA5A5_A5A5, sc_addr, 1));
    assert_eq!(ts_sc, with_lane(0xA5A5_A5A5, sc_addr, 1));
    assert_eq!(py_bw, with_lane(0xA5A5_A5A5, bw_addr, 1));
    assert_eq!(ts_bw, with_lane(0xA5A5_A5A5, bw_addr, 1));
    // The SC write must not bleed into the BW table and vice versa.
    assert_ne!(sc_addr, bw_addr);
}

#[test]
fn upgrade_levels_round_trip_over_both_domains() {
    let dir = tempdir("scstate-upgrade-roundtrip-");
    let py_body = indent_py(&[
        "hits = Var(0)",
        "set_upgrade_level(0, 0, 200)",
        "when(upgrade_level(0, 0).eq(200), lambda: hits.iadd(1))",
        "set_upgrade_level(60, 11, 15)",
        "when(upgrade_level(60, 11).eq(15), lambda: hits.iadd(1))",
        "when(upgrade_level(45, 11).eq(0x5A), lambda: hits.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const hits = Var(0);",
        "setUpgradeLevel(0, 0, 200);",
        "when(upgradeLevel(0, 0).eq(200), () => hits.iadd(1));",
        "setUpgradeLevel(60, 11, 15);",
        "when(upgradeLevel(60, 11).eq(15), () => hits.iadd(1));",
        "when(upgradeLevel(45, 11).eq(0x5a), () => hits.iadd(1));",
    ]);
    let py_path = write_py(dir.path(), "upgrade.py", &py_body);
    let ts_path = write_ts(dir.path(), "upgrade.ts", &ts_body);

    let last_bw = upgrade_addr(60, 11);
    let last_sc = upgrade_addr(45, 11);
    assert_eq!(last_bw, UPGRADE_BW_BASE + 14 + 165);
    assert_eq!(last_sc, UPGRADE_SC_BASE + 45 + 506);
    let seeds = vec![(dword_epd(last_sc), with_lane(0, last_sc, 0x5A))];
    let pair = Pair::run("upgrade-roundtrip", &py_path, &ts_path, &seeds);

    assert_eq!(
        common::read_var(&pair.py.0, &pair.py.1, 0),
        3,
        "py: 200 round-trip, 15 round-trip, and the SC base + 45 + 46*11 lane"
    );
    assert_eq!(common::read_var(&pair.ts.0, &pair.ts.1, 0), 3);
}

#[test]
fn upgrade_writes_are_byte_isolated_inside_one_dword() {
    let dir = tempdir("scstate-upgrade-lanes-");
    let seed = 0x1122_3344;
    let base = UPGRADE_SC_BASE;
    for lane in 0..4u32 {
        // Upgrades 0..3 for player 0 all live in the dword at the SC base.
        let py_line = format!("set_upgrade_level({lane}, 0, 0x7F)");
        let ts_line = format!("setUpgradeLevel({lane}, 0, 0x7F);");
        let py_body = indent_py(&[py_line.as_str()]);
        let ts_body = indent_ts(&[ts_line.as_str()]);
        let py_path = write_py(dir.path(), &format!("lane{lane}.py"), &py_body);
        let ts_path = write_ts(dir.path(), &format!("lane{lane}.ts"), &ts_body);
        let seeds = vec![(epd_u32(base), seed)];
        let pair = Pair::run("upgrade-lanes", &py_path, &ts_path, &seeds);

        let expected = with_lane(seed, base + lane, 0x7F);
        let (py_dword, ts_dword) = pair.dword("upgrade-lanes", base + lane);
        assert_eq!(py_dword, expected, "py lane {lane}");
        assert_eq!(ts_dword, expected, "ts lane {lane}");
        assert_eq!(lane_of(py_dword, base + lane), 0x7F);
    }
}

fn sweep_py(out: &mut String, upgrade: u32, players: u32) {
    for p in 0..players {
        writeln!(out, "    player = Var(0)").unwrap();
        writeln!(out, "    SetDeaths(player.value, SetTo, {p}, 0)").unwrap();
        writeln!(out, "    set_upgrade_level({upgrade}, player, {})", p + 1).unwrap();
    }
    for p in 0..players {
        writeln!(out, "    player = Var(0)").unwrap();
        writeln!(out, "    SetDeaths(player.value, SetTo, {p}, 0)").unwrap();
        writeln!(out, "    got = Var(0)").unwrap();
        writeln!(out, "    got.set(upgrade_level({upgrade}, player))").unwrap();
        writeln!(out, "    when(got.eq({}), lambda: hits.iadd(1))", p + 1).unwrap();
    }
}

fn sweep_ts(out: &mut String, upgrade: u32, players: u32, tag: &str) {
    for p in 0..players {
        writeln!(out, "  const player_{tag}_{p} = Var(0);").unwrap();
        writeln!(out, "  SetDeaths(player_{tag}_{p}.value, SetTo, {p}, 0);").unwrap();
        writeln!(
            out,
            "  setUpgradeLevel({upgrade}, player_{tag}_{p}, {});",
            p + 1
        )
        .unwrap();
    }
    for p in 0..players {
        writeln!(out, "  const reader_{tag}_{p} = Var(0);").unwrap();
        writeln!(out, "  SetDeaths(reader_{tag}_{p}.value, SetTo, {p}, 0);").unwrap();
        writeln!(out, "  const got_{tag}_{p} = Var(0);").unwrap();
        writeln!(
            out,
            "  got_{tag}_{p}.set(upgradeLevel({upgrade}, reader_{tag}_{p}));"
        )
        .unwrap();
        writeln!(
            out,
            "  when(got_{tag}_{p}.eq({}), () => hits.iadd(1));",
            p + 1
        )
        .unwrap();
    }
}

#[test]
fn runtime_player_sweeps_both_parities_and_all_four_lanes() {
    let dir = tempdir("scstate-runtime-sweep-");
    let mut py_body = String::from("    hits = Var(0)\n");
    sweep_py(&mut py_body, 0, 12); // SC stride 46: lanes 0 and 2 only.
    sweep_py(&mut py_body, 46, 12); // BW stride 15: all four lanes.
    let mut ts_body = String::from("  const hits = Var(0);\n");
    sweep_ts(&mut ts_body, 0, 12, "sc");
    sweep_ts(&mut ts_body, 46, 12, "bw");

    let py_path = write_py(dir.path(), "sweep.py", &py_body);
    let ts_path = write_ts(dir.path(), "sweep.ts", &ts_body);
    let seeds = vec![(CP_EPD, 0x1234)];
    let pair = Pair::run("runtime-sweep", &py_path, &ts_path, &seeds);

    assert_eq!(
        common::read_var(&pair.py.0, &pair.py.1, 0),
        24,
        "py reads back all twelve SC and twelve BW values"
    );
    assert_eq!(common::read_var(&pair.ts.0, &pair.ts.1, 0), 24);
    assert_eq!(pair.cp_values(), (0x1234, 0x1234), "ambient CP restored");

    for player in 0..12u32 {
        let sc_addr = upgrade_addr(0, player);
        let bw_addr = upgrade_addr(46, player);
        let (py_sc, ts_sc) = pair.dword("sweep-sc", sc_addr);
        let (py_bw, ts_bw) = pair.dword("sweep-bw", bw_addr);
        assert_eq!(lane_of(py_sc, sc_addr), player + 1);
        assert_eq!(lane_of(ts_sc, sc_addr), player + 1);
        assert_eq!(lane_of(py_bw, bw_addr), player + 1);
        assert_eq!(lane_of(ts_bw, bw_addr), player + 1);
    }
}

fn parity_bodies() -> (String, String) {
    let py_body = indent_py(&[
        "count = Var(0)",
        "when(tech_researched(5, 3), lambda: count.iadd(1))",
        "when(tech_researched(23, 2), lambda: count.iadd(1))",
        "when(tech_researched(24, 2), lambda: count.iadd(1))",
        "set_tech_researched(0, 0, True)",
        "when(tech_researched_var(0, 0).eq(1), lambda: count.iadd(1))",
        "p = Var(2)",
        "set_tech_researched(1, p, True)",
        "when(tech_researched_var(1, p).eq(1), lambda: count.iadd(1))",
        "set_upgrade_level(0, 0, 200)",
        "when(upgrade_level(0, 0).eq(200), lambda: count.iadd(1))",
        "set_upgrade_level(46, 0, 7)",
        "when(upgrade_level(46, 0).eq(7), lambda: count.iadd(1))",
        "q = Var(3)",
        "set_upgrade_level(46, q, 9)",
        "when(upgrade_level(46, q).eq(9), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "when(techResearched(5, 3), () => count.iadd(1));",
        "when(techResearched(23, 2), () => count.iadd(1));",
        "when(techResearched(24, 2), () => count.iadd(1));",
        "setTechResearched(0, 0, true);",
        "when(techResearchedVar(0, 0).eq(1), () => count.iadd(1));",
        "const p = Var(2);",
        "setTechResearched(1, p, true);",
        "when(techResearchedVar(1, p).eq(1), () => count.iadd(1));",
        "setUpgradeLevel(0, 0, 200);",
        "when(upgradeLevel(0, 0).eq(200), () => count.iadd(1));",
        "setUpgradeLevel(46, 0, 7);",
        "when(upgradeLevel(46, 0).eq(7), () => count.iadd(1));",
        "const q = Var(3);",
        "setUpgradeLevel(46, q, 9);",
        "when(upgradeLevel(46, q).eq(9), () => count.iadd(1));",
    ]);
    (py_body, ts_body)
}

#[test]
fn frontends_agree_on_the_whole_surface_and_emit_identical_hir() {
    let dir = tempdir("scstate-parity-");
    let (py_body, ts_body) = parity_bodies();
    let py_path = write_py(dir.path(), "all.py", &py_body);
    let ts_path = write_ts(dir.path(), "all.ts", &ts_body);

    let tech5_p3 = tech_addr(5, 3);
    let tech23_p2 = tech_addr(23, 2);
    let tech24_p2 = tech_addr(24, 2);
    let seeds = vec![
        (dword_epd(tech5_p3), with_lane(0, tech5_p3, 1)),
        (dword_epd(tech23_p2), with_lane(0, tech23_p2, 1)),
        (dword_epd(tech24_p2), with_lane(0, tech24_p2, 1)),
        (CP_EPD, 0x1234),
    ];
    let pair = Pair::run("parity", &py_path, &ts_path, &seeds);

    // Three seeded conditions + runtime tech + runtime upgrade reads.
    assert_eq!(
        common::read_var(&pair.py.0, &pair.py.1, 0),
        8,
        "py whole-surface count"
    );
    assert_eq!(common::read_var(&pair.ts.0, &pair.ts.1, 0), 8);
    assert_eq!(pair.cp_values(), (0x1234, 0x1234), "ambient CP restored");

    let py_hir = capture_source(&py_path, Frontend::Py).expect("capture py HIR");
    let ts_hir = capture_source(&ts_path, Frontend::Ts).expect("capture ts HIR");
    assert_eq!(py_hir, ts_hir, "Py and TS must emit byte-identical HIR");
}

fn dump_ir(path: &Path) -> (String, serde_json::Value) {
    let output = Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args([
            "dump-ir",
            path.to_str().unwrap(),
            "--json",
            "--loops",
            "1",
        ])
        .env("EUDKIT_ROOT", repo_root())
        .current_dir(repo_root())
        .output()
        .expect("run eudkit dump-ir");
    assert!(
        output.status.success(),
        "{}: dump-ir failed: {}",
        path.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    let raw = String::from_utf8(output.stdout).expect("dump-ir stdout is UTF-8");
    let report = serde_json::from_str(&raw)
        .unwrap_or_else(|err| panic!("{}: invalid dump-ir JSON: {err}\n{raw}", path.display()));
    (raw, report)
}

fn triple(report: &serde_json::Value, key: &str) -> (u64, u64, u64) {
    let cost = &report[key];
    (
        cost["triggers"].as_u64().expect("triggers"),
        cost["conditions"].as_u64().expect("conditions"),
        cost["actions"].as_u64().expect("actions"),
    )
}

struct CostCase {
    name: &'static str,
    py: &'static str,
    ts: &'static str,
    static_cost: (u64, u64, u64),
    executed_cost: (u64, u64, u64),
}

const fn case(
    name: &'static str,
    py: &'static str,
    ts: &'static str,
    static_cost: (u64, u64, u64),
    executed_cost: (u64, u64, u64),
) -> CostCase {
    CostCase {
        name,
        py,
        ts,
        static_cost,
        executed_cost,
    }
}

#[test]
fn scstate_costs_are_pinned_for_both_frontends() {
    let dir = tempdir("scstate-costs-");
    let cases = [
        // A detached condition costs 0 trigger nodes; the frontend suites pin
        // its one-condition shape.
        case(
            "tech_researched_const",
            "    tech_researched(5, 3)\n",
            "  techResearched(5, 3);\n",
            (0, 0, 0),
            (0, 0, 0),
        ),
        case(
            "tech_researched_var_const",
            "    tech_researched_var(0, 0)\n",
            "  techResearchedVar(0, 0);\n",
            (10, 9, 10),
            (10, 9, 1),
        ),
        case(
            "set_tech_researched_const",
            "    set_tech_researched(0, 0, True)\n",
            "  setTechResearched(0, 0, true);\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "upgrade_level_const",
            "    upgrade_level(0, 0)\n",
            "  upgradeLevel(0, 0);\n",
            (9, 8, 9),
            (9, 8, 1),
        ),
        case(
            "set_upgrade_level_const",
            "    set_upgrade_level(0, 0, 3)\n",
            "  setUpgradeLevel(0, 0, 3);\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "tech_researched_var_runtime",
            "    p = Var(1)\n    tech_researched_var(0, p)\n",
            "  const p = Var(1);\n  techResearchedVar(0, p);\n",
            (54, 41, 295),
            (62, 41, 766),
        ),
        case(
            "set_tech_researched_runtime",
            "    p = Var(1)\n    set_tech_researched(0, p, True)\n",
            "  const p = Var(1);\n  setTechResearched(0, p, true);\n",
            (44, 32, 286),
            (52, 32, 766),
        ),
        case(
            "upgrade_level_runtime",
            "    p = Var(1)\n    upgrade_level(0, p)\n",
            "  const p = Var(1);\n  upgradeLevel(0, p);\n",
            (99, 68, 485),
            (78, 44, 1171),
        ),
        case(
            "set_upgrade_level_runtime",
            "    p = Var(1)\n    set_upgrade_level(0, p, 7)\n",
            "  const p = Var(1);\n  setUpgradeLevel(0, p, 7);\n",
            (55, 36, 433),
            (66, 36, 1102),
        ),
    ];

    for cost_case in cases {
        let py_path = write_py(dir.path(), &format!("{}.py", cost_case.name), cost_case.py);
        let ts_path = write_ts(dir.path(), &format!("{}.ts", cost_case.name), cost_case.ts);
        let (py_raw, py_report) = dump_ir(&py_path);
        let (ts_raw, ts_report) = dump_ir(&ts_path);

        assert_eq!(
            py_raw, ts_raw,
            "{}: Py and TS cost reports must be byte-identical",
            cost_case.name
        );
        assert_eq!(py_report, ts_report, "{}: parsed report drift", cost_case.name);
        assert_eq!(
            triple(&py_report, "static_cost"),
            cost_case.static_cost,
            "{}: static cost (triggers, conditions, actions)",
            cost_case.name
        );
        assert_eq!(
            triple(&py_report, "executed_cost"),
            cost_case.executed_cost,
            "{}: executed cost (triggers, conditions, actions)",
            cost_case.name
        );

        // §5 ceilings: the constant rows are exact; the runtime rows are hard
        // upper bounds.
        let (static_t, _, _) = cost_case.static_cost;
        let (exec_t, _, _) = cost_case.executed_cost;
        match cost_case.name {
            "tech_researched_const" => assert_eq!((static_t, exec_t), (0, 0)),
            "tech_researched_var_const" => {
                assert!(static_t <= 10, "tech_researched_var constant static");
                assert!(exec_t <= 10, "tech_researched_var constant executed");
            }
            "set_tech_researched_const" => {
                assert!(static_t <= 1, "set_tech_researched constant static");
                assert!(exec_t <= 1, "set_tech_researched constant executed");
            }
            "upgrade_level_const" => {
                assert!(static_t <= 9, "upgrade_level constant static");
                assert!(exec_t <= 9, "upgrade_level constant executed");
            }
            "set_upgrade_level_const" => {
                assert!(static_t <= 1, "set_upgrade_level constant static");
                assert!(exec_t <= 1, "set_upgrade_level constant executed");
            }
            "tech_researched_var_runtime" => {
                assert!(static_t <= 300, "tech_researched_var runtime static");
                assert!(exec_t <= 200, "tech_researched_var runtime executed");
            }
            "set_tech_researched_runtime" => {
                assert!(static_t <= 280, "set_tech_researched runtime static");
                assert!(exec_t <= 180, "set_tech_researched runtime executed");
            }
            "upgrade_level_runtime" => {
                assert!(static_t <= 380, "upgrade_level runtime static");
                assert!(exec_t <= 240, "upgrade_level runtime executed");
            }
            "set_upgrade_level_runtime" => {
                assert!(static_t <= 320, "set_upgrade_level runtime static");
                assert!(exec_t <= 200, "set_upgrade_level runtime executed");
            }
            _ => {}
        }
    }
}
