import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  Accumulate,
  Add,
  All,
  AllPlayers,
  Allies,
  AlliedVictory,
  Ally,
  Anywhere,
  AtLeast,
  AtMost,
  Bring,
  CenterView,
  Command,
  CommandLeast,
  CommandLeastAt,
  CommandMost,
  CommandMostAt,
  CountdownTimer,
  CreateUnit,
  CreateUnitWithProperties,
  CurrentPlayer,
  Draw,
  ElapsedTime,
  Enemy,
  EudslError,
  Exactly,
  Foes,
  Force1,
  Force2,
  Force3,
  Force4,
  Gas,
  GiveUnits,
  HighestScore,
  KillUnit,
  KillUnitAt,
  Kills,
  LeaderBoardComputerPlayers,
  LeaderBoardControl,
  LeaderBoardControlAt,
  LeaderBoardGoalControl,
  LeaderBoardGoalControlAt,
  LeaderBoardGoalKills,
  LeaderBoardGoalResources,
  LeaderBoardGoalScore,
  LeaderBoardGreed,
  LeaderBoardKills,
  LeaderBoardResources,
  LeaderBoardScore,
  LeastKills,
  LeastResources,
  LowestScore,
  MinimapPing,
  ModifyUnitEnergy,
  ModifyUnitHangarCount,
  ModifyUnitHitPoints,
  ModifyUnitResourceAmount,
  ModifyUnitShields,
  MostKills,
  MostResources,
  MoveLocation,
  MoveUnit,
  MuteUnitSpeech,
  NeutralPlayers,
  NonAlliedVictoryPlayers,
  Opponents,
  Order,
  OrderAttack,
  OrderMove,
  OrderPatrol,
  Ore,
  OreAndGas,
  P1,
  P2,
  PauseGame,
  PauseTimer,
  PlayWAV,
  PropDisable,
  PropEnable,
  PropToggle,
  RemoveUnit,
  RemoveUnitAt,
  RunAIScript,
  RunAIScriptAt,
  Score,
  ScoreBuildings,
  ScoreCustom,
  ScoreKills,
  ScoreKillsAndRazings,
  ScoreRazings,
  ScoreTotal,
  ScoreUnits,
  ScoreUnitsAndBuildings,
  SetAllianceStatus,
  SetCountdownTimer,
  SetDoodadState,
  SetInvincibility,
  SetMissionObjectives,
  SetNextScenario,
  SetResources,
  SetScore,
  SetSwitch,
  SetTo,
  Subtract,
  Switch,
  SwitchActionClear,
  SwitchActionRandom,
  SwitchActionSet,
  SwitchActionToggle,
  SwitchCleared,
  SwitchSet,
  TalkingPortrait,
  Transmission,
  Trigger,
  UnitProperty,
  UnMuteUnitSpeech,
  UnpauseGame,
  UnpauseTimer,
  buildModule,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

test("stock vocabulary values match the StarEdit/CHK tables", () => {
  assert.equal(Anywhere, 64);
  assert.equal(All, 0);
  assert.deepEqual(
    [Foes, Allies, NeutralPlayers, AllPlayers, Force1, Force2, Force3, Force4, NonAlliedVictoryPlayers],
    [14, 15, 16, 17, 18, 19, 20, 21, 26],
  );
  assert.equal(CurrentPlayer, 13);
  assert.deepEqual([Ore, Gas, OreAndGas], [0, 1, 2]);
  assert.deepEqual(
    [
      ScoreTotal,
      ScoreUnits,
      ScoreBuildings,
      ScoreUnitsAndBuildings,
      ScoreKills,
      ScoreRazings,
      ScoreKillsAndRazings,
      ScoreCustom,
    ],
    [0, 1, 2, 3, 4, 5, 6, 7],
  );
  assert.deepEqual([SwitchSet, SwitchCleared], [2, 3]);
  assert.deepEqual(
    [SwitchActionSet, SwitchActionClear, SwitchActionToggle, SwitchActionRandom],
    [4, 5, 6, 11],
  );
  assert.deepEqual([PropEnable, PropDisable, PropToggle], [4, 5, 6]);
  assert.deepEqual([OrderMove, OrderPatrol, OrderAttack], [0, 1, 2]);
  assert.deepEqual([Ally, AlliedVictory], [1, 2]);
});

