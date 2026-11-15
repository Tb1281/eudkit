/**
 * Var-scope tests: `Var` allocation outside `buildModule` must fail loud.
 *
 * A `Var` created outside `buildModule` used to receive a silent fallback
 * id (`fallbackVarCounter++`), colliding with the first in-module var and
 * silently corrupting output (the aligned-string-padding smoke program
 * showed `AB` instead of `ABCDEFGH`). The builder owns the id space by
 * construction, so outside allocation has no id space: it throws
 * `EUDSL-SCOPE`, exactly like `allocString` already does.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { FixedString, Var, buildModule, when } from "../index.ts";
import { allocVarSlot, getNextPageSlot, getNextVarSlot } from "../var.ts";
import { EudslError } from "../view.ts";

function assertScopeError(fn: () => unknown): void {
  assert.throws(fn, (e: unknown) => {
    assert.ok(e instanceof EudslError, `expected EudslError, got ${e}`);
    assert.equal(e.code, "EUDSL-SCOPE");
    return true;
  });
}

test("var scope: Var() outside buildModule throws EUDSL-SCOPE", () => {
  assertScopeError(() => Var(1));
});

test("var scope: var-slot allocation outside buildModule throws EUDSL-SCOPE", () => {
  assertScopeError(() => allocVarSlot(0));
  assertScopeError(() => getNextVarSlot());
  assertScopeError(() => getNextPageSlot());
});

test("var scope: aligned-string-padding shape with var outside buildModule fails at build time", () => {
  assertScopeError(() => {
    // Throws here, before buildModule runs: no module (and no map) is produced.
    const yes = Var(1);
    buildModule(() => {
      const control = new FixedString(16);
      control.printAt(1, "ABCDEFGH");
      const padded = new FixedString(32);
      padded.write("AB");
      when(yes.eq(1), () => {
        padded.append("CD");
      });
      padded.append("EFGH");
      padded.displayAt(2);
    });
  });
});
// Padded buffer dwords, little-endian: "AB\x1a\x1a" | "CD\x1a\x1a" | "EFGH" | terminator.
const PADDED_BUFFER = [437928513, 437929027, 1212630597, 0];

function findPaddedBuffer(epds: Record<string, number>): number | null {
  for (const key of Object.keys(epds)) {
    const base = Number(key);
    if (
      PADDED_BUFFER.every((want, i) => epds[String(base + i)] === want)
    ) {
      return base;
    }
  }
  return null;
}

test("var scope: aligned-string-padding shape with var inside yields padded buffer at loop-2 steady state", () => {
  const mod = buildModule(() => {
    const yes = Var(1);
    const control = new FixedString(16);
    control.printAt(1, "ABCDEFGH");
    const padded = new FixedString(32);
    padded.write("AB");
    when(yes.eq(1), () => {
      padded.append("CD");
    });
    padded.append("EFGH");
    padded.displayAt(2);
  });
  // Loop 2+ steady state, not loop 1 only: the outside-var breakage
  // (terminator poisoning) is invisible at loop 1.
  const report = runSim(mod.toJson(), 2);
  assert.notEqual(
    findPaddedBuffer(report.external_epds),
    null,
    "padded buffer not found in sim external_epds at loop 2",
  );
});
