"""T-WP7: ``for_each_unit_list`` — U/T/S/D walks with a ``Break`` sentinel.

The walk is a runtime ``while`` over the global CUnit list: one ambient
CurrentPlayer save per site, the 20T CP-relative reader per node, and an
unconditional restore after the loop. ``U``/``T``/``S`` follow ``next``
(``ptr + 1``); ``D`` starts at the tail and follows ``prev`` (``ptr + 0``).
``F`` is not a unit list and is rejected.
"""

from __future__ import annotations

import json
import unittest

from py import (
    BREAK,
    EudslError,
    UNIT_LIST_D,
    UNIT_LIST_S,
    UNIT_LIST_T,
    UNIT_LIST_U,
    UnitLoop,
    Var,
    build_module,
    for_each_unit_list,
)
from py.cp import CP_ADDR
from py.hir import AtLeast, AtMost, Exactly
from py.tests.helpers import flatten_stmts
from py.tests.units_hir import (
    UNIT_LIST_HEADS,
    UNIT_LIST_LINK_OFFSETS,
    acts_targeting,
    cp_stores,
    hir_module,
    loop_done_id,
    loop_ptr_id,
    var_init,
    walk_while,
)


class ListConstantsTest(unittest.TestCase):
    def test_list_names_are_the_four_unit_lists(self) -> None:
        self.assertEqual(
            (UNIT_LIST_U, UNIT_LIST_T, UNIT_LIST_S, UNIT_LIST_D),
            ("U", "T", "S", "D"),
        )

    def test_camel_case_alias_is_the_same_helper(self) -> None:
        from py import forEachUnitList, for_each_unit_list

        self.assertIs(forEachUnitList, for_each_unit_list)


class LoopShapeTest(unittest.TestCase):
    def test_one_ambient_read_one_while_and_one_final_restore(self) -> None:
        module = hir_module(
            lambda: for_each_unit_list(UNIT_LIST_U, lambda unit, loop: None)
        )
        funcs = module["funcs"]
        self.assertEqual(sorted(len(fn["body"]) for fn in funcs), [20, 33])
        ambient_id = next(fn["id"] for fn in funcs if len(fn["body"]) == 33)

        calls = [s for s in flatten_stmts(module["stmts"]) if s["kind"] == "call"]
        self.assertEqual(
            len([c for c in calls if c["func"] == ambient_id]),
            1,
            "the 33T ambient CP read must happen exactly once per site",
        )

        while_stmt = walk_while(module)
        self.assertEqual(
            [c["amount"] for c in while_stmt["conds"]],
            [19025, 161657, 0],
        )
        self.assertEqual(
            [c["comparison"] for c in while_stmt["conds"]],
            [AtLeast, AtMost, Exactly],
        )

        # The last top-level statement is the unconditional CP restore, even
        # though the loop body is gated on `done`.
        ambient_call = next(
            s
            for s in module["stmts"]
            if s["kind"] == "call" and s["func"] == ambient_id
        )
        last = module["stmts"][-1]
        self.assertEqual(last["kind"], "varstore")
        self.assertEqual(last["dst_addr"], CP_ADDR)
        self.assertEqual(last["src_var"], ambient_call["dst"])

    def test_head_store_uses_the_documented_head_literal(self) -> None:
        for list_name, head in UNIT_LIST_HEADS.items():
            with self.subTest(list_name=list_name):
                module = hir_module(
                    lambda list_name=list_name: for_each_unit_list(
                        list_name, lambda unit, loop: None
                    )
                )
                stores = cp_stores(module["stmts"])
                head_stores = [
                    s
                    for s in stores
                    if var_init(module, s["src_var"]) == head
                ]
                self.assertEqual(len(head_stores), 1)
                self.assertIn(head, [v["init"] for v in module["vars"]])


class LinkDirectionTest(unittest.TestCase):
    def test_forward_lists_read_next_and_dying_reads_prev(self) -> None:
        for list_name in UNIT_LIST_HEADS:
            with self.subTest(list_name=list_name):
                module = hir_module(
                    lambda list_name=list_name: for_each_unit_list(
                        list_name, lambda unit, loop: None
                    )
                )
                while_stmt = walk_while(module)
                ptr = loop_ptr_id(while_stmt)
                body = while_stmt["body"]
                bind = cp_stores(body)[0]

                if UNIT_LIST_LINK_OFFSETS[list_name] == 1:
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
                        any(act["value"] == 1 for act in acts_targeting(body, link)),
                        "the forward link adds +1 to ptr",
                    )
                else:
                    self.assertEqual(
                        bind["src_var"],
                        ptr,
                        "D must read prev at ptr + 0, not ptr + 1",
                    )

    def test_dying_head_is_the_tail_literal(self) -> None:
        module = hir_module(
            lambda: for_each_unit_list(UNIT_LIST_D, lambda unit, loop: None)
        )
        head_stores = [
            s
            for s in cp_stores(module["stmts"])
            if var_init(module, s["src_var"]) == 161842
        ]
        self.assertEqual(len(head_stores), 1)

    def test_free_list_dwords_never_appear_in_the_hir(self) -> None:
        for list_name in UNIT_LIST_HEADS:
            with self.subTest(list_name=list_name):
                module = hir_module(
                    lambda list_name=list_name: for_each_unit_list(
                        list_name, lambda unit, loop: None
                    )
                )
                text = json.dumps(module)
                self.assertNotIn("161845", text)
                self.assertNotIn("161846", text)


