/**
 * Ticket #98: sibling gate fusion admitted for any flat body (phase 1).
 *
 * `crates/eudkit-opt/src/temporal.rs` step 3 merges adjacent identically-gated
 * sibling `when`/`once` blocks. Before #98 the admission predicate only
 * accepted two body kinds: leaf act runs (up to the 64-act `Trigger` cap) and
 * pure `VarTransfer`/`VarBatch` runs. A body holding a `VarStore` — or a
 * transfer and an act side by side — never merged even under a plain paged-var
 * gate the #90 hazard guard would accept.
 *
 * Phase 1 widens the admission to any *flat* body (`Act | VarTransfer |
 * VarBatch | VarStore`); nested `when`/`once`/`While` still rejects, and the
 * #90 storage clauses are reused unchanged. Phase 1 left an external-state
 * gate (`Memory`/`Deaths`/`Switch`) over a `VarStore` body unmerged; phase 2
 * (#112) now adds a precise-address clause, so the `Memory(FLAG)` pair over
 * stores at `TABLE`/`TABLE+4` merges and moved into the merge class below.
 *
 * The merge pins below are red before the change and green after; the hazard
 * pins are green on both sides (they pin that the widening did not weaken the
 * #90 guard). Figures are `[triggers, conditions, actions, total_bytes]` from
 * `runStatic`; `var_values` comes from `runSim`.
 *
 * The same probes live in `py/tests/test_ticket98.py`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic } from "./helpers.ts";
import {
  AtLeast,
  Memory,
  SetDeaths,
  SetMemory,
  SetTo,
  Trigger,
  Var,
  buildModule,
  emitVarStore,
  when,
} from "../index.ts";

const FLAG = 0x57f0b4;
const TABLE = 0x600000;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

/**
 * Paged gate `g == 1`, eight single-`VarStore` bodies.
 *
 * The idiomatic unrolled `for i: if g: table[i] = src` shape. A store needs no
 * read-set admission (its destination is an absolute address), so all eight
 * bodies share one firing.
 */
function eightStoreBodies(): string {
  return buildModule(() => {
    const g = Var(1);
    const src = Var(3);
    for (let i = 0; i < 8; i += 1) {
      when([g.eq(1)], () => {
        emitVarStore(src.id, { addr: TABLE + 4 * i });
      });
    }
  }).toJson();
}

/** Paged gate, four bodies of two stores each: the accumulated re-check. */
function fourPairStoreBodies(): string {
  return buildModule(() => {
    const g = Var(1);
    const src = Var(3);
    for (let i = 0; i < 4; i += 1) {
      when([g.eq(1)], () => {
        emitVarStore(src.id, { addr: TABLE + 8 * i });
        emitVarStore(src.id, { addr: TABLE + 4 + 8 * i });
      });
    }
  }).toJson();
}

/** Paged gate over `[act, act]` and `[transfer, act]` bodies. */
function mixedActTransferPair(): string {
  return buildModule(() => {
    const g = Var(1);
    const w = Var(0);
    const src = Var(3);
    when([g.eq(1)], () => {
      w.set(9);
      SetDeaths(TABLE, SetTo, 1);
    });
    when([g.eq(1)], () => {
      src.set(w);
      SetDeaths(TABLE + 4, SetTo, 2);
    });
  }).toJson();
}

/** Paged gate over two `[VarStore, VarTransfer]` bodies. */
function storeTransferPair(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(3);
    const b = Var(5);
    const x = Var(0);
    const y = Var(0);
    when([g.eq(1)], () => {
      emitVarStore(a.id, { addr: TABLE });
      x.set(a);
    });
    when([g.eq(1)], () => {
      emitVarStore(b.id, { addr: TABLE + 4 });
      y.set(b);
    });
  }).toJson();
}

/**
 * Paged gate over two trigger-field store bodies.
 *
 * A `VarStore` with `triggerId` writes a trigger field, not paged storage, so
 * a paged gate (which reads only paged storage) is safe; an external gate
 * would still reject the body (clause (ii)).
 */
function dstTriggerPair(): string {
  return buildModule(() => {
    const g = Var(1);
    const src = Var(3);
    new Trigger({ conds: [], acts: [] });
    when([g.eq(1)], () => {
      emitVarStore(src.id, { triggerId: 0, offset: 20 });
    });
    when([g.eq(1)], () => {
      emitVarStore(src.id, { triggerId: 0, offset: 24 });
    });
  }).toJson();
}

/**
 * External `Memory` gate over two store bodies at distinct dwords.
 *
 * Phase 2 (#112) keys both stores on `Epd(TABLE)`/`Epd(TABLE+4)` and the gate
 * on `Epd(FLAG)`, so the pair merges; this records that hand-off.
 */
function externalGateStorePair(): string {
  return buildModule(() => {
    const src = Var(3);
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE });
    });
    when([Memory(FLAG, AtLeast, 1)], () => {
      emitVarStore(src.id, { addr: TABLE + 4 });
    });
  }).toJson();
}

