"""ADR 0028: ``cond_all`` / ``cond_any`` / ``cond_not`` + GatePlan lowering.

Python mirror of the TypeScript HIR contract: the three combinators build the
same runtime predicates as the cond lists they replace, with flag gates only
where OR / opaque NOT need one.  These tests pin the public surface (aliases,
arity, type validation, truthiness refusal), the edge identities, the canonical
prelude shape and cost, raw-``Trigger`` splicing/caps, and that ``while_loop``
re-emits the same plan at the tail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from py import (
    Always,
    CUnit,
    EudslError,
    Never,
    Trigger,
    Var,
    build_module,
    condAll,
    condAny,
    condNot,
    cond_all,
    cond_any,
    cond_not,
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
)
from py.tests.helpers import run_sim, run_static


def _kinds(module) -> list[str]:
    return [stmt["kind"] for stmt in json.loads(module.to_json())["module"]["stmts"]]


def _doc(module) -> dict:
    return json.loads(module.to_json())["module"]


def _static(module) -> dict:
    return run_static(module.to_json())["static_cost"]


def _when_conds(module) -> list[int]:
    return [len(stmt["conds"]) for stmt in _doc(module)["stmts"] if stmt["kind"] == "when"]


class CombinatorSurfaceTests(unittest.TestCase):
    def test_camel_case_aliases_are_the_same_objects(self) -> None:
        self.assertIs(condAll, cond_all)
        self.assertIs(condAny, cond_any)
        self.assertIs(condNot, cond_not)

        def prog() -> None:
            a = Var(1)
            b = Var(1)
            hit = Var(0)
            when(condAll(a.eq(1), b.eq(1)), lambda: hit.iadd(1))
            when(condAny(a.eq(0), b.eq(1)), lambda: hit.iadd(1))
            when(condNot(a.eq(0)), lambda: hit.iadd(1))

        run_sim(build_module(prog).to_json(), 1)

    def test_cond_all_empty_splices_to_no_conditions(self) -> None:
        def with_combinator() -> None:
            hit = Var(0)
            when(cond_all(), lambda: hit.iadd(1))

        def with_empty_list() -> None:
            hit = Var(0)
            when([], lambda: hit.iadd(1))

        self.assertEqual(
            build_module(with_combinator).to_json(),
            build_module(with_empty_list).to_json(),
        )

        def raw() -> None:
            trig = Trigger(conds=[cond_all()], acts=[])
            self.assertEqual(len(trig.conds), 0)

        build_module(raw)

    def test_cond_any_empty_is_never(self) -> None:
        def with_combinator() -> None:
            hit = Var(0)
            when(cond_any(), lambda: hit.iadd(1))

        def with_never() -> None:
            hit = Var(0)
            when([Never()], lambda: hit.iadd(1))

        self.assertEqual(
            build_module(with_combinator).to_json(),
            build_module(with_never).to_json(),
        )

        def raw() -> None:
            trig = Trigger(conds=[cond_any()], acts=[])
            self.assertEqual(len(trig.conds), 1)
            self.assertEqual(trig.conds[0]["condtype"], 23)

        build_module(raw)

    def test_empty_nesting_identities_collapse_before_planning(self) -> None:
        def build(make) -> str:
            def prog() -> None:
                hit = Var(0)
                when(make(), lambda: hit.iadd(1))

            return build_module(prog).to_json()

        zero = build(cond_all)
        never = build(lambda: Never())
        self.assertEqual(build(lambda: cond_any(cond_all())), zero)
        self.assertEqual(build(lambda: cond_not(cond_any())), zero)
        self.assertEqual(build(lambda: cond_all(cond_all())), zero)
        self.assertEqual(build(lambda: cond_not(cond_all())), never)
        self.assertEqual(build(lambda: cond_not(cond_all(cond_all()))), never)

    def test_cond_any_flattens_same_kind_nesting_onto_one_flag(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            a = Var(1)
            b = Var(1)
            c = Var(1)
            hit = Var(0)
            ids["hit"] = hit.id
            when(
                cond_any(cond_any(a.eq(1), b.eq(1)), c.eq(1)),
                lambda: hit.iadd(1),
            )

        module = build_module(prog)
        # a, b, c, hit + exactly one shared flag.
        self.assertEqual(len(_doc(module)["vars"]), 5)
        resets = [
            stmt["acts"][0]["relocs"][0].get("target_var")
            for stmt in _doc(module)["stmts"]
            if stmt["kind"] == "trigger"
        ]
        self.assertEqual(resets, [4])
        static = _static(module)
        self.assertEqual(static["triggers"], 5)  # N=3 -> N+2T
        self.assertEqual(static["conditions"], 4)
        self.assertEqual(run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 1)

    def test_cond_not_double_negation_returns_the_input(self) -> None:
        x = cond_all(Always())
        y = cond_any(Never())
        opaque = Always()
        for value in (x, y, opaque):
            with self.subTest(value=type(value).__name__):
                self.assertIs(cond_not(cond_not(value)), value)

    def test_combinators_reject_non_conditions(self) -> None:
        rows = (
            ("cond_not-zero", lambda: cond_not()),
            ("cond_not-two", lambda: cond_not(Always(), Never())),
            ("cond_all-bool", lambda: cond_all(True)),
            ("cond_all-int", lambda: cond_all(1)),
            ("cond_all-float", lambda: cond_all(1.5)),
            ("cond_all-str", lambda: cond_all("x")),
            ("cond_all-none", lambda: cond_all(None)),
            ("cond_any-object", lambda: cond_any(object())),
            ("cond_any-list", lambda: cond_any([Always()])),
        )
        for label, call in rows:
            with self.subTest(row=label):
                with self.assertRaises(EudslError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "EUDSL-TYPE")

    def test_composite_truthiness_is_refused(self) -> None:
        rows = (
            cond_all(Always()),
            cond_any(Always()),
            cond_not(Always()),
        )
        for value in rows:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(EudslError) as ctx:
                    bool(value)
                self.assertEqual(ctx.exception.code, "EUDSL-COND-TRUTHIFY")


class CondAllMatrixTests(unittest.TestCase):
    def _module(self, n: int):
        def prog() -> None:
            conditions = [Var(1) for _ in range(n)]
            hit = Var(0)
            when(
                cond_all(*(value.eq(1) for value in conditions)),
                lambda: hit.iadd(1),
            )

        return build_module(prog)

    def test_zero_args_is_one_empty_condition_list(self) -> None:
        module = self._module(0)
        self.assertEqual(_when_conds(module), [0])
        self.assertEqual(_static(module)["conditions"], 0)

    def test_one_arg_splices_to_one_condition(self) -> None:
        module = self._module(1)
        self.assertEqual(_when_conds(module), [1])
        self.assertEqual(_static(module)["conditions"], 1)

    def test_sixteen_args_stay_one_gate(self) -> None:
        module = self._module(16)
        self.assertEqual(_when_conds(module), [16])
        static = _static(module)
        self.assertEqual(static["conditions"], 16)
        self.assertEqual(static["triggers"], 1)

    def test_seventeen_args_lower_through_chunking(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            conditions = [Var(1) for _ in range(17)]
            hit = Var(0)
            ids["hit"] = hit.id
            when(
                cond_all(*(value.eq(1) for value in conditions)),
                lambda: hit.iadd(1),
            )

        module = build_module(prog)
        self.assertEqual(_when_conds(module), [17])
        static = _static(module)
        self.assertEqual(static["conditions"], 17)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][ids["hit"]], 1)


class CondAnyMatrixTests(unittest.TestCase):
    def _module(self, n: int):
        ids: dict[str, int] = {}

        def prog() -> None:
            conditions = [Var(1) for _ in range(n)]
            hit = Var(0)
            ids["hit"] = hit.id
            when(
                cond_any(*(value.eq(1) for value in conditions)),
                lambda: hit.iadd(1),
            )

        return build_module(prog), ids

    def test_zero_branches_is_never_without_a_flag(self) -> None:
        module, _ids = self._module(0)
        self.assertEqual(_when_conds(module), [1])
        static = _static(module)
        self.assertEqual(static["conditions"], 1)
        # `Never` is the condition; no flag variable is allocated.
        self.assertEqual(len(_doc(module)["vars"]), 1)

    def test_branch_matrix_and_cost_pins(self) -> None:
        # N branches -> reset trigger + N branch `when`s + the caller gate.
        rows = {
            # cond_any(x) is the identity x: no flag, no prelude.
            1: (1, 1, 2),
            2: (4, 3, 4),
            3: (5, 4, 5),
            16: (18, 17, 18),
        }
        for n, (triggers, conditions, vars_count) in rows.items():
            with self.subTest(n=n):
                module, ids = self._module(n)
                static = _static(module)
                self.assertEqual(static["triggers"], triggers)
                self.assertEqual(static["conditions"], conditions)
                self.assertEqual(len(_doc(module)["vars"]), vars_count)
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 1
                )

    def test_truth_table_for_two_plain_conditions(self) -> None:
        for a, b in ((0, 0), (1, 0), (0, 1), (1, 1)):
            ids: dict[str, int] = {}

            def prog(a: int = a, b: int = b) -> None:
                left = Var(a)
                right = Var(b)
                hit = Var(0)
                ids["hit"] = hit.id
                when(
                    cond_any(left.eq(1), right.eq(1)),
                    lambda: hit.iadd(1),
                )

            report = run_sim(build_module(prog).to_json(), 1)
            self.assertEqual(
                report["var_values"][ids["hit"]],
                1 if (a or b) else 0,
                f"a={a} b={b}",
            )

    def test_or_of_an_and_with_more_than_sixteen_conditions(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            conditions = [Var(1) for _ in range(20)]
            other = Var(0)
            hit = Var(0)
            ids["hit"] = hit.id
            when(
                cond_any(
                    cond_all(*(value.eq(1) for value in conditions)),
                    other.eq(1),
                ),
                lambda: hit.iadd(1),
            )

        module = build_module(prog)
        static = _static(module)
        # reset + 20-condition chunked branch + 1-condition branch + caller
        # gate; 20 branch conds + the other branch + the flag gate.
        self.assertEqual(static["triggers"], 7)
        self.assertEqual(static["conditions"], 22)
        # The 20-condition AND branch is chunked by ADR 0027 inside its `when`.
        self.assertEqual(
            run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 1
        )


class CombinatorEdgeTests(unittest.TestCase):
    def test_cond_all_of_empty_any_is_statically_false(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            other = Var(1)
            hit = Var(0)
            ids["hit"] = hit.id
            when(cond_all(cond_any(), other.eq(1)), lambda: hit.iadd(1))

        module = build_module(prog)
        static = _static(module)
        self.assertEqual(static["triggers"], 1)
        self.assertEqual(static["conditions"], 2)  # Never + other
        self.assertEqual(run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 0)

    def test_cond_any_of_empty_all_is_statically_true(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            other = Var(0)
            hit = Var(0)
            ids["hit"] = hit.id
            when(cond_any(cond_all(), other.eq(1)), lambda: hit.iadd(1))

        module = build_module(prog)
        self.assertEqual(run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 1)

    def test_cond_any_empty_is_inert_in_while_until_and_rising(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            loop = Var(0)
            until_hit = Var(0)
            edge = Var(0)
            ids.update(loop=loop.id, until=until_hit.id, edge=edge.id)
            while_loop(cond_any(), lambda: loop.iadd(1))
            until(cond_any(), lambda: until_hit.iadd(1))
            on_rising(cond_any(), lambda: edge.iadd(1))

        values = run_sim(build_module(prog).to_json(), 3)["var_values"]
        self.assertEqual(values[ids["loop"]], 0)
        # until(cond_any()) is until(Never): the body runs every loop.
        self.assertEqual(values[ids["until"]], 3)
        self.assertEqual(values[ids["edge"]], 0)

    def test_reused_combinator_variable_gets_one_flag_per_when(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            a = Var(1)
            b = Var(0)
            first = Var(0)
            second = Var(0)
            ids.update(first=first.id, second=second.id)
            condition = cond_any(a.eq(1), b.eq(1))
            when(condition, lambda: first.iadd(1))
            when(condition, lambda: second.iadd(1))

        module = build_module(prog)
        # a, b, first, second + one independent gate flag per site.
        self.assertEqual(len(_doc(module)["vars"]), 6)
        resets = [
            stmt["acts"][0]["relocs"][0].get("target_var")
            for stmt in _doc(module)["stmts"]
            if stmt["kind"] == "trigger"
        ]
        self.assertEqual(len(resets), 2)
        self.assertNotEqual(resets[0], resets[1])
        values = run_sim(module.to_json(), 1)["var_values"]
        self.assertEqual(values[ids["first"]], 1)
        self.assertEqual(values[ids["second"]], 1)

    def test_until_chunks_a_seventeen_condition_cond_all_like_the_flat_list(
        self,
    ) -> None:
        def build(wrapped: bool) -> str:
            def prog() -> None:
                values = [Var(1) for _ in range(17)]
                conditions = [value.eq(1) for value in values]
                hit = Var(0)
                until(
                    cond_all(*conditions) if wrapped else conditions,
                    lambda: hit.iadd(1),
                )

            return build_module(prog).to_json()

        self.assertEqual(build(True), build(False))


class CondNotLeafTests(unittest.TestCase):
    def test_deferred_ne_complement_is_a_direct_eq(self) -> None:
        module = self._when_module(lambda: cond_not(CUnit(1000).hp.ne(500)))
        static = _static(module)
        # 1C / 0T: the complement is the direct `field == 500` cond, so the
        # `when` is the only trigger and no flag var exists.
        self.assertEqual(static["conditions"], 1)
        self.assertEqual(static["triggers"], 1)
        self.assertEqual(len(_doc(module)["vars"]), 1)

        for seed, want in ((500, 1), (499, 0), (501, 0)):
            with self.subTest(seed=seed):
                def prog(seed: int = seed) -> None:
                    CUnit(1000).hp.set(seed)
                    hit = Var(0)
                    when(
                        cond_not(CUnit(1000).hp.ne(500)),
                        lambda: hit.iadd(1),
                    )

                report = run_sim(build_module(prog).to_json(), 1)
                self.assertEqual(report["var_values"][0], want)

    def test_cond_not_accepts_the_cond_keyword(self) -> None:
        def keyword() -> None:
            hit = Var(0)
            when(cond_not(cond=Always()), lambda: hit.iadd(1))

        def positional() -> None:
            hit = Var(0)
            when(cond_not(Always()), lambda: hit.iadd(1))

        self.assertEqual(
            build_module(keyword).to_json(), build_module(positional).to_json()
        )

    def test_single_condition_all_is_an_identity_for_not(self) -> None:
        def direct() -> None:
            hit = Var(0)
            when(CUnit(1000).hp.eq(500), lambda: hit.iadd(1))

        def nested() -> None:
            hit = Var(0)
            when(
                cond_not(cond_all(CUnit(1000).hp.ne(500))),
                lambda: hit.iadd(1),
            )

        self.assertEqual(
            build_module(direct).to_json(), build_module(nested).to_json()
        )
        static = _static(build_module(nested))
        self.assertEqual(static["triggers"], 1)
        self.assertEqual(static["conditions"], 1)

    def _when_module(self, make_cond):
        def prog() -> None:
            hit = Var(0)
            when(make_cond(), lambda: hit.iadd(1))

        return build_module(prog)

    def test_deferred_cond_complements_each_operator(self) -> None:
        rows = (
            ("lt", lambda a, b: a.lt(b), 0),
            ("le", lambda a, b: a.le(b), 0),
            ("gt", lambda a, b: a.gt(b), 1),
            ("ge", lambda a, b: a.ge(b), 1),
            ("eq", lambda a, b: a.eq(b), 1),
            ("ne", lambda a, b: a.ne(b), 0),
        )
        for name, make, want in rows:
            with self.subTest(op=name):
                def prog(make=make) -> None:
                    a = Var(2)
                    b = Var(5)
                    hit = Var(0)
                    when(cond_not(make(a, b)), lambda: hit.iadd(1))

                report = run_sim(build_module(prog).to_json(), 1)
                # a=2, b=5: the complement of each comparison.
                self.assertEqual(report["var_values"][2], want)

    def test_always_and_never_complement_directly(self) -> None:
        def via_complement() -> None:
            hit = Var(0)
            when(cond_not(Always()), lambda: hit.iadd(1))
            when(cond_not(Never()), lambda: hit.iadd(1))

        def via_literals() -> None:
            hit = Var(0)
            when([Never()], lambda: hit.iadd(1))
            when([Always()], lambda: hit.iadd(1))

        self.assertEqual(
            build_module(via_complement).to_json(),
            build_module(via_literals).to_json(),
        )

    def test_opaque_cond_uses_the_not_opaque_flag_gate(self) -> None:
        def make(seed: int):
            ids: dict[str, int] = {}

            def prog() -> None:
                value = Var(seed)
                hit = Var(0)
                ids["hit"] = hit.id
                when(cond_not(value.eq(1)), lambda: hit.iadd(1))

            return prog, ids

        for seed, want in ((0, 1), (1, 0)):
            with self.subTest(seed=seed):
                prog, ids = make(seed)
                module = build_module(prog)
                self.assertEqual(
                    run_sim(module.to_json(), 1)["var_values"][ids["hit"]],
                    want,
                )
                static = _static(module)
                # reset trigger + `when(value.eq(1))` + the caller gate.
                self.assertEqual(static["triggers"], 3)
                self.assertEqual(static["conditions"], 2)

    def test_de_morgan_not_all_and_not_any(self) -> None:
        def not_all(a: int, b: int) -> int:
            def prog() -> None:
                va = Var(a)
                vb = Var(b)
                hit = Var(0)
                when(
                    cond_not(cond_all(va.eq(1), vb.eq(1))),
                    lambda: hit.iadd(1),
                )

            return run_sim(build_module(prog).to_json(), 1)["var_values"][2]

        def not_any(a: int, b: int) -> int:
            def prog() -> None:
                va = Var(a)
                vb = Var(b)
                hit = Var(0)
                when(
                    cond_not(cond_any(va.eq(1), vb.eq(1))),
                    lambda: hit.iadd(1),
                )

            return run_sim(build_module(prog).to_json(), 1)["var_values"][2]

        for a, b in ((0, 0), (0, 1), (1, 0), (1, 1)):
            with self.subTest(a=a, b=b):
                self.assertEqual(not_all(a, b), 0 if (a and b) else 1)
                self.assertEqual(not_any(a, b), 0 if (a or b) else 1)

    def test_double_negation_builds_the_original_condition(self) -> None:
        def direct() -> None:
            value = Var(1)
            hit = Var(0)
            when(value.eq(1), lambda: hit.iadd(1))

        def nested() -> None:
            value = Var(1)
            hit = Var(0)
            condition = value.eq(1)
            when(cond_not(cond_not(condition)), lambda: hit.iadd(1))

        self.assertEqual(
            build_module(direct).to_json(), build_module(nested).to_json()
        )


class TriggerCombinatorTests(unittest.TestCase):
    def test_cond_all_splices_into_the_trigger_slots(self) -> None:
        def prog() -> None:
            a = Var(1)
            b = Var(1)
            hit = Var(0)
            trig = Trigger(
                conds=[cond_all(a.eq(1), b.eq(1))],
                acts=[hit.act_add(1)],
            )
            self.assertEqual(len(trig.conds), 2)

        build_module(prog)

    def test_cond_all_empty_splices_to_zero_trigger_conditions(self) -> None:
        def prog() -> None:
            trig = Trigger(conds=[cond_all()], acts=[])
            self.assertEqual(len(trig.conds), 0)

        build_module(prog)

    def test_seventeen_spliced_conditions_hit_the_bounds_error(self) -> None:
        def prog() -> None:
            values = [Var(1) for _ in range(17)]
            Trigger(
                conds=[cond_all(*(value.eq(1) for value in values))],
                acts=[],
            )

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_sixteen_spliced_conditions_fit(self) -> None:
        def prog() -> None:
            values = [Var(1) for _ in range(16)]
            trig = Trigger(
                conds=[cond_all(*(value.eq(1) for value in values))],
                acts=[],
            )
            self.assertEqual(len(trig.conds), 16)

        build_module(prog)

    def test_cond_any_prelude_is_emitted_before_the_trigger(self) -> None:
        def prog() -> None:
            a = Var(1)
            b = Var(0)
            hit = Var(0)
            Trigger(
                conds=[cond_any(a.eq(1), b.eq(1))],
                acts=[hit.act_add(1)],
            )

        module = build_module(prog)
        self.assertEqual(
            _kinds(module), ["trigger", "when", "when", "trigger"]
        )
        doc = _doc(module)
        # One reset trigger, two branch `when`s, then the caller's trigger.
        self.assertEqual([len(stmt.get("conds", [])) for stmt in doc["stmts"]], [0, 1, 1, 1])
        # a, b, hit, and exactly one shared flag.
        self.assertEqual(len(doc["vars"]), 4)

    def test_cond_not_of_a_leaf_needs_no_prelude_in_a_raw_trigger(self) -> None:
        def prog() -> None:
            trig = Trigger(
                conds=[cond_not(CUnit(1000).hp.ne(500))],
                acts=[],
            )
            self.assertEqual(len(trig.conds), 1)
            self.assertEqual(trig.conds[0]["comparison"], 10)  # Exactly
            self.assertEqual(trig.conds[0]["amount"], 500)

        build_module(prog)

    def test_cond_any_detached_needs_a_builder(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            Trigger(conds=[cond_any(Always(), Never())], acts=[])
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")

    def test_opaque_cond_not_detached_needs_a_builder(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            Trigger(conds=[cond_not(CUnit(1000).hp.eq(5))], acts=[])
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")

    def test_detached_deferred_ne_keeps_the_scope_error(self) -> None:
        with self.assertRaises(EudslError) as ctx:
            Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[])
        self.assertEqual(ctx.exception.code, "EUDSL-SCOPE")

    def test_detached_empty_cond_any_still_works(self) -> None:
        trig = Trigger(conds=[cond_any()], acts=[])
        self.assertEqual(len(trig.conds), 1)
        self.assertEqual(trig.conds[0]["condtype"], 23)


def _deep_condition(a: Var, b: Var, c: Var, d: Var):
    """A nested all/any/not tree that is true for (1, 1, 0, 0)."""
    return cond_any(
        cond_all(a.eq(1), b.eq(1)),
        cond_not(cond_any(c.eq(1), cond_not(d.eq(1)))),
    )


class DeepNestingTemporalTests(unittest.TestCase):
    def test_when_and_once_and_until_accept_the_same_deep_tree(self) -> None:
        def when_prog(ids: dict) -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            ids["hit"] = hit.id
            when(_deep_condition(a, b, c, d), lambda: hit.iadd(1))

        def once_prog(ids: dict) -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            ids["hit"] = hit.id
            once(_deep_condition(a, b, c, d), lambda: hit.iadd(1))

        def until_prog(ids: dict) -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            ids["hit"] = hit.id
            until(_deep_condition(a, b, c, d), lambda: hit.iadd(1))

        info = {}
        for name, factory in (
            ("when", when_prog),
            ("once", once_prog),
            ("until", until_prog),
        ):
            ids: dict[str, int] = {}
            module = build_module(lambda factory=factory: factory(ids))
            info[name] = run_sim(module.to_json(), 2)["var_values"][ids["hit"]]
        self.assertEqual(info, {"when": 2, "once": 1, "until": 0})

    def test_while_loop_accepts_a_deep_tree(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            guard = Var(2)
            iters = Var(0)
            ids.update(guard=guard.id, iters=iters.id)

            def body() -> None:
                guard.isub(1)
                iters.iadd(1)

            while_loop(cond_all(_deep_condition(a, b, c, d), guard.ge(1)), body)

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["guard"]], 0)
        self.assertEqual(report["var_values"][ids["iters"]], 2)

    def test_edges_accept_the_deep_tree(self) -> None:
        ids: dict[str, int] = {}

        def rising_prog() -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            ids["hit"] = hit.id
            on_rising(_deep_condition(a, b, c, d), lambda: hit.iadd(1))

        module = build_module(rising_prog)
        self.assertEqual(run_sim(module.to_json(), 1)["var_values"][ids["hit"]], 1)
        self.assertEqual(run_sim(module.to_json(), 2)["var_values"][ids["hit"]], 1)

        def falling_prog() -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            ids["hit"] = hit.id
            frame = Var(0)
            frame.iadd(1)
            on_falling(_deep_condition(a, b, c, d), lambda: hit.iadd(1))
            when(frame.eq(2), lambda: a.set(0))

        module = build_module(falling_prog)
        self.assertEqual(run_sim(module.to_json(), 2)["var_values"][ids["hit"]], 0)
        self.assertEqual(run_sim(module.to_json(), 3)["var_values"][ids["hit"]], 1)


class WhileLoopTailRefreshTests(unittest.TestCase):
    def test_cond_any_gate_is_rebuilt_at_the_loop_tail(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            index = Var(0)
            iters = Var(0)
            ids.update(index=index.id, iters=iters.id)

            def body() -> None:
                index.iadd(1)
                iters.iadd(1)

            # The guard branch caps the loop if the tail ever stops refreshing
            # the OR flag, so a regression fails on values instead of hanging.
            while_loop(
                cond_any(index.lt(3), iters.ge(100)),
                body,
            )

        module = build_module(prog)
        report = run_sim(module.to_json(), 1)
        self.assertEqual(report["var_values"][ids["index"]], 3)
        self.assertEqual(report["var_values"][ids["iters"]], 3)
        # index, iters, and one shared OR flag.
        self.assertEqual(len(_doc(module)["vars"]), 3)

    def test_while_loop_with_cond_any_of_deferred_ne_refreshes(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            CUnit(1000).hp.set(0)
            iters = Var(0)
            ids["iters"] = iters.id

            def body() -> None:
                CUnit(1000).hp.set(500)
                iters.iadd(1)

            while_loop(cond_any(CUnit(1000).hp.ne(500)), body)

        module = build_module(prog)
        self.assertEqual(
            run_sim(module.to_json(), 1)["var_values"][ids["iters"]], 1
        )


class TriggerBoundsAndValidationTests(unittest.TestCase):
    def test_trigger_seventeen_leaf_conditions_still_error(self) -> None:
        def prog() -> None:
            values = [Var(1) for _ in range(17)]
            Trigger(conds=[value.eq(1) for value in values], acts=[])

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")

    def test_non_condition_arguments_error_before_any_emission(self) -> None:
        # Validation must run before the first flag allocation: an invalid
        # trailing item cannot leave a reset trigger or Var behind.
        def prog() -> None:
            value = Var(1)
            when([cond_any(value.eq(1)), 1.5], lambda: None)

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      CUnit,
      Trigger,
      Var,
      buildModule,
      condAll,
      condAny,
      condNot,
      until,
      when,
      whileLoop,
    } from "./index.ts";

    const cases = {
      all17When: () =>
        buildModule(() => {
          const values = Array.from({ length: 17 }, () => Var(1));
          const hit = Var(0);
          when(
            condAll(...values.map((value) => value.eq(1))),
            () => hit.iadd(1),
          );
        }).toJson(),
      anyOfNestedAll20: () =>
        buildModule(() => {
          const values = Array.from({ length: 20 }, () => Var(1));
          const other = Var(0);
          const hit = Var(0);
          when(
            condAny(
              condAll(...values.map((value) => value.eq(1))),
              other.eq(1),
            ),
            () => hit.iadd(1),
          );
        }).toJson(),
      anyFlattens: () =>
        buildModule(() => {
          const a = Var(1);
          const b = Var(1);
          const c = Var(1);
          const hit = Var(0);
          when(
            condAny(condAny(a.eq(1), b.eq(1)), c.eq(1)),
            () => hit.iadd(1),
          );
        }).toJson(),
      notLeaf: () =>
        buildModule(() => {
          when(condNot(CUnit(1000).hp.ne(500)), () => {});
        }).toJson(),
      notOpaque: () =>
        buildModule(() => {
          when(condNot(CUnit(1000).hp.ne(0)), () => {});
        }).toJson(),
      deMorgan: () =>
        buildModule(() => {
          const a = Var(1);
          const b = Var(0);
          const c = Var(1);
          const hit = Var(0);
          when(
            condNot(condAll(a.eq(1), b.eq(0), c.eq(1))),
            () => hit.iadd(1),
          );
        }).toJson(),
      emptyIdentity: () =>
        buildModule(() => {
          const other = Var(0);
          const hit = Var(0);
          when(condAny(condAll(), other.eq(1)), () => hit.iadd(1));
        }).toJson(),
      deepWhen: () =>
        buildModule(() => {
          const [a, b, c, d] = [Var(1), Var(1), Var(0), Var(0)];
          const hit = Var(0);
          when(
            condAny(
              condAll(a.eq(1), b.eq(1)),
              condNot(condAny(c.eq(1), condNot(d.eq(1)))),
            ),
            () => hit.iadd(1),
          );
        }).toJson(),
      mixedWhile: () =>
        buildModule(() => {
          CUnit(1000).hp.set(0);
          const guard = Var(2);
          const other = Var(0);
          const iters = Var(0);
          whileLoop(
            [CUnit(1000).hp.ne(500), condAny(guard.ge(1), other.eq(1))],
            () => {
              CUnit(1000).hp.set(500);
              guard.isub(1);
              iters.iadd(1);
            },
          );
        }).toJson(),
      until17: () =>
        buildModule(() => {
          const values = Array.from({ length: 17 }, () => Var(1));
          const hit = Var(0);
          until(condAll(...values.map((value) => value.eq(1))), () =>
            hit.iadd(1),
          );
        }).toJson(),
      rawAnyPrelude: () =>
        buildModule(() => {
          const a = Var(1);
          const b = Var(0);
          const hit = Var(0);
          new Trigger({
            conds: [condAny(a.eq(1), b.eq(1))],
            acts: [hit.act_add(1)],
          });
        }).toJson(),
      rawAll16: () =>
        buildModule(() => {
          const values = Array.from({ length: 16 }, () => Var(1));
          new Trigger({
            conds: [condAll(...values.map((value) => value.eq(1)))],
            acts: [],
          });
        }).toJson(),
      badAllType: () => {
        condAll(true);
        return "unreachable";
      },
      rawBounds: () =>
        buildModule(() => {
          const values = Array.from({ length: 17 }, () => Var(1));
          new Trigger({
            conds: [condAll(...values.map((value) => value.eq(1)))],
            acts: [],
          });
        }).toJson(),
      detachedAny: () => {
        new Trigger({
          conds: [condAny(CUnit(1000).hp.eq(1), CUnit(1000).hp.eq(2))],
          acts: [],
        });
        return "unreachable";
      },
      detachedNe: () => {
        new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [] });
        return "unreachable";
      },
    };

    const output = {};
    for (const [name, run] of Object.entries(cases)) {
      try {
        output[name] = { json: run() };
      } catch (error) {
        output[name] = { code: error?.code ?? String(error) };
      }
    }
    console.log(JSON.stringify(output));
    """
)


