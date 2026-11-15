"""Contracts for the SC/BW tech and upgrade state tables (`T-ABS-SCDATA`).

The slot arithmetic is checked against an independent reference that spells
out the §4.1 formulas, so a copy of the module's own helper cannot hide a
regression. The structural tests pin the constant fast path (one masked
`DeathsX`/`SetDeathsX`) and the runtime shape (a `with_epd` binding plus the
four-arm lane dispatch for upgrades). A source grep keeps the absorbed
`SetNextPtr`/`VProc` cache machinery out of the module.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Callable

from py import ReadonlyVar, SetDeaths, SetTo, Var, build_module, when
from py.cp import CP_ADDR
from py.hir import AtLeast, Exactly, SetTo
from py.scstate import (
    MAX_PLAYER_ID,
    TECH_COUNT,
    TECH_SC_COUNT,
    TECH_STATE_BW_BASE,
    TECH_STATE_BW_STRIDE,
    TECH_STATE_SC_BASE,
    TECH_STATE_SC_STRIDE,
    UPGRADE_COUNT,
    UPGRADE_SC_COUNT,
    UPGRADE_STATE_BW_BASE,
    UPGRADE_STATE_BW_STRIDE,
    UPGRADE_STATE_SC_BASE,
    UPGRADE_STATE_SC_STRIDE,
    set_tech_researched,
    set_upgrade_level,
    tech_researched,
    tech_researched_var,
    tech_state_slot,
    upgrade_level,
    upgrade_state_slot,
)
from py.tests.helpers import flatten_stmts, run_sim
from py.view import EPD, EudslError

SCSTATE_SOURCE = Path(__file__).resolve().parent.parent / "scstate.py"
PLAYERS = (0, 1, 7, 11)


def _tech_addr(tech: int, player: int) -> int:
    """Independent §4.1 reference: (base + index offset) + stride * player."""
    if tech < TECH_SC_COUNT:
        return TECH_STATE_SC_BASE + tech + TECH_STATE_SC_STRIDE * player
    return (
        TECH_STATE_BW_BASE
        + (tech - TECH_SC_COUNT)
        + TECH_STATE_BW_STRIDE * player
    )


def _upgrade_addr(upgrade: int, player: int) -> int:
    if upgrade < UPGRADE_SC_COUNT:
        return (
            UPGRADE_STATE_SC_BASE
            + upgrade
            + UPGRADE_STATE_SC_STRIDE * player
        )
    return (
        UPGRADE_STATE_BW_BASE
        + (upgrade - UPGRADE_SC_COUNT)
        + UPGRADE_STATE_BW_STRIDE * player
    )


def _slot(addr: int) -> tuple[int, int]:
    return EPD(addr), 8 * (addr & 3)


def _when_nodes(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [stmt for stmt in stmts if stmt["kind"] == "when"]


def _act_nodes(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every action node, including bare `act` nodes inside a `when` body."""
    out: list[dict[str, Any]] = []
    for stmt in stmts:
        if stmt["kind"] == "act":
            out.append(stmt)
        out.extend(stmt.get("acts") or [])
        out.extend(_act_nodes(stmt.get("body") or []))
        out.extend(_act_nodes(stmt.get("else_body") or []))
    return out


def _cp_stores(stmts: list[dict[str, Any]]) -> int:
    return sum(
        1
        for stmt in stmts
        if stmt["kind"] == "varstore" and stmt.get("dst_addr") == CP_ADDR
    )


def _error(factory: Callable[[], Any]) -> EudslError:
    with unittest.TestCase().assertRaises(EudslError) as caught:

        def prog() -> None:
            factory()

        build_module(prog)
    return caught.exception


