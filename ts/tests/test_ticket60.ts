/**
 * Ticket #60: root act/transfer interleave must not split the transfer batch.
 *
 * `crates/eudkit-opt/src/lib.rs` kept one *pending transfer run* at the module
 * root, but every root action statement flushed it: an `act, act, x.set(y),
 * w.set(z)` program formed one batch, while the same four items written
 * `act, x.set(y), act, w.set(z)` formed two. Two batches mean two page
 * setups (one extra static trigger) and two page firings per pass (two extra
 * executed triggers) for identical work.
 *
 * These pins are red before the fix and green after. Both probe programs are
 * the issue's own reproduction:
 *
 * - grouped root: 3T/0C/71A/7,232 B static, 3T/0C/71A executed;
 * - interleaved root: same as grouped (was 4T/0C/72A/9,640 B static and
 *   5T/0C/136A executed — +1 static trigger and +2 executed triggers);
 * - the same run of 8 items (4 actions, 4 transfers) stays equal at both
 *   shapes, so interleaving adds nothing in a longer run either.
 *
 * The guard is load-bearing: a root action carrying `relocs[].target_var`
 * may read or write a paged variable slot, so it keeps source order and still
 * ends the run (pinned at the pre-fix 4T/72A/9,640 B and 5T/136A). A
 * conditional root trigger is not the `can_unpack_into_basic_block` shape,
 * so it also still ends the run.
 *
 * Body interleave is out of scope here (it is a separate follow-up needing the
 * same act↔transfer dependency analysis); these probes stay at the module root.
 * The same probes live in `py/tests/test_ticket60.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, type SimReport } from "./helpers.ts";
import { SetDeaths, SetTo, Var, buildModule } from "../index.ts";

const STATIC_KEYS = ["triggers", "conditions", "actions", "total_bytes"] as const;
const EXEC_KEYS = ["triggers", "conditions", "actions"] as const;

interface Probe {
  hir: string;
  dstIds: number[];
  srcIds: number[];
}

/**
 * N constant actions and N independent copies at the module root. `grouped`
 * writes all actions first, then all copies; `interleaved` alternates one
 * action and one copy. Both describe the same work.
 */
function rootProbe(n: number, interleaved: boolean): Probe {
  const dstIds: number[] = [];
  const srcIds: number[] = [];
  const hir = buildModule(() => {
    const srcs = Array.from({ length: n }, (_, i) => Var(10 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    if (interleaved) {
      for (let i = 0; i < n; i += 1) {
        SetDeaths(0, SetTo, i + 1, 0);
        dsts[i].set(srcs[i]);
      }
    } else {
      for (let i = 0; i < n; i += 1) {
        SetDeaths(0, SetTo, i + 1, 0);
      }
      for (let i = 0; i < n; i += 1) {
        dsts[i].set(srcs[i]);
      }
    }
    dstIds.push(...dsts.map((dst) => dst.id));
    srcIds.push(...srcs.map((src) => src.id));
  }).toJson();
  return { hir, dstIds, srcIds };
}

function cost(hir: string): [number[], number[]] {
  const staticReport = runStatic(hir);
  const sim = runSim(hir, 1);
  return [
    STATIC_KEYS.map((key) => staticReport.static_cost[key]),
    EXEC_KEYS.map((key) => sim.executed_cost[key]),
  ];
}

/**
 * Make the second interleaved root action name a variable. The DSL has no
 * surface that emits an act↔var dependency between two independent copies at
 * the root, so this is exercised at the HIR-JSON seam: add the
 * `relocs[].target_var` entry lowering would produce for a page-slot operand.
 */
function withDependentSecondAction(hir: string, targetVar: number): string {
  const doc = JSON.parse(hir);
  for (const stmt of doc.module.stmts) {
    if (stmt.kind !== "trigger" || !stmt.acts?.length) continue;
    const act = stmt.acts[0];
    if (act.value === 2) {
      act.relocs = [
        {
          field_offset: 20,
          target_var: targetVar,
          target_offset: 20,
          mode: 1,
        },
      ];
      return JSON.stringify(doc);
    }
  }
  throw new Error("second root action trigger not found");
}

test("ticket 60: grouped root probe cost", () => {
  assert.deepEqual(cost(rootProbe(2, false).hir), [
    [3, 0, 71, 7_232],
    [3, 0, 71],
  ]);
});

test("ticket 60: interleaved root probe matches the grouped cost", () => {
  const grouped = cost(rootProbe(2, false).hir);
  const interleaved = cost(rootProbe(2, true).hir);
  assert.deepEqual(interleaved, grouped);
  assert.deepEqual(interleaved, [
    [3, 0, 71, 7_232],
    [3, 0, 71],
  ]);
});

test("ticket 60: interleaved root probe writes every destination", () => {
  const probe = rootProbe(2, true);
  const report: SimReport = runSim(probe.hir, 1);
  assert.deepEqual(
    probe.dstIds.map((varId) => report.var_values[varId]),
    [10, 11],
  );
});

test("ticket 60: longer interleaved root run stays equal to grouped", () => {
  const grouped = cost(rootProbe(4, false).hir);
  const probe = rootProbe(4, true);
  assert.deepEqual(cost(probe.hir), grouped);
  assert.deepEqual(grouped, [
    [3, 0, 77, 7_240],
    [3, 0, 77],
  ]);
  const report: SimReport = runSim(probe.hir, 1);
  assert.deepEqual(
    probe.dstIds.map((varId) => report.var_values[varId]),
    [10, 11, 12, 13],
  );
});

test("ticket 60: a dependent root action still ends the transfer run", () => {
  const probe = rootProbe(2, true);
  const dependent = withDependentSecondAction(probe.hir, probe.srcIds[0]);
  // Kept in source order: two one-transfer batches with the action between
  // them, i.e. exactly the pre-fix interleaved cost.
  assert.deepEqual(cost(dependent), [
    [4, 0, 72, 9_640],
    [5, 0, 136],
  ]);
  const report: SimReport = runSim(dependent, 1);
  assert.deepEqual(
    probe.dstIds.map((varId) => report.var_values[varId]),
    [10, 11],
  );
});
