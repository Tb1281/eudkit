"""once() non-leaf path-complete restore sim tests.

Python mirror of ts/test_once_nonleaf.ts. A once whose body contains a
control statement (here an inner when) cannot pack into a leaf trigger,
so it lowers through crates/eudkit-lower/src/control.rs lower_once.
That path built a self-disabling gate with no path-complete restore, so
later loops walked through the disabled gate via its stale next pointer
and re-executed the body every loop. These tests assert the CORRECT
behaviour: the body runs exactly once.

Sim semantics: run_sim(mod.to_json(), N) simulates N trigger loops from
the initial state, so "loop 2" means the state after two full loops.
A frame counter incremented by an unconditional act at the top of the
module drives the per-loop conditions.
"""

import unittest

from py import Var, build_module, once, when

from py.tests.helpers import run_sim
class OnceNonleafTests(unittest.TestCase):
  def test_nonleaf_once_runs_exactly_once(self):
    # Var order: frame, count.
    def make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      # Once fires on loop 1 only (frame == 1). The inner when is true
      # on every loop, so a stale gate re-entering the body is visible.
      def once_body():
        def inner_body():
          count.iadd(1)

        when(frame.ge(1), inner_body)

      once(frame.eq(1), once_body)

    mod = build_module(make)
    # Loop 1: once fires, inner when true. State: frame 1, count 1.
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["var_values"][:2], [1, 1])
    # Loops 2-4: once disabled. Correct: count stays 1.
    # A stale once gate re-enters the body and gives count 2, 3, 4.
    rep2 = run_sim(mod.to_json(), 2)
    self.assertEqual(rep2["var_values"][:2], [2, 1])
    rep3 = run_sim(mod.to_json(), 3)
    self.assertEqual(rep3["var_values"][:2], [3, 1])
    rep4 = run_sim(mod.to_json(), 4)
    self.assertEqual(rep4["var_values"][:2], [4, 1])

  def test_nonleaf_once_nested_inside_when_runs_exactly_once(self):
    # Var order: frame, count.
    def make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      # Outer when true on every loop; the once inside must fire once.
      def outer_body():
        def once_body():
          def inner_body():
            count.iadd(1)

          when(frame.ge(1), inner_body)

        once(frame.eq(1), once_body)

      when(frame.ge(1), outer_body)

    mod = build_module(make)
    # Loop 1: outer true, once fires, inner when true. State: 1, 1.
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["var_values"][:2], [1, 1])
    # Loops 2 and 4: outer true, once disabled. Correct: count stays 1.
    # A stale once gate re-enters the body and gives count 2 and 4.
    rep2 = run_sim(mod.to_json(), 2)
    self.assertEqual(rep2["var_values"][:2], [2, 1])
    rep4 = run_sim(mod.to_json(), 4)
    self.assertEqual(rep4["var_values"][:2], [4, 1])

if __name__ == "__main__":
  unittest.main()
