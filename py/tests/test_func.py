"""Callable subroutine (`func`) frontend tests.

A subroutine body is emitted once per module: call sites bind arguments,
jump into the shared body, and copy the result back into a fresh var. These
tests cover the capture/placement contract (body statements must land in
``module.funcs`` and not leak into the caller's scope) and the call-site
errors.
"""

import json
import unittest

from py import AtLeast, DeathsX, Var, build_module, func, when
from py.tests.helpers import run_sim
from py.view import EudslError


class FuncTests(unittest.TestCase):
  def test_body_emitted_once_and_calls_share_it(self):
    def make():
      acc = Var(0)
      seen = Var(0)

      def bump(v):
        v.iadd(1)
        return v

      inc = func(bump)
      a = inc(acc)
      acc.set(a)
      b = inc(5)
      acc.set(b)
      c = inc(acc)
      acc.set(c)
      seen.iadd(1)

    mod = build_module(make)
    doc = json.loads(mod.to_json())

    self.assertEqual(len(doc["module"]["funcs"]), 1)
    self.assertEqual(len(doc["module"]["funcs"][0]["params"]), 1)
    # Raw HIR is pre-optimization: body statements stay captured in the func.
    # The only root trigger is the trailing `seen.iadd(1)` act. One call passes
    # a constant argument, so the all-or-nothing multi-site rule keeps every
    # call shared instead of partially inlining the function.
    top_kinds = [s["kind"] for s in doc["module"]["stmts"]]
    self.assertEqual(top_kinds.count("trigger"), 1)
    self.assertEqual(top_kinds.count("call"), 3)

    rep = run_sim(mod.to_json(), 1)
    # acc: 0 -> 1 -> 6 -> 7; seen: 1.
    self.assertEqual(rep["var_values"][0], 7)
    self.assertEqual(rep["var_values"][1], 1)

  def test_capture_keeps_nested_scopes_in_the_body(self):
    def make():
      hits = Var(0)
      flag = Var(0)

      def record():
        hits.iadd(1)
        with when(flag.eq(0)):
          hits.iadd(10)

      rec = func(record)
      rec()
      rec()

    mod = build_module(make)
    doc = json.loads(mod.to_json())
    body = doc["module"]["funcs"][0]["body"]
    self.assertEqual([s["kind"] for s in body], ["trigger", "when"])
    self.assertEqual(
      [s["kind"] for s in doc["module"]["stmts"]], ["call", "call"]
    )

    rep = run_sim(mod.to_json(), 1)
    # Two sites of a `Trigger + When` body: the static estimate ties (shared
    # `2 + 2` against two duplicated copies), and `INLINE_EXTRA_TRIGGERS = 0`
    # accepts the tie. The gate is evaluated once per site rather than once for
    # the shared body, which is the extra condition the static tie accepts.
    self.assertEqual(rep["static_cost"]["triggers"], 4)
    self.assertEqual(rep["static_cost"]["conditions"], 2)
    self.assertEqual(rep["static_cost"]["actions"], 4)
    self.assertEqual(rep["executed_cost"]["triggers"], 4)
    self.assertEqual(rep["executed_cost"]["conditions"], 2)
    self.assertEqual(rep["executed_cost"]["actions"], 4)
    self.assertEqual(rep["var_values"][0], 22)

  def test_small_bodies_inline_and_large_bodies_share(self):
    def measure(acts, sites):
      def make():
        target = Var(0)

        def kernel():
          for _ in range(acts):
            target.iadd(1)

        k = func(kernel)
        for _ in range(sites):
          k()

      return run_sim(build_module(make).to_json(), 1)

    # A 12-act leaf run at one site has always been single-site inlined.
    one = measure(12, 1)
    self.assertEqual(one["static_cost"]["triggers"], 1)
    self.assertEqual(one["static_cost"]["actions"], 12)
    # At three sites the copies pack into one trigger: 36 actions, no shared
    # call machinery. That is the cost-based win over `4T` shared.
    three = measure(12, 3)
    self.assertEqual(three["static_cost"]["triggers"], 1)
    self.assertEqual(three["static_cost"]["actions"], 36)
    self.assertEqual(three["executed_cost"]["triggers"], 1)
    self.assertEqual(three["executed_cost"]["actions"], 36)
    self.assertEqual(three["var_values"][0], 36)

    # A 200-act body needs 7 packed triggers when duplicated against a shared
    # 6, so `INLINE_EXTRA_TRIGGERS = 0` keeps it shared.
    large = measure(200, 2)
    self.assertEqual(large["static_cost"]["triggers"], 6)
    self.assertEqual(large["static_cost"]["actions"], 202)
    self.assertEqual(large["executed_cost"]["triggers"], 10)
    self.assertEqual(large["executed_cost"]["actions"], 402)
    self.assertEqual(large["var_values"][0], 400)

  def test_a_destination_bound_argument_reads_the_pre_call_value(self):
    def make():
      x = Var(7)
      y = Var(11)

      def aliasing(p):
        r = Var(0)
        r.set(0)
        with when([DeathsX(p.value, AtLeast, 1, 0, 1)]):
          r.iadd(5)
        return r

      k = func(aliasing)
      x.set(k(x))
      y.set(k(y))

    rep = run_sim(build_module(make).to_json(), 1)
    # Both sites bind the destination to the argument, and the body writes its
    # result before reading the parameter. Inlining substitutes `x` for `p`, so
    # the gate would read the freshly written 0 result instead of the pre-call
    # value; the call must stay shared and see the original argument (7 and 11
    # are both non-zero), yielding 5 at each site.
    self.assertEqual(rep["var_values"][0], 5)
    self.assertEqual(rep["var_values"][1], 5)

  def test_inline_crossover_tracks_body_size_and_sites(self):
    def measure(acts, sites, separated=False):
      def make():
        target = Var(0)

        def kernel():
          for _ in range(acts):
            target.iadd(1)

        k = func(kernel)
        for site in range(sites):
          if separated and site == 1:
            # The frontend emits this as an unconditional `Trigger`, which
            # splits the scheduler's act run: the copies cannot pack across it.
            target.iadd(1)
          k()

      rep = run_sim(build_module(make).to_json(), 1)
      static = rep["static_cost"]
      executed = rep["executed_cost"]
      return (
        (static["triggers"], static["conditions"], static["actions"]),
        (executed["triggers"], executed["conditions"], executed["actions"]),
        rep["var_values"][0],
      )

    cases = [
      # (body acts, sites, separated, static, executed, value)
      (1, 2, False, (1, 0, 2), (1, 0, 2), 2),
      (8, 2, False, (1, 0, 16), (1, 0, 16), 16),
      (40, 2, False, (2, 0, 80), (2, 0, 80), 80),
      (100, 2, False, (4, 0, 200), (4, 0, 200), 200),
      (100, 3, False, (5, 0, 300), (5, 0, 300), 300),
      (100, 2, True, (5, 0, 201), (5, 0, 201), 201),
    ]
    for acts, sites, separated, static, executed, value in cases:
      with self.subTest(acts=acts, sites=sites, separated=separated):
        got_static, got_executed, got_value = measure(acts, sites, separated)
        self.assertEqual(got_static, static)
        self.assertEqual(got_executed, executed)
        self.assertEqual(got_value, value)

  def test_boundary_bodies_stay_shared(self):
    def measure(acts, sites):
      def make():
        target = Var(0)

        def kernel():
          for _ in range(acts):
            target.iadd(1)

        k = func(kernel)
        for _ in range(sites):
          k()

      rep = run_sim(build_module(make).to_json(), 1)
      static = rep["static_cost"]
      executed = rep["executed_cost"]
      return (
        (static["triggers"], static["conditions"], static["actions"]),
        (executed["triggers"], executed["conditions"], executed["actions"]),
      )

    # `ceil(sites * acts / 64)` exceeds `ceil(acts / 64) + sites` in every
    # case, so the default rejects the extra static trigger.
    self.assertEqual(measure(200, 2), ((6, 0, 202), (10, 0, 402)))
    self.assertEqual(measure(100, 4), ((6, 0, 104), (12, 0, 404)))

  def test_call_arity_is_checked(self):
    def make():
      def kernel(a, b):
        a.iadd(b)

      k = func(kernel)
      k(1)

    with self.assertRaises(EudslError) as ctx:
      build_module(make)
    self.assertIn("expects 2 argument(s), got 1", str(ctx.exception))

  def test_call_arguments_must_be_var_or_int(self):
    def make():
      def kernel(a):
        a.iadd(1)

      k = func(kernel)
      k("nope")

    with self.assertRaises(EudslError) as ctx:
      build_module(make)
    self.assertIn("Var or a constant integer", str(ctx.exception))

  def test_body_must_return_var_or_none(self):
    def make():
      def kernel():
        return 5

      func(kernel)

    with self.assertRaises(EudslError) as ctx:
      build_module(make)
    self.assertIn("return a Var or None", str(ctx.exception))

  def test_func_requires_build_module(self):
    with self.assertRaises(EudslError) as ctx:
      func(lambda: None)
    self.assertIn("inside buildModule()", str(ctx.exception))


