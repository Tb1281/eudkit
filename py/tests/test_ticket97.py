"""Ticket #97: ReadonlyVar must be a trustworthy read-only boundary."""

import operator
import unittest

from py import (
    EudslError,
    Expr,
    FixedString,
    ReadonlyVar,
    Var,
    as_readonly,
    build_module,
    lazy_var,
    when,
)
from py.tests.helpers import run_sim

MUTATION_METHODS = [
    "set",
    "iadd",
    "isub",
    "imul",
    "idiv",
    "imod",
    "iand",
    "ior",
    "ixor",
    "ishl",
    "ishr",
    "act_set",
    "act_add",
    "act_sub",
]

IN_PLACE_OPERATORS = [
    ("+=", operator.iadd),
    ("-=", operator.isub),
    ("*=", operator.imul),
    ("/=", operator.itruediv),
    ("//=", operator.ifloordiv),
    ("%=", operator.imod),
    ("&=", operator.iand),
    ("|=", operator.ior),
    ("^=", operator.ixor),
    ("<<=", operator.ilshift),
    (">>=", operator.irshift),
]

FIELD_REFS_AND_COPY = ["value", "dest", "mask", "modifier", "copy"]


class Ticket97ReadonlyContractTest(unittest.TestCase):
    def assert_readonly_error(self, operation: str, thunk) -> None:
        with self.assertRaises(EudslError) as captured:
            thunk()
        self.assertEqual(captured.exception.code, "EUDSL-READONLY")
        message = str(captured.exception)
        self.assertIn(operation, message)
        self.assertIn("export a mutator function", message)
        self.assertIn("owning module", message)

    def test_field_refs_and_copy_are_absent_from_public_surface(self):
        def prog():
            rv = as_readonly(Var(1))

            for name in FIELD_REFS_AND_COPY:
                with self.subTest(name=name):
                    self.assertFalse(hasattr(rv, name))
                    with self.assertRaises(AttributeError):
                        getattr(rv, name)

        build_module(prog)

    def test_direct_mutation_methods_raise_actionable_readonly_error(self):
        def prog():
            rv = as_readonly(Var(1))

            for name in MUTATION_METHODS:
                with self.subTest(method=name):
                    self.assert_readonly_error(name, lambda: getattr(rv, name)(2))

        build_module(prog)

    def test_in_place_operators_raise_actionable_readonly_error(self):
        def prog():
            rv = as_readonly(Var(1))

            for symbol, op in IN_PLACE_OPERATORS:
                with self.subTest(operator=symbol):
                    self.assert_readonly_error(symbol, lambda: op(rv, 2))

        build_module(prog)

    def test_pure_expressions_keep_the_readonly_view_as_operand(self):
        def prog():
            rv = as_readonly(Var(10))

            expr = rv + 1
            self.assertIsInstance(expr, Expr)
            self.assertIsInstance(expr.lhs, ReadonlyVar)
            self.assert_readonly_error("set()", lambda: expr.lhs.set(1))
            self.assertFalse(hasattr(expr.lhs, "copy"))

            exponent = (rv * 2).bitXor(3)
            self.assertIsInstance(exponent.lhs, Expr)
            self.assertIsInstance(exponent.lhs.lhs, ReadonlyVar)

        build_module(prog)

    def test_owner_handle_remains_the_only_mutation_path(self):
        wave = lazy_var(0)
        readonly_wave = as_readonly(wave)

        def advance_wave():
            wave.iadd(1)

        def prog():
            with when(readonly_wave.atLeast(1)):
                pass
            advance_wave()
            self.assertEqual(wave.init, 0)

        build_module(prog)

    def test_owner_mutator_is_the_supported_mutation_path(self):
        wave = lazy_var(0)
        readonly_wave = as_readonly(wave)
        seen_id = -1

        def advance_wave():
            wave.iadd(1)

        def prog():
            nonlocal seen_id
            seen = Var(0)
            advance_wave()
            with when(readonly_wave.atLeast(1)):
                seen.set(readonly_wave)
            seen_id = seen.id

        mod = build_module(prog)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(report["var_values"][seen_id], 1)

    def test_conditions_and_formatting_still_work_on_the_view(self):
        wave = lazy_var(3)
        readonly_wave = as_readonly(wave)

        def prog():
            self.assertEqual(readonly_wave.exactly(3), wave.eq(3))
            self.assertEqual(readonly_wave > 2, wave.gt(2))

            s = FixedString(16)
            s.write("wave={:d}", readonly_wave)

        build_module(prog)


if __name__ == "__main__":
    unittest.main()
