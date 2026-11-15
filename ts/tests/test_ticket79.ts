/**
 * Ticket #79: guard the leaf-act sibling `when` merge against RAW hazards.
 *
 * `crates/eudkit-opt/src/temporal.rs` merges adjacent sibling `when` blocks
 * with identical condition lists when both bodies are leaf acts. The transfer
 * merge has always been dependency-guarded, but the leaf-act merge was not: the
 * merged group evaluates the shared gate once, so a first body that writes a
 * variable the gate reads lets the second body run against a stale gate.
 *
 * These pins are red before the fix and green after:
 *
 * - RAW probe (`when(v==1){v.set(2)}` then `when(v==1){w.set(9)}`, `v=1`):
 *   the second body must not run, so `w` stays `0` (a naive merge gives
 *   `w=9`). Static cost 2T/2C/2A/4,824 B because the siblings stay apart;
 * - merge-positive control (same gate, bodies writing other variables):
 *   1T/1C/2A/2,420 B static and both writes land.
 *
 * The same probes live in `py/tests/test_ticket79.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, type SimReport } from "./helpers.ts";
import { Var, buildModule, when } from "../index.ts";

/** First body falsifies the shared gate; the second must not ride along. */
function hazard(): string {
  return buildModule(() => {
    const v = Var(1);
    const w = Var(0);
    when([v.eq(1)], () => {
      v.set(2);
    });
    when([v.eq(1)], () => {
      w.set(9);
    });
  }).toJson();
}

/** Identical gate, bodies write variables the gate does not read. */
function positive(): string {
  return buildModule(() => {
    const g = Var(1);
    const w = Var(0);
    const u = Var(0);
    when([g.eq(1)], () => {
      w.set(9);
    });
    when([g.eq(1)], () => {
      u.set(7);
    });
  }).toJson();
}

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

test("Ticket 79: RAW probe stays unmerged and keeps w zero", () => {
  // Vars: [v, w], v init 1. Loop 1: v==1, so v.set(2) runs; with the guard
  // the second gate re-reads v and w.set(9) never runs.
  const report: SimReport = runSim(hazard(), 2);
  assert.deepEqual(report.var_values, [2, 0]);
  assert.deepEqual(staticCost(hazard()), [2, 2, 2, 4_824]);
});

test("Ticket 79: merge-positive case still merges", () => {
  // Neither body touches the gate variable, so the siblings share one gate
  // evaluation: one trigger / one condition / both acts.
  const report: SimReport = runSim(positive(), 1);
  assert.deepEqual(report.var_values, [1, 9, 7]);
  assert.deepEqual(staticCost(positive()), [1, 1, 2, 2_420]);
});
