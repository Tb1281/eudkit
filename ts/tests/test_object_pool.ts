/**
 * T-ABS-VIEW-POOL: `ObjectPool` free-list and storage semantics.
 *
 * Mirror of `py/tests/test_object_pool.py`; the shared frontend proof lives in
 * `crates/eudkit-cli/tests/view_pool_runtime.rs`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { Var, buildModule, when } from "../index.ts";
import { ObjectPool, POOL_NULL } from "../queue.ts";
import { EudslError, POOL_NULL as VIEW_POOL_NULL } from "../view.ts";
import { runSim } from "./helpers.ts";

function run(fn: () => void, loops = 1) {
  return runSim(buildModule(fn).toJson(), loops);
}

function values(report: ReturnType<typeof runSim>, ids: number[]): number[] {
  return ids.map((id) => report.var_values[id]);
}

function codeOf(err: unknown): string {
  return err instanceof EudslError ? err.code : "";
}

test("fresh pool allocates in order then returns POOL_NULL", () => {
  const ids: number[] = [];
  const report = run(() => {
    const pool = new ObjectPool(4);
    for (let i = 0; i < 5; i++) {
      ids.push(pool.allocate().id);
    }
  });
  assert.deepEqual(values(report, ids), [0, 1, 2, 3, POOL_NULL]);
});

test("release is LIFO", () => {
  const ids: number[] = [];
  const report = run(() => {
    const pool = new ObjectPool(4);
    pool.allocate();
    pool.allocate();
    pool.release(1);
    ids.push(pool.allocate().id);
  });
  assert.deepEqual(values(report, ids), [1]);
});

test("full and empty flip at the right points", () => {
  let hitId = 0;
  const report = run(() => {
    const hit = Var(0);
    hitId = hit.id;
    const pool = new ObjectPool(2);
    when(pool.empty(), () => {
      hit.iadd(1);
    });
    pool.allocate();
    pool.allocate();
    when(pool.full(), () => {
      hit.iadd(1);
    });
  });
  assert.equal(report.var_values[hitId], 2);
});

test("clear restores the fresh order", () => {
  const ids: number[] = [];
  const report = run(() => {
    const pool = new ObjectPool(3);
    pool.allocate();
    pool.allocate();
    pool.clear();
    ids.push(pool.allocate().id);
  });
  assert.deepEqual(values(report, ids), [0]);
});

test("storage round-trips through constant and runtime indices", () => {
  let constantId = 0;
  let runtimeId = 0;
  const report = run(() => {
    const pool = new ObjectPool(4);
    pool.set(1, 42);
    constantId = (pool.get(1) as any).id;
    const index = Var(1);
    runtimeId = (pool.get(index) as any).id;
  });
  assert.equal(report.var_values[constantId], 42);
  assert.equal(report.var_values[runtimeId], 42);
});

test("capacity validation", () => {
  const cases: [number, string][] = [
    [0, "EUDSL-BOUNDS"],
    [-1, "EUDSL-BOUNDS"],
    [0xffffffff, "EUDSL-RANGE"],
    [true as unknown as number, "EUDSL-TYPE"],
  ];
  for (const [capacity, code] of cases) {
    assert.throws(
      () => buildModule(() => new ObjectPool(capacity)),
      (err) => codeOf(err) === code,
    );
  }
});

test("POOL_NULL is the shared sentinel", () => {
  assert.equal(POOL_NULL, 0xffffffff);
  assert.equal(POOL_NULL, VIEW_POOL_NULL);
});