test("Bring emits condition type 3 in the stock fields", () => {
  const mod = buildModule(() => {
    new Trigger({
      conds: [Bring(Foes, Exactly, 7, 65, Anywhere)],
      acts: [],
      preserved: true,
    });
  });
  const cond = mod.triggers[0].conds[0];
  assert.deepEqual(cond, {
    locid: 64,
    player: 14,
    amount: 7,
    unit: 65,
    comparison: 10,
    condtype: 3,
    restype: 0,
    flags: 0,
    eudx: 0,
  });
});

test("CreateUnit emits action type 44 with the location, player, unit, count, and stock flags", () => {
  const mod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [CreateUnit(3, 65, Anywhere, P1)],
      preserved: true,
    });
  });
  const act = mod.triggers[0].acts[0];
  assert.deepEqual(act, {
    mask: 64,
    strid: 0,
    wavid: 0,
    time: 0,
    dest: 0,
    value: 0,
    unit: 65,
    acttype: 44,
    modifier: 3,
    flags: 20,
    padding: 0,
    eudx: 0,
  });
});

test("stock helpers reject out-of-range location, count, unit, player, and enum values", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => Bring(P1, Exactly, 1, 0, 256));
  bounds(() => Bring(12, Exactly, 1, 0, Anywhere));
  bounds(() => Bring(P1, 2, 1, 0, Anywhere));
  bounds(() => Bring(P1, Exactly, -1, 0, Anywhere));
  bounds(() => Bring(P1, Exactly, 1, 228, Anywhere));
  bounds(() => CreateUnit(0, 0, Anywhere, P1));
  bounds(() => CreateUnit(256, 0, Anywhere, P1));
  bounds(() => CreateUnit(1, 228, Anywhere, P1));
  bounds(() => CreateUnit(1, 0, 256, P1));
  bounds(() => CreateUnit(1, 0, Anywhere, 12));
});

