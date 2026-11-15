"""Ticket #52: equality dunders reject the rejected scalar domain.

The sibling comparison surfaces already validate their operand (ADR 0019:
validate, do not coerce): ``v.eq(1.5)``, ``v < 1.5``, and
``u.hp == 1.5`` raise ``EUDSL-TYPE``. The equality dunders were the remaining
hole: on HEAD ``v == 1.5`` / ``v == "abc"`` / ``v == True`` fell through the
``NotImplemented`` protocol and became a plain Python ``False`` (and ``!=`` a
plain ``True``), so ``if v == 1.5:`` silently took the ``else`` branch.

This ticket keeps the protocol where it belongs — ``None`` and unrelated
objects still return ``NotImplemented`` (so ``v == None`` stays ``False``) —
and routes the rejected scalar domain (``float`` / ``str`` / ``bool``) through
the existing ``eq`` / ``ne`` guard. Plain ``int``, ``Var``, ``Expr``, and
``ReadonlyVar`` operands keep building the same conditions as before.

The eager list-form guard is the second half: ``when([cond == True], body)``
used to build ``conds: [false]`` for the Rust parser to reject later; it now
raises ``EUDSL-TYPE`` at the call. The bare ``when(cond == True, body)`` is a
documented build-time drop (ADR 0020) and stays pinned as such, as does the
``HirCond`` dict API (deliberately no ``__eq__`` override).
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from typing import Any, Callable

from py import (
    EudslError,
    Trigger,
    Var,
    as_readonly,
    build_module,
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
)
from py.hir import HirCond
from py.tests.helpers import REPO_ROOT
from py.var import DeferredCond

_VAR_OPERAND = "integer constant, a Var, or an Expr"
_FIELD_OPERAND = "field comparison must be a plain integer"
_LIST_BOOLEAN = "condition list must not contain a boolean"

_BARE_BOOLEAN_TS_SCRIPT = textwrap.dedent(
    """
    import {
      EudslError,
      Var,
      buildModule,
      onFalling,
      onRising,
      whileLoop,
    } from "./ts/index.ts";

    const rows = [
      ["onRising", () => onRising(false, () => {})],
      ["onFalling", () => onFalling(false, () => {})],
      ["whileLoopFalse", () => whileLoop(false, () => {})],
      ["whileLoopTrue", () => whileLoop(true, () => {})],
    ];
    const results = [];
    for (const [label, call] of rows) {
      try {
        buildModule(() => {
          call();
        });
        results.push(label + ":ok");
      } catch (error) {
        results.push(
          label + ":" + (error instanceof EudslError ? error.code : "non-eudsl"),
        );
      }
    }
    process.stdout.write(results.join("\\n"));
    """
)


class EqualityDunderResidualsTest(unittest.TestCase):
    def _assert_eudsl_type(
        self, build: Callable[[], Any], expected: str
    ) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(build)
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
        self.assertIn(expected, str(ctx.exception))

    # -- value dunders ------------------------------------------------------

    def test_var_eq_ne_reject_float_str_bool(self) -> None:
        """HEAD: ``False`` / ``True`` / ``False`` / ``False`` / ``True``.

        ``Var.__eq__`` / ``__ne__`` returned ``NotImplemented`` for every
        non-plain-int, so the comparison fell back to Python identity and never
        reached the existing guarded ``eq`` / ``ne`` bodies.
        """
        rows = (
            ("eq-float", "eq", 1.5),
            ("ne-float", "ne", 1.5),
            ("eq-str", "eq", "abc"),
            ("eq-bool", "eq", True),
            ("ne-bool", "ne", True),
        )
        for label, op, value in rows:
            def build(op: str = op, value: Any = value) -> None:
                v = Var(0)
                if op == "eq":
                    _ = v == value
                else:
                    _ = v != value

            with self.subTest(row=label):
                self._assert_eudsl_type(build, _VAR_OPERAND)

    def test_expr_eq_ne_reject_float_str_bool(self) -> None:
        """HEAD: ``False`` / ``True`` / ``False`` / ``True`` for the four rows."""
        rows = (
            ("eq-float", "eq", 1.5),
            ("ne-str", "ne", "abc"),
            ("eq-bool", "eq", True),
            ("ne-bool", "ne", True),
        )
        for label, op, value in rows:
            def build(op: str = op, value: Any = value) -> None:
                expr = Var(0).add(1)
                if op == "eq":
                    _ = expr == value
                else:
                    _ = expr != value

            with self.subTest(row=label):
                self._assert_eudsl_type(build, _VAR_OPERAND)

    def test_readonly_var_eq_ne_inherit_the_guard(self) -> None:
        """``ReadonlyVar`` delegates to its owner's dunders, so it inherits the rule."""
        rows = (("eq-float", "eq", 1.5), ("ne-str", "ne", "abc"))
        for label, op, value in rows:
            def build(op: str = op, value: Any = value) -> None:
                v = Var(0)
                if op == "eq":
                    _ = as_readonly(v) == value
                else:
                    _ = as_readonly(v) != value

            with self.subTest(row=label):
                self._assert_eudsl_type(build, _VAR_OPERAND)

    def test_trigger_field_ref_eq_ne_reject_str(self) -> None:
        """HEAD: ``player == "abc"`` is ``False``; ``!=`` is ``True``."""
        rows = (("eq-str", "eq"), ("ne-str", "ne"))
        for label, op in rows:
            def build(op: str = op) -> None:
                hit = Var(0)
                t = Trigger(conds=[], acts=[hit.act_set(1)])
                player = t.cond(0).player
                if op == "eq":
                    _ = player == "abc"
                else:
                    _ = player != "abc"

            with self.subTest(row=label):
                self._assert_eudsl_type(build, _FIELD_OPERAND)

    # -- protocol controls --------------------------------------------------

    def test_none_and_unrelated_operands_stay_notimplemented(self) -> None:
        """Control (green before and after): the ``== None`` idiom is not an error."""
        def build() -> None:
            v = Var(0)
            self.assertIs(v == None, False)  # noqa: E711
            self.assertIs(v != None, True)  # noqa: E711
            self.assertIs(v == object(), False)
            self.assertIs(v != object(), True)

        build_module(build)

    def test_plain_int_and_runtime_operands_unchanged(self) -> None:
        """Control (green before and after): the legal operand domain is unchanged."""
        def build() -> None:
            v = Var(0)
            w = Var(1)
            self.assertIsInstance(v == 1, HirCond)
            self.assertIsInstance(v == w, DeferredCond)
            self.assertIsInstance(v == as_readonly(w), DeferredCond)
            # ``Expr`` now delegates ``ReadonlyVar`` explicitly instead of
            # relying on the reflected comparison; the result is the same.
            self.assertIsInstance(v.add(1) == as_readonly(w), DeferredCond)

        build_module(build)

    # -- HirCond residuals --------------------------------------------------

    def test_cond_dict_residuals(self) -> None:
        """Pin the decided residual: ``HirCond`` keeps the plain dict API.

        ``cond == True`` is ``False`` (``dict.__eq__``) and ``cond is None`` is
        ``False``; ``HirCond.__eq__`` is deliberately not overridden because
        whole-HIR ``assertEqual`` compares dicts recursively.
        """
        def build() -> None:
            v = Var(0)
            cond = v.eq(1)
            self.assertIs(cond == True, False)  # noqa: E712
            self.assertIs(cond is None, False)
            self.assertEqual(dict(cond), cond)
            self.assertEqual(json.loads(json.dumps(cond)), dict(cond))

        build_module(build)

    def test_bare_cond_eq_true_still_drops(self) -> None:
        """Pin the documented drop: the bare form is indistinguishable from ``when(False)``."""
        def dropped() -> None:
            v = Var(0)
            when(v.eq(1) == True, lambda: Var(0).iadd(1))  # noqa: E712

        def control() -> None:
            Var(0)

        self.assertEqual(
            build_module(dropped).to_json(), build_module(control).to_json()
        )

    # -- eager list guard ---------------------------------------------------

    def test_cond_list_boolean_is_eager_type_error(self) -> None:
        """HEAD: builds ``conds: [false]`` for the Rust parser to reject later.

        The shared guard must cover every control form, not just the two that
        own the original red rows.
        """
        rows = (
            ("when", when),
            ("once", once),
            ("until", until),
            ("while_loop", while_loop),
            ("on_rising", on_rising),
            ("on_falling", on_falling),
        )
        items = (
            ("literal-true", lambda: True),
            ("literal-false", lambda: False),
            ("cond-eq-true", lambda: Var(0).eq(1) == True),  # noqa: E712
        )
        for label, control in rows:
            for item_label, make_item in items:
                def build(
                    control: Callable[..., Any] = control,
                    make_item: Callable[[], Any] = make_item,
                ) -> None:
                    hit = Var(0)
                    control([make_item()], lambda: hit.iadd(1))

                with self.subTest(form=label, item=item_label):
                    self._assert_eudsl_type(build, _LIST_BOOLEAN)

    def test_bare_boolean_while_loop_is_eager_type_error(self) -> None:
        """A bare boolean is one cond item, so ``while_loop`` must reject it."""
        for value in (True, False):
            def build(value: bool = value) -> None:
                hit = Var(0)
                while_loop(value, lambda: hit.iadd(1))

            with self.subTest(value=value):
                self._assert_eudsl_type(build, _LIST_BOOLEAN)

    def test_bare_false_edge_and_while_forms_match_ts(self) -> None:
        """HEAD: py rejects ``on_rising(False)`` / ``on_falling(False)``, but
        TS treated ``false`` as "no conds" and built an unconditional form.
        Both frontends now reject the bare boolean consistently.
        """
        rows = (
            ("onRising", lambda: on_rising(False, lambda: None)),
            ("onFalling", lambda: on_falling(False, lambda: None)),
            ("whileLoopFalse", lambda: while_loop(False, lambda: None)),
            ("whileLoopTrue", lambda: while_loop(True, lambda: None)),
        )
        py_codes: list[str] = []
        for label, call in rows:
            with self.subTest(frontend="py", form=label):
                with self.assertRaises(EudslError) as ctx:
                    build_module(call)
                py_codes.append(f"{label}:{ctx.exception.code}")

        res = subprocess.run(
            ["node", "--input-type=module", "-e", _BARE_BOOLEAN_TS_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.stdout.strip().splitlines(), py_codes)

    def test_cond_list_boolean_control(self) -> None:
        """HEAD: a literal boolean item builds ``conds: [true]``."""
        def build() -> None:
            hit = Var(0)
            when([True], lambda: hit.iadd(1))

        self._assert_eudsl_type(build, _LIST_BOOLEAN)


if __name__ == "__main__":
    unittest.main()