def _parity_cases() -> dict:
    """Mirror ``_TS_PARITY_SCRIPT`` case-for-case, in the same order."""

    def all17_when() -> str:
        def prog() -> None:
            values = [Var(1) for _ in range(17)]
            hit = Var(0)
            when(cond_all(*(value.eq(1) for value in values)), lambda: hit.iadd(1))

        return build_module(prog).to_json()

    def any_of_nested_all20() -> str:
        def prog() -> None:
            values = [Var(1) for _ in range(20)]
            other = Var(0)
            hit = Var(0)
            when(
                cond_any(
                    cond_all(*(value.eq(1) for value in values)),
                    other.eq(1),
                ),
                lambda: hit.iadd(1),
            )

        return build_module(prog).to_json()

    def any_flattens() -> str:
        def prog() -> None:
            a = Var(1)
            b = Var(1)
            c = Var(1)
            hit = Var(0)
            when(
                cond_any(cond_any(a.eq(1), b.eq(1)), c.eq(1)),
                lambda: hit.iadd(1),
            )

        return build_module(prog).to_json()

    def not_leaf() -> str:
        def prog() -> None:
            when(cond_not(CUnit(1000).hp.ne(500)), lambda: None)

        return build_module(prog).to_json()

    def not_opaque() -> str:
        def prog() -> None:
            when(cond_not(CUnit(1000).hp.ne(0)), lambda: None)

        return build_module(prog).to_json()

    def de_morgan() -> str:
        def prog() -> None:
            a = Var(1)
            b = Var(0)
            c = Var(1)
            hit = Var(0)
            when(
                cond_not(cond_all(a.eq(1), b.eq(0), c.eq(1))),
                lambda: hit.iadd(1),
            )

        return build_module(prog).to_json()

    def empty_identity() -> str:
        def prog() -> None:
            other = Var(0)
            hit = Var(0)
            when(cond_any(cond_all(), other.eq(1)), lambda: hit.iadd(1))

        return build_module(prog).to_json()

    def deep_when() -> str:
        def prog() -> None:
            a, b, c, d = (Var(v) for v in (1, 1, 0, 0))
            hit = Var(0)
            when(
                cond_any(
                    cond_all(a.eq(1), b.eq(1)),
                    cond_not(cond_any(c.eq(1), cond_not(d.eq(1)))),
                ),
                lambda: hit.iadd(1),
            )

        return build_module(prog).to_json()

    def mixed_while() -> str:
        def prog() -> None:
            CUnit(1000).hp.set(0)
            guard = Var(2)
            other = Var(0)
            iters = Var(0)

            def body() -> None:
                CUnit(1000).hp.set(500)
                guard.isub(1)
                iters.iadd(1)

            while_loop(
                [CUnit(1000).hp.ne(500), cond_any(guard.ge(1), other.eq(1))],
                body,
            )

        return build_module(prog).to_json()

    def until17() -> str:
        def prog() -> None:
            values = [Var(1) for _ in range(17)]
            hit = Var(0)
            until(cond_all(*(value.eq(1) for value in values)), lambda: hit.iadd(1))

        return build_module(prog).to_json()

    def raw_any_prelude() -> str:
        def prog() -> None:
            a = Var(1)
            b = Var(0)
            hit = Var(0)
            Trigger(conds=[cond_any(a.eq(1), b.eq(1))], acts=[hit.act_add(1)])

        return build_module(prog).to_json()

    def raw_all16() -> str:
        def prog() -> None:
            values = [Var(1) for _ in range(16)]
            Trigger(conds=[cond_all(*(value.eq(1) for value in values))], acts=[])

        return build_module(prog).to_json()

    def bad_all_type() -> str:
        cond_all(True)
        return "unreachable"

    def raw_bounds() -> str:
        def prog() -> None:
            values = [Var(1) for _ in range(17)]
            Trigger(conds=[cond_all(*(value.eq(1) for value in values))], acts=[])

        return build_module(prog).to_json()

    def detached_any() -> str:
        Trigger(
            conds=[cond_any(CUnit(1000).hp.eq(1), CUnit(1000).hp.eq(2))],
            acts=[],
        )
        return "unreachable"

    def detached_ne() -> str:
        Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[])
        return "unreachable"

    return {
        "all17When": all17_when,
        "anyOfNestedAll20": any_of_nested_all20,
        "anyFlattens": any_flattens,
        "notLeaf": not_leaf,
        "notOpaque": not_opaque,
        "deMorgan": de_morgan,
        "emptyIdentity": empty_identity,
        "deepWhen": deep_when,
        "mixedWhile": mixed_while,
        "until17": until17,
        "rawAnyPrelude": raw_any_prelude,
        "rawAll16": raw_all16,
        "badAllType": bad_all_type,
        "rawBounds": raw_bounds,
        "detachedAny": detached_any,
        "detachedNe": detached_ne,
    }


