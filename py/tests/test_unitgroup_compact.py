"""Issue #101: opt-in compact ``UnitGroup`` mode.

The compact mode emits the ``for_each`` body once and drives it with a
runtime CurrentPlayer cursor loop over the same page slots. This suite pins
the semantics, the save/restore contract, the Py/TS parity fixture, and the
static/executed cost shape.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from py import (
    AtLeast,
    CPCursor,
    DeathsX,
    EPD_CURRENT_PLAYER,
    Exactly,
    FieldSpec,
    MemFieldHandle,
    Never,
    SetDeaths,
    SetTo,
    Subtract,
    UnitGroup,
    Var,
    build_module,
    defineMemView,
    lazyUnitGroup,
    when,
    with_cp,
    while_loop,
)
from py.tests.helpers import flatten_stmts, run_sim

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_CP_REGISTER_SCHEMA = defineMemView(
    "CurrentPlayerRegister",
    4,
    {"value": FieldSpec(0, 4)},
)


def _read_cp_register() -> Var:
    """Materialise the CurrentPlayer register dword into a ``Var``."""
    return MemFieldHandle(
        _CP_REGISTER_SCHEMA,
        FieldSpec(0, 4),
        EPD_CURRENT_PLAYER,
        "value",
    ).read()


def _set_slots(group: UnitGroup, values: dict[int, int]) -> None:
    for index, value in values.items():
        group.slots[index].set(value)


class CompactUnitGroupTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Iteration semantics
    # ------------------------------------------------------------------

    def test_for_each_visits_every_live_slot_once_in_slot_order(self) -> None:
        ids: dict[str, int] = {}
        order: list[int] = []

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            phase = Var(0)
            count = Var(0)
            total = Var(0)

            def body(epd: Var) -> None:
                count.iadd(1)
                total.iadd(epd)
                order.append(len(order))

            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000), g.add(3000)))
            when([phase.eq(1)], lambda: g.for_each(body))
            phase.iadd(1)
            ids.update(count=count.id, total=total.id)

        report = run_sim(build_module(prog).to_json(), 3)
        self.assertEqual(report["var_values"][ids["count"]], 3)
        self.assertEqual(report["var_values"][ids["total"]], 6000)
        self.assertEqual(len(order), 1, "body must be emitted exactly once")

    def test_for_each_empty_group_does_not_run_and_restores_cp(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            before = Var(0)
            after = Var(0)
            count = Var(0)
            with with_cp(1234):
                before.set(_read_cp_register())
                g.for_each(lambda epd: count.iadd(1))
                after.set(_read_cp_register())
            ids.update(before=before.id, after=after.id, count=count.id)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["count"]], 0)
        self.assertEqual(report["var_values"][ids["before"]], 1234)
        self.assertEqual(report["var_values"][ids["after"]], 1234)

    def test_for_each_page_boundaries(self) -> None:
        for capacity in (1, 63, 64, 65, 127, 128, 1000):
            with self.subTest(capacity=capacity):
                indexes = sorted({0, min(63, capacity - 1), capacity - 1})
                expected = sum(1000 + i for i in indexes)
                ids: dict[str, int] = {}

                def prog() -> None:
                    g = UnitGroup(capacity, autoPrune=False, compact=True)
                    _set_slots(g, {i: 1000 + i for i in indexes})
                    count = Var(0)
                    total = Var(0)

                    def body(epd: Var) -> None:
                        count.iadd(1)
                        total.iadd(epd)

                    g.for_each(body)
                    ids.update(count=count.id, total=total.id)

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["var_values"][ids["count"]], len(indexes))
                self.assertEqual(report["var_values"][ids["total"]], expected)

    def test_with_cp_inside_body_does_not_break_the_cursor(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            _set_slots(g, {0: 10, 1: 20})
            count = Var(0)
            total = Var(0)
            seen = Var(0)

            def body(epd: Var) -> None:
                when(
                    [epd.eq(10)],
                    lambda: with_cp(999, lambda cp: seen.set(cp.at(0).read())),
                )
                count.iadd(1)
                total.iadd(epd)

            g.for_each(body)
            ids.update(count=count.id, total=total.id, seen=seen.id)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["count"]], 2)
        self.assertEqual(report["var_values"][ids["total"]], 30)
        self.assertEqual(report["var_values"][ids["seen"]], 0)

    def test_nested_compact_loops(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            outer = UnitGroup(2, autoPrune=False, compact=True)
            inner = UnitGroup(2, autoPrune=False, compact=True)
            _set_slots(outer, {0: 10, 1: 20})
            _set_slots(inner, {0: 30, 1: 40})
            outer_count = Var(0)
            inner_count = Var(0)
            inner_total = Var(0)

            def inner_body(epd: Var) -> None:
                inner_count.iadd(1)
                inner_total.iadd(epd)

            def outer_body(epd: Var) -> None:
                outer_count.iadd(1)
                inner.for_each(inner_body)

            outer.for_each(outer_body)
            ids.update(
                outer_count=outer_count.id,
                inner_count=inner_count.id,
                inner_total=inner_total.id,
            )

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["outer_count"]], 2)
        self.assertEqual(report["var_values"][ids["inner_count"]], 4)
        self.assertEqual(report["var_values"][ids["inner_total"]], 140)

    def test_mutation_during_iteration_keeps_hole_order(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            phase = Var(0)
            count = Var(0)
            total = Var(0)

            def body(epd: Var) -> None:
                count.iadd(1)
                total.iadd(epd)
                when([epd.eq(20)], lambda: g.remove(20))

            when([phase.eq(0)], lambda: (g.add(10), g.add(20), g.add(30)))
            when([phase.eq(1)], lambda: g.for_each(body))
            phase.iadd(1)
            ids.update(
                count=count.id,
                total=total.id,
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
                s2=g.slots[2].id,
            )

        report = run_sim(build_module(prog).to_json(), 2)
        self.assertEqual(report["var_values"][ids["count"]], 3)
        self.assertEqual(report["var_values"][ids["total"]], 60)
        self.assertEqual(report["var_values"][ids["length"]], 2)
        self.assertEqual(report["var_values"][ids["s0"]], 10)
        self.assertEqual(report["var_values"][ids["s1"]], 0)
        self.assertEqual(report["var_values"][ids["s2"]], 30)

    def test_add_during_iteration_is_visible_to_later_slots(self) -> None:
        results = {}

        for compact in (False, True):
            ids: dict[str, int] = {}

            def prog() -> None:
                g = UnitGroup(3, autoPrune=False, compact=compact)
                g.add(10)
                count = Var(0)
                total = Var(0)

                def body(epd: Var) -> None:
                    count.iadd(1)
                    total.iadd(epd)
                    when([epd.eq(10)], lambda: g.add(20))

                g.for_each(body)
                ids.update(
                    count=count.id,
                    total=total.id,
                    length=g.length.id,
                    s0=g.slots[0].id,
                    s1=g.slots[1].id,
                    s2=g.slots[2].id,
                )

            report = run_sim(build_module(prog).to_json(), 1)
            results[compact] = {
                key: report["var_values"][value] for key, value in ids.items()
            }

        self.assertEqual(results[False], results[True])
        self.assertEqual(results[True]["count"], 2)
        self.assertEqual(results[True]["total"], 30)
        self.assertEqual(results[True]["length"], 2)
        self.assertEqual(
            [results[True][key] for key in ("s0", "s1", "s2")],
            [10, 20, 0],
        )

    def test_for_each_callback_receives_a_read_only_snapshot(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            g.add(10)
            g.add(20)
            count = Var(0)
            total = Var(0)

            def body(epd: Var) -> None:
                count.iadd(1)
                total.iadd(epd)
                # Compact bodies get a scratch snapshot, not the slot Var.
                epd.set(0)

            g.for_each(body)
            ids.update(
                count=count.id,
                total=total.id,
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["count"]], 2)
        self.assertEqual(values[ids["total"]], 30)
        self.assertEqual(values[ids["length"]], 2)
        self.assertEqual([values[ids["s0"]], values[ids["s1"]]], [10, 20])

    # ------------------------------------------------------------------
    # Mutation semantics
    # ------------------------------------------------------------------

    def test_add_remove_clear_semantics(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            phase = Var(0)
            snap = [Var(0) for _ in range(8)]

            def capture(offset: int) -> None:
                snap[offset].set(g.length)
                snap[offset + 1].set(g.slots[0])
                snap[offset + 2].set(g.slots[1])
                snap[offset + 3].set(g.slots[2])

            when(
                [phase.eq(0)],
                lambda: (g.add(10), g.add(20), g.add(20), g.add(30)),
            )
            when([phase.eq(1)], lambda: capture(0))
            when([phase.eq(2)], lambda: g.remove(20))
            when([phase.eq(3)], lambda: capture(4))
            when([phase.eq(4)], lambda: g.clear())
            phase.iadd(1)
            ids.update(
                phase=phase.id,
                length=snap[0].id,
                s0=snap[1].id,
                s1=snap[2].id,
                s2=snap[3].id,
                r_length=snap[4].id,
                r_s0=snap[5].id,
                r_s1=snap[6].id,
                r_s2=snap[7].id,
            )

        report = run_sim(build_module(prog).to_json(), 5)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 3)
        self.assertEqual([values[ids[k]] for k in ("s0", "s1", "s2")], [10, 20, 20])
        self.assertEqual(values[ids["r_length"]], 1)
        self.assertEqual([values[ids[k]] for k in ("r_s0", "r_s1", "r_s2")], [10, 0, 0])

        # Phase 4 clears everything; the final module state is all zero.
        self.assertEqual(values[ids["phase"]], 5)

    def test_add_fills_first_empty_slot_and_is_a_noop_when_full(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            g.add(10)
            g.add(20)
            g.add(30)
            g.remove(10)
            g.add(40)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 2)
        self.assertEqual(values[ids["s0"]], 40)
        self.assertEqual(values[ids["s1"]], 20)

    def test_add_and_remove_accept_runtime_var_epds(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            epd = Var(4000)
            phase = Var(0)
            when([phase.eq(0)], lambda: g.add(epd))
            when([phase.eq(1)], lambda: (g.add(5000), g.add(epd)))
            when([phase.eq(2)], lambda: g.remove(epd))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
                s2=g.slots[2].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 1)
        self.assertEqual(
            [values[ids[k]] for k in ("s0", "s1", "s2")],
            [0, 5000, 0],
        )

    def test_remove_clears_every_duplicate(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            g.add(5)
            g.add(5)
            g.add(7)
            g.remove(5)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
                s2=g.slots[2].id,
            )

        report = run_sim(build_module(prog).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 1)
        self.assertEqual([values[ids[k]] for k in ("s0", "s1", "s2")], [0, 0, 7])

    def test_clear_zeroes_storage_and_length(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            g.add(1000)
            g.add(2000)
            g.clear()
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 1)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 0)
        self.assertEqual(values[ids["s0"]], 0)
        self.assertEqual(values[ids["s1"]], 0)

    # ------------------------------------------------------------------
    # autoPrune
    # ------------------------------------------------------------------

    def test_compact_auto_prune_default_sprite_field(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            # 1000 is alive: sprite field EPD 1000 + 3 is non-zero.
            SetDeaths(1003, SetTo, 1, 0)
            g = UnitGroup(2, compact=True)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 1)
        self.assertEqual(values[ids["s0"]], 1000)
        self.assertEqual(values[ids["s1"]], 0)

    def test_compact_auto_prune_unlimiter_field(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            # Unlimiter order field is unit EPD + 0x13, masked with 0xFF00.
            SetDeaths(1000 + 0x13, SetTo, 0x0100, 0)
            g = UnitGroup(2, compact=True, unlimiter=True)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 1)
        self.assertEqual(values[ids["s0"]], 1000)
        self.assertEqual(values[ids["s1"]], 0)

    def test_compact_auto_prune_custom_dead_check(self) -> None:
        ids: dict[str, int] = {}
        checks: list[Var] = []

        def keep_all(check: Var) -> object:
            checks.append(check)
            return Never()

        def prog() -> None:
            g = UnitGroup(2, compact=True, deadCheck=keep_all)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 2)
        self.assertEqual(values[ids["s0"]], 1000)
        self.assertEqual(values[ids["s1"]], 2000)
        self.assertEqual(len(checks), 1, "compact prune emits the callback once")

    def test_compact_auto_prune_custom_dead_check_can_kill(self) -> None:
        ids: dict[str, int] = {}

        def kill_all(check: Var) -> object:
            # Reads EPD 0, which is zero in the simulator.
            return DeathsX(0, Exactly, 0, 0, 0xFFFFFFFF)

        def prog() -> None:
            g = UnitGroup(2, compact=True, deadCheck=kill_all)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 0)
        self.assertEqual(values[ids["s0"]], 0)
        self.assertEqual(values[ids["s1"]], 0)

    def test_compact_custom_dead_check_hir_is_live_gated(self) -> None:
        # A predicate that emits HIR must be emitted inside the per-slot live
        # gate, matching the unroll. With no live slots its HIR never runs.
        empty_ids: dict[str, int] = {}

        def empty_prog() -> None:
            hits = Var(0)

            def count_hit(check: Var) -> object:
                hits.iadd(1)
                return Never()

            UnitGroup(3, compact=True, deadCheck=count_hit)
            empty_ids["hits"] = hits.id

        empty_report = run_sim(build_module(empty_prog).to_json(), 2)
        self.assertEqual(empty_report["var_values"][empty_ids["hits"]], 0)

        # One live slot: the predicate HIR runs once per trigger loop for the
        # live slot, not once per storage slot.
        live_ids: dict[str, int] = {}

        def live_prog() -> None:
            hits = Var(0)

            def count_hit(check: Var) -> object:
                hits.iadd(1)
                return Never()

            g = UnitGroup(3, compact=True, deadCheck=count_hit)
            g.slots[0].set(1000)
            live_ids["hits"] = hits.id

        live_report = run_sim(build_module(live_prog).to_json(), 3)
        self.assertEqual(live_report["var_values"][live_ids["hits"]], 2)

    def test_compact_auto_prune_can_be_disabled(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(2, autoPrune=False, compact=True)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)
            ids.update(
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
            )

        report = run_sim(build_module(prog).to_json(), 3)
        values = report["var_values"]
        self.assertEqual(values[ids["length"]], 2)
        self.assertEqual(values[ids["s0"]], 1000)
        self.assertEqual(values[ids["s1"]], 2000)

    # ------------------------------------------------------------------
    # Lazy handles and the default path
    # ------------------------------------------------------------------

    def test_lazy_compact_flag_is_a_pure_read(self) -> None:
        ug = lazyUnitGroup(3, compact=True)
        self.assertEqual(ug.capacity, 3)
        self.assertTrue(ug.compact)
        self.assertTrue(ug.autoPrune)
        self.assertFalse(ug.unlimiter)

        plain = lazyUnitGroup(2)
        self.assertFalse(plain.compact)

        ids: dict[str, int] = {}

        def prog() -> None:
            count = Var(0)
            ug.for_each(lambda epd: count.iadd(1))
            ids.update(count=count.id)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["count"]], 0)

    def test_lazy_compact_group_exercises_prune_add_remove_and_clear(self) -> None:
        ids: dict[str, int] = {}
        g = lazyUnitGroup(3, compact=True)

        def prog() -> None:
            # Resolve at top level so the deferred autoPrune sweep is not
            # emitted inside a conditional branch.
            _ = g.length
            # 1000 and 3000 stay alive through autoPrune; 2000 dies.
            SetDeaths(1003, SetTo, 1, 0)
            SetDeaths(3003, SetTo, 1, 0)
            phase = Var(0)
            seen = Var(0)
            total = Var(0)

            def body(epd: Var) -> None:
                seen.iadd(1)
                total.iadd(epd)

            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            when([phase.eq(1)], lambda: g.for_each(body))
            when([phase.eq(2)], lambda: g.add(3000))
            when([phase.eq(3)], lambda: g.remove(1000))
            when([phase.eq(4)], lambda: g.for_each(body))
            when([phase.eq(5)], lambda: g.clear())
            phase.iadd(1)
            ids.update(
                seen=seen.id,
                total=total.id,
                length=g.length.id,
                s0=g.slots[0].id,
                s1=g.slots[1].id,
                s2=g.slots[2].id,
            )

        report = run_sim(build_module(prog).to_json(), 6)
        values = report["var_values"]
        # autoPrune removed 2000 before the first for_each; remove(1000)
        # left 3000; clear zeroed the group.
        self.assertEqual(values[ids["seen"]], 2)
        self.assertEqual(values[ids["total"]], 4000)
        self.assertEqual(values[ids["length"]], 0)
        self.assertEqual(
            [values[ids[key]] for key in ("s0", "s1", "s2")],
            [0, 0, 0],
        )

    def test_default_unit_group_hir_is_byte_identical(self) -> None:
        def prog() -> None:
            g = UnitGroup(2)
            phase = Var(0)
            when([phase.eq(0)], lambda: (g.add(1000), g.add(2000)))
            phase.iadd(1)

        digest = hashlib.sha256(build_module(prog).to_json().encode()).hexdigest()
        self.assertEqual(
            digest,
            "7522123dc49c7a0a3ad2f8f43dd49f04a868b36564bb53ccb9028970f69b21e0",
        )

    def test_compact_body_is_emitted_once_at_static_level(self) -> None:
        ids: dict[str, int] = {}
        compact_counts: dict[str, int] = {}

        def compact_prog() -> None:
            g = UnitGroup(4, autoPrune=False, compact=True)
            _set_slots(g, {0: 1000, 1: 2000, 2: 3000, 3: 4000})
            out = Var(0)

            def body(epd: Var) -> None:
                out.iadd(epd)

            g.for_each(body)
            ids.update(out=out.id)

        def unroll_prog() -> None:
            g = UnitGroup(4, autoPrune=False)
            _set_slots(g, {0: 1000, 1: 2000, 2: 3000, 3: 4000})
            out = Var(0)

            def body(epd: Var) -> None:
                out.iadd(epd)

            g.for_each(body)

        def count_transfers(hir_json: str, dst: int) -> int:
            stmts = json.loads(hir_json)["module"]["stmts"]
            return sum(
                1
                for stmt in flatten_stmts(stmts)
                if stmt["kind"] == "vartransfer" and stmt["dst_var"] == dst
            )

        compact_hir = build_module(compact_prog).to_json()
        unroll_hir = build_module(unroll_prog).to_json()
        compact_counts["transfers"] = count_transfers(compact_hir, ids["out"])
        self.assertEqual(compact_counts["transfers"], 1)
        self.assertGreater(count_transfers(unroll_hir, ids["out"]), 1)

    # ------------------------------------------------------------------
    # Cost shape
    # ------------------------------------------------------------------

    def _cost_program(self, capacity: int, compact: bool) -> dict[str, object]:
        def prog() -> None:
            g = UnitGroup(capacity, autoPrune=False, compact=compact)
            _set_slots(g, {0: 1000, 1: 2000, 2: 3000})
            out = Var(0)

            def body(epd: Var) -> None:
                out.iadd(epd)
                when([epd.eq(1000)], lambda: out.iadd(1))
                when([epd.ge(2000)], lambda: out.iadd(2))
                when([epd.le(3000)], lambda: out.iadd(3))

            g.for_each(body)

        return run_sim(build_module(prog).to_json(), 1)

    def test_compact_static_cost_is_independent_of_capacity(self) -> None:
        small = self._cost_program(64, compact=True)["static_cost"]
        big = self._cost_program(1000, compact=True)["static_cost"]
        unroll = self._cost_program(64, compact=False)["static_cost"]

        # Only storage pages scale with capacity: 1000 slots is 16 pages,
        # 64 slots is one page. The loop/reader scaffolding is shared.
        self.assertLessEqual(big["triggers"] - small["triggers"], 16)
        self.assertLessEqual(
            big["total_bytes"] - small["total_bytes"],
            16 * 2408,
        )
        self.assertLess(big["triggers"], unroll["triggers"])

    def test_compact_executed_cost_and_body_once_per_live_entry(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            g = UnitGroup(4, autoPrune=False, compact=True)
            _set_slots(g, {0: 1000, 1: 2000, 2: 3000, 3: 4000})
            count = Var(0)
            total = Var(0)

            def body(epd: Var) -> None:
                count.iadd(1)
                total.iadd(epd)

            g.for_each(body)
            ids.update(count=count.id, total=total.id)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["count"]], 4)
        self.assertEqual(report["var_values"][ids["total"]], 10000)
        executed = report["executed_cost"]
        self.assertGreater(executed["triggers"], 0)
        self.assertTrue(report["self_targeted_invariant_holds"])

    def test_compact_whole_group_cost_at_capacity_1000(self) -> None:
        def run(mutate: bool) -> dict[str, int]:
            def prog() -> None:
                g = UnitGroup(1000, compact=True)
                g.for_each(lambda epd: None)
                if mutate:
                    g.add(1000)
                    g.remove(1000)
                    g.clear()

            report = run_sim(build_module(prog).to_json(), 1)
            return {
                "triggers": report["static_cost"]["triggers"],
                "bytes": report["static_cost"]["total_bytes"],
                "executed": report["executed_cost"]["triggers"],
            }

        # #97: the compact sweep's two full-dword reads share one module
        # reader: 176T -> 144T static (-32T; the second inline 33T read shrinks
        # to the 6T call sequence, the first site keeps the 33T body and the
        # reader's reset/copy fold into their neighbours). Executed drops
        # 18076 -> 16081 because the per-member inline read chains were
        # replaced by the same shared body reached through the call.
        self.assertEqual(
            run(False),
            {"triggers": 144, "bytes": 346776, "executed": 16081},
        )
        self.assertEqual(
            run(True),
            {"triggers": 175, "bytes": 421452, "executed": 38201},
        )

    # ------------------------------------------------------------------
    # Py/TS parity
    # ------------------------------------------------------------------

    def test_compact_hir_is_byte_identical_with_typescript(self) -> None:
        ts_script = r"""
