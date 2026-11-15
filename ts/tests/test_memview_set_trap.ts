/**
 * Ticket #28: `MemViewInstance` supports field assignment like Python.
 *
 * Python's `MemViewInstance.__setattr__` writes through to `field.set(value)`,
 * so `u.hp = 1500` emits the write act. The TS proxy only had a `get` trap, so
 * a field assignment fell through to the default `[[Set]]` and created an own
 * data property that shadowed the handle — the write was dropped and the next
 * `u.hp.eq(...)` threw `TypeError`. This pins the `set` trap that restores the
 * py parity without changing `schema`/`baseEpd` semantics.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  EudslError,
  MemView,
  Var,
  buildModule,
  defineMemView,
  when,
} from "../index.ts";

// hp: dword at byte 0 (EPD base + 0), mp: word at byte 4 (EPD base + 1).
const SetTrapUnit = defineMemView("SetTrapUnit", 8, {
  hp: { offset: 0, size: 4 },
  mp: { offset: 4, size: 2 },
});

const BASE_EPD = 3000;

function hasOwn(target: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(target, key);
}

test("write-through assignment emits the set act and keeps the handle usable", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const u = MemView(SetTrapUnit, BASE_EPD);
    u.hp = 5;
    u.mp = 50;
    // The same handles must stay usable: a dropped write would shadow them
    // with numbers and these calls would throw TypeError.
    const hit = Var(0);
    ids.hit = hit.id;
    when([u.hp.eq(5)], () => hit.iadd(1));
    when([u.mp.ge(10)], () => hit.iadd(1));
  });

  const report = runSim(mod.toJson());
  assert.equal(report.external_epds[`${BASE_EPD}`], 5);
  assert.equal(report.external_epds[`${BASE_EPD + 1}`], 50);
  assert.equal(report.var_values[ids.hit], 2);
});

test("unknown field assignment stays a loud EUDSL-BOUNDS", () => {
  buildModule(() => {
    const u = MemView(SetTrapUnit, BASE_EPD);
    assert.throws(
      () => {
        (u as any).shield = 1;
      },
      (err: unknown) =>
        err instanceof EudslError &&
        (err as EudslError).code === "EUDSL-BOUNDS",
    );
  });
});

test("schema and baseEpd are untouched by field assignment", () => {
  const outside = MemView(SetTrapUnit, BASE_EPD);
  assert.equal(outside.schema, SetTrapUnit);
  assert.equal(outside.baseEpd, BASE_EPD);
  assert.equal(hasOwn(outside, "hp"), false);

  buildModule(() => {
    const u = MemView(SetTrapUnit, BASE_EPD);
    assert.equal(hasOwn(u, "hp"), false);
    assert.equal(typeof u.hp.eq, "function");

    u.hp = 5;

    assert.equal(hasOwn(u, "hp"), false);
    assert.equal(u.schema, SetTrapUnit);
    assert.equal(u.baseEpd, BASE_EPD);
    assert.equal(typeof u.hp.eq, "function");
  });

  assert.equal(hasOwn(outside, "hp"), false);
});
