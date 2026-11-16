//! `T-ABS-MATH`: runtime parity and pinned cost gates for the absorbed
//! signed div/mod, `pow`, table-driven trig, and binary search.
//!
//! Every runtime fixture seeds its operands with `SetDeaths` and writes the
//! result to an external EPD marker. The test checks that both frontends emit
//! byte-identical HIR and execute to the same markers, then pins the per-site
//! cost against §5.3.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit::{Frontend, capture_source, prepare_source};
use eudkit_lir::{EPD_BASE, ExecutedCost, init_simulation};
use serde_json::Value;

mod common;

const MARKER_EPD: u32 = 9000;

fn py_imports() -> &'static str {
    "from py import Var, SetDeaths, SetTo, build_module\n\
     from py.hir import AddrReloc, emitVarStore\n\
     from py.mathf import (\n\
         div_towards_zero, mod_towards_zero, div_floor, mod_floor,\n\
         div_euclid, mod_euclid, pow, atan2, atan2_256, lengthdir,\n\
         lengthdir_256, binary_min, binary_max,\n\
     )"
}

fn ts_imports() -> String {
    let root = common::repo_root().display();
    format!(
        "import {{ Var, SetDeaths, SetTo, AddrReloc, buildModule, emitVarStore, \
         divTowardsZero, modTowardsZero, divFloor, modFloor, divEuclid, modEuclid, \
         pow, atan2, atan2_256, lengthdir, lengthdir_256, binaryMin, binaryMax }} \
         from \"{root}/ts/index.ts\";"
    )
}

fn marker_addr(index: u32) -> u32 {
    EPD_BASE.wrapping_add(MARKER_EPD.wrapping_add(index).wrapping_mul(4))
}

fn py_store(name: &str, index: u32) -> String {
    format!(
        "    emitVarStore({name}.id, AddrReloc({}))\n",
        marker_addr(index)
    )
}

fn ts_store(name: &str, index: u32) -> String {
    format!(
        "  emitVarStore({name}.id, AddrReloc({}));\n",
        marker_addr(index)
    )
}

fn run_markers(
    frontend: Frontend,
    path: &Path,
    seeds: &[(u32, u32)],
    count: u32,
) -> (Vec<u32>, ExecutedCost) {
    let prepared = prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    let cost = sim.step_loop();
    let markers = (0..count)
        .map(|index| sim.read_epd(MARKER_EPD.wrapping_add(index)))
        .collect();
    (markers, cost)
}

fn marker_pair(
    stem: &str,
    py_body: &str,
    ts_body: &str,
    seeds: &[(u32, u32)],
    count: u32,
) -> Vec<u32> {
    let dir = common::tempdir("mathf-");
    let py = common::write_py(
        dir.path(),
        &format!("{stem}.py"),
        py_imports(),
        py_body,
    );
    let ts = common::write_ts(
        dir.path(),
        &format!("{stem}.ts"),
        &ts_imports(),
        ts_body,
    );

    let py_hir = capture_source(&py, Frontend::Py).expect("capture Python HIR");
    let ts_hir = capture_source(&ts, Frontend::Ts).expect("capture TS HIR");
    assert_eq!(
        py_hir, ts_hir,
        "{stem}: frontends must emit byte-identical HIR"
    );

    let (py_markers, _) = run_markers(Frontend::Py, &py, seeds, count);
    let (ts_markers, _) = run_markers(Frontend::Ts, &ts, seeds, count);
    assert_eq!(
        py_markers, ts_markers,
        "{stem}: frontends must execute to the same markers"
    );
    py_markers
}

fn unsigned(value: i32) -> u32 {
    value as u32
}

