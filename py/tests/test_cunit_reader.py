"""T-WP6: the 20T CUnit pointer reader.

The reader folds the CUnit pool geometry into 18 bit tests of the dword that
holds a CUnit byte pointer: the shifted pointer space ``S = A >> 2`` has
invariant bits 0, 1 and 20, so only ``S`` bits 2-19 (address bits 4-21) need a
test. The literal form is exactly 1 seeded reset + 18 tests + 1 bias = 20T; the
shared CP-relative body is declared once per module; and the loop form never
reads the ambient CurrentPlayer value.

These tests pin the corrected constants (``CUNIT_LAST_EPD`` is
``19025 + 84 * 1698 = 161657``, **not** ``20723``), the exact HIR shape of all
three forms, the null sentinel rejected by the *upper* bound of
``in_cunit_pool``, and the two ``EUDSL-TYPE`` guards.
"""

from __future__ import annotations

import json
import unittest

from py import Var, build_module
from py.hir import Add, AtLeast, AtMost, SetTo
from py.tests.helpers import flatten_stmts
from py.view import (
    CURRENT_PLAYER,
    CUNIT_BASE_EPD,
    CUNIT_LAST_EPD,
    CUNIT_POOL_BASE,
    CUNIT_POOL_END,
    CUNIT_SLOT_COUNT,
    CUNIT_SLOT_EPDS,
    CUNIT_SLOT_SIZE,
    EPD,
    EudslError,
    in_cunit_pool,
    read_cunit_pointer,
    read_cunit_pointer_saved,
)

SET_DEATHS = 45
_LIST_HEAD = EPD(0x628430)

_NULL_SENTINEL = (0x100002 + 0xFFE9D727) & 0xFFFFFFFF


def _module(prog) -> dict:
    return json.loads(build_module(prog).to_json())["module"]


def _triggers(module: dict) -> list[dict]:
    return [s for s in module["stmts"] if s["kind"] == "trigger"]


def _all_conds(module: dict) -> list[dict]:
    return [
        cond
        for stmt in flatten_stmts(module["stmts"])
        for cond in (stmt.get("conds") or [])
    ]