class SlotTableTest(unittest.TestCase):
    def test_tech_slot_sweep_matches_the_independent_reference(self) -> None:
        for tech in range(TECH_COUNT):
            for player in PLAYERS:
                with self.subTest(tech=tech, player=player):
                    self.assertEqual(
                        tech_state_slot(tech, player), _slot(_tech_addr(tech, player))
                    )

    def test_tech_slot_spot_checks_the_sc_bw_split(self) -> None:
        # `tech 0 / p1` is `0x58CF44 + 0 + 24`; the ticket's `+ 1` is the
        # index of the neighbouring entry, covered by the `tech 1` spot check.
        self.assertEqual(tech_state_slot(0, 0), _slot(0x58CF44))
        self.assertEqual(tech_state_slot(0, 1), _slot(0x58CF44 + 24))
        self.assertEqual(tech_state_slot(1, 1), _slot(0x58CF5D))
        self.assertEqual(tech_state_slot(1, 1)[1], 8)
        self.assertEqual(tech_state_slot(24, 1), _slot(0x58F154))
        self.assertEqual(tech_state_slot(24, 1)[1], 0)

    def test_upgrade_slot_sweep_matches_the_independent_reference(self) -> None:
        for upgrade in range(UPGRADE_COUNT):
            for player in PLAYERS:
                with self.subTest(upgrade=upgrade, player=player):
                    self.assertEqual(
                        upgrade_state_slot(upgrade, player),
                        _slot(_upgrade_addr(upgrade, player)),
                    )

    def test_upgrade_lane_cycles_follow_the_two_and_three_mod_four_strides(
        self,
    ) -> None:
        # SC base is 4-aligned and its stride is 2 mod 4, so lane = (2p + u).
        for upgrade in range(UPGRADE_SC_COUNT):
            for player in PLAYERS:
                with self.subTest(domain="sc", upgrade=upgrade, player=player):
                    self.assertEqual(
                        upgrade_state_slot(upgrade, player)[1],
                        8 * ((upgrade + 2 * player) & 3),
                    )
        # BW base is 4-aligned and its stride is 3 mod 4.
        for upgrade in range(UPGRADE_SC_COUNT, UPGRADE_COUNT):
            offset = upgrade - UPGRADE_SC_COUNT
            for player in PLAYERS:
                with self.subTest(domain="bw", upgrade=upgrade, player=player):
                    self.assertEqual(
                        upgrade_state_slot(upgrade, player)[1],
                        8 * ((offset + 3 * player) & 3),
                    )

    def test_slot_helpers_accept_plain_player_ids_only(self) -> None:
        self.assertEqual(_error(lambda: tech_state_slot(0, Var(0))).code, "EUDSL-TYPE")
        self.assertEqual(
            _error(lambda: upgrade_state_slot(0, Var(0))).code, "EUDSL-TYPE"
        )
        self.assertEqual(
            _error(lambda: tech_state_slot(0, 1.0)).code, "EUDSL-TYPE"
        )


