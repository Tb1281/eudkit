"""Ticket #44: a runtime condition is never a Python bool (ADR 0020).

`when(conds, body)` runs `body` once, at build time; every comparison
(`v.eq(1)`, `v == 1`, a field read, `Never()`) is a HIR record describing an
in-game test, never a build-time answer. Before this guard, `if`/`while`/
`assert`/ternary/`and`/`or`/`not` silently picked a branch while `buildModule`
ran: `if v.eq(1): A else: B` emitted only A, `a and b` dropped a condition,
`while v == 0:` never returned, and `if Never():` ran the then-branch for a
condition that can never hold.

These tests pin the loud behaviour: every form raises `EUDSL-COND-TRUTHIFY`
before an act is queued, while the sanctioned spellings (`when`, `once`,
`until`, `while_loop`, a raw `Trigger`) still build. Each form is its own
subtest so a partially applied fix (one missing `__bool__`, or the
`is not None` slot-view edits) fails at least one row.

Rows 1-7 are **red on `e8272f2`** (each docstring records what HEAD did);
rows 8-10 are controls that are **green before and after** — they prove the
dict API, the sanctioned spellings, and the README mapping are unchanged.
"""

from __future__ import annotations

import json
import threading
import unittest

from py import (
    Always,
    CUnit,
    EudslError,
    Never,
    Trigger,
    Var,
    as_readonly,
    build_module,
    once,
    until,
    when,
    while_loop,
)
from py.tests.helpers import run_sim

_COND_SUBSTRING = "runtime condition"
_VALUE_SUBSTRING = "runtime value"


