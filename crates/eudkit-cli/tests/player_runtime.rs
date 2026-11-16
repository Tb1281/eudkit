//! `T-ABS-PLAYER`: player state, existence, and all-player broadcasts against
//! the LIR simulator.
//!
//! The game's player tables sit outside the assembled payload, so the
//! simulator exposes them through `external_epds`. Seeding one 12-byte
//! existence record lets the real condition-15 masked comparison run without a
//! map; the same trick seeds `dwUserPlayerId`, the game tick, and player 3's
//! minerals. Both frontends go through the shared capture/lowering pipeline;
//! they must be byte-identical and semantically identical.
//!
//! Costs are pinned exactly from `dump-ir --json --loops 1`; the §5 ceilings
//! are asserted only where the observed cost fits the re-blessed row.

use std::path::{Path, PathBuf};
use std::process::Command;

use eudkit::{Frontend, capture_source, prepare_source};
use eudkit_lir::{SimulationState, epd_u32, init_simulation};

mod common;
use common::{CP_EPD, repo_root, tempdir};

/// `PLAYER_TABLE_ADDR`: 12 bytes (three dwords) per existence record.
const PLAYER_TABLE_ADDR: u32 = 0x0051_A280;
const PLAYER_SLOT_BYTES: u32 = 12;
const PLAYER_SLOT_COUNT: u32 = 8;
const USER_PLAYER_ADDR: u32 = 0x0051_2684;
const GAME_TICK_ADDR: u32 = 0x0057_F23C;
const MINERALS_ADDR: u32 = 0x0057_F0F0;

const PY_IMPORTS: &str = "from py import (\n\
         \x20   SetDeaths,\n\
         \x20   SetTo,\n\
         \x20   Var,\n\
         \x20   build_module,\n\
         \x20   when,\n\
         )\n\
         from py.cp import read_current_player_register\n\
         from py.player import (\n\
         \x20   PVariable,\n\
         \x20   Player,\n\
         \x20   center_view_all,\n\
         \x20   display_text_all,\n\
         \x20   for_each_player,\n\
         \x20   game_tick,\n\
         \x20   is_user,\n\
         \x20   minimap_ping_all,\n\
         \x20   play_wav_all,\n\
         \x20   player_exists,\n\
         \x20   set_mission_objectives_all,\n\
         \x20   talking_portrait_all,\n\
         \x20   turbo,\n\
         \x20   user_player,\n\
         \x20   )";

const TS_IMPORTS: &str = "import {\n\
         \x20 SetDeaths,\n\
         \x20 SetTo,\n\
         \x20 Var,\n\
         \x20 buildModule,\n\
         \x20 readCurrentPlayerRegister,\n\
         \x20 when,\n\
         } from \"{root}/ts/index.ts\";\n\
         import {\n\
         \x20 PVariable,\n\
         \x20 Player,\n\
         \x20 centerViewAll,\n\
         \x20 displayTextAll,\n\
         \x20 forEachPlayer,\n\
         \x20 gameTick,\n\
         \x20 isUser,\n\
         \x20 minimapPingAll,\n\
         \x20 playWavAll,\n\
         \x20 playerExists,\n\
         \x20 setMissionObjectivesAll,\n\
         \x20 talkingPortraitAll,\n\
         \x20 turbo,\n\
         \x20 userPlayer,\n\
         \x20 } from \"{root}/ts/player.ts\";";

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

/// Seed the "has left" dword of one 12-byte existence record.
///
/// A present player carries any non-sentinel value; an absent player carries
/// the sentinel `!(base + 4)` that `player_exists` looks for.
fn seed_player(seeds: &mut Vec<(u32, u32)>, player: u32, present: bool) {
    let base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player;
    let value = if present { 0u32 } else { !(base + 4) };
    seeds.push((epd_u32(base + 8), value));
}

fn existence_seeds(present: &[u32]) -> Vec<(u32, u32)> {
    let mut seeds = Vec::new();
    for player in 0..PLAYER_SLOT_COUNT {
        seed_player(&mut seeds, player, present.contains(&player));
    }
    seeds
}

/// Run one frontend and keep the simulator so tests can inspect external EPDs.
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

