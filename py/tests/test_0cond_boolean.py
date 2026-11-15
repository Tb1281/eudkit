"""0-cond + boolean conds tests (decided 2026-11-04).

Python mirror of ts/test_0cond_boolean.ts, plus Py-specific forms:
context-manager style (`with when(True):` / `with when(False):` /
`with until(True):` / `with until(False):`), the one-argument callable
form (`when(body)` executes the body with 0 conds), and the until
non-overload (`until(callable)` is rejected eagerly with `EUDSL-TYPE`
instead of executing the callable — until keeps conds REQUIRED).

Scope: when/once/until only. Booleans inside a cond list are out of
scope (list handling unchanged).
"""

import json
import unittest

from py import Always, Never, Var, build_module, once, until, when
from py.view import EudslError

from py.tests.helpers import run_sim, run_static
def collect_by_kind(node, kind, out=None):
    if out is None:
        out = []
    if isinstance(node, list):
        for item in node:
            collect_by_kind(item, kind, out)
    elif isinstance(node, dict):
        if node.get("kind") == kind:
            out.append(node)
        for value in node.values():
            collect_by_kind(value, kind, out)
    return out

def empty_module_json() -> str:
    return build_module(lambda: None).to_json()

class ZeroCondBooleanTests(unittest.TestCase):
    def test_when_body_emits_0cond_when_node(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            when(body)

        mod = build_module(make)
        whens = collect_by_kind(json.loads(mod.to_json()), "when")
        self.assertEqual(len(whens), 1)
        self.assertEqual(whens[0]["conds"], [])

    def test_when_true_identical_to_when_body(self):
        def make_true():
            count = Var(0)

            def body():
                count.iadd(1)

            when(True, body)

        def make_body():
            count = Var(0)

            def body():
                count.iadd(1)

            when(body)

        self.assertEqual(
            build_module(make_true).to_json(), build_module(make_body).to_json()
        )

    def test_when_false_drops_block_and_never_runs_body(self):
        # The var is allocated INSIDE the body: if the body ran, a decl
        # and a node would appear and the JSON would differ from empty.
        def make():
            def body():
                v = Var(0)
                v.iadd(1)

            when(False, body)

        self.assertEqual(build_module(make).to_json(), empty_module_json())

    def test_when_body_runs_every_loop(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            when(body)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json(), 1)["var_values"], [1])
        self.assertEqual(run_sim(mod.to_json(), 3)["var_values"], [3])

    def test_when_false_never_runs_sim(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            when(False, body)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json(), 3)["var_values"], [0])

    def test_once_body_runs_exactly_once_unconditionally(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            once(body)

        mod = build_module(make)
        onces = collect_by_kind(json.loads(mod.to_json()), "once")
        self.assertEqual(len(onces), 1)
        self.assertEqual(onces[0]["conds"], [])
        self.assertEqual(run_sim(mod.to_json(), 1)["var_values"], [1])
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [1])

    def test_once_true_identical_to_once_body(self):
        def make_true():
            count = Var(0)

            def body():
                count.iadd(1)

            once(True, body)

        def make_body():
            count = Var(0)

            def body():
                count.iadd(1)

            once(body)

        self.assertEqual(
            build_module(make_true).to_json(), build_module(make_body).to_json()
        )

    def test_once_false_drops_block(self):
        def make():
            def body():
                v = Var(0)
                v.iadd(1)

            once(False, body)

        self.assertEqual(build_module(make).to_json(), empty_module_json())

    def test_until_true_emits_until_node_with_0conds(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            until(True, body)

        mod = build_module(make)
        untils = collect_by_kind(json.loads(mod.to_json()), "until")
        self.assertEqual(len(untils), 1)
        self.assertEqual(untils[0]["conds"], [])

    def test_until_true_body_never_runs_sim(self):
        # Exclusive semantics (issue #72): a 0-cond until trips on the
        # first loop, so the body never runs. Opt emits only the reset
        # trip-when; there is no body statement, so no leaf act can ride
        # along in a merged trip+body trigger either.
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            until(True, body)

        mod = build_module(make)
        # Vars: [count, latch].
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [0, 0])

    @staticmethod
    def _static(hir_json):
        s = run_static(hir_json)["static_cost"]
        return (
            s["triggers"],
            s["conditions"],
            s["actions"],
            s["total_bytes"],
        )

    def test_until_true_flat_transfer_body_never_runs_sim(self):
        # Regression pin: only the reset trip-when is emitted, so the
        # transfer body has no statement to run (d stays 0 after 4
        # loops). Static cost is the trip trigger alone.
        def make():
            s = Var(5)
            d = Var(0)

            def body():
                d.set(s)

            until(True, body)

        mod = build_module(make)
        self.assertEqual(self._static(mod.to_json()), (1, 1, 1, 2416))
        # Vars: [s, d, latch].
        #
        # Do NOT pin executed_cost.triggers: it reports the LAST loop's
        # cost and skips disabled triggers, so a never-run body
        # legitimately reports 0T. var_values plus the static cost is
        # the only sound pin.
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [5, 0, 0])

    def test_until_true_mixed_leaf_and_transfer_body_never_runs_sim(self):
        # Neither write happens: the leaf act has no body-when to ride
        # along in, and the transfer is its own (dropped) statement.
        def make():
            s = Var(5)
            c = Var(0)
            d = Var(0)

            def body():
                c.iadd(1)
                d.set(s)

            until(True, body)

        mod = build_module(make)
        self.assertEqual(self._static(mod.to_json()), (1, 1, 1, 2416))
        # Vars: [s, c, d, latch].
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [5, 0, 0, 0])

    def test_until_true_nested_when_body_never_runs_sim(self):
        # A nested when is a second statement that re-checks its own
        # gate; with no body-when emitted at all it never runs.
        def make():
            g = Var(1)
            d = Var(0)

            def body():
                def inner():
                    d.iadd(1)

                when(g.eq(1), inner)

            until(True, body)

        mod = build_module(make)
        self.assertEqual(self._static(mod.to_json()), (1, 1, 1, 2412))
        # Vars: [g, d, latch].
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [1, 0, 0])

    def test_until_always_body_never_runs_sim(self):
        # Widened-predicate pin: Always() (condtype 22) reaches the same
        # trip-only emission through the array/masked-Var folds, so the
        # unconditional predicate must include all-Always conds.
        def make():
            s = Var(5)
            d = Var(0)

            def body():
                d.set(s)

            until(Always(), body)

        mod = build_module(make)
        self.assertEqual(self._static(mod.to_json()), (1, 1, 1, 2416))
        # Vars: [s, d, latch].
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [5, 0, 0])

    def test_until_controls_unchanged(self):
        # Gated and False paths must stay byte-identical to baseline.
        def make_never():
            d = Var(0)

            def body():
                d.iadd(1)

            until(False, body)

        never = build_module(make_never)
        self.assertEqual(self._static(never.to_json()), (2, 3, 2, 4824))
        self.assertEqual(run_sim(never.to_json(), 4)["var_values"], [4, 1])

        def make_gated(g_init):
            def make():
                s = Var(5)
                d = Var(0)
                g = Var(g_init)

                def body():
                    d.set(s)

                until(g.eq(1), body)

            return build_module(make)

        closed = make_gated(0)
        self.assertEqual(self._static(closed.to_json()), (5, 3, 70, 12048))
        # Loop 1 runs the body (g still 0), loop 2 trips.
        self.assertEqual(
            run_sim(closed.to_json(), 4)["var_values"], [5, 5, 0, 1]
        )

        open_ = make_gated(1)
        self.assertEqual(self._static(open_.to_json()), (5, 3, 70, 12048))
        self.assertEqual(run_sim(open_.to_json(), 4)["var_values"], [5, 0, 1, 0])

    def test_until_true_leaf_act_body_never_runs(self):
        # A flat leaf act body has no body-when to ride along in once
        # the body is dropped: only the reset trip-when is emitted.
        def make():
            d = Var(0)

            def body():
                d.iadd(1)

            until(True, body)

        mod = build_module(make)
        self.assertEqual(self._static(mod.to_json()), (1, 1, 1, 2412))
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [0, 0])

    def test_until_false_maps_to_never_cond(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            until(False, body)

        mod = build_module(make)
        untils = collect_by_kind(json.loads(mod.to_json()), "until")
        self.assertEqual(len(untils), 1)
        self.assertEqual(untils[0]["conds"], [Never()])

    def test_until_false_identical_to_until_never(self):
        def make_false():
            count = Var(0)

            def body():
                count.iadd(1)

            until(False, body)

        def make_never():
            count = Var(0)

            def body():
                count.iadd(1)

            until(Never(), body)

        self.assertEqual(
            build_module(make_false).to_json(),
            build_module(make_never).to_json(),
        )

    def test_until_false_runs_body_every_loop_sim(self):
        def make():
            count = Var(0)

            def body():
                count.iadd(1)

            until(False, body)

        mod = build_module(make)
        # Vars: [count, latch]. The trip-when never fires, so the
        # latch stays 1 and the body-when runs every loop.
        self.assertEqual(run_sim(mod.to_json(), 1)["var_values"], [1, 1])
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [4, 1])

    def test_until_normal_cond_still_works(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)

            def body():
                count.iadd(1)

            until(frame.ge(2), body)

        mod = build_module(make)
        # Loop 1: frame 1 < 2, body runs. Loop 2: trip fires, body skipped.
        # Vars: [frame, count, latch].
        self.assertEqual(run_sim(mod.to_json(), 3)["var_values"], [3, 1, 0])

    def test_cm_when_true_runs_every_loop(self):
        def make():
            count = Var(0)
            with when(True):
                count.iadd(1)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json(), 3)["var_values"], [3])

    def test_cm_when_false_queues_no_node(self):
        def make():
            count = Var(0)
            with when(False):
                count.iadd(1)

        mod = build_module(make)
        whens = collect_by_kind(json.loads(mod.to_json()), "when")
        self.assertEqual(whens, [])

    def test_cm_until_true_body_never_runs(self):
        # Same contract as the callback form: only the reset trip-when
        # is emitted, so the body never runs.
        def make():
            count = Var(0)
            with until(True):
                count.iadd(1)

        mod = build_module(make)
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [0, 0])

    def test_cm_until_false_runs_body_every_loop(self):
        def make():
            count = Var(0)
            with until(False):
                count.iadd(1)

        mod = build_module(make)
        # Vars: [count, latch]. CM form maps to Never exactly like
        # the callback form: latch stays 1, body runs every loop.
        self.assertEqual(run_sim(mod.to_json(), 1)["var_values"], [1, 1])
        self.assertEqual(run_sim(mod.to_json(), 4)["var_values"], [4, 1])

    def test_until_callable_is_not_body_only(self):
        # until keeps conds REQUIRED: a single callable argument is NOT
        # executed as a body.  It is validated eagerly and rejected as a
        # non-condition, matching TS ``until(body)``.
        called = []

        def body():
            called.append(1)

        def make():
            until(body)

        with self.assertRaises(EudslError) as ctx:
            build_module(make)
        self.assertEqual(ctx.exception.code, "EUDSL-TYPE")
        self.assertEqual(called, [])

if __name__ == "__main__":
    unittest.main()
