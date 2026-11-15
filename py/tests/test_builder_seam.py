"""Builder seam contract tests (candidate 8, shape C).

These tests pin the builder seam interface directly -- scope entry/exit
flushing, flush ordering, scope-serial invalidation, explicit-registration
ordering, and outside-buildModule fallback -- instead of covering it only
indirectly via whole-program String/temporal tests. They are pins, not
bug fixes: every behaviour asserted here is the current behaviour, and
the two mirror asymmetries (flusher dedup, root pop) are pinned AS-IS
per language, explicitly NOT unified (see the per-language tests below).

One pin was revised, not merely updated: the outside-buildModule var
fallback (candidate-8 shape C) turned out to be the corruption mechanism
behind the aligned-string-padding bug, so the var-scope fix revises that
contract to raising EUDSL-SCOPE. The Trigger fallback pin is unchanged.
"""

from __future__ import annotations

import unittest

from py import (
    EPD,
    FixedString,
    SetDeathsX,
    SetTo,
    Trigger,
    buildModule,
)
from py.trigger import _get_active_module_builder, _register_trigger
from py.var import (
    _alloc_var_slot,
    _get_next_page_slot,
    _get_next_var_slot,
    _get_scope_serial,
)
from py.view import EudslError


def _death_act(value: int) -> dict:
    return SetDeathsX(EPD(0x58A368), SetTo, value, 0, 0xFFFFFFFF)


