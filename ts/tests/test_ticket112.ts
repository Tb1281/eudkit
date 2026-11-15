/**
 * Ticket #112: precise external-gate address keys for sibling gate fusion.
 *
 * `crates/eudkit-opt/src/temporal.rs` step 3 merges adjacent identically-gated
 * sibling `when`/`once` blocks. #98 phase 1 admitted any *flat* body but kept
 * the #90 storage guard unchanged: under an external-state gate (`Memory` /
 * `Deaths`, condtype 15; `Switch`, condtype 11) the earlier body had to write
 * only paged variables, so any absolute write — every `VarStore`, every
 * `SetMemory`/`SetDeaths` — refused the merge even when the body wrote a
 * *different* dword than the gate reads.
 *
 * Phase 2 adds a third admission clause: the merge is allowed when every
 * external cell the earlier body writes is provably a different dword from
 * every cell the gate reads. The key model is a private `StorageKey` in
 * `temporal.rs`: paged variables (`Var`), EPD addresses (`Epd`, the raw HIR
 * field value — `Memory(FLAG).player == SetMemory(FLAG).dest == epd_u32(FLAG)`),
 * and switch ids (`Switch`). Unknown writes fall back to today's answer, so
 * the clause only ever admits a merge; it never withdraws one.
 *
 * The merge pins below are red before the change and green after; the hazard
 * pins are green on both sides. Figures are `[triggers, conditions, actions,
 * total_bytes]` from `runStatic`; `var_values`/`external_epds` come from
 * `runSim`. Every row was re-derived with this repo's own harness, and the
 * merged reference (the same statements hand-written in one `when`) reports
 * identical simulation state.
 *
 * The unaligned-`dst_addr` case cannot be built through the DSL after #111
 * (the frontends reject unaligned absolute dword addresses), so its unit pin
 * lives in `crates/eudkit-opt/src/temporal.rs` next to the key model.
 *
 * Cycle 3 #02 adds the *backward* merge in the same step 3: an unconditional
 * act run between two identically-gated `when` siblings is moved ahead of the
 * gate when it provably commutes with the earlier leaf body and the gate's
 * storage (`when(G){A}; X; when(G){B}` -> `X; when(G){A;B}`). The adopted
 * matrix below is red before that path and green after; the rejected matrix
 * pins the narrow scope (gate-cell/gate-var writes, RAW/WAR, conditional
 * barriers, `VarStore` barriers, and the deliberate `once` exclusion). The
 * `exec` triples are `[triggers, conditions, actions]` from `runSim`.
 *
 * The same probes live in `py/tests/test_ticket112.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic } from "./helpers.ts";
import {
  Always,
  AtLeast,
  CurrentPlayer,
  Deaths,
  EPD,
  FixedString,
  Memory,
  SetDeaths,
  SetMemory,
  SetStringDword,
  SetSwitch,
  SetTo,
  Switch,
  SwitchActionSet,
  SwitchSet,
  Trigger,
  Var,
  buildModule,
  emitVarStore,
  once,
  when,
} from "../index.ts";

const FLAG = 0x57f0b4;
const TABLE = 0x600000;
const TABLE_EPD = 120_615;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

function executedCost(hirJson: string, loops = 1): number[] {
  const cost = runSim(hirJson, loops).executed_cost;
  return [cost.triggers, cost.conditions, cost.actions];
}

/** `Memory(FLAG)` gate, eight single-store bodies at distinct dwords. */
function memoryGateEightStoreBodies(): string {
  return buildModule(() => {
    const src = Var(3);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    for (let i = 0; i < 8; i += 1) {
      when([Memory(FLAG, AtLeast, 1)], () => {
        emitVarStore(src.id, { addr: TABLE + 4 * i });
      });
    }
  }).toJson();
}

/** `Memory(FLAG)` gate, two `SetMemory` acts on distinct cells. */
function memoryGateTwoSetMemoryActs(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetMemory(TABLE, SetTo, 5);
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetMemory(TABLE + 4, SetTo, 6);
    });
  }).toJson();
}

