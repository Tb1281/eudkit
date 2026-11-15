"""The air-collision scrubber (issue #119): a port of euddraft's
``plugins/noAirCollision.py``.

The source's ``beforeTriggerExec`` emits 115 triggers whose 7,311 actions all
``SetMemory(dummy, SetTo, 0)``, and its ``onPluginStart`` rewrites every action
destination to the collision table: ``P, P + 1, ... P + 7,310``, where ``P`` is
the EPD read from ``0x6D5CD8``. The trigger loop then zeroes the table.

What is pinned here:

* the constants: 115 triggers, the ``64 x 114 + 15`` split (7,311 actions),
  action 0's ``dest`` at ``+86`` EPDs, the ``+8`` action stride, the inner-loop
  bounds ``504`` (63 * 8) and ``120`` (15 * 8), and the cross-trigger ``+90``
  (602 - 64 * 8);
* the emitted zeroing triggers: 115 condition-free triggers after the patch
  block, with exactly those action counts and every action a
  ``SetMemory(0x6509B0, SetTo, 0)`` placeholder;
* the patch block: one ``once`` before the zeros, the ``f_epdread_epd`` read,
  the mode-1 seed that points the scratch action at ``EPD(t[0]) + 86``, the
  variable store of the table EPD, and the nested loop whose field bumps are
  the source's ``k.AddNumber(8)`` / ``reph_epd.AddNumber(1)`` /
  ``AddNumber(90)`` with the last-trigger bound of ``120``.

The runtime proof that the patched pass really zeroes exactly ``P .. P+7310``
lives in ``crates/eudkit-lower/tests/noaircollision_parity.rs`` (the Python
frontend has no out-of-payload simulator hook here; the Rust test also pins
Python/TypeScript HIR equality).
"""

from __future__ import annotations

import json
import unittest

from py import (
    ACTION_EPD_STRIDE,
    Add,
    CROSS_TRIGGER_EPD_STEP,
    FIRST_ACTION_DEST_EPD_OFFSET,
    FULL_TRIGGER_ACTIONS,
    FULL_TRIGGER_EPD_LIMIT,
    LAST_TRIGGER_ACTIONS,
    LAST_TRIGGER_EPD_LIMIT,
    REPH_EPD_ADDR,
    TOTAL_ACTIONS,
    TRIGGER_COUNT,
    TRIGGER_EPD_STRIDE,
    SetTo,
    build_module,
    noAirCollision,
    no_air_collision,
)
from py.cp import CP_ADDR
from py.view import EPD_BASE

ACT_BASE = 328
ACT_DEST_OFFSET = 16
ACT_VALUE_OFFSET = 20
ACT_STRIDE = 32


def _module(body) -> dict:
    return json.loads(build_module(body).to_json())["module"]


def _acts(stmt: dict) -> list[dict]:
    return stmt["acts"]


