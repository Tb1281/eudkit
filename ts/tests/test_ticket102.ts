/**
 * Ticket #102: shared `mulShiftAdd` and the single-diff gated div ladder.
 *
 * Follow-up to spike #82. Two changes are pinned here:
 *
 * - Var x Var `mul` now routes through `mulShiftAdd` in `var.ts` (the kernel
 *   the camera helper always had), deleting the 528-trigger bit-pair matrix;
 * - Var / Var `div` / `mod` keeps the restoring ladder but replaces the
 *   per-bit `diff` + `subAmt` pair with one hoisted `diff` and a gated
 *   `when([diff.eq(0), den.ge(1)])`, accumulating into a fresh `quot` and
 *   copying back once. `den >= 1` preserves divide-by-zero == 0.
 *
 * Measured on one site (`o.set(x.mul(y))` / `o.set(x.div(y))`):
 *
 * | case       | vars | static T | C    | A   | bytes     |
 * | ---------- | ---- | -------- | ---- | --- | --------- |
 * | mul before | 3    | 529      | 1056 | 529 | 1,273,844 |
 * | mul after  | 5    | 165      | 33   | 392 | 397,328   |
 * | div before | 69   | 354      | 128  | 834 | 852,568   |
 * | div after  | 6    | 291      | 96   | 771 | 700,740   |
 *
 * Cycle 2 ticket 02 splits the 32 multiplier gates into chunks of 16 and gates
 * the high chunk on `b & 0xFFFF0000`. A sparse multiplier skips that chunk
 * (`0x5`: E 77/17/1,351, previously 137/32/2,356); a dense one pays the one
 * extra gate (`0xDEADBEEF`: E 205/33/3,920, previously 203/32/3,918).
 *
 * Cycle 4 hoists `diff.set(den)` above the numerator-bit trigger, so transfer
 * coalescing folds the independent transfers into one page firing per bit. The
 * current pins are: `div` / `mod` static 259/96/741, 623,684 B and executed
 * 271/68/4,787; paired `divmod` static 259/96/743, 623,688 B and executed
 * 271/68/4,789.
 *
 * The correctness lattices (49 mul / 108 div cells in ts; 99 / 180 in py) are
 * the spike's; each batch is built into one module so the whole lattice costs
 * a handful of simulator runs instead of one per cell.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { Var, buildModule } from "../index.ts";
import { runSim } from "./helpers.ts";

const MUL_A = [
  0, 1, 2, 255, 65535, 65536, 0x80000000,
];
const MUL_B = [
  0, 1, 3, 255, 65535, 65536, 0xffffffff,
];

const DIV_NUM = [
  0, 1, 7, 255, 1000, 65535, 65536, 0x12345678, 0x7fffffff, 0x80000000,
  0xfffffffe, 0xffffffff,
];
const DIV_DEN = [
  0, 1, 3, 7, 255, 65535, 0x7fffffff, 0x80000000, 0xffffffff,
];

const MUL_SITE_STATIC = {
  triggers: 165,
  conditions: 33,
  actions: 392,
  trigger_bytes: 397320,
  blob_bytes: 8,
  total_bytes: 397328,
};
const DIV_SITE_STATIC = {
  triggers: 259,
  conditions: 96,
  actions: 741,
  trigger_bytes: 623672,
  blob_bytes: 12,
  total_bytes: 623684,
};

const BATCH = 30;

function runBatched(
  cells: readonly (readonly [number, number])[],
  emit: (a: number, b: number) => number,
): [[number, number], number][] {
  const out: [[number, number], number][] = [];
  for (let start = 0; start < cells.length; start += BATCH) {
    const batch = cells.slice(start, start + BATCH);
    const slots: number[] = [];
    const mod = buildModule(() => {
      for (const [a, b] of batch) slots.push(emit(a, b));
    });
    const values = runSim(mod.toJson(), 1).var_values;
    batch.forEach((cell, i) => {
      out.push([[cell[0], cell[1]], values[slots[i]]]);
    });
  }
  return out;
}

test("Ticket 102: Var x Var mul 49-cell lattice", () => {
  const cells = MUL_A.flatMap(
    (a): [number, number][] => MUL_B.map((b) => [a, b]),
  );
  assert.equal(cells.length, 49);
  const results = runBatched(cells, (a, b) => {
    const x = Var(a);
    const y = Var(b);
    const o = Var(0);
    o.set(x.mul(y));
    return o.id;
  });
  for (const [cell, got] of results) {
    const [a, b] = cell;
    assert.equal(got, Math.imul(a, b) >>> 0, `${a} * ${b}`);
  }
});

test("Ticket 102: mul copy-back is aliasing-safe for both operands", () => {
  const captured: { x?: number; y?: number } = {};
  const mod = buildModule(() => {
    const x = Var(6);
    const y = Var(7);
    const z = Var(3);
    x.set(x.mul(y)); // dst === a
    y.set(z.mul(y)); // dst === b
    captured.x = x.id;
    captured.y = y.id;
  });
  const values = runSim(mod.toJson(), 1).var_values;
  assert.equal(values[captured.x!], 42);
  assert.equal(values[captured.y!], 21);
});

test("Ticket 102: one-site mul static + executed cost", () => {
  const mod = buildModule(() => {
    const a = Var(0xdeadbeef);
    const b = Var(0xffff);
    const o = Var(0);
    o.set(a.mul(b));
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(report.static_cost, MUL_SITE_STATIC);
  // `0xFFFF` is all-zero in its high half, so it takes the chunk-16 early
  // exit. The add fires on every set low bit, and the gate adds one visited
  // trigger/condition; `0xDEADBEEF` / `0xFFFFFFFF` are the dense cases.
  assert.deepEqual(
    {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
    { triggers: 119, conditions: 17, actions: 2345 },
  );
});

test("Ticket 102: chunked multipliers match native products", () => {
  const cases: [number, number][] = [
    [0xdeadbeef, 0x5],
    [0xdeadbeef, 0xffff],
    [0xdeadbeef, 0xdeadbeef],
    [0xdeadbeef, 0],
    [0xdeadbeef, 1],
    [0xdeadbeef, 0xffffffff],
  ];
  for (const [a, b] of cases) {
    const slot: number[] = [];
    const mod = buildModule(() => {
      const o = Var(0);
      o.set(Var(a).mul(Var(b)));
      slot.push(o.id);
    });
    const values = runSim(mod.toJson(), 1).var_values;
    assert.equal(values[slot[0]], Math.imul(a, b) >>> 0, `${a} * ${b}`);
  }
});

function mulExecutedCost(a: number, b: number) {
  const mod = buildModule(() => {
    const o = Var(0);
    o.set(Var(a).mul(Var(b)));
  });
  const report = runSim(mod.toJson(), 1);
  return {
    staticCost: report.static_cost,
    executed: {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
  };
}

test("Ticket 102: small multiplier skips the high chunk", () => {
  const { staticCost, executed } = mulExecutedCost(0xdeadbeef, 0x5);
  assert.deepEqual(staticCost, MUL_SITE_STATIC);
  assert.ok(executed.triggers <= 77, `triggers ${executed.triggers}`);
  assert.ok(executed.conditions <= 17, `conditions ${executed.conditions}`);
  assert.ok(executed.actions <= 1351, `actions ${executed.actions}`);
});

test("Ticket 102: dense multiplier pays at most one gate", () => {
  // Baseline 203/32/3,918: the gate may add at most 2T/2C/4A.
  const { staticCost, executed } = mulExecutedCost(0xdeadbeef, 0xdeadbeef);
  assert.deepEqual(staticCost, MUL_SITE_STATIC);
  assert.ok(executed.triggers <= 205, `triggers ${executed.triggers}`);
  assert.ok(executed.conditions <= 34, `conditions ${executed.conditions}`);
  assert.ok(executed.actions <= 3922, `actions ${executed.actions}`);
});

for (const op of ["div", "mod"] as const) {
  test(`Ticket 102: Var/Var ${op} 108-cell lattice incl. den == 0`, () => {
    const cells = DIV_NUM.flatMap(
      (a): [number, number][] => DIV_DEN.map((b) => [a, b]),
    );
    assert.equal(cells.length, 108);
    const results = runBatched(cells, (a, b) => {
      const x = Var(a);
      const y = Var(b);
      const o = Var(0);
      o.set(op === "div" ? x.div(y) : x.mod(y));
      return o.id;
    });
    for (const [cell, got] of results) {
      const [a, b] = cell;
      const want =
        op === "div"
          ? b
            ? Math.floor(a / b) >>> 0
            : 0
          : b
            ? (a % b) >>> 0
            : a >>> 0;
      assert.equal(got >>> 0, want, `${a} ${op} ${b}`);
    }
  });
}

test("Ticket 102: gated div copy-back is aliasing-safe for both operands", () => {
  const captured: { x?: number; d?: number; e?: number } = {};
  const mod = buildModule(() => {
    const x = Var(1000);
    const y = Var(7);
    x.set(x.div(y)); // dst === num
    const q = Var(1000);
    const d = Var(7);
    d.set(q.div(d)); // dst === den
    const r = Var(1000);
    const e = Var(7);
    e.set(r.mod(e)); // dst === den, mod path
    captured.x = x.id;
    captured.d = d.id;
    captured.e = e.id;
  });
  const values = runSim(mod.toJson(), 1).var_values;
  assert.equal(values[captured.x!], 142);
  assert.equal(values[captured.d!], 142);
  assert.equal(values[captured.e!], 1000 % 7);
});

test("Ticket 102: one-site div static + executed cost", () => {
  const mod = buildModule(() => {
    const a = Var(1000);
    const b = Var(7);
    const o = Var(0);
    o.set(a.div(b));
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(report.static_cost, DIV_SITE_STATIC);
  assert.deepEqual(
    {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
    { triggers: 271, conditions: 68, actions: 4787 },
  );
});

test("Ticket 102: den == 0 skips the gated body", () => {
  const mod = buildModule(() => {
    const a = Var(12345);
    const b = Var(0);
    const o = Var(0);
    o.set(a.div(b));
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(report.static_cost, DIV_SITE_STATIC);
  assert.deepEqual(
    {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
    { triggers: 259, conditions: 96, actions: 4491 },
  );
});
