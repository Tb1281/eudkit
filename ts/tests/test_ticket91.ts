/**
 * Ticket #91: merge adjacent identically-gated `once` blocks too.
 *
 * `crates/eudkit-opt/src/temporal.rs` step 3 merged only sibling `when`
 * blocks, so two adjacent `once` blocks with identical condition lists each
 * packed on their own in step 4 into a `preserved: false` `Trigger` — two
 * rows that self-disable independently, at full per-row cost. #91 extends the
 * same step to `Once`+`Once` (never `Once`+`When`) with the *same*
 * dependency guard #90 defined, so an external-state gate cannot ride over an
 * earlier body's write (defect A in `once` form).
 *
 * These pins are red before the fix and green after (static cost, the
 * hand-built one-`once` control cost):
 *
 * - gated leaf-act N=8: 8T/8C/8A/19,296 B → 1T/1C/8A/2,440 B;
 * - gated leaf-act N=2: 2T/2C/2A/4,824 B → 1T/1C/2A/2,416 B;
 * - gated transfer N=8: 25T/8C/104A/60,236 B → 4T/1C/83A/9,668 B;
 * - gated transfer N=2: 7T/2C/74A/16,868 B → 4T/1C/71A/9,644 B;
 * - 0-cond transfer N=8: 25T/0C/104A/60,232 B → 4T/0C/83A/9,664 B;
 * - paged-var gate with an init trigger: 3T/2C/3A/7,236 B →
 *   2T/1C/3A/4,828 B, values `[1, 9, 7]` both ways.
 *
 * Transfer twins use a distinct source var per sibling (the #69 shape); a
 * shared source var does not coalesce and is not the pin. `executed_cost`
 * reports the *last* loop and is `0T` for both merged and unmerged `once`s
 * after loop 1, so the pins are static cost plus `var_values`.
 *
 * The negative pins must stay unmerged, different-gate first (the F1 lesson
 * from #73: it is the mutation pin for the identical-conds precondition):
 * different gates 7T/2C/74A/16,868 B; an earlier body that writes the gate
 * var 7T/2C/74A/16,864 B; a non-leaf pair 11T/4C/78A/26,500 B; the
 * external-state hazard 3T/2C/3A/7,228 B with `d == 0` at loops=1 (a naive
 * merge gives 2T/1C/3A/4,820 B with `d == 7`); the var-gate hazard
 * 3T/2C/3A/7,232 B with values `[2, 0]` (a naive merge gives 2T/1C/3A/4,824
 * B, values `[2, 9]`); and a mixed `once`+`when` pair 2T/2C/2A/4,824 B,
 * values `[1, 1, 4]` after 4 loops.
 *
 * The same probes live in `py/tests/test_ticket91.py`.
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
  once,
  when,
} from "../index.ts";

const FLAG = 0x57f0b4;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

/** N sibling `once`s with one identical paged-var gate and one act each. */
function leafSiblings(n: number): string {
  return buildModule(() => {
    const gate = Var(0);
    const vars_ = Array.from({ length: n }, () => Var(0));
    for (let i = 0; i < n; i += 1) {
      const v = vars_[i];
      once([gate.eq(0)], () => {
        v.set(1 + i);
      });
    }
  }).toJson();
}

/** N sibling `once`s with one identical-gate transfer each (#69 shape). */
function transferSiblings(n: number): string {
  return buildModule(() => {
    const gate = Var(0);
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    for (let i = 0; i < n; i += 1) {
      const dst = dsts[i];
      const src = srcs[i];
      once([gate.eq(0)], () => {
        dst.set(src);
      });
    }
  }).toJson();
}

/** The same N transfers under 0-cond `once`s; no unused gate var. */
function zeroCondTransferSiblings(n: number): string {
  return buildModule(() => {
    const srcs = Array.from({ length: n }, (_, i) => Var(1 + i));
    const dsts = Array.from({ length: n }, () => Var(0));
    for (let i = 0; i < n; i += 1) {
      const dst = dsts[i];
      const src = srcs[i];
      once([], () => {
        dst.set(src);
      });
    }
  }).toJson();
}

/** Identical paged-var gates plus an unrelated static init trigger. */
function pagedPairWithInit(): string {
  return buildModule(() => {
    const g = Var(1);
    const w = Var(0);
    const u = Var(0);
    new Trigger({ conds: [], acts: [SetDeaths(0, SetTo, 1)] });
    once([g.eq(1)], () => {
      w.set(9);
    });
    once([g.eq(1)], () => {
      u.set(7);
    });
  }).toJson();
}

/** Two adjacent transfer `once`s under *different* gates: must not merge. */
function differentGates(): string {
  return buildModule(() => {
    const g1 = Var(0);
    const g2 = Var(0);
    const s = Var(5);
    const d = Var(0);
    const e = Var(0);
    once([g1.eq(0)], () => {
      d.set(s);
    });
    once([g2.eq(0)], () => {
      e.set(s);
    });
  }).toJson();
}

