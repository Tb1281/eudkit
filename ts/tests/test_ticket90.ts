/**
 * Ticket #90: an external-state gate must not merge over an act that writes it.
 *
 * `crates/eudkit-opt/src/temporal.rs` step 3 merges adjacent sibling `when`
 * blocks with identical condition lists when both bodies are leaf acts. The
 * merge guard only checks whether a body act carries a `relocs[].target_var`
 * in the gate's read set, and `cond_read_vars` only collects reloc targets.
 * A `Memory` / `Deaths` / `Switch` gate has no relocs — it names its location
 * with an absolute number — so the guard passes vacuously and the first
 * body's write to that cell is treated as harmless. The merged group then
 * evaluates the shared gate once, before the first body runs, and the second
 * body rides along on a stale gate.
 *
 * These hazard pins are red before the fix and green after; each builds one
 * init trigger (so the gate holds on loop 1) plus the pair, and asserts the
 * correct (unmerged) static cost and that the second body never runs:
 *
 * - `Memory(FLAG, AtLeast, 1)` + `SetMemory(FLAG, SetTo, 0)` / `d.set(7)`;
 * - `Deaths(EPD(FLAG), AtLeast, 1)` + `SetDeaths(...)` / `d.set(7)`;
 * - `Switch(1, SwitchSet)` + `SetSwitch(1, SwitchActionClear)` / `d.set(7)`.
 *
 * The positive pins must stay merged: paged-var gates (plain and with an init
 * trigger), an external gate whose leaf bodies write only paged vars (the
 * `bodyWritesOnlyPagedVars` path), an external gate over a transfer pair, and
 * a masked `DeathsX` read — the last catches a `locid`/`eudx` over-tightening
 * of `condKeyedByPagedVar`.
 *
 * The same probes live in `py/tests/test_ticket90.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic, type SimReport } from "./helpers.ts";
import {
  AtLeast,
  Deaths,
  DeathsX,
  EPD,
  Memory,
  SetDeaths,
  SetMemory,
  SetSwitch,
  SetTo,
  Switch,
  SwitchActionClear,
  SwitchActionSet,
  SwitchSet,
  Trigger,
  Var,
  buildModule,
  when,
} from "../index.ts";

const FLAG = 0x57f0b4;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

/** First body clears the memory cell the shared gate reads. */
function memoryHazard(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    const d = Var(0);
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetMemory(FLAG, SetTo, 0);
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      d.set(7);
    });
  }).toJson();
}

/** Same shape, but the gate and the write go through the Deaths wire form. */
function deathsHazard(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    const d = Var(0);
    when([Deaths(EPD(FLAG), AtLeast, 1)], () => {
      SetDeaths(EPD(FLAG), SetTo, 0);
    });
    when([Deaths(EPD(FLAG), AtLeast, 1)], () => {
      d.set(7);
    });
  }).toJson();
}

/** Same shape for the Switch condition, which names an absolute switch id. */
function switchHazard(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetSwitch(1, SwitchActionSet)] });
    const d = Var(0);
    when([Switch(1, SwitchSet)], () => {
      SetSwitch(1, SwitchActionClear);
    });
    when([Switch(1, SwitchSet)], () => {
      d.set(7);
    });
  }).toJson();
}

/** Identical paged-var gates; both leaf bodies write other variables. */
function pagedPair(): string {
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

/** The paged-var pair plus an unrelated static init trigger. */
function pagedPairWithInit(): string {
  return buildModule(() => {
    const g = Var(1);
    const w = Var(0);
    const u = Var(0);
    new Trigger({ conds: [], acts: [SetDeaths(0, SetTo, 1)] });
    when([g.eq(1)], () => {
      w.set(9);
    });
    when([g.eq(1)], () => {
      u.set(7);
    });
  }).toJson();
}

/** External gate, but both leaf bodies write only paged variables. */
function externalGatePagedBodies(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    const w = Var(0);
    const u = Var(0);
    when([Memory(FLAG, AtLeast, 1)], () => {
      w.set(9);
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      u.set(7);
    });
  }).toJson();
}

/** External `Memory` + `Switch` gate over two distinct transfer runs. */
function externalGateTransferPair(): string {
  return buildModule(() => {
    const src0 = Var(3);
    const src1 = Var(5);
    const dst0 = Var(0);
    const dst1 = Var(0);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    new Trigger({ conds: [], acts: [SetSwitch(1, SwitchActionSet)] });
    when([Memory(FLAG, AtLeast, 1), Switch(1, SwitchSet)], () => {
      dst0.set(src0);
    });
    when([Memory(FLAG, AtLeast, 1), Switch(1, SwitchSet)], () => {
      dst1.set(src1);
    });
  }).toJson();
}

/** Masked `DeathsX` read carries eudx/locid but is still a paged-var gate. */
function maskedPair(): string {
  return buildModule(() => {
    const b = Var(3);
    const w = Var(0);
    const u = Var(0);
    when([DeathsX(b.value, AtLeast, 1, 0, 0xff)], () => {
      w.set(9);
    });
    when([DeathsX(b.value, AtLeast, 1, 0, 0xff)], () => {
      u.set(7);
    });
  }).toJson();
}

test("Ticket 90: Memory gate hazard stays unmerged", () => {
  // Loop 1: FLAG holds, first body clears it; the guarded second gate
  // re-reads it, so d.set(7) never runs.
  const report: SimReport = runSim(memoryHazard(), 1);
  assert.deepEqual(report.var_values, [0]);
  assert.deepEqual(staticCost(memoryHazard()), [3, 2, 3, 7_228]);
});

test("Ticket 90: Deaths gate hazard stays unmerged", () => {
  const report: SimReport = runSim(deathsHazard(), 1);
  assert.deepEqual(report.var_values, [0]);
  assert.deepEqual(staticCost(deathsHazard()), [3, 2, 3, 7_228]);
});

test("Ticket 90: Switch gate hazard stays unmerged", () => {
  const report: SimReport = runSim(switchHazard(), 1);
  assert.deepEqual(report.var_values, [0]);
  assert.deepEqual(staticCost(switchHazard()), [3, 2, 3, 7_228]);
});

test("Ticket 90: paged-var gate still merges", () => {
  const report: SimReport = runSim(pagedPair(), 1);
  assert.deepEqual(report.var_values, [1, 9, 7]);
  assert.deepEqual(staticCost(pagedPair()), [1, 1, 2, 2_420]);
});

test("Ticket 90: paged-var gate with init still merges", () => {
  const report: SimReport = runSim(pagedPairWithInit(), 1);
  assert.deepEqual(report.var_values, [1, 9, 7]);
  assert.deepEqual(staticCost(pagedPairWithInit()), [2, 1, 3, 4_828]);
});

test("Ticket 90: external gate with paged-only bodies still merges", () => {
  const report: SimReport = runSim(externalGatePagedBodies(), 1);
  assert.deepEqual(report.var_values, [9, 7]);
  assert.deepEqual(staticCost(externalGatePagedBodies()), [2, 1, 3, 4_824]);
});

test("Ticket 90: external gate transfer pair still merges", () => {
  const report: SimReport = runSim(externalGateTransferPair(), 1);
  assert.deepEqual(report.var_values, [3, 5, 3, 5]);
  assert.deepEqual(staticCost(externalGateTransferPair()), [6, 2, 73, 14_456]);
});

test("Ticket 90: masked DeathsX gate still merges", () => {
  const report: SimReport = runSim(maskedPair(), 1);
  assert.deepEqual(report.var_values, [3, 9, 7]);
  assert.deepEqual(staticCost(maskedPair()), [1, 1, 2, 2_420]);
});
