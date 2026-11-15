//! Cross-language stock-trigger catalog tests.
//!
//! Each test compiles the same program from the TypeScript and Python DSLs,
//! asserts byte-identical HIR, then checks the emitted raw trigger fields and
//! the compiled map's TRIG records.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{RAW_TRIGGER_BYTE_SIZE, RawTrigger, lir2payload, static_cost};
use eudkit_lower::{compile_hir_to_map, lower_document, lower_optimized_module};
use eudkit_opt::cell::analyze_storage;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn run_node(script: &str) -> String {
    let out = Command::new("node")
        .args(["--input-type=module", "-e", script])
        .current_dir(repo_root())
        .output()
        .expect("node must run TS snippet");
    assert!(
        out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn run_python(script: &str) -> String {
    let out = Command::new("python3")
        .args(["-c", script])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run Python snippet");
    assert!(
        out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn assert_hir_parity(ts: &str, py: &str) -> String {
    let ts_hir = run_node(ts);
    let py_hir = run_python(py);
    assert_eq!(ts_hir, py_hir, "TS and Python must emit byte-identical HIR");
    ts_hir
}

fn read_section(out_scx: &Path, name: &[u8; 4]) -> Vec<u8> {
    let mut archive =
        stormlib::Archive::open(out_scx, stormlib::OpenArchiveFlags::empty()).unwrap();
    let raw = archive
        .open_file("staredit\\scenario.chk")
        .unwrap()
        .read_all()
        .unwrap();
    drop(archive);
    let mut chkt = eudkit_map::chk::Chk::new();
    assert!(chkt.loadchk(&raw), "compiled map CHK must load");
    chkt.getsection(name)
        .unwrap_or_else(|| panic!("compiled map must contain {}", String::from_utf8_lossy(name)))
        .to_vec()
}

/// The compiled program triggers live in the STRx payload as runtime
/// 2,408-byte records. Scan for a record with the expected first condition
/// and action.
fn find_payload_trigger<'a>(strx: &'a [u8], cond_type: u8, act_type: u8) -> &'a [u8] {
    strx.windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.conds[0].condtype == cond_type && raw.acts[0].acttype == act_type
        })
        .unwrap_or_else(|| {
            panic!("no payload trigger with condition {cond_type} and action {act_type}")
        })
}

