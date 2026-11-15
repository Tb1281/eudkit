"""Player state, existence, and all-player broadcasts (`T-ABS-PLAYER`).

The existing name-matcher contract lives in ``test_player.py`` and stays
untouched; this module pins the new surface from §3/§4 of the ticket:

* ``turbo`` is one ``SetMemory(0x6509A0, SetTo, 0)`` action;
* ``user_player``/``game_tick`` share one 32-bit reader body per address;
* ``player_exists`` is the constant table predicate (two triggers) or
  arithmetic + ``with_epd`` (no ``SetNextPtr``/``Switch``);
* ``for_each_player`` saves/restores ``CurrentPlayer`` once per site and emits
  the body once (loop) or once per selected player (unrolled);
* ``Player``/``PlayerField`` mirror the ``scdata/player.py`` dword arrays and
  reject runtime values; ``PVariable`` is an eight-slot ``FixedArray``;
* every broadcast helper is a gated ``for_each_player`` over one stock action
  and validates its raw location/unit arguments before emitting.

Exact static/executed cost pins for these sites live in
``crates/eudkit-cli/tests/player_runtime.rs`` so both frontends are measured
through the same lowering pipeline.
"""

from __future__ import annotations

import inspect
import json
import unittest
from typing import Any, Callable

import py.player as player_module
from py import (
    Add,
    AtLeast,
    AtMost,
    Deaths,
    EudslError,
    Exactly,
    Never,
    SetDeaths,
    SetTo,
    Subtract,
    Var,
    build_module,
    when,
)
from py.camera import GAME_TICK_ADDR, USER_PLAYER_ADDR
from py.cp import CP_ADDR
from py.player import (
    PLAYER_MAX_ID,
    PLAYER_SLOT_BYTES,
    PLAYER_SLOT_COUNT,
    PLAYER_TABLE_ADDR,
    PVariable,
    Player,
    PlayerField,
    TURBO_ADDR,
    center_view_all,
    centerViewAll,
    display_text_all,
    displayTextAll,
    for_each_player,
    forEachPlayer,
    game_tick,
    gameTick,
    is_user,
    isUser,
    minimap_ping_all,
    minimapPingAll,
    play_wav_all,
    playWavAll,
    player_exists,
    playerExists,
    set_mission_objectives_all,
    setMissionObjectivesAll,
    talking_portrait_all,
    talkingPortraitAll,
    turbo,
    user_player,
    userPlayer,
)
from py.trigger import _get_active_module_builder
from py.view import CURRENT_PLAYER, EPD, EPD_CURRENT_PLAYER

#: §4.2 — the ``scdata/player.py`` dword arrays, one element per player.
FIELD_ADDRS: dict[str, int] = {
    "minerals": 0x57F0F0,
    "gas": 0x57F120,
    "cumulative_gas": 0x57F150,
    "cumulative_minerals": 0x57F180,
    "zerg_control_available": 0x582144,
    "zerg_control_used": 0x582174,
    "zerg_control_max": 0x5821A4,
    "terran_supply_available": 0x5821D4,
    "terran_supply_used": 0x582204,
    "terran_supply_max": 0x582234,
    "protoss_psi_available": 0x582264,
    "protoss_psi_used": 0x582294,
    "protoss_psi_max": 0x5822C4,
    "unit_score": 0x581E44,
    "building_score": 0x582024,
    "kill_score": 0x581F04,
    "razing_score": 0x582054,
    "custom_score": 0x5822F4,
}

CAMEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("cumulativeMinerals", "cumulative_minerals"),
    ("cumulativeGas", "cumulative_gas"),
    ("zergControlAvailable", "zerg_control_available"),
    ("zergControlUsed", "zerg_control_used"),
    ("zergControlMax", "zerg_control_max"),
    ("terranSupplyAvailable", "terran_supply_available"),
    ("terranSupplyUsed", "terran_supply_used"),
    ("terranSupplyMax", "terran_supply_max"),
    ("protossPsiAvailable", "protoss_psi_available"),
    ("protossPsiUsed", "protoss_psi_used"),
    ("protossPsiMax", "protoss_psi_max"),
    ("unitScore", "unit_score"),
    ("buildingScore", "building_score"),
    ("killScore", "kill_score"),
    ("razingScore", "razing_score"),
    ("customScore", "custom_score"),
    ("mineral", "minerals"),
    ("cumulativeMineral", "cumulative_minerals"),
)

NEW_EXPORTS = {
    "PLAYER_TABLE_ADDR",
    "PLAYER_SLOT_BYTES",
    "PLAYER_SLOT_COUNT",
    "PLAYER_MAX_ID",
    "TURBO_ADDR",
    "turbo",
    "user_player",
    "userPlayer",
    "game_tick",
    "gameTick",
    "is_user",
    "isUser",
    "player_exists",
    "playerExists",
    "for_each_player",
    "forEachPlayer",
    "Player",
    "PlayerField",
    "PVariable",
    "display_text_all",
    "displayTextAll",
    "play_wav_all",
    "playWavAll",
    "minimap_ping_all",
    "minimapPingAll",
    "center_view_all",
    "centerViewAll",
    "set_mission_objectives_all",
    "setMissionObjectivesAll",
    "talking_portrait_all",
    "talkingPortraitAll",
}


def _build(body: Callable[[], Any]) -> dict[str, Any]:
    return json.loads(build_module(body).to_json())["module"]


def _stmts(body: Callable[[], Any]) -> list[dict[str, Any]]:
    return _build(body)["stmts"]