#[test]
fn div_family_executes_the_same_quotients_on_both_frontends() {
    let py_body = format!(
        "    a = Var(0)\n    b = Var(0)\n\
         \x20   SetDeaths(a.value, SetTo, {}, 0)\n\
         \x20   SetDeaths(b.value, SetTo, 2, 0)\n\
         {}",
        unsigned(-7),
        ["div_towards_zero", "mod_towards_zero", "div_floor", "mod_floor", "div_euclid", "mod_euclid"]
            .iter()
            .enumerate()
            .map(|(index, name)| {
                format!(
                    "    r{index} = {name}(a, b)\n{}",
                    py_store(&format!("r{index}"), index as u32)
                )
            })
            .collect::<String>()
    );
    let ts_body = format!(
        "  const a = Var(0);\n  const b = Var(0);\n\
         \x20 SetDeaths(a.value, SetTo, {}, 0);\n\
         \x20 SetDeaths(b.value, SetTo, 2, 0);\n\
         {}",
        unsigned(-7),
        ["divTowardsZero", "modTowardsZero", "divFloor", "modFloor", "divEuclid", "modEuclid"]
            .iter()
            .enumerate()
            .map(|(index, name)| {
                format!(
                    "  const r{index} = {name}(a, b);\n{}",
                    ts_store(&format!("r{index}"), index as u32)
                )
            })
            .collect::<String>()
    );

    let markers = marker_pair("div-family", &py_body, &ts_body, &[], 6);
    assert_eq!(
        markers,
        vec![
            unsigned(-3), // towards-zero quotient
            unsigned(-1), // towards-zero remainder
            unsigned(-4), // floor quotient
            1,            // floor remainder
            unsigned(-4), // euclid quotient
            1,            // euclid remainder
        ]
    );
}

#[test]
fn pow_family_uses_the_runtime_multiplication_chain() {
    let py_body = format!(
        "    x = Var(0)\n    SetDeaths(x.value, SetTo, 3, 0)\n\
         \x20   r0 = pow(x, 5)\n{}    r1 = pow(x, 31)\n{}    r2 = pow(x, 5, bits=8)\n{}",
        py_store("r0", 0),
        py_store("r1", 1),
        py_store("r2", 2),
    );
    let ts_body = format!(
        "  const x = Var(0);\n  SetDeaths(x.value, SetTo, 3, 0);\n\
         \x20 const r0 = pow(x, 5);\n{}  const r1 = pow(x, 31);\n{}  const r2 = pow(x, 5, 8);\n{}",
        ts_store("r0", 0),
        ts_store("r1", 1),
        ts_store("r2", 2),
    );

    let markers = marker_pair("pow-family", &py_body, &ts_body, &[], 3);
    assert_eq!(
        markers,
        vec![243, 3u32.wrapping_pow(31), 243 & 0xFF]
    );
}

#[test]
fn atan2_family_uses_the_shared_table() {
    let py_body = format!(
        "    y = Var(0)\n    x = Var(0)\n\
         \x20   SetDeaths(y.value, SetTo, {}, 0)\n\
         \x20   SetDeaths(x.value, SetTo, 0, 0)\n\
         \x20   r0 = atan2(y, x)\n{}    r1 = atan2_256(y, x)\n{}",
        unsigned(-1),
        py_store("r0", 0),
        py_store("r1", 1),
    );
    let ts_body = format!(
        "  const y = Var(0);\n  const x = Var(0);\n\
         \x20 SetDeaths(y.value, SetTo, {}, 0);\n\
         \x20 SetDeaths(x.value, SetTo, 0, 0);\n\
         \x20 const r0 = atan2(y, x);\n{}  const r1 = atan2_256(y, x);\n{}",
        unsigned(-1),
        ts_store("r0", 0),
        ts_store("r1", 1),
    );

    let markers = marker_pair("atan2-family", &py_body, &ts_body, &[], 2);
    assert_eq!(markers, vec![270, 192]);
}

#[test]
fn lengthdir_family_uses_the_shared_tables() {
    let py_body = format!(
        "    length = Var(0)\n    angle = Var(0)\n\
         \x20   SetDeaths(length.value, SetTo, 100, 0)\n\
         \x20   SetDeaths(angle.value, SetTo, 30, 0)\n\
         \x20   dx, dy = lengthdir(length, angle)\n{}{}\
         \x20   SetDeaths(angle.value, SetTo, 64, 0)\n\
         \x20   dx2, dy2 = lengthdir_256(length, angle)\n{}{}",
        py_store("dx", 0),
        py_store("dy", 1),
        py_store("dx2", 2),
        py_store("dy2", 3),
    );
    let ts_body = format!(
        "  const length = Var(0);\n  const angle = Var(0);\n\
         \x20 SetDeaths(length.value, SetTo, 100, 0);\n\
         \x20 SetDeaths(angle.value, SetTo, 30, 0);\n\
         \x20 const [dx, dy] = lengthdir(length, angle);\n{}{}\
         \x20 SetDeaths(angle.value, SetTo, 64, 0);\n\
         \x20 const [dx2, dy2] = lengthdir_256(length, angle);\n{}{}",
        ts_store("dx", 0),
        ts_store("dy", 1),
        ts_store("dx2", 2),
        ts_store("dy2", 3),
    );

    let markers = marker_pair("lengthdir-family", &py_body, &ts_body, &[], 4);
    assert!(
        markers[0].abs_diff(86) <= 1 && markers[1].abs_diff(50) <= 1,
        "lengthdir(100, 30) = ({}, {})",
        markers[0],
        markers[1]
    );
    assert_eq!(&markers[2..], &[0, 100]);
}

