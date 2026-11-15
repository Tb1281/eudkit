"""Stock trigger vocabulary and Bring/CreateUnit tests for the Python DSL."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
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
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


class TestStockVocabulary(unittest.TestCase):
    def test_stock_vocabulary_values(self) -> None:
        self.assertEqual(Anywhere, 64)
        self.assertEqual(All, 0)
        self.assertEqual(
            [
                Foes,
                Allies,
                NeutralPlayers,
                AllPlayers,
                Force1,
                Force2,
                Force3,
                Force4,
                NonAlliedVictoryPlayers,
            ],
            [14, 15, 16, 17, 18, 19, 20, 21, 26],
        )
        self.assertEqual(CurrentPlayer, 13)
        self.assertEqual([Ore, Gas, OreAndGas], [0, 1, 2])
        self.assertEqual(
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
        )
        self.assertEqual([SwitchSet, SwitchCleared], [2, 3])
        self.assertEqual(
            [SwitchActionSet, SwitchActionClear, SwitchActionToggle, SwitchActionRandom],
            [4, 5, 6, 11],
        )
        self.assertEqual([PropEnable, PropDisable, PropToggle], [4, 5, 6])
        self.assertEqual([OrderMove, OrderPatrol, OrderAttack], [0, 1, 2])
        self.assertEqual([Ally, AlliedVictory], [1, 2])

    def test_bring_emits_condition_type_3(self) -> None:
        mod = buildModule(
            lambda: Trigger(
                conds=[Bring(Foes, Exactly, 7, 65, Anywhere)],
                acts=[],
                preserved=True,
            )
        )
        self.assertEqual(
            dict(mod.triggers[0].conds[0]),
            {
                "locid": 64,
                "player": 14,
                "amount": 7,
                "unit": 65,
                "comparison": 10,
                "condtype": 3,
                "restype": 0,
                "flags": 0,
                "eudx": 0,
            },
        )

    def test_create_unit_emits_action_type_44(self) -> None:
        mod = buildModule(
            lambda: Trigger(
                conds=[],
                acts=[CreateUnit(3, 65, Anywhere, P1)],
                preserved=True,
            )
        )
        self.assertEqual(
            dict(mod.triggers[0].acts[0]),
            {
                "mask": 64,
                "strid": 0,
                "wavid": 0,
                "time": 0,
                "dest": 0,
                "value": 0,
                "unit": 65,
                "acttype": 44,
                "modifier": 3,
                "flags": 20,
                "padding": 0,
                "eudx": 0,
            },
        )

    def test_stock_helpers_reject_out_of_range_values(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: Bring(P1, Exactly, 1, 0, 256))
        bounds(lambda: Bring(12, Exactly, 1, 0, Anywhere))
        bounds(lambda: Bring(P1, 2, 1, 0, Anywhere))
        bounds(lambda: Bring(P1, Exactly, -1, 0, Anywhere))
        bounds(lambda: Bring(P1, Exactly, 1, 228, Anywhere))
        bounds(lambda: CreateUnit(0, 0, Anywhere, P1))
        bounds(lambda: CreateUnit(256, 0, Anywhere, P1))
        bounds(lambda: CreateUnit(1, 228, Anywhere, P1))
        bounds(lambda: CreateUnit(1, 0, 256, P1))
        bounds(lambda: CreateUnit(1, 0, Anywhere, 12))

    def test_lowered_record_fields(self) -> None:
        def prog() -> None:
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

        mod = buildModule(prog)
        with tempfile.TemporaryDirectory() as tmp:
            hir_path = Path(tmp) / "prog.hir.json"
            hir_path.write_text(mod.to_json(), encoding="utf-8")
            lowered = subprocess.run(
                [str(EUDKIT_COMPILE), "lower", str(hir_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            lir = json.loads(lowered.stdout)
            trig = next(item for item in lir["module"]["items"] if item["kind"] == "trigger")
            def cond_fields(c):
                return {
                    k: c[k]
                    for k in (
                        "locid",
                        "player",
                        "amount",
                        "unit",
                        "comparison",
                        "condtype",
                        "restype",
                        "flags",
                        "eudx",
                    )
                }

            def act_fields(a):
                return {
                    k: a[k]
                    for k in (
                        "mask",
                        "strid",
                        "wavid",
                        "time",
                        "dest",
                        "value",
                        "unit",
                        "acttype",
                        "modifier",
                        "flags",
                        "padding",
                        "eudx",
                    )
                }

            self.assertEqual(
                cond_fields(trig["conds"][0]),
                dict(
                    locid=64,
                    player=15,
                    amount=0x80000000,
                    unit=65,
                    comparison=0,
                    condtype=3,
                    restype=0,
                    flags=0,
                    eudx=0,
                ),
            )
            self.assertEqual(
                cond_fields(trig["conds"][1]),
                dict(
                    locid=0,
                    player=0,
                    amount=1,
                    unit=0,
                    comparison=10,
                    condtype=3,
                    restype=0,
                    flags=0,
                    eudx=0,
                ),
            )
            self.assertEqual(
                act_fields(trig["acts"][0]),
                dict(
                    mask=64,
                    strid=0,
                    wavid=0,
                    time=0,
                    dest=14,
                    value=0,
                    unit=65,
                    acttype=44,
                    modifier=1,
                    flags=20,
                    padding=0,
                    eudx=0,
                ),
            )
            self.assertEqual(
                act_fields(trig["acts"][1]),
                dict(
                    mask=255,
                    strid=0,
                    wavid=0,
                    time=0,
                    dest=26,
                    value=0,
                    unit=227,
                    acttype=44,
                    modifier=255,
                    flags=20,
                    padding=0,
                    eudx=0,
                ),
            )


def cond(condtype: int, **fields: int) -> dict[str, int]:
    out = {
        "locid": 0,
        "player": 0,
        "amount": 0,
        "unit": 0,
        "comparison": 0,
        "condtype": condtype,
        "restype": 0,
        "flags": 0,
        "eudx": 0,
    }
    out.update(fields)
    return out


class TestRemainingStockConditions(unittest.TestCase):
    def test_remaining_conditions_emit_documented_ids_and_fields(self) -> None:
        def prog() -> None:
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

        mod = buildModule(prog)
        self.assertEqual(
            [dict(c) for c in mod.triggers[0].conds],
            [
                cond(4, amount=5, restype=Gas),
                cond(2, player=P2, amount=6, unit=65, comparison=AtMost),
                cond(16, unit=65),
                cond(17, locid=7, unit=65),
                cond(6, unit=65),
                cond(7, locid=8, unit=65),
                cond(1, amount=100, comparison=Exactly),
                cond(12, amount=200, comparison=AtLeast),
                cond(9, restype=ScoreKills),
            ],
        )
        self.assertEqual(
            [dict(c) for c in mod.triggers[1].conds],
            [
                cond(5, player=P1, amount=3, unit=65, comparison=AtLeast),
                cond(18, unit=65),
                cond(20, restype=OreAndGas),
                cond(19, restype=ScoreCustom),
                cond(8, unit=65),
                cond(10, restype=Gas),
                cond(14, player=P2, amount=2, comparison=AtMost),
                cond(21, player=P1, amount=9, comparison=Exactly, restype=ScoreKills),
                cond(11, comparison=SwitchCleared, restype=200),
            ],
        )

    def test_remaining_conditions_validate_stock_ranges(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: Accumulate(12, AtLeast, 1, Ore))
        bounds(lambda: Accumulate(P1, AtLeast, 1, 3))
        bounds(lambda: Command(P1, 2, 1, 0))
        bounds(lambda: CommandLeast(228))
        bounds(lambda: CommandLeastAt(0, 256))
        bounds(lambda: CommandMostAt(228, 0))
        bounds(lambda: CountdownTimer(Exactly, -1))
        bounds(lambda: ElapsedTime(2, 1))
        bounds(lambda: HighestScore(8))
        bounds(lambda: Kills(P1, AtLeast, -1, 0))
        bounds(lambda: LeastKills(228))
        bounds(lambda: LeastResources(3))
        bounds(lambda: LowestScore(8))
        bounds(lambda: MostKills(228))
        bounds(lambda: MostResources(3))
        bounds(lambda: Opponents(12, AtLeast, 1))
        bounds(lambda: Score(P1, ScoreKills, Exactly, -1))
        bounds(lambda: Switch(256, SwitchSet))
        bounds(lambda: Switch(0, 4))


def act(acttype: int, **fields: int) -> dict[str, int]:
    out = {
        "mask": 0,
        "strid": 0,
        "wavid": 0,
        "time": 0,
        "dest": 0,
        "value": 0,
        "unit": 0,
        "acttype": acttype,
        "modifier": 0,
        "flags": 0,
        "padding": 0,
        "eudx": 0,
    }
    out.update(fields)
    return out


class TestUnitAndLocationActions(unittest.TestCase):
    def test_unit_and_location_actions_emit_documented_fields(self) -> None:
        def prog() -> None:
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

        mod = buildModule(prog)
        self.assertEqual(
            [dict(a) for a in mod.triggers[0].acts],
            [
                act(22, dest=P1, unit=65, flags=20),
                act(23, mask=Anywhere, dest=P2, unit=65, modifier=3, flags=20),
                act(24, dest=P1, unit=65, flags=20),
                act(25, mask=Anywhere, dest=P2, unit=65, modifier=All, flags=20),
                act(39, mask=7, value=8, unit=65, modifier=All, flags=20),
                act(48, mask=Anywhere, dest=P1, value=P2, unit=65, modifier=5, flags=20),
                act(46, mask=7, value=8, unit=65, modifier=OrderPatrol, flags=20),
                act(38, mask=Anywhere, value=9, unit=65, flags=20),
                act(42, mask=Anywhere, unit=65, modifier=PropToggle, flags=20),
                act(43, mask=Anywhere, unit=65, modifier=PropEnable, flags=20),
                act(49, mask=Anywhere, value=50, unit=65, flags=20),
                act(50, mask=7, dest=P2, value=75, unit=65, modifier=2, flags=20),
                act(51, mask=Anywhere, value=100, unit=65, modifier=255, flags=20),
                act(52, mask=Anywhere, value=5000, flags=4),
                act(53, mask=Anywhere, value=5, unit=65, flags=20),
            ],
        )

    def test_unit_and_location_actions_validate_ranges(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: KillUnit(228, P1))
        bounds(lambda: KillUnit(0, 12))
        bounds(lambda: KillUnitAt(256, 0, Anywhere, P1))
        bounds(lambda: KillUnitAt(-1, 0, Anywhere, P1))
        bounds(lambda: KillUnitAt(All, 0, 256, P1))
        bounds(lambda: RemoveUnitAt(256, 0, Anywhere, P1))
        bounds(lambda: MoveUnit(256, 0, P1, 0, 0))
        bounds(lambda: MoveUnit(All, 0, 12, 0, 0))
        bounds(lambda: MoveUnit(All, 0, P1, 256, 0))
        bounds(lambda: MoveUnit(All, 0, P1, 0, 256))
        bounds(lambda: GiveUnits(256, 0, P1, 0, P2))
        bounds(lambda: GiveUnits(All, 0, P1, 0, 12))
        bounds(lambda: Order(0, P1, 0, 3, 0))
        bounds(lambda: Order(228, P1, 0, OrderMove, 0))
        bounds(lambda: MoveLocation(256, 0, P1, 0))
        bounds(lambda: MoveLocation(0, 228, P1, 0))
        bounds(lambda: MoveLocation(0, 0, P1, 256))
        bounds(lambda: SetDoodadState(3, 0, P1, 0))
        bounds(lambda: SetDoodadState(PropEnable, 228, P1, 0))
        bounds(lambda: SetInvincibility(7, 0, P1, 0))
        bounds(lambda: ModifyUnitHitPoints(256, 0, P1, 0, 50))
        bounds(lambda: ModifyUnitHitPoints(All, 228, P1, 0, 50))
        bounds(lambda: ModifyUnitHitPoints(All, 0, P1, 0, 101))
        bounds(lambda: ModifyUnitEnergy(All, 0, P1, 0, -1))
        bounds(lambda: ModifyUnitShields(All, 0, P1, 0, 101))
        bounds(lambda: ModifyUnitResourceAmount(256, P1, 0, 5000))
        bounds(lambda: ModifyUnitResourceAmount(All, P1, 0, -1))
        bounds(lambda: ModifyUnitHangarCount(-1, All, 0, P1, 0))
        bounds(lambda: ModifyUnitHangarCount(0, 256, 0, P1, 0))


class TestGameStateAndUIActions(unittest.TestCase):
    def test_game_state_and_ui_actions_emit_documented_fields(self) -> None:
        def prog() -> None:
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

        mod = buildModule(prog)
        self.assertEqual(
            [dict(a) for a in mod.triggers[0].acts],
            [
                act(5, flags=4),
                act(6, flags=4),
                act(10, mask=Anywhere, flags=4),
                dict(act(12, flags=4), text="New objectives"),
                act(13, value=200, modifier=SwitchActionToggle, flags=4),
                act(14, time=5000, modifier=Add, flags=4),
                act(15, value=10, flags=4),
                act(16, mask=7, value=11, flags=4),
                act(26, dest=P2, value=5000, unit=Gas, modifier=Subtract, flags=4),
                act(27, dest=P1, value=100, unit=ScoreKills, modifier=Add, flags=4),
                act(28, mask=Anywhere, flags=4),
                act(29, time=1000, unit=65, flags=20),
                act(30, flags=4),
                act(31, flags=4),
                dict(act(41, flags=4), text="next.scm"),
                act(54, flags=4),
                act(55, flags=4),
                act(56, flags=4),
                act(57, dest=P1, unit=Ally, flags=4),
            ],
        )

    def test_game_state_and_ui_actions_validate_ranges(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: CenterView(256))
        bounds(lambda: SetSwitch(256, SwitchActionSet))
        bounds(lambda: SetSwitch(0, 7))
        bounds(lambda: SetCountdownTimer(6, 0))
        bounds(lambda: SetCountdownTimer(Add, -1))
        bounds(lambda: RunAIScript(-1))
        bounds(lambda: RunAIScriptAt(10, 256))
        bounds(lambda: SetResources(12, SetTo, 0, Ore))
        bounds(lambda: SetResources(P1, 0, 0, Ore))
        bounds(lambda: SetResources(P1, SetTo, -1, Ore))
        bounds(lambda: SetResources(P1, SetTo, 0, 3))
        bounds(lambda: SetScore(12, SetTo, 0, ScoreKills))
        bounds(lambda: SetScore(P1, SetTo, 0, 8))
        bounds(lambda: MinimapPing(256))
        bounds(lambda: TalkingPortrait(228, 0))
        bounds(lambda: TalkingPortrait(0, -1))
        bounds(lambda: SetAllianceStatus(12, Ally))
        bounds(lambda: SetAllianceStatus(P1, 3))



class TestLeaderBoardActions(unittest.TestCase):
    def test_leaderboard_actions_emit_documented_fields(self) -> None:
        def prog() -> None:
            Trigger(
                conds=[],
                acts=[
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
                preserved=True,
            )

        mod = buildModule(prog)
        self.assertEqual(
            [dict(a) for a in mod.triggers[0].acts],
            [
                dict(act(17, unit=65, flags=20), text="Control"),
                dict(act(18, mask=Anywhere, unit=65, flags=20), text="ControlAt"),
                dict(act(19, unit=Gas, flags=4), text="Resources"),
                dict(act(20, unit=65, flags=20), text="Kills"),
                dict(act(21, unit=ScoreKills, flags=4), text="Score"),
                act(32, modifier=PropDisable, flags=4),
                dict(act(33, value=10, unit=65, flags=20), text="GoalControl"),
                dict(
                    act(34, mask=Anywhere, value=10, unit=65, flags=20),
                    text="GoalControlAt",
                ),
                dict(act(35, value=10, unit=Gas, flags=4), text="GoalResources"),
                dict(act(36, value=10, unit=65, flags=20), text="GoalKills"),
                dict(act(37, value=10, unit=ScoreKills, flags=4), text="GoalScore"),
                act(40, value=10, flags=4),
            ],
        )

    def test_leaderboard_actions_validate_ranges(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: LeaderBoardControl(228, "x"))
        bounds(lambda: LeaderBoardControlAt(0, 256, "x"))
        bounds(lambda: LeaderBoardResources(3, "x"))
        bounds(lambda: LeaderBoardKills(228, "x"))
        bounds(lambda: LeaderBoardScore(8, "x"))
        bounds(lambda: LeaderBoardComputerPlayers(3))
        bounds(lambda: LeaderBoardGoalControl(-1, 0, "x"))
        bounds(lambda: LeaderBoardGoalControl(0, 228, "x"))
        bounds(lambda: LeaderBoardGoalControlAt(0, 0, 256, "x"))
        bounds(lambda: LeaderBoardGoalResources(-1, Gas, "x"))
        bounds(lambda: LeaderBoardGoalResources(0, 3, "x"))
        bounds(lambda: LeaderBoardGoalKills(-1, 0, "x"))
        bounds(lambda: LeaderBoardGoalScore(-1, ScoreKills, "x"))
        bounds(lambda: LeaderBoardGoalScore(0, 8, "x"))
        bounds(lambda: LeaderBoardGreed(-1))



class TestTransmission(unittest.TestCase):
    def test_transmission_emits_text_and_sound_separately(self) -> None:
        def prog() -> None:
            Trigger(
                conds=[],
                acts=[
                    Transmission(65, Anywhere, "sound.wav", Add, 1000, "Hello"),
                    PlayWAV("sound.wav"),
                ],
                preserved=True,
            )

        mod = buildModule(prog)
        self.assertEqual(
            [dict(a) for a in mod.triggers[0].acts],
            [
                dict(
                    act(
                        7,
                        mask=Anywhere,
                        time=1000,
                        unit=65,
                        modifier=Add,
                        flags=4,
                    ),
                    text="Hello",
                    wav="sound.wav",
                ),
                dict(act(8, flags=4), wav="sound.wav"),
            ],
        )

    def test_transmission_validates_stock_ranges(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        bounds(lambda: Transmission(228, Anywhere, "", SetTo, 0, ""))
        bounds(lambda: Transmission(0, 256, "", SetTo, 0, ""))
        bounds(lambda: Transmission(0, 0, "", 6, 0, ""))
        bounds(lambda: Transmission(0, 0, "", SetTo, -1, ""))
        bounds(lambda: Transmission(0, 0, "", SetTo, 0, "", 256))



class TestCreateUnitWithProperties(unittest.TestCase):
    def test_unit_property_builds_normalized_record(self) -> None:
        self.assertEqual(UnitProperty(), [0] * 20)
        self.assertEqual(
            UnitProperty(
                hit_points=50,
                shields=75,
                energy=25,
                resource=300,
                hangar=2,
                cloaked=True,
                burrowed=False,
                invincible=True,
            ),
            [17, 0, 62, 0, 0, 50, 75, 25, 44, 1, 0, 0, 2, 0, 17, 0, 0, 0, 0, 0],
        )
        # 100% HP/shields and 0 hangar mean "unset".
        self.assertEqual(
            UnitProperty(hit_points=100, shields=100, hangar=0),
            [0] * 20,
        )
        # An explicit False flag is equivalent to unset.
        self.assertEqual(
            UnitProperty(cloaked=False, burrowed=False),
            [0] * 20,
        )
        cloaked = UnitProperty(cloaked=True)
        self.assertEqual(cloaked[0], 1)  # sprpvalid bit 0
        self.assertEqual(cloaked[14], 1)  # sprpflag bit 0

    def test_create_unit_with_properties_emits_documented_fields(self) -> None:
        properties = UnitProperty(hit_points=50)

        def prog() -> None:
            Trigger(
                conds=[],
                acts=[CreateUnitWithProperties(3, 65, Anywhere, P1, properties)],
                preserved=True,
            )

        mod = buildModule(prog)
        self.assertEqual(
            [dict(a) for a in mod.triggers[0].acts],
            [
                dict(
                    act(
                        11,
                        mask=Anywhere,
                        dest=P1,
                        value=0,
                        unit=65,
                        modifier=3,
                        flags=28,
                    ),
                    uprp=properties,
                ),
            ],
        )

    def test_unit_property_and_action_validate_inputs(self) -> None:
        def bounds(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

        def type_error(fn) -> None:
            with self.assertRaises(EudslError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

        bounds(lambda: UnitProperty(hit_points=-1))
        bounds(lambda: UnitProperty(hit_points=101))
        bounds(lambda: UnitProperty(hit_points=1.5))
        bounds(lambda: UnitProperty(shields=101))
        bounds(lambda: UnitProperty(energy=-1))
        bounds(lambda: UnitProperty(resource=65536))
        bounds(lambda: UnitProperty(resource=-1))
        bounds(lambda: UnitProperty(hangar=256))
        bounds(lambda: UnitProperty(hangar=-1))
        type_error(lambda: UnitProperty(cloaked="yes"))  # type: ignore[arg-type]
        bounds(lambda: CreateUnitWithProperties(0, 65, Anywhere, P1, UnitProperty()))
        bounds(lambda: CreateUnitWithProperties(256, 65, Anywhere, P1, UnitProperty()))
        bounds(lambda: CreateUnitWithProperties(1, 228, Anywhere, P1, UnitProperty()))
        bounds(lambda: CreateUnitWithProperties(1, 65, 256, P1, UnitProperty()))
        bounds(lambda: CreateUnitWithProperties(1, 65, Anywhere, 12, UnitProperty()))
        type_error(lambda: CreateUnitWithProperties(1, 65, Anywhere, P1, []))
        type_error(
            lambda: CreateUnitWithProperties(
                1, 65, Anywhere, P1, [256] + [0] * 19
            )
        )


if __name__ == "__main__":
    unittest.main()
