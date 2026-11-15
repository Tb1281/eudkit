/**
 * T-WP6: the 20T CUnit pointer reader.
 *
 * The reader folds the CUnit pool geometry into 18 bit tests of the dword that
 * holds a CUnit byte pointer: the shifted pointer space `S = A >>> 2` has
 * invariant bits 0, 1 and 20, so only `S` bits 2-19 (address bits 4-21) need a
 * test. The literal form is exactly 1 seeded reset + 18 tests + 1 bias = 20T;
 * the shared CP-relative body is declared once per module; and the loop form
 * never reads the ambient CurrentPlayer value.
 *
 * These tests pin the corrected constants (`CUNIT_LAST_EPD` is
 * `19025 + 84 * 1698 = 161657`, **not** `20723`), the exact HIR shape and
 * executed costs of all three forms, the null sentinel rejected by the
 * *upper* bound of `inCunitPool`, and the two `EUDSL-TYPE` guards.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  AtLeast,
  AtMost,
  EudslError,
  SetDeaths,
  SetTo,
  Var,
  buildModule,
  when,
  type HirCond,
} from "../index.ts";
import {
  CURRENT_PLAYER,
  CUNIT_BASE_EPD,
  CUNIT_LAST_EPD,
  CUNIT_POOL_BASE,
  CUNIT_POOL_END,
  CUNIT_SLOT_COUNT,
  CUNIT_SLOT_EPDS,
  CUNIT_SLOT_SIZE,
  EPD,
  inCunitPool,
  readCunitPointer,
  readCunitPointerSaved,
} from "../view.ts";
import { runSim } from "./helpers.ts";

const SET_DEATHS = 45;
const LIST_HEAD = EPD(0x628430);
const NULL_SENTINEL = (0x100002 + 0xffe9d727) >>> 0;

// HIR JSON is intentionally loosely typed here: the tests read the exact wire
// shape, which the typed builder interfaces deliberately do not expose.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = { [key: string]: any };

function moduleOf(fn: () => void): Json {
  return JSON.parse(buildModule(fn).toJson()).module as Json;
}

function triggersOf(module: Json): Json[] {
  return (module.stmts as Json[]).filter((s) => s.kind === "trigger");
}

function isEudslType(err: unknown): boolean {
  return err instanceof EudslError && err.code === "EUDSL-TYPE";
}

test("CUnit pool constants and derived EPD bound", () => {
  assert.equal(CUNIT_POOL_BASE, 0x59cca8);
  assert.equal(CUNIT_SLOT_SIZE, 336);
  assert.equal(CUNIT_SLOT_EPDS, CUNIT_SLOT_SIZE / 4);
  assert.equal(CUNIT_SLOT_COUNT, 1699);
  assert.equal(CUNIT_POOL_END, 0x628297);
  assert.equal(
    CUNIT_POOL_END,
    CUNIT_POOL_BASE + CUNIT_SLOT_SIZE * CUNIT_SLOT_COUNT - 1,
  );

  assert.equal(CUNIT_BASE_EPD, 19025);
  assert.equal(EPD(CUNIT_POOL_BASE), CUNIT_BASE_EPD);
  assert.equal(CUNIT_LAST_EPD, 161657);
  assert.equal(EPD(0x628148), CUNIT_LAST_EPD);
  assert.equal(
    CUNIT_LAST_EPD,
    CUNIT_BASE_EPD + CUNIT_SLOT_EPDS * (CUNIT_SLOT_COUNT - 1),
  );
  // The naive slot-count bound is the bug this constant must never regress
  // to: it rejects every real unit after the 21st slot.
  assert.notEqual(CUNIT_LAST_EPD, CUNIT_BASE_EPD + CUNIT_SLOT_COUNT - 1);
});

test("null sentinel is above the upper bound", () => {
  assert.equal(NULL_SENTINEL, 0xfff9d729);
  assert.ok(NULL_SENTINEL >= CUNIT_BASE_EPD);
  assert.ok(NULL_SENTINEL > CUNIT_LAST_EPD);
});

test("literal form is exactly 20 triggers and 18 conditions", () => {
  const triggers = triggersOf(moduleOf(() => void readCunitPointer(LIST_HEAD)));
  assert.equal(triggers.length, 20);
  assert.equal(
    triggers.reduce((n, t) => n + (t.conds as unknown[]).length, 0),
    18,
  );

  const seed = triggers[0];
  assert.deepEqual(seed.conds, []);
  assert.equal(seed.acts.length, 1);
  assert.equal(seed.acts[0].acttype, SET_DEATHS);
  assert.equal(seed.acts[0].value, 0x100002);
  assert.equal(seed.acts[0].modifier, SetTo);

  const bias = triggers[19];
  assert.deepEqual(bias.conds, []);
  assert.equal(bias.acts[0].value, 0xffe9d727);
  assert.equal(bias.acts[0].modifier, Add);
});

test("literal form tests address bits 4 to 21 in order", () => {
  const triggers = triggersOf(moduleOf(() => void readCunitPointer(LIST_HEAD)));
  const resultId = triggers[0].acts[0].relocs[0].target_var;

  for (let index = 0; index < 18; index++) {
    const sourceBit = index + 4;
    const trigger = triggers[index + 1];
    assert.equal(trigger.conds.length, 1);
    const cond = trigger.conds[0];
    assert.equal(cond.condtype, 15);
    assert.equal(cond.player, LIST_HEAD);
    assert.equal(cond.comparison, AtLeast);
    assert.equal(cond.amount, 1 << sourceBit);

    const act = trigger.acts[0];
    assert.equal(act.acttype, SET_DEATHS);
    assert.equal(act.modifier, Add);
    assert.equal(act.value, 1 << (sourceBit - 2));
    assert.equal(act.mask, 0xffffffff);
    assert.equal(act.relocs[0].target_var, resultId);
  }
});

test("literal form has no leading zero reset", () => {
  const triggers = triggersOf(moduleOf(() => void readCunitPointer(LIST_HEAD)));
  assert.equal(triggers[0].acts[0].value, 0x100002);
  assert.notEqual(triggers[0].acts[0].value, 0);
});

test("runtime form emits ambient read, store, call and restore", () => {
  const module = moduleOf(() => void readCunitPointer(Var(0)));
  assert.deepEqual(
    (module.stmts as Json[]).map((s) => s.kind),
    ["vartransfer", "call", "varstore", "call", "varstore"],
  );
  assert.equal(module.funcs.length, 2);
  assert.deepEqual(
    (module.funcs as Json[]).map((f) => f.body.length).sort((a, b) => a - b),
    [20, 33],
  );
});

test("runtime form declares the CUnit body once for two sites", () => {
  const module = moduleOf(() => {
    const pointer = Var(0);
    const out1 = Var(0);
    const out2 = Var(0);
    out1.set(readCunitPointer(pointer));
    out2.set(readCunitPointer(pointer));
  });

  assert.equal(module.funcs.length, 2);
  const cunitBody = (module.funcs as Json[]).find((f) => f.body.length === 20);
  assert.ok(cunitBody);
  const conds = (cunitBody.body as Json[]).flatMap((s) => s.conds ?? []);
  assert.equal(conds.length, 18);
  assert.ok(conds.every((c) => c.player === CURRENT_PLAYER));
  assert.equal(cunitBody.body[0].acts[0].value, 0x100002);
  assert.equal(cunitBody.body[19].acts[0].value, 0xffe9d727);

  const stmts = module.stmts as Json[];
  assert.equal(stmts.filter((s) => s.kind === "call").length, 4);
  assert.equal(stmts.filter((s) => s.kind === "varstore").length, 4);
});

test("saved form never reads the ambient CP", () => {
  const module = moduleOf(() => {
    const saved = Var(0);
    const out = Var(0);
    out.set(readCunitPointerSaved(saved, LIST_HEAD));
  });

  assert.equal(module.funcs.length, 1);
  assert.equal(module.funcs[0].body.length, 20);
  assert.deepEqual(
    (module.stmts as Json[]).map((s) => s.kind),
    ["varstore", "call", "varstore", "vartransfer"],
  );
  const stores = (module.stmts as Json[]).filter((s) => s.kind === "varstore");
  assert.equal(stores.length, 2);
  assert.ok(stores.every((s) => s.dst_addr === 0x6509b0));
  assert.equal(stores[1].src_var, 0); // the caller's saved Var
});

test("saved form accepts an Expr pointer", () => {
  const module = moduleOf(() => {
    const saved = Var(0);
    const pointer = Var(LIST_HEAD);
    readCunitPointerSaved(saved, pointer.add(1));
  });
  assert.equal(
    (module.stmts as Json[]).filter((s) => s.kind === "call").length,
    1,
  );
  assert.equal(module.funcs.length, 1);
});

test("savedCp must be a Var", () => {
  assert.throws(
    () => buildModule(() => readCunitPointerSaved(5 as never, LIST_HEAD)),
    isEudslType,
  );
});

test("bool target is rejected", () => {
  assert.throws(
    () => buildModule(() => readCunitPointer(true as never)),
    isEudslType,
  );
});

test("inCunitPool returns exactly two splicable conditions", () => {
  let gate: HirCond[] = [];
  const module = moduleOf(() => {
    const ptr = Var(0);
    gate = inCunitPool(ptr);
  });
  assert.equal(gate.length, 2);
  assert.deepEqual(
    gate.map((c) => c.amount),
    [CUNIT_BASE_EPD, CUNIT_LAST_EPD],
  );
  assert.deepEqual(
    gate.map((c) => c.comparison),
    [AtLeast, AtMost],
  );
  // A predicate, not a trigger: it never wraps the conditions.
  assert.deepEqual(module.stmts, []);
});

test("literal read executes exactly 20 triggers", () => {
  const mod = buildModule(() => void readCunitPointer(LIST_HEAD));
  assert.equal(runSim(mod.toJson(), 1).executed_cost.triggers, 20);
});

test("saved form executes 24 triggers per node", () => {
  const mod = buildModule(() => {
    const saved = Var(0);
    readCunitPointerSaved(saved, LIST_HEAD);
  });
  assert.equal(runSim(mod.toJson(), 1).executed_cost.triggers, 24);
});

function readSeeded(headEpd: number, pointer: number): number {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    SetDeaths(headEpd, SetTo, pointer, 0);
    const out = Var(0);
    ids.out = out.id;
    out.set(readCunitPointer(headEpd));
  });
  return runSim(mod.toJson()).var_values[ids.out];
}

test("simulator reads the pool base and last slot EPDs", () => {
  assert.equal(readSeeded(LIST_HEAD, CUNIT_POOL_BASE), 19025);
  assert.equal(readSeeded(LIST_HEAD, 0x628148), 161657);
});

test("simulator reads the null pointer as the sentinel", () => {
  assert.equal(readSeeded(LIST_HEAD, 0), 0xfff9d729);
});

test("inCunitPool rejects the null sentinel via the upper bound", () => {
  const ids = { out: -1, hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(LIST_HEAD, SetTo, 0, 0);
    const out = Var(0);
    ids.out = out.id;
    out.set(readCunitPointer(LIST_HEAD));
    const hit = Var(0);
    ids.hit = hit.id;
    when([...inCunitPool(out), hit.eq(0)], () => hit.iadd(1));
  });
  const values = runSim(mod.toJson()).var_values;
  assert.equal(values[ids.out], 0xfff9d729);
  assert.equal(values[ids.hit], 0);
});