import {
  UnitGroup, Var, buildModule, when,
} from "./ts/index.ts";

const mod = buildModule(() => {
  const g = new UnitGroup(3, { autoPrune: false, compact: true });
  const phase = Var(0);
  const out = Var(0);

  when([phase.eq(0)], () => {
    g.add(1000);
    g.add(2000);
    g.add(2000);
  });
  when([phase.eq(1)], () => {
    g.for_each((epd) => { out.iadd(epd); });
    g.remove(1000);
    g.add(3000);
  });
  when([phase.eq(2)], () => { g.clear(); });

  const pruned = new UnitGroup(2, { compact: true });
  pruned.add(4000);

  const unlimiter = new UnitGroup(2, { compact: true, unlimiter: true });
  unlimiter.add(5000);

  phase.iadd(1);
});
process.stdout.write(mod.toJson());
"""

        def prog() -> None:
            g = UnitGroup(3, autoPrune=False, compact=True)
            phase = Var(0)
            out = Var(0)

            when(
                [phase.eq(0)],
                lambda: (g.add(1000), g.add(2000), g.add(2000)),
            )
            when(
                [phase.eq(1)],
                lambda: (
                    g.for_each(lambda epd: out.iadd(epd)),
                    g.remove(1000),
                    g.add(3000),
                ),
            )
            when([phase.eq(2)], lambda: g.clear())

            pruned = UnitGroup(2, compact=True)
            pruned.add(4000)

            unlimiter = UnitGroup(2, compact=True, unlimiter=True)
            unlimiter.add(5000)

            phase.iadd(1)

        ts_output = subprocess.check_output(
            ["node", "--input-type=module", "-e", ts_script],
            cwd=REPO_ROOT,
            text=True,
        )
        self.assertEqual(build_module(prog).to_json(), ts_output)


if __name__ == "__main__":
    unittest.main()
