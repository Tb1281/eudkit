//! `match_names` / `matchNames` against the LIR simulator (`T-WP8`).
//!
//! The player-name table is outside the assembled payload, so the simulator
//! exposes it through `external_epds`: seeding player 0's 36-byte record lets
//! these tests exercise the real condition-15 masked comparison without a map.
//! Both frontends are run through the shared capture/lowering pipeline; the
//! matcher must be byte-identical and semantically identical.

use std::fs;
use std::path::{Path, PathBuf};

use eudkit::{capture_source, prepare_hir_json, Frontend};
use eudkit_lir::{epd_u32, init_simulation, SimulationState};

const PLAYER_NAME_TABLE: u32 = 0x57EE_E8;
const PLAYER_NAME_EPDS: u32 = 9;
const PLAYER_NAME_BYTES: usize = 36;
const MARKER_EPD: u32 = 9000;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn tempdir() -> tempfile::TempDir {
    tempfile::tempdir().expect("create tempdir")
}

fn string_list(items: &[&str]) -> String {
    let rendered: Vec<String> = items
        .iter()
        .map(|item| serde_json::to_string(item).expect("render string"))
        .collect();
    format!("[{}]", rendered.join(", "))
}

fn int_list(items: &[u32]) -> String {
    let rendered: Vec<String> = items.iter().map(u32::to_string).collect();
    format!("[{}]", rendered.join(", "))
}

fn py_source(names: &[&str], players: &[u32], body: &str) -> String {
    format!(
        "from py.player import match_names\n\
         from py import build_module, SetDeaths, SetTo\n\n\
         def on_match(player, name):\n    {body}\n\n\
         def prog():\n    match_names({}, on_match, players={})\n\n\
         module = build_module(prog)\n",
        string_list(names),
        int_list(players),
    )
}

fn ts_source(names: &[&str], players: &[u32], body: &str) -> String {
    let root = repo_root().display();
    format!(
        "import {{ matchNames }} from \"{root}/ts/player.ts\";\n\
         import {{ buildModule, SetDeaths, SetTo }} from \"{root}/ts/index.ts\";\n\n\
         const mod = buildModule(() => {{\n\
           matchNames({}, (player, name) => {{\n    {body}\n  }}, {});\n\
         }});\n\
         export const module = mod;\n",
        string_list(names),
        int_list(players),
    )
}

fn seed_name(sim: &mut SimulationState, player: u32, bytes: &[u8]) {
    assert!(bytes.len() <= PLAYER_NAME_BYTES, "name record is 36 bytes");
    let mut record = [0u8; PLAYER_NAME_BYTES];
    record[..bytes.len()].copy_from_slice(bytes);
    let base = epd_u32(PLAYER_NAME_TABLE).wrapping_add(PLAYER_NAME_EPDS * player);
    for w in 0..PLAYER_NAME_EPDS {
        let off = (w as usize) * 4;
        let dword = u32::from_le_bytes(record[off..off + 4].try_into().unwrap());
        sim.external_epds.insert(base.wrapping_add(w), dword);
    }
}

fn run_match(
    frontend: Frontend,
    dir: &Path,
    stem: &str,
    names: &[&str],
    players: &[u32],
    body_py: &str,
    body_ts: &str,
    seed: &[u8],
) -> SimulationState {
    let source = match frontend {
        Frontend::Py => py_source(names, players, body_py),
        Frontend::Ts => ts_source(names, players, body_ts),
    };
    let path = dir.join(format!("{stem}.{}", frontend.as_str()));
    fs::write(&path, source).expect("write fixture source");

    let hir = capture_source(&path, frontend).expect("capture frontend HIR");
    let prepared = prepare_hir_json(&hir, None).expect("lower HIR");
    let mut sim = init_simulation(&prepared.lir).expect("init simulator");
    seed_name(&mut sim, 0, seed);
    sim.step_loop();
    sim
}

fn run_both(
    stem: &str,
    names: &[&str],
    players: &[u32],
    body_py: &str,
    body_ts: &str,
    seed: &[u8],
) -> [(usize, usize, usize, u32); 2] {
    let dir = tempdir();
    let py = run_match(
        Frontend::Py,
        dir.path(),
        stem,
        names,
        players,
        body_py,
        body_ts,
        seed,
    );
    let ts = run_match(
        Frontend::Ts,
        dir.path(),
        stem,
        names,
        players,
        body_py,
        body_ts,
        seed,
    );
    let marker = |sim: &SimulationState| sim.external_epds.get(&MARKER_EPD).copied().unwrap_or(0);
    let summarize = |sim: &SimulationState| {
        (
            sim.last_executed_cost.triggers,
            sim.last_executed_cost.conditions,
            sim.last_executed_cost.actions,
            marker(sim),
        )
    };
    let py_result = summarize(&py);
    let ts_result = summarize(&ts);
    assert_eq!(py_result, ts_result, "frontends must agree on execution");
    [py_result, ts_result]
}

