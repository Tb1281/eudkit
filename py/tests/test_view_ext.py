"""T-ABS-VIEW-POOL: raw bit helpers, flag vocabularies, and `CUnitView`.

The tests pin the HIR shape and the cost ceilings from §5 of the ticket. Every
runtime-EPD assertion is HIR-level: the native simulator proof lives in
``crates/eudkit-cli/tests/view_pool_runtime.rs``.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Callable

from py import EPD, Var, build_module
from py.queue import POOL_NULL
from py.tests.helpers import flatten_acts, flatten_stmts, run_sim, run_static
from py.view import (
    AvailabilityFlags,
    CUnitView,
    EudslError,
    GroupFlags,
    PathingFlags,
    StatusFlags,
    read_bit,
    read_bit_var,
    read_cunit_pointer,
    split_dword,
    split_word,
    write_bit,
    write_mask,
)

BASE = EPD(0x59CCA8)
ADDR = 0x58DC4C
LOCATION_EPD = EPD(0x58DC4C)


def _module(fn: Callable[[], Any]) -> dict[str, Any]:
    return json.loads(build_module(fn).to_json())["module"]


def _stmts(fn: Callable[[], Any]) -> list[dict[str, Any]]:
    return flatten_stmts(_module(fn)["stmts"])


def _acts(fn: Callable[[], Any]) -> list[dict[str, Any]]:
    return flatten_acts(_module(fn)["stmts"])


def _code(exc: BaseException) -> str:
    return getattr(exc, "code", "")


class SplitTests(unittest.TestCase):
    def test_split_dword_constant_folds_to_two_constant_vars(self) -> None:
        out: dict[str, Any] = {}

        def prog() -> None:
            out["hi"], out["lo"] = split_dword(0x12345678)

        module = _module(prog)
        self.assertEqual(out["hi"].init, 0x1234)
        self.assertEqual(out["lo"].init, 0x5678)
        self.assertEqual(module["stmts"], [])

    def test_split_word_splits_a_word_into_two_bytes(self) -> None:
        out: dict[str, Any] = {}

        def prog() -> None:
            out["hi"], out["lo"] = split_word(0x1234)

        _module(prog)
        self.assertEqual(out["hi"].init, 0x12)
        self.assertEqual(out["lo"].init, 0x34)

    def test_split_runtime_is_under_the_ceiling(self) -> None:
        for split in (split_dword, split_word):
            with self.subTest(split=split.__name__):
                module = _module(lambda: split(Var(0)))
                self.assertGreater(len(module["stmts"]), 0)
                self.assertLessEqual(len(module["stmts"]), 48)

    def test_split_rejects_bool(self) -> None:
        for split in (split_dword, split_word):
            with self.subTest(split=split.__name__):
                with self.assertRaises(EudslError) as caught:
                    _module(lambda: split(True))
                self.assertEqual(_code(caught.exception), "EUDSL-TYPE")


class BitHelperTests(unittest.TestCase):
    def test_read_bit_is_one_condition_and_zero_acts(self) -> None:
        from py import when

        def prog() -> None:
            when(read_bit(ADDR, 0), lambda: None)

        module = _module(prog)
        self.assertEqual(len(module["stmts"]), 1)
        cond = module["stmts"][0]["conds"][0]
        self.assertEqual(cond["condtype"], 15)
        self.assertEqual(cond["player"], EPD(ADDR))
        self.assertEqual(cond["amount"], 1)
        self.assertEqual(cond["locid"], 1)
        self.assertEqual(module["stmts"][0]["body"], [])

    def test_read_bit_bit_31_uses_the_top_mask(self) -> None:
        from py import when

        def prog() -> None:
            when(read_bit(ADDR, 31), lambda: None)

        cond = _module(prog)["stmts"][0]["conds"][0]
        self.assertEqual(cond["locid"], 0x80000000)
        self.assertEqual(cond["amount"], 0x80000000)

    def test_read_bit_address_and_index_guards(self) -> None:
        with self.assertRaises(EudslError) as caught:
            _module(lambda: read_bit(ADDR + 1, 0))
        self.assertEqual(_code(caught.exception), "EUDSL-BOUNDS")

        for bit in (32, -1):
            with self.subTest(bit=bit):
                with self.assertRaises(EudslError) as caught:
                    _module(lambda: read_bit(ADDR, bit))
                self.assertEqual(_code(caught.exception), "EUDSL-RANGE")

        with self.assertRaises(EudslError) as caught:
            _module(lambda: read_bit(ADDR, True))
        self.assertEqual(_code(caught.exception), "EUDSL-TYPE")

    def test_read_bit_var_is_two_triggers(self) -> None:
        module = _module(lambda: read_bit_var(ADDR, 4))
        self.assertEqual(len(module["stmts"]), 2)
        cond = module["stmts"][1]["conds"][0]
        self.assertEqual(cond["locid"], 0x10)
        self.assertEqual(cond["player"], EPD(ADDR))

    def test_write_bit_emits_one_masked_act(self) -> None:
        acts = _acts(lambda: write_bit(ADDR, 4, True))
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["dest"], EPD(ADDR))
        self.assertEqual(acts[0]["value"], 0x10)
        self.assertEqual(acts[0]["mask"], 0x10)
        self.assertEqual(acts[0]["modifier"], 7)

        acts = _acts(lambda: write_bit(ADDR, 4, False))
        self.assertEqual(acts[0]["value"], 0)

    def test_write_bit_rejects_non_bool(self) -> None:
        with self.assertRaises(EudslError) as caught:
            _module(lambda: write_bit(ADDR, 4, 1))
        self.assertEqual(_code(caught.exception), "EUDSL-TYPE")

    def test_write_mask_validates_mask_and_value(self) -> None:
        acts = _acts(lambda: write_mask(ADDR, 0x1200, 0xFF00))
        self.assertEqual(acts[0]["value"], 0x1200)
        self.assertEqual(acts[0]["mask"], 0xFF00)

        with self.assertRaises(EudslError) as caught:
            _module(lambda: write_mask(ADDR, 0x12, 0xFF00))
        self.assertEqual(_code(caught.exception), "EUDSL-BOUNDS")

        with self.assertRaises(EudslError) as caught:
            _module(lambda: write_mask(ADDR, 0, 0))
        self.assertEqual(_code(caught.exception), "EUDSL-RANGE")


class FlagTests(unittest.TestCase):
    def test_status_flags_are_distinct_single_bits(self) -> None:
        values = {
            name: value
            for name, value in vars(StatusFlags).items()
            if not name.startswith("_")
        }
        self.assertEqual(len(values), 32)
        self.assertTrue(all(value & (value - 1) == 0 for value in values.values()))
        self.assertEqual(len(set(values.values())), 32)

    def test_pathing_flags(self) -> None:
        self.assertEqual(PathingFlags.HasCollision, 0x01)
        self.assertEqual(PathingFlags.IsStacked, 0x02)
        self.assertEqual(PathingFlags.Decollide, 0x04)

    def test_unit_type_flag_tables(self) -> None:
        self.assertEqual(
            [GroupFlags.Zerg, GroupFlags.Terran, GroupFlags.Protoss, GroupFlags.Men,
             GroupFlags.Building, GroupFlags.Factory, GroupFlags.Independent,
             GroupFlags.Neutral],
            [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80],
        )
        self.assertEqual(
            [AvailabilityFlags.NonNeutral, AvailabilityFlags.UnitListing,
             AvailabilityFlags.MissionBriefing, AvailabilityFlags.PlayerSettings,
             AvailabilityFlags.AllRaces, AvailabilityFlags.SetDoodadState,
             AvailabilityFlags.NonLocationTriggers, AvailabilityFlags.UnitHeroSettings,
             AvailabilityFlags.LocationTriggers, AvailabilityFlags.BroodWarOnly],
            [0x001, 0x002, 0x004, 0x008, 0x010, 0x020, 0x040, 0x080, 0x100, 0x200],
        )

    def test_group_flags_are_unit_type_data(self) -> None:
        from py import view

        self.assertEqual(view.GROUP_FLAGS_TARGET, "unit-type")
        self.assertIn("unit-type", GroupFlags.__doc__ or "")
        self.assertIn("not", (GroupFlags.__doc__ or "").lower())


class CUnitViewConstantTests(unittest.TestCase):
    def test_mem_and_field_resolution(self) -> None:
        out: dict[str, Any] = {}

        def prog() -> None:
            out["u"] = CUnitView(BASE)
            out["owner"] = out["u"].field("owner")
            out["status"] = out["u"].field("statusFlags")

        _module(prog)
        self.assertEqual(out["u"].epd, BASE)
        self.assertEqual(out["u"].mem.schema.name, "CUnit")
        self.assertEqual(out["owner"].epd, BASE + 19)
        self.assertEqual(out["status"].epd, BASE + 55)

    def test_read_by_name_matches_the_field_handle(self) -> None:
        for name in ("hp", "owner", "statusFlags"):
            with self.subTest(name=name):
                self.assertEqual(
                    build_module(
                        lambda name=name: CUnitView(BASE).read(name)
                    ).to_json(),
                    build_module(
                        lambda name=name: CUnitView(BASE).field(name).read()
                    ).to_json(),
                )

    def test_unknown_field(self) -> None:
        def prog() -> None:
            CUnitView(BASE).field("nope")

        with self.assertRaises(EudslError) as caught:
            _module(prog)
        self.assertEqual(_code(caught.exception), "EUDSL-UNKNOWN-FIELD")

    def test_give_to_writes_only_the_owner_byte(self) -> None:
        acts = _acts(lambda: CUnitView(BASE).give_to(3))
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["dest"], BASE + 19)
        self.assertEqual(acts[0]["value"], 3)
        self.assertEqual(acts[0]["mask"], 0xFF)

    def test_die_and_remove_act_order(self) -> None:
        die = _acts(lambda: CUnitView(BASE).die())
        self.assertEqual(len(die), 1)
        self.assertEqual(
            (die[0]["dest"], die[0]["value"], die[0]["mask"]),
            (BASE + 19, 0, 0xFF00),
        )

        remove = _acts(lambda: CUnitView(BASE).remove())
        self.assertEqual(len(remove), 2)
        self.assertEqual(
            (remove[0]["dest"], remove[0]["value"], remove[0]["mask"]),
            (BASE + 36, 0x04000000, 0xFF000000),
        )
        self.assertEqual(
            (remove[1]["dest"], remove[1]["value"], remove[1]["mask"]),
            (BASE + 19, 0, 0xFF00),
        )

    def test_set_invincible(self) -> None:
        acts = _acts(lambda: CUnitView(BASE).set_invincible())
        self.assertEqual(
            (acts[0]["dest"], acts[0]["value"], acts[0]["mask"]),
            (BASE + 55, 0x04000000, 0x04000000),
        )
        acts = _acts(lambda: CUnitView(BASE).set_invincible(False))
        self.assertEqual(acts[0]["value"], 0)

    def test_is_burrowed_reads_bit_four_of_status_flags(self) -> None:
        module = _module(lambda: CUnitView(BASE).is_burrowed())
        self.assertEqual(len(module["stmts"]), 2)
        cond = module["stmts"][1]["conds"][0]
        self.assertEqual(cond["player"], BASE + 55)
        self.assertEqual(cond["locid"], 0x10)

    def test_links_use_the_reader_at_the_expected_dword(self) -> None:
        self.assertEqual(
            build_module(lambda: CUnitView(BASE).next_unit()).to_json(),
            build_module(lambda: read_cunit_pointer(BASE + 1)).to_json(),
        )
        self.assertEqual(
            build_module(lambda: CUnitView(BASE).prev_unit()).to_json(),
            build_module(lambda: read_cunit_pointer(BASE)).to_json(),
        )

    def test_sprite_epd_uses_the_pointer_reader_at_dword_three(self) -> None:
        from py.view import read_epd_pointer

        self.assertEqual(
            build_module(lambda: CUnitView(BASE).sprite_epd()).to_json(),
            build_module(lambda: read_epd_pointer(BASE + 3)).to_json(),
        )

    def test_set_color_reads_sprite_and_writes_the_player_lane(self) -> None:
        acts = _acts(lambda: CUnitView(BASE).set_color(5))
        masked = [act for act in acts if act["mask"] == 0x00FF0000]
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0]["value"], 5 << 16)

    def test_validation_guards(self) -> None:
        for call, code in (
            (lambda: CUnitView(BASE).give_to(12), "EUDSL-RANGE"),
            (lambda: CUnitView(BASE).give_to(True), "EUDSL-TYPE"),
            (lambda: CUnitView(BASE).set_color(-1), "EUDSL-RANGE"),
            (lambda: CUnitView(BASE).set_location(256), "EUDSL-RANGE"),
            (lambda: CUnitView(BASE).set_location(True), "EUDSL-TYPE"),
            (lambda: CUnitView(True), "EUDSL-TYPE"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(EudslError) as caught:
                    _module(call)
                self.assertEqual(_code(caught.exception), code)


class CUnitViewRuntimeTests(unittest.TestCase):
    def test_read_at_runtime_epd_uses_cp_with_the_field_window(self) -> None:
        module = _module(lambda: CUnitView(Var(BASE)).read("owner"))
        conditions = [
            cond
            for stmt in flatten_stmts(module["stmts"])
            for cond in stmt.get("conds", [])
        ]
        self.assertEqual(
            {
                cond["locid"]
                for cond in conditions
                if cond["player"] == 13 and cond["locid"]
            },
            {1 << bit for bit in range(8)},
        )

        full = _module(lambda: CUnitView(Var(BASE)).read("hp"))
        full_conditions = [
            cond
            for func in full.get("funcs", [])
            for stmt in flatten_stmts(func["body"])
            for cond in stmt.get("conds", [])
        ]
        self.assertTrue(
            any(cond["player"] == 13 for cond in full_conditions),
            "the full-dword read must gate on the CP-bound dword",
        )

    def test_runtime_masked_write_uses_a_pointer_and_varstore(self) -> None:
        module = _module(lambda: CUnitView(Var(BASE)).give_to(2))
        statements = flatten_stmts(module["stmts"])
        varstores = [s for s in statements if s["kind"] == "varstore"]
        self.assertTrue(varstores)
        self.assertEqual(varstores[0]["dst_offset"], 344)
        acts = flatten_acts(module["stmts"])
        masked = [a for a in acts if a["mask"] == 0xFF]
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0]["value"], 2)

    def test_runtime_is_burrowed_binds_cp_and_stays_under_ceiling(self) -> None:
        module = _module(lambda: CUnitView(Var(BASE)).is_burrowed())
        conditions = [
            cond
            for stmt in flatten_stmts(module["stmts"])
            for cond in stmt.get("conds", [])
        ]
        self.assertTrue(any(cond["player"] == 13 for cond in conditions))
        static = run_static(build_module(lambda: CUnitView(Var(BASE)).is_burrowed()).to_json())
        executed = run_sim(
            build_module(lambda: CUnitView(Var(BASE)).is_burrowed()).to_json()
        )
        self.assertLessEqual(static["static_cost"]["triggers"], 80)
        self.assertLessEqual(executed["executed_cost"]["triggers"], 70)

    def test_runtime_set_location_reads_the_record_and_fills_pos(self) -> None:
        module = _module(lambda: CUnitView(Var(BASE)).set_location(7))
        function_conds = [
            cond
            for func in module.get("funcs", [])
            for stmt in flatten_stmts(func["body"])
            for cond in stmt.get("conds", [])
        ]
        players = {cond["player"] for cond in function_conds}
        self.assertIn(LOCATION_EPD + 7 * 5, players)
        self.assertIn(LOCATION_EPD + 7 * 5 + 1, players)
        self.assertEqual(
            sum(1 for stmt in flatten_stmts(module["stmts"]) if stmt["kind"] == "while"),
            1,
        )

    def test_runtime_costs_are_under_the_ceilings(self) -> None:
        sites = (
            (lambda: CUnitView(Var(BASE)).give_to(2), 40),
            (lambda: CUnitView(Var(BASE)).die(), 40),
            (lambda: CUnitView(Var(BASE)).set_invincible(), 40),
            (lambda: CUnitView(Var(BASE)).set_location(7), 180),
        )
        for fn, ceiling in sites:
            with self.subTest(ceiling=ceiling):
                module = build_module(fn).to_json()
                self.assertLessEqual(
                    run_static(module)["static_cost"]["triggers"], ceiling
                )
                self.assertLessEqual(
                    run_sim(module)["executed_cost"]["triggers"], ceiling
                )

    def test_constant_reader_costs_stay_pinned(self) -> None:
        self.assertEqual(len(_module(lambda: CUnitView(BASE).next_unit())["stmts"]), 20)
        self.assertEqual(len(_module(lambda: CUnitView(BASE).prev_unit())["stmts"]), 20)
        self.assertEqual(len(_module(lambda: CUnitView(BASE).sprite_epd())["stmts"]), 32)
        self.assertEqual(len(_module(lambda: CUnitView(BASE).is_burrowed())["stmts"]), 2)


class CUnitViewAliasTests(unittest.TestCase):
    def test_camel_case_methods_are_the_snake_case_twins(self) -> None:
        for camel, snake in (
            ("nextUnit", "next_unit"),
            ("prevUnit", "prev_unit"),
            ("spriteEpd", "sprite_epd"),
            ("isBurrowed", "is_burrowed"),
            ("setInvincible", "set_invincible"),
            ("giveTo", "give_to"),
            ("setColor", "set_color"),
            ("setLocation", "set_location"),
        ):
            with self.subTest(camel=camel):
                self.assertIs(
                    getattr(CUnitView, camel), getattr(CUnitView, snake)
                )


class PoolNullTests(unittest.TestCase):
    def test_pool_null_is_shared(self) -> None:
        from py import queue, view

        self.assertEqual(POOL_NULL, 0xFFFFFFFF)
        self.assertIs(queue.POOL_NULL, view.POOL_NULL)


if __name__ == "__main__":
    unittest.main()