#[test]
fn stock_vocabulary_tracer_bullet_emits_20_byte_condition_and_32_byte_action() {
    let ts = r#"
import { Anywhere, AtLeast, Bring, CreateUnit, Foes, P1, Trigger, buildModule } from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [Bring(Foes, AtLeast, 7, 65, Anywhere)],
    acts: [CreateUnit(3, 65, Anywhere, P1)],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import Anywhere, AtLeast, Bring, CreateUnit, Foes, P1, Trigger, buildModule
def prog():
    Trigger(
        conds=[Bring(Foes, AtLeast, 7, 65, Anywhere)],
        acts=[CreateUnit(3, 65, Anywhere, P1)],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let lir = lower_document(&doc).unwrap();
    assert_eq!(static_cost(&lir).triggers, 1);

    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(
        payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap(),
    );
    assert_eq!(raw.conds[0].condtype, 3);
    assert_eq!(raw.conds[0].locid, 64);
    assert_eq!(raw.conds[0].player, 14);
    assert_eq!(raw.conds[0].amount, 7);
    assert_eq!(raw.conds[0].unit, 65);
    assert_eq!(raw.conds[0].comparison, 0);

    assert_eq!(raw.acts[0].acttype, 44);
    assert_eq!(raw.acts[0].mask, 64);
    assert_eq!(raw.acts[0].dest, 0);
    assert_eq!(raw.acts[0].value, 0);
    assert_eq!(raw.acts[0].unit, 65);
    assert_eq!(raw.acts[0].modifier, 3);
    assert_eq!(raw.acts[0].flags, 20);

    // The compiled map carries the same 20-byte condition and 32-byte action
    // inside a 2408-byte record in the STRx payload.
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-tracer.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();
    let strx = read_section(&out_scx, b"STRx");
    let record = find_payload_trigger(&strx, 3, 44);
    let raw = RawTrigger::decode(record.try_into().unwrap());
    assert_eq!(raw.conds[0].condtype, 3);
    assert_eq!(raw.conds[0].locid, 64);
    assert_eq!(raw.conds[0].player, 14);
    assert_eq!(raw.conds[0].amount, 7);
    assert_eq!(raw.conds[0].unit, 65);
    assert_eq!(raw.acts[0].acttype, 44);
    assert_eq!(raw.acts[0].mask, 64);
    assert_eq!(raw.acts[0].dest, 0);
    assert_eq!(raw.acts[0].unit, 65);
    assert_eq!(raw.acts[0].modifier, 3);
    assert_eq!(raw.acts[0].flags, 20);
}

#[test]
fn remaining_stock_conditions_emit_expected_20_byte_records() {
    let ts = r#"
import {
  Accumulate, AtLeast, AtMost, Command, CommandLeast, CommandLeastAt,
  CommandMost, CommandMostAt, CountdownTimer, ElapsedTime, Exactly, Gas,
  HighestScore, Kills, LeastKills, LeastResources, LowestScore, MostKills,
  MostResources, Opponents, OreAndGas, P1, P2, Score, ScoreCustom, ScoreKills,
  Switch, SwitchCleared, Trigger, buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [
      Accumulate(P1, AtLeast, 5, Gas),
      Command(P2, AtMost, 6, 65),
      CommandLeast(65),
      CommandLeastAt(65, 7),
      CommandMost(65),
      CommandMostAt(65, 8),
      CountdownTimer(Exactly, 100),
      ElapsedTime(AtLeast, 200),
      HighestScore(ScoreKills),
    ],
    acts: [],
    preserved: true,
  });
  new Trigger({
    conds: [
      Kills(P1, AtLeast, 3, 65),
      LeastKills(65),
      LeastResources(OreAndGas),
      LowestScore(ScoreCustom),
      MostKills(65),
      MostResources(Gas),
      Opponents(P2, AtMost, 2),
      Score(P1, ScoreKills, Exactly, 9),
      Switch(200, SwitchCleared),
    ],
    acts: [],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Accumulate, AtLeast, AtMost, Command, CommandLeast, CommandLeastAt,
    CommandMost, CommandMostAt, CountdownTimer, ElapsedTime, Exactly, Gas,
    HighestScore, Kills, LeastKills, LeastResources, LowestScore, MostKills,
    MostResources, Opponents, OreAndGas, P1, P2, Score, ScoreCustom, ScoreKills,
    Switch, SwitchCleared, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[
            Accumulate(P1, AtLeast, 5, Gas),
            Command(P2, AtMost, 6, 65),
            CommandLeast(65),
            CommandLeastAt(65, 7),
            CommandMost(65),
            CommandMostAt(65, 8),
            CountdownTimer(Exactly, 100),
            ElapsedTime(AtLeast, 200),
            HighestScore(ScoreKills),
        ],
        acts=[],
        preserved=True,
    )
    Trigger(
        conds=[
            Kills(P1, AtLeast, 3, 65),
            LeastKills(65),
            LeastResources(OreAndGas),
            LowestScore(ScoreCustom),
            MostKills(65),
            MostResources(Gas),
            Opponents(P2, AtMost, 2),
            Score(P1, ScoreKills, Exactly, 9),
            Switch(200, SwitchCleared),
        ],
        acts=[],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    // Lower the authored triggers directly: this test pins condition-record
    // encoding, and the post-schedule dead-gate pass would remove a 0-act gate
    // before it could be encoded.
    let storage = analyze_storage(&doc.module);
    let lir = lower_optimized_module(&doc.module, &storage, &[]).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 2);
    assert_eq!(cost.conditions, 18);
    assert_eq!(cost.actions, 0);

    let payload = lir2payload(&lir).unwrap();
    let raw0 = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    let raw1 = RawTrigger::decode(
        payload.data[RAW_TRIGGER_BYTE_SIZE..2 * RAW_TRIGGER_BYTE_SIZE]
            .try_into()
            .unwrap(),
    );

    let expected0: [(u8, u32, u32, u32, u16, u8, u8); 9] = [
        // condtype, locid, player, amount, unit, comparison, restype
        (4, 0, 0, 5, 0, 0, 1),
        (2, 0, 1, 6, 65, 1, 0),
        (16, 0, 0, 0, 65, 0, 0),
        (17, 7, 0, 0, 65, 0, 0),
        (6, 0, 0, 0, 65, 0, 0),
        (7, 8, 0, 0, 65, 0, 0),
        (1, 0, 0, 100, 0, 10, 0),
        (12, 0, 0, 200, 0, 0, 0),
        (9, 0, 0, 0, 0, 0, 4),
    ];
    let expected1: [(u8, u32, u32, u32, u16, u8, u8); 9] = [
        (5, 0, 0, 3, 65, 0, 0),
        (18, 0, 0, 0, 65, 0, 0),
        (20, 0, 0, 0, 0, 0, 2),
        (19, 0, 0, 0, 0, 0, 7),
        (8, 0, 0, 0, 65, 0, 0),
        (10, 0, 0, 0, 0, 0, 1),
        (14, 0, 1, 2, 0, 1, 0),
        (21, 0, 0, 9, 0, 10, 4),
        (11, 0, 0, 0, 0, 3, 200),
    ];
    for (i, want) in expected0.iter().enumerate() {
        let c = &raw0.conds[i];
        assert_eq!(
            (c.condtype, c.locid, c.player, c.amount, c.unit, c.comparison, c.restype),
            *want,
            "condition {i} in trigger 0"
        );
    }
    for (i, want) in expected1.iter().enumerate() {
        let c = &raw1.conds[i];
        assert_eq!(
            (c.condtype, c.locid, c.player, c.amount, c.unit, c.comparison, c.restype),
            *want,
            "condition {i} in trigger 1"
        );
    }
}

#[test]
fn unit_and_location_actions_emit_expected_32_byte_records() {
    let ts = r#"
import {
  All, Anywhere, GiveUnits, KillUnit, KillUnitAt, ModifyUnitEnergy,
  ModifyUnitHangarCount, ModifyUnitHitPoints, ModifyUnitResourceAmount,
  ModifyUnitShields, MoveLocation, MoveUnit, Order, OrderPatrol, P1, P2,
  PropEnable, PropToggle, RemoveUnit, RemoveUnitAt, SetDoodadState,
  SetInvincibility, Trigger, buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: [
      KillUnit(65, P1),
      KillUnitAt(3, 65, Anywhere, P2),
      RemoveUnit(65, P1),
      RemoveUnitAt(All, 65, Anywhere, P2),
      MoveUnit(All, 65, P1, 7, 8),
      GiveUnits(5, 65, P1, Anywhere, P2),
      Order(65, P1, 7, OrderPatrol, 8),
      MoveLocation(9, 65, P1, Anywhere),
      SetDoodadState(PropToggle, 65, P1, Anywhere),
      SetInvincibility(PropEnable, 65, P1, Anywhere),
      ModifyUnitHitPoints(All, 65, P1, Anywhere, 50),
      ModifyUnitEnergy(2, 65, P2, 7, 75),
      ModifyUnitShields(255, 65, P1, Anywhere, 100),
      ModifyUnitResourceAmount(All, P1, Anywhere, 5000),
      ModifyUnitHangarCount(5, All, 65, P1, Anywhere),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    All, Anywhere, GiveUnits, KillUnit, KillUnitAt, ModifyUnitEnergy,
    ModifyUnitHangarCount, ModifyUnitHitPoints, ModifyUnitResourceAmount,
    ModifyUnitShields, MoveLocation, MoveUnit, Order, OrderPatrol, P1, P2,
    PropEnable, PropToggle, RemoveUnit, RemoveUnitAt, SetDoodadState,
    SetInvincibility, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            KillUnit(65, P1),
            KillUnitAt(3, 65, Anywhere, P2),
            RemoveUnit(65, P1),
            RemoveUnitAt(All, 65, Anywhere, P2),
            MoveUnit(All, 65, P1, 7, 8),
            GiveUnits(5, 65, P1, Anywhere, P2),
            Order(65, P1, 7, OrderPatrol, 8),
            MoveLocation(9, 65, P1, Anywhere),
            SetDoodadState(PropToggle, 65, P1, Anywhere),
            SetInvincibility(PropEnable, 65, P1, Anywhere),
            ModifyUnitHitPoints(All, 65, P1, Anywhere, 50),
            ModifyUnitEnergy(2, 65, P2, 7, 75),
            ModifyUnitShields(255, 65, P1, Anywhere, 100),
            ModifyUnitResourceAmount(All, P1, Anywhere, 5000),
            ModifyUnitHangarCount(5, All, 65, P1, Anywhere),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let lir = lower_document(&doc).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 1);
    assert_eq!(cost.conditions, 0);
    assert_eq!(cost.actions, 15);

    let expected: [(u8, u32, u32, u32, u16, u8, u8); 15] = [
        // acttype, mask, dest, value, unit, modifier, flags
        (22, 0, 0, 0, 65, 0, 20),
        (23, 64, 1, 0, 65, 3, 20),
        (24, 0, 0, 0, 65, 0, 20),
        (25, 64, 1, 0, 65, 0, 20),
        (39, 7, 0, 8, 65, 0, 20),
        (48, 64, 0, 1, 65, 5, 20),
        (46, 7, 0, 8, 65, 1, 20),
        (38, 64, 0, 9, 65, 0, 20),
        (42, 64, 0, 0, 65, 6, 20),
        (43, 64, 0, 0, 65, 4, 20),
        (49, 64, 0, 50, 65, 0, 20),
        (50, 7, 1, 75, 65, 2, 20),
        (51, 64, 0, 100, 65, 255, 20),
        (52, 64, 0, 5000, 0, 0, 4),
        (53, 64, 0, 5, 65, 0, 20),
    ];

    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    for (i, want) in expected.iter().enumerate() {
        let a = &raw.acts[i];
        assert_eq!(
            (a.acttype, a.mask, a.dest, a.value, a.unit, a.modifier, a.flags),
            *want,
            "action {i}"
        );
    }

    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-unit-actions.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();
    let strx = read_section(&out_scx, b"STRx");
    let record = strx
        .windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.acts[0].acttype == 22 && raw.acts[1].acttype == 23
        })
        .expect("compiled map must contain the unit action trigger");
    let raw = RawTrigger::decode(record.try_into().unwrap());
    for (i, want) in expected.iter().enumerate() {
        let a = &raw.acts[i];
        assert_eq!(
            (a.acttype, a.mask, a.dest, a.value, a.unit, a.modifier, a.flags),
            *want,
            "compiled action {i}"
        );
    }
}

