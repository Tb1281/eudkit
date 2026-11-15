/**
 * Single-entry-point guard tests (user rule, 2026-11-04): `buildModule`
 * is a single entry point, one per map, like Rust `main`. A nested
 * `buildModule` call must throw `EUDSL-SCOPE`. Sequential builds (each
 * building its own map) are unaffected: the guard checks the ACTIVE
 * builder only, which is cleared when a `buildModule` call returns,
 * including when its body throws.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { Var, buildModule } from "../index.ts";

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

function decls(mod: { toJson(): string }): Array<[number, number]> {
  return JSON.parse(mod.toJson()).module.vars.map((v: any) => [v.id, v.init]);
}

test("single-entry: nested buildModule throws EUDSL-SCOPE", () => {
  assertCode(
    () =>
      buildModule(() => {
        const a = Var(1);
        a.iadd(1);
        buildModule(() => {
          const b = Var(2);
          b.iadd(1);
        });
      }),
    "EUDSL-SCOPE",
  );
});

test("single-entry: sequential builds succeed with independent outputs", () => {
  const mod1 = buildModule(() => {
    const a = Var(1);
    a.iadd(1);
  });
  const mod2 = buildModule(() => {
    const b = Var(2);
    b.iadd(1);
  });
  assert.deepEqual(decls(mod1), [[0, 1]]);
  assert.deepEqual(decls(mod2), [[0, 2]]);
});

test("single-entry: build after a throwing body succeeds (cleanup)", () => {
  assert.throws(
    () =>
      buildModule(() => {
        const a = Var(1);
        a.iadd(1);
        throw new Error("boom");
      }),
    /boom/,
  );
  const mod = buildModule(() => {
    const b = Var(5);
    b.iadd(1);
  });
  assert.deepEqual(decls(mod), [[0, 5]]);
});

test("single-entry: outer module completes after a caught nested throw", () => {
  let caught = false;
  const mod = buildModule(() => {
    const a = Var(1);
    a.iadd(1);
    try {
      buildModule(() => {
        const b = Var(2);
        b.iadd(1);
      });
    } catch (e: any) {
      assert.equal(e.code, "EUDSL-SCOPE");
      caught = true;
    }
    a.iadd(1);
  });
  assert.equal(caught, true);
  assert.deepEqual(decls(mod), [[0, 1]]);
});