/** Same gate, but the earlier body writes the gate variable: must not merge. */
function guardWritesGate(): string {
  return buildModule(() => {
    const c = Var(1);
    const s = Var(5);
    const d = Var(0);
    once([c.eq(1)], () => {
      c.set(s);
    });
    once([c.eq(1)], () => {
      d.set(s);
    });
  }).toJson();
}

/** Two identical-gate `once`s whose bodies are nested `when`s. */
function nonleafPair(): string {
  return buildModule(() => {
    const g = Var(1);
    const h = Var(0);
    const s = Var(5);
    const d = Var(0);
    const e = Var(0);
    once([g.eq(1)], () => {
      when([h.eq(0)], () => {
        d.set(s);
      });
    });
    once([g.eq(1)], () => {
      when([h.eq(0)], () => {
        e.set(s);
      });
    });
  }).toJson();
}

/** First body clears the memory cell the shared gate reads (defect A). */
function externalStateHazard(): string {
  return buildModule(() => {
    new Trigger({ conds: [], acts: [SetMemory(FLAG, SetTo, 1)] });
    const d = Var(0);
    once([Memory(FLAG, AtLeast, 1)], () => {
      SetMemory(FLAG, SetTo, 0);
    });
    once([Memory(FLAG, AtLeast, 1)], () => {
      d.set(7);
    });
  }).toJson();
}

/** First body rewrites the paged-var gate: the second must re-observe it. */
function varGateHazard(): string {
  return buildModule(() => {
    const g = Var(1);
    const w = Var(0);
    new Trigger({ conds: [], acts: [SetDeaths(0, SetTo, 1)] });
    once([g.eq(1)], () => {
      g.set(2);
    });
    once([g.eq(1)], () => {
      w.set(9);
    });
  }).toJson();
}

/** `once`+`when` under the same gate: never merged, different cadences. */
function mixedPair(): string {
  return buildModule(() => {
    const g = Var(1);
    const x = Var(0);
    const y = Var(0);
    once([g.eq(1)], () => {
      x.set(1);
    });
    when([g.eq(1)], () => {
      y.iadd(1);
    });
  }).toJson();
}

test("Ticket 91: gated leaf once siblings merge N=8", () => {
  assert.deepEqual(runSim(leafSiblings(8), 1).var_values, [0, 1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(staticCost(leafSiblings(8)), [1, 1, 8, 2_440]);
});

test("Ticket 91: gated leaf once siblings merge N=2", () => {
  assert.deepEqual(runSim(leafSiblings(2), 1).var_values, [0, 1, 2]);
  assert.deepEqual(staticCost(leafSiblings(2)), [1, 1, 2, 2_416]);
});

test("Ticket 91: gated transfer once siblings merge N=8", () => {
  assert.deepEqual(
    runSim(transferSiblings(8), 1).var_values,
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8],
  );
  assert.deepEqual(staticCost(transferSiblings(8)), [4, 1, 83, 9_668]);
});

test("Ticket 91: gated transfer once siblings merge N=2", () => {
  assert.deepEqual(runSim(transferSiblings(2), 1).var_values, [0, 1, 2, 1, 2]);
  assert.deepEqual(staticCost(transferSiblings(2)), [4, 1, 71, 9_644]);
});

test("Ticket 91: zero-cond transfer once siblings merge N=8", () => {
  assert.deepEqual(
    runSim(zeroCondTransferSiblings(8), 1).var_values,
    [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8],
  );
  assert.deepEqual(staticCost(zeroCondTransferSiblings(8)), [4, 0, 83, 9_664]);
});

test("Ticket 91: paged-var gate once siblings merge", () => {
  assert.deepEqual(runSim(pagedPairWithInit(), 1).var_values, [1, 9, 7]);
  assert.deepEqual(staticCost(pagedPairWithInit()), [2, 1, 3, 4_828]);
});

test("Ticket 91: different gates stay unmerged", () => {
  assert.deepEqual(staticCost(differentGates()), [7, 2, 74, 16_868]);
});

test("Ticket 91: earlier body writing the gate var stays unmerged", () => {
  assert.deepEqual(staticCost(guardWritesGate()), [7, 2, 74, 16_864]);
});

test("Ticket 91: non-leaf once pair stays unmerged", () => {
  assert.deepEqual(staticCost(nonleafPair()), [11, 4, 78, 26_500]);
});

test("Ticket 91: external-state gate hazard stays unmerged", () => {
  assert.deepEqual(runSim(externalStateHazard(), 1).var_values, [0]);
  assert.deepEqual(staticCost(externalStateHazard()), [3, 2, 3, 7_228]);
});

test("Ticket 91: var-gate hazard stays unmerged", () => {
  assert.deepEqual(runSim(varGateHazard(), 1).var_values, [2, 0]);
  assert.deepEqual(staticCost(varGateHazard()), [3, 2, 3, 7_232]);
});

test("Ticket 91: mixed once and when stays unmerged", () => {
  assert.deepEqual(runSim(mixedPair(), 4).var_values, [1, 1, 4]);
  assert.deepEqual(staticCost(mixedPair()), [2, 2, 2, 4_824]);
});
