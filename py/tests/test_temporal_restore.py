"""Temporal candidate 2, option C — path-complete restore sim tests.

Python mirror of ts/test_temporal_restore.ts. Confirmed tests (D1-D3, plus
D2b): each case asserts the CORRECT behaviour. On unmodified code every
case failed, confirming the stale-next class that option C (path-complete
restore in crates/eudkit-lower/src/control.rs) fixes. These are now
passing lock tests for that guarantee.

Sim semantics: run_sim(mod.to_json(), N) simulates N trigger loops from
the initial state, so "loop 2" means the state after two full loops.
A frame counter incremented by an unconditional act at the top of the
module drives the per-loop conditions.
"""

import unittest

from py import Var, build_module, once, when, while_loop

from py.tests.helpers import run_sim
class TemporalRestoreTests(unittest.TestCase):
  def test_d1_nested_last_stale_gate(self):
    # Var order: frame, outer_count, inner_count.
    def make():
      frame = Var(0)
      outer_count = Var(0)
      inner_count = Var(0)
      frame.iadd(1)

      def outer_body():
        outer_count.iadd(1)

        def inner_body():
          inner_count.iadd(1)

        when(frame.eq(2), inner_body)

      when(frame.eq(1), outer_body)

    mod = build_module(make)
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["var_values"][:3], [1, 1, 0])
    # Loop 2: outer false, inner true. A stale outer gate would give
    # [2, 2, 1]; correct behaviour keeps the outer body from running.
    rep2 = run_sim(mod.to_json(), 2)
    self.assertEqual(rep2["var_values"][:3], [2, 1, 0])

  def test_d2_while_premature_exit(self):
    # Var order: counter, sum, inner_count. The inner when body is
    # deliberately non-leaf (it contains a nested when), so the inner
    # when stays a When in lowering instead of leaf-packing.
    def make():
      counter = Var(3)
      sum_ = Var(0)
      inner_count = Var(0)

      def body():
        counter.isub(1)
        sum_.iadd(10)

        def inner_body():
          inner_count.iadd(1)
          when(inner_count.eq(999), lambda: inner_count.iadd(10))

        when(counter.eq(99), inner_body)

      while_loop(counter.gt(0), body)

    mod = build_module(make)
    rep = run_sim(mod.to_json(), 1)
    # Correct: three intra-frame iterations. Premature exit after the
    # first iteration gives [2, 10, 0].
    self.assertEqual(rep["var_values"][:3], [0, 30, 0])

  def test_d2b_leaf_packed_inner_when_in_while_body(self):
    # Same shape as D2, but the inner when body is a single act, so it
    # leaf-packs. On unmodified code this panicked the sim with an
    # infinite trigger loop; after the fix it terminates correctly.
    def make():
      counter = Var(3)
      sum_ = Var(0)
      inner_count = Var(0)

      def body():
        counter.isub(1)
        sum_.iadd(10)

        def inner_body():
          inner_count.iadd(1)

        when(counter.eq(99), inner_body)

      while_loop(counter.gt(0), body)

    mod = build_module(make)
    rep = run_sim(mod.to_json(), 1)
    self.assertEqual(rep["var_values"][:3], [0, 30, 0])

  def test_d3_once_self_disable_plus_appended_restore(self):
    # Var order: frame, outer_count, once_count.
    def make():
      frame = Var(0)
      outer_count = Var(0)
      once_count = Var(0)
      frame.iadd(1)

      def outer_body():
        outer_count.iadd(1)
        once(frame.eq(1), lambda: once_count.iadd(1))

      when(frame.le(2), outer_body)

    mod = build_module(make)
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["var_values"][:3], [1, 1, 1])
    rep2 = run_sim(mod.to_json(), 2)
    self.assertEqual(rep2["var_values"][:3], [2, 2, 1])
    # Loop 3: outer false. A stale outer gate would give outer 3.
    rep3 = run_sim(mod.to_json(), 3)
    self.assertEqual(rep3["var_values"][:3], [3, 2, 1])

if __name__ == "__main__":
  unittest.main()