/** First body writes the gate variable, then a store: must stay apart. */
function gateWritingStoreBody(): string {
  return buildModule(() => {
    const g = Var(1);
    const src = Var(3);
    const d = Var(0);
    when([g.eq(1)], () => {
      g.set(2);
      emitVarStore(src.id, { addr: TABLE });
    });
    when([g.eq(1)], () => {
      d.set(9);
    });
  }).toJson();
}

/** Two identically-gated leaf-act bodies straddling the 64-act cap. */
function capBoundary(first: number, second: number): string {
  return buildModule(() => {
    const g = Var(1);
    when([g.eq(1)], () => {
      for (let i = 0; i < first; i += 1) {
        SetDeaths(TABLE + 4 * i, SetTo, 1);
      }
    });
    when([g.eq(1)], () => {
      for (let i = 0; i < second; i += 1) {
        SetDeaths(TABLE + 4 * (100 + i), SetTo, 1);
      }
    });
  }).toJson();
}

/** Three identically-gated paged siblings, one paged act each. */
function threeSiblings(): string {
  return buildModule(() => {
    const g = Var(1);
    const a = Var(0);
    const b = Var(0);
    const c = Var(0);
    when([g.eq(1)], () => {
      a.set(1);
    });
    when([g.eq(1)], () => {
      b.set(2);
    });
    when([g.eq(1)], () => {
      c.set(3);
    });
  }).toJson();
}

/** The middle sibling writes the gate variable: the third must not ride. */
function threeSiblingsMiddleWritesGate(): string {
  return buildModule(() => {
    const a = Var(0);
    const b = Var(0);
    const g = Var(1);
    const c = Var(0);
    when([g.eq(1)], () => {
      a.set(2);
    });
    when([g.eq(1)], () => {
      g.set(2);
      b.set(1);
    });
    when([g.eq(1)], () => {
      c.set(3);
    });
  }).toJson();
}

/**
 * Body-2's store reads body-1's act; a later gate must see the store.
 */
function orderingAfterMerge(): string {
  return buildModule(() => {
    const g = Var(1);
    const v = Var(0);
    const d = Var(0);
    when([g.eq(1)], () => {
      v.set(5);
    });
    when([g.eq(1)], () => {
      emitVarStore(v.id, { addr: TABLE });
    });
    when([Memory(TABLE, AtLeast, 5)], () => {
      d.iadd(1);
    });
  }).toJson();
}

/**
 * Body-2's transfer reads body-1's transfer destination.
 *
 * The merge must not hoist the transfer ahead of body-1's firing: `z = x`
 * has to observe the transfer `x = y`, so `z` ends at 5, not the stale 0.
 *
 * Both bodies are transfers (`Var.set(Var)` emits a `VarTransfer`), so this
 * pin does not exercise the scheduler's act-hoist guard
 * (`act_is_agnostic`) — the two guard pins below do that.
 */
function readerAfterTransfer(): string {
  return buildModule(() => {
    const g = Var(1);
    const x = Var(0);
    const y = Var(5);
    const z = Var(0);
    when([g.eq(1)], () => {
      x.set(y);
    });
    when([g.eq(1)], () => {
      z.set(x);
    });
  }).toJson();
}

/**
 * Body-2 act writes the source of body-1's store.
 *
 * The scheduler must keep the act behind the store firing: the store has to
 * read `v == 0` before `v.set(2)` runs, so a later `Memory(TABLE) >= 1`
 * gate must not fire. This is a scheduler-guard pin, not a merge pin: it is
 * green on the pre-#98 tree too (separate triggers preserve order) and goes
 * red if `act_is_agnostic` ever claims the act can run either side of the
 * store firing, so no static cost is pinned here.
 */
function actWritingMergedStoreSource(): string {
  return buildModule(() => {
    const g = Var(1);
    const v = Var(0);
    const d = Var(0);
    when([g.eq(1)], () => {
      emitVarStore(v.id, { addr: TABLE });
    });
    when([g.eq(1)], () => {
      v.set(2);
    });
    when([Memory(TABLE, AtLeast, 1)], () => {
      d.set(1);
    });
  }).toJson();
}

/**
 * Body-2 act writes the merged store's destination.
 *
 * `SetMemory` has an opaque (absolute-address) destination, so the scheduler
 * must keep it behind the store firing: the store writes `TABLE = 0` first,
 * then the act writes `TABLE = 9`. Same guard-pin shape as above — green
 * before #98, red under a broken `act_is_agnostic` — so no static cost is
 * pinned here.
 */
function opaqueActAfterMergedStore(): string {
  return buildModule(() => {
    const g = Var(1);
    const v = Var(0);
    const d = Var(0);
    when([g.eq(1)], () => {
      emitVarStore(v.id, { addr: TABLE });
    });
    when([g.eq(1)], () => {
      SetMemory(TABLE, SetTo, 9);
    });
    when([Memory(TABLE, AtLeast, 1)], () => {
      d.set(1);
    });
  }).toJson();
}

