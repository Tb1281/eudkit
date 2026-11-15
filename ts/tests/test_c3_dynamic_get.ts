/**
 * Issue #86 (C3): size-gated `FixedArray.get` via the PageSlotEPD reader.
 *
 * From `length >= DYNAMIC_GET_MIN_LENGTH` a dynamic `get` reads the element
 * through the `CurrentPlayer` register instead of unrolling one
 * `when(index.eq(k))` branch per element: seed CP at `EPD(elem0.value)`, add
 * one carry-free PageSlotEPD weight per index bit, reset `out`, run 32 guarded
 * bit tests, then reset CP to the canonical loop-start `0` (ADR 0004; the
 * spike's `zero` policy).
 *
 * Measured on this branch (reader-only module, debug simulator):
 *
 * | N   | unroll static/exec | reader static/exec | reader bytes |
 * |-----|--------------------|--------------------|--------------|
 * | 63  | 190 / 66           | — (below gate)     | —            |
 * | 64  | 193 / 67           | 42 / 41            | 101,144      |
 * | 65  | 197 / 68           | 44 / 42            | 105,960      |
 * | 128 | 386 / 131          | 44 / 42            | 105,960      |
 * | 256 | 772 / 259          | 47 / 43            | 113,184      |
 *
 * The 64 threshold keeps the cost-gate fixture (N=4) on the unroll, so
 * `cost_gate.expected.json` stays byte-identical. Spike #80 provenance:
 * `docs/spikes/c3-dynamic-get/README.md`.
 *
 * CP contract: the reader treats `CurrentPlayer` as scratch and leaves it at
 * `0`. A non-zero ambient CP is not preserved; a `withCP` cursor that moved
 * re-anchors on exit, one that did not is silently landed on `0`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  DYNAMIC_GET_MIN_LENGTH,
  EPD_CURRENT_PLAYER,
  FixedArray,
  Var,
  arrayDword,
  buildModule,
  when,
  withCP,
  type FixedArrayImpl,
  type VarImpl,
} from "../index.ts";

import { runSim, runStatic } from "./helpers.ts";

interface ReaderCostCase {
  length: number;
  static: [number, number, number];
  triggerBytes: number;
  totalBytes: number;
  executed: [number, number, number];
}

const READER_COST_CASES: ReaderCostCase[] = [
  {
    length: 64,
    static: [42, 70, 105],
    triggerBytes: 101_136,
    totalBytes: 101_144,
    executed: [41, 70, 4],
  },
  {
    length: 65,
    static: [44, 71, 170],
    triggerBytes: 105_952,
    totalBytes: 105_960,
    executed: [42, 71, 4],
  },
  {
    length: 128,
    static: [44, 71, 170],
    triggerBytes: 105_952,
    totalBytes: 105_960,
    executed: [42, 71, 4],
  },
  {
    length: 256,
    static: [47, 72, 299],
    triggerBytes: 113_176,
    totalBytes: 113_184,
    executed: [43, 72, 4],
  },
];

function readerOnly(length: number): () => void {
  return () => {
    const arr = FixedArray(arrayDword, length);
    arr.get(Var(1));
  };
}

/** The pre-#86 read path, used as the parity reference above the gate. */
function unrollGet(arr: FixedArrayImpl<VarImpl>, index: VarImpl): VarImpl {
  const out = Var(0);
  for (let k = 0; k < arr.length; k++) {
    const elem = arr.vars[k];
    when(index.eq(k), () => out.set(elem));
  }
  return out;
}

test("dynamic get gate threshold is pinned", () => {
  // Mirrored in py/array.py; there is no shared constants module, so both
  // suites pin the literal.
  assert.equal(DYNAMIC_GET_MIN_LENGTH, 64);
});

test("N=63 stays on the unroll and N=64 switches to the reader", () => {
  const static63 = runStatic(buildModule(readerOnly(63)).toJson()).static_cost;
  assert.deepEqual(
    [static63.triggers, static63.conditions, static63.actions],
    [190, 63, 379],
    "N=63 must stay on the 3N+pages unroll (3*63+1)",
  );

  const static64 = runStatic(buildModule(readerOnly(64)).toJson()).static_cost;
  assert.deepEqual(
    [static64.triggers, static64.conditions, static64.actions],
    [42, 70, 105],
    "N=64 is the first reader size (35+bits+pages)",
  );
});

for (const probe of READER_COST_CASES) {
  test(`cost: dynamic get reader N=${probe.length}`, () => {
    const js = buildModule(readerOnly(probe.length)).toJson();
    const staticCost = runStatic(js).static_cost;
    assert.deepEqual(
      [staticCost.triggers, staticCost.conditions, staticCost.actions],
      probe.static,
    );
    assert.equal(staticCost.trigger_bytes, probe.triggerBytes);
    assert.equal(staticCost.total_bytes, probe.totalBytes);

    const executed = runSim(js, 1).executed_cost;
    assert.deepEqual(
      [executed.triggers, executed.conditions, executed.actions],
      probe.executed,
    );
  });
}