/** `Deaths(EPD(FLAG))` gate, two store bodies at distinct dwords. */
function deathsGateTwoStoreBodies(): string {
  return buildModule(() => {
    const src = Var(3);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Deaths(EPD(FLAG), AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE });
    });
    when([Deaths(EPD(FLAG), AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE + 4 });
    });
  }).toJson();
}

/** `Memory(FLAG)` gate over `[act, store]` and `[store, act]`. */
function mixedActStorePair(): string {
  return buildModule(() => {
    const src = Var(3);
    const act = Var(1);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      act.set(5);
      emitVarStore(src.id, { addr: TABLE });
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE + 4 });
      act.set(6);
    });
  }).toJson();
}

/** `Switch(1)` gate, two `SetSwitch` acts on other switches. */
function switchGateTwoSetSwitchActs(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetSwitch(1, SwitchActionSet)] });
    when([Switch(1, SwitchSet)], () => {
      SetSwitch(2, SwitchActionSet);
    });
    when([Switch(1, SwitchSet)], () => {
      SetSwitch(3, SwitchActionSet);
    });
  }).toJson();
}

/**
 * The earlier body clears the very cell the shared gate reads.
 *
 * Unmerged, the second gate re-reads `FLAG == 0` and never fires, so `TABLE`
 * stays unwritten. A merge would evaluate the gate once and let both stores
 * run (`TABLE == 7`) — the equality case the key model must keep rejecting.
 */
function sameCellHazardFromGateClearingSource(): string {
  return buildModule(() => {
    const a = Var(0);
    const b = Var(7);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(a.id, { addr: FLAG });
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(b.id, { addr: TABLE });
    });
  }).toJson();
}

/** `Deaths(CurrentPlayer)` names a dynamic base, so its key is unknown. */
function currentPlayerGateStorePair(): string {
  return buildModule(() => {
    const src = Var(3);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Deaths(CurrentPlayer, AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE });
    });
    when([Deaths(CurrentPlayer, AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE + 4 });
    });
  }).toJson();
}

/** A trigger-field store writes no address the key model can compare. */
function dstTriggerStorePairUnderMemoryGate(): string {
  return buildModule(() => {
    const src = Var(3);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(src.id, { triggerId: 0, offset: 20 });
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(src.id, { triggerId: 0, offset: 24 });
    });
  }).toJson();
}

/** `SetStringDword` writes through a string reloc, not an address key. */
function setStringDwordPairUnderMemoryGate(): string {
  return buildModule(() => {
    const buf = new FixedString(64);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetStringDword(buf.messageIndex, 0, 1);
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetStringDword(buf.messageIndex, 4, 2);
    });
  }).toJson();
}

// ---------------------------------------------------------------------------
// Cycle 3 #02: backward merge across an unconditional act run
// ---------------------------------------------------------------------------

/** `when(g){A}; SetDeaths(9000); when(g){B}`: an absolute moved act. */
function pagedGateBareActRun(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    new Trigger({ conds: [], acts: [SetDeaths(9000, SetTo, 5)] });
    when([g.eq(1)], () => {
      b.set(1);
    });
  }).toJson();
}

/** Paged gate, the run writes an unrelated variable `x`. */
function pagedGateRunWritesAnotherVar(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    const x = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    x.set(5);
    when([g.eq(1)], () => {
      b.set(1);
    });
  }).toJson();
}

