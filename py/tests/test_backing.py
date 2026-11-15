"""Backing-level tests for container indexing arithmetic.

These tests cover the pure arithmetic that the backing module consolidates:
carry-free page/slot weights, constant EPD decomposition, element masks, and
the runtime PageSlotEPD address. They run against the public surfaces first
and are re-pointed at the backing module after migration.
"""

from __future__ import annotations

import unittest

from py import FixedArray, Queue, Var, build_module
from py.array import pageSlotEpd as pageSlotEpdViaArray
from py.array import pageSlotEpdWeight as pageSlotEpdWeightViaArray
from py.backing import (
    PageSlotEPD,
    pageSlotBitCond,
    pageSlotEpd,
    pageSlotEpdWeight,
)
from py.trigger import pageSlotEpdWeight as pageSlotEpdWeightViaTrigger
from py.view import EudslError

from py.tests.helpers import run_sim
class BackingTests(unittest.TestCase):
    def test_backing_exports_are_reexported_unchanged(self):
        self.assertIs(pageSlotEpdViaArray, pageSlotEpd)
        self.assertIs(pageSlotEpdWeightViaArray, pageSlotEpdWeight)
        self.assertIs(pageSlotEpdWeightViaTrigger, pageSlotEpdWeight)

    def test_weights_are_carry_free_for_bits_0_to_6(self):
        expected = [8, 16, 32, 64, 128, 256, 602]
        for bit in range(7):
            self.assertEqual(pageSlotEpdWeight(bit), expected[bit])
        self.assertEqual(pageSlotEpdWeight(7), 1204)

    def test_weights_follow_the_weight_formula_for_bits_0_to_28(self):
        for bit in range(6):
            self.assertEqual(pageSlotEpdWeight(bit), 8 << bit)
        for bit in range(6, 29):
            self.assertEqual(pageSlotEpdWeight(bit), (602 << (bit - 6)) & 0xFFFFFFFF)

    def test_weights_reject_out_of_range_bits(self):
        for bad in (32, -1, 1.5, True):
            with self.assertRaises(EudslError):
                pageSlotEpdWeight(bad)

    def test_page_slot_epd_pinned_values(self):
        self.assertEqual(pageSlotEpd(1000, 0), 1000)
        self.assertEqual(pageSlotEpd(1000, 1), 1008)
        self.assertEqual(pageSlotEpd(1000, 63), 1504)
        self.assertEqual(pageSlotEpd(1000, 64), 1602)
        self.assertEqual(pageSlotEpd(1000, 65), 1610)
        self.assertEqual(pageSlotEpd(0, 65), 610)
        self.assertEqual(pageSlotEpd(0, 128), 1204)

    def test_page_slot_epd_rejects_invalid_indices(self):
        for bad in (-1, 1.5, True):
            with self.assertRaises(EudslError):
                pageSlotEpd(1000, bad)

    def test_page_slot_epd_decomposes_into_page_plus_slot_stride(self):
        for index in range(1024):
            expected = ((index >> 6) * 602 + (index & 63) * 8) & 0xFFFFFFFF
            self.assertEqual(pageSlotEpd(0, index), expected)
            self.assertEqual(pageSlotEpd(1000, index), (1000 + expected) & 0xFFFFFFFF)

    def test_element_masks_from_fixedarray_mask_surface(self):
        def build():
            self.assertEqual(FixedArray("byte", 4).mask, 0xFF)
            self.assertEqual(FixedArray("word", 4).mask, 0xFFFF)
            self.assertEqual(FixedArray("dword", 4).mask, 0xFFFFFFFF)
            self.assertEqual(FixedArray("var", 4).mask, 0xFFFFFFFF)
            self.assertEqual(FixedArray("dword", 64).pageCount, 1)
            self.assertEqual(FixedArray("dword", 65).pageCount, 2)

        build_module(build)

    def test_element_masks_from_queue_out_box_surface(self):
        def build():
            self.assertEqual(Queue("byte", 4).outBox.bitmask, 0xFF)
            self.assertEqual(Queue("word", 4).outBox.bitmask, 0xFFFF)
            self.assertEqual(Queue("dword", 4).outBox.bitmask, 0xFFFFFFFF)
            self.assertEqual(Queue("var", 4).outBox.bitmask, 0xFFFFFFFF)

        build_module(build)

    def test_page_slot_bit_cond_is_callable(self):
        # Behavioural coverage is in the PageSlotEPD runtime test below;
        # pinning HirCond field layout is outside the backing seam.
        self.assertTrue(callable(pageSlotBitCond))

    def test_page_slot_epd_runtime_address_accumulates_set_bit_weights(self):
        captured: dict[str, int] = {}

        def build():
            base = Var(100)
            index = Var(65)
            epd = PageSlotEPD(base, index, 7)
            out = Var(0)
            out.set(epd)
            captured["out"] = out.id

        mod = build_module(build)
        report = run_sim(mod.to_json(), 2)
        # base 100 + page 1 (602) + slot 1 (8) = 710.
        self.assertEqual(report["var_values"][captured["out"]], 710)

if __name__ == "__main__":
    unittest.main()