test("N=256 reproduces the spike headline", () => {
  const js = buildModule(readerOnly(256)).toJson();
  assert.equal(runStatic(js).static_cost.triggers, 47);
  assert.equal(runStatic(js).static_cost.total_bytes, 113_184);
  assert.equal(runSim(js, 1).executed_cost.triggers, 43);
});

function readerVsUnroll(
  length: number,
  indices: readonly number[],
): { values: number[]; ids: Record<string, number> } {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, length);
    for (let i = 0; i < length; i++) arr.set(i, 1000 + i);
    for (const idx of indices) {
      ids[`reader${idx}`] = arr.get(Var(idx)).id;
      ids[`unroll${idx}`] = unrollGet(arr, Var(idx)).id;
    }
  });
  return { values: runSim(mod.toJson(), 1).var_values, ids };
}

test("reader matches the unroll including cross-page indices", () => {
  const cases: Record<number, readonly number[]> = {
    64: [0, 63],
    65: [0, 63, 64],
    128: [0, 63, 64, 127],
    256: [0, 63, 64, 127, 128, 191, 192, 255],
  };
  for (const [lengthText, indices] of Object.entries(cases)) {
    const length = Number(lengthText);
    const { values, ids } = readerVsUnroll(length, indices);
    for (const idx of indices) {
      assert.equal(values[ids[`reader${idx}`]], 1000 + idx, `reader N=${length} idx ${idx}`);
      assert.equal(values[ids[`unroll${idx}`]], 1000 + idx, `unroll N=${length} idx ${idx}`);
    }
  }
});

test("out-of-range index matches the unroll zero", () => {
  const cases: Record<number, readonly number[]> = {
    64: [64, 300],
    65: [65, 300],
    256: [256, 300, 1024],
  };
  for (const [lengthText, indices] of Object.entries(cases)) {
    const length = Number(lengthText);
    const { values, ids } = readerVsUnroll(length, indices);
    for (const idx of indices) {
      assert.equal(values[ids[`reader${idx}`]], 0, `reader N=${length} idx ${idx}`);
      assert.equal(values[ids[`unroll${idx}`]], 0, `unroll N=${length} idx ${idx}`);
    }
  }
});

test("two reads at one site reseed CP", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    for (let i = 0; i < 64; i++) arr.set(i, 2000 + i);
    const idx = Var(3);
    ids.a = arr.get(idx).id;
    ids.b = arr.get(Var(11)).id;
  });
  const values = runSim(mod.toJson(), 1).var_values;
  assert.deepEqual([values[ids.a], values[ids.b]], [2003, 2011]);
});

test("replayed read seeds afresh on every pass", () => {
  // A read inside a loop body must not inherit the previous pass's CP.
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    for (let i = 0; i < 64; i++) arr.set(i, 3000 + i);
    const phase = Var(0);
    const out = Var(0);
    ids.out = out.id;
    [5, 63, 64, 0].forEach((idx, p) => {
      when(phase.eq(p), () => out.set(arr.get(Var(idx))));
    });
    phase.iadd(1);
  });
  // 5 -> 3005, 63 -> 3063, 64 is out of range -> 0, 0 -> 3000.
  assert.equal(runSim(mod.toJson(), 4).var_values[ids.out], 3000);
});

test("reader outside withCP leaves CP at zero", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    for (let i = 0; i < 64; i++) arr.set(i, 3000 + i);
    ids.out = arr.get(Var(5)).id;
  });
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.out], 3005);
  assert.equal(report.external_epds[String(EPD_CURRENT_PLAYER)] ?? 0, 0);
});

test("reader inside an unmoved withCP lands CP on zero (documented hazard)", () => {
  // `CPCursor.restore()` only re-anchors when the build-time offset is
  // non-zero, so a body that reads without hopping never restores the ambient
  // CP. This pins today's contract, not a desired one.
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    for (let i = 0; i < 64; i++) arr.set(i, 3000 + i);
    withCP(7000, () => {
      ids.out = arr.get(Var(5)).id;
    });
  });
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.out], 3005);
  assert.equal(report.external_epds[String(EPD_CURRENT_PLAYER)] ?? 0, 0);
});

test("reader inside a moved withCP restores the base", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 64);
    for (let i = 0; i < 64; i++) arr.set(i, 3000 + i);
    withCP(7000, (cp) => {
      cp.moveTo(3);
      ids.out = arr.get(Var(5)).id;
    });
  });
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.out], 3005);
  assert.equal(report.external_epds[String(EPD_CURRENT_PLAYER)] ?? 0, 7000);
});

test("guard adds conditions only: bits + 32 bit tests + 32 guards", () => {
  const static256 = runStatic(buildModule(readerOnly(256)).toJson()).static_cost;
  assert.equal(static256.conditions, 8 + 32 + 32);
});

test("guard adds no trigger: seed + reset + zero + bits + 32 + pages", () => {
  const static64 = runStatic(buildModule(readerOnly(64)).toJson()).static_cost;
  const static256 = runStatic(buildModule(readerOnly(256)).toJson()).static_cost;
  assert.equal(static64.triggers, 1 + 1 + 6 + 1 + 32 + 1);
  assert.equal(static256.triggers, 4 + 1 + 8 + 1 + 32 + 1);
});
