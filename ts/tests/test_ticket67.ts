/**
 * Ticket #67: coalesce adjacent variable transfers inside temporal bodies.
 *
 * The opt pipeline used to batch adjacent `VarTransfer` statements only in
 * `module.stmts`; every `when`/`once`/`while` body (and every
 * `until`/`edge`/`every`/`after` desugar expansion) fired one page per
 * transfer. ADR 0001 §3 records the unconditional rule, so this is a defect
 * against the design, not a deferred decision.
 *
 * These pins are red before the fix (body transfers stay split) and green after
 * (the same `can_coalesce_transfer` rule runs over every statement list):
 *
 * - `when` body N=2: 4T/1C/71A/9,644 B static, 4T/1C/71A executed
 *   (was 5T/1C/72A/12,052 B and 6T/1C/136A);
 * - `when` body N=8: 4T/1C/83A/9,668 B static, 4T/1C/83A executed
 *   (was 11T/1C/90A/26,524 B and 18T/1C/538A);
 * - `while` body N=2: 4T/1C/72A/9,644 B static, 9T/3C/144A executed
 *   (was 5T/1C/73A/12,052 B and 13T/3C/274A);
 * - `while` body N=8: 4T/1C/84A/9,668 B static, 9T/3C/168A executed
 *   (was 11T/1C/91A/26,524 B and 37T/3C/1,078A);
 * - `once` body N=2/N=8: 4T/1C/71A/9,644 B and 4T/1C/83A/9,668 B static,
 *   4T/1C/71A and 4T/1C/83A executed — the shared `optimize_stmts` recursion
 *   makes `once` cost the same as `when`;
 * - `else_body` (JSON surgery, no public surface yet) N=2/N=8: 5T/1C/71A/
 *   12,052 B and 5T/1C/83A/12,076 B static; 2T/1C/2A executed while the gate
 *   is true and 4T/1C/69A / 4T/1C/81A executed when the else arm runs (the
 *   extra trigger over the body case is the else-branch machinery, 2,408 B).
 *
 * The guard rules do not relax inside a body: RAW, WAR, non-additive WAW,
 * duplicate source, and a `VarStore` in the middle all still split the run
 * (each pins 5 static triggers versus 4 for a legal run); additive WAW still
 * coalesces; 65 adjacent transfers still form two batches.
 * The same probes live in `py/tests/test_ticket67.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, type SimReport } from "./helpers.ts";
import {
  SetMemory,
  SetTo,
  Var,
  buildModule,
  once,
  when,
  whileLoop,
  type Var as VarType,
} from "../index.ts";

const STORE_ADDR = 0x640b58;

/** True-gated `when` body of N adjacent `dst[i].set(src[i])` copies. */
function whenBody(n: number): string {
  return buildModule(() => {
    const gate = Var(1);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    when([gate.eq(1)], () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
    });
  }).toJson();
}

/** N adjacent copies plus `g.iadd(1)` inside `whileLoop(g.lt(2))`. */
function whileBody(n: number): string {
  return buildModule(() => {
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    const g = Var(0);
    whileLoop(g.lt(2), () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
      g.iadd(1);
    });
  }).toJson();
}

/** True-at-start `once` body of N adjacent `dst[i].set(src[i])` copies. */
function onceBody(n: number): string {
  return buildModule(() => {
    const gate = Var(0);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    once([gate.eq(0)], () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
    });
  }).toJson();
}

/**
 * `when` whose N-transfer body is moved into `else_body` by JSON surgery.
 *
 * The DSL has no `else` surface yet (#51 is a decision), so the else arm is
 * exercised at the HIR-JSON seam: build a conventional true-gated `when`, then
 * move its body into `else_body`. `amount = 2` closes the gate so the else arm
 * runs. Returns the HIR JSON plus the N destination var ids.
 */
function elseBody(n: number, amount = 1): { hir: string; ids: number[] } {
  const ids: number[] = [];
  const mod = buildModule(() => {
    const gate = Var(1);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    when([gate.eq(amount)], () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
    });
    ids.push(...dsts.map((dst) => dst.id));
  });
  const doc: any = JSON.parse(mod.toJson());
  const node = doc.module.stmts.find(
    (stmt: any) => stmt.kind === "when" && (stmt.body?.length ?? 0) > 0,
  );
  node.else_body = node.body;
  node.body = [];
  return { hir: JSON.stringify(doc), ids };
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

test("Ticket 67: when body N=2 cost", () => {
  assert.deepEqual(cost(whenBody(2)), [
    [4, 1, 71, 9_644],
    [4, 1, 71],
  ]);
});