#[test]
fn game_state_and_ui_actions_emit_expected_records_and_strings() {
    let ts = r#"
import {
  Add, Ally, Anywhere, CenterView, Draw, Gas, MinimapPing, MuteUnitSpeech,
  P1, P2, PauseGame, PauseTimer, RunAIScript, RunAIScriptAt, ScoreKills,
  SetAllianceStatus, SetCountdownTimer, SetMissionObjectives, SetNextScenario,
  SetResources, SetScore, SetSwitch, Subtract, SwitchActionToggle,
  TalkingPortrait, Trigger, UnMuteUnitSpeech, UnpauseGame, UnpauseTimer,
  buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: [
      PauseGame(),
      UnpauseGame(),
      CenterView(Anywhere),
      SetMissionObjectives("New objectives"),
      SetSwitch(200, SwitchActionToggle),
      SetCountdownTimer(Add, 5000),
      RunAIScript(10),
      RunAIScriptAt(11, 7),
      SetResources(P2, Subtract, 5000, Gas),
      SetScore(P1, Add, 100, ScoreKills),
      MinimapPing(Anywhere),
      TalkingPortrait(65, 1000),
      MuteUnitSpeech(),
      UnMuteUnitSpeech(),
      SetNextScenario("next.scm"),
      PauseTimer(),
      UnpauseTimer(),
      Draw(),
      SetAllianceStatus(P1, Ally),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Add, Ally, Anywhere, CenterView, Draw, Gas, MinimapPing, MuteUnitSpeech,
    P1, P2, PauseGame, PauseTimer, RunAIScript, RunAIScriptAt, ScoreKills,
    SetAllianceStatus, SetCountdownTimer, SetMissionObjectives, SetNextScenario,
    SetResources, SetScore, SetSwitch, Subtract, SwitchActionToggle,
    TalkingPortrait, Trigger, UnMuteUnitSpeech, UnpauseGame, UnpauseTimer,
    buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            PauseGame(),
            UnpauseGame(),
            CenterView(Anywhere),
            SetMissionObjectives("New objectives"),
            SetSwitch(200, SwitchActionToggle),
            SetCountdownTimer(Add, 5000),
            RunAIScript(10),
            RunAIScriptAt(11, 7),
            SetResources(P2, Subtract, 5000, Gas),
            SetScore(P1, Add, 100, ScoreKills),
            MinimapPing(Anywhere),
            TalkingPortrait(65, 1000),
            MuteUnitSpeech(),
            UnMuteUnitSpeech(),
            SetNextScenario("next.scm"),
            PauseTimer(),
            UnpauseTimer(),
            Draw(),
            SetAllianceStatus(P1, Ally),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let lir = lower_document(&doc).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 1);
    assert_eq!(cost.conditions, 0);
    assert_eq!(cost.actions, 19);

    let expected: [(u8, u32, u32, u32, u32, u16, u8, u8); 19] = [
        // acttype, mask, time, dest, value, unit, modifier, flags
        (5, 0, 0, 0, 0, 0, 0, 4),
        (6, 0, 0, 0, 0, 0, 0, 4),
        (10, 64, 0, 0, 0, 0, 0, 4),
        (12, 0, 0, 0, 0, 0, 0, 4),
        (13, 0, 0, 0, 200, 0, 6, 4),
        (14, 0, 5000, 0, 0, 0, 8, 4),
        (15, 0, 0, 0, 10, 0, 0, 4),
        (16, 7, 0, 0, 11, 0, 0, 4),
        (26, 0, 0, 1, 5000, 1, 9, 4),
        (27, 0, 0, 0, 100, 4, 8, 4),
        (28, 64, 0, 0, 0, 0, 0, 4),
        (29, 0, 1000, 0, 0, 65, 0, 20),
        (30, 0, 0, 0, 0, 0, 0, 4),
        (31, 0, 0, 0, 0, 0, 0, 4),
        (41, 0, 0, 0, 0, 0, 0, 4),
        (54, 0, 0, 0, 0, 0, 0, 4),
        (55, 0, 0, 0, 0, 0, 0, 4),
        (56, 0, 0, 0, 0, 0, 0, 4),
        (57, 0, 0, 0, 0, 1, 0, 4),
    ];

    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    for (i, want) in expected.iter().enumerate() {
        let a = &raw.acts[i];
        assert_eq!(
            (a.acttype, a.mask, a.time, a.dest, a.value, a.unit, a.modifier, a.flags),
            *want,
            "action {i}"
        );
    }
    let objectives_strid = raw.acts[3].strid;
    let scenario_strid = raw.acts[14].strid;
    assert_ne!(objectives_strid, 0);
    assert_ne!(scenario_strid, 0);
    assert_ne!(objectives_strid, scenario_strid);

    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-game-state.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();
    let strx = read_section(&out_scx, b"STRx");
    let ids = parse_strx_ids(&strx);

    let record = strx
        .windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.acts[0].acttype == 5 && raw.acts[1].acttype == 6
        })
        .expect("compiled map must contain the game-state action trigger");
    let raw = RawTrigger::decode(record.try_into().unwrap());
    for (i, want) in expected.iter().enumerate() {
        let a = &raw.acts[i];
        assert_eq!(
            (a.acttype, a.mask, a.time, a.dest, a.value, a.unit, a.modifier, a.flags),
            *want,
            "compiled action {i}"
        );
    }
    assert_eq!(
        ids[raw.acts[3].strid as usize - 1],
        b"New objectives".to_vec()
    );
    assert_eq!(
        ids[raw.acts[14].strid as usize - 1],
        b"next.scm".to_vec()
    );
}