test("Ticket 98: eight store bodies share one firing", () => {
  assert.deepEqual(runSim(eightStoreBodies(), 1).var_values, [1, 3]);
  assert.deepEqual(staticCost(eightStoreBodies()), [11, 1, 90, 26_492]);
});

test("Ticket 98: four pair store bodies share one firing", () => {
  assert.deepEqual(runSim(fourPairStoreBodies(), 1).var_values, [1, 3]);
  assert.deepEqual(staticCost(fourPairStoreBodies()), [11, 1, 90, 26_492]);
});

test("Ticket 98: mixed act+transfer pair shares one gate", () => {
  assert.deepEqual(runSim(mixedActTransferPair(), 1).var_values, [1, 9, 9]);
  assert.deepEqual(staticCost(mixedActTransferPair()), [4, 1, 72, 9_640]);
});

test("Ticket 98: store+transfer pair shares one gate", () => {
  assert.deepEqual(runSim(storeTransferPair(), 1).var_values, [1, 3, 5, 3, 5]);
  assert.deepEqual(staticCost(storeTransferPair()), [6, 1, 77, 14_460]);
});

test("Ticket 98: dst_trigger store pair shares one gate", () => {
  assert.deepEqual(runSim(dstTriggerPair(), 1).var_values, [1, 3]);
  assert.deepEqual(staticCost(dstTriggerPair()), [6, 1, 72, 14_452]);
});

test("Ticket 98: external gate over store bodies merges", () => {
  // Phase 2 (#112): the gate reads Epd(FLAG), the bodies write
  // Epd(TABLE)/Epd(TABLE+4), so the dwords are provably disjoint. Simulation
  // state is identical to the pre-#112 unmerged pin.
  assert.deepEqual(runSim(externalGateStorePair(), 1).var_values, [3]);
  assert.deepEqual(runSim(externalGateStorePair(), 1).external_epds, {
    "120615": 3,
    "120616": 3,
    "4294955860": 1,
  });
  assert.deepEqual(staticCost(externalGateStorePair()), [6, 1, 73, 14_448]);
});

test("Ticket 98: store body that writes the gate var stays unmerged", () => {
  assert.deepEqual(runSim(gateWritingStoreBody(), 1).var_values, [2, 3, 0]);
  assert.deepEqual(staticCost(gateWritingStoreBody()), [5, 2, 71, 12_048]);
});

test("Ticket 98: leaf-act cap still bounds a leaf pair", () => {
  // 63+1 fits one 64A trigger; 63+2 does not, and the pair must not fall
  // through to the uncapped flat admission.
  assert.deepEqual(staticCost(capBoundary(63, 1)), [1, 1, 64, 2_412]);
  assert.deepEqual(staticCost(capBoundary(63, 2)), [2, 2, 65, 4_820]);
});

test("Ticket 98: three leaf siblings all merge", () => {
  assert.deepEqual(runSim(threeSiblings(), 1).var_values, [1, 1, 2, 3]);
  assert.deepEqual(staticCost(threeSiblings()), [1, 1, 3, 2_420]);
});

test("Ticket 98: third sibling does not ride on a stale gate", () => {
  assert.deepEqual(runSim(threeSiblingsMiddleWritesGate(), 1).var_values, [
    2, 1, 2, 0,
  ]);
  assert.deepEqual(staticCost(threeSiblingsMiddleWritesGate()), [
    3, 3, 4, 7_240,
  ]);
});

test("Ticket 98: store in a merged body keeps source order", () => {
  // Merging body 1 into body 2's store must not hoist the store ahead of
  // body-1's act: the later `Memory(X) >= 5` gate has to observe the store,
  // so `d` reaches 2 after two loops (a hoist would see v == 0).
  assert.deepEqual(runSim(orderingAfterMerge(), 2).var_values, [1, 5, 2]);
  assert.deepEqual(staticCost(orderingAfterMerge()), [5, 2, 71, 12_048]);
});

test("Ticket 98: transfer reading a merged transfer destination runs after it", () => {
  // `when(g){x=y}` + `when(g){z=x}` merges into one gate; the scheduler must
  // keep `z = x` behind the transfer firing, so z is 5, not 0.
  assert.deepEqual(runSim(readerAfterTransfer(), 1).var_values, [1, 5, 5, 5]);
  assert.deepEqual(staticCost(readerAfterTransfer()), [5, 1, 72, 12_048]);
});

test("Ticket 98: act writing a merged store source stays behind the store", () => {
  // A hoist would let the store read v == 2, so the Memory gate would fire
  // and d would reach 1; correct order leaves TABLE == 0, d == 0.
  assert.deepEqual(runSim(actWritingMergedStoreSource(), 1).var_values, [
    1, 2, 0,
  ]);
});

test("Ticket 98: opaque act after a merged store stays behind it", () => {
  // A hoist would let the store overwrite the act, leaving TABLE == 0 and
  // d == 0; correct order leaves TABLE == 9, so the gate fires.
  assert.deepEqual(runSim(opaqueActAfterMergedStore(), 1).var_values, [
    1, 0, 1,
  ]);
});
