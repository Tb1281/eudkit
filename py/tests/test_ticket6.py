import unittest

from py import (
  ACT_COUNT,
  DisplayText,
  Func,
  Trigger,
  Var,
  after,
  build_module,
  every,
  func,
  on_falling,
  on_rising,
  onFalling,
  onRising,
  once,
  until,
  when,
  while_loop,
  whileLoop,
)

from py.tests.helpers import run_sim
class Ticket6Tests(unittest.TestCase):
  def test_1t_inline_leaf_packing_callback_and_context_manager(self):
    # Context manager form
    def make_cm():
      x = Var(10)
      y = Var(20)
      with when(x == 10):
        x.set(42)
        y.iadd(5)
        DisplayText("when executed")

    mod_cm = build_module(make_cm)
    rep_cm = run_sim(mod_cm.to_json(), 1)
    self.assertEqual(rep_cm["static_cost"]["triggers"], 1)  # 1 leaf trigger, scalars demoted to Cells
    self.assertEqual(rep_cm["executed_cost"]["triggers"], 1)
    self.assertEqual(rep_cm["var_values"][0], 42)
    self.assertEqual(rep_cm["var_values"][1], 25)

    # Callback form
    def make_cb():
      x = Var(10)
      y = Var(20)

      def body():
        x.set(42)
        y.iadd(5)
        DisplayText("when executed")

      when(x.eq(10), body)

    mod_cb = build_module(make_cb)
    self.assertEqual(mod_cm.to_json(), mod_cb.to_json())

  def test_consecutive_inline_when_blocks_merge_up_to_64a(self):
    # Neither body writes the gate variable, so the sibling merge is legal
    # (#79's RAW guard only splits bodies that touch a variable the gate
    # reads; the hazardous variant is pinned in test_ticket79).
    def make():
      gate = Var(1)
      b = Var(2)
      c = Var(3)
      with when(gate == 1):
        b.set(100)
      with when(gate == 1):
        c.set(200)

    mod = build_module(make)
    rep = run_sim(mod.to_json(), 1)
    self.assertEqual(rep["static_cost"]["triggers"], 1)  # 1 leaf trigger, scalars demoted to Cells
    self.assertEqual(rep["executed_cost"]["triggers"], 1)
    self.assertEqual(rep["var_values"][1], 100)
    self.assertEqual(rep["var_values"][2], 200)

  def test_nested_when_conjoins_with_cse_and_comparison_fusion(self):
    def make():
      x = Var(5)
      y = Var(10)
      target = Var(0)
      with when([x >= 5, x >= 3]):
        with when([y <= 10, y >= 10]):
          target.set(999)

    mod = build_module(make)
    rep = run_sim(mod.to_json(), 1)
    self.assertEqual(rep["static_cost"]["triggers"], 1)  # 1 leaf trigger, scalars demoted to Cells
    self.assertEqual(rep["executed_cost"]["triggers"], 1)
    self.assertEqual(rep["var_values"][2], 999)

  def test_once_self_disables_at_0t_executed(self):
    def make():
      cnt = Var(0)
      trig = Var(1)
      with once(trig == 1):
        cnt.iadd(1)

    mod = build_module(make)
    # Loop 1
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["static_cost"]["triggers"], 1)  # 1 leaf trigger, scalars demoted to Cells
    self.assertEqual(rep1["executed_cost"]["triggers"], 1)
    self.assertEqual(rep1["var_values"][0], 1)

    # Loop 2
    rep2 = run_sim(mod.to_json(), 2)
    self.assertEqual(rep2["executed_cost"]["triggers"], 0)
    self.assertEqual(rep2["var_values"][0], 1)

  def test_until_context_manager(self):
    def make():
      tick = Var(0)
      flag = Var(0)
      with when(tick >= 3):
        flag.set(1)
      with until(flag == 1):
        tick.iadd(1)

    mod = build_module(make)
    rep1 = run_sim(mod.to_json(), 1)
    self.assertEqual(rep1["var_values"][0], 1)

    rep3 = run_sim(mod.to_json(), 3)
    self.assertEqual(rep3["var_values"][0], 3)

    rep4 = run_sim(mod.to_json(), 4)
    self.assertEqual(rep4["var_values"][0], 3)
    self.assertEqual(rep4["var_values"][1], 1)

  def test_on_rising_and_on_falling(self):
    def make_rising():
      input_var = Var(1)
      edge_count = Var(0)
      with on_rising(input_var == 1):
        edge_count.iadd(1)

    mod_r = build_module(make_rising)
    rep_r1 = run_sim(mod_r.to_json(), 1)
    self.assertEqual(rep_r1["var_values"][1], 1)
    rep_r2 = run_sim(mod_r.to_json(), 2)
    self.assertEqual(rep_r2["var_values"][1], 1)

    def make_falling():
      input_var = Var(1)
      fall_count = Var(0)
      frame = Var(0)
      frame.iadd(1)
      with when(frame >= 2):
        input_var.set(0)
      with on_falling(input_var == 1):
        fall_count.iadd(1)

    mod_f = build_module(make_falling)
    rep_f1 = run_sim(mod_f.to_json(), 1)
    self.assertEqual(rep_f1["var_values"][1], 0)
    rep_f2 = run_sim(mod_f.to_json(), 2)
    self.assertEqual(rep_f2["var_values"][1], 1)
    rep_f3 = run_sim(mod_f.to_json(), 3)
    self.assertEqual(rep_f3["var_values"][1], 1)

  def test_every_and_after(self):
    def make_ev():
      hits = Var(0)
      with every(3):
        hits.iadd(1)

    mod_ev = build_module(make_ev)
    self.assertEqual(run_sim(mod_ev.to_json(), 1)["var_values"][0], 0)
    self.assertEqual(run_sim(mod_ev.to_json(), 2)["var_values"][0], 0)
    self.assertEqual(run_sim(mod_ev.to_json(), 3)["var_values"][0], 1)
    self.assertEqual(run_sim(mod_ev.to_json(), 6)["var_values"][0], 2)

    def make_aft():
      fired = Var(0)
      with after(2):
        fired.iadd(1)

    mod_aft = build_module(make_aft)
    self.assertEqual(run_sim(mod_aft.to_json(), 1)["var_values"][0], 0)
    self.assertEqual(run_sim(mod_aft.to_json(), 2)["var_values"][0], 0)
    self.assertEqual(run_sim(mod_aft.to_json(), 3)["var_values"][0], 1)

  def test_while_loop_and_func(self):
    def make():
      cnt = Var(4)
      acc = Var(0)
      with while_loop(cnt > 0):
        cnt.isub(1)
        acc.iadd(10)

      step = Func(lambda: acc.iadd(5))
      step()
      step()

    mod = build_module(make)
    rep = run_sim(mod.to_json(), 1)
    self.assertEqual(rep["var_values"][0], 0)
    self.assertEqual(rep["var_values"][1], 50)

if __name__ == "__main__":
  unittest.main()