class TestNoAirCollisionConstants(unittest.TestCase):
    def test_source_constants(self):
        self.assertEqual(REPH_EPD_ADDR, 0x6D5CD8)
        self.assertEqual(TRIGGER_COUNT, 115)
        self.assertEqual((FULL_TRIGGER_ACTIONS, LAST_TRIGGER_ACTIONS), (64, 15))
        self.assertEqual(TOTAL_ACTIONS, 7_311)
        self.assertEqual(TOTAL_ACTIONS, 114 * 64 + 15)

    def test_patch_addressing_matches_the_source(self):
        # Action 0's dest: 328 byte action base + 16 byte dest field.
        self.assertEqual(FIRST_ACTION_DEST_EPD_OFFSET, 86)
        self.assertEqual(
            FIRST_ACTION_DEST_EPD_OFFSET, (ACT_BASE + ACT_DEST_OFFSET) // 4
        )
        # Actions are 32 bytes = 8 EPDs apart (`k.AddNumber(8)`).
        self.assertEqual(ACTION_EPD_STRIDE, 8)
        self.assertEqual(ACTION_EPD_STRIDE, ACT_STRIDE // 4)
        # `k <= 63 * 8`, and the last trigger breaks at `15 * 8`.
        self.assertEqual(FULL_TRIGGER_EPD_LIMIT, 504)
        self.assertEqual(LAST_TRIGGER_EPD_LIMIT, 120)
        # Consecutive trigger records are 602 EPDs apart; the walk has already
        # covered 64 * 8 of that, so it skips 90.
        self.assertEqual(TRIGGER_EPD_STRIDE, 602)
        self.assertEqual(CROSS_TRIGGER_EPD_STEP, 90)
        self.assertEqual(
            CROSS_TRIGGER_EPD_STEP, TRIGGER_EPD_STRIDE - 64 * 8
        )

    def test_camel_case_alias_is_the_same_helper(self):
        self.assertIs(noAirCollision, no_air_collision)


class TestEmittedZeroingTriggers(unittest.TestCase):
    """The source's `beforeTriggerExec` triggers and their action split."""

    def setUp(self):
        self.mod = _module(noAirCollision)
        self.once = self.mod["stmts"][0]
        self.zeros = self.mod["stmts"][1:]

    def test_patch_block_is_a_once_before_the_zeroing_triggers(self):
        self.assertEqual(self.once["kind"], "once")
        self.assertEqual(self.once["conds"], [])
        self.assertEqual(len(self.zeros), TRIGGER_COUNT)
        self.assertTrue(all(stmt["kind"] == "trigger" for stmt in self.zeros))

    def test_action_split_is_64_times_114_plus_15(self):
        counts = [len(_acts(stmt)) for stmt in self.zeros]
        self.assertEqual(counts, [64] * 114 + [15])
        self.assertEqual(sum(counts), TOTAL_ACTIONS)

    def test_every_action_is_the_harmless_placeholder(self):
        for stmt in self.zeros:
            self.assertEqual(stmt["conds"], [])
            self.assertTrue(stmt["preserved"])
            for act in _acts(stmt):
                self.assertEqual(act["acttype"], 45)  # SetMemory wire form
                self.assertEqual(act["modifier"], SetTo)
                # `SetMemory` addresses memory by EPD.
                self.assertEqual(act["dest"], ((CP_ADDR - EPD_BASE) // 4) & 0xFFFFFFFF)
                self.assertEqual(act["value"], 0)
                self.assertNotIn("relocs", act)


class TestPatchBlock(unittest.TestCase):
    """`onPluginStart`: read the table pointer, seed, then walk the actions."""

    def setUp(self):
        self.mod = _module(noAirCollision)
        self.once = self.mod["stmts"][0]
        self.body = self.once["body"]
        self.zero_ids = [stmt["id"] for stmt in self.mod["stmts"][1:]]

    def _seed_trigger(self) -> dict:
        for stmt in self.body:
            if stmt["kind"] != "trigger" or len(stmt["acts"]) != 1:
                continue
            relocs = stmt["acts"][0].get("relocs", [])
            if len(relocs) == 2 and "target_trigger" in relocs[0]:
                return stmt
        self.fail("no field-seeding trigger found")

    def test_table_pointer_read_is_f_epdread_epd(self):
        # Accumulator reset, then one bit test per set bit of 0xFFFFFFFC, then
        # the single EPD base subtraction.
        reset = self.body[0]
        self.assertEqual(reset["kind"], "trigger")
        self.assertEqual(reset["acts"][0]["value"], 0)
        self.assertEqual(
            reset["acts"][0]["relocs"],
            [{"field_offset": ACT_DEST_OFFSET, "target_var": 0,
              "target_offset": ACT_VALUE_OFFSET, "mode": 1}],
        )
        bit_tests = self.body[1:31]
        self.assertEqual(len(bit_tests), 30)  # bits 2..31 of 0xFFFFFFFC
        for trigger in bit_tests:
            self.assertEqual(len(trigger["conds"]), 1)
            self.assertEqual(trigger["conds"][0]["condtype"], 15)
        subtract = self.body[31]
        self.assertEqual(subtract["kind"], "act")
        self.assertEqual(subtract["modifier"], Add)
        self.assertEqual(subtract["value"], (-(EPD_BASE // 4)) & 0xFFFFFFFF)

    def test_seed_points_the_scratch_dest_at_epd_of_first_action(self):
        seed = self._seed_trigger()
        self.assertEqual(seed["conds"], [])
        self.assertTrue(seed["preserved"])
        relocs = seed["acts"][0]["relocs"]
        # dest <- writer.act(0).dest (328 + 16), value <- EPD(t[0].act(0).dest).
        self.assertEqual(seed["acts"][0]["dest"], 0)
        self.assertEqual(seed["acts"][0]["value"], 0)
        self.assertEqual(relocs[0]["field_offset"], ACT_DEST_OFFSET)
        self.assertEqual(relocs[0]["mode"], 1)
        self.assertEqual(relocs[0]["target_offset"], ACT_BASE + ACT_DEST_OFFSET)
        self.assertEqual(relocs[1]["field_offset"], ACT_VALUE_OFFSET)
        self.assertEqual(relocs[1]["mode"], 1)
        self.assertEqual(relocs[1]["target_offset"], ACT_BASE + ACT_DEST_OFFSET)
        self.assertEqual(relocs[1]["target_trigger"], self.zero_ids[0])

    def test_table_epd_is_stored_into_the_scratch_value_field(self):
        writer_id = self._seed_trigger()["acts"][0]["relocs"][0]["target_trigger"]
        stores = [stmt for stmt in self.body if stmt["kind"] == "varstore"]
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0]["src_var"], 0)  # the table EPD variable
        self.assertEqual(stores[0]["dst_trigger"], writer_id)
        self.assertEqual(stores[0]["dst_offset"], ACT_BASE + ACT_VALUE_OFFSET)
        self.assertEqual(stores[0]["modifier"], SetTo)
        # The scratch action itself is registered inside the inner loop.
        self.assertIn(writer_id, self._inner_trigger_ids())

    def _outer(self) -> dict:
        self.assertEqual(self.body[-1]["kind"], "while")
        return self.body[-1]

    def _inner(self) -> dict:
        return self._outer()["body"][5]

    def _inner_trigger_ids(self) -> list[int]:
        return [
            stmt["id"] for stmt in self._inner()["body"] if stmt["kind"] == "trigger"
        ]

    def test_outer_loop_walks_all_115_triggers(self):
        outer = self._outer()
        self.assertEqual(outer["conds"], [
            {
                "locid": 0, "player": 0, "amount": TRIGGER_COUNT - 1,
                "unit": 0, "comparison": 1, "condtype": 15, "restype": 0,
                "flags": 0, "eudx": 0,
                "relocs": [{"field_offset": 4, "target_var": 1,
                            "target_offset": ACT_VALUE_OFFSET, "mode": 1}],
            }
        ])

    def test_outer_body_sets_the_bounds_then_steps_i_and_the_dest(self):
        body = self._outer()["body"]
        self.assertEqual([stmt["kind"] for stmt in body],
                         ["act", "when", "act", "vartransfer", "vartransfer", "while",
                          "act", "act"])
        # limit = 63 * 8
        self.assertEqual(body[0]["value"], FULL_TRIGGER_EPD_LIMIT)
        # when(i == 114): limit = 15 * 8
        self.assertEqual(body[1]["conds"][0]["amount"], TRIGGER_COUNT - 1)
        self.assertEqual(body[1]["conds"][0]["comparison"], 10)  # Exactly
        self.assertEqual(body[1]["body"][0]["value"], LAST_TRIGGER_EPD_LIMIT)
        # k = 0
        self.assertEqual(body[2]["value"], 0)
        # i += 1
        self.assertEqual(body[6]["value"], 1)
        self.assertEqual(body[6]["relocs"][0]["target_var"], 1)
        # dest += 90 (602 - 64 * 8) for the next trigger's action 0.
        self.assertEqual(body[7]["value"], CROSS_TRIGGER_EPD_STEP)
        self.assertEqual(body[7]["relocs"][0]["field_offset"], ACT_DEST_OFFSET)
        self.assertEqual(body[7]["relocs"][0]["target_offset"], ACT_BASE + ACT_DEST_OFFSET)

    def test_inner_loop_emits_the_write_then_advances_dest_and_value(self):
        inner = self._inner()
        (writer,) = [stmt for stmt in inner["body"] if stmt["kind"] == "trigger"]
        # The scratch action is `SetMemoryEPD(s[i] + k, SetTo, reph_epd)`.
        self.assertEqual(len(writer["acts"]), 1)
        self.assertEqual(writer["acts"][0]["acttype"], 45)
        self.assertEqual(writer["acts"][0]["modifier"], SetTo)
        self.assertEqual(writer["acts"][0]["value"], 0)

        # k <= limit
        self.assertEqual(inner["conds"][0]["comparison"], 10)
        # dest += 8, then value += 1, then k += 8.
        bumps = [
            stmt for stmt in inner["body"]
            if stmt["kind"] == "act"
            and stmt.get("relocs")
            and "target_trigger" in stmt["relocs"][0]
        ]
        self.assertEqual([(bump["value"], bump["relocs"][0]["target_offset"])
                          for bump in bumps],
                         [(ACTION_EPD_STRIDE, ACT_BASE + ACT_DEST_OFFSET),
                          (1, ACT_BASE + ACT_VALUE_OFFSET)])
        k_bump = inner["body"][3]
        self.assertEqual(k_bump["value"], ACTION_EPD_STRIDE)
        self.assertEqual(k_bump["relocs"][0]["target_var"], 2)


if __name__ == "__main__":
    unittest.main()