#[test]
fn player_exists_constant_and_runtime_track_the_table_asymmetrically() {
    let dir = tempdir("player-exists-");
    let py_body = indent_py(&[
        "c0 = Var(0)",
        "c1 = Var(0)",
        "c3 = Var(0)",
        "c2 = Var(0)",
        "when(player_exists(0).eq(1), lambda: c0.iadd(1))",
        "when(player_exists(1).eq(1), lambda: c1.iadd(1))",
        "p = Var(0)",
        "p.set(3)",
        "when(player_exists(p).eq(1), lambda: c3.iadd(1))",
        "p.set(2)",
        "when(player_exists(p).eq(1), lambda: c2.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const c0 = Var(0);",
        "const c1 = Var(0);",
        "const c3 = Var(0);",
        "const c2 = Var(0);",
        "when(playerExists(0).eq(1), () => c0.iadd(1));",
        "when(playerExists(1).eq(1), () => c1.iadd(1));",
        "const p = Var(0);",
        "p.set(3);",
        "when(playerExists(p).eq(1), () => c3.iadd(1));",
        "p.set(2);",
        "when(playerExists(p).eq(1), () => c2.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "exists.py", &py_body);
    let ts_path = write_ts(dir.path(), "exists.ts", &ts_body);

    // `[0, 3]` proves constant 0 vs 1 and runtime 3 vs 2, while `[3]` alone
    // proves the constants do not accidentally share the runtime seed.
    let scenarios: [(&[u32], [u32; 4]); 2] = [
        (&[0, 3], [1, 0, 1, 0]),
        (&[3], [0, 0, 1, 0]),
    ];
    for (present, expected) in scenarios {
        let mut seeds = existence_seeds(present);
        seeds.push((CP_EPD, 0x1234));

        let (py_prepared, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
        let (ts_prepared, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);

        for (var_id, want) in expected.iter().enumerate() {
            assert_eq!(
                common::read_var(&py_prepared, &py_sim, var_id),
                *want,
                "py existence counter {var_id} for present {present:?}"
            );
            assert_eq!(
                common::read_var(&ts_prepared, &ts_sim, var_id),
                *want,
                "ts existence counter {var_id} for present {present:?}"
            );
        }
        assert_eq!(
            py_sim.last_executed_cost.triggers,
            ts_sim.last_executed_cost.triggers,
            "existence executed triggers must agree for present {present:?}"
        );
        assert_eq!(py_sim.read_epd(CP_EPD), 0x1234, "py must not bind CP");
        assert_eq!(ts_sim.read_epd(CP_EPD), 0x1234, "ts must not bind CP");
    }
}

#[test]
fn player_exists_runtime_guard_returns_false_for_slot_eight() {
    let dir = tempdir("player-exists-guard-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "p = Var(0)",
        "p.set(8)",
        "when(player_exists(p).eq(1), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const p = Var(0);",
        "p.set(8);",
        "when(playerExists(p).eq(1), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "guard.py", &py_body);
    let ts_path = write_ts(dir.path(), "guard.ts", &ts_body);

    // Slot 8 must leave the result at 0 without reading past the table.
    let mut seeds = existence_seeds(&[0]);
    seeds.push((CP_EPD, 0x1234));

    let (py, ts) = common::run_case("player_exists_guard", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 0, "py out-of-range existence must be false");
    assert_eq!(ts.count, 0, "ts out-of-range existence must be false");
    assert_eq!(py.cp_value, 0x1234, "py must not bind CP on the guard path");
    assert_eq!(ts.cp_value, 0x1234, "ts must not bind CP on the guard path");
}

#[test]
fn user_tick_and_is_user_read_the_seeded_dwords() {
    let dir = tempdir("player-readers-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "u = user_player()",
        "when(u.eq(2), lambda: count.iadd(1))",
        "when(is_user(2), lambda: count.iadd(1))",
        "t = game_tick()",
        "when(t.ge(7), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const u = userPlayer();",
        "when(u.eq(2), () => count.iadd(1));",
        "when(isUser(2), () => count.iadd(1));",
        "const t = gameTick();",
        "when(t.ge(7), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "readers.py", &py_body);
    let ts_path = write_ts(dir.path(), "readers.ts", &ts_body);
    let seeds = vec![
        (epd_u32(USER_PLAYER_ADDR), 2),
        (epd_u32(GAME_TICK_ADDR), 7),
        (CP_EPD, 0x1234),
    ];

    let (py, ts) = common::run_case("readers", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 3, "user, is_user, and game_tick all hold");
    assert_eq!(ts.count, 3);
}

#[test]
fn player_field_reads_and_writes_the_minerals_dword() {
    let dir = tempdir("player-field-");
    let py_body = indent_py(&[
        "count = Var(0)",
        "f = Player(3).minerals",
        "first = f.read()",
        "when(first.eq(777), lambda: count.iadd(1))",
        "f.set(500)",
        "second = f.read()",
        "when(second.eq(500), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "const f = new Player(3).minerals;",
        "const first = f.read();",
        "when(first.eq(777), () => count.iadd(1));",
        "f.set(500);",
        "const second = f.read();",
        "when(second.eq(500), () => count.iadd(1));",
    ]);

    let py_path = write_py(dir.path(), "field.py", &py_body);
    let ts_path = write_ts(dir.path(), "field.ts", &ts_body);
    let minerals_epd = epd_u32(MINERALS_ADDR + 4 * 3);
    let seeds = vec![(minerals_epd, 777), (CP_EPD, 0x1234)];

    let (py, _ts) = common::run_case("player_field", &py_path, &ts_path, &seeds);
    assert_eq!(py.count, 2, "read seeded 777, then read back the 500 write");

    let (_prepared, sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    assert_eq!(
        sim.external_epds.get(&minerals_epd).copied(),
        Some(500),
        "f.set(500) must land in the minerals dword"
    );
}

#[test]
fn broadcast_records_active_slots_and_restores_cp() {
    let dir = tempdir("player-broadcast-");
    let py_body = indent_py(&[
        "pv = PVariable()",
        "def body(player):",
        "    q = Var(0)",
        "    q.set(player)",
        "    q.iadd(10)",
        "    pv.set(player, q)",
        "for_each_player(body)",
    ]);
    let ts_body = indent_ts(&[
        "const pv = new PVariable();",
        "forEachPlayer((player) => {",
        "  const q = Var(0);",
        "  q.set(player);",
        "  q.iadd(10);",
        "  pv.set(player, q);",
        "});",
    ]);

    let py_path = write_py(dir.path(), "broadcast.py", &py_body);
    let ts_path = write_ts(dir.path(), "broadcast.ts", &ts_body);

    // Player 1 has left; the other seven slots record `id + 10`.
    let mut seeds = existence_seeds(&[0, 2, 3, 4, 5, 6, 7]);
    seeds.push((CP_EPD, 0x1234));

    let (py_prepared, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (ts_prepared, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);

    let expected = [10, 0, 12, 13, 14, 15, 16, 17];
    for (slot, want) in expected.iter().enumerate() {
        assert_eq!(
            common::read_var(&py_prepared, &py_sim, slot),
            *want,
            "py PVariable slot {slot}"
        );
        assert_eq!(
            common::read_var(&ts_prepared, &ts_sim, slot),
            *want,
            "ts PVariable slot {slot}"
        );
    }

    assert_eq!(py_sim.read_epd(CP_EPD), 0x1234, "py must preserve CP");
    assert_eq!(ts_sim.read_epd(CP_EPD), 0x1234, "ts must preserve CP");
}

#[test]
fn for_each_player_does_not_overwrite_cp_inside_body() {
    let dir = tempdir("player-no-cp-");
    let py_body = indent_py(&[
        "pv = PVariable()",
        "def body(player):",
        "    pv.set(player, read_current_player_register())",
        "for_each_player(body, players=[0, 2])",
    ]);
    let ts_body = indent_ts(&[
        "const pv = new PVariable();",
        "forEachPlayer((player) => {",
        "  pv.set(player, readCurrentPlayerRegister());",
        "}, { players: [0, 2] });",
    ]);

    let py_path = write_py(dir.path(), "no_cp.py", &py_body);
    let ts_path = write_ts(dir.path(), "no_cp.ts", &ts_body);

    let mut seeds = existence_seeds(&[0, 2]);
    seeds.push((CP_EPD, 0x1234));

    let (py_prepared, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (ts_prepared, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);

    for slot in [0, 2] {
        assert_eq!(common::read_var(&py_prepared, &py_sim, slot), 0x1234);
        assert_eq!(common::read_var(&ts_prepared, &ts_sim, slot), 0x1234);
    }
    assert_eq!(py_sim.read_epd(CP_EPD), 0x1234);
    assert_eq!(ts_sim.read_epd(CP_EPD), 0x1234);
}

#[test]
fn all_broadcast_runs_as_local_observer_and_restores_cp() {
    let dir = tempdir("player-all-local-");
    let py_body = indent_py(&[
        "display_text_all(\"hello\")",
        "play_wav_all(\"sound\")",
    ]);
    let ts_body = indent_ts(&[
        "displayTextAll(\"hello\");",
        "playWavAll(\"sound\");",
    ]);

    let py_path = write_py(dir.path(), "all_local.py", &py_body);
    let ts_path = write_ts(dir.path(), "all_local.ts", &ts_body);

    // Observer slot 8 should still run without error and restore ambient CP.
    let seeds = vec![(epd_u32(USER_PLAYER_ADDR), 8), (CP_EPD, 0x1234)];

    let (_py_prepared, py_sim) = run_with_sim(Frontend::Py, &py_path, &seeds);
    let (_ts_prepared, ts_sim) = run_with_sim(Frontend::Ts, &ts_path, &seeds);

    assert_eq!(py_sim.read_epd(CP_EPD), 0x1234, "py broadcast must restore CP");
    assert_eq!(ts_sim.read_epd(CP_EPD), 0x1234, "ts broadcast must restore CP");
}

fn parity_bodies() -> (String, String) {
    let py_body = indent_py(&[
        "count = Var(0)",
        "turbo()",
        "u = user_player()",
        "when(u.eq(2), lambda: count.iadd(1))",
        "when(is_user(2), lambda: count.iadd(1))",
        "t = game_tick()",
        "when(t.ge(1), lambda: count.iadd(1))",
        "when(player_exists(0).eq(1), lambda: count.iadd(1))",
        "when(player_exists(1).eq(1), lambda: count.iadd(1))",
        "p = Var(0)",
        "p.set(2)",
        "when(player_exists(p).eq(1), lambda: count.iadd(1))",
        "f = Player(3).minerals",
        "f.set(500)",
        "f.iadd(3)",
        "f.isub(2)",
        "when(f.eq(501), lambda: count.iadd(1))",
        "for_each_player(lambda q: count.iadd(1), players=[0, 2])",
        "for_each_player(lambda q: count.iadd(1))",
        "display_text_all(\"hello\")",
        "play_wav_all(\"sound\")",
        "minimap_ping_all(3)",
        "center_view_all(4)",
        "set_mission_objectives_all(\"obj\")",
        "talking_portrait_all(5, 100)",
        "pv = PVariable(5)",
        "pv.set(3, 9)",
        "when(pv.get(3).eq(9), lambda: count.iadd(1))",
    ]);
    let ts_body = indent_ts(&[
        "const count = Var(0);",
        "turbo();",
        "const u = userPlayer();",
        "when(u.eq(2), () => count.iadd(1));",
        "when(isUser(2), () => count.iadd(1));",
        "const t = gameTick();",
        "when(t.ge(1), () => count.iadd(1));",
        "when(playerExists(0).eq(1), () => count.iadd(1));",
        "when(playerExists(1).eq(1), () => count.iadd(1));",
        "const p = Var(0);",
        "p.set(2);",
        "when(playerExists(p).eq(1), () => count.iadd(1));",
        "const f = new Player(3).minerals;",
        "f.set(500);",
        "f.iadd(3);",
        "f.isub(2);",
        "when(f.eq(501), () => count.iadd(1));",
        "forEachPlayer((q) => count.iadd(1), { players: [0, 2] });",
        "forEachPlayer((q) => count.iadd(1));",
        "displayTextAll(\"hello\");",
        "playWavAll(\"sound\");",
        "minimapPingAll(3);",
        "centerViewAll(4);",
        "setMissionObjectivesAll(\"obj\");",
        "talkingPortraitAll(5, 100);",
        "const pv = new PVariable(5);",
        "pv.set(3, 9);",
        "when(pv.get(3).eq(9), () => count.iadd(1));",
    ]);
    (py_body, ts_body)
}

#[test]
fn frontends_agree_on_the_whole_surface_and_emit_identical_hir() {
    let dir = tempdir("player-parity-");
    let (py_body, ts_body) = parity_bodies();
    let py_path = write_py(dir.path(), "all.py", &py_body);
    let ts_path = write_ts(dir.path(), "all.ts", &ts_body);

    let mut seeds = existence_seeds(&[0, 2]);
    seeds.push((epd_u32(USER_PLAYER_ADDR), 2));
    seeds.push((epd_u32(GAME_TICK_ADDR), 5));
    seeds.push((epd_u32(MINERALS_ADDR + 4 * 3), 777));
    seeds.push((CP_EPD, 0x1234));

    let (py, ts) = common::run_case("all", &py_path, &ts_path, &seeds);
    // user + is_user + tick + pex(0) + runtime(2) + field eq + 2 unrolled
    // + 2 looped + PVariable get = 11; pex(1) is absent and contributes 0.
    assert_eq!(py.count, 11, "py whole-surface count");
    assert_eq!(ts.count, 11, "ts whole-surface count");
    assert_eq!(py.cp_value, 0x1234, "py CP restore");
    assert_eq!(ts.cp_value, 0x1234, "ts CP restore");

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
fn player_costs_are_pinned_for_both_frontends() {
    let dir = tempdir("player-costs-");
    // Observed through `dump-ir --json --loops 1`. The 32-bit shared reader and
    // `PlayerField.read` pay 33 = 32 bit tests + 1 accumulator reset, while the
    // 8-bit `dwUserPlayerId` reader pays 9 = 8 bit tests + 1 reset. Runtime
    // `player_exists` is a condition probe (15 static triggers, 24 executed),
    // not the old CP-reader body. PVariable's runtime index paths are
    // substrate-driven (25/11 and 6/5), not the old 1/1 row.
    let cases = [
        case(
            "turbo",
            "    turbo()\n",
            "  turbo();\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "user_player",
            "    user_player()\n",
            "  userPlayer();\n",
            (9, 8, 9),
            (9, 8, 1),
        ),
        case(
            "game_tick",
            "    game_tick()\n",
            "  gameTick();\n",
            (33, 32, 33),
            (33, 32, 1),
        ),
        case(
            "is_user",
            "    when(is_user(3), lambda: None)\n",
            "  when(isUser(3), () => {});\n",
            (9, 8, 9),
            (9, 8, 1),
        ),
        case(
            "pex_const",
            "    player_exists(3)\n",
            "  playerExists(3);\n",
            (2, 1, 2),
            (2, 1, 1),
        ),
        case(
            "pex_runtime",
            "    p = Var(1)\n    when(player_exists(p).eq(1), lambda: None)\n",
            "  const p = Var(1);\n  when(playerExists(p).eq(1), () => {});\n",
            (15, 2, 154),
            (24, 2, 729),
        ),
        case(
            "loop_active_body",
            "    for_each_player(lambda q: SetDeaths(9000, SetTo, q, 0))\n",
            "  forEachPlayer((q) => { SetDeaths(9000, SetTo, q, 0); });\n",
            (18, 4, 158),
            (217, 33, 5864),
        ),
        case(
            "loop_inactive_body",
            "    for_each_player(lambda q: SetDeaths(9000, SetTo, q, 0), active_only=False)\n",
            "  forEachPlayer((q) => { SetDeaths(9000, SetTo, q, 0); }, { activeOnly: false });\n",
            (2, 1, 4),
            (17, 9, 32),
        ),
        case(
            "unroll_gated",
            "    for_each_player(lambda q: SetDeaths(9000, SetTo, q, 0), players=[0, 2])\n",
            "  forEachPlayer((q) => { SetDeaths(9000, SetTo, q, 0); }, { players: [0, 2] });\n",
            (6, 4, 6),
            (6, 4, 4),
        ),
        case(
            "unroll_ungated",
            "    for_each_player(lambda q: SetDeaths(9000, SetTo, q, 0), active_only=False, players=[0, 2])\n",
            "  forEachPlayer((q) => { SetDeaths(9000, SetTo, q, 0); }, { activeOnly: false, players: [0, 2] });\n",
            (1, 0, 2),
            (1, 0, 2),
        ),
        case(
            "broadcast_display",
            "    display_text_all(\"hello\")\n",
            "  displayTextAll(\"hello\");\n",
            (46, 40, 113),
            (47, 40, 137),
        ),
        case(
            "field_read",
            "    Player(3).minerals.read()\n",
            "  new Player(3).minerals.read();\n",
            (33, 32, 33),
            (33, 32, 1),
        ),
        case(
            "field_set",
            "    Player(3).minerals.set(500)\n",
            "  new Player(3).minerals.set(500);\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "field_iadd",
            "    Player(3).minerals.iadd(500)\n",
            "  new Player(3).minerals.iadd(500);\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "field_isub",
            "    Player(3).minerals.isub(500)\n",
            "  new Player(3).minerals.isub(500);\n",
            (1, 0, 1),
            (1, 0, 1),
        ),
        case(
            "field_eq",
            "    when(Player(3).minerals.eq(777), lambda: None)\n",
            "  when(new Player(3).minerals.eq(777), () => {});\n",
            (0, 0, 0),
            (0, 0, 0),
        ),
        case(
            "pvar_init0",
            "    PVariable()\n",
            "  new PVariable();\n",
            (1, 0, 64),
            (1, 0, 64),
        ),
        case(
            "pvar_init5",
            "    PVariable(5)\n",
            "  new PVariable(5);\n",
            (2, 0, 72),
            (1, 0, 8),
        ),
        case(
            "pvar_const_get",
            "    v = PVariable()\n    v.get(3)\n",
            "  const v = new PVariable();\n  v.get(3);\n",
            (1, 0, 64),
            (1, 0, 64),
        ),
        case(
            "pvar_const_set",
            "    v = PVariable()\n    v.set(3, 9)\n",
            "  const v = new PVariable();\n  v.set(3, 9);\n",
            (2, 0, 65),
            (1, 0, 1),
        ),
        case(
            "pvar_rt_get",
            "    v = PVariable()\n    i = Var(2)\n    v.get(i)\n",
            "  const v = new PVariable();\n  const i = Var(2);\n  v.get(i);\n",
            (25, 8, 104),
            (11, 8, 69),
        ),
        case(
            "pvar_rt_set",
            "    v = PVariable()\n    i = Var(2)\n    v.set(i, 9)\n",
            "  const v = new PVariable();\n  const i = Var(2);\n  v.set(i, 9);\n",
            (6, 7, 72),
            (5, 7, 6),
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

        // §5 ceilings, asserted only where the observed cost fits the row.
        let (static_t, _, _) = cost_case.static_cost;
        let (exec_t, _, _) = cost_case.executed_cost;
        match cost_case.name {
            "pex_runtime" => {
                assert!(static_t <= 120, "pex runtime static ceiling");
                assert!(exec_t <= 115, "pex runtime executed ceiling");
            }
            "loop_active_body" => {
                assert!(static_t <= 130, "loop active static ceiling");
                assert!(exec_t <= 900, "loop active executed ceiling");
            }
            "loop_inactive_body" => {
                assert!(static_t <= 60, "loop inactive static ceiling");
                assert!(exec_t <= 100, "loop inactive executed ceiling");
            }
            // The body adds no triggers (one action), so the n=2 row bounds
            // both modes at 45 triggers.
            "unroll_gated" | "unroll_ungated" => {
                assert!(static_t <= 45, "unrolled static ceiling");
                assert!(exec_t <= 45, "unrolled executed ceiling");
            }
            _ => {}
        }
    }
}