class ConstantPathTest(unittest.TestCase):
    def test_tech_researched_is_one_condition_with_no_actions(self) -> None:
        def prog() -> None:
            when(tech_researched(5, 3), lambda: None)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        whens = _when_nodes(stmts)
        self.assertEqual(len(whens), 1)
        conds = whens[0]["conds"]
        self.assertEqual(len(conds), 1)
        cond = conds[0]
        epd, shift = tech_state_slot(5, 3)
        self.assertEqual(cond["condtype"], 15)  # Deaths / Memory wire form
        self.assertEqual(cond["player"], epd)
        self.assertEqual(cond["comparison"], AtLeast)
        self.assertEqual(cond["amount"], 1)
        self.assertEqual(cond["locid"], 0xFF << shift)
        self.assertFalse(whens[0].get("body"))
        self.assertEqual(_act_nodes(stmts), [])

    def test_set_tech_researched_masks_exactly_one_lane(self) -> None:
        for on, value in ((True, 1), (False, 0)):
            with self.subTest(on=on):
                def prog(on: bool = on) -> None:
                    set_tech_researched(5, 3, on)

                stmts = build_module(prog).to_hir()["module"]["stmts"]
                self.assertEqual([s["kind"] for s in stmts], ["trigger"])
                acts = stmts[0]["acts"]
                self.assertEqual(len(acts), 1)
                epd, shift = tech_state_slot(5, 3)
                act = acts[0]
                self.assertEqual(act["acttype"], 45)
                self.assertEqual(act["dest"], epd)
                self.assertEqual(act["value"], value << shift)
                self.assertEqual(act["mask"], 0xFF << shift)
                self.assertEqual(act["modifier"], SetTo)

    def test_upgrade_level_uses_the_bw_base_and_reads_the_byte_window(self) -> None:
        def prog() -> None:
            out = Var(0)
            out.set(upgrade_level(46, 0))

        epd, shift = upgrade_state_slot(46, 0)
        self.assertEqual(epd, EPD(UPGRADE_STATE_BW_BASE))
        stmts = build_module(prog).to_hir()["module"]["stmts"]
        triggers = [s for s in stmts if s["kind"] == "trigger"]
        # One accumulator reset plus one trigger per bit in the byte window.
        self.assertEqual(len(triggers), 9)
        masks = {
            cond["locid"]
            for trigger in triggers
            for cond in (trigger.get("conds") or [])
        }
        self.assertEqual(masks, {1 << bit for bit in range(shift, shift + 8)})

    def test_set_upgrade_level_shifts_the_value_into_the_lane(self) -> None:
        def prog() -> None:
            set_upgrade_level(46, 0, 3)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        self.assertEqual([s["kind"] for s in stmts], ["trigger"])
        acts = stmts[0]["acts"]
        self.assertEqual(len(acts), 1)
        epd, shift = upgrade_state_slot(46, 0)
        self.assertEqual(acts[0]["dest"], epd)
        self.assertEqual(acts[0]["value"], 3 << shift)
        self.assertEqual(acts[0]["mask"], 0xFF << shift)
        self.assertEqual(acts[0]["modifier"], SetTo)


class ValidationTest(unittest.TestCase):
    def test_range_errors_name_the_documented_domains(self) -> None:
        factories: list[Callable[[], Any]] = [
            lambda: tech_state_slot(44, 0),
            lambda: tech_researched(44, 0),
            lambda: tech_researched_var(44, 0),
            lambda: set_tech_researched(44, 0, True),
            lambda: upgrade_state_slot(61, 0),
            lambda: upgrade_level(61, 0),
            lambda: set_upgrade_level(61, 0, 0),
            lambda: tech_state_slot(0, 12),
            lambda: upgrade_state_slot(0, 12),
            lambda: tech_researched(0, 12),
            lambda: tech_researched_var(0, 12),
            lambda: upgrade_level(0, 12),
            lambda: set_tech_researched(0, 12, True),
            lambda: set_upgrade_level(0, 12, 0),
            lambda: set_upgrade_level(0, 0, 256),
            lambda: set_upgrade_level(0, 0, -1),
        ]
        for factory in factories:
            with self.subTest(factory=factory):
                self.assertEqual(_error(factory).code, "EUDSL-RANGE")

    def test_type_errors_cover_bools_floats_and_wrong_operands(self) -> None:
        self.assertEqual(_error(lambda: tech_researched(True, 0)).code, "EUDSL-TYPE")
        self.assertEqual(
            _error(lambda: tech_researched_var(0, 1.0)).code, "EUDSL-TYPE"
        )
        self.assertEqual(
            _error(lambda: set_tech_researched(0, 0, 1)).code, "EUDSL-TYPE"
        )
        self.assertEqual(
            _error(lambda: set_upgrade_level(0, 0, True)).code, "EUDSL-TYPE"
        )
        self.assertEqual(
            _error(lambda: tech_researched_var(0, "p")).code, "EUDSL-TYPE"
        )

    def test_tech_researched_rejects_a_runtime_player_and_names_the_var_form(
        self,
    ) -> None:
        error = _error(lambda: tech_researched(1, Var(0)))
        self.assertEqual(error.code, "EUDSL-TYPE")
        self.assertIn("tech_researched_var", str(error))

    def test_rejections_emit_no_partial_statements(self) -> None:
        # The runtime operand is allocated in both programs: the assertion is
        # about emitted statements, not the variable table.
        def baseline() -> None:
            value = Var(0)
            value.set(1)
            player = Var(2)
            _ = player

        cases: list[Callable[[Var], Any]] = [
            lambda player: set_upgrade_level(0, 0, 256),
            lambda player: set_tech_researched(44, 0, True),
            lambda player: tech_researched(1, player),
            lambda player: upgrade_level(0, 12),
        ]
        want = build_module(baseline).to_json()
        for factory in cases:
            with self.subTest(factory=factory):

                def failing(factory: Callable[[Var], Any] = factory) -> None:
                    value = Var(0)
                    value.set(1)
                    player = Var(2)
                    try:
                        factory(player)
                    except EudslError:
                        pass

                self.assertEqual(build_module(failing).to_json(), want)


