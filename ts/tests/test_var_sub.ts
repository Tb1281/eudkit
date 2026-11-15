/**
 * Issue #42: arithmetic subtraction wraps mod 2^32.
 *
 * StarCraft's `Subtract` action saturates at zero, so `a - b`, `.isub()`,
 * and `-=` lower to wrapping addition (`a + 1 + (0xFFFFFFFF - b)`) like
 * eudplib. Comparison helpers keep the saturating action.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { SetDeathsX, Subtract, Trigger, Var, buildModule, when } from "../index.ts";

test("binary a - b wraps mod 2^32 (3 - 5 == 0xFFFFFFFE)", () => {
  const mod = buildModule(() => {
    const a = Var(3);
    const b = Var(5);
    const z = Var(0);
    z.set(a.sub(b));
  });
  // The trailing value is the internal complement temporary.
  assert.deepEqual(runSim(mod.toJson()).var_values.slice(0, 3), [
    3,
    5,
    0xfffffffe,
  ]);
});

test("binary 0 - 1 wraps mod 2^32", () => {
  const mod = buildModule(() => {
    const a = Var(0);
    const z = Var(0);
    z.set(a.sub(1));
  });
  assert.deepEqual(runSim(mod.toJson()).var_values, [0, 0xffffffff]);
});

test("isub(number) wraps mod 2^32", () => {
  const mod = buildModule(() => {
    const v = Var(0);
    v.isub(1);
  });
  assert.deepEqual(runSim(mod.toJson()).var_values, [0xffffffff]);
});

test("isub(number) supports negative constants", () => {
  const mod = buildModule(() => {
    const v = Var(1);
    v.isub(-1);
  });
  assert.deepEqual(runSim(mod.toJson()).var_values, [2]);
});

test("isub(v) self-aliasing subtracts the original value", () => {
  const mod = buildModule(() => {
    const v = Var(7);
    v.isub(v);
  });
  // The trailing value is the internal complement temporary.
  assert.equal(runSim(mod.toJson()).var_values[0], 0);
});

test("binary subtraction where dst aliases the rhs reads the original rhs", () => {
  const mod = buildModule(() => {
    const a = Var(3);
    const b = Var(5);
    b.set(a.sub(b));
  });
  // Trailing values are the internal `tmp` and complement temporaries.
  assert.deepEqual(runSim(mod.toJson()).var_values.slice(0, 2), [3, 0xfffffffe]);
});

test("binary subtraction where lhs and rhs are the same var yields 0", () => {
  const mod = buildModule(() => {
    const a = Var(7);
    const z = Var(1);
    z.set(a.sub(a));
  });
  // The trailing value is the internal complement temporary.
  assert.deepEqual(runSim(mod.toJson()).var_values.slice(0, 2), [7, 0]);
});

test("comparison helpers keep saturating semantics (5 vs 3)", () => {
  const mod = buildModule(() => {
    const a = Var(5);
    const b = Var(3);
    const lt = Var(0);
    const ge = Var(0);
    const gt = Var(0);
    const le = Var(0);
    const eq = Var(0);
    const ne = Var(0);
    when(a.lt(b), () => lt.iadd(1));
    when(a.ge(b), () => ge.iadd(1));
    when(a.gt(b), () => gt.iadd(1));
    when(a.le(b), () => le.iadd(1));
    when(a.eq(b), () => eq.iadd(1));
    when(a.ne(b), () => ne.iadd(1));
  });
  // Counters are allocated before the comparison temporaries.
  const counters = runSim(mod.toJson()).var_values.slice(2, 8);
  // Wrapping subtraction would flip lt (true) and ge (false).
  assert.deepEqual(counters, [0, 1, 1, 0, 0, 1]);
});

test("comparison helpers keep saturating semantics (3 vs 5)", () => {
  const mod = buildModule(() => {
    const a = Var(3);
    const b = Var(5);
    const lt = Var(0);
    const ge = Var(0);
    const gt = Var(0);
    const le = Var(0);
    const eq = Var(0);
    const ne = Var(0);
    when(a.lt(b), () => lt.iadd(1));
    when(a.ge(b), () => ge.iadd(1));
    when(a.gt(b), () => gt.iadd(1));
    when(a.le(b), () => le.iadd(1));
    when(a.eq(b), () => eq.iadd(1));
    when(a.ne(b), () => ne.iadd(1));
  });
  const counters = runSim(mod.toJson()).var_values.slice(2, 8);
  assert.deepEqual(counters, [1, 0, 0, 1, 0, 1]);
});

test("the simulator still models raw Subtract as saturating", () => {
  const mod = buildModule(() => {
    const v = Var(0);
    new Trigger({
      conds: [],
      acts: [SetDeathsX(v.value, Subtract, 1, 0, 0xffffffff)],
    });
  });
  assert.deepEqual(runSim(mod.toJson()).var_values, [0]);
});
