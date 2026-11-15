import subprocess
import unittest
from pathlib import Path

from py import (
  ACT_COUNT,
  FixedArray,
  PageSlotEPD,
  Trigger,
  TriggerStruct,
  Var,
  arrayDword,
  build_module,
  dword,
  pageSlotEpdWeight,
  word,
)

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
class Ticket7Tests(unittest.TestCase):
  def test_sim_report_labels_costs_pre_assembly(self):
    report = run_sim(build_module(lambda: None).to_json())
    self.assertIs(report["pre_assembly"], True)

  def test_page_slot_epd_weights_are_carry_free(self):
    for b in range(6):
      self.assertEqual(pageSlotEpdWeight(b), 8 << b)
    self.assertEqual(pageSlotEpdWeight(6), 602)
    self.assertEqual(pageSlotEpdWeight(7), 1204)
    with self.assertRaisesRegex(Exception, "EUDSL-BOUNDS"):
      pageSlotEpdWeight(32)

  def test_writable_array_64_allocates_one_page_and_static_access_is_0t_1c(self):
    def make():
      arr = FixedArray(arrayDword, ACT_COUNT)
      self.assertEqual(arr.pageCount, 1)
      self.assertEqual(len(arr.vars), 64)
      arr.set(5, 777)
      out = Var(0)
      Trigger(conds=[arr.get(5).eq(777)], acts=[out.act_set(1)])

    mod = build_module(make)
    report = run_sim(mod.to_json())
    self.assertEqual(report["static_cost"]["triggers"], 3)
    self.assertEqual(report["executed_cost"]["triggers"], 2)
    self.assertEqual(report["var_values"][5], 777)
    self.assertEqual(report["var_values"][64], 1)

  def test_clear_and_set_all_execute_one_firing_per_page(self):
    def make():
      arr = FixedArray(arrayDword, 128)
      self.assertEqual(arr.pageCount, 2)
      arr.clear()

    report = run_sim(build_module(make).to_json())
    self.assertEqual(report["static_cost"]["triggers"], 4)
    self.assertEqual(report["executed_cost"]["triggers"], 2)
    self.assertEqual(report["executed_cost"]["actions"], 128)
    self.assertTrue(all(v == 0 for v in report["var_values"]))

  def test_dynamic_page_slot_epd_indexing_emits_log2_bits(self):
    def make():
      arr = FixedArray(arrayDword, 64)
      index = Var(42)
      arr.set(index, 1234)

    report = run_sim(build_module(make).to_json())
    # 6 address-bit triggers + reset + pointer write + backing TriggerPage.
    self.assertEqual(report["static_cost"]["triggers"], 9)
    self.assertEqual(report["var_values"][42], 1234)

  def test_dynamic_read_materializes_indexed_element(self):
    def make():
      arr = FixedArray(arrayDword, 4)
      arr.set(0, 11)
      arr.set(3, 44)
      index = Var(3)
      out = arr.get(index)
      result = Var(0)
      result.set(out)
      return result

    holder = {}
    mod = build_module(lambda: holder.setdefault("result", make()))
    report = run_sim(mod.to_json())
    result_id = holder["result"].id
    self.assertEqual(report["var_values"][result_id], 44)

  def test_const_array_allocates_zero_pages_and_folds(self):
    def make():
      table = FixedArray.const([10, 20, 30, 40])
      self.assertEqual(table.pageCount, 0)
      self.assertEqual(int(table.get(2)), 30)
      always = table.get(2).eq(30)
      never = table.get(2).eq(31)
      self.assertEqual(always["condtype"], 22)
      self.assertEqual(never["condtype"], 23)
      r = Var(0)
      if int(table.get(2)) == 30:
        r.set(1)
      return r

    holder = {}
    mod = build_module(lambda: holder.setdefault("r", make()))
    report = run_sim(mod.to_json())
    self.assertEqual(report["static_cost"]["triggers"], 1)
    self.assertEqual(report["var_values"][holder["r"].id], 1)

  def test_struct_array_allocates_one_page_per_element(self):
    Point = TriggerStruct("Point", {"x": dword(), "y": word()})

    def make():
      points = FixedArray(Point, 2)
      self.assertEqual(points.pageCount, 2)
      self.assertEqual(points.get(0).pageIndex, 0)
      self.assertEqual(points.get(1).pageIndex, 1)
      points.get(1).x.set(7)
      points.get(1).y.set(9)
      out = Var(0)
      out.set(points.get(1).x)
      return out

    holder = {}
    mod = build_module(lambda: holder.setdefault("out", make()))
    report = run_sim(mod.to_json())
    self.assertEqual(report["var_values"][holder["out"].id], 7)

  def test_page_slot_epd_runtime_address(self):
    def make():
      base = Var(100)
      index = Var(5)
      epd = PageSlotEPD(base, index, 6)
      out = Var(0)
      out.set(epd)
      return out

    holder = {}
    mod = build_module(lambda: holder.setdefault("out", make()))
    report = run_sim(mod.to_json(), 2)
    self.assertEqual(report["var_values"][holder["out"].id], 140)

  def test_byte_identical_hir_parity_with_typescript(self):
    def prog():
      arr = FixedArray(arrayDword, 4)
      arr.set(0, 11)
      arr.set(3, 44)
      index = Var(3)
      out = arr.get(index)
      result = Var(0)
      result.set(out)
      table = FixedArray.const([10, 20, 30, 40])
      Trigger(conds=[table.get(2).eq(30)], acts=[result.act_set(1)])

    py_hir_json = build_module(prog).to_json()

    ts_script = """
import { FixedArray, Trigger, Var, arrayDword, buildModule } from "./ts/index.ts";

const mod = buildModule(() => {
  const arr = FixedArray(arrayDword, 4);
  arr.set(0, 11);
  arr.set(3, 44);
  const index = Var(3);
  const out = arr.get(index);
  const result = Var(0);
  result.set(out);
  const table = FixedArray.const([10, 20, 30, 40]);
  new Trigger({ conds: [table.get(2).eq(30)], acts: [result.act_set(1)] });
});

process.stdout.write(mod.toJson());
"""
    res = subprocess.run(
      ["node", "--input-type=module", "-e", ts_script],
      cwd=str(REPO_ROOT),
      check=True,
      capture_output=True,
      text=True,
    )
    ts_hir_json = res.stdout
    self.assertEqual(py_hir_json, ts_hir_json)

    report = run_sim(py_hir_json)
    self.assertEqual(report["var_values"][5], 44)
    self.assertEqual(report["var_values"][6], 1)

  def test_const_array_rejects_mutation(self):
    table = FixedArray.const([1, 2, 3])
    with self.assertRaisesRegex(Exception, "read-only"):
      table.set(0, 9)
    with self.assertRaisesRegex(Exception, "out of bounds"):
      table.get(3)

if __name__ == "__main__":
  unittest.main()