class ConditionTruthinessTests(unittest.TestCase):
    def _assert_truthify(self, build, substring: str = _COND_SUBSTRING) -> None:
        """Assert `build()` raises the guard with the code plus one stable substring."""
        with self.assertRaises(EudslError) as ctx:
            build_module(build)
        self.assertEqual(ctx.exception.code, "EUDSL-COND-TRUTHIFY")
        self.assertIn(substring, str(ctx.exception))
        # The spelling pointer is the migration path, whatever the site.
        self.assertIn("when()/once()/while_loop()", str(ctx.exception))

    # -- 1. if / else on a runtime condition ------------------------------

    def test_if_else_on_runtime_condition_raises(self):
        """HEAD: builds, and the HIR is byte-identical to the then-only module.

        Measured on `e8272f2`: `if v.eq(1): hit.iadd(1) else: hit.iadd(2)`
        builds a 369-char HIR equal to the then-only module (369 == 369); the
        `else` half is dead code.
        """

        def build():
            v = Var(0)
            hit = Var(0)
            if v.eq(1):
                hit.iadd(1)
            else:
                hit.iadd(2)

        self._assert_truthify(build)

    # -- 2. dunder comparison and a variable RHS ---------------------------

    def test_if_dunder_comparison_raises(self):
        """HEAD: builds with the same 369-char then-only HIR; `else` dropped."""

        def build_eq_const():
            v = Var(0)
            hit = Var(0)
            if v == 1:
                hit.iadd(1)
            else:
                hit.iadd(2)

        def build_eq_var():
            v = Var(0)
            w = Var(0)
            hit = Var(0)
            if v == w:  # DeferredCond: a variable RHS is always truthy today
                hit.iadd(1)
            else:
                hit.iadd(2)

        for name, fn in (
            ("const", build_eq_const),
            ("var", build_eq_var),
        ):
            with self.subTest(rhs=name):
                self._assert_truthify(fn)

    # -- 3. and / or ------------------------------------------------------

    def test_and_or_raise(self):
        """HEAD: `and` keeps only `lt`; `or` keeps only `eq` — one cond dropped."""

        def build_and():
            v = Var(0)
            hit = Var(0)
            if v.eq(1) and v.lt(5):
                hit.iadd(1)

        def build_or():
            v = Var(0)
            hit = Var(0)
            if v.eq(1) or v.lt(5):
                hit.iadd(1)

        for name, fn in (("and", build_and), ("or", build_or)):
            with self.subTest(op=name):
                self._assert_truthify(fn)

    # -- 4. not / ternary / assert ---------------------------------------

    def test_not_ternary_assert_raise(self):
        """HEAD: `not c` always runs the body; the ternary always takes then;
        `assert c` never fires (all build silently)."""

        def build_not():
            v = Var(0)
            hit = Var(0)
            if not v.eq(1):
                hit.iadd(1)

        def build_ternary():
            v = Var(0)
            hit = Var(0)
            hit.iadd(1 if v.eq(1) else 2)

        def build_assert():
            v = Var(0)
            assert v.eq(1)

        for name, fn in (
            ("not", build_not),
            ("ternary", build_ternary),
            ("assert", build_assert),
        ):
            with self.subTest(form=name):
                self._assert_truthify(fn)

    # -- 5. bare values ---------------------------------------------------

    def test_bare_values_raise(self):
        """HEAD: `if v:` / `if expr:` / a field read are always true; the
        `DeferredNe` row (`hp.ne(5)`) is the seventh site the scan missed."""

        def build_var():
            v = Var(0)
            if v:
                pass

        def build_readonly():
            v = Var(0)
            if as_readonly(v):
                pass

        def build_expr():
            v = Var(0)
            if v.add(1):
                pass

        def build_field():
            CUnit(1000).hp  # noqa: B018 - construction is the point
            if CUnit(1000).hp:
                pass

        def build_trigger_field():
            t = Trigger(conds=[], acts=[])
            if t.cond(0).player:
                pass

        def build_deferred_ne():
            if CUnit(1000).hp.ne(5):
                pass

        for name, fn, substring in (
            ("Var", build_var, _VALUE_SUBSTRING),
            ("ReadonlyVar", build_readonly, _VALUE_SUBSTRING),
            ("Expr", build_expr, _VALUE_SUBSTRING),
            ("MemFieldHandle", build_field, _VALUE_SUBSTRING),
            ("TriggerFieldRef", build_trigger_field, _VALUE_SUBSTRING),
            ("DeferredNe", build_deferred_ne, _COND_SUBSTRING),
        ):
            with self.subTest(site=name):
                self._assert_truthify(fn, substring)

    # -- 6. Never / Always ------------------------------------------------

    def test_never_and_always_raise(self):
        """HEAD: `if Never():` and `if Always():` both run the then-branch;
        `bool(Never())` is `True` — a condition that can never hold is truthy."""

        def build_never():
            if Never():
                pass

        def build_always():
            if Always():
                pass

        def build_bool():
            bool(Never())

        for name, fn in (
            ("Never", build_never),
            ("Always", build_always),
            ("bool", build_bool),
        ):
            with self.subTest(form=name):
                self._assert_truthify(fn)

    # -- 7. while: fail fast, never hang ---------------------------------

    def test_truthiness_in_while_fails_fast(self):
        """HEAD: `while v == 0: pass` never returns (killed at 5 s).

        The body is non-emitting, so a regression cannot grow the module to
        OOM while the test waits; the thread guard is what keeps CI alive if
        the guard is ever reverted.
        """

        captured: list[BaseException] = []

        def run():
            def build():
                v = Var(0)
                while v == 0:  # noqa: B018 - the truthiness test is the point
                    pass

            try:
                build_module(build)
            except BaseException as exc:  # noqa: BLE001 - captured for the assert
                captured.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "HEAD hangs: guard did not fail fast")
        self.assertEqual(len(captured), 1, "the guard did not raise")
        self.assertIsInstance(captured[0], EudslError)
        self.assertEqual(captured[0].code, "EUDSL-COND-TRUTHIFY")

    # -- 8. the cond dict API is unchanged (control) ----------------------

    def test_cond_dict_api_is_unchanged(self):
        """Control (green before and after): a condition is still a dict —
        indexing, `dict()`, JSON round-trip, and equality on whole HIR trees
        all work; `when([cond], ...)` builds the same HIR as a plain copy."""

        def build_cond():
            v = Var(0)
            hit = Var(0)
            cond = v.eq(1)
            self.assertEqual(cond["condtype"], 15)
            self.assertEqual(cond, dict(cond))
            self.assertEqual(json.loads(json.dumps(cond)), dict(cond))
            never = Never()
            self.assertEqual(never["condtype"], 23)
            self.assertEqual([never], [Never()])
            when([cond], lambda: hit.iadd(1))

        def build_plain():
            v = Var(0)
            hit = Var(0)
            plain = dict(v.eq(1))
            when([plain], lambda: hit.iadd(1))

        self.assertEqual(
            build_module(build_cond).to_json(), build_module(build_plain).to_json()
        )

    # -- 9. sanctioned spellings still build (control) --------------------

    def test_sanctioned_spellings_still_build(self):
        """Control (green before and after): every sanctioned spelling still
        builds, and the 0-cond boolean semantics of `test_0cond_boolean.py`
        do not move."""

        def build():
            v = Var(0)
            hit = Var(0)
            c = v.eq(1)
            when([c], lambda: hit.iadd(1))
            when(c, lambda: hit.iadd(1))
            with when([c]):
                hit.iadd(1)
            once([c], lambda: hit.iadd(1))
            until([c], lambda: hit.iadd(1))
            while_loop([v.lt(2)], lambda: v.iadd(1))
            Trigger(conds=[c], acts=[hit.act_add(1)])
            when(True, lambda: hit.iadd(1))
            when(False, lambda: hit.iadd(1))
            # The other two views of the same cond list must build too.
            when([Never()], lambda: hit.iadd(1))
            when([Always()], lambda: hit.iadd(1))

        mod = build_module(build)
        self.assertGreater(len(mod.to_json()), 0)

    # -- 10. the README mapping is real runtime branching (control) -------

    def test_mapping_table_is_runtime_branching(self):
        """Control (green before and after): prove the recommended
        replacements branch in-game, not at build time.

        With `a` false and `b` true:
        * `when([a, b], body)` does **not** run (the old `a and b` spelling
          would keep `b` alone and run);
        * `when([a], body)` + `when([b], body)` is the `or` mapping and runs
          exactly once;
        * `when([complement(a)])` is the `else` mapping and runs while the
          then-block does not;
        * `while_loop` repeats its body and stops when the cond goes false.
        """

        ids: dict[str, int] = {}

        def build():
            a = Var(0)  # a == 0, so `a.eq(1)` is false
            b = Var(1)  # b == 1, so `b.eq(1)` is true
            both = Var(0)
            only_b = Var(0)
            or_hit = Var(0)
            then_hit = Var(0)
            else_hit = Var(0)
            when([a.eq(1), b.eq(1)], lambda: both.iadd(1))
            when([b.eq(1)], lambda: only_b.iadd(1))
            when([a.eq(1)], lambda: or_hit.iadd(1))
            when([b.eq(1)], lambda: or_hit.iadd(1))
            when([a.eq(1)], lambda: then_hit.iadd(1))
            when([a.ne(1)], lambda: else_hit.iadd(1))
            ids.update(
                both=both.id,
                only_b=only_b.id,
                or_hit=or_hit.id,
                then_hit=then_hit.id,
                else_hit=else_hit.id,
            )

        report = run_sim(build_module(build).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["both"]], 0)
        self.assertEqual(values[ids["only_b"]], 1)
        self.assertEqual(values[ids["or_hit"]], 1)
        self.assertEqual(values[ids["then_hit"]], 0)
        self.assertEqual(values[ids["else_hit"]], 1)

    def test_while_loop_is_the_sanctioned_runtime_while(self):
        """Control (green before and after): `while_loop` repeats in-game and
        stops when its cond goes false — the `while c: body` replacement."""

        ids: dict[str, int] = {}

        def build():
            i = Var(0)
            total = Var(0)

            def body():
                i.iadd(1)
                total.iadd(1)

            while_loop([i.lt(3)], body)
            ids.update(i=i.id, total=total.id)

        report = run_sim(build_module(build).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["i"]], 3)
        self.assertEqual(values[ids["total"]], 3)


if __name__ == "__main__":
    unittest.main()
