/**
 * FE0: one canonical `describeValue` per frontend.
 *
 * Mirror of `py/tests/test_value_helpers.py`. `ts/view.ts` owns the only
 * implementation; `blockio` and `datadumper` alias it as `describe`, while
 * `string` keeps a thin `describeArg` adapter for its pre-dedupe `null`
 * rendering. The tests pin the rendered surface for `bool`, non-integral
 * float, `NaN`, `Infinity`, string, `null`, `undefined` and a Var-like handle,
 * then prove each narrowed guard renders its operand exactly as before FE0.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  EudslError,
  FixedString,
  dataDumper,
  describeValue,
  lazyVar,
  memsetEPD,
  requirePlainInt,
} from "../index.ts";

function messageFrom(call: () => void): string {
  try {
    call();
  } catch (error) {
    assert.ok(error instanceof EudslError);
    return error.message;
  }
  assert.fail("expected EudslError");
}

test("describeValue renders bool by typeof", () => {
  assert.equal(describeValue(true), "a boolean");
  assert.equal(describeValue(false), "a boolean");
});

test("describeValue renders numbers bare", () => {
  assert.equal(describeValue(0), "0");
  assert.equal(describeValue(-1), "-1");
  assert.equal(describeValue(1.5), "1.5");
  assert.equal(describeValue(NaN), "NaN");
  assert.equal(describeValue(Infinity), "Infinity");
  assert.equal(describeValue(-Infinity), "-Infinity");
});

test("describeValue renders strings via JSON", () => {
  assert.equal(describeValue("abc"), '"abc"');
});

test("describeValue renders null and undefined by typeof", () => {
  assert.equal(describeValue(null), "a object");
  assert.equal(describeValue(undefined), "a undefined");
});

test("describeValue renders a Var-like handle by typeof", () => {
  assert.equal(describeValue(lazyVar(0)), "a object");
});

test("requirePlainInt rejects non-integers", () => {
  for (const value of [true, 1.5, NaN, Infinity, "1", null, undefined, lazyVar(0)]) {
    assert.throws(
      () => requirePlainInt(value, "operand"),
      (error: unknown) =>
        error instanceof EudslError &&
        error.code === "EUDSL-TYPE" &&
        error.message.includes(`got ${describeValue(value)}`),
    );
  }
});

test("blockio guard renders operands like describeValue", () => {
  for (const value of [true, "abc", null, undefined]) {
    const message = messageFrom(() => memsetEPD(0, value as unknown as number, 1));
    assert.ok(
      message.includes(`got ${describeValue(value)}`),
      `${message} should render ${describeValue(value)}`,
    );
  }
});

test("datadumper payload guard renders operands like describeValue", () => {
  for (const value of [true, 1.5, NaN, Infinity, "abc", null, undefined]) {
    const message = messageFrom(() =>
      dataDumper(value as unknown as Uint8Array, []),
    );
    assert.ok(
      message.includes(`got ${describeValue(value)}`),
      `${message} should render ${describeValue(value)}`,
    );
  }
});

test("string size guard keeps the pre-dedupe null rendering", () => {
  for (const value of [true, 1.5, NaN, Infinity, "abc", null, undefined]) {
    const rendered = value === null ? "null" : describeValue(value);
    const message = messageFrom(() => new FixedString(value as unknown as number));
    assert.ok(
      message.includes(`got ${rendered}`),
      `${message} should render ${rendered}`,
    );
  }
});

test("exact pre-dedupe messages are preserved", () => {
  assert.equal(
    messageFrom(() => new FixedString("abc" as unknown as number)),
    'EUDSL-BOUNDS: FixedString(): size must be a positive integer (bytes), got "abc"',
  );
  assert.equal(
    messageFrom(() => new FixedString(null as unknown as number)),
    "EUDSL-BOUNDS: FixedString(): size must be a positive integer (bytes), got null",
  );
  assert.equal(
    messageFrom(() => memsetEPD(0, true as unknown as number, 1)),
    "EUDSL-TYPE: memsetEPD(): value must be an integer or a game-time value, got a boolean",
  );
  assert.equal(
    messageFrom(() => dataDumper(null as unknown as Uint8Array, [])),
    "EUDSL-TYPE: dataDumper(): inputData must be bytes, got a object",
  );
});
