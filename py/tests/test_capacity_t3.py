"""T-WP10-T2: ``free_list_walk`` / ``free_slot_count`` — tier 3 one-shot scan.

The free list is a plain forward chain from ``0x628438`` through each node's
``next`` link (``ptr + 1``). The walk is the same shape as ``T-WP7``'s forward
unit-list walk, with two extra pieces:

* a ``remaining`` scan guard initialised to ``CUNIT_FREE_SCAN_MAX`` (1699) and
  decremented per node, so a cycle stops instead of hanging the game;
* a one-call-site rule per module, enforced **before** emission.

``free_slot_count()`` delegates to ``free_list_walk`` and therefore shares its
call site.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from py import (
    BREAK,
    AtLeast,
    AtMost,
    EudslError,
    Exactly,
    SetTo,
    Var,
    build_module,
)
from py.capacity import (
    CUNIT_FREE_HEAD,
    CUNIT_FREE_SCAN_MAX,
    free_list_walk,
    free_slot_count,
)
from py.cp import CP_ADDR
from py.hir import Add
from py.tests.helpers import flatten_stmts
from py.view import (
    CUNIT_BASE_EPD,
    CUNIT_LAST_EPD,
    CUNIT_SLOT_COUNT,
    EPD,
)


def _module(prog) -> dict[str, Any]:
    return json.loads(build_module(prog).to_json())["module"]


def _whiles(module: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in module["stmts"] if s["kind"] == "while"]


def _while(module: dict[str, Any]) -> dict[str, Any]:
    whiles = _whiles(module)
    assert len(whiles) == 1, whiles
    return whiles[0]


def _ptr_id(while_stmt: dict[str, Any]) -> int:
    return while_stmt["conds"][0]["relocs"][0]["target_var"]


def _done_id(while_stmt: dict[str, Any]) -> int:
    return while_stmt["conds"][2]["relocs"][0]["target_var"]


def _cp_stores(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        s
        for s in stmts
        if s["kind"] == "varstore" and s.get("dst_addr") == CP_ADDR
    ]


def _var_init(module: dict[str, Any], var_id: int) -> int:
    return next(v["init"] for v in module["vars"] if v["id"] == var_id)


def _remaining_id(module: dict[str, Any]) -> int:
    """The scan guard: the var the loop decrements by one per node."""
    body = _while(module)["body"]
    dec = next(
        s
        for s in body
        if s["kind"] == "act" and s.get("value") == 0xFFFFFFFF
    )
    return dec["relocs"][0]["target_var"]


def _acts_anywhere(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level acts plus the acts packed inside preserved triggers."""
    out: list[dict[str, Any]] = []
    for stmt in flatten_stmts(stmts):
        if stmt["kind"] == "act":
            out.append(stmt)
        out.extend(stmt.get("acts", []))
    return out


def _acts_targeting(stmts: list[dict[str, Any]], var_id: int) -> list[dict[str, Any]]:
    return [
        stmt
        for stmt in stmts
        if stmt["kind"] == "act"
        and any(r.get("target_var") == var_id for r in stmt.get("relocs", []))
    ]


class ConstantsTest(unittest.TestCase):
    def test_scan_max_is_the_pool_slot_count(self) -> None:
        self.assertEqual(CUNIT_FREE_SCAN_MAX, 1699)
        self.assertEqual(CUNIT_FREE_SCAN_MAX, CUNIT_SLOT_COUNT)

    def test_free_head_is_the_documented_dword(self) -> None:
        self.assertEqual(CUNIT_FREE_HEAD, 0x628438)
        self.assertEqual(EPD(CUNIT_FREE_HEAD), 161845)