#[test]
fn binary_search_family_finds_and_misses() {
    let py_body = format!(
        "    r0 = binary_min(0, 1000, lambda x: x.ge(250))\n{}\
         \x20   r1 = binary_max(0, 1000, lambda x: x.lt(300))\n{}\
         \x20   r2 = binary_min(0, 1000, lambda x: x.ge(1001))\n{}\
         \x20   r3 = binary_max(0, 1000, lambda x: x.lt(0))\n{}",
        py_store("r0", 0),
        py_store("r1", 1),
        py_store("r2", 2),
        py_store("r3", 3),
    );
    let ts_body = format!(
        "  const r0 = binaryMin(0, 1000, (x) => x.ge(250));\n{}\
         \x20 const r1 = binaryMax(0, 1000, (x) => x.lt(300));\n{}\
         \x20 const r2 = binaryMin(0, 1000, (x) => x.ge(1001));\n{}\
         \x20 const r3 = binaryMax(0, 1000, (x) => x.lt(0));\n{}",
        ts_store("r0", 0),
        ts_store("r1", 1),
        ts_store("r2", 2),
        ts_store("r3", 3),
    );

    let markers = marker_pair("binary-family", &py_body, &ts_body, &[], 4);
    assert_eq!(markers, vec![250, 299, 1001, 0xFFFF_FFFF]);
}

#[test]
fn tables_agree_with_an_independent_reference() {
    let py_body = format!(
        "    r0 = atan2(1, 1)\n{}\
         \x20   r1 = atan2(1, 0)\n{}\
         \x20   r2 = atan2(0, 1)\n{}\
         \x20   r3 = atan2(-1, 0)\n{}\
         \x20   dx, dy = lengthdir(100, 30)\n{}{}\
         \x20   dx2, dy2 = lengthdir(10000, 30)\n{}{}",
        py_store("r0", 0),
        py_store("r1", 1),
        py_store("r2", 2),
        py_store("r3", 3),
        py_store("dx", 4),
        py_store("dy", 5),
        py_store("dx2", 6),
        py_store("dy2", 7),
    );
    let ts_body = format!(
        "  const r0 = atan2(1, 1);\n{}\
         \x20 const r1 = atan2(1, 0);\n{}\
         \x20 const r2 = atan2(0, 1);\n{}\
         \x20 const r3 = atan2(-1, 0);\n{}\
         \x20 const [dx, dy] = lengthdir(100, 30);\n{}{}\
         \x20 const [dx2, dy2] = lengthdir(10000, 30);\n{}{}",
        ts_store("r0", 0),
        ts_store("r1", 1),
        ts_store("r2", 2),
        ts_store("r3", 3),
        ts_store("dx", 4),
        ts_store("dy", 5),
        ts_store("dx2", 6),
        ts_store("dy2", 7),
    );

    let markers = marker_pair("table", &py_body, &ts_body, &[], 8);
    assert_eq!(&markers[..4], &[45, 90, 0, 270]);
    assert!(
        markers[4].abs_diff(86) <= 1 && markers[5].abs_diff(50) <= 1,
        "lengthdir(100, 30) = ({}, {})",
        markers[4],
        markers[5]
    );
    assert!(
        markers[6].abs_diff(8660) <= 1 && markers[7].abs_diff(4999) <= 1,
        "lengthdir(10000, 30) = ({}, {})",
        markers[6],
        markers[7]
    );
}

#[test]
fn constant_operands_fold_to_one_act_and_no_conditions() {
    let dir = common::tempdir("mathf-fold-");
    let py_body = "    d = div_floor(7, 2)\n    m = mod_floor(-7, 2)\n    p = pow(3, 5)\n";
    let ts_body =
        "  const d = divFloor(7, 2);\n  const m = modFloor(-7, 2);\n  const p = pow(3, 5);\n";
    let py = common::write_py(dir.path(), "fold.py", py_imports(), py_body);
    let ts = common::write_ts(dir.path(), "fold.ts", &ts_imports(), ts_body);

    let py_hir = capture_source(&py, Frontend::Py).expect("capture Python HIR");
    let ts_hir = capture_source(&ts, Frontend::Ts).expect("capture TS HIR");
    assert_eq!(py_hir, ts_hir, "fold: frontends must agree");

    let hir: Value = serde_json::from_str(&py_hir).expect("valid HIR JSON");
    let stmts = hir["module"]["stmts"].as_array().expect("stmts array");
    assert_eq!(stmts.len(), 1, "the optimizer packs one constant trigger");
    let acts = stmts[0]["acts"].as_array().expect("acts array");
    assert_eq!(stmts[0]["conds"].as_array().unwrap().len(), 0);
    let values: Vec<u64> = acts
        .iter()
        .map(|act| act["value"].as_u64().expect("constant act value"))
        .collect();
    assert_eq!(values, vec![3, 1, 243]);
}

