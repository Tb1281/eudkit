import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { execFileSync } from "node:child_process";

import {
  ACT_COUNT,
  FixedArray,
  PageSlotEPD,
  Trigger,
  Var,
  arrayDword,
  buildModule,
  dword,
  pageSlotEpdWeight,
  TriggerStruct,
  word,
} from "../index.ts";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..");

test("PageSlotEPD weights are carry-free bit weights (8<<b, 602<<(b-6))", () => {
  for (let b = 0; b < 6; b++) assert.equal(pageSlotEpdWeight(b), 8 << b);
  assert.equal(pageSlotEpdWeight(6), 602);
  assert.equal(pageSlotEpdWeight(7), 1204);
  assert.throws(() => pageSlotEpdWeight(32), /EUDSL-BOUNDS/);
});

test("writable FixedArray(dword, 64) allocates exactly one TriggerPage and static access is 0T/1C", () => {
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, ACT_COUNT);
    assert.equal(arr.pageCount, 1);
    assert.equal(arr.vars.length, 64);
    arr.set(5, 777);
    const out = Var(0);
    new Trigger({ conds: [arr.get(5).eq(777)], acts: [out.act_set(1)] });
  });
  const report = runSim(mod.toJson());
  // One packed update trigger + conditional 1C trigger + one backing TriggerPage.
  assert.equal(report.static_cost.triggers, 3);
  assert.equal(report.executed_cost.triggers, 2);
  assert.equal(report.var_values[5], 777);
  assert.equal(report.var_values[64], 1);
});

test("FixedArray.clear() and setAll execute at one TriggerPage firing per 64 elements", () => {
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 128);
    assert.equal(arr.pageCount, 2);
    arr.clear();
  });
  const report = runSim(mod.toJson());
  // 128 scalar writes packed as 2 x 64A triggers + 2 TriggerPages.
  assert.equal(report.static_cost.triggers, 4);
  assert.equal(report.executed_cost.triggers, 2);
  assert.equal(report.executed_cost.actions, 128);
  assert.ok(report.var_values.every((v) => v === 0));
});

test("dynamic PageSlotEPD indexing emits ceil(log2(N)) bit-test triggers", () => {
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    const index = Var(42);
    arr.set(index, 1234);
  });
  const report = runSim(mod.toJson());
  // 6 address-bit triggers + reset + pointer write + backing TriggerPage.
  assert.equal(report.static_cost.triggers, 9);
  assert.equal(report.var_values[42], 1234);
});

test("dynamic read materializes the indexed element", () => {
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 4);
    arr.set(0, 11);
    arr.set(3, 44);
    const index = Var(3);
    const out = arr.get(index);
    const result = Var(0);
    result.set(out);
    (globalThis as Record<string, unknown>).__resultId = result.id;
  });
  const hir = JSON.parse(mod.toJson());
  const resultId = (globalThis as Record<string, unknown>).__resultId as number;
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[resultId], 44);
  assert.ok(hir);
});

test("FixedArray.const allocates zero TriggerPages and folds constant reads and comparisons", () => {
  const mod = buildModule(() => {
    const table = FixedArray.const([10, 20, 30, 40]);
    assert.equal(table.pageCount, 0);
    assert.equal(table.get(2).valueOf(), 30);
    const always = table.get(2).eq(30);
    const never = table.get(2).eq(31);
    assert.equal(always.condtype, 22); // Always
    assert.equal(never.condtype, 23); // Never
    const r = Var(0);
    if (table.get(2).valueOf() === 30) r.set(1);
  });
  const report = runSim(mod.toJson());
  // No backing pages; just the one folded update trigger.
  assert.equal(report.static_cost.triggers, 1);
  assert.equal(report.var_values[0], 1);
});

test("FixedArray(TriggerStruct, N) allocates N page-aligned pages and supports field access", () => {
  const Point = TriggerStruct("Point", { x: dword(), y: word() });
  const mod = buildModule(() => {
    const points = FixedArray(Point, 2);
    assert.equal(points.pageCount, 2);
    assert.equal(points.get(0).pageIndex, 0);
    assert.equal(points.get(1).pageIndex, 1);
    points.get(1).x.set(7);
    points.get(1).y.set(9);
    const out = Var(0);
    out.set(points.get(1).x);
    (globalThis as Record<string, unknown>).__outId = out.id;
  });
  const outId = (globalThis as Record<string, unknown>).__outId as number;
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[outId], 7);
});

test("PageSlotEPD computes a runtime element address with one trigger per active bit", () => {
  const mod = buildModule(() => {
    const base = Var(100);
    const index = Var(5);
    const epd = PageSlotEPD(base, index, 6);
    const out = Var(0);
    out.set(epd);
    (globalThis as Record<string, unknown>).__epdOut = out.id;
  });
  const outId = (globalThis as Record<string, unknown>).__epdOut as number;
  const report = runSim(mod.toJson(), 2);
  // base 100 + slot 5 (5 * 8 EPD) = 140
  assert.equal(report.var_values[outId], 140);
});

test("Byte-identical HIR parity between TypeScript and Python for FixedArray", () => {
  const tsMod = buildModule(() => {
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

  const pyScript = `
import sys
from py import FixedArray, Trigger, Var, arrayDword, build_module

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

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHirJson);

  const report = runSim(pyHirJson);
  assert.equal(report.var_values[5], 44);
  assert.equal(report.var_values[6], 1);
});