class WalkShapeTest(unittest.TestCase):
    def test_one_ambient_read_one_while_and_one_final_restore(self) -> None:
        module = _module(lambda: free_slot_count())
        funcs = module["funcs"]
        self.assertEqual(sorted(len(fn["body"]) for fn in funcs), [20, 33])
        ambient_id = next(fn["id"] for fn in funcs if len(fn["body"]) == 33)

        calls = [s for s in flatten_stmts(module["stmts"]) if s["kind"] == "call"]
        self.assertEqual(
            len([c for c in calls if c["func"] == ambient_id]),
            1,
            "the 33T ambient CP read must happen exactly once per site",
        )

        ambient_call = next(
            s
            for s in module["stmts"]
            if s["kind"] == "call" and s["func"] == ambient_id
        )
        last = module["stmts"][-1]
        self.assertEqual(last["kind"], "varstore")
        self.assertEqual(last["dst_addr"], CP_ADDR)
        self.assertEqual(last["src_var"], ambient_call["dst"])

    def test_head_store_uses_the_free_list_head_literal(self) -> None:
        module = _module(lambda: free_slot_count())
        head_stores = [
            s
            for s in _cp_stores(module["stmts"])
            if _var_init(module, s["src_var"]) == EPD(CUNIT_FREE_HEAD)
        ]
        self.assertEqual(len(head_stores), 1)
        self.assertIn(EPD(CUNIT_FREE_HEAD), [v["init"] for v in module["vars"]])

    def test_gate_is_the_pool_bound_then_done(self) -> None:
        while_stmt = _while(_module(lambda: free_slot_count()))
        self.assertEqual(
            [c["amount"] for c in while_stmt["conds"]],
            [CUNIT_BASE_EPD, CUNIT_LAST_EPD, 0],
        )
        self.assertEqual(
            [c["comparison"] for c in while_stmt["conds"]],
            [AtLeast, AtMost, Exactly],
        )
        # The upper bound is what rejects the null sentinel; pin it explicitly.
        self.assertEqual(while_stmt["conds"][1]["amount"], 161657)

    def test_link_is_read_from_ptr_plus_one_before_the_body(self) -> None:
        while_stmt = _while(_module(lambda: free_slot_count()))
        body = while_stmt["body"]
        ptr = _ptr_id(while_stmt)
        bind = _cp_stores(body)[0]
        self.assertNotEqual(bind["src_var"], ptr)
        link = bind["src_var"]
        self.assertTrue(
            any(
                s["kind"] == "vartransfer"
                and s["dst_var"] == link
                and s["src_var"] == ptr
                for s in body
            ),
            "the forward link must be materialised from ptr",
        )
        self.assertTrue(
            any(act["value"] == 1 for act in _acts_targeting(body, link)),
            "the forward link adds +1 to ptr",
        )


class CycleGuardTest(unittest.TestCase):
    def test_remaining_starts_at_scan_max_and_decrements_per_node(self) -> None:
        module = _module(lambda: free_slot_count())
        while_stmt = _while(module)
        body = while_stmt["body"]

        remaining = _remaining_id(module)
        self.assertEqual(_var_init(module, remaining), 0)
        init_acts = [
            act
            for act in _acts_anywhere(module["stmts"])
            if act.get("value") == CUNIT_FREE_SCAN_MAX
            and any(
                r.get("target_var") == remaining for r in act.get("relocs", [])
            )
        ]
        self.assertEqual(len(init_acts), 1, "the guard starts at 1699")
        acts = _acts_targeting(body, remaining)
        self.assertEqual(len(acts), 1)
        # `Var.isub(1)` emits the two's-complement add; the value is the
        # wrapped -1, which is exactly one subtraction.
        self.assertEqual(acts[0]["value"], 0xFFFFFFFF)
        self.assertEqual(acts[0]["modifier"], Add)

    def test_remaining_zero_sets_done_and_stops_the_advance(self) -> None:
        module = _module(lambda: free_slot_count())
        while_stmt = _while(module)
        body = while_stmt["body"]
        done = _done_id(while_stmt)
        remaining = _remaining_id(module)

        stop = next(
            s
            for s in body
            if s["kind"] == "when"
            and len(s["conds"]) == 1
            and s["conds"][0]["relocs"][0]["target_var"] == remaining
            and s["conds"][0]["amount"] == 0
        )
        self.assertEqual(
            [(a["value"], a["modifier"]) for a in _acts_targeting(stop["body"], done)],
            [(1, SetTo)],
        )