test("Ticket 67: when body N=8 cost", () => {
  assert.deepEqual(cost(whenBody(8)), [
    [4, 1, 83, 9_668],
    [4, 1, 83],
  ]);
});

test("Ticket 67: while body N=2 cost", () => {
  assert.deepEqual(cost(whileBody(2)), [
    [4, 1, 72, 9_644],
    [9, 3, 144],
  ]);
});

test("Ticket 67: while body N=8 cost", () => {
  assert.deepEqual(cost(whileBody(8)), [
    [4, 1, 84, 9_668],
    [9, 3, 168],
  ]);
});

test("Ticket 67: static triggers are flat in run length", () => {
  assert.equal(runStatic(whenBody(1)).static_cost.triggers, 4);
  assert.equal(runStatic(whenBody(8)).static_cost.triggers, 4);
  assert.equal(runStatic(whileBody(1)).static_cost.triggers, 4);
  assert.equal(runStatic(whileBody(8)).static_cost.triggers, 4);
});

test("Ticket 67: once body N=2 cost", () => {
  assert.deepEqual(cost(onceBody(2)), [
    [4, 1, 71, 9_644],
    [4, 1, 71],
  ]);
});

test("Ticket 67: once body N=8 cost", () => {
  assert.deepEqual(cost(onceBody(8)), [
    [4, 1, 83, 9_668],
    [4, 1, 83],
  ]);
});

test("Ticket 67: once static triggers are flat in run length", () => {
  assert.equal(runStatic(onceBody(1)).static_cost.triggers, 4);
  assert.equal(runStatic(onceBody(8)).static_cost.triggers, 4);
});

test("Ticket 67: else_body N=2 runs when the gate is true", () => {
  const { hir, ids } = elseBody(2);
  assert.deepEqual(cost(hir), [
    [5, 1, 71, 12_052],
    [2, 1, 2],
  ]);
  const report = runSim(hir, 1);
  assert.deepEqual(
    ids.map((varId) => report.var_values[varId]),
    [0, 0],
  );
});

test("Ticket 67: else_body N=8 runs when the gate is true", () => {
  const { hir } = elseBody(8);
  assert.deepEqual(cost(hir), [
    [5, 1, 83, 12_076],
    [2, 1, 2],
  ]);
});

test("Ticket 67: a closed gate runs the else arm and coalesces it", () => {
  const n2 = elseBody(2, 2);
  assert.deepEqual(cost(n2.hir), [
    [5, 1, 71, 12_052],
    [4, 1, 69],
  ]);
  const report2 = runSim(n2.hir, 1);
  assert.deepEqual(
    n2.ids.map((varId) => report2.var_values[varId]),
    [1, 2],
  );
  const n8 = elseBody(8, 2);
  assert.deepEqual(cost(n8.hir), [
    [5, 1, 83, 12_076],
    [4, 1, 81],
  ]);
  const report8 = runSim(n8.hir, 1);
  assert.deepEqual(
    n8.ids.map((varId) => report8.var_values[varId]),
    Array.from({ length: 8 }, (_, i) => i + 1),
  );
});

interface GuardValues {
  src: number;
  src2: number;
  dst: number;
  dst2: number;
  acc: number;
}

interface GuardRun {
  values: GuardValues;
  /** Emitted static trigger count: 4 for a coalesced run, 5 when a rule splits. */
  staticTriggers: number;
}

/** Run one `when` body and return the named variable values plus cost shape. */
function runGuard(
  body: (
    gate: VarType,
    src: VarType,
    src2: VarType,
    dst: VarType,
    dst2: VarType,
    acc: VarType,
  ) => void,
): GuardRun {
  const ids: Record<keyof GuardValues, number> = {
    src: -1,
    src2: -1,
    dst: -1,
    dst2: -1,
    acc: -1,
  };
  const mod = buildModule(() => {
    const gate = Var(1);
    const src = Var(10);
    const src2 = Var(20);
    const dst = Var(0);
    const dst2 = Var(0);
    const acc = Var(1);
    body(gate, src, src2, dst, dst2, acc);
    ids.src = src.id;
    ids.src2 = src2.id;
    ids.dst = dst.id;
    ids.dst2 = dst2.id;
    ids.acc = acc.id;
  });
  const hir = mod.toJson();
  const staticTriggers = runStatic(hir).static_cost.triggers;
  const report: SimReport = runSim(hir, 1);
  return {
    staticTriggers,
    values: {
      src: report.var_values[ids.src],
      src2: report.var_values[ids.src2],
      dst: report.var_values[ids.dst],
      dst2: report.var_values[ids.dst2],
      acc: report.var_values[ids.acc],
    },
  };
}