fn parse_strx_ids(section: &[u8]) -> Vec<Vec<u8>> {
    let n = u32::from_le_bytes(section[0..4].try_into().unwrap()) as usize;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let o = 4 + 4 * i;
        let off = u32::from_le_bytes(section[o..o + 4].try_into().unwrap()) as usize;
        let end = section[off..]
            .iter()
            .position(|&b| b == 0)
            .unwrap_or_else(|| panic!("STRx id {}: unterminated string", i + 1));
        out.push(section[off..off + end].to_vec());
    }
    out
}

#[test]
fn leaderboard_actions_emit_expected_records_and_intern_labels_once() {
    let ts = r#"
import {
  Anywhere, Gas, LeaderBoardComputerPlayers, LeaderBoardControl,
  LeaderBoardControlAt, LeaderBoardGoalControl, LeaderBoardGoalControlAt,
  LeaderBoardGoalKills, LeaderBoardGoalResources, LeaderBoardGoalScore,
  LeaderBoardGreed, LeaderBoardKills, LeaderBoardResources, LeaderBoardScore,
  PropDisable, ScoreCustom, ScoreKills, Trigger, buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: [
      LeaderBoardControl(65, "Shared"),
      LeaderBoardControlAt(65, Anywhere, "At"),
      LeaderBoardResources(Gas, "Shared"),
      LeaderBoardKills(65, "Kills"),
      LeaderBoardScore(ScoreKills, "Score"),
      LeaderBoardComputerPlayers(PropDisable),
      LeaderBoardGoalControl(10, 65, "Goal"),
      LeaderBoardGoalControlAt(10, 65, Anywhere, "GoalAt"),
      LeaderBoardGoalResources(10, Gas, "GoalRes"),
      LeaderBoardGoalKills(10, 65, "GoalKills"),
      LeaderBoardGoalScore(10, ScoreCustom, "GoalScore"),
      LeaderBoardGreed(10),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Anywhere, Gas, LeaderBoardComputerPlayers, LeaderBoardControl,
    LeaderBoardControlAt, LeaderBoardGoalControl, LeaderBoardGoalControlAt,
    LeaderBoardGoalKills, LeaderBoardGoalResources, LeaderBoardGoalScore,
    LeaderBoardGreed, LeaderBoardKills, LeaderBoardResources, LeaderBoardScore,
    PropDisable, ScoreCustom, ScoreKills, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            LeaderBoardControl(65, "Shared"),
            LeaderBoardControlAt(65, Anywhere, "At"),
            LeaderBoardResources(Gas, "Shared"),
            LeaderBoardKills(65, "Kills"),
            LeaderBoardScore(ScoreKills, "Score"),
            LeaderBoardComputerPlayers(PropDisable),
            LeaderBoardGoalControl(10, 65, "Goal"),
            LeaderBoardGoalControlAt(10, 65, Anywhere, "GoalAt"),
            LeaderBoardGoalResources(10, Gas, "GoalRes"),
            LeaderBoardGoalKills(10, 65, "GoalKills"),
            LeaderBoardGoalScore(10, ScoreCustom, "GoalScore"),
            LeaderBoardGreed(10),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let lir = lower_document(&doc).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 1);
    assert_eq!(cost.conditions, 0);
    assert_eq!(cost.actions, 12);

    let expected: [(u8, u32, u32, u16, u8, u8); 12] = [
        // acttype, mask, value, unit, modifier, flags
        (17, 0, 0, 65, 0, 20),
        (18, 64, 0, 65, 0, 20),
        (19, 0, 0, 1, 0, 4),
        (20, 0, 0, 65, 0, 20),
        (21, 0, 0, 4, 0, 4),
        (32, 0, 0, 0, 5, 4),
        (33, 0, 10, 65, 0, 20),
        (34, 64, 10, 65, 0, 20),
        (35, 0, 10, 1, 0, 4),
        (36, 0, 10, 65, 0, 20),
        (37, 0, 10, 7, 0, 4),
        (40, 0, 10, 0, 0, 4),
    ];

    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    for (i, want) in expected.iter().enumerate() {
        let a = &raw.acts[i];
        assert_eq!(
            (a.acttype, a.mask, a.value, a.unit, a.modifier, a.flags),
            *want,
            "action {i}"
        );
    }
    assert_ne!(raw.acts[0].strid, 0);
    assert_eq!(
        raw.acts[0].strid, raw.acts[2].strid,
        "the same label must intern once"
    );
    assert_ne!(raw.acts[0].strid, raw.acts[1].strid);

    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-leaderboard.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();
    let strx = read_section(&out_scx, b"STRx");
    let ids = parse_strx_ids(&strx);
    let record = strx
        .windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.acts[0].acttype == 17 && raw.acts[1].acttype == 18
        })
        .expect("compiled map must contain the leaderboard action trigger");
    let raw = RawTrigger::decode(record.try_into().unwrap());
    let labels = [
        "Shared", "At", "Shared", "Kills", "Score", "", "Goal", "GoalAt",
        "GoalRes", "GoalKills", "GoalScore", "",
    ];
    for (i, label) in labels.iter().enumerate() {
        if !label.is_empty() {
            assert_eq!(
                ids[raw.acts[i].strid as usize - 1],
                label.as_bytes().to_vec(),
                "compiled label for action {i}"
            );
        }
    }
}