class OneCallSiteTest(unittest.TestCase):
    def test_second_call_is_rejected_before_any_emission(self) -> None:
        errors: list[EudslError] = []

        def prog() -> None:
            marker = Var(0)
            free_slot_count()

            def second_body() -> None:
                marker.iadd(1)

            try:
                free_list_walk(second_body)
            except EudslError as exc:  # noqa: PERF203 - assertion under test
                errors.append(exc)

        module = _module(prog)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "EUDSL-BOUNDS")
        self.assertIn("ADR 0015", str(errors[0]))
        self.assertIn("one call site per module", str(errors[0]))
        # The rejected call emitted nothing: its body never ran, so the marker
        # var is untouched, and there is exactly one loop.
        self.assertEqual(_acts_targeting(flatten_stmts(module["stmts"]), 0), [])
        self.assertEqual(len(_whiles(module)), 1)

    def test_free_slot_count_consumes_the_call_site(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(
                lambda: (
                    free_slot_count(),
                    free_list_walk(lambda: None),
                )
            )
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")
        self.assertIn("ADR 0015", str(ctx.exception))

    def test_free_slot_count_after_a_walk_leaves_no_var_or_hir(self) -> None:
        control = _module(lambda: free_list_walk(lambda: None))
        errors: list[EudslError] = []

        def prog() -> None:
            free_list_walk(lambda: None)
            try:
                free_slot_count()
            except EudslError as exc:  # noqa: PERF203 - assertion under test
                errors.append(exc)

        module = _module(prog)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "EUDSL-BOUNDS")
        # The rejected call claims the call site before allocating its count
        # var, so it leaves no variable and no HIR behind.
        self.assertEqual(module["vars"], control["vars"])
        self.assertEqual(len(_whiles(module)), 1)
        self.assertEqual(module["stmts"], control["stmts"])

    def test_two_explicit_walks_are_rejected(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(
                lambda: (
                    free_list_walk(lambda: None),
                    free_list_walk(lambda: None),
                )
            )
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_free_list_empty_does_not_consume_the_budget(self) -> None:
        from py.capacity import free_list_empty

        def prog() -> None:
            free_list_empty()
            free_list_empty()
            free_slot_count()
            free_list_empty()

        module = _module(prog)
        self.assertEqual(len(_whiles(module)), 1)

    def test_a_fresh_module_gets_a_fresh_call_site(self) -> None:
        first = _module(lambda: free_slot_count())
        second = _module(lambda: free_slot_count())
        self.assertEqual(len(_whiles(first)), 1)
        self.assertEqual(len(_whiles(second)), 1)


class DiagnosticsTest(unittest.TestCase):
    def test_body_must_be_callable(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: free_list_walk(5))  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_max_nodes_outside_the_pool_is_bounds(self) -> None:
        for value in (0, 1700):
            with self.subTest(value=value):
                with self.assertRaises(EudslError) as ctx:
                    build_module(
                        lambda value=value: free_list_walk(
                            lambda: None, max_nodes=value
                        )
                    )
                self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_max_nodes_bool_is_type(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(
                lambda: free_list_walk(lambda: None, max_nodes=True)  # type: ignore[arg-type]
            )
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_outside_a_builder_is_scope(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            free_list_walk(lambda: None)
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")


class BreakTest(unittest.TestCase):
    def test_returning_break_stops_after_the_first_node(self) -> None:
        def prog() -> None:
            count = Var(0)

            def body():
                count.iadd(1)
                return BREAK

            free_list_walk(body)

        module = _module(prog)
        while_stmt = _while(module)
        done = _done_id(while_stmt)
        body = while_stmt["body"]
        marker_index = next(
            i
            for i, stmt in enumerate(body)
            if stmt["kind"] == "act"
            and any(r.get("target_var") == 0 for r in stmt.get("relocs", []))
        )
        done_indices = [
            i
            for i, stmt in enumerate(body)
            if stmt["kind"] == "act"
            and any(r.get("target_var") == done for r in stmt.get("relocs", []))
        ]
        self.assertEqual(len(done_indices), 1)
        self.assertLess(marker_index, done_indices[0])

    def test_returning_anything_else_continues(self) -> None:
        def prog() -> None:
            count = Var(0)

            def body():
                count.iadd(1)
                return True

            free_list_walk(body)

        module = _module(prog)
        while_stmt = _while(module)
        self.assertEqual(_acts_targeting(while_stmt["body"], _done_id(while_stmt)), [])


if __name__ == "__main__":
    unittest.main()