class RuntimePlayerTest(unittest.TestCase):
    def test_tech_runtime_read_binds_the_epd_and_reads_the_byte_window(
        self,
    ) -> None:
        def prog() -> None:
            player = Var(0)
            tech_researched_var(0, player)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        self.assertEqual(_cp_stores(stmts), 2, "one bind and one restore")
        masks = {
            cond["locid"]
            for stmt in stmts
            for cond in (stmt.get("conds") or [])
            if cond.get("condtype") == 15 and cond.get("locid")
        }
        # Tech strides are 0 mod 4, so the lane is the constant SC base lane.
        shift = 8 * (TECH_STATE_SC_BASE & 3)
        self.assertEqual(masks, {1 << bit for bit in range(shift, shift + 8)})
        whens = _when_nodes(stmts)
        self.assertEqual(len(whens), 1, "one 0/1 normalisation")
        self.assertEqual(whens[0]["conds"][0]["comparison"], AtLeast)
        self.assertEqual(whens[0]["conds"][0]["amount"], 1)

    def test_tech_runtime_write_is_one_masked_action_inside_with_epd(
        self,
    ) -> None:
        def prog() -> None:
            player = Var(0)
            set_tech_researched(0, player, True)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        self.assertEqual(_cp_stores(stmts), 2, "one bind and one restore")
        shift = 8 * (TECH_STATE_SC_BASE & 3)
        masked = [
            act
            for act in _act_nodes(stmts)
            if act.get("acttype") == 45 and act.get("dest") == 13
        ]
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0]["mask"], 0xFF << shift)
        self.assertEqual(masked[0]["value"], 1 << shift)

    def test_upgrade_runtime_read_dispatches_four_lane_arms(self) -> None:
        def prog() -> None:
            player = Var(0)
            upgrade_level(0, player)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        self.assertEqual(_cp_stores(stmts), 2, "one bind and one restore")
        whens = _when_nodes(stmts)
        self.assertEqual(len(whens), 4)
        self.assertEqual(
            [whens[k]["conds"][0]["amount"] for k in range(4)], [0, 1, 2, 3]
        )
        for k, arm in enumerate(whens):
            self.assertEqual(arm["conds"][0]["comparison"], Exactly)
            masks = {
                cond["locid"]
                for stmt in flatten_stmts(arm["body"])
                for cond in (stmt.get("conds") or [])
                if cond.get("locid")
            }
            self.assertEqual(
                masks,
                {1 << bit for bit in range(8 * k, 8 * k + 8)},
                f"lane {k} byte window",
            )

    def test_upgrade_runtime_write_dispatches_four_lane_arms(self) -> None:
        def prog() -> None:
            player = Var(0)
            set_upgrade_level(0, player, 7)

        stmts = build_module(prog).to_hir()["module"]["stmts"]
        self.assertEqual(_cp_stores(stmts), 2, "one bind and one restore")
        whens = _when_nodes(stmts)
        self.assertEqual(len(whens), 4)
        for k, arm in enumerate(whens):
            body = arm["body"]
            self.assertEqual([node["kind"] for node in body], ["act"])
            act = body[0]
            self.assertEqual(act["acttype"], 45)
            self.assertEqual(act["dest"], 13)  # CurrentPlayer
            self.assertEqual(act["mask"], 0xFF << (8 * k))
            self.assertEqual(act["value"], 7 << (8 * k))

    def test_readonly_var_and_expr_players_are_accepted(self) -> None:
        def prog() -> None:
            player = Var(3)
            tech_researched_var(0, ReadonlyVar(player))
            tech_researched_var(0, player.add(1))

        build_module(prog)


