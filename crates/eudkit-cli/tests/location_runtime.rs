//! `T-ABS-LOCATION`: typed location handles over the 5-dword table and
//! build-time geometry against the LIR simulator.
//!
//! The location table sits outside the assembled payload, so the simulator
//! seeds it through `external_epds`. Every write then lands in the same map and
//! can be read back with `read_epd`, including dword 4, which the handle must
//! never touch. `CreateUnit` is an engine act the simulator does not model, so
//! geometry tests count act 44 in the captured HIR and read the final record
//! from the seeded table.
//!
//! Costs are pinned exactly from `dump-ir --json --loops 1`; the §5 ceilings are
//! asserted alongside every pin.

use std::path::{Path, PathBuf};
use std::process::Command;

use eudkit::{Frontend, capture_source, prepare_source};
use eudkit_lir::{SimulationState, epd_u32, init_simulation};

mod common;
use common::{CP_EPD, repo_root, tempdir};

/// `LOCATION_TABLE_ADDR` from `py/camera.py`.
const LOCATION_TABLE_ADDR: u32 = 0x0058_DC4C;
/// `LOCATION_STRIDE_DWORDS` from `py/camera.py`.
const LOCATION_STRIDE_DWORDS: u32 = 5;
/// dword 4 of every record; the handle must leave it untouched.
const SENTINEL: u32 = 0xCAFE_BABE;
/// Ambient CurrentPlayer value restored by `with_epd`.
const SAVED_CP: u32 = 0x1234;

fn location_epd(id: u32) -> u32 {
    epd_u32(LOCATION_TABLE_ADDR) + LOCATION_STRIDE_DWORDS * id
}

fn location_seeds(id: u32, values: [u32; 4]) -> Vec<(u32, u32)> {
    let base = location_epd(id);
    let mut seeds = vec![
        (base, values[0]),
        (base + 1, values[1]),
        (base + 2, values[2]),
        (base + 3, values[3]),
        (base + 4, SENTINEL),
    ];
    seeds.push((CP_EPD, SAVED_CP));
    seeds
}

const PY_IMPORTS: &str = "from py import (\n\
         \x20   SetTo,\n\
         \x20   Var,\n\
         \x20   build_module,\n\
         \x20   when,\n\
         )\n\
         from py.location import (\n\
         \x20   Location,\n\
         \x20   draw_circle,\n\
         \x20   draw_line,\n\
         \x20   draw_polygon,\n\
         \x20   draw_star,\n\
         \x20   getloc_tl,\n\
         \x20   location_epd,\n\
         )";

const TS_IMPORTS: &str = "import {\n\
         \x20 SetTo,\n\
         \x20 Var,\n\
         \x20 buildModule,\n\
         \x20 when,\n\
         } from \"{root}/ts/index.ts\";\n\
         import {\n\
         \x20 Location,\n\
         \x20 drawCircle,\n\
         \x20 drawLine,\n\
         \x20 drawPolygon,\n\
         \x20 drawStar,\n\
         \x20 getlocTl,\n\
         \x20 locationEpd,\n\
         } from \"{root}/ts/location.ts\";";

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

fn run_with_sim(
    frontend: Frontend,
    path: &Path,
    seeds: &[(u32, u32)],
) -> (eudkit::PreparedProgram, SimulationState) {
    let prepared = prepare_source(path, Some(frontend), None)
        .unwrap_or_else(|err| panic!("{}: prepare_source failed: {err}", path.display()));
    let mut sim = init_simulation(&prepared.lir)
        .unwrap_or_else(|err| panic!("{}: init_simulation failed: {err}", path.display()));
    for (epd, value) in seeds {
        sim.external_epds.insert(*epd, *value);
    }
    sim.step_loop();
    (prepared, sim)
}

fn record(sim: &SimulationState, id: u32) -> [u32; 4] {
    let base = location_epd(id);
    [
        sim.read_epd(base),
        sim.read_epd(base + 1),
        sim.read_epd(base + 2),
        sim.read_epd(base + 3),
    ]
}