class TsPyParityTests(unittest.TestCase):
    def test_ts_and_py_combinator_hir_are_byte_identical(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        ts_dir = Path(__file__).resolve().parents[2] / "ts"
        completed = subprocess.run(
            [
                node,
                "--experimental-strip-types",
                "--no-warnings",
                "--input-type=module",
                "-e",
                _TS_PARITY_SCRIPT,
            ],
            cwd=ts_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"node failed:\n{completed.stderr}",
        )
        ts_results = json.loads(completed.stdout)

        py_results = {}
        for name, run in _parity_cases().items():
            try:
                py_results[name] = {"json": run()}
            except EudslError as error:
                py_results[name] = {"code": error.code}

        self.assertEqual(sorted(ts_results), sorted(py_results))
        for name, py_result in py_results.items():
            with self.subTest(case=name):
                self.assertEqual(ts_results[name], py_result)

    def test_ts_and_py_error_codes_match(self) -> None:
        # These three rows are the detached and bounds failures the two
        # implementations must reject identically before emitting anything.
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        ts_dir = Path(__file__).resolve().parents[2] / "ts"
        completed = subprocess.run(
            [
                node,
                "--experimental-strip-types",
                "--no-warnings",
                "--input-type=module",
                "-e",
                _TS_PARITY_SCRIPT,
            ],
            cwd=ts_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"node failed:\n{completed.stderr}",
        )
        ts_results = json.loads(completed.stdout)
        expected = {
            "badAllType": "EUDSL-TYPE",
            "rawBounds": "EUDSL-BOUNDS",
            "detachedAny": "EUDSL-SCOPE",
            "detachedNe": "EUDSL-SCOPE",
        }
        for name, code in expected.items():
            with self.subTest(case=name):
                self.assertEqual(ts_results[name], {"code": code})


if __name__ == "__main__":
    unittest.main()