def _flat(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        out.append(node)
        out.extend(_flat(node.get("body") or []))
        out.extend(_flat(node.get("else_body") or []))
    return out


def _all_nodes(module: dict[str, Any]) -> list[dict[str, Any]]:
    return _flat(module["stmts"]) + [
        node for func in module.get("funcs", []) for node in _flat(func["body"])
    ]


def _conds(module: dict[str, Any]) -> list[dict[str, Any]]:
    return [cond for node in _all_nodes(module) for cond in (node.get("conds") or [])]


def _acts(module: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in _all_nodes(module):
        if node.get("kind") == "act":
            out.append(node)
        out.extend(node.get("acts") or [])
    return out


def _code(fn: Callable[[], Any]) -> str:
    with unittest.TestCase().assertRaises(EudslError) as caught:
        fn()
    return caught.exception.code


class ConstantsAndAliasesTest(unittest.TestCase):
    def test_documented_constants(self) -> None:
        self.assertEqual(PLAYER_TABLE_ADDR, 0x51A280)
        self.assertEqual(PLAYER_SLOT_BYTES, 12)
        self.assertEqual(PLAYER_SLOT_COUNT, 8)
        self.assertEqual(PLAYER_MAX_ID, 11)
        self.assertEqual(TURBO_ADDR, 0x6509A0)
        # Re-exported from camera.py, not redefined.
        self.assertIs(player_module.USER_PLAYER_ADDR, USER_PLAYER_ADDR)
        self.assertIs(player_module.GAME_TICK_ADDR, GAME_TICK_ADDR)

    def test_all_has_every_new_name_exactly_once(self) -> None:
        names = player_module.__all__
        self.assertEqual(len(names), len(set(names)))
        for name in NEW_EXPORTS:
            self.assertIn(name, names)
        # §9: the address constants stay owned/exported by camera.py.
        self.assertNotIn("USER_PLAYER_ADDR", names)
        self.assertNotIn("GAME_TICK_ADDR", names)

    def test_camelcase_aliases_are_the_snake_case_callables(self) -> None:
        pairs = (
            (userPlayer, user_player),
            (gameTick, game_tick),
            (isUser, is_user),
            (playerExists, player_exists),
            (forEachPlayer, for_each_player),
            (displayTextAll, display_text_all),
            (playWavAll, play_wav_all),
            (minimapPingAll, minimap_ping_all),
            (centerViewAll, center_view_all),
            (setMissionObjectivesAll, set_mission_objectives_all),
            (talkingPortraitAll, talking_portrait_all),
        )
        for camel, snake in pairs:
            self.assertIs(camel, snake)

    def test_player_field_camelcase_aliases_share_one_handle(self) -> None:
        player = Player(3)
        for camel, snake in CAMEL_FIELDS:
            self.assertIs(getattr(player, camel), getattr(player, snake))


class TurboTest(unittest.TestCase):
    def test_turbo_is_one_set_memory_action_in_one_trigger(self) -> None:
        stmts = _stmts(turbo)
        self.assertEqual(len(stmts), 1)
        trigger = stmts[0]
        self.assertEqual(trigger["kind"], "trigger")
        self.assertEqual(len(trigger["acts"]), 1)
        act = trigger["acts"][0]
        self.assertEqual(act["acttype"], 45)
        self.assertEqual(act["modifier"], SetTo)
        self.assertEqual(act["value"], 0)
        self.assertEqual(act["dest"], EPD(TURBO_ADDR))
        self.assertEqual(EPD(TURBO_ADDR), 203151)


class ReaderTest(unittest.TestCase):
    def test_user_player_and_game_tick_each_declare_one_shared_body(self) -> None:
        module = _build(lambda: (user_player(), user_player(), game_tick()))
        funcs = module["funcs"]
        self.assertEqual(len(funcs), 2)
        calls = [node for node in module["stmts"] if node["kind"] == "call"]
        self.assertEqual([call["func"] for call in calls], [0, 0, 1])

        first_cond: dict[int, int] = {}
        for func in funcs:
            self.assertEqual(len(func["body"]), 33)
            conds = [cond for trigger in func["body"] for cond in trigger["conds"]]
            self.assertEqual(len(conds), 32)
            self.assertEqual({cond["condtype"] for cond in conds}, {15})
            first_cond[func["id"]] = conds[0]["player"]

        self.assertEqual(first_cond[0], EPD(USER_PLAYER_ADDR) & 0xFFFFFFFF)
        self.assertEqual(first_cond[1], EPD(GAME_TICK_ADDR) & 0xFFFFFFFF)

    def test_two_user_player_sites_share_one_reader_result_var_per_call(self) -> None:
        module = _build(lambda: (user_player(), user_player()))
        self.assertEqual(len(module["funcs"]), 1)
        calls = [node for node in module["stmts"] if node["kind"] == "call"]
        self.assertEqual([call["func"] for call in calls], [0, 0])
        self.assertNotEqual(calls[0]["dst"], calls[1]["dst"])


class IsUserTest(unittest.TestCase):
    def test_constant_is_one_condition_against_the_shared_read(self) -> None:
        module = _build(
            lambda: when(is_user(3), lambda: SetDeaths(9000, SetTo, 1, 0))
        )
        top = [node for node in module["stmts"] if node["kind"] == "when"]
        self.assertEqual(len(top), 1)
        cond = top[0]["conds"][0]
        self.assertEqual(cond["condtype"], 15)
        self.assertEqual(cond["comparison"], Exactly)
        self.assertEqual(cond["amount"], 3)
        self.assertEqual(cond["player"], 0)
        calls = [node for node in module["stmts"] if node["kind"] == "call"]
        self.assertEqual(cond["relocs"][0]["target_var"], calls[0]["dst"])

    def test_runtime_is_a_var_equality_condition(self) -> None:
        module = _build(
            lambda: when(is_user(Var(4)), lambda: SetDeaths(9000, SetTo, 1, 0))
        )
        conds = _conds(module)
        self.assertEqual({cond["condtype"] for cond in conds}, {15})
        top = [node for node in module["stmts"] if node["kind"] == "when"]
        cond = top[0]["conds"][0]
        self.assertEqual(cond["comparison"], Exactly)
        self.assertTrue(cond["relocs"])

    def test_rejects_non_operands(self) -> None:
        for bad in (True, "3", 3.0, None):
            self.assertEqual(
                _code(
                    lambda bad=bad: build_module(
                        lambda: when(is_user(bad), lambda: None)
                    )
                ),
                "EUDSL-TYPE",
            )


class PlayerExistsTest(unittest.TestCase):
    def test_constant_path_emits_two_triggers_and_the_table_condition(self) -> None:
        for player in range(PLAYER_SLOT_COUNT):
            module = _build(lambda player=player: player_exists(player))
            stmts = module["stmts"]
            self.assertEqual([node["kind"] for node in stmts], ["trigger", "when"])

            base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player
            cond = stmts[1]["conds"][0]
            self.assertEqual(cond["player"], EPD(base + 8) & 0xFFFFFFFF)
            self.assertEqual(cond["amount"], (~(base + 4)) & 0xFFFFFFFF)
            self.assertEqual(cond["comparison"], Exactly)
            self.assertEqual(cond["condtype"], 15)

            setter = stmts[0]["acts"][0]
            self.assertEqual(setter["acttype"], 45)
            self.assertEqual(setter["modifier"], SetTo)
            self.assertEqual(setter["value"], 1)
            cleared = stmts[1]["body"][0]
            self.assertEqual(cleared["value"], 0)
            self.assertEqual(
                setter["relocs"][0]["target_var"],
                cleared["relocs"][0]["target_var"],
            )

    def test_constant_rejects_eight_and_above(self) -> None:
        for bad in (8, -1, 9):
            self.assertEqual(
                _code(lambda bad=bad: build_module(lambda: player_exists(bad))),
                "EUDSL-RANGE",
            )

    def test_constant_rejects_bools_and_non_integers(self) -> None:
        for bad in (True, "0", None, 0.0):
            self.assertEqual(
                _code(lambda bad=bad: build_module(lambda: player_exists(bad))),
                "EUDSL-TYPE",
            )

    def test_runtime_path_is_arithmetic_and_one_cp_binding(self) -> None:
        module = _build(
            lambda: when(player_exists(Var(3)).eq(1), lambda: None)
        )
        # One CP-relative read per existence site, never the 8-arm switch.
        self.assertEqual(len(module["funcs"]), 2)
        func_players = {
            cond["player"]
            for func in module["funcs"]
            for node in _flat(func["body"])
            for cond in (node.get("conds") or [])
        }
        self.assertEqual(func_players, {EPD_CURRENT_PLAYER, CURRENT_PLAYER})

        conds = _conds(module)
        self.assertEqual({cond["condtype"] for cond in conds}, {15})

        nodes = _all_nodes(module)
        for node in nodes:
            for reloc in node.get("relocs") or []:
                self.assertNotIn("target_trigger", reloc)
            for cond in node.get("conds") or []:
                for reloc in cond.get("relocs") or []:
                    self.assertNotIn("target_trigger", reloc)
            for act in node.get("acts") or []:
                for reloc in act.get("relocs") or []:
                    self.assertNotIn("target_trigger", reloc)

        cp_stores = [
            node
            for node in nodes
            if node.get("kind") == "varstore" and node.get("dst_addr") == CP_ADDR
        ]
        self.assertEqual(len(cp_stores), 2)

        # §6 deviation: the switch spelling is not in the source either.
        source = inspect.getsource(player_exists)
        self.assertNotIn("SetNextPtr", source)
        self.assertNotIn("Switch", source)

    def test_runtime_rejects_non_operands(self) -> None:
        for bad in (True, "1", None, 1.0):
            self.assertEqual(
                _code(
                    lambda bad=bad: build_module(
                        lambda: when(player_exists(bad).eq(1), lambda: None)
                    )
                ),
                "EUDSL-TYPE",
            )


class ForEachPlayerTest(unittest.TestCase):
    def test_loop_emits_body_once_and_restores_cp_unconditionally(self) -> None:
        module = _build(
            lambda: for_each_player(
                lambda player: SetDeaths(9000, SetTo, player, 0)
            )
        )
        stmts = module["stmts"]
        self.assertEqual([node["kind"] for node in stmts], ["call", "while", "varstore"])

        loop = stmts[1]
        self.assertEqual(loop["conds"][0]["amount"], PLAYER_SLOT_COUNT - 1)
        self.assertEqual(loop["conds"][0]["comparison"], AtMost)

        markers = [act for act in _acts(module) if act.get("dest") == 9000]
        self.assertEqual(len(markers), 1)

        current_id = loop["conds"][0]["relocs"][0]["target_var"]
        loop_nodes = _flat(loop["body"])
        seeds = [
            node
            for node in loop_nodes
            if node.get("kind") == "varstore"
            and node.get("dst_addr") == CP_ADDR
            and node.get("src_var") == current_id
        ]
        self.assertEqual(len(seeds), 1)

        restore = stmts[2]
        self.assertEqual(restore["dst_addr"], CP_ADDR)
        self.assertEqual(restore["src_var"], stmts[0]["dst"])

    def test_unrolled_mode_gates_once_per_selected_player(self) -> None:
        module = _build(
            lambda: for_each_player(
                lambda player: SetDeaths(9000, SetTo, player, 0),
                players=[0, 2],
            )
        )
        stmts = module["stmts"]
        self.assertEqual(
            [node["kind"] for node in stmts],
            ["call", "when", "when", "varstore"],
        )
        for player, node in zip((0, 2), stmts[1:3]):
            base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player
            cond = node["conds"][0]
            self.assertEqual(cond["player"], EPD(base + 8) & 0xFFFFFFFF)
            self.assertEqual(cond["amount"], (~(base + 4)) & 0xFFFFFFFF)

            acts = [act for act in _flat(node["body"]) if act.get("kind") == "act"]
            self.assertEqual(acts[0]["dest"], EPD_CURRENT_PLAYER)
            self.assertEqual(acts[0]["value"], player)
            self.assertEqual(acts[1]["dest"], 9000)
        self.assertEqual(stmts[3]["src_var"], stmts[0]["dst"])

    def test_unrolled_ungated_mode_has_no_gate(self) -> None:
        module = _build(
            lambda: for_each_player(
                lambda player: SetDeaths(9000, SetTo, player, 0),
                active_only=False,
                players=[0, 2],
            )
        )
        stmts = module["stmts"]
        self.assertEqual([node["kind"] for node in stmts], ["call", "trigger", "varstore"])
        acts = stmts[1]["acts"]
        self.assertEqual([act["dest"] for act in acts], [EPD_CURRENT_PLAYER, 9000] * 2)
        self.assertEqual(
            [act["value"] for act in acts[:2]],
            [0, 0],
        )

    def test_loop_ungated_mode_has_no_player_exists_read(self) -> None:
        module = _build(
            lambda: for_each_player(
                lambda player: SetDeaths(9000, SetTo, player, 0),
                active_only=False,
            )
        )
        self.assertEqual(len(module["funcs"]), 1)
        func_players = {
            cond["player"]
            for func in module["funcs"]
            for node in _flat(func["body"])
            for cond in (node.get("conds") or [])
        }
        self.assertEqual(func_players, {EPD_CURRENT_PLAYER})

    def test_validation_leaves_no_partial_emission(self) -> None:
        cases: list[tuple[Callable[[], Any], str]] = [
            (lambda: for_each_player(lambda player: None, players={0, 1}), "EUDSL-TYPE"),
            (
                lambda: for_each_player(lambda player: None, players=frozenset({0})),
                "EUDSL-TYPE",
            ),
            (lambda: for_each_player(lambda player: None, players=[0, 0]), "EUDSL-BOUNDS"),
            (lambda: for_each_player(lambda player: None, players=[]), "EUDSL-BOUNDS"),
            (lambda: for_each_player(lambda player: None, players=[12]), "EUDSL-RANGE"),
            (lambda: for_each_player(lambda player: None, players=[True]), "EUDSL-TYPE"),
            (lambda: for_each_player(None), "EUDSL-TYPE"),
        ]

        def prog() -> None:
            builder = _get_active_module_builder()
            self.assertIsNotNone(builder)
            before = len(builder.entries)
            for call, expected in cases:
                with self.assertRaises(EudslError) as caught:
                    call()
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(builder.entries), before)

        build_module(prog)


class PlayerFieldTest(unittest.TestCase):
    def test_every_field_maps_to_the_documented_address(self) -> None:
        for player in (0, 7, 11):
            handles = Player(player)
            self.assertEqual(handles.index, player)
            for name, addr in FIELD_ADDRS.items():
                field = getattr(handles, name)
                self.assertIsInstance(field, PlayerField)
                self.assertEqual(field.player, player)
                self.assertEqual(field.base_addr, addr)
                self.assertEqual(field.addr, (addr + 4 * player) & 0xFFFFFFFF)
                self.assertEqual(field.epd, (EPD(addr) + player) & 0xFFFFFFFF)

    def test_player_index_guards(self) -> None:
        for bad in (12, -1):
            self.assertEqual(_code(lambda bad=bad: Player(bad)), "EUDSL-RANGE")
        for bad in (True, "3", None, 3.0):
            self.assertEqual(_code(lambda bad=bad: Player(bad)), "EUDSL-TYPE")
        for player in (0, 7, 8, 11):
            self.assertEqual(Player(player).index, player)

    def test_set_iadd_isub_emit_one_deaths_act(self) -> None:
        expected_epd = (EPD(FIELD_ADDRS["minerals"]) + 3) & 0xFFFFFFFF
        for method, modifier, value in (
            ("set", SetTo, 500),
            ("iadd", Add, 3),
            ("isub", Subtract, 2),
        ):
            module = _build(
                lambda method=method, value=value: getattr(
                    Player(3).minerals, method
                )(value)
            )
            stmts = module["stmts"]
            self.assertEqual(len(stmts), 1)
            self.assertEqual(stmts[0]["kind"], "trigger")
            act = stmts[0]["acts"][0]
            self.assertEqual(act["acttype"], 45)
            self.assertEqual(act["modifier"], modifier)
            self.assertEqual(act["value"], value)
            self.assertEqual(act["dest"], expected_epd)

    def test_comparisons_emit_one_condition(self) -> None:
        expected_epd = (EPD(FIELD_ADDRS["minerals"]) + 3) & 0xFFFFFFFF
        for method, comparison, rhs, amount in (
            ("eq", Exactly, 777, 777),
            ("ge", AtLeast, 5, 5),
            ("le", AtMost, 5, 5),
            ("gt", AtLeast, 5, 6),
            ("lt", AtMost, 5, 4),
        ):
            module = _build(
                lambda method=method, rhs=rhs: when(
                    getattr(Player(3).minerals, method)(rhs), lambda: None
                )
            )
            cond = [
                node for node in module["stmts"] if node["kind"] == "when"
            ][0]["conds"][0]
            self.assertEqual(cond["condtype"], 15)
            self.assertEqual(cond["comparison"], comparison)
            self.assertEqual(cond["amount"], amount)
            self.assertEqual(cond["player"], expected_epd)

    def test_ne_delegates_and_extreme_gt_lt_become_never(self) -> None:
        module = _build(
            lambda: when(Player(3).minerals.ne(0), lambda: None)
        )
        cond = [
            node for node in module["stmts"] if node["kind"] == "when"
        ][0]["conds"][0]
        self.assertEqual(cond["comparison"], AtLeast)
        self.assertEqual(cond["amount"], 1)

        for value, method in ((0xFFFFFFFF, "gt"), (0, "lt")):
            module = _build(
                lambda value=value, method=method: when(
                    getattr(Player(3).minerals, method)(value), lambda: None
                )
            )
            cond = [
                node for node in module["stmts"] if node["kind"] == "when"
            ][0]["conds"][0]
            self.assertEqual(cond["condtype"], 23)

    def test_runtime_values_are_type_errors(self) -> None:
        for method in ("set", "iadd", "isub"):
            self.assertEqual(
                _code(
                    lambda method=method: build_module(
                        lambda: getattr(Player(3).minerals, method)(Var(5))
                    )
                ),
                "EUDSL-TYPE",
            )
        for method in ("eq", "ne", "ge", "le", "gt", "lt"):
            self.assertEqual(
                _code(
                    lambda method=method: build_module(
                        lambda: when(
                            getattr(Player(3).minerals, method)(Var(5)), lambda: None
                        )
                    )
                ),
                "EUDSL-TYPE",
            )
        self.assertEqual(
            _code(
                lambda: build_module(lambda: Player(3).minerals.set(True))
            ),
            "EUDSL-TYPE",
        )

    def test_player_exists_method_delegates(self) -> None:
        self.assertEqual(
            _build(lambda: Player(3).exists()),
            _build(lambda: player_exists(3)),
        )


class PVariableTest(unittest.TestCase):
    def test_storage_is_an_eight_slot_fixed_array(self) -> None:
        def prog() -> None:
            variable = PVariable()
            self.assertEqual(variable.storage.length, PLAYER_SLOT_COUNT)
            self.assertEqual(len(variable.storage.vars), PLAYER_SLOT_COUNT)

        build_module(prog)

    def test_constant_get_and_set(self) -> None:
        def prog() -> None:
            variable = PVariable()
            variable.set(3, 9)
            held = variable.get(3)
            self.assertIsInstance(held, Var)
            self.assertIs(held, variable.storage.vars[3])

        module = _build(prog)
        acts = _acts(module)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["value"], 9)
        self.assertEqual(acts[0]["modifier"], SetTo)

    def test_init_zero_emits_no_statements(self) -> None:
        module = _build(lambda: PVariable())
        self.assertEqual(module["stmts"], [])
        self.assertEqual(len(module["vars"]), PLAYER_SLOT_COUNT)

    def test_init_nonzero_fills_every_slot(self) -> None:
        module = _build(lambda: PVariable(5))
        stmts = module["stmts"]
        self.assertEqual(len(stmts), 1)
        acts = stmts[0]["acts"]
        self.assertEqual(len(acts), PLAYER_SLOT_COUNT)
        self.assertEqual({act["value"] for act in acts}, {5})
        self.assertEqual({act["modifier"] for act in acts}, {SetTo})
        self.assertEqual(
            sorted(act["relocs"][0]["target_var"] for act in acts),
            list(range(PLAYER_SLOT_COUNT)),
        )

    def test_constant_index_guards(self) -> None:
        for bad in (8, -1):
            self.assertEqual(
                _code(lambda bad=bad: build_module(lambda: PVariable().get(bad))),
                "EUDSL-RANGE",
            )
            self.assertEqual(
                _code(
                    lambda bad=bad: build_module(lambda: PVariable().set(bad, 0))
                ),
                "EUDSL-RANGE",
            )
        for bad in (True, "0", None):
            self.assertEqual(
                _code(lambda bad=bad: build_module(lambda: PVariable().get(bad))),
                "EUDSL-TYPE",
            )

    def test_runtime_index_and_value_paths(self) -> None:
        def prog() -> None:
            variable = PVariable()
            index = Var(2)
            held = variable.get(index)
            self.assertIsInstance(held, Var)
            variable.set(index, 9)
            variable.set(index, Var(7))

        module = _build(prog)
        self.assertGreater(len(_conds(module)), 0)
        self.assertGreater(len(_acts(module)), 0)

    def test_init_and_value_guards(self) -> None:
        self.assertEqual(
            _code(lambda: build_module(lambda: PVariable(True))),
            "EUDSL-TYPE",
        )
        self.assertEqual(
            _code(lambda: build_module(lambda: PVariable().set(0, True))),
            "EUDSL-TYPE",
        )
        self.assertEqual(
            _code(lambda: build_module(lambda: PVariable().set(0, "9"))),
            "EUDSL-TYPE",
        )