fn count_acttype_in_nodes(nodes: &[serde_json::Value], acttype: u64) -> usize {
    let mut total = 0;
    for node in nodes {
        if node.get("kind").and_then(|value| value.as_str()) == Some("act")
            && node.get("acttype").and_then(|value| value.as_u64()) == Some(acttype)
        {
            total += 1;
        }
        if let Some(actions) = node.get("acts").and_then(|value| value.as_array()) {
            total += actions
                .iter()
                .filter(|act| act.get("acttype").and_then(|v| v.as_u64()) == Some(acttype))
                .count();
        }
        for key in ["body", "else_body"] {
            if let Some(children) = node.get(key).and_then(|value| value.as_array()) {
                total += count_acttype_in_nodes(children, acttype);
            }
        }
    }
    total
}

/// Count `acttype` actions in one captured HIR document.
fn count_acttype(hir: &str, acttype: u64) -> usize {
    let document: serde_json::Value = serde_json::from_str(hir).expect("valid HIR JSON");
    let module = &document["module"];
    let mut total = count_acttype_in_nodes(
        module["stmts"].as_array().expect("module stmts"),
        acttype,
    );
    if let Some(funcs) = module.get("funcs").and_then(|value| value.as_array()) {
        for func in funcs {
            if let Some(body) = func.get("body").and_then(|value| value.as_array()) {
                total += count_acttype_in_nodes(body, acttype);
            }
        }
    }
    total
}