class DiagnosticsTest(unittest.TestCase):
    def test_free_list_is_bounds_not_type(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(
                lambda: for_each_unit_list("F", lambda unit, loop: None)
            )
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")
        self.assertIn("free list", str(ctx.exception))

    def test_unknown_or_non_string_list_names_are_type_errors(self) -> None:
        for value in ("f", "X", 5, True):
            with self.subTest(value=value):
                with self.assertRaises(EudslError) as ctx:
                    build_module(
                        lambda value=value: for_each_unit_list(
                            value, lambda unit, loop: None
                        )
                    )
                self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_reentrant_must_be_a_plain_bool(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(
                lambda: for_each_unit_list(
                    UNIT_LIST_U, lambda unit, loop: None, reentrant=1
                )
            )
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_body_must_be_callable(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: for_each_unit_list(UNIT_LIST_U, 5))
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_break_outside_a_walk_is_a_type_error(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            build_module(lambda: UnitLoop(UNIT_LIST_U, Var(0)).break_())
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")


class BreakContractTest(unittest.TestCase):
    def test_break_emits_done_set_and_can_be_called_twice(self) -> None:
        def prog() -> None:
            counter = Var(0)

            def body(unit, loop) -> None:
                counter.iadd(1)
                loop.break_()
                loop.break_()

            for_each_unit_list(UNIT_LIST_U, body)

        module = hir_module(prog)
        while_stmt = walk_while(module)
        done = loop_done_id(while_stmt)
        acts = acts_targeting(while_stmt["body"], done)
        self.assertEqual(len(acts), 2)
        self.assertTrue(all(act["value"] == 1 for act in acts))

    def test_returning_break_emits_done_after_the_body(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            marker = Var(0)
            ids["marker"] = marker.id

            def body(unit, loop):
                marker.iadd(1)
                return BREAK

            for_each_unit_list(UNIT_LIST_U, body)

        module = hir_module(prog)
        while_stmt = walk_while(module)
        done = loop_done_id(while_stmt)
        marker_index = next(
            i
            for i, stmt in enumerate(while_stmt["body"])
            if stmt["kind"] == "act"
            and any(
                r.get("target_var") == ids["marker"]
                for r in stmt.get("relocs", [])
            )
        )
        done_indices = [
            i
            for i, stmt in enumerate(while_stmt["body"])
            if stmt["kind"] == "act"
            and any(r.get("target_var") == done for r in stmt.get("relocs", []))
        ]
        self.assertEqual(len(done_indices), 1)
        self.assertLess(marker_index, done_indices[0])

    def test_returning_true_continues_and_never_sets_done(self) -> None:
        def prog() -> None:
            marker = Var(0)

            def body(unit, loop):
                marker.iadd(1)
                return True

            for_each_unit_list(UNIT_LIST_U, body)

        module = hir_module(prog)
        while_stmt = walk_while(module)
        self.assertEqual(acts_targeting(while_stmt["body"], loop_done_id(while_stmt)), [])


class ReentrantOrderTest(unittest.TestCase):
    def _reader_and_body_indices(self, reentrant: bool) -> tuple[int, int]:
        ids: dict[str, int] = {}

        def prog() -> None:
            marker = Var(0)
            ids["marker"] = marker.id

            def body(unit, loop) -> None:
                marker.iadd(1)

            for_each_unit_list(UNIT_LIST_U, body, reentrant=reentrant)

        module = hir_module(prog)
        body = walk_while(module)["body"]
        reader_index = next(i for i, s in enumerate(body) if s["kind"] == "call")
        marker_index = next(
            i
            for i, s in enumerate(body)
            if s["kind"] == "act"
            and any(r.get("target_var") == ids["marker"] for r in s.get("relocs", []))
        )
        return reader_index, marker_index

    def test_reentrant_reads_the_link_before_the_body(self) -> None:
        reader, marker = self._reader_and_body_indices(True)
        self.assertLess(reader, marker)

    def test_default_reads_the_link_after_the_body(self) -> None:
        reader, marker = self._reader_and_body_indices(False)
        self.assertGreater(reader, marker)


if __name__ == "__main__":
    unittest.main()