#[test]
fn transmission_interns_text_and_sound_independently_and_playwav_still_uses_wavid() {
    let ts = r#"
import {
  Add, Anywhere, PlayWAV, SetTo, Subtract, Transmission, Trigger, buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: [
      Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
      Transmission(65, 7, "sound.wav", SetTo, 2000, "Hello"),
      PlayWAV("sound.wav"),
      Transmission(65, Anywhere, "other.wav", Subtract, 3000, "Other"),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Add, Anywhere, PlayWAV, SetTo, Subtract, Transmission, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
            Transmission(65, 7, "sound.wav", SetTo, 2000, "Hello"),
            PlayWAV("sound.wav"),
            Transmission(65, Anywhere, "other.wav", Subtract, 3000, "Other"),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let lir = lower_document(&doc).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 1);
    assert_eq!(cost.conditions, 0);
    assert_eq!(cost.actions, 4);

    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    let first = &raw.acts[0];
    assert_eq!(first.acttype, 7);
    assert_eq!(first.mask, 64);
    assert_eq!(first.time, 1000);
    assert_eq!(first.unit, 65);
    assert_eq!(first.modifier, 8);
    assert_eq!(first.flags, 4);
    assert_ne!(first.strid, 0);
    assert_ne!(first.wavid, 0);
    assert_ne!(first.strid, first.wavid);

    // Same text and sound reuse the same ids across transmissions.
    assert_eq!(raw.acts[1].strid, first.strid);
    assert_eq!(raw.acts[1].wavid, first.wavid);
    // PlayWAV keeps its sound in wavid and has no text id.
    assert_eq!(raw.acts[2].acttype, 8);
    assert_eq!(raw.acts[2].wavid, first.wavid);
    assert_eq!(raw.acts[2].strid, 0);
    // A different text and sound get different ids.
    assert_ne!(raw.acts[3].strid, first.strid);
    assert_ne!(raw.acts[3].wavid, first.wavid);

    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-transmission.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();
    let strx = read_section(&out_scx, b"STRx");
    let ids = parse_strx_ids(&strx);
    let record = strx
        .windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.acts[0].acttype == 7 && raw.acts[2].acttype == 8
        })
        .expect("compiled map must contain the transmission trigger");
    let raw = RawTrigger::decode(record.try_into().unwrap());
    assert_eq!(ids[raw.acts[0].strid as usize - 1], b"Hello".to_vec());
    assert_eq!(ids[raw.acts[0].wavid as usize - 1], b"sound.wav".to_vec());
    assert_eq!(ids[raw.acts[1].strid as usize - 1], b"Hello".to_vec());
    assert_eq!(ids[raw.acts[1].wavid as usize - 1], b"sound.wav".to_vec());
    assert_eq!(ids[raw.acts[2].wavid as usize - 1], b"sound.wav".to_vec());
    assert_eq!(ids[raw.acts[3].strid as usize - 1], b"Other".to_vec());
    assert_eq!(ids[raw.acts[3].wavid as usize - 1], b"other.wav".to_vec());
}

