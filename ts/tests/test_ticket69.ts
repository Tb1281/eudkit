/**
 * Ticket #69: merge sibling `when` blocks with transfer bodies under a guard.
 *
 * `crates/eudkit-opt/src/temporal.rs` already merges adjacent sibling `when`
 * blocks with identical conditions when both bodies are leaf acts (the 64-act
 * merge). Pure transfer bodies fell through unmerged, so the same gate cost
 * `3T + 4T` per extra sibling instead of sharing one conditional group. The
 * merge is only legal when no transfer writes a variable the shared gate reads
 * (the guard); otherwise the second gate must observe the first body's writes.
 *
 * These pins are red before the fix and green after:
 *
 * - N=2 siblings: 4T/1C/71A/9,644 B static, 4T/1C/71A executed
 *   (was 7T/2C/74A/16,868 B and 8T/2C/138A);
 * - N=8 siblings: 4T/1C/83A/9,668 B static, 4T/1C/83A executed
 *   (was 25T/8C/104A/60,236 B and 32T/8C/552A) — O(N) -> O(1);
 * - different-gate pair: 7T/2C/74A/16,868 B static, 8T/2C/138A executed,
 *   values unchanged. This is the mutation pin for the identical-gates
 *   precondition: deleting `w1.conds == w2.conds` in the transfer branch
 *   merges the pair into 5T/1C/72A/12,052 B and 6T/1C/136A. The values are
 *   identical either way, so the cost tuple is the pin;
 * - transfer-order pair: 5T/1C/74A/12,044 B static, 6T/1C/138A executed
 *   (was 7T/2C/76A/16,860 B and 8T/2C/140A), values unchanged — the shared
 *   gate passes the guard here, so this pins post-merge order preservation,
 *   not the guard;
 * - two siblings inside `whileLoop(c.lt(2))`: 6T/2C/74A/14,460 B static,
 *   13T/5C/148A executed (was 9T/3C/77A/21,684 B and 21T/7C/282A);
 * - multi-condition gate (`Memory` + `Switch`): 7T/2C/75A/16,868 B static,
 *   4T/1C/67A executed (was 9T/4C/77A/21,684 B and 5T/2C/67A).
 *
 * The guard is load-bearing. A naive merge corrupts the runtime `writeFrom`
 * capacity guards in `test_ticket9` (sibling `when(pos < cap)` blocks must
 * re-check `pos` after each byte): `AAXYZ` instead of `AAXY` and 438T ->
 * 434T. Here the scan's guard shape stays unmerged at 7T/2C/74A/16,864 B and
 * keeps `h=0` (a naive merge gives 5T static and `h=8`).
 *
 * The same probes live in `py/tests/test_ticket69.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, type SimReport } from "./helpers.ts";
import {
  AtLeast,
  Memory,
  Switch,
  SwitchSet,
  Var,
  buildModule,
  when,
  whileLoop,
} from "../index.ts";

/** N sibling `when`s with one identical-gate transfer each. */
function siblings(n: number): string {
  return buildModule(() => {
    const gate = Var(0);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    for (let i = 0; i < n; i += 1) {
      const dst = dsts[i];
      const src = srcs[i];
      when([gate.eq(0)], () => {
        dst.set(src);
      });
    }
  }).toJson();
}

/** The same N transfers inside one `when` (the hand-written control). */
function oneWhen(n: number): string {
  return buildModule(() => {
    const gate = Var(0);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    when([gate.eq(0)], () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
    });
  }).toJson();
}

/**
 * Two same-gate siblings: pins the post-merge transfer order, not the guard.
 *
 * The shared gate reads var 0; both bodies write vars 1 and 2, so no
 * destination is in the read set and the guard *passes*. The siblings merge
 * and the coalescer preserves the merged order (`x.set(y)` then `y.set(x)`),
 * keeping both at 7 — a guard-less merge produces the same values. The guard
 * pin is the different-gates case below.
 */
function transferOrderPair(): string {
  return buildModule(() => {
    const gate = Var(0);
    const x = Var(5);
    const y = Var(7);
    when([gate.eq(0)], () => {
      x.set(y);
    });
    when([gate.eq(0)], () => {
      y.set(x);
    });
  }).toJson();
}

/**
 * Two adjacent transfer siblings under *different* gates: must not merge.
 *
 * Mutation pin: with `w1.conds == w2.conds` deleted in the transfer branch of
 * the merge step, the pair folds into one gate and the cost drops to
 * 5T/1C/72A/12,052 B static, 6T/1C/136A executed. The runtime values are
 * identical either way, so the cost tuple is the pin.
 */
function differentGatesPair(): string {
  return buildModule(() => {
    const g1 = Var(0);
    const g2 = Var(0);
    const s = Var(5);
    const d = Var(0);
    const e = Var(0);
    when([g1.eq(0)], () => {
      d.set(s);
    });
    when([g2.eq(0)], () => {
      e.set(s);
    });
  }).toJson();
}

/** Two identical-gate transfer siblings inside `whileLoop(c.lt(2))`. */
function whilePair(): string {
  return buildModule(() => {
    const c = Var(0);
    const x = Var(5);
    const y = Var(7);
    const z = Var(0);
    const w = Var(9);
    whileLoop(c.lt(2), () => {
      when([c.lt(2)], () => {
        x.set(y);
      });
      when([c.lt(2)], () => {
        z.set(w);
      });
      c.iadd(1);
    });
  }).toJson();
}

