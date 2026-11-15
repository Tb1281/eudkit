"""Ticket #84: Var copy method, in-place void return, and declarative action constructors.

TDD tests asserting:
- v.set(1), v.iadd(1) return None in Python.
- v += 1 keeps v as Var and increments the value.
- v.copy() creates an independent Var that copies value at runtime.
- v.act_set(1), v.act_add(1), v.act_sub(1) produce expected HirAct actions.
- MemFieldHandle in-place methods return None and act_* return HirAct.
"""

from __future__ import annotations

import unittest

from py import (
    Add,
    CUnit,
    SetTo,
    Subtract,
    Trigger,
    Var,
    build_module,
)
from py.tests.helpers import run_sim


class Ticket84Tests(unittest.TestCase):
    def test_var_inplace_methods_return_none(self):
        def prog():
            v = Var(10)
            self.assertIsNone(v.set(5))
            self.assertIsNone(v.iadd(3))
            self.assertIsNone(v.isub(2))
            self.assertIsNone(v.imul(2))
            self.assertIsNone(v.idiv(2))
            self.assertIsNone(v.imod(5))

        build_module(prog)

    def test_mem_field_handle_inplace_methods_return_none(self):
        def prog():
            u = CUnit(1000)
            self.assertIsNone(u.hp.set(500))
            self.assertIsNone(u.hp.iadd(50))
            self.assertIsNone(u.hp.isub(20))

        build_module(prog)

    def test_var_dunder_inplace_operators_keep_var_and_mutate(self):
        def prog():
            v = Var(10)
            v += 5
            self.assertIsInstance(v, Var)
            v -= 2
            self.assertIsInstance(v, Var)
            v *= 3
            self.assertIsInstance(v, Var)
            v //= 2
            self.assertIsInstance(v, Var)

        mod = build_module(prog)
        report = run_sim(mod.to_json(), 1)
        # (10 + 5 - 2) * 3 // 2 = 13 * 3 // 2 = 39 // 2 = 19
        self.assertEqual(report["var_values"][0], 19)

    def test_var_copy_creates_independent_var(self):
        def prog():
            v1 = Var(42)
            v2 = v1.copy()
            self.assertIsInstance(v2, Var)
            self.assertNotEqual(v1.id, v2.id)

            v2.iadd(10)

        mod = build_module(prog)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(report["var_values"][0], 42)
        self.assertEqual(report["var_values"][1], 52)

    def test_var_declarative_actions(self):
        def prog():
            v = Var(100)

            a_set = v.act_set(25)
            self.assertEqual(a_set["acttype"], 45)
            self.assertEqual(a_set["modifier"], SetTo)
            self.assertEqual(a_set["value"], 25)
            self.assertEqual(a_set["mask"], 0xFFFFFFFF)

            a_add = v.act_add(7)
            self.assertEqual(a_add["acttype"], 45)
            self.assertEqual(a_add["modifier"], Add)
            self.assertEqual(a_add["value"], 7)

            a_sub = v.act_sub(3)
            self.assertEqual(a_sub["acttype"], 45)
            self.assertEqual(a_sub["modifier"], Add)
            self.assertEqual(a_sub["value"], (-3) & 0xFFFFFFFF)

            Trigger(acts=[v.act_set(1), v.act_add(2), v.act_sub(1)])

        build_module(prog)

    def test_mem_field_handle_declarative_actions(self):
        def prog():
            u = CUnit(1000)

            a_set = u.hp.act_set(500)
            self.assertEqual(a_set["acttype"], 45)
            self.assertEqual(a_set["modifier"], SetTo)
            self.assertEqual(a_set["value"], 500)

            a_add = u.hp.act_add(50)
            self.assertEqual(a_add["acttype"], 45)
            self.assertEqual(a_add["modifier"], Add)
            self.assertEqual(a_add["value"], 50)

            a_sub = u.hp.act_sub(20)
            self.assertEqual(a_sub["acttype"], 45)
            self.assertEqual(a_sub["modifier"], Subtract)
            self.assertEqual(a_sub["value"], 20)

            Trigger(acts=[u.hp.act_set(100), u.hp.act_add(10), u.hp.act_sub(5)])

        build_module(prog)