#[test]
fn bounds_round_trips_the_seeded_record() {
    let dir = tempdir("location-bounds-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "left, top, right, bottom = Location(7).bounds()",
        "when(left.eq(100), lambda: count.iadd(1))",
        "when(top.eq(200), lambda: count.iadd(1))",
        "when(right.eq(300), lambda: count.iadd(1))",
        "when(bottom.eq(400), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const [left, top, right, bottom] = new Location(7).bounds();",
        "when(left.eq(100), () => count.iadd(1));",
        "when(top.eq(200), () => count.iadd(1));",
        "when(right.eq(300), () => count.iadd(1));",
        "when(bottom.eq(400), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "bounds.py", &py_body);
    let ts_path = write_ts(dir.path(), "bounds.ts", &ts_body);
    let seeds = location_seeds(7, [100, 200, 300, 400]);

    let (py, ts) = common::run_case("bounds", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 4, "py bounds markers");
    assert_eq!(ts.count, 4, "ts bounds markers");
    assert_eq!(py.cp_value, SAVED_CP, "py must leave CP alone");
    assert_eq!(ts.cp_value, SAVED_CP, "ts must leave CP alone");
}

#[test]
fn set_writes_two_coordinates_and_leaves_dword_four() {
    let dir = tempdir("location-set-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "Location(7).set(11, 22)",
        "left, top, right, bottom = Location(7).bounds()",
        "when(left.eq(11), lambda: count.iadd(1))",
        "when(top.eq(22), lambda: count.iadd(1))",
        "when(right.eq(11), lambda: count.iadd(1))",
        "when(bottom.eq(22), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "new Location(7).set(11, 22);",
        "const [left, top, right, bottom] = new Location(7).bounds();",
        "when(left.eq(11), () => count.iadd(1));",
        "when(top.eq(22), () => count.iadd(1));",
        "when(right.eq(11), () => count.iadd(1));",
        "when(bottom.eq(22), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "set.py", &py_body);
    let ts_path = write_ts(dir.path(), "set.ts", &ts_body);
    let seeds = location_seeds(7, [100, 200, 300, 400]);

    let (py, ts) = common::run_case("set", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 4, "py set markers");
    assert_eq!(ts.count, 4, "ts set markers");

    let (_, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (_, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);
    assert_eq!(record(&py_sim, 7), [11, 22, 11, 22], "py record");
    assert_eq!(record(&ts_sim, 7), [11, 22, 11, 22], "ts record");
    assert_eq!(
        py_sim.read_epd(location_epd(7) + 4),
        SENTINEL,
        "py dword 4"
    );
    assert_eq!(
        ts_sim.read_epd(location_epd(7) + 4),
        SENTINEL,
        "ts dword 4"
    );
}

#[test]
fn dilate_and_add_update_the_record() {
    let dir = tempdir("location-dilate-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "loc = Location(7)",
        "loc.dilate(10, 20)",
        "loc.add(1, 2, 3, 4)",
        "left, top, right, bottom = loc.bounds()",
        "when(left.eq(91), lambda: count.iadd(1))",
        "when(top.eq(182), lambda: count.iadd(1))",
        "when(right.eq(313), lambda: count.iadd(1))",
        "when(bottom.eq(424), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const loc = new Location(7);",
        "loc.dilate(10, 20);",
        "loc.add(1, 2, 3, 4);",
        "const [left, top, right, bottom] = loc.bounds();",
        "when(left.eq(91), () => count.iadd(1));",
        "when(top.eq(182), () => count.iadd(1));",
        "when(right.eq(313), () => count.iadd(1));",
        "when(bottom.eq(424), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "dilate.py", &py_body);
    let ts_path = write_ts(dir.path(), "dilate.ts", &ts_body);
    let seeds = location_seeds(7, [100, 200, 300, 400]);

    let (py, ts) = common::run_case("dilate", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 4, "py dilate/add markers");
    assert_eq!(ts.count, 4, "ts dilate/add markers");

    let (_, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (_, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);
    assert_eq!(record(&py_sim, 7), [91, 182, 313, 424], "py record");
    assert_eq!(record(&ts_sim, 7), [91, 182, 313, 424], "ts record");
}

#[test]
fn center_folds_the_record() {
    let dir = tempdir("location-center-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "cx, cy = Location(7).center()",
        "when(cx.eq(50), lambda: count.iadd(1))",
        "when(cy.eq(30), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const [cx, cy] = new Location(7).center();",
        "when(cx.eq(50), () => count.iadd(1));",
        "when(cy.eq(30), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "center.py", &py_body);
    let ts_path = write_ts(dir.path(), "center.ts", &ts_body);
    let seeds = location_seeds(7, [0, 0, 100, 60]);

    let (py, ts) = common::run_case("center", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 2, "py center markers");
    assert_eq!(ts.count, 2, "ts center markers");
}

fn geometry_case(
    label: &str,
    py_call: &str,
    ts_call: &str,
    expected_last: [u32; 4],
    create_units: usize,
) {
    let dir = tempdir(&format!("location-{label}-"));
    let py_body = indent_py(&["count = Var(0)", py_call, "count.iadd(1)"]);
    let ts_body = indent_ts(&["const count = Var(0);", ts_call, "count.iadd(1);"]);

    let py_path = write_py(dir.path(), &format!("{label}.py"), &py_body);
    let ts_path = write_ts(dir.path(), &format!("{label}.ts"), &ts_body);
    let seeds = location_seeds(7, [100, 200, 300, 400]);

    let (py, ts) = common::run_case(label, &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 1, "{label}: py body ran once");
    assert_eq!(ts.count, 1, "{label}: ts body ran once");

    let (_, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (_, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);
    assert_eq!(record(&py_sim, 7), expected_last, "{label}: py last dot");
    assert_eq!(record(&ts_sim, 7), expected_last, "{label}: ts last dot");

    let py_hir = capture_source(&py_path, Frontend::Py).expect("capture py HIR");
    let ts_hir = capture_source(&ts_path, Frontend::Ts).expect("capture ts HIR");
    assert_eq!(py_hir, ts_hir, "{label}: Py and TS must emit identical HIR");
    assert_eq!(
        count_acttype(&py_hir, 44),
        create_units,
        "{label}: CreateUnit acts"
    );
}

#[test]
fn circle_leaves_the_record_at_its_last_dot() {
    geometry_case(
        "circle",
        "draw_circle(7, 1, 0, 512, 512, radius=64, dots=8)",
        "drawCircle(7, 1, 0, 512, 512, { radius: 64, dots: 8 });",
        [557, 467, 557, 467],
        8,
    );
}

#[test]
fn line_leaves_the_record_at_its_last_point() {
    geometry_case(
        "line",
        "draw_line(7, 1, 0, 0, 0, 100, 100, dots=3)",
        "drawLine(7, 1, 0, 0, 0, 100, 100, { dots: 3 });",
        [100, 100, 100, 100],
        3,
    );
}

#[test]
fn polygon_leaves_the_record_at_its_last_point() {
    geometry_case(
        "polygon",
        "draw_polygon(7, 1, 0, [(0, 0), (100, 0), (50, 100)], dots_per_edge=1)",
        "drawPolygon(7, 1, 0, [[0, 0], [100, 0], [50, 100]], { dotsPerEdge: 1 });",
        [50, 100, 50, 100],
        3,
    );
}

#[test]
fn star_leaves_the_record_at_its_last_corner() {
    geometry_case(
        "star",
        "draw_star(7, 1, 0, 512, 512, vertices=5, density=2, radius=64, dots_per_edge=1)",
        "drawStar(7, 1, 0, 512, 512, { vertices: 5, density: 2, radius: 64, dotsPerEdge: 1 });",
        [460, 474, 460, 474],
        5,
    );
}

fn parity_bodies() -> (String, String) {
    let py_body = indent_py(&[
        "count = Var(0)",
        "loc = Location(7)",
        "left, top, right, bottom = loc.bounds()",
        "when(left.eq(100), lambda: count.iadd(1))",
        "when(top.eq(200), lambda: count.iadd(1))",
        "when(right.eq(300), lambda: count.iadd(1))",
        "when(bottom.eq(400), lambda: count.iadd(1))",
        "tl_left, tl_top = loc.top_left()",
        "when(tl_left.eq(100), lambda: count.iadd(1))",
        "when(tl_top.eq(200), lambda: count.iadd(1))",
        "gl_left, gl_top = getloc_tl(7)",
        "when(gl_left.eq(100), lambda: count.iadd(1))",
        "when(gl_top.eq(200), lambda: count.iadd(1))",
        "cx, cy = loc.center()",
        "when(cx.eq(200), lambda: count.iadd(1))",
        "when(cy.eq(300), lambda: count.iadd(1))",
        "loc.set(11, 22)",
        "loc.add(1, 2, 3, 4)",
        "loc.dilate(2, 3)",
        "loc.move(500, 600, 10, 20)",
        "left, top, right, bottom = loc.bounds()",
        "when(left.eq(490), lambda: count.iadd(1))",
        "when(top.eq(580), lambda: count.iadd(1))",
        "when(right.eq(510), lambda: count.iadd(1))",
        "when(bottom.eq(620), lambda: count.iadd(1))",
        "x = Var(1)",
        "y = Var(2)",
        "loc.set(x, y)",
        "loc.add(x, y)",
        "left, top, right, bottom = loc.bounds()",
        "when(left.eq(2), lambda: count.iadd(1))",
        "when(top.eq(4), lambda: count.iadd(1))",
        "when(right.eq(2), lambda: count.iadd(1))",
        "when(bottom.eq(4), lambda: count.iadd(1))",
        "runtime_id = Var(3)",
        "remote = Location(runtime_id)",
        "remote.set(10, 20)",
        "r_left, r_top, r_right, r_bottom = remote.bounds()",
        "when(r_left.eq(10), lambda: count.iadd(1))",
        "when(r_top.eq(20), lambda: count.iadd(1))",
        "when(r_right.eq(10), lambda: count.iadd(1))",
        "when(r_bottom.eq(20), lambda: count.iadd(1))",
        "draw_circle(7, 1, 0, 512, 512, radius=64, dots=8)",
        "draw_line(7, 1, 0, 0, 0, 100, 100, dots=3)",
        "draw_polygon(7, 1, 0, [(0, 0), (100, 0), (50, 100)], dots_per_edge=1)",
        "draw_star(7, 1, 0, 512, 512, vertices=5, density=2, radius=64, dots_per_edge=1)",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const loc = new Location(7);",
        "const [left, top, right, bottom] = loc.bounds();",
        "when(left.eq(100), () => count.iadd(1));",
        "when(top.eq(200), () => count.iadd(1));",
        "when(right.eq(300), () => count.iadd(1));",
        "when(bottom.eq(400), () => count.iadd(1));",
        "const [tlLeft, tlTop] = loc.topLeft();",
        "when(tlLeft.eq(100), () => count.iadd(1));",
        "when(tlTop.eq(200), () => count.iadd(1));",
        "const [glLeft, glTop] = getlocTl(7);",
        "when(glLeft.eq(100), () => count.iadd(1));",
        "when(glTop.eq(200), () => count.iadd(1));",
        "const [cx, cy] = loc.center();",
        "when(cx.eq(200), () => count.iadd(1));",
        "when(cy.eq(300), () => count.iadd(1));",
        "loc.set(11, 22);",
        "loc.add(1, 2, 3, 4);",
        "loc.dilate(2, 3);",
        "loc.move(500, 600, 10, 20);",
        "const [left2, top2, right2, bottom2] = loc.bounds();",
        "when(left2.eq(490), () => count.iadd(1));",
        "when(top2.eq(580), () => count.iadd(1));",
        "when(right2.eq(510), () => count.iadd(1));",
        "when(bottom2.eq(620), () => count.iadd(1));",
        "const x = Var(1);",
        "const y = Var(2);",
        "loc.set(x, y);",
        "loc.add(x, y);",
        "const [left3, top3, right3, bottom3] = loc.bounds();",
        "when(left3.eq(2), () => count.iadd(1));",
        "when(top3.eq(4), () => count.iadd(1));",
        "when(right3.eq(2), () => count.iadd(1));",
        "when(bottom3.eq(4), () => count.iadd(1));",
        "const runtimeId = Var(3);",
        "const remote = new Location(runtimeId);",
        "remote.set(10, 20);",
        "const [rLeft, rTop, rRight, rBottom] = remote.bounds();",
        "when(rLeft.eq(10), () => count.iadd(1));",
        "when(rTop.eq(20), () => count.iadd(1));",
        "when(rRight.eq(10), () => count.iadd(1));",
        "when(rBottom.eq(20), () => count.iadd(1));",
        "drawCircle(7, 1, 0, 512, 512, { radius: 64, dots: 8 });",
        "drawLine(7, 1, 0, 0, 0, 100, 100, { dots: 3 });",
        "drawPolygon(7, 1, 0, [[0, 0], [100, 0], [50, 100]], { dotsPerEdge: 1 });",
        "drawStar(7, 1, 0, 512, 512, { vertices: 5, density: 2, radius: 64, dotsPerEdge: 1 });",
    ]);
    (py_body, ts_body)
}

#[test]
fn frontends_agree_on_the_whole_surface_and_emit_identical_hir() {
    let dir = tempdir("location-parity-");
    let (py_body, ts_body) = parity_bodies();
    let py_path = write_py(dir.path(), "all.py", &py_body);
    let ts_path = write_ts(dir.path(), "all.ts", &ts_body);

    let mut seeds = location_seeds(7, [100, 200, 300, 400]);
    seeds.extend(location_seeds(3, [0, 0, 0, 0]));

    let (py, ts) = common::run_case("all", &py_path, &ts_path, &seeds);
    // Four bounds + two top_left + two getloc_tl + two center + four after the
    // mutators + four runtime coordinates + four runtime-id reads.
    assert_eq!(py.count, 22, "py whole-surface count");
    assert_eq!(ts.count, 22, "ts whole-surface count");
    assert_eq!(py.cp_value, SAVED_CP, "py CP restore");
    assert_eq!(ts.cp_value, SAVED_CP, "ts CP restore");

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
fn location_costs_are_pinned_for_both_frontends() {
    let dir = tempdir("location-costs-");
    // Observed through `dump-ir --json --loops 1`. The shared 32-bit reader
    // costs 33 = 32 bit tests + one accumulator reset per dword, so `bounds`
    // pays 132 and `top_left` pays 66; `center` adds the two shift kernels.
    let cases = [
        case(
            "set_const",
            "    Location(7).set(11, 22)\n",
            "  new Location(7).set(11, 22);\n",
            (1, 0, 4),
            (1, 0, 4),
        ),
        case(
            "set_rt_coords",
            "    x = Var(1)\n    y = Var(2)\n    Location(7).set(x, y)\n",
            "  const x = Var(1);\n  const y = Var(2);\n  new Location(7).set(x, y);\n",
            (29, 8, 104),
            (36, 12, 296),
        ),
        case(
            "set_rt_id",
            "    v = Var(3)\n    Location(v).set(11, 22)\n",
            "  const v = Var(3);\n  new Location(v).set(11, 22);\n",
            (40, 8, 218),
            (53, 12, 794),
        ),
        case(
            "add_const",
            "    Location(7).add(0, 5)\n",
            "  new Location(7).add(0, 5);\n",
            (1, 0, 2),
            (1, 0, 2),
        ),
        case(
            "add4_const",
            "    Location(7).add(1, 2, 3, 4)\n",
            "  new Location(7).add(1, 2, 3, 4);\n",
            (1, 0, 4),
            (1, 0, 4),
        ),
        case(
            "dilate_const",
            "    Location(7).dilate(10, 20)\n",
            "  new Location(7).dilate(10, 20);\n",
            (1, 0, 4),
            (1, 0, 4),
        ),
        case(
            "move_const",
            "    Location(7).move(500, 600, 10, 20)\n",
            "  new Location(7).move(500, 600, 10, 20);\n",
            (1, 0, 4),
            (1, 0, 4),
        ),
        case(
            "bounds_const",
            "    Location(7).bounds()\n",
            "  new Location(7).bounds();\n",
            (132, 128, 132),
            (132, 128, 4),
        ),
        case(
            "top_left_const",
            "    Location(7).top_left()\n",
            "  new Location(7).topLeft();\n",
            (66, 64, 66),
            (66, 64, 2),
        ),
        case(
            "getloc_tl_const",
            "    getloc_tl(7)\n",
            "  getlocTl(7);\n",
            (66, 64, 66),
            (66, 64, 2),
        ),
        case(
            "center_const",
            "    Location(7).center()\n",
            "  new Location(7).center();\n",
            (138, 128, 290),
            (141, 128, 354),
        ),
        case(
            "draw_circle_8",
            "    draw_circle(7, 1, 0, 512, 512, radius=64, dots=8)\n",
            "  drawCircle(7, 1, 0, 512, 512, { radius: 64, dots: 8 });\n",
            (1, 0, 40),
            (1, 0, 40),
        ),
        case(
            "draw_circle_32",
            "    draw_circle(7, 1, 0, 512, 512, radius=64, dots=32)\n",
            "  drawCircle(7, 1, 0, 512, 512, { radius: 64, dots: 32 });\n",
            (3, 0, 160),
            (3, 0, 160),
        ),
        case(
            "draw_line_3",
            "    draw_line(7, 1, 0, 0, 0, 100, 100, dots=3)\n",
            "  drawLine(7, 1, 0, 0, 0, 100, 100, { dots: 3 });\n",
            (1, 0, 15),
            (1, 0, 15),
        ),
        case(
            "draw_polygon_tri_1",
            "    draw_polygon(7, 1, 0, [(0, 0), (100, 0), (50, 100)], dots_per_edge=1)\n",
            "  drawPolygon(7, 1, 0, [[0, 0], [100, 0], [50, 100]], { dotsPerEdge: 1 });\n",
            (1, 0, 15),
            (1, 0, 15),
        ),
        case(
            "draw_star_5_1",
            "    draw_star(7, 1, 0, 512, 512, vertices=5, density=2, radius=64, dots_per_edge=1)\n",
            "  drawStar(7, 1, 0, 512, 512, { vertices: 5, density: 2, radius: 64, dotsPerEdge: 1 });\n",
            (1, 0, 25),
            (1, 0, 25),
        ),
        case(
            "draw_star_5_8",
            "    draw_star(7, 1, 0, 512, 512, vertices=5, density=2, radius=64)\n",
            "  drawStar(7, 1, 0, 512, 512, { vertices: 5, density: 2, radius: 64 });\n",
            (4, 0, 200),
            (4, 0, 200),
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

        let (static_t, _, _) = cost_case.static_cost;
        let (exec_t, _, _) = cost_case.executed_cost;
        match cost_case.name {
            "set_const" | "add_const" | "add4_const" | "dilate_const" | "move_const" => {
                assert_eq!(static_t, 1, "{}: §5 constant writer", cost_case.name);
                assert_eq!(exec_t, 1, "{}: §5 constant writer", cost_case.name);
            }
            "set_rt_coords" | "set_rt_id" => {
                assert!(static_t <= 360, "{}: §5 runtime static", cost_case.name);
                assert!(exec_t <= 320, "{}: §5 runtime executed", cost_case.name);
            }
            "bounds_const" => {
                assert!(static_t <= 140, "bounds static");
                assert!(exec_t <= 140, "bounds executed");
            }
            "top_left_const" | "getloc_tl_const" => {
                assert!(static_t <= 72, "top-left static");
                assert!(exec_t <= 72, "top-left executed");
            }
            "center_const" => {
                assert!(static_t <= 160, "center static");
                assert!(exec_t <= 160, "center executed");
            }
            "draw_circle_8" => assert!(static_t <= 9 && exec_t <= 9, "8-dot circle"),
            "draw_circle_32" => assert!(static_t <= 11 && exec_t <= 11, "32-dot circle"),
            "draw_line_3" | "draw_polygon_tri_1" => {
                assert!(static_t <= 9 && exec_t <= 9, "three-point path");
            }
            "draw_star_5_1" => assert!(static_t <= 18 && exec_t <= 18, "one-dot star"),
            "draw_star_5_8" => assert!(static_t <= 32 && exec_t <= 32, "eight-dot star"),
            _ => {}
        }
    }
}