fn marker_source() -> (&'static str, &'static str) {
    (
        "SetDeaths(9000, SetTo, player * 10 + (1 if name == \"Armo\" else 2), 0)",
        "SetDeaths(9000, SetTo, player * 10 + (name === \"Armo\" ? 1 : 2), 0);",
    )
}

#[test]
fn player_zero_matches_armoha_not_armo_and_player_one_stays_dark() {
    let (body_py, body_ts) = marker_source();
    for (triggers, conditions, actions, marker) in run_both(
        "armoha",
        &["Armo", "Armoha"],
        &[0, 1],
        body_py,
        body_ts,
        b"Armoha",
    ) {
        assert_eq!(marker, 2, "only the second candidate name may match");
        // Two arms for player 0 (2 + 2 conditions), two failing first guesses
        // for the zeroed player 1 record (1 + 1), and one body action.
        assert_eq!(triggers, 4);
        assert_eq!(conditions, 6);
        assert_eq!(actions, 1);
    }
}

#[test]
fn armo_never_matches_a_record_containing_armoha() {
    let body_py = "SetDeaths(9000, SetTo, 1, 0)";
    let body_ts = "SetDeaths(9000, SetTo, 1, 0);";
    for (triggers, conditions, actions, marker) in
        run_both("prefix", &["Armo"], &[0], body_py, body_ts, b"Armoha")
    {
        assert_eq!(marker, 0, "the NUL-inclusive mask rejects the prefix");
        assert_eq!(triggers, 1);
        assert_eq!(conditions, 2);
        assert_eq!(actions, 0);
    }
}

#[test]
fn trailing_garbage_after_the_nul_does_not_block_a_match() {
    let body_py = "SetDeaths(9000, SetTo, 7, 0)";
    let body_ts = "SetDeaths(9000, SetTo, 7, 0);";
    let mut seed = [0xAAu8; PLAYER_NAME_BYTES];
    seed[..5].copy_from_slice(b"Armo\0");
    for (triggers, conditions, actions, marker) in
        run_both("garbage", &["Armo"], &[0], body_py, body_ts, &seed)
    {
        assert_eq!(
            marker, 7,
            "bytes outside the NUL-inclusive mask are ignored"
        );
        assert_eq!(triggers, 1);
        assert_eq!(conditions, 2);
        assert_eq!(actions, 1);
    }
}

#[test]
fn a_full_thirty_five_byte_name_matches_all_nine_words() {
    let name = "A".repeat(35);
    let body_py = "SetDeaths(9000, SetTo, 9, 0)";
    let body_ts = "SetDeaths(9000, SetTo, 9, 0);";
    let mut seed = [0u8; PLAYER_NAME_BYTES];
    seed[..35].copy_from_slice(name.as_bytes());
    for (triggers, conditions, actions, marker) in
        run_both("long", &[&name], &[0], body_py, body_ts, &seed)
    {
        assert_eq!(marker, 9);
        // Nine word conditions all hold, plus the body action.
        assert_eq!(triggers, 1);
        assert_eq!(conditions, 9);
        assert_eq!(actions, 1);
    }
}

#[test]
fn frontends_emit_byte_identical_hir_for_the_two_name_fixture() {
    let dir = tempdir();
    let names = ["Armo", "Armoha"];
    let players = [0, 1];
    let (body_py, body_ts) = marker_source();
    let mut hirs = Vec::new();
    for frontend in [Frontend::Py, Frontend::Ts] {
        let source = match frontend {
            Frontend::Py => py_source(&names, &players, body_py),
            Frontend::Ts => ts_source(&names, &players, body_ts),
        };
        let path: PathBuf = dir.path().join(format!("parity.{}", frontend.as_str()));
        fs::write(&path, source).unwrap();
        hirs.push(capture_source(&path, frontend).expect("capture frontend HIR"));
    }
    assert_eq!(hirs[0], hirs[1]);
}
