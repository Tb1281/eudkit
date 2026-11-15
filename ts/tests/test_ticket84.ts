import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  CUnit,
  SetDeathsX,
  SetTo,
  Subtract,
  Trigger,
  Var,
  buildModule,
} from "../index.ts";
import { runSim } from "./helpers.ts";

test("Var imperative in-place methods return void / undefined", () => {
  buildModule(() => {
    const v = Var(10);
    const rSet = v.set(5);
    assert.equal(rSet, undefined);

    const rAdd = v.iadd(3);
    assert.equal(rAdd, undefined);

    const rSub = v.isub(2);
    assert.equal(rSub, undefined);

    const rMul = v.imul(2);
    assert.equal(rMul, undefined);

    const rDiv = v.idiv(2);
    assert.equal(rDiv, undefined);

    const rMod = v.imod(5);
    assert.equal(rMod, undefined);
  });
});

test("MemFieldHandle imperative in-place methods return void / undefined", () => {
  buildModule(() => {
    const u = CUnit(1000);
    const rSet = u.hp.set(500);
    assert.equal(rSet, undefined);

    const rAdd = u.hp.iadd(50);
    assert.equal(rAdd, undefined);

    const rSub = u.hp.isub(20);
    assert.equal(rSub, undefined);
  });
});

test("Var.copy() creates an independent Var that copies value at runtime", () => {
  const mod = buildModule(() => {
    const v1 = Var(42);
    const v2 = v1.copy();
    assert.notEqual(v1.id, v2.id);

    v2.iadd(10);
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[0], 42);
  assert.equal(report.var_values[1], 52);
});

test("Var declarative action builders act_set, act_add, act_sub", () => {
  buildModule(() => {
    const v = Var(100);

    const aSet = v.act_set(25);
    assert.equal(aSet.acttype, 45);
    assert.equal(aSet.modifier, SetTo);
    assert.equal(aSet.value, 25);
    assert.equal(aSet.mask, 0xffffffff);

    const aAdd = v.act_add(7);
    assert.equal(aAdd.acttype, 45);
    assert.equal(aAdd.modifier, Add);
    assert.equal(aAdd.value, 7);

    const aSub = v.act_sub(3);
    assert.equal(aSub.acttype, 45);
    assert.equal(aSub.modifier, Add);
    assert.equal(aSub.value, (-3) >>> 0);

    // No TS-only camelCase action-constructor surface (ADR 0002).
    assert.equal("actSet" in v, false);
    assert.equal("actAdd" in v, false);
    assert.equal("actSub" in v, false);

    // Can be passed in Trigger acts array
    new Trigger({
      acts: [v.act_set(1), v.act_add(2), v.act_sub(1)],
    });
  });
});

test("MemFieldHandle declarative action builders act_set, act_add, act_sub", () => {
  buildModule(() => {
    const u = CUnit(1000);

    const aSet = u.hp.act_set(500);
    assert.equal(aSet.acttype, 45);
    assert.equal(aSet.modifier, SetTo);
    assert.equal(aSet.value, 500);

    const aAdd = u.hp.act_add(50);
    assert.equal(aAdd.acttype, 45);
    assert.equal(aAdd.modifier, Add);
    assert.equal(aAdd.value, 50);

    const aSub = u.hp.act_sub(20);
    assert.equal(aSub.acttype, 45);
    assert.equal(aSub.modifier, Subtract);
    assert.equal(aSub.value, 20);

    // No TS-only camelCase action-constructor surface (ADR 0002).
    assert.equal("actSet" in u.hp, false);
    assert.equal("actAdd" in u.hp, false);
    assert.equal("actSub" in u.hp, false);

    new Trigger({
      acts: [u.hp.act_set(100), u.hp.act_add(10), u.hp.act_sub(5)],
    });
  });
});