#[test]
fn signed_divmod_matches_the_matrix_and_zero_contract() {
    let cases: [(i32, i32); 9] = [
        (7, 2),
        (-7, 2),
        (7, -2),
        (-7, -2),
        (-1, 2),
        (5, 7),
        (0, 5),
        (-7, 0),
        (7, 0),
    ];

    let specs: [(&str, &str, &str, &str); 6] = [
        ("div_towards_zero", "divTowardsZero", "towards_zero", "q"),
        ("mod_towards_zero", "modTowardsZero", "towards_zero", "r"),
        ("div_floor", "divFloor", "floor", "q"),
        ("mod_floor", "modFloor", "floor", "r"),
        ("div_euclid", "divEuclid", "euclid", "q"),
        ("mod_euclid", "modEuclid", "euclid", "r"),
    ];

    for (py_name, ts_name, mode, part) in specs {
        let mut py_body = String::from("    a = Var(0)\n    b = Var(0)\n");
        let mut ts_body =
            String::from("  const a = Var(0);\n  const b = Var(0);\n");
        let mut expected = Vec::new();
        for (index, (a, b)) in cases.iter().enumerate() {
            py_body.push_str(&format!(
                "    SetDeaths(a.value, SetTo, {}, 0)\n\
                 \x20   SetDeaths(b.value, SetTo, {}, 0)\n\
                 \x20   r{index} = {py_name}(a, b)\n{}",
                unsigned(*a),
                unsigned(*b),
                py_store(&format!("r{index}"), index as u32),
            ));
            ts_body.push_str(&format!(
                "  SetDeaths(a.value, SetTo, {}, 0);\n\
                 \x20 SetDeaths(b.value, SetTo, {}, 0);\n\
                 \x20 const r{index} = {ts_name}(a, b);\n{}",
                unsigned(*a),
                unsigned(*b),
                ts_store(&format!("r{index}"), index as u32),
            ));
            expected.push(expected_signed(mode, part, *a, *b));
        }

        let markers = marker_pair(
            &format!("matrix-{py_name}"),
            &py_body,
            &ts_body,
            &[],
            cases.len() as u32,
        );
        assert_eq!(markers, expected, "{py_name}");
    }
}

fn expected_signed(mode: &str, part: &str, a: i32, b: i32) -> u32 {
    if b == 0 {
        return if part == "q" { 0 } else { unsigned(a) };
    }
    let (q, r) = match mode {
        "towards_zero" => (a / b, a % b),
        "floor" => {
            let mut q = a / b;
            let mut r = a % b;
            if r != 0 && ((r < 0) != (b < 0)) {
                q -= 1;
                r += b;
            }
            (q, r)
        }
        "euclid" => (a.div_euclid(b), a.rem_euclid(b)),
        _ => unreachable!("unknown mode"),
    };
    if part == "q" {
        unsigned(q)
    } else {
        unsigned(r)
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
        panic!("{}: invalid dump-ir JSON: {err}", path.display())
    })
}