/** `Memory(FLAG)` gate (FLAG starts 0), the run writes `FLAG+4`. */
function externalGateRunWritesADisjointCell(): string {
  return buildModule(() => {
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetDeaths(TABLE, SetTo, 1);
    });
    new Trigger({ conds: [], acts: [SetMemory(FLAG + 4, SetTo, 5)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** Three identically-gated siblings with two unconditional runs between. */
function threeSeparatedPagedGroups(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    const c = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    new Trigger({ conds: [], acts: [SetDeaths(9000, SetTo, 5)] });
    when([g.eq(1)], () => {
      b.set(1);
    });
    new Trigger({ conds: [], acts: [SetDeaths(9004, SetTo, 6)] });
    when([g.eq(1)], () => {
      c.set(1);
    });
  }).toJson();
}

/** `Memory(FLAG)` gate (FLAG starts 0), the run writes `FLAG` itself. */
function externalGateRunWritesTheGateCell(): string {
  return buildModule(() => {
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetDeaths(TABLE, SetTo, 1);
    });
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 0)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** Paged gate, the run writes the gate variable itself (`g.iadd(1)`). */
function pagedGateRunWritesTheGateVar(): string {
  return buildModule(() => {
    const g = Var(1);
    when([g.eq(1)], () => {
      SetDeaths(TABLE, SetTo, 1);
    });
    g.iadd(1);
    when([g.eq(1)], () => {
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** RAW: the earlier body reads `x` and the run writes `x`. */
function bodyReadsTheRunWrittenVar(): string {
  return buildModule(() => {
    const g = Var(1);
    const x = Var(0);
    when([g.eq(1)], () => {
      SetDeaths(TABLE, SetTo, x);
    });
    x.set(5);
    when([g.eq(1)], () => {
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** WAR: the earlier body writes `x` and the run reads `x`. */
function runReadsTheBodyWrittenVar(): string {
  return buildModule(() => {
    const g = Var(1);
    const x = Var(0);
    when([g.eq(1)], () => {
      x.set(5);
    });
    new Trigger({ conds: [], acts: [SetDeaths(TABLE, SetTo, x)] });
    when([g.eq(1)], () => {
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** A conditional intervening trigger is not an unconditional act run. */
function conditionalTriggerBetweenTheGates(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    when([Always()], () => {
      SetDeaths(TABLE, SetTo, 5);
    });
    when([g.eq(1)], () => {
      b.set(1);
    });
  }).toJson();
}

/** Two separated `once` blocks: the backward path is `when` only. */
function twoSeparatedOnceSiblings(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    once([g.eq(1)], () => {
      a.set(1);
    });
    new Trigger({ conds: [], acts: [SetDeaths(9000, SetTo, 5)] });
    once([g.eq(1)], () => {
      b.set(1);
    });
  }).toJson();
}

/** Adjacent identical gates: the existing forward merge, unchanged. */
function adjacentPagedWhenControl(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    when([g.eq(1)], () => {
      b.set(1);
    });
  }).toJson();
}

test("Ticket 112: paged gate merges across a bare act run", () => {
  // Before: [3, 2, 3, 7_228] static, [3, 2, 3] executed.
  const sim = runSim(pagedGateBareActRun(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1]);
  assert.equal(sim.external_epds["9000"] ?? 0, 5);
  assert.deepEqual(executedCost(pagedGateBareActRun()), [2, 1, 3]);
  assert.deepEqual(staticCost(pagedGateBareActRun()), [2, 1, 3, 4_820]);
});

test("Ticket 112: paged gate merges across a var write", () => {
  // Before: [3, 2, 3, 7_232] static, [3, 2, 3] executed.
  const sim = runSim(pagedGateRunWritesAnotherVar(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1, 5]);
  assert.deepEqual(executedCost(pagedGateRunWritesAnotherVar()), [2, 1, 3]);
  assert.deepEqual(staticCost(pagedGateRunWritesAnotherVar()), [
    2, 1, 3, 4_824,
  ]);
});

test("Ticket 112: external gate merges across a disjoint cell write", () => {
  // Before: [3, 2, 3, 7_224] static, [3, 2, 1] executed. FLAG stays 0, so
  // the bodies do not run; only the moved disjoint write executes.
  const sim = runSim(externalGateRunWritesADisjointCell(), 1);
  const flagKey = `${EPD(FLAG) >>> 0}`;
  const movedKey = `${EPD(FLAG + 4) >>> 0}`;
  assert.equal(sim.external_epds[flagKey] ?? 0, 0);
  assert.equal(sim.external_epds[movedKey] ?? 0, 5);
  assert.deepEqual(executedCost(externalGateRunWritesADisjointCell()), [
    2, 1, 1,
  ]);
  assert.deepEqual(staticCost(externalGateRunWritesADisjointCell()), [
    2, 1, 3, 4_816,
  ]);
});

test("Ticket 112: three separated groups merge into one gate", () => {
  // Before: [5, 3, 5, 12_044] static, [5, 3, 5] executed. After: 2T, not
  // 3T — the two moved unconditional acts pack into one trigger.
  const sim = runSim(threeSeparatedPagedGroups(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1, 1]);
  assert.equal(sim.external_epds["9000"] ?? 0, 5);
  assert.equal(sim.external_epds["9004"] ?? 0, 6);
  assert.deepEqual(executedCost(threeSeparatedPagedGroups()), [2, 1, 5]);
  assert.deepEqual(staticCost(threeSeparatedPagedGroups()), [
    2, 1, 5, 4_820,
  ]);
});

test("Ticket 112: external gate run writing the gate cell stays unmerged", () => {
  // Before and after: [3, 2, 3, 7_224] static, [3, 2, 1] executed.
  const sim = runSim(externalGateRunWritesTheGateCell(), 1);
  assert.equal(sim.external_epds[`${EPD(FLAG) >>> 0}`] ?? 0, 0);
  assert.deepEqual(executedCost(externalGateRunWritesTheGateCell()), [
    3, 2, 1,
  ]);
  assert.deepEqual(staticCost(externalGateRunWritesTheGateCell()), [
    3, 2, 3, 7_224,
  ]);
});

test("Ticket 112: paged gate run writing the gate var stays unmerged", () => {
  // Before and after: [3, 2, 3, 7_228] static, [3, 2, 2] executed.
  const sim = runSim(pagedGateRunWritesTheGateVar(), 1);
  assert.deepEqual(sim.var_values, [2]);
  assert.deepEqual(executedCost(pagedGateRunWritesTheGateVar()), [3, 2, 2]);
  assert.deepEqual(staticCost(pagedGateRunWritesTheGateVar()), [
    3, 2, 3, 7_228,
  ]);
});

test("Ticket 112: body read of a run-written var stays unmerged", () => {
  // Before and after: [3, 2, 3, 7_232] static, [3, 2, 3] executed.
  const sim = runSim(bodyReadsTheRunWrittenVar(), 1);
  assert.deepEqual(sim.var_values, [1, 5]);
  assert.deepEqual(executedCost(bodyReadsTheRunWrittenVar()), [3, 2, 3]);
  assert.deepEqual(staticCost(bodyReadsTheRunWrittenVar()), [
    3, 2, 3, 7_232,
  ]);
});

test("Ticket 112: run read of a body-written var stays unmerged", () => {
  // Before and after: [3, 2, 3, 7_232] static, [3, 2, 3] executed.
  const sim = runSim(runReadsTheBodyWrittenVar(), 1);
  assert.deepEqual(sim.var_values, [1, 5]);
  assert.deepEqual(executedCost(runReadsTheBodyWrittenVar()), [3, 2, 3]);
  assert.deepEqual(staticCost(runReadsTheBodyWrittenVar()), [
    3, 2, 3, 7_232,
  ]);
});

test("Ticket 112: conditional trigger between the gates stays unmerged", () => {
  // Before and after: [3, 3, 3, 7_228] static, [3, 3, 3] executed.
  const sim = runSim(conditionalTriggerBetweenTheGates(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1]);
  assert.deepEqual(executedCost(conditionalTriggerBetweenTheGates()), [
    3, 3, 3,
  ]);
  assert.deepEqual(staticCost(conditionalTriggerBetweenTheGates()), [
    3, 3, 3, 7_228,
  ]);
});

test("Ticket 112: separated once siblings stay unmerged", () => {
  // Before and after: [3, 2, 3, 7_228] static, [3, 2, 3] executed.
  const sim = runSim(twoSeparatedOnceSiblings(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1]);
  assert.equal(sim.external_epds["9000"] ?? 0, 5);
  assert.deepEqual(executedCost(twoSeparatedOnceSiblings()), [3, 2, 3]);
  assert.deepEqual(staticCost(twoSeparatedOnceSiblings()), [
    3, 2, 3, 7_228,
  ]);
});

test("Ticket 112: adjacent paged when control is unchanged", () => {
  // The existing forward merge control: [1, 1, 2, 2_412], exec [1, 1, 2].
  const sim = runSim(adjacentPagedWhenControl(), 1);
  assert.deepEqual(sim.var_values, [1, 1, 1]);
  assert.deepEqual(executedCost(adjacentPagedWhenControl()), [1, 1, 2]);
  assert.deepEqual(staticCost(adjacentPagedWhenControl()), [
    1, 1, 2, 2_412,
  ]);
});

test("Ticket 112: Memory gate over eight store bodies merges", () => {
  assert.deepEqual(runSim(memoryGateEightStoreBodies(), 1).var_values, [3]);
  assert.deepEqual(staticCost(memoryGateEightStoreBodies()), [
    12, 1, 91, 28_896,
  ]);
});

test("Ticket 112: Memory gate over two SetMemory acts merges", () => {
  assert.deepEqual(runSim(memoryGateTwoSetMemoryActs(), 1).var_values, []);
  assert.deepEqual(staticCost(memoryGateTwoSetMemoryActs()), [2, 1, 3, 4_816]);
});

test("Ticket 112: Deaths gate over two store bodies merges", () => {
  assert.deepEqual(runSim(deathsGateTwoStoreBodies(), 1).var_values, [3]);
  assert.deepEqual(staticCost(deathsGateTwoStoreBodies()), [6, 1, 73, 14_448]);
});

test("Ticket 112: Memory gate over mixed act+store pair merges", () => {
  assert.deepEqual(runSim(mixedActStorePair(), 1).var_values, [3, 6]);
  assert.deepEqual(staticCost(mixedActStorePair()), [6, 1, 75, 14_452]);
});

test("Ticket 112: Switch gate over two SetSwitch acts merges", () => {
  assert.deepEqual(runSim(switchGateTwoSetSwitchActs(), 1).var_values, []);
  assert.deepEqual(staticCost(switchGateTwoSetSwitchActs()), [
    2, 1, 3, 4_816,
  ]);
});

test("Ticket 112: same-cell hazard stays unmerged", () => {
  // Naive merge: [5, 1, 72, 12_040] with TABLE == 7. Unmerged, the second
  // gate re-reads FLAG == 0, so TABLE is never written.
  const sim = runSim(sameCellHazardFromGateClearingSource(), 1);
  assert.deepEqual(sim.var_values, [0, 7]);
  assert.equal(sim.external_epds[`${TABLE_EPD}`] ?? 0, 0);
  assert.deepEqual(staticCost(sameCellHazardFromGateClearingSource()), [
    8, 2, 75, 19_264,
  ]);
});

test("Ticket 112: CurrentPlayer gate stays unmerged", () => {
  // Naive merge: [6, 1, 73, 14_448]. CurrentPlayer is a dynamic base, so the
  // gate's key is unknown and must not compare as a dword.
  assert.deepEqual(runSim(currentPlayerGateStorePair(), 1).var_values, [3]);
  assert.deepEqual(staticCost(currentPlayerGateStorePair()), [
    8, 2, 75, 19_264,
  ]);
});

test("Ticket 112: dst_trigger store pair stays unmerged", () => {
  // Naive merge: [6, 1, 73, 14_448]. A trigger field is not an address.
  assert.deepEqual(runSim(dstTriggerStorePairUnderMemoryGate(), 1).var_values, [
    3,
  ]);
  assert.deepEqual(staticCost(dstTriggerStorePairUnderMemoryGate()), [
    8, 2, 75, 19_264,
  ]);
});

test("Ticket 112: SetStringDword pair stays unmerged", () => {
  // Naive merge: [2, 1, 3, 4_816]. A string-buffer write carries a
  // target_string reloc at the dest field, not a comparable address.
  assert.deepEqual(runSim(setStringDwordPairUnderMemoryGate(), 1).var_values, []);
  assert.deepEqual(staticCost(setStringDwordPairUnderMemoryGate()), [
    3, 2, 3, 7_224,
  ]);
});