class SharedReaderTests(unittest.TestCase):
  """#97: `field.read()` on a full dword shares one body per address."""

  def test_shared_reader_cost_is_per_site(self):
    from py import FieldSpec, defineMemView
    from py.tests.helpers import run_sim

    schema = defineMemView("SharedReadProbe", 4, {"value": FieldSpec(0, 4)})

    def measured(sites):
      ids = []

      def prog():
        from py import Var
        from py.view import make_memview

        for _ in range(sites):
          out = Var(0)
          ids.append(out.id)
          out.set(make_memview(schema, 3000).value.read())

      report = run_sim(build_module(prog).to_json(), 1)
      return report, [report["var_values"][i] for i in ids]

    one, one_values = measured(1)
    four, four_values = measured(4)

    # Runtime values stay right: every site reads the same EPD.
    self.assertEqual(one_values, [0])
    self.assertEqual(four_values, [0] * 4)

    # A single call site is inlined by the single-call optimizer, so it pays
    # just the 33T body (the old inline read cost 36T). Three further sites
    # share that body and add only their call sequence: 33T -> 43T
    # (43 - 33 = 10 = 3 x ~3T), never another 33T inline copy.
    self.assertEqual(one["static_cost"]["triggers"], 33)
    self.assertEqual(four["static_cost"]["triggers"], 43)


if __name__ == "__main__":
  unittest.main()