class BroadcastTest(unittest.TestCase):
    ACTTYPE_NAMES = {
        8: "PlayWAV",
        9: "DisplayText",
        10: "CenterView",
        12: "SetMissionObjectives",
        28: "MinimapPing",
        29: "TalkingPortrait",
    }

    def _module_for(self, call: Callable[[], Any]) -> dict[str, Any]:
        module = _build(call)
        while_stmts = [node for node in module["stmts"] if node["kind"] == "while"]
        self.assertEqual(len(while_stmts), 1)
        self.assertEqual(while_stmts[0]["conds"][0]["amount"], PLAYER_SLOT_COUNT - 1)
        self.assertEqual(module["stmts"][-1]["dst_addr"], CP_ADDR)
        return module

    def test_each_helper_emits_one_loop_over_the_stock_action(self) -> None:
        cases = (
            (lambda: display_text_all("hello"), 9, {"text": "hello", "flags": 4}),
            (lambda: play_wav_all("sound"), 8, {"wav": "sound", "flags": 4}),
            (lambda: minimap_ping_all(3), 28, {"mask": 3, "flags": 4}),
            (lambda: center_view_all(4), 10, {"mask": 4, "flags": 4}),
            (
                lambda: set_mission_objectives_all("obj"),
                12,
                {"text": "obj", "flags": 4},
            ),
            (
                lambda: talking_portrait_all(5, 100),
                29,
                {"unit": 5, "time": 100, "flags": 20},
            ),
        )
        for call, acttype, expected in cases:
            module = self._module_for(call)
            matches = [act for act in _acts(module) if act.get("acttype") == acttype]
            self.assertEqual(len(matches), 1, self.ACTTYPE_NAMES[acttype])
            for key, value in expected.items():
                self.assertEqual(matches[0][key], value)

    def test_bad_arguments_are_rejected_before_the_loop(self) -> None:
        cases: list[tuple[Callable[[], Any], str]] = [
            (lambda: display_text_all(3), "EUDSL-TYPE"),
            (lambda: play_wav_all(3), "EUDSL-TYPE"),
            (lambda: set_mission_objectives_all(3), "EUDSL-TYPE"),
            (lambda: minimap_ping_all(256), "EUDSL-RANGE"),
            (lambda: minimap_ping_all(-1), "EUDSL-RANGE"),
            (lambda: minimap_ping_all(True), "EUDSL-TYPE"),
            (lambda: center_view_all(256), "EUDSL-RANGE"),
            (lambda: talking_portrait_all(999, 0), "EUDSL-RANGE"),
            (lambda: talking_portrait_all(0, -1), "EUDSL-RANGE"),
            (lambda: talking_portrait_all(0, 0x1_0000_0000), "EUDSL-RANGE"),
        ]

        def prog() -> None:
            builder = _get_active_module_builder()
            self.assertIsNotNone(builder)
            before = len(builder.entries)
            for call, expected in cases:
                with self.assertRaises(EudslError) as caught:
                    call()
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(builder.entries), before)

        build_module(prog)


if __name__ == "__main__":
    unittest.main()