class SourceHygieneTest(unittest.TestCase):
    def test_no_cache_vproc_or_unmasked_memory_tokens(self) -> None:
        source = SCSTATE_SOURCE.read_text(encoding="utf-8")
        forbidden = (
            "SetNextPtr",
            "Switch",
            "Forward",
            "VProc",
            "SetDest",
            "_get_stride_cache",
            "EUDByteReader",
            "EUDLightBool",
            "SetMemory(",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_player_and_location_modules_are_not_imported(self) -> None:
        source = SCSTATE_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("from .player", source)
        self.assertNotIn("from .location", source)


class ExportSurfaceTest(unittest.TestCase):
    def test_all_matches_the_ticket_and_aliases_are_the_same_objects(self) -> None:
        import py.scstate as scstate

        expected = [
            "PlayerOperand",
            "TECH_COUNT", "TECH_SC_COUNT", "TECH_STATE_SC_BASE",
            "TECH_STATE_BW_BASE", "TECH_STATE_SC_STRIDE",
            "TECH_STATE_BW_STRIDE",
            "UPGRADE_COUNT", "UPGRADE_SC_COUNT", "UPGRADE_STATE_SC_BASE",
            "UPGRADE_STATE_BW_BASE", "UPGRADE_STATE_SC_STRIDE",
            "UPGRADE_STATE_BW_STRIDE", "MAX_PLAYER_ID",
            "tech_state_slot", "techStateSlot", "upgrade_state_slot",
            "upgradeStateSlot",
            "tech_researched", "techResearched", "tech_researched_var",
            "techResearchedVar", "upgrade_level", "upgradeLevel",
            "set_tech_researched", "setTechResearched",
            "set_upgrade_level", "setUpgradeLevel",
        ]
        self.assertEqual(scstate.__all__, expected)
        aliases = {
            "techStateSlot": scstate.tech_state_slot,
            "upgradeStateSlot": scstate.upgrade_state_slot,
            "techResearched": scstate.tech_researched,
            "techResearchedVar": scstate.tech_researched_var,
            "upgradeLevel": scstate.upgrade_level,
            "setTechResearched": scstate.set_tech_researched,
            "setUpgradeLevel": scstate.set_upgrade_level,
        }
        for alias, target in aliases.items():
            with self.subTest(alias=alias):
                self.assertIs(getattr(scstate, alias), target)

    def test_constants_match_the_ticket(self) -> None:
        self.assertEqual(TECH_COUNT, 44)
        self.assertEqual(TECH_SC_COUNT, 24)
        self.assertEqual(TECH_STATE_SC_BASE, 0x58CF44)
        self.assertEqual(TECH_STATE_BW_BASE, 0x58F140)
        self.assertEqual(TECH_STATE_SC_STRIDE, 24)
        self.assertEqual(TECH_STATE_BW_STRIDE, 20)
        self.assertEqual(UPGRADE_COUNT, 61)
        self.assertEqual(UPGRADE_SC_COUNT, 46)
        self.assertEqual(UPGRADE_STATE_SC_BASE, 0x58D2B0)
        self.assertEqual(UPGRADE_STATE_BW_BASE, 0x58F32C)
        self.assertEqual(UPGRADE_STATE_SC_STRIDE, 46)
        self.assertEqual(UPGRADE_STATE_BW_STRIDE, 15)
        self.assertEqual(MAX_PLAYER_ID, 11)


class SimulationTest(unittest.TestCase):
    """The fast path really reads and writes only the targeted byte."""

    def test_constant_round_trip_preserves_neighbour_lanes(self) -> None:
        epd = EPD(0x58CF44)
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(epd, SetTo, 0xAAAAAA00, 0)
            set_tech_researched(0, 0, True)
            out = Var(0)
            ids["out"] = out.id
            out.set(tech_researched_var(0, 0))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["out"]], 1)
        self.assertEqual(report["external_epds"][str(epd)], 0xAAAAAA01)


if __name__ == "__main__":
    unittest.main()
