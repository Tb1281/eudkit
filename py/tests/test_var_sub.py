"""Issue #42: arithmetic subtraction wraps mod 2^32.

StarCraft's ``Subtract`` action saturates at zero, so ``a - b``,
``.isub()``, and ``-=`` lower to wrapping addition
(``a + 1 + (0xFFFFFFFF - b)``) like eudplib. Comparison helpers keep the
saturating action.

Python mirror of ts/test_var_sub.ts.
"""

from __future__ import annotations

import unittest

from py import SetDeathsX, Subtract, Trigger, Var, build_module, when

from py.tests.helpers import run_sim
class VarSubTests(unittest.TestCase):
    def test_binary_a_minus_b_wraps(self):
        def make():
            a = Var(3)
            b = Var(5)
            z = Var(0)
            z.set(a - b)

        mod = build_module(make)
        # The trailing value is the internal complement temporary.
        self.assertEqual(
            run_sim(mod.to_json())["var_values"][:3], [3, 5, 0xFFFFFFFE]
        )

    def test_binary_zero_minus_one_wraps(self):
        def make():
            a = Var(0)
            z = Var(0)
            z.set(a - 1)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"], [0, 0xFFFFFFFF])

    def test_isub_number_wraps(self):
        def make():
            v = Var(0)
            v.isub(1)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"], [0xFFFFFFFF])

    def test_inplace_minus_equals_wraps(self):
        def make():
            v = Var(0)
            v -= 1

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"], [0xFFFFFFFF])

    def test_isub_number_supports_negative_constants(self):
        def make():
            v = Var(1)
            v.isub(-1)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"], [2])

    def test_isub_self_aliasing_subtracts_original_value(self):
        def make():
            v = Var(7)
            v.isub(v)

        mod = build_module(make)
        # The trailing value is the internal complement temporary.
        self.assertEqual(run_sim(mod.to_json())["var_values"][0], 0)

    def test_inplace_minus_equals_self_aliasing(self):
        def make():
            v = Var(7)
            v -= v

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"][0], 0)

    def test_binary_subtraction_where_dst_aliases_rhs(self):
        def make():
            a = Var(3)
            b = Var(5)
            b.set(a - b)

        mod = build_module(make)
        # Trailing values are the internal `tmp` and complement temporaries.
        self.assertEqual(
            run_sim(mod.to_json())["var_values"][:2], [3, 0xFFFFFFFE]
        )

    def test_binary_subtraction_same_var_yields_zero(self):
        def make():
            a = Var(7)
            z = Var(1)
            z.set(a - a)

        mod = build_module(make)
        # The trailing value is the internal complement temporary.
        self.assertEqual(run_sim(mod.to_json())["var_values"][:2], [7, 0])

    def test_comparison_helpers_keep_saturating_semantics_5_vs_3(self):
        def make():
            a = Var(5)
            b = Var(3)
            lt = Var(0)
            ge = Var(0)
            gt = Var(0)
            le = Var(0)
            eq = Var(0)
            ne = Var(0)
            when(a < b, lambda: lt.iadd(1))
            when(a >= b, lambda: ge.iadd(1))
            when(a > b, lambda: gt.iadd(1))
            when(a <= b, lambda: le.iadd(1))
            when(a == b, lambda: eq.iadd(1))
            when(a != b, lambda: ne.iadd(1))

        mod = build_module(make)
        # Counters are allocated before the comparison temporaries.
        counters = run_sim(mod.to_json())["var_values"][2:8]
        # Wrapping subtraction would flip lt (true) and ge (false).
        self.assertEqual(counters, [0, 1, 1, 0, 0, 1])

    def test_comparison_helpers_keep_saturating_semantics_3_vs_5(self):
        def make():
            a = Var(3)
            b = Var(5)
            lt = Var(0)
            ge = Var(0)
            gt = Var(0)
            le = Var(0)
            eq = Var(0)
            ne = Var(0)
            when(a < b, lambda: lt.iadd(1))
            when(a >= b, lambda: ge.iadd(1))
            when(a > b, lambda: gt.iadd(1))
            when(a <= b, lambda: le.iadd(1))
            when(a == b, lambda: eq.iadd(1))
            when(a != b, lambda: ne.iadd(1))

        mod = build_module(make)
        counters = run_sim(mod.to_json())["var_values"][2:8]
        self.assertEqual(counters, [1, 0, 0, 1, 0, 1])

    def test_simulator_still_models_raw_subtract_as_saturating(self):
        def make():
            v = Var(0)
            Trigger(
                conds=[],
                acts=[SetDeathsX(v.value, Subtract, 1, 0, 0xFFFFFFFF)],
            )

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json())["var_values"], [0])

if __name__ == "__main__":
    unittest.main()
