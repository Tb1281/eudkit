import assert from "node:assert/strict";
import test from "node:test";

import {
  EPD_CURRENT_PLAYER,
  EudslError,
  buildModule,
  withCP,
} from "../index.ts";

/** Non-integral CP bases: NaN, the infinities, and a fractional EPD. */
const NON_INTEGRAL_BASES: readonly number[] = [
  NaN,
  Infinity,
  -Infinity,
  5000.5,
];

test("withCP rejects every non-integral CP base with EUDSL-TYPE", () => {
  for (const base of NON_INTEGRAL_BASES) {
    assert.throws(
      () => withCP(base, () => {}),
      (err: unknown) =>
        err instanceof EudslError && err.code === "EUDSL-TYPE",
      `withCP(${String(base)}) must throw EUDSL-TYPE`,
    );
  }
});

test("withCP rejects before queueing any act", () => {
  // Each module's only content is the rejected call, so a module that still
  // emits anything proves the seed act was queued before validation.
  for (const base of NON_INTEGRAL_BASES) {
    const mod = buildModule(() => {
      try {
        withCP(base, () => {});
      } catch {
        // expected: the base is rejected before any act is queued
      }
    });
    const stmts = (
      JSON.parse(mod.toJson()) as { module: { stmts: unknown[] } }
    ).module.stmts;
    assert.equal(
      stmts.length,
      0,
      `withCP(${String(base)}) must queue 0 statements, got ${stmts.length}`,
    );
  }
});

test("withCP message names the bad value", () => {
  assert.throws(
    () => withCP(NaN, () => {}),
    (err: unknown) =>
      err instanceof EudslError && err.message.includes("NaN"),
    "withCP(NaN) message must contain NaN",
  );
});

test("withCP still accepts integral CP bases, including the 0 sentinel", () => {
  // 0 is the "follow the live CP" sentinel and negatives are legal EPDs;
  // the new predicate must not narrow today's accepted set.
  for (const base of [0, 5000, -1, 0xffffffff]) {
    const mod = buildModule(() => {
      withCP(base, () => {});
    });
    const stmts = (
      JSON.parse(mod.toJson()) as {
        module: { stmts: { acts: Record<string, number>[] }[] };
      }
    ).module.stmts;
    assert.equal(stmts.length, 1, `withCP(${base}) must emit one trigger`);
    assert.deepEqual(
      stmts[0].acts.map((act) => ({
        dest: act.dest,
        value: act.value,
        acttype: act.acttype,
        modifier: act.modifier,
      })),
      [
        {
          dest: EPD_CURRENT_PLAYER,
          // HIR stores act values as u32, so -1 serialises as 0xffffffff.
          value: base >>> 0,
          acttype: 45,
          modifier: 7,
        },
      ],
    );
  }
});
