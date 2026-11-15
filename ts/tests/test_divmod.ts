/**
 * Cycle 2 ticket 03: one-ladder `divmod`.
 *
 * The dynamic-divisor restoring ladder already materialises `quot` and `rem`;
 * `div` and `mod` each pay for the whole ladder and copy one result. `divmod`
 * exposes both from a single ladder:
 *
 * - `a.divmod(b)` and the exported `divmod(a, b)` helper return a
 *   `[quot, rem]` pair of fresh `Var`s;
 * - `den == 0` keeps the current contract and yields `[0, dividend]`;
 * - constant divisors reuse the constant-divisor ladder and still return both
 *   values;
 * - `div` and `mod` keep their current single-result HIR (the existing
 *   `test_ticket55` / `test_ticket102` pins move only by the two #122 reset
 *   actions).
 *
 * Cost pins for one paired site over `a = 1000`, `b = 7` (cycle 4 spec):
 * static 259 T / 96 C / 743 A and 623,688 B total, executed 271 / 68 / 4,789.
 * The single-result `div` site is 259 / 96 / 741 and 271 / 68 / 4,787, so the
 * pair costs exactly the two extra copy-back actions of the second result.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { Var, buildModule, divmod } from "../index.ts";
import { runSim } from "./helpers.ts";

const DIV_NUM = [
  0, 1, 7, 255, 1000, 65535, 65536, 0x12345678, 0x7fffffff, 0x80000000,
  0xfffffffe, 0xffffffff,
];
const DIV_DEN = [
  0, 1, 3, 7, 255, 65535, 0x7fffffff, 0x80000000, 0xffffffff,
];

const CONST_DIVISORS = [1, 2, 3, 7, 16, 0xffffffff];
const CONST_DIVIDENDS = [0, 1, 7, 255, 1000, 0xffffffff];

const DIVMOD_SITE_STATIC = {
  triggers: 259,
  conditions: 96,
  actions: 743,
  total_bytes: 623688,
};
const DIVMOD_SITE_EXECUTED = { triggers: 271, conditions: 68, actions: 4789 };

const BATCH = 30;

function native(a: number, b: number): [number, number] {
  const x = a >>> 0;
  const y = b >>> 0;
  if (y === 0) return [0, x];
  return [Math.floor(x / y) >>> 0, (x % y) >>> 0];
}

function runBatched(
  cells: readonly (readonly [number, number])[],
  emit: (a: number, b: number) => [number, number],
): [[number, number], [number, number]][] {
  const out: [[number, number], [number, number]][] = [];
  for (let start = 0; start < cells.length; start += BATCH) {
    const batch = cells.slice(start, start + BATCH);
    const slots: [number, number][] = [];
    const mod = buildModule(() => {
      for (const [a, b] of batch) slots.push(emit(a, b));
    });
    const values = runSim(mod.toJson(), 1).var_values;
    batch.forEach((cell, i) => {
      out.push([
        [cell[0], cell[1]],
        [values[slots[i][0]], values[slots[i][1]]],
      ]);
    });
  }
  return out;
}

test("Ticket 03: divmod 108-cell dynamic lattice incl. den == 0", () => {
  const cells = DIV_NUM.flatMap(
    (a): [number, number][] => DIV_DEN.map((b) => [a, b]),
  );
  assert.equal(cells.length, 108);
  const results = runBatched(cells, (a, b) => {
    const x = Var(a);
    const y = Var(b);
    const [q, r] = divmod(x, y);
    return [q.id, r.id];
  });
  for (const [cell, got] of results) {
    assert.deepEqual(got, native(cell[0], cell[1]), `divmod(${cell[0]}, ${cell[1]})`);
  }
});

test("Ticket 03: Var.divmod matches the exported helper", () => {
  const cells = DIV_NUM.flatMap(
    (a): [number, number][] => DIV_DEN.map((b) => [a, b]),
  );
  const results = runBatched(cells, (a, b) => {
    const x = Var(a);
    const y = Var(b);
    const [q, r] = x.divmod(y);
    return [q.id, r.id];
  });
  for (const [cell, got] of results) {
    assert.deepEqual(got, native(cell[0], cell[1]), `${cell[0]}.divmod(${cell[1]})`);
  }
});

test("Ticket 03: results are fresh Vars and alias-safe", () => {
  const captured: { q?: number; r?: number } = {};
  const mod = buildModule(() => {
    let q = Var(1000);
    let r = Var(7);
    const ids = new Set([q.id, r.id]);
    [q, r] = divmod(q, r);
    assert.equal(ids.has(q.id), false);
    assert.equal(ids.has(r.id), false);
    q.iadd(r);
    captured.q = q.id;
    captured.r = r.id;
  });
  const values = runSim(mod.toJson(), 1).var_values;
  assert.equal(values[captured.q!], 142 + 6);
  assert.equal(values[captured.r!], 6);
});

test("Ticket 03: constant-divisor divmod matches the dynamic path", () => {
  for (const c of CONST_DIVISORS) {
    for (const a of CONST_DIVIDENDS) {
      const captured: { ids?: [number, number, number, number] } = {};
      const mod = buildModule(() => {
        const x = Var(a);
        const [qc, rc] = divmod(x, c);
        const xd = Var(a);
        const yd = Var(c);
        const [qd, rd] = divmod(xd, yd);
        captured.ids = [qc.id, rc.id, qd.id, rd.id];
      });
      const values = runSim(mod.toJson(), 1).var_values;
      const [qc, rc, qd, rd] = captured.ids!;
      const want = native(a, c);
      assert.deepEqual([values[qc], values[rc]], want, `const ${a}/${c}`);
      assert.deepEqual([values[qd], values[rd]], want, `dynamic ${a}/${c}`);
    }
  }
});

test("Ticket 03: one paired divmod site has the pinned cost", () => {
  const captured: { q?: number; r?: number } = {};
  const mod = buildModule(() => {
    const a = Var(1000);
    const b = Var(7);
    const [q, r] = divmod(a, b);
    captured.q = q.id;
    captured.r = r.id;
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(
    {
      triggers: report.static_cost.triggers,
      conditions: report.static_cost.conditions,
      actions: report.static_cost.actions,
      total_bytes: report.static_cost.total_bytes,
    },
    DIVMOD_SITE_STATIC,
  );
  assert.deepEqual(
    {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
    DIVMOD_SITE_EXECUTED,
  );
  // The two-ladder `div` + `mod` baseline is 667 executed triggers.
  assert.ok(report.executed_cost.triggers < 667);
});