class CunitConstantsTest(unittest.TestCase):
    def test_pool_constants_and_derived_epd_bound(self) -> None:
        self.assertEqual(CUNIT_POOL_BASE, 0x59CCA8)
        self.assertEqual(CUNIT_SLOT_SIZE, 336)
        self.assertEqual(CUNIT_SLOT_EPDS, CUNIT_SLOT_SIZE // 4)
        self.assertEqual(CUNIT_SLOT_COUNT, 1699)
        self.assertEqual(CUNIT_POOL_END, 0x628297)
        self.assertEqual(
            CUNIT_POOL_END,
            CUNIT_POOL_BASE + CUNIT_SLOT_SIZE * CUNIT_SLOT_COUNT - 1,
        )

        self.assertEqual(CUNIT_BASE_EPD, 19025)
        self.assertEqual(EPD(CUNIT_POOL_BASE), CUNIT_BASE_EPD)
        self.assertEqual(CUNIT_LAST_EPD, 161657)
        self.assertEqual(EPD(0x628148), CUNIT_LAST_EPD)
        self.assertEqual(
            CUNIT_LAST_EPD,
            CUNIT_BASE_EPD + CUNIT_SLOT_EPDS * (CUNIT_SLOT_COUNT - 1),
        )
        # The naive slot-count bound is the bug this constant must never
        # regress to: it rejects every real unit after the 21st slot.
        self.assertNotEqual(CUNIT_LAST_EPD, CUNIT_BASE_EPD + CUNIT_SLOT_COUNT - 1)

    def test_null_sentinel_is_above_the_upper_bound(self) -> None:
        self.assertEqual(_NULL_SENTINEL, 0xFFF9D729)
        self.assertGreaterEqual(_NULL_SENTINEL, CUNIT_BASE_EPD)
        self.assertGreater(_NULL_SENTINEL, CUNIT_LAST_EPD)


class LiteralFormTest(unittest.TestCase):
    def test_literal_form_is_exactly_20_triggers_and_18_conditions(self) -> None:
        module = _module(lambda: read_cunit_pointer(_LIST_HEAD))
        triggers = _triggers(module)

        self.assertEqual(len(triggers), 20)
        self.assertEqual(sum(len(t["conds"]) for t in triggers), 18)

        seed = triggers[0]
        self.assertEqual(seed["conds"], [])
        self.assertEqual(len(seed["acts"]), 1)
        self.assertEqual(seed["acts"][0]["acttype"], SET_DEATHS)
        self.assertEqual(seed["acts"][0]["value"], 0x100002)
        self.assertEqual(seed["acts"][0]["modifier"], SetTo)

        bias = triggers[-1]
        self.assertEqual(bias["conds"], [])
        self.assertEqual(bias["acts"][0]["value"], 0xFFE9D727)
        self.assertEqual(bias["acts"][0]["modifier"], Add)

    def test_literal_form_tests_address_bits_4_to_21_in_order(self) -> None:
        module = _module(lambda: read_cunit_pointer(_LIST_HEAD))
        triggers = _triggers(module)
        result_id = triggers[0]["acts"][0]["relocs"][0]["target_var"]

        for index, source_bit in enumerate(range(4, 22)):
            trigger = triggers[index + 1]
            self.assertEqual(len(trigger["conds"]), 1)
            cond = trigger["conds"][0]
            self.assertEqual(cond["condtype"], 15)
            self.assertEqual(cond["player"], _LIST_HEAD)
            self.assertEqual(cond["comparison"], AtLeast)
            self.assertEqual(cond["amount"], 1 << source_bit)

            act = trigger["acts"][0]
            self.assertEqual(act["acttype"], SET_DEATHS)
            self.assertEqual(act["modifier"], Add)
            self.assertEqual(act["value"], 1 << (source_bit - 2))
            self.assertEqual(act["mask"], 0xFFFFFFFF)
            self.assertEqual(act["relocs"][0]["target_var"], result_id)

    def test_literal_form_has_no_leading_zero_reset(self) -> None:
        # The seeded reset is the first act; a `res.set(0)` before it is the
        # exact 1T regression this ticket removes.
        triggers = _triggers(_module(lambda: read_cunit_pointer(_LIST_HEAD)))
        self.assertEqual(triggers[0]["acts"][0]["value"], 0x100002)
        self.assertNotEqual(triggers[0]["acts"][0]["value"], 0)


class RuntimeFormTest(unittest.TestCase):
    def test_runtime_form_emits_ambient_read_store_call_and_restore(self) -> None:
        module = _module(lambda: read_cunit_pointer(Var(0)))
        kinds = [stmt["kind"] for stmt in module["stmts"]]
        self.assertEqual(
            kinds,
            ["vartransfer", "call", "varstore", "call", "varstore"],
        )
        # Two distinct bodies: the 33-stmt ambient dword reader and the 20-stmt
        # CP-relative CUnit reader.
        self.assertEqual(len(module["funcs"]), 2)
        self.assertEqual(
            sorted(len(fn["body"]) for fn in module["funcs"]), [20, 33]
        )

    def test_runtime_form_declares_the_cunit_body_once_for_two_sites(self) -> None:
        def prog() -> None:
            pointer = Var(0)
            out1 = Var(0)
            out2 = Var(0)
            out1.set(read_cunit_pointer(pointer))
            out2.set(read_cunit_pointer(pointer))

        module = _module(prog)
        self.assertEqual(len(module["funcs"]), 2)
        cunit_body = next(
            fn for fn in module["funcs"] if len(fn["body"]) == 20
        )
        conds = [
            cond
            for stmt in cunit_body["body"]
            for cond in (stmt.get("conds") or [])
        ]
        self.assertEqual(len(conds), 18)
        self.assertTrue(all(cond["player"] == CURRENT_PLAYER for cond in conds))
        self.assertEqual(cunit_body["body"][0]["acts"][0]["value"], 0x100002)
        self.assertEqual(cunit_body["body"][-1]["acts"][0]["value"], 0xFFE9D727)

        # Two sites, each: ambient call + CUnit call; bind + restore stores.
        self.assertEqual(
            len([s for s in module["stmts"] if s["kind"] == "call"]), 4
        )
        self.assertEqual(
            len([s for s in module["stmts"] if s["kind"] == "varstore"]), 4
        )


class SavedFormTest(unittest.TestCase):
    def test_saved_form_never_reads_the_ambient_cp(self) -> None:
        def prog() -> None:
            saved = Var(0)
            out = Var(0)
            out.set(read_cunit_pointer_saved(saved, _LIST_HEAD))

        module = _module(prog)
        # Only the CUnit body is declared: no 33-stmt ambient dword reader.
        self.assertEqual(len(module["funcs"]), 1)
        self.assertEqual(len(module["funcs"][0]["body"]), 20)

        kinds = [stmt["kind"] for stmt in module["stmts"]]
        self.assertEqual(
            kinds, ["varstore", "call", "varstore", "vartransfer"]
        )
        stores = [s for s in module["stmts"] if s["kind"] == "varstore"]
        self.assertEqual(len(stores), 2)
        self.assertTrue(all(s["dst_addr"] == 0x6509B0 for s in stores))
        self.assertEqual(stores[1]["src_var"], 0)  # the caller's saved Var

    def test_saved_form_accepts_an_expr_pointer(self) -> None:
        def prog() -> None:
            saved = Var(0)
            pointer = Var(_LIST_HEAD)
            read_cunit_pointer_saved(saved, pointer.add(1))

        module = _module(prog)
        self.assertEqual(len([s for s in module["stmts"] if s["kind"] == "call"]), 1)
        self.assertEqual(len(module["funcs"]), 1)

    def test_saved_cp_must_be_a_var(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: read_cunit_pointer_saved(5, _LIST_HEAD))
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_bool_target_is_rejected(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: read_cunit_pointer(True))
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")


class InCunitPoolTest(unittest.TestCase):
    def test_returns_exactly_two_splicable_conditions(self) -> None:
        captured: dict[str, list] = {}

        def prog() -> None:
            ptr = Var(0)
            captured["gate"] = in_cunit_pool(ptr)

        module = _module(prog)
        gate = captured["gate"]
        self.assertEqual(len(gate), 2)
        self.assertEqual([c["amount"] for c in gate], [CUNIT_BASE_EPD, CUNIT_LAST_EPD])
        self.assertEqual(
            [c["comparison"] for c in gate], [AtLeast, AtMost]
        )
        # A predicate, not a trigger: it never wraps the conditions.
        self.assertEqual(module["stmts"], [])


if __name__ == "__main__":
    unittest.main()