#[test]
fn create_unit_with_properties_allocates_uprp_slots_and_emits_sections() {
    let ts = r#"
import {
  Anywhere, CreateUnitWithProperties, P1, Trigger, UnitProperty, buildModule,
} from "./ts/index.ts";
const first = UnitProperty({
  hitPoints: 50,
  shields: 75,
  energy: 25,
  resource: 300,
  hangar: 2,
  cloaked: true,
  burrowed: false,
  invincible: true,
});
const second = UnitProperty({ hitPoints: 90 });
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: [
      CreateUnitWithProperties(1, 65, Anywhere, P1, first),
      CreateUnitWithProperties(2, 65, Anywhere, P1, second),
      CreateUnitWithProperties(3, 65, Anywhere, P1, first),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Anywhere, CreateUnitWithProperties, P1, Trigger, UnitProperty, buildModule,
)
def prog():
    first = UnitProperty(
        hit_points=50,
        shields=75,
        energy=25,
        resource=300,
        hangar=2,
        cloaked=True,
        burrowed=False,
        invincible=True,
    )
    second = UnitProperty(hit_points=90)
    Trigger(
        conds=[],
        acts=[
            CreateUnitWithProperties(1, 65, Anywhere, P1, first),
            CreateUnitWithProperties(2, 65, Anywhere, P1, second),
            CreateUnitWithProperties(3, 65, Anywhere, P1, first),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let doc = hir_from_json(&hir_json).unwrap();
    let first: [u8; 20] = [
        17, 0, 62, 0, 0, 50, 75, 25, 44, 1, 0, 0, 2, 0, 17, 0, 0, 0, 0, 0,
    ];
    let second: [u8; 20] = [
        0, 0, 2, 0, 0, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ];
    assert_eq!(doc.module.stmts.len(), 1);
    let eudkit_hir::HirStmt::Trigger(trigger) = &doc.module.stmts[0] else {
        panic!("expected a direct trigger");
    };
    assert_eq!(trigger.acts.len(), 3);
    assert_eq!(trigger.acts[0].uprp, Some(first));
    assert_eq!(trigger.acts[1].uprp, Some(second));
    assert_eq!(trigger.acts[2].uprp, Some(first));

    let lir = lower_document(&doc).unwrap();
    let cost = static_cost(&lir);
    assert_eq!(cost.triggers, 1);
    assert_eq!(cost.conditions, 0);
    assert_eq!(cost.actions, 3);

    // Before map assembly the property index is unresolved.
    let payload = lir2payload(&lir).unwrap();
    let raw = RawTrigger::decode(payload.data[..RAW_TRIGGER_BYTE_SIZE].try_into().unwrap());
    assert_eq!(raw.acts[0].acttype, 11);
    assert_eq!(raw.acts[0].value, 0);
    assert_eq!(raw.acts[0].flags, 28);
    assert_eq!(raw.acts[0].modifier, 1);

    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-cuwp.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap();

    let uprp = read_section(&out_scx, b"UPRP");
    let upus = read_section(&out_scx, b"UPUS");
    assert_eq!(uprp.len(), 1280);
    assert_eq!(upus.len(), 64);
    assert_eq!(&uprp[..20], &first);
    assert_eq!(&uprp[20..40], &second);
    assert!(uprp[40..].iter().all(|&b| b == 0));
    assert_eq!(&upus[..3], &[1, 1, 0]);
    assert!(upus[3..].iter().all(|&b| b == 0));

    let strx = read_section(&out_scx, b"STRx");
    let record = strx
        .windows(RAW_TRIGGER_BYTE_SIZE)
        .find(|window| {
            let raw = RawTrigger::decode((*window).try_into().unwrap());
            raw.acts[0].acttype == 11 && raw.acts[1].acttype == 11
        })
        .expect("compiled map must contain the CUWP trigger");
    let raw = RawTrigger::decode(record.try_into().unwrap());
    assert_eq!(raw.acts[0].value, 1, "first record -> slot 0 (1-based 1)");
    assert_eq!(raw.acts[1].value, 2, "second record -> slot 1 (1-based 2)");
    assert_eq!(raw.acts[2].value, 1, "duplicate record reuses slot 0");
    assert_eq!(raw.acts[0].unit, 65);
    assert_eq!(raw.acts[0].mask, 64);
    assert_eq!(raw.acts[0].dest, 0);
    assert_eq!(raw.acts[0].flags, 28);
}

#[test]
fn create_unit_with_properties_reports_uprp_exhaustion() {
    let ts = r#"
import {
  Anywhere, CreateUnitWithProperties, P1, Trigger, UnitProperty, buildModule,
} from "./ts/index.ts";
const mod = buildModule(() => {
  new Trigger({
    conds: [],
    acts: Array.from({ length: 64 }, (_, hp) =>
      CreateUnitWithProperties(1, 65, Anywhere, P1, UnitProperty({ hitPoints: hp })),
    ),
    preserved: true,
  });
  new Trigger({
    conds: [],
    acts: [
      CreateUnitWithProperties(1, 65, Anywhere, P1, UnitProperty({ hitPoints: 64 })),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;
    let py = r#"
import sys
from py import (
    Anywhere, CreateUnitWithProperties, P1, Trigger, UnitProperty, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            CreateUnitWithProperties(1, 65, Anywhere, P1, UnitProperty(hit_points=hp))
            for hp in range(64)
        ],
        preserved=True,
    )
    Trigger(
        conds=[],
        acts=[
            CreateUnitWithProperties(1, 65, Anywhere, P1, UnitProperty(hit_points=64)),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;
    let hir_json = assert_hir_parity(ts, py);
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("stock-cuwp-overflow.scx");
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let err = compile_hir_to_map(&hir_json, &basemap, &out_scx).unwrap_err();
    match err {
        eudkit_lower::LowerError::Map(eudkit_map::MapTailError::UprpOverflow) => {}
        other => panic!("expected UprpOverflow, got {other:?}"),
    }
}
