"""Ticket #96: Cross-file encapsulation: ReadonlyVar type wrapper for imported lazy handles."""

import unittest
from py import (
    EudslError,
    FixedString,
    ReadonlyVar,
    Var,
    asReadonly,
    as_readonly,
    build_module,
    lazyVar,
    lazy_var,
    when,
)
from py.tests.helpers import run_sim


class Ticket96ReadonlyVarTest(unittest.TestCase):
    def test_eager_var_properties(self):
        def prog():
            v = Var(42, explicit_mask=0xFFFF)
            rv = as_readonly(v)
            self.assertIsInstance(rv, ReadonlyVar)
            self.assertEqual(rv.init, 42)
            self.assertEqual(rv.initial_value, 42)
            self.assertEqual(rv.initialValue, 42)
            self.assertEqual(rv.bitmask, 0xFFFF)
            self.assertIsInstance(rv.id, int)
            self.assertFalse(hasattr(rv, "value"))
            self.assertFalse(hasattr(rv, "copy"))

        build_module(prog)

    def test_condition_methods_and_aliases(self):
        def prog():
            v = Var(10)
            rv = as_readonly(v)

            c_eq = rv.eq(10)
            c_ne = rv.ne(10)
            c_ge = rv.ge(10)
            c_le = rv.le(10)
            c_gt = rv.gt(10)
            c_lt = rv.lt(10)

            self.assertEqual(rv.at_least(10), c_ge)
            self.assertEqual(rv.atLeast(10), c_ge)
            self.assertEqual(rv.at_most(10), c_le)
            self.assertEqual(rv.atMost(10), c_le)
            self.assertEqual(rv.exactly(10), c_eq)

            # Python comparison operators
            self.assertEqual(rv == 10, c_eq)
            c_ne0 = rv.ne(0)
            self.assertEqual(rv != 0, c_ne0)
            self.assertEqual((rv != 10)["comparison"], c_ne["comparison"])
            self.assertEqual(rv >= 10, c_ge)
            self.assertEqual(rv <= 10, c_le)
            self.assertEqual(rv > 10, c_gt)
            self.assertEqual(rv < 10, c_lt)

        build_module(prog)

    def test_mutations_disallowed(self):
        def prog():
            v = Var(10)
            rv = as_readonly(v)

            # Every direct mutator must raise the documented read-only error.
            for method_name in [
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
            ]:
                with self.subTest(method=method_name):
                    with self.assertRaises(EudslError) as captured:
                        getattr(rv, method_name)(5)
                    self.assertEqual(captured.exception.code, "EUDSL-READONLY")

            # In-place operators must raise the documented read-only error.
            for operation in (
                lambda: rv.__iadd__(1),
                lambda: rv.__isub__(1),
                lambda: rv.__imul__(2),
                lambda: rv.__itruediv__(2),
                lambda: rv.__ifloordiv__(2),
                lambda: rv.__imod__(2),
                lambda: rv.__iand__(2),
                lambda: rv.__ior__(2),
                lambda: rv.__ixor__(2),
                lambda: rv.__ilshift__(2),
                lambda: rv.__irshift__(2),
            ):
                with self.assertRaises(EudslError) as captured:
                    operation()
                self.assertEqual(captured.exception.code, "EUDSL-READONLY")

            # The owner's raw handle is still mutable.
            v.iadd(1)

        build_module(prog)

    def test_lazy_var_cross_file_flow(self):
        # Module-level declaration outside build_module
        counter = lazy_var(123)
        ro_counter = as_readonly(counter)

        self.assertIsInstance(ro_counter, ReadonlyVar)
        self.assertEqual(ro_counter.init, 123)
        self.assertEqual(ro_counter.bitmask, 0xFFFFFFFF)

        def prog():
            # Resolves inside build_module
            self.assertIsInstance(ro_counter.id, int)

            # Reading into another mutable Var
            dest = Var(0)
            dest.set(ro_counter)
            dest.iadd(ro_counter)
            dest.isub(ro_counter)
            dest.set(ro_counter + 1)

            # Condition in when()
            flag = Var(0)
            with when(ro_counter.exactly(123)):
                flag.set(1)

            # FixedString format placeholder support
            s = FixedString(64)
            s.write("counter={:d} hex={:08x}", ro_counter, ro_counter)

        build_module(prog)

    def test_readonly_var_as_position_and_line(self):
        pos = lazy_var(0)
        line = lazy_var(0)
        ro_pos = as_readonly(pos)
        ro_line = as_readonly(line)

        def prog():
            s = FixedString(16)
            s.writeFrom(ro_pos, "ok")
            s.printAt(ro_line, "ok")
            s.displayAt(ro_pos)

        build_module(prog)

    def test_as_readonly_idempotence(self):
        def prog():
            v = Var(5)
            rv1 = as_readonly(v)
            rv2 = as_readonly(rv1)
            rv3 = asReadonly(rv1)
            self.assertIs(rv1, rv2)
            self.assertIs(rv1, rv3)

        build_module(prog)

    def test_simulation_consumer_reads_from_owner_readonly_var(self):
        owner_var = lazy_var(42)
        ro_counter = as_readonly(owner_var)

        consumer_id = -1
        owner_id = -1

        def prog():
            nonlocal consumer_id, owner_id
            consumer_var = Var(0)
            with when(ro_counter.exactly(42)):
                consumer_var.set(ro_counter)
                consumer_var.iadd(8)
            consumer_id = consumer_var.id
            owner_id = ro_counter.id

        mod = build_module(prog)
        report = run_sim(mod.to_json(), 1)
        self.assertEqual(report["var_values"][owner_id], 42)
        self.assertEqual(report["var_values"][consumer_id], 50)