test("TypeScript and Python emit byte-identical HIR and the same lowered records", () => {
  const source = (): void => {
    new Trigger({
      conds: [
        Bring(Allies, AtLeast, 0x8000_0000, 65, Anywhere),
        Bring(P1, Exactly, 1, 0, 0),
      ],
      acts: [
        CreateUnit(1, 65, Anywhere, Foes),
        CreateUnit(255, 227, 255, NonAlliedVictoryPlayers),
      ],
      preserved: true,
    });
  };
  const tsMod = buildModule(source);
  const tsHir = tsMod.toJson();

  const pyScript = `
from py import (
    Allies, Anywhere, AtLeast, Bring, CreateUnit, Exactly, Foes, P1,
    NonAlliedVictoryPlayers, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[
            Bring(Allies, AtLeast, 0x80000000, 65, Anywhere),
            Bring(P1, Exactly, 1, 0, 0),
        ],
        acts=[
            CreateUnit(1, 65, Anywhere, Foes),
            CreateUnit(255, 227, 255, NonAlliedVictoryPlayers),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsHir, pyHir);

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-stock-"));
  try {
    const hirPath = path.join(dir, "prog.hir.json");
    fs.writeFileSync(hirPath, tsHir, "utf-8");
    const lir = JSON.parse(
      execFileSync(EUDKIT_COMPILE, ["lower", hirPath], { encoding: "utf-8" }),
    );
    const trig = lir.module.items.find((item: { kind: string }) => item.kind === "trigger");
    assert.ok(trig);
    assert.deepEqual(condFields(trig.conds[0]), {
      locid: 64,
      player: 15,
      amount: 0x8000_0000,
      unit: 65,
      comparison: 0,
      condtype: 3,
      restype: 0,
      flags: 0,
      eudx: 0,
    });
    assert.deepEqual(condFields(trig.conds[1]), {
      locid: 0,
      player: 0,
      amount: 1,
      unit: 0,
      comparison: 10,
      condtype: 3,
      restype: 0,
      flags: 0,
      eudx: 0,
    });
    assert.deepEqual(actFields(trig.acts[0]), {
      mask: 64,
      strid: 0,
      wavid: 0,
      time: 0,
      dest: 14,
      value: 0,
      unit: 65,
      acttype: 44,
      modifier: 1,
      flags: 20,
      padding: 0,
      eudx: 0,
    });
    assert.deepEqual(actFields(trig.acts[1]), {
      mask: 255,
      strid: 0,
      wavid: 0,
      time: 0,
      dest: 26,
      value: 0,
      unit: 227,
      acttype: 44,
      modifier: 255,
      flags: 20,
      padding: 0,
      eudx: 0,
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// Typed LIR records (issue #101) flatten one `CTrigCondition` /
// `CTrigAction` per cond/act, so pick the binary fields to compare.
function condFields(c: Record<string, unknown>) {
  return {
    locid: c.locid,
    player: c.player,
    amount: c.amount,
    unit: c.unit,
    comparison: c.comparison,
    condtype: c.condtype,
    restype: c.restype,
    flags: c.flags,
    eudx: c.eudx,
  };
}

function actFields(a: Record<string, unknown>) {
  return {
    mask: a.mask,
    strid: a.strid,
    wavid: a.wavid,
    time: a.time,
    dest: a.dest,
    value: a.value,
    unit: a.unit,
    acttype: a.acttype,
    modifier: a.modifier,
    flags: a.flags,
    padding: a.padding,
    eudx: a.eudx,
  };
}

function cond(
  condtype: number,
  fields: Partial<{
    locid: number;
    player: number;
    amount: number;
    unit: number;
    comparison: number;
    restype: number;
  }> = {},
) {
  return {
    locid: 0,
    player: 0,
    amount: 0,
    unit: 0,
    comparison: 0,
    condtype,
    restype: 0,
    flags: 0,
    eudx: 0,
    ...fields,
  };
}

test("remaining stock conditions emit their documented IDs and stock fields", () => {
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

  assert.deepEqual(
    mod.triggers[0].conds,
    [
      cond(4, { amount: 5, restype: Gas }),
      cond(2, { player: P2, amount: 6, unit: 65, comparison: AtMost }),
      cond(16, { unit: 65 }),
      cond(17, { locid: 7, unit: 65 }),
      cond(6, { unit: 65 }),
      cond(7, { locid: 8, unit: 65 }),
      cond(1, { amount: 100, comparison: Exactly }),
      cond(12, { amount: 200, comparison: AtLeast }),
      cond(9, { restype: ScoreKills }),
    ],
  );
  assert.deepEqual(
    mod.triggers[1].conds,
    [
      cond(5, { player: P1, amount: 3, unit: 65, comparison: AtLeast }),
      cond(18, { unit: 65 }),
      cond(20, { restype: OreAndGas }),
      cond(19, { restype: ScoreCustom }),
      cond(8, { unit: 65 }),
      cond(10, { restype: Gas }),
      cond(14, { player: P2, amount: 2, comparison: AtMost }),
      cond(21, { player: P1, amount: 9, comparison: Exactly, restype: ScoreKills }),
      cond(11, { comparison: SwitchCleared, restype: 200 }),
    ],
  );
});

test("remaining stock conditions validate their stock ranges", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => Accumulate(12, AtLeast, 1, Ore));
  bounds(() => Accumulate(P1, AtLeast, 1, 3));
  bounds(() => Command(P1, 2, 1, 0));
  bounds(() => CommandLeast(228));
  bounds(() => CommandLeastAt(0, 256));
  bounds(() => CommandMostAt(228, 0));
  bounds(() => CountdownTimer(Exactly, -1));
  bounds(() => ElapsedTime(2, 1));
  bounds(() => HighestScore(8));
  bounds(() => Kills(P1, AtLeast, -1, 0));
  bounds(() => LeastKills(228));
  bounds(() => LeastResources(3));
  bounds(() => LowestScore(8));
  bounds(() => MostKills(228));
  bounds(() => MostResources(3));
  bounds(() => Opponents(12, AtLeast, 1));
  bounds(() => Score(P1, ScoreKills, Exactly, -1));
  bounds(() => Switch(256, SwitchSet));
  bounds(() => Switch(0, 4));
});

test("remaining stock conditions emit byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
    new Trigger({
      conds: [
        Accumulate(P1, AtLeast, 5, Gas),
        Command(P2, AtMost, 6, 65),
        CommandMostAt(65, 8),
        CountdownTimer(Exactly, 100),
        HighestScore(ScoreKills),
        Kills(P1, AtLeast, 3, 65),
        LeastResources(OreAndGas),
        Opponents(P2, AtMost, 2),
        Score(P1, ScoreKills, Exactly, 9),
        Switch(200, SwitchCleared),
      ],
      acts: [],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Accumulate, AtLeast, AtMost, Command, CommandMostAt, CountdownTimer,
    Exactly, Gas, HighestScore, Kills, LeastResources, Opponents, OreAndGas,
    P1, P2, Score, ScoreKills, Switch, SwitchCleared, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[
            Accumulate(P1, AtLeast, 5, Gas),
            Command(P2, AtMost, 6, 65),
            CommandMostAt(65, 8),
            CountdownTimer(Exactly, 100),
            HighestScore(ScoreKills),
            Kills(P1, AtLeast, 3, 65),
            LeastResources(OreAndGas),
            Opponents(P2, AtMost, 2),
            Score(P1, ScoreKills, Exactly, 9),
            Switch(200, SwitchCleared),
        ],
        acts=[],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});

function act(
  acttype: number,
  fields: Partial<{
    mask: number;
    dest: number;
    value: number;
    unit: number;
    modifier: number;
    flags: number;
    time: number;
  }> = {},
) {
  return {
    mask: 0,
    strid: 0,
    wavid: 0,
    time: 0,
    dest: 0,
    value: 0,
    unit: 0,
    acttype,
    modifier: 0,
    flags: 0,
    padding: 0,
    eudx: 0,
    ...fields,
  };
}

test("unit and location actions emit their documented IDs and stock fields", () => {
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

  assert.deepEqual(mod.triggers[0].acts, [
    act(22, { dest: P1, unit: 65, flags: 20 }),
    act(23, { mask: Anywhere, dest: P2, unit: 65, modifier: 3, flags: 20 }),
    act(24, { dest: P1, unit: 65, flags: 20 }),
    act(25, { mask: Anywhere, dest: P2, unit: 65, modifier: All, flags: 20 }),
    act(39, { mask: 7, value: 8, unit: 65, modifier: All, flags: 20 }),
    act(48, { mask: Anywhere, dest: P1, value: P2, unit: 65, modifier: 5, flags: 20 }),
    act(46, { mask: 7, value: 8, unit: 65, modifier: OrderPatrol, flags: 20 }),
    act(38, { mask: Anywhere, value: 9, unit: 65, flags: 20 }),
    act(42, { mask: Anywhere, unit: 65, modifier: PropToggle, flags: 20 }),
    act(43, { mask: Anywhere, unit: 65, modifier: PropEnable, flags: 20 }),
    act(49, { mask: Anywhere, value: 50, unit: 65, flags: 20 }),
    act(50, { mask: 7, dest: P2, value: 75, unit: 65, modifier: 2, flags: 20 }),
    act(51, { mask: Anywhere, value: 100, unit: 65, modifier: 255, flags: 20 }),
    act(52, { mask: Anywhere, value: 5000, flags: 4 }),
    act(53, { mask: Anywhere, value: 5, unit: 65, flags: 20 }),
  ]);
});

test("unit and location actions validate count, location, unit, player, and enums", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => KillUnit(228, P1));
  bounds(() => KillUnit(0, 12));
  bounds(() => KillUnitAt(256, 0, Anywhere, P1));
  bounds(() => KillUnitAt(-1, 0, Anywhere, P1));
  bounds(() => KillUnitAt(All, 0, 256, P1));
  bounds(() => RemoveUnitAt(256, 0, Anywhere, P1));
  bounds(() => MoveUnit(256, 0, P1, 0, 0));
  bounds(() => MoveUnit(All, 0, 12, 0, 0));
  bounds(() => MoveUnit(All, 0, P1, 256, 0));
  bounds(() => MoveUnit(All, 0, P1, 0, 256));
  bounds(() => GiveUnits(256, 0, P1, 0, P2));
  bounds(() => GiveUnits(All, 0, P1, 0, 12));
  bounds(() => Order(0, P1, 0, 3, 0));
  bounds(() => Order(228, P1, 0, OrderMove, 0));
  bounds(() => MoveLocation(256, 0, P1, 0));
  bounds(() => MoveLocation(0, 228, P1, 0));
  bounds(() => MoveLocation(0, 0, P1, 256));
  bounds(() => SetDoodadState(3, 0, P1, 0));
  bounds(() => SetDoodadState(PropEnable, 228, P1, 0));
  bounds(() => SetInvincibility(7, 0, P1, 0));
  bounds(() => ModifyUnitHitPoints(256, 0, P1, 0, 50));
  bounds(() => ModifyUnitHitPoints(All, 228, P1, 0, 50));
  bounds(() => ModifyUnitHitPoints(All, 0, P1, 0, 101));
  bounds(() => ModifyUnitEnergy(All, 0, P1, 0, -1));
  bounds(() => ModifyUnitShields(All, 0, P1, 0, 101));
  bounds(() => ModifyUnitResourceAmount(256, P1, 0, 5000));
  bounds(() => ModifyUnitResourceAmount(All, P1, 0, -1));
  bounds(() => ModifyUnitHangarCount(-1, All, 0, P1, 0));
  bounds(() => ModifyUnitHangarCount(0, 256, 0, P1, 0));
});

test("unit and location actions emit byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        KillUnitAt(All, 65, Anywhere, P2),
        MoveUnit(3, 65, P1, 7, 8),
        GiveUnits(All, 65, P2, Anywhere, P1),
        Order(65, P1, 7, OrderAttack, 8),
        MoveLocation(9, 65, P1, Anywhere),
        SetDoodadState(PropDisable, 65, P1, 7),
        ModifyUnitHitPoints(2, 65, P2, Anywhere, 50),
        ModifyUnitResourceAmount(All, P1, 7, 5000),
        ModifyUnitHangarCount(5, 255, 65, P2, Anywhere),
      ],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    All, Anywhere, GiveUnits, KillUnitAt, ModifyUnitHangarCount,
    ModifyUnitHitPoints, ModifyUnitResourceAmount, MoveLocation, MoveUnit,
    Order, OrderAttack, P1, P2, PropDisable, SetDoodadState, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            KillUnitAt(All, 65, Anywhere, P2),
            MoveUnit(3, 65, P1, 7, 8),
            GiveUnits(All, 65, P2, Anywhere, P1),
            Order(65, P1, 7, OrderAttack, 8),
            MoveLocation(9, 65, P1, Anywhere),
            SetDoodadState(PropDisable, 65, P1, 7),
            ModifyUnitHitPoints(2, 65, P2, Anywhere, 50),
            ModifyUnitResourceAmount(All, P1, 7, 5000),
            ModifyUnitHangarCount(5, 255, 65, P2, Anywhere),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});

test("game-state and UI actions emit their documented IDs and stock fields", () => {
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

  assert.deepEqual(mod.triggers[0].acts, [
    act(5, { flags: 4 }),
    act(6, { flags: 4 }),
    act(10, { mask: Anywhere, flags: 4 }),
    { ...act(12, { flags: 4 }), text: "New objectives" },
    act(13, { value: 200, modifier: SwitchActionToggle, flags: 4 }),
    act(14, { time: 5000, modifier: Add, flags: 4 }),
    act(15, { value: 10, flags: 4 }),
    act(16, { mask: 7, value: 11, flags: 4 }),
    act(26, { dest: P2, value: 5000, unit: Gas, modifier: Subtract, flags: 4 }),
    act(27, { dest: P1, value: 100, unit: ScoreKills, modifier: Add, flags: 4 }),
    act(28, { mask: Anywhere, flags: 4 }),
    act(29, { time: 1000, unit: 65, flags: 20 }),
    act(30, { flags: 4 }),
    act(31, { flags: 4 }),
    { ...act(41, { flags: 4 }), text: "next.scm" },
    act(54, { flags: 4 }),
    act(55, { flags: 4 }),
    act(56, { flags: 4 }),
    act(57, { dest: P1, unit: Ally, flags: 4 }),
  ]);
});

test("game-state and UI actions validate locations, players, and enums", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => CenterView(256));
  bounds(() => SetSwitch(256, SwitchActionSet));
  bounds(() => SetSwitch(0, 7));
  bounds(() => SetCountdownTimer(6, 0));
  bounds(() => SetCountdownTimer(Add, -1));
  bounds(() => RunAIScript(-1));
  bounds(() => RunAIScriptAt(10, 256));
  bounds(() => SetResources(12, SetTo, 0, Ore));
  bounds(() => SetResources(P1, 0, 0, Ore));
  bounds(() => SetResources(P1, SetTo, -1, Ore));
  bounds(() => SetResources(P1, SetTo, 0, 3));
  bounds(() => SetScore(12, SetTo, 0, ScoreKills));
  bounds(() => SetScore(P1, SetTo, 0, 8));
  bounds(() => MinimapPing(256));
  bounds(() => TalkingPortrait(228, 0));
  bounds(() => TalkingPortrait(0, -1));
  bounds(() => SetAllianceStatus(12, Ally));
  bounds(() => SetAllianceStatus(P1, 3));
});

test("game-state and UI actions emit byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        CenterView(Anywhere),
        SetMissionObjectives("Objectives"),
        SetSwitch(200, SwitchActionRandom),
        SetCountdownTimer(Subtract, 300),
        RunAIScriptAt(11, 7),
        SetResources(P2, Add, 5000, Gas),
        SetScore(P1, Subtract, 100, ScoreKills),
        MinimapPing(7),
        TalkingPortrait(65, 1000),
        SetNextScenario("next.scm"),
        SetAllianceStatus(P2, Enemy),
      ],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Add, Anywhere, CenterView, Enemy, Gas, MinimapPing, P1, P2,
    RunAIScriptAt, ScoreKills, SetAllianceStatus, SetCountdownTimer,
    SetMissionObjectives, SetNextScenario, SetResources, SetScore, SetSwitch,
    Subtract, SwitchActionRandom, TalkingPortrait, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            CenterView(Anywhere),
            SetMissionObjectives("Objectives"),
            SetSwitch(200, SwitchActionRandom),
            SetCountdownTimer(Subtract, 300),
            RunAIScriptAt(11, 7),
            SetResources(P2, Add, 5000, Gas),
            SetScore(P1, Subtract, 100, ScoreKills),
            MinimapPing(7),
            TalkingPortrait(65, 1000),
            SetNextScenario("next.scm"),
            SetAllianceStatus(P2, Enemy),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});

test("leaderboard actions emit their documented IDs and stock fields", () => {
  const mod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        LeaderBoardControl(65, "Control"),
        LeaderBoardControlAt(65, Anywhere, "ControlAt"),
        LeaderBoardResources(Gas, "Resources"),
        LeaderBoardKills(65, "Kills"),
        LeaderBoardScore(ScoreKills, "Score"),
        LeaderBoardComputerPlayers(PropDisable),
        LeaderBoardGoalControl(10, 65, "GoalControl"),
        LeaderBoardGoalControlAt(10, 65, Anywhere, "GoalControlAt"),
        LeaderBoardGoalResources(10, Gas, "GoalResources"),
        LeaderBoardGoalKills(10, 65, "GoalKills"),
        LeaderBoardGoalScore(10, ScoreKills, "GoalScore"),
        LeaderBoardGreed(10),
      ],
      preserved: true,
    });
  });

  assert.deepEqual(mod.triggers[0].acts, [
    { ...act(17, { unit: 65, flags: 20 }), text: "Control" },
    { ...act(18, { mask: Anywhere, unit: 65, flags: 20 }), text: "ControlAt" },
    { ...act(19, { unit: Gas, flags: 4 }), text: "Resources" },
    { ...act(20, { unit: 65, flags: 20 }), text: "Kills" },
    { ...act(21, { unit: ScoreKills, flags: 4 }), text: "Score" },
    act(32, { modifier: PropDisable, flags: 4 }),
    { ...act(33, { value: 10, unit: 65, flags: 20 }), text: "GoalControl" },
    {
      ...act(34, { mask: Anywhere, value: 10, unit: 65, flags: 20 }),
      text: "GoalControlAt",
    },
    { ...act(35, { value: 10, unit: Gas, flags: 4 }), text: "GoalResources" },
    { ...act(36, { value: 10, unit: 65, flags: 20 }), text: "GoalKills" },
    { ...act(37, { value: 10, unit: ScoreKills, flags: 4 }), text: "GoalScore" },
    act(40, { value: 10, flags: 4 }),
  ]);
});

test("leaderboard actions validate goals, locations, units, and enums", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => LeaderBoardControl(228, "x"));
  bounds(() => LeaderBoardControlAt(0, 256, "x"));
  bounds(() => LeaderBoardResources(3, "x"));
  bounds(() => LeaderBoardKills(228, "x"));
  bounds(() => LeaderBoardScore(8, "x"));
  bounds(() => LeaderBoardComputerPlayers(3));
  bounds(() => LeaderBoardGoalControl(-1, 0, "x"));
  bounds(() => LeaderBoardGoalControl(0, 228, "x"));
  bounds(() => LeaderBoardGoalControlAt(0, 0, 256, "x"));
  bounds(() => LeaderBoardGoalResources(-1, Gas, "x"));
  bounds(() => LeaderBoardGoalResources(0, 3, "x"));
  bounds(() => LeaderBoardGoalKills(-1, 0, "x"));
  bounds(() => LeaderBoardGoalScore(-1, ScoreKills, "x"));
  bounds(() => LeaderBoardGoalScore(0, 8, "x"));
  bounds(() => LeaderBoardGreed(-1));
});

test("leaderboard actions emit byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        LeaderBoardControl(65, "Control"),
        LeaderBoardControlAt(65, Anywhere, "ControlAt"),
        LeaderBoardResources(OreAndGas, "Resources"),
        LeaderBoardComputerPlayers(PropToggle),
        LeaderBoardGoalControl(10, 65, "GoalControl"),
        LeaderBoardGoalControlAt(10, 65, 7, "GoalControlAt"),
        LeaderBoardGoalResources(10, Gas, "GoalResources"),
        LeaderBoardGoalKills(10, 65, "GoalKills"),
        LeaderBoardGoalScore(10, ScoreCustom, "GoalScore"),
        LeaderBoardGreed(10),
      ],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Anywhere, Gas, LeaderBoardComputerPlayers, LeaderBoardControl,
    LeaderBoardControlAt, LeaderBoardGoalControl, LeaderBoardGoalControlAt,
    LeaderBoardGoalKills, LeaderBoardGoalResources, LeaderBoardGoalScore,
    LeaderBoardGreed, LeaderBoardResources, OreAndGas, PropToggle, ScoreCustom,
    Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            LeaderBoardControl(65, "Control"),
            LeaderBoardControlAt(65, Anywhere, "ControlAt"),
            LeaderBoardResources(OreAndGas, "Resources"),
            LeaderBoardComputerPlayers(PropToggle),
            LeaderBoardGoalControl(10, 65, "GoalControl"),
            LeaderBoardGoalControlAt(10, 65, 7, "GoalControlAt"),
            LeaderBoardGoalResources(10, Gas, "GoalResources"),
            LeaderBoardGoalKills(10, 65, "GoalKills"),
            LeaderBoardGoalScore(10, ScoreCustom, "GoalScore"),
            LeaderBoardGreed(10),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});

test("Transmission emits action type 7 with independent text and sound slots", () => {
  const mod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
        PlayWAV("sound.wav"),
      ],
      preserved: true,
    });
  });

  assert.deepEqual(mod.triggers[0].acts, [
    {
      ...act(7, { mask: Anywhere, time: 1000, unit: 65, modifier: Add, flags: 4 }),
      text: "Hello",
      wav: "sound.wav",
    },
    { ...act(8, { flags: 4 }), wav: "sound.wav" },
  ]);
});

test("Transmission validates its stock ranges", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  bounds(() => Transmission(228, Anywhere, "", SetTo, 0, ""));
  bounds(() => Transmission(0, 256, "", SetTo, 0, ""));
  bounds(() => Transmission(0, 0, "", 6, 0, ""));
  bounds(() => Transmission(0, 0, "", SetTo, -1, ""));
  bounds(() => Transmission(0, 0, "", SetTo, 0, "", 256));
});

test("Transmission emits byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [
        Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
        Transmission(65, 7, "sound.wav", SetTo, 2000, "Hello"),
        Transmission(65, Anywhere, "other.wav", Subtract, 3000, "Other"),
      ],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Add, Anywhere, SetTo, Subtract, Transmission, Trigger, buildModule,
)
def prog():
    Trigger(
        conds=[],
        acts=[
            Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
            Transmission(65, 7, "sound.wav", SetTo, 2000, "Hello"),
            Transmission(65, Anywhere, "other.wav", Subtract, 3000, "Other"),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});

test("UnitProperty builds the normalized 20-byte UPRP record", () => {
  assert.deepEqual(UnitProperty(), new Array(20).fill(0));
  assert.deepEqual(
    UnitProperty({ hitPoints: 50, shields: 75, energy: 25, resource: 300, hangar: 2, cloaked: true, burrowed: false, invincible: true }),
    [17, 0, 62, 0, 0, 50, 75, 25, 44, 1, 0, 0, 2, 0, 17, 0, 0, 0, 0, 0],
  );
  // 100% HP/shields and 0 hangar mean "unset".
  assert.deepEqual(
    UnitProperty({ hitPoints: 100, shields: 100, hangar: 0 }),
    new Array(20).fill(0),
  );
  // An explicit false flag is equivalent to unset.
  assert.deepEqual(UnitProperty({ cloaked: false, burrowed: false }), new Array(20).fill(0));
  const cloaked = UnitProperty({ cloaked: true });
  assert.equal(cloaked[0], 1); // sprpvalid bit 0
  assert.equal(cloaked[14], 1); // sprpflag bit 0
});

test("CreateUnitWithProperties emits action type 11 with flags 28", () => {
  const properties = UnitProperty({ hitPoints: 50 });
  const mod = buildModule(() => {
    new Trigger({
      conds: [],
      acts: [CreateUnitWithProperties(3, 65, Anywhere, P1, properties)],
      preserved: true,
    });
  });
  assert.deepEqual(mod.triggers[0].acts, [
    {
      ...act(11, { mask: Anywhere, dest: P1, value: 0, unit: 65, modifier: 3, flags: 28 }),
      uprp: properties,
    },
  ]);
});

test("UnitProperty and CreateUnitWithProperties validate their inputs", () => {
  const bounds = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    });
  };
  const type = (fn: () => unknown): void => {
    assert.throws(fn, (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-TYPE");
      return true;
    });
  };
  bounds(() => UnitProperty({ hitPoints: -1 }));
  bounds(() => UnitProperty({ hitPoints: 101 }));
  bounds(() => UnitProperty({ hitPoints: 1.5 }));
  bounds(() => UnitProperty({ shields: 101 }));
  bounds(() => UnitProperty({ energy: -1 }));
  bounds(() => UnitProperty({ resource: 65536 }));
  bounds(() => UnitProperty({ resource: -1 }));
  bounds(() => UnitProperty({ hangar: 256 }));
  bounds(() => UnitProperty({ hangar: -1 }));
  type(() => UnitProperty({ cloaked: "yes" as unknown as boolean }));
  type(() => UnitProperty({ unknown: 1 } as unknown as { hitPoints: number }));
  bounds(() => CreateUnitWithProperties(0, 65, Anywhere, P1, UnitProperty()));
  bounds(() => CreateUnitWithProperties(256, 65, Anywhere, P1, UnitProperty()));
  bounds(() => CreateUnitWithProperties(1, 228, Anywhere, P1, UnitProperty()));
  bounds(() => CreateUnitWithProperties(1, 65, 256, P1, UnitProperty()));
  bounds(() => CreateUnitWithProperties(1, 65, Anywhere, 12, UnitProperty()));
  type(() => CreateUnitWithProperties(1, 65, Anywhere, P1, []));
  type(() => CreateUnitWithProperties(1, 65, Anywhere, P1, [256, ...new Array(19).fill(0)]));
});

test("UnitProperty and CreateUnitWithProperties emit byte-identical TS/Python HIR", () => {
  const tsMod = buildModule(() => {
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
    new Trigger({
      conds: [],
      acts: [
        CreateUnitWithProperties(1, 65, Anywhere, P1, first),
        CreateUnitWithProperties(2, 65, 7, P2, second),
        CreateUnitWithProperties(3, 65, Anywhere, P1, first),
      ],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Anywhere, CreateUnitWithProperties, P1, P2, Trigger, UnitProperty,
    buildModule,
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
            CreateUnitWithProperties(2, 65, 7, P2, second),
            CreateUnitWithProperties(3, 65, Anywhere, P1, first),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});
