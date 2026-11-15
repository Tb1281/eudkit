import test from "node:test";
import assert from "node:assert/strict";

import { runSim } from "./helpers.ts";

import {
  buildModule,
  Var,
  lazyVar,
  asReadonly,
  type ReadonlyVar,
  FixedString,
  when,
} from "../index.ts";

// Compile-time assertion that mutating methods are NOT present on ReadonlyVar:
type HasSet<T> = "set" extends keyof T ? true : false;
type HasIadd<T> = "iadd" extends keyof T ? true : false;
type HasIsub<T> = "isub" extends keyof T ? true : false;
type HasActSet<T> = "act_set" extends keyof T ? true : false;

type _AssertNoSet = HasSet<ReadonlyVar> extends false ? true : never;
type _AssertNoIadd = HasIadd<ReadonlyVar> extends false ? true : never;
type _AssertNoIsub = HasIsub<ReadonlyVar> extends false ? true : never;
type _AssertNoActSet = HasActSet<ReadonlyVar> extends false ? true : never;

const _typeCheckAssertions: [_AssertNoSet, _AssertNoIadd, _AssertNoIsub, _AssertNoActSet] = [
  true,
  true,
  true,
  true,
];

test("Ticket 96: ReadonlyVar on eager Var allows reading id, init, bitmask", () => {
  buildModule(() => {
    const v = Var(42, undefined, 0xffff);
    const rv: ReadonlyVar = asReadonly(v);

    assert.equal(rv.init, 42);
    assert.equal(rv.bitmask, 0xffff);
    assert.equal(typeof rv.id, "number");
  });
});

test("Ticket 96: ReadonlyVar supports condition methods eq, ne, ge, le, gt, lt, atLeast, atMost, exactly", () => {
  buildModule(() => {
    const v = Var(10);
    const rv = asReadonly(v);

    const cEq = rv.eq(10);
    assert.ok(cEq);
    const cNe = rv.ne(10);
    assert.ok(cNe);
    const cGe = rv.ge(10);
    assert.ok(cGe);
    const cLe = rv.le(10);
    assert.ok(cLe);
    const cGt = rv.gt(10);
    assert.ok(cGt);
    const cLt = rv.lt(10);
    assert.ok(cLt);

    const cAtLeast = rv.atLeast(10);
    assert.deepEqual(cAtLeast, cGe);

    const cAtMost = rv.atMost(10);
    assert.deepEqual(cAtMost, cLe);

    const cExactly = rv.exactly(10);
    assert.deepEqual(cExactly, cEq);
  });
});

test("Ticket 96: ReadonlyVar works transparently with lazyVar across modules", () => {
  // Declared at module scope outside buildModule
  const globalCounter = lazyVar(123, 0xffffffff);
  const roCounter = asReadonly(globalCounter);

  assert.equal(roCounter.init, 123);
  assert.equal(roCounter.bitmask, 0xffffffff);

  buildModule(() => {
    // Resolves cleanly inside buildModule
    assert.equal(typeof roCounter.id, "number");
    const cond = roCounter.exactly(123);
    assert.ok(cond);

    // Another var can read from ReadonlyVar
    const target = Var(0);
    target.set(roCounter);
    target.iadd(roCounter);
    target.isub(roCounter);

    // FixedString can interpolate ReadonlyVar
    const str = new FixedString(64);
    str.write("counter={:d} hex={:08x}", roCounter, roCounter);
  });
});

test("Ticket 96: ReadonlyVar works as writeFrom position and displayAt line", () => {
  const pos = lazyVar(0);
  const line = lazyVar(0);
  const roPos = asReadonly(pos);
  const roLine = asReadonly(line);

  buildModule(() => {
    const str = new FixedString(16);
    str.writeFrom(roPos, "ok");
    str.printAt(roLine, "ok");
    str.displayAt(roPos);
  });
});

test("Ticket 96: ReadonlyVar is idempotent under asReadonly", () => {
  buildModule(() => {
    const v = Var(5);
    const rv1 = asReadonly(v);
    const rv2 = asReadonly(rv1);
    assert.equal(rv1, rv2);
  });
});

test("Ticket 96: ReadonlyVar works in when() conditions and trigger structures", () => {
  let executed = false;
  buildModule(() => {
    const v = Var(7);
    const rv = asReadonly(v);

    when(rv.atLeast(5), () => {
      executed = true;
    });
  });
  assert.ok(executed);
});

test("Ticket 96: mutating operations are disallowed on ReadonlyVar type", () => {
  buildModule(() => {
    const v = Var(10);
    const rv: ReadonlyVar = asReadonly(v);

    // @ts-expect-error - .set() does not exist on ReadonlyVar
    rv.set;
    // @ts-expect-error - .iadd() does not exist on ReadonlyVar
    rv.iadd;
    // @ts-expect-error - .isub() does not exist on ReadonlyVar
    rv.isub;
    // @ts-expect-error - .imul() does not exist on ReadonlyVar
    rv.imul;
    // @ts-expect-error - .act_set() does not exist on ReadonlyVar
    rv.act_set;
  });
});

test("Ticket 96: simulation: consumer reads from owner ReadonlyVar", () => {
  const ownerVar = lazyVar(42);
  const roCounter = asReadonly(ownerVar);

  let consumerId = -1;
  let ownerId = -1;
  const mod = buildModule(() => {
    const consumerVar = Var(0);
    when(roCounter.exactly(42), () => {
      consumerVar.set(roCounter);
      consumerVar.iadd(8);
    });
    consumerId = consumerVar.id;
    ownerId = roCounter.id;
  });

  const state = runSim(mod.toJson(), 1);
  assert.equal(state.var_values[ownerId], 42);
  assert.equal(state.var_values[consumerId], 50);
});