/** A two-condition gate (`Memory` + `Switch`) shared by both siblings. */
function multiCond(): string {
  return buildModule(() => {
    const s = Var(8);
    const h = Var(0);
    const src = Var(9);
    const r = Var(0);
    const conds = [Memory(0x640b58, AtLeast, 1), Switch(1, SwitchSet)];
    when(conds, () => {
      s.set(src);
    });
    when(conds, () => {
      h.set(src);
    });
    r.set(src);
  }).toJson();
}

/** First sibling writes the variable its own gate reads: must not merge. */
function guard(): string {
  return buildModule(() => {
    const g = Var(1);
    const s = Var(8);
    const h = Var(0);
    when([g.eq(1)], () => {
      g.set(s);
    });
    when([g.eq(1)], () => {
      h.set(s);
    });
  }).toJson();
}

function cost(hirJson: string): [number[], number[]] {
  const staticCost = runStatic(hirJson).static_cost;
  const executed = runSim(hirJson, 1).executed_cost;
  return [
    [
      staticCost.triggers,
      staticCost.conditions,
      staticCost.actions,
      staticCost.total_bytes,
    ],
    [executed.triggers, executed.conditions, executed.actions],
  ];
}

test("Ticket 69: N=1 sibling control is unchanged", () => {
  assert.deepEqual(cost(siblings(1)), [
    [4, 1, 69, 9_640],
    [4, 1, 69],
  ]);
});

test("Ticket 69: N=2 siblings cost", () => {
  assert.deepEqual(cost(siblings(2)), [
    [4, 1, 71, 9_644],
    [4, 1, 71],
  ]);
});

test("Ticket 69: N=8 siblings cost", () => {
  assert.deepEqual(cost(siblings(8)), [
    [4, 1, 83, 9_668],
    [4, 1, 83],
  ]);
});

test("Ticket 69: merged pair preserves transfer order", () => {
  assert.deepEqual(cost(transferOrderPair()), [
    [5, 1, 74, 12_044],
    [6, 1, 138],
  ]);
  const report: SimReport = runSim(transferOrderPair(), 1);
  assert.deepEqual([report.var_values[1], report.var_values[2]], [7, 7]);
});

test("Ticket 69: different gates stay unmerged", () => {
  // The identical-gates precondition, pinned by cost: a merge here would
  // evaluate the second sibling's gate once, at the first sibling's
  // evaluation, instead of re-evaluating it after the first body's write.
  // Values are unchanged, so the cost tuple is the pin.
  assert.deepEqual(cost(differentGatesPair()), [
    [7, 2, 74, 16_868],
    [8, 2, 138],
  ]);
  const report: SimReport = runSim(differentGatesPair(), 1);
  assert.deepEqual(report.var_values, [0, 0, 5, 5, 5]);
});

test("Ticket 69: two siblings inside a while body", () => {
  assert.deepEqual(cost(whilePair()), [
    [6, 2, 74, 14_460],
    [13, 5, 148],
  ]);
  const report: SimReport = runSim(whilePair(), 1);
  assert.deepEqual(report.var_values, [2, 7, 7, 9, 9]);
});

test("Ticket 69: multi-condition gate cost and values", () => {
  assert.deepEqual(cost(multiCond()), [
    [7, 2, 75, 16_868],
    [4, 1, 67],
  ]);
  const report: SimReport = runSim(multiCond(), 1);
  assert.deepEqual(report.var_values, [8, 0, 9, 9]);
});

test("Ticket 69: static triggers are flat in sibling count", () => {
  assert.equal(runStatic(siblings(1)).static_cost.triggers, 4);
  assert.equal(runStatic(siblings(8)).static_cost.triggers, 4);
  assert.deepEqual(cost(siblings(8)), cost(oneWhen(8)));
  assert.deepEqual(cost(siblings(2)), cost(oneWhen(2)));
});

test("Ticket 69: merged transfer group writes both destinations through one gate", () => {
  // Transfer-body equivalent of the leaf-act merged-gate pin in
  // `ts/tests/test_ticket6.ts`: two identical-gate sibling transfer bodies
  // become one static conditional group: one condition count, both
  // destinations written, same cost as writing them in one when.
  const [staticCost, executed] = cost(siblings(2));
  assert.equal(staticCost[1], 1); // one shared gate, evaluated once
  assert.equal(executed[1], 1);
  const report: SimReport = runSim(siblings(2), 1);
  // var ids: gate 0, srcs 1..2 (init 1, 2), dsts 3..4 (init 0).
  assert.equal(report.var_values[3], 1);
  assert.equal(report.var_values[4], 2);
});

test("Ticket 69: the guard keeps gate-writing siblings unmerged", () => {
  // The first body writes the gate variable. Merging would fold the second
  // gate into the first evaluation: 5T static and `h=8`, silently changing
  // the program. The guard must reject it.
  assert.equal(cost(guard())[0][0], 7); // still 7 static triggers
  assert.deepEqual(cost(guard())[0], [7, 2, 74, 16_864]);
  const report: SimReport = runSim(guard(), 1);
  assert.deepEqual(report.var_values, [8, 8, 0]); // g, s, h — h stays 0
  const executed = runSim(guard(), 2).executed_cost;
  assert.deepEqual(
    [executed.triggers, executed.conditions, executed.actions],
    [2, 2, 0],
  );
});
