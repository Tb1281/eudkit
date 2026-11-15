"""Single-entry-point guard tests (user rule, 2026-11-04): ``build_module``
is a single entry point, one per map, like Rust ``main``. A nested
``build_module`` call must throw ``EUDSL-SCOPE``. Sequential builds (each
building its own map) are unaffected: the guard checks the ACTIVE builder
only, which is cleared when a ``build_module`` call returns, including
when its body throws.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import Var, build_module
from py.view import EudslError


def decls(mod: Any) -> list:
    return [(v["id"], v["init"]) for v in json.loads(mod.to_json())["module"]["vars"]]


class TestSingleEntry(unittest.TestCase):
    def test_nested_build_module_throws(self) -> None:
        def outer() -> None:
            a = Var(1)
            a.iadd(1)

            def inner() -> None:
                b = Var(2)
                b.iadd(1)

            build_module(inner)

        with self.assertRaises(EudslError) as cm:
            build_module(outer)
        self.assertEqual(cm.exception.code, "EUDSL-SCOPE")

    def test_sequential_builds_independent(self) -> None:
        def body1() -> None:
            a = Var(1)
            a.iadd(1)

        def body2() -> None:
            b = Var(2)
            b.iadd(1)

        mod1 = build_module(body1)
        mod2 = build_module(body2)
        self.assertEqual(decls(mod1), [(0, 1)])
        self.assertEqual(decls(mod2), [(0, 2)])

    def test_build_after_throwing_body_succeeds(self) -> None:
        def throwing() -> None:
            a = Var(1)
            a.iadd(1)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            build_module(throwing)

        def body() -> None:
            b = Var(5)
            b.iadd(1)

        self.assertEqual(decls(build_module(body)), [(0, 5)])

    def test_caught_nested_throw_outer_completes(self) -> None:
        caught: list = []

        def outer() -> None:
            a = Var(1)
            a.iadd(1)
            try:

                def inner() -> None:
                    b = Var(2)
                    b.iadd(1)

                build_module(inner)
            except EudslError as exc:
                self.assertEqual(exc.code, "EUDSL-SCOPE")
                caught.append(True)
            a.iadd(1)

        mod = build_module(outer)
        self.assertEqual(caught, [True])
        self.assertEqual(decls(mod), [(0, 1)])


if __name__ == "__main__":
    unittest.main()