#[test]
fn per_site_costs_are_pinned_and_under_the_ticket_ceilings() {
    let dir = common::tempdir("mathf-cost-");
    // name, Python body, TS body, static (T/C/A/bytes), executed T,
    // static ceiling, executed ceiling.
    let sites: [(&str, &str, &str, (u64, u64, u64, u64), u64, u64, u64); 10] = [
        (
            "div_runtime",
            "    a = Var(0)\n    b = Var(0)\n    out = Var(0)\n    out.set(div_floor(a, b))\n",
            "  const a = Var(0);\n  const b = Var(0);\n  const out = Var(0);\n  out.set(divFloor(a, b));\n",
            (287, 103, 851, 691096),
            275,
            350,
            310,
        ),
        (
            "div_const",
            "    a = Var(0)\n    out = Var(0)\n    out.set(div_floor(a, 3))\n",
            "  const a = Var(0);\n  const out = Var(0);\n  out.set(divFloor(a, 3));\n",
            (55, 36, 212, 132440),
            47,
            120,
            80,
        ),
        (
            "pow5",
            "    x = Var(0)\n    out = Var(0)\n    out.set(pow(x, 5))\n",
            "  const x = Var(0);\n  const out = Var(0);\n  out.set(pow(x, 5));\n",
            (493, 99, 1056, 1187144),
            215,
            165 * 3 + 40,
            90 * 3 + 20,
        ),
        (
            "pow31",
            "    x = Var(0)\n    out = Var(0)\n    out.set(pow(x, 31))\n",
            "  const x = Var(0);\n  const out = Var(0);\n  out.set(pow(x, 31));\n",
            (1308, 264, 2696, 3149664),
            565,
            165 * 8 + 40,
            90 * 8 + 20,
        ),
        (
            "atan2",
            "    y = Var(0)\n    x = Var(0)\n    out = Var(0)\n    out.set(atan2(y, x))\n",
            "  const y = Var(0);\n  const x = Var(0);\n  const out = Var(0);\n  out.set(atan2(y, x));\n",
            (384, 166, 1515, 924672),
            400,
            420,
            430,
        ),
        (
            "atan2_256",
            "    y = Var(0)\n    x = Var(0)\n    out = Var(0)\n    out.set(atan2_256(y, x))\n",
            "  const y = Var(0);\n  const x = Var(0);\n  const out = Var(0);\n  out.set(atan2_256(y, x));\n",
            (376, 166, 1233, 905408),
            385,
            420,
            430,
        ),
        (
            "lengthdir",
            "    length = Var(0)\n    angle = Var(0)\n    dx, dy = lengthdir(length, angle)\n",
            "  const length = Var(0);\n  const angle = Var(0);\n  const [dx, dy] = lengthdir(length, angle);\n",
            (493, 185, 1183, 1187144),
            338,
            520,
            410,
        ),
        (
            "lengthdir_256",
            "    length = Var(0)\n    angle = Var(0)\n    dx, dy = lengthdir_256(length, angle)\n",
            "  const length = Var(0);\n  const angle = Var(0);\n  const [dx, dy] = lengthdir_256(length, angle);\n",
            (443, 135, 1145, 1066744),
            290,
            520,
            410,
        ),
        (
            "binary_min",
            "    out = Var(0)\n    out.set(binary_min(0, 1000, lambda x: x.ge(250)))\n",
            "  const out = Var(0);\n  out.set(binaryMin(0, 1000, (x) => x.ge(250)));\n",
            (114, 10, 827, 274512),
            155,
            40 + 30 * 10,
            40 + 30 * 10,
        ),
        (
            "binary_max",
            "    out = Var(0)\n    out.set(binary_max(0, 1000, lambda x: x.lt(300)))\n",
            "  const out = Var(0);\n  out.set(binaryMax(0, 1000, (x) => x.lt(300)));\n",
            (125, 10, 924, 301000),
            177,
            40 + 30 * 10,
            40 + 30 * 10,
        ),
    ];

    for (name, py_body, ts_body, (triggers, conditions, actions, bytes), executed, static_max, executed_max) in sites {
        let py: PathBuf = common::write_py(dir.path(), &format!("{name}.py"), py_imports(), py_body);
        let ts: PathBuf = common::write_ts(dir.path(), &format!("{name}.ts"), &ts_imports(), ts_body);
        let py_report = cost_report(&py);
        let ts_report = cost_report(&ts);
        assert_eq!(py_report, ts_report, "{name}: cost reports must match");

        let static_cost = &py_report["static_cost"];
        assert_eq!(
            (
                static_cost["triggers"].as_u64().unwrap(),
                static_cost["conditions"].as_u64().unwrap(),
                static_cost["actions"].as_u64().unwrap(),
                static_cost["trigger_bytes"].as_u64().unwrap(),
            ),
            (triggers, conditions, actions, bytes),
            "{name}: static pin"
        );
        let executed_triggers = py_report["executed_cost"]["triggers"].as_u64().unwrap();
        assert_eq!(
            executed_triggers, executed,
            "{name}: executed pin"
        );
        assert!(
            triggers <= static_max,
            "{name}: static {triggers} exceeds ceiling {static_max}"
        );
        assert!(
            executed_triggers <= executed_max,
            "{name}: executed {executed_triggers} exceeds ceiling {executed_max}"
        );
    }
}
