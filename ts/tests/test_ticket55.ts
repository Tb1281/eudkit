/**
 * Ticket #55: in-place doubling in the dynamic-divisor division ladder.
 *
 * The long-division ladder in `evalExprInto` used to materialise `rem.shl(1)`
 * into a fresh `nextRem` temporary and copy it back with `rem.set(nextRem)` on
 * each of its 32 unrolled iterations. Doubling through `rem.iadd(rem)` — a
 * self-transfer (`dst_var === src_var`, modifier Add), the same lowering shape
 * as the everyday `v.set(v.add(v))` idiom — deletes the temporary and the
 * copy-back, and writes the doubled value straight through `rem.value`.
 *
 * Two pins live here:
 *
 * - a division **result** pin over an 18-case dynamic-divisor matrix. It is
 *   green on both the pre-#55 tree and the patched tree *by design*: identical
 *   `var_values` is the whole point, because the refactor must not change
 *   semantics. It is a forward guard, not a change detector — its teeth are
 *   demonstrated by mutating the ladder (drop the doubling, or flip the
 *   `SetDeathsX` modifier), which makes it fail;
 * - a static + executed **cost** pin on the one-site division probe, which is
 *   red on the pre-#55 tree (1315 T / 1120 C / 1888 A / 3,166,656 B static and
 *   1443 / 1120 / 9044 executed) and green after (#55: 354 / 128 / 834 /
 *   852,568 B static and 513 / 128 / 10,952 executed; **#102** reshaped the
 *   ladder again — one hoisted `diff` plus a gated `when`, a fresh quotient,
 *   and one copy-back — so the pin moved to 291 / 96 / 771 / 700,740 B static
 *   and 335 / 68 / 6,865 executed). **Cycle 4** then hoisted `diff.set(den)`
 *   above the numerator-bit trigger so transfer coalescing folds the
 *   independent transfers into one page firing per bit, moving the pin to
 *   259 / 96 / 741 / 623,684 B static and 271 / 68 / 4,787 executed). See
 *   `test_ticket102.ts` for the shared kernel and the divide-by-zero /
 *   aliasing / `mod` pins.
 *
 * Honest caveat, pinned deliberately: #55 made executed *actions* rise,
 * 9,044 -> 10,952 (+21%), even while executed triggers fell 64% and executed
 * conditions fell 89%; total executed ops were essentially flat (11,607 ->
 * 11,593). #102 then cut both static and executed actions (771 static,
 * 6,865 executed), because the gated body does the subtract and the quotient
 * bit in one pass instead of zeroing a second temporary every bit. Do not
 * "fix" either number without re-reading #55 and #102.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { Var, buildModule } from "../index.ts";

// Dynamic-divisor matrix: every divisor is non-zero, and the halves cover small
// values, exact multiples, powers of two, and the uint32 boundaries.
const CASES: [number, number][] = [
  [1000, 7],
  [0, 7],
  [1, 7],
  [7, 7],
  [6, 7],
  [13, 3],
  [0xffffffff, 7],
  [0xffffffff, 0xffff],
  [123456789, 1000000],
  [0x80000000, 3],
  [65535, 0x10000],
  [0x7fffffff, 0x7fffffff],
  [1, 1],
  [0xffffffff, 0xffffffff],
  [999999999, 1],
  [5, 0xffffffff],
  [0xf0000000, 0x10],
  [0x12345678, 0x9abcdef1],
];

// One dynamic-division site over three Vars: `q.set(a.div(b))`.
const PROBE_STATIC_COST = {
  triggers: 259,
  conditions: 96,
  actions: 741,
  trigger_bytes: 623672,
  blob_bytes: 12,
  total_bytes: 623684,
};
// #102: the gated body subtracts and sets the quotient bit in one pass, so the
// static and executed action counts both fall relative to the #55 ladder.
// Cycle 4: `diff.set(den)` is hoisted above the numerator-bit trigger, so the
// independent transfers coalesce into one page firing per bit.
const PROBE_EXECUTED_COST = { triggers: 271, conditions: 68, actions: 4787 };

test("Ticket 55: dynamic-divisor division matches native floor division (18 cases)", () => {
  for (const [a, b] of CASES) {
    const captured: { quot?: number; rem?: number } = {};
    const mod = buildModule(() => {
      const va = Var(a);
      const vb = Var(b);
      const quot = Var(0);
      const rem = Var(0);
      quot.set(va.div(vb));
      rem.set(va.mod(vb));
      captured.quot = quot.id;
      captured.rem = rem.id;
    });
    const report = runSim(mod.toJson(), 1);
    assert.equal(
      report.var_values[captured.quot!],
      Math.floor(a / b) >>> 0,
      `quotient for ${a} // ${b}`,
    );
    assert.equal(
      report.var_values[captured.rem!],
      (a % b) >>> 0,
      `remainder for ${a} % ${b}`,
    );
  }
});

test("Ticket 55: one-site division probe static + executed cost", () => {
  const mod = buildModule(() => {
    const a = Var(1000);
    const b = Var(7);
    const q = Var(0);
    q.set(a.div(b));
  });
  const report = runSim(mod.toJson(), 1);
  // Static map size: #102's single-diff gated ladder replaces the per-bit
  // diff + subAmt pair with one hoisted diff and one gated body.
  assert.deepEqual(report.static_cost, PROBE_STATIC_COST);
  // Executed cost: triggers, conditions and actions all fall; the body
  // subtracts and sets the quotient bit inside one gate.
  assert.deepEqual(
    {
      triggers: report.executed_cost.triggers,
      conditions: report.executed_cost.conditions,
      actions: report.executed_cost.actions,
    },
    PROBE_EXECUTED_COST,
  );
  assert.equal(report.executed_cost.visited_items.length, 271);
});
