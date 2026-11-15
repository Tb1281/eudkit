"""``lazyVar`` public API tests (globals P1-02).

``lazyVar`` is the thin public factory over the P1-01 deferred base: a
handle declared anywhere (no builder needed) that resolves to a fully
eager binding on first use inside ``build_module``, one binding per
builder. Eager ``Var`` is unchanged; ``lazyVar`` is opt-in only.
Compatible with both `pytest` and `python3 -m unittest`.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import Var, build_module, lazyVar, lazy_var
from py.tests import lazy_var_fixture as fixture
from py.temporal import when
from py.view import EudslError

from py.tests.helpers import run_sim
def hir_json(mod: Any) -> dict:
    return json.loads(mod.to_json())

def decls(mod: Any) -> list[tuple[int, int]]:
    return [(v["id"], v["init"]) for v in hir_json(mod)["module"]["vars"]]

class LazyVarTest(unittest.TestCase):
    def test_declaration_outside_builder_allocates_nothing(self) -> None:
        handle = lazyVar(7)

        def body() -> None:
            Var(0).set(1)

        with_handle = build_module(body)
        without_handle = build_module(body)
        # Identical output proves the unused handle consumed no id, emitted
        # no decl, and emitted no statement.
        self.assertEqual(hir_json(with_handle), hir_json(without_handle))
        self.assertEqual(decls(with_handle), [(0, 0)])
        self.assertEqual(handle.init, 7)

    def test_call_shape_mask_and_page_storage(self) -> None:
        masked = lazyVar(3, mask=0xFF)
        paged = lazyVar(0, storage="page")
        # Pure reads never resolve.
        self.assertEqual(masked.bitmask, 0xFF)
        self.assertEqual(masked.init, 3)
        twin_paged: list[Var] = []

        def body() -> None:
            masked.iadd(1)
            paged.iadd(1)

        def twin_body() -> None:
            m = Var(3, explicit_mask=0xFF)
            p = Var(0, storage="page")
            twin_paged.append(p)
            m.iadd(1)
            p.iadd(1)

        mod = build_module(body)
        twin = build_module(twin_body)
        self.assertEqual(hir_json(mod), hir_json(twin))
        # Page coordinates match the eager page var (single-record
        # post-build read on the lazy handle, both aliases).
        self.assertEqual(paged.pageIndex, twin_paged[0].pageIndex)
        self.assertEqual(paged.page_index, twin_paged[0].page_index)
        self.assertEqual(paged.slotIndex, twin_paged[0].slotIndex)
        self.assertEqual(paged.slot_index, twin_paged[0].slot_index)

    def test_non_const_init_raises_type_eagerly(self) -> None:
        # Fail fast at the declaration site, outside any builder.
        with self.assertRaises(EudslError) as cm:
            lazyVar("nope")  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "EUDSL-TYPE")

        def body() -> None:
            e = Var(0)
            with self.assertRaises(EudslError) as cm2:
                lazyVar(e)
            self.assertEqual(cm2.exception.code, "EUDSL-TYPE")
            with self.assertRaises(EudslError) as cm3:
                lazyVar(e.add(1))
            self.assertEqual(cm3.exception.code, "EUDSL-TYPE")

        build_module(body)

    def test_works_everywhere_eager_var_works(self) -> None:
        g = lazyVar(5)
        self.assertIsInstance(g, Var)

        def body() -> None:
            g.iadd(1)
            h = Var(0)
            h.set(g)
            h2 = Var(0)
            h2.set(g.add(1))
            when(g.eq(6), lambda: h.iadd(10))

        def twin_body() -> None:
            v = Var(5)
            v.iadd(1)
            h = Var(0)
            h.set(v)
            h2 = Var(0)
            h2.set(v.add(1))
            when(v.eq(6), lambda: h.iadd(10))

        self.assertEqual(
            hir_json(build_module(body)), hir_json(build_module(twin_body))
        )

    def test_out_of_order_first_use(self) -> None:
        a = lazyVar(11)
        b = lazyVar(22)

        def body() -> None:
            b.iadd(1)
            a.iadd(1)

        def reordered_body() -> None:
            bb = Var(22)
            aa = Var(11)
            bb.iadd(1)
            aa.iadd(1)

        def declaration_order_body() -> None:
            aa = Var(11)
            bb = Var(22)
            bb.iadd(1)
            aa.iadd(1)

        mod = build_module(body)
        self.assertEqual(
            hir_json(mod), hir_json(build_module(reordered_body))
        )
        self.assertNotEqual(
            hir_json(mod), hir_json(build_module(declaration_order_body))
        )
        # Ids follow first use: B first.
        self.assertEqual(decls(mod), [(0, 22), (1, 11)])

    def test_never_used_emits_nothing(self) -> None:
        unused = lazyVar(9)

        def body() -> None:
            Var(0).set(1)

        with_unused = build_module(body)
        without_unused = build_module(body)
        self.assertEqual(hir_json(with_unused), hir_json(without_unused))
        self.assertEqual(decls(with_unused), [(0, 0)])

    def test_same_handle_under_two_builders(self) -> None:
        g = lazyVar(4)

        def body1() -> None:
            g.iadd(1)

        def body2() -> None:
            first = Var(0)
            first.set(1)
            g.iadd(1)

        def twin1() -> None:
            v = Var(4)
            v.iadd(1)

        def twin2() -> None:
            first = Var(0)
            first.set(1)
            v = Var(4)
            v.iadd(1)

        mod1 = build_module(body1)
        mod2 = build_module(body2)
        # Neither module references the other's ids: mod1 binds the handle
        # at id 0, mod2 at id 1.
        self.assertEqual(hir_json(mod1), hir_json(build_module(twin1)))
        self.assertEqual(hir_json(mod2), hir_json(build_module(twin2)))
        self.assertEqual(decls(mod1), [(0, 4)])
        self.assertEqual(decls(mod2), [(0, 0), (1, 4)])

    def test_conditional_first_use_with_const_init(self) -> None:
        g = lazyVar(3)

        def body() -> None:
            flag = Var(0)
            when(flag.eq(0), lambda: g.iadd(1))

        def twin_body() -> None:
            flag = Var(0)
            v = Var(3)
            when(flag.eq(0), lambda: v.iadd(1))

        self.assertEqual(
            hir_json(build_module(body)), hir_json(build_module(twin_body))
        )

    def test_cross_file_use_via_fixture_module(self) -> None:
        # Attribute form, as in real user code (`sub.globalVar += 1`): a
        # bare-global `+=` would rebind the name instead of mutating the
        # handle, so the fixture is used through its module object.
        def body() -> None:
            fixture.g += 1

        def twin_body() -> None:
            v = Var(5)
            v += 1

        mod = build_module(body)
        self.assertEqual(hir_json(mod), hir_json(build_module(twin_body)))
        self.assertEqual(decls(mod), [(0, 5)])

    def test_no_caller_chosen_id(self) -> None:
        first = lazyVar(7)
        second = lazyVar(7)

        def body() -> None:
            second.iadd(1)
            first.iadd(1)

        self.assertEqual(decls(build_module(body)), [(0, 7), (1, 7)])

    def test_snake_case_alias(self) -> None:
        self.assertIs(lazy_var, lazyVar)
        m = lazy_var(6)

        def body() -> None:
            m.iadd(1)

        self.assertEqual(decls(build_module(body)), [(0, 6)])

    def test_runs_like_eager_twin(self) -> None:
        g = lazyVar(10)

        def body() -> None:
            g.iadd(5)
            out = Var(0)
            out.set(g)

        def twin_body() -> None:
            v = Var(10)
            v.iadd(5)
            out = Var(0)
            out.set(v)

        a = run_sim(build_module(body).to_json())
        b = run_sim(build_module(twin_body).to_json())
        self.assertEqual(a["var_values"], b["var_values"])
        self.assertEqual(a["executed_cost"], b["executed_cost"])

if __name__ == "__main__":
    unittest.main()