class TestBuilderSeamPythonDsl(unittest.TestCase):
    def test_flushers_run_in_registration_order_at_scope_boundaries(self) -> None:
        log: list[str] = []

        def body() -> None:
            builder = _get_active_module_builder()
            self.assertIsNotNone(builder)
            builder.register_flusher(lambda _align: log.append("first"))
            builder.register_flusher(lambda _align: log.append("second"))
            builder.push_scope()
            self.assertEqual(log, ["first", "second"])
            builder.pop_scope()
            self.assertEqual(log, ["first", "second", "first", "second"])

        buildModule(body)
        # Module-end flush runs the flushers a third time.
        self.assertEqual(log, ["first", "second"] * 3)

    def test_register_trigger_places_trigger_at_registration_time(self) -> None:
        patch_act = _death_act(0x11111111)
        ids: dict[str, int] = {}

        def body() -> None:
            builder = _get_active_module_builder()
            t0 = Trigger(conds=[], acts=[_death_act(1)], preserved=True)
            ids["t0"] = t0.id
            # Constructed before t1 but registered after it: construction
            # order must NOT determine HIR position.
            ptr = Trigger(
                conds=[], acts=[_death_act(2)], preserved=True, register=False
            )
            ids["ptr"] = ptr.id
            builder.queue_act(patch_act)
            t1 = Trigger(conds=[], acts=[_death_act(3)], preserved=True)
            ids["t1"] = t1.id
            _register_trigger(ptr)

        mod = buildModule(body)
        self.assertLess(ids["ptr"], ids["t1"], "ptr id allocated before t1 id")
        got = [t.id for t in mod.triggers]
        # t0 and t1 register immediately, but the patch act queued between
        # them flushes into a bin-packed trigger BEFORE t1 at assembly
        # (pending acts always flush before the following trigger entry);
        # ptr, which registers last via _register_trigger, still comes
        # last -- after both t1 and its own patching act.
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0], ids["t0"])
        self.assertEqual(got[2], ids["t1"])
        self.assertEqual(got[3], ids["ptr"])
        packed = mod.triggers[1]
        self.assertEqual(len(packed.acts), 1)
        self.assertIs(packed.acts[0], patch_act)

    def test_scope_boundary_invalidates_string_static_positions(self) -> None:
        seen: dict[str, int] = {}

        def body() -> None:
            s = FixedString(8)
            seen["recorded"] = s._static_serial
            seen["before"] = _get_scope_serial()
            self.assertEqual(seen["recorded"], seen["before"])
            builder = _get_active_module_builder()
            builder.push_scope()
            builder.pop_scope()
            seen["after"] = _get_scope_serial()

        buildModule(body)
        # One serial bump per scope change: push_scope AND pop_scope.
        self.assertEqual(seen["after"], seen["before"] + 2)
        self.assertNotEqual(seen["recorded"], seen["after"])

    def test_same_flusher_registered_twice_runs_twice_per_flush(self) -> None:
        # Per-language pin, NOT unified with TypeScript: the Python
        # mirror holds flushers in a list (no dedup), so the same
        # function runs twice per flush. See the TS Set-dedup test.
        count = [0]

        def fn(_align: bool) -> None:
            count[0] += 1

        def body() -> None:
            builder = _get_active_module_builder()
            builder.register_flusher(fn)
            builder.register_flusher(fn)
            builder.push_scope()
            self.assertEqual(count[0], 2)

        buildModule(body)

    def test_root_pop_keeps_the_root_scope(self) -> None:
        # Per-language pin, NOT unified with TypeScript: the Python
        # mirror guards the root scope (returns [] and keeps it), so
        # the queued act survives into the final module.
        # NOTE: act factories auto-queue into the active builder, so
        # the act is created OUTSIDE buildModule; queue_act inside
        # then queues it exactly once instead of twice.
        act = _death_act(9)
        popped: list = []

        def body() -> None:
            builder = _get_active_module_builder()
            builder.queue_act(act)
            popped.extend(builder.pop_scope())
            # Restore a stack frame for symmetry with the TS test.
            builder.push_scope()

        mod = buildModule(body)
        self.assertEqual(popped, [])
        self.assertEqual(len(mod.triggers), 1)
        self.assertEqual(len(mod.triggers[0].acts), 1)
        self.assertIs(mod.triggers[0].acts[0], act)

    def test_two_strings_flush_in_construction_order_at_boundary(self) -> None:
        def body() -> None:
            s1 = FixedString(8)
            s2 = FixedString(8)
            # write() only marks the terminator as needed. At the
            # next scope flush, main's FixedString emits TWO acts
            # per string: one reloc at target_offset 0 (content
            # dword) and one at target_offset 4 (terminator dword).
            # The order of these acts in the final module IS the
            # flusher order; this test pins that order, not act count.
            s1.write("A")
            s2.write("B")
            builder = _get_active_module_builder()
            builder.push_scope()
            builder.pop_scope()

        mod = buildModule(body)
        order = []
        for t in mod.triggers:
            for act in t.acts:
                for reloc in act.get("relocs") or []:
                    if "target_string" in reloc:
                        order.append(reloc["target_string"])
        # Order only: all acts of the first-constructed string
        # precede all acts of the second-constructed string. The
        # literal sequence is [0, 0, 1, 1] on main's FixedString
        # (two acts per string), but pinning that would pin
        # incidental act-splitting, which is FixedString's business,
        # not the seam's.
        first_occurrence = list(dict.fromkeys(order))
        self.assertEqual(first_occurrence, [0, 1])
        self.assertLess(
            max(i for i, v in enumerate(order) if v == 0),
            min(i for i, v in enumerate(order) if v == 1),
        )

    def test_flusher_registered_during_flush_runs_only_at_next_flush(self) -> None:
        # Per-language pin: flush_all iterates a snapshot copy of the
        # list, so a flusher added during a flush is NOT visited in
        # that pass -- unlike the TypeScript mirror's live Set
        # iteration. It runs at the next flush instead.
        log: list[str] = []

        def body() -> None:
            builder = _get_active_module_builder()
            builder.push_scope()
            builder.register_flusher(lambda _align: log.append("mid"))
            builder.pop_scope()
            self.assertEqual(log, ["mid"])
            added: list[bool] = []

            def second(_align: bool) -> None:
                log.append("second")

            def first(_align: bool) -> None:
                log.append("first")
                if not added:
                    added.append(True)
                    builder.register_flusher(second)

            builder.register_flusher(first)
            builder.push_scope()
            # NOTE: flushers persist module-wide, so "mid" re-runs at
            # this flush too; the assertions include those re-runs.
            self.assertEqual(log, ["mid", "mid", "first"])

        buildModule(body)
        # Module-end flush runs all three flushers once more.
        self.assertEqual(
            log, ["mid", "mid", "first", "mid", "first", "second"]
        )

    # CONTRACT REVISION (var-scope fix): the outside-buildModule var fallback
    # below was a candidate-8 pin, but the fallback id collides with the first
    # in-module var and silently corrupts output, so outside var allocation now
    # raises EUDSL-SCOPE like _alloc_string already does. Trigger fallback is
    # unchanged (report-only, out of the var fix scope) and keeps its own pin.
    def test_outside_build_module_var_allocation_raises(self) -> None:
        for fn in (_alloc_var_slot, _get_next_var_slot, _get_next_page_slot):
            with self.assertRaises(EudslError) as cm:
                fn(0) if fn is _alloc_var_slot else fn()
            self.assertEqual(cm.exception.code, "EUDSL-SCOPE")

    def test_outside_build_module_trigger_ids_still_fall_back_to_counters(self) -> None:
        t0 = Trigger(conds=[], acts=[])
        t1 = Trigger(conds=[], acts=[])
        self.assertEqual(t1.id, t0.id + 1)


if __name__ == "__main__":
    unittest.main()