test("Ticket 67: RAW hazard splits and values are correct", () => {
  const { values, staticTriggers } = runGuard((gate, src, src2, dst) => {
    when([gate.eq(1)], () => {
      dst.set(src);
      src2.set(dst);
    });
  });
  // A split run keeps per-transfer firing: no batch saves the trigger.
  assert.equal(staticTriggers, 5);
  assert.equal(values.dst, 10);
  assert.equal(values.src2, 10);
});

test("Ticket 67: WAR hazard splits and values are correct", () => {
  const { values, staticTriggers } = runGuard((gate, src, src2, dst) => {
    when([gate.eq(1)], () => {
      dst.set(src);
      src.set(src2);
    });
  });
  assert.equal(staticTriggers, 5);
  assert.equal(values.dst, 10);
  assert.equal(values.src, 20);
});

test("Ticket 67: non-additive WAW splits and last write wins", () => {
  const { values, staticTriggers } = runGuard((gate, src, src2, dst) => {
    when([gate.eq(1)], () => {
      dst.set(src);
      dst.set(src2);
    });
  });
  assert.equal(staticTriggers, 5);
  assert.equal(values.dst, 20);
});

test("Ticket 67: duplicate source splits and both destinations are written", () => {
  const { values, staticTriggers } = runGuard((gate, src, _src2, dst, dst2) => {
    when([gate.eq(1)], () => {
      dst.set(src);
      dst2.set(src);
    });
  });
  assert.equal(staticTriggers, 5);
  assert.equal(values.dst, 10);
  assert.equal(values.dst2, 10);
});

test("Ticket 67: a VarStore joins the run", () => {
  // #96: a store no longer breaks the transfer run. It is a batch member like
  // any transfer: the independent transfers coalesce around it, both sources
  // and the store share one page firing, and the values are unchanged. (Before
  // #96 the store forced a second firing: 5 triggers.)
  const { values, staticTriggers } = runGuard((gate, src, src2, dst, dst2) => {
    when([gate.eq(1)], () => {
      dst.set(src);
      SetMemory(STORE_ADDR, SetTo, 7);
      dst2.set(src2);
    });
  });
  assert.equal(staticTriggers, 4);
  assert.equal(values.dst, 10);
  assert.equal(values.dst2, 20);
});

test("Ticket 67: additive WAW coalesces and sums", () => {
  const { values, staticTriggers } = runGuard(
    (gate, src, src2, _dst, _dst2, acc) => {
      when([gate.eq(1)], () => {
        acc.iadd(src);
        acc.iadd(src2);
      });
    },
  );
  // Additive WAW is the one hazard the rule lets share a batch.
  assert.equal(staticTriggers, 4);
  assert.equal(values.acc, 31);
});

test("Ticket 67: a legal run shares one batch and a hazard does not", () => {
  const legal = buildModule(() => {
    const gate = Var(1);
    const src = Var(10);
    const src2 = Var(20);
    const dst = Var(0);
    const dst2 = Var(0);
    when([gate.eq(1)], () => {
      dst.set(src);
      dst2.set(src2);
    });
  }).toJson();
  const hazard = buildModule(() => {
    const gate = Var(1);
    const src = Var(10);
    const src2 = Var(20);
    const dst = Var(0);
    const dst2 = Var(0);
    when([gate.eq(1)], () => {
      dst.set(src);
      src2.set(dst);
    });
  }).toJson();
  assert.equal(runStatic(legal).static_cost.triggers, 4);
  assert.equal(runStatic(hazard).static_cost.triggers, 5);
});

test("Ticket 67: a 65-transfer body forms two batches and writes every destination", () => {
  const n = 65;
  const ids: number[] = [];
  const mod = buildModule(() => {
    const srcs = Array.from({ length: n }, (_, i) => Var(1000 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    when(true, () => {
      for (let i = 0; i < n; i += 1) dsts[i].set(srcs[i]);
    });
    ids.push(...dsts.map((dst) => dst.id));
  });
  const hir = mod.toJson();
  const report = runSim(hir, 1);
  assert.deepEqual(
    ids.map((varId) => report.var_values[varId]),
    Array.from({ length: n }, (_, i) => 1000 + i),
  );
  // 65 transfers exceed one page capacity: two batches, still O(1) pages.
  assert.equal(runStatic(hir).static_cost.triggers, 8);
});
