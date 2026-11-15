/**
 * 0-cond + boolean conds tests (decided 2026-11-04).
 *
 * when()/once() gain body-only overloads (unconditional, 0 conds);
 * boolean true maps to 0 conds (NOT an Always cond); boolean false
 * drops the whole block at the DSL level (body never runs, no node
 * queued); until() keeps conds REQUIRED but accepts booleans as cond
 * values: until(true) trips immediately (body never runs: opt emits
 * only the reset trip-when), while
 * until(false) maps to a Never cond: the trip-when never fires, so
 * the latch stays 1 and the body runs every loop — exactly the
 * decided until(false) semantics, identical to until(Never(), body).
 *
 * Scope: when/once/until only. edge/every/after/whileLoop keep conds
 * as before (not covered here). Booleans inside a cond list are
 * out of scope (list handling unchanged).
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic } from "./helpers.ts";

import { Always, Never, Var, buildModule, once, until, when } from "../index.ts";

function collectByKind(node: unknown, kind: string, out: any[] = []): any[] {
  if (Array.isArray(node)) {
    for (const x of node) collectByKind(x, kind, out);
  } else if (node && typeof node === "object") {
    const rec = node as Record<string, unknown>;
    if (rec.kind === kind) out.push(rec);
    for (const v of Object.values(rec)) collectByKind(v, kind, out);
  }
  return out;
}

function emptyModuleJson(): string {
  return buildModule(() => {}).toJson();
}

test("when(body) emits a when node with 0 conds", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    when(() => {
      count.iadd(1);
    });
  });
  const whens = collectByKind(JSON.parse(mod.toJson()), "when");
  assert.equal(whens.length, 1);
  assert.deepEqual(whens[0].conds, []);
});

test("when(true, body) is identical to when(body)", () => {
  const a = buildModule(() => {
    const count = Var(0);
    when(true, () => {
      count.iadd(1);
    });
  });
  const b = buildModule(() => {
    const count = Var(0);
    when(() => {
      count.iadd(1);
    });
  });
  assert.equal(a.toJson(), b.toJson());
});

test("when(false, body) drops the block and never runs the body", () => {
  // The var is allocated INSIDE the body: if the body ran, a decl
  // and a node would appear and the JSON would differ from empty.
  const mod = buildModule(() => {
    when(false, () => {
      const v = Var(0);
      v.iadd(1);
    });
  });
  assert.equal(mod.toJson(), emptyModuleJson());
});

test("when(body) runs every loop", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    when(() => {
      count.iadd(1);
    });
  });
  assert.deepEqual(runSim(mod.toJson(), 1).var_values, [1]);
  assert.deepEqual(runSim(mod.toJson(), 3).var_values, [3]);
});

test("when(false, body) never runs (sim)", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    when(false, () => {
      count.iadd(1);
    });
  });
  assert.deepEqual(runSim(mod.toJson(), 3).var_values, [0]);
});

test("once(body) runs exactly once unconditionally", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    once(() => {
      count.iadd(1);
    });
  });
  const onces = collectByKind(JSON.parse(mod.toJson()), "once");
  assert.equal(onces.length, 1);
  assert.deepEqual(onces[0].conds, []);
  assert.deepEqual(runSim(mod.toJson(), 1).var_values, [1]);
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [1]);
});

test("once(true, body) is identical to once(body)", () => {
  const a = buildModule(() => {
    const count = Var(0);
    once(true, () => {
      count.iadd(1);
    });
  });
  const b = buildModule(() => {
    const count = Var(0);
    once(() => {
      count.iadd(1);
    });
  });
  assert.equal(a.toJson(), b.toJson());
});

test("once(false, body) drops the block and never runs the body", () => {
  const mod = buildModule(() => {
    once(false, () => {
      const v = Var(0);
      v.iadd(1);
    });
  });
  assert.equal(mod.toJson(), emptyModuleJson());
});

test("until(true, body) emits an until node with 0 conds", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    until(true, () => {
      count.iadd(1);
    });
  });
  const untils = collectByKind(JSON.parse(mod.toJson()), "until");
  assert.equal(untils.length, 1);
  assert.deepEqual(untils[0].conds, []);
});

test("until(true, body) never runs the body", () => {
  // Exclusive semantics (issue #72): a 0-cond until trips on the first
  // loop, so the body never runs. Opt emits only the reset trip-when;
  // there is no body statement, so no leaf act can ride along in a
  // merged trip+body trigger either.
  const mod = buildModule(() => {
    const count = Var(0);
    until(true, () => {
      count.iadd(1);
    });
  });
  // Vars: [count, latch].
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [0, 0]);
});

function staticCost(hirJson: string): number[] {
  const s = runStatic(hirJson).static_cost;
  return [s.triggers, s.conditions, s.actions, s.total_bytes];
}

test("until(true, d.set(s)) never runs the body", () => {
  // Regression pin: only the reset trip-when is emitted, so the
  // transfer body has no statement to run (d stays 0 after 4 loops).
  // Static cost is the trip trigger alone.
  const mod = buildModule(() => {
    const s = Var(5);
    const d = Var(0);
    until(true, () => {
      d.set(s);
    });
  });
  assert.deepEqual(staticCost(mod.toJson()), [1, 1, 1, 2416]);
  // Vars: [s, d, latch].
  //
  // Do NOT pin executed_cost.triggers: it reports the LAST loop's cost
  // and skips disabled triggers, so a never-run body legitimately
  // reports 0T. var_values plus the static cost is the only sound pin.
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [5, 0, 0]);
});

test("until(true, {c.iadd(1); d.set(s)}) never runs either body statement", () => {
  // Neither write happens: the leaf act has no body-when to ride
  // along in, and the transfer is its own (dropped) statement.
  const mod = buildModule(() => {
    const s = Var(5);
    const c = Var(0);
    const d = Var(0);
    until(true, () => {
      c.iadd(1);
      d.set(s);
    });
  });
  assert.deepEqual(staticCost(mod.toJson()), [1, 1, 1, 2416]);
  // Vars: [s, c, d, latch].
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [5, 0, 0, 0]);
});

test("until(true, nested when body) never runs the nested block", () => {
  // A nested when is a second statement that re-checks its own gate:
  // with no body-when emitted at all it never runs.
  const mod = buildModule(() => {
    const g = Var(1);
    const d = Var(0);
    until(true, () => {
      when(g.eq(1), () => {
        d.iadd(1);
      });
    });
  });
  assert.deepEqual(staticCost(mod.toJson()), [1, 1, 1, 2412]);
  // Vars: [g, d, latch].
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [1, 0, 0]);
});

test("until(Always(), body) never runs the body", () => {
  // Widened-predicate pin: Always() (condtype 22) reaches the same
  // trip-only emission through the array/masked-Var folds, so the
  // unconditional predicate must include all-Always conds.
  const mod = buildModule(() => {
    const s = Var(5);
    const d = Var(0);
    until(Always(), () => {
      d.set(s);
    });
  });
  assert.deepEqual(staticCost(mod.toJson()), [1, 1, 1, 2416]);
  // Vars: [s, d, latch].
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [5, 0, 0]);
});

test("until controls unchanged for gated and False conds", () => {
  // Gated and False paths must stay byte-identical to baseline.
  const never = buildModule(() => {
    const d = Var(0);
    until(false, () => {
      d.iadd(1);
    });
  });
  assert.deepEqual(staticCost(never.toJson()), [2, 3, 2, 4824]);
  assert.deepEqual(runSim(never.toJson(), 4).var_values, [4, 1]);

  const gated = (gInit: number) =>
    buildModule(() => {
      const s = Var(5);
      const d = Var(0);
      const g = Var(gInit);
      until(g.eq(1), () => {
        d.set(s);
      });
    });

  const closed = gated(0);
  assert.deepEqual(staticCost(closed.toJson()), [5, 3, 70, 12048]);
  // Loop 1 runs the body (g still 0), loop 2 trips.
  assert.deepEqual(runSim(closed.toJson(), 4).var_values, [5, 5, 0, 1]);

  const open = gated(1);
  assert.deepEqual(staticCost(open.toJson()), [5, 3, 70, 12048]);
  assert.deepEqual(runSim(open.toJson(), 4).var_values, [5, 0, 1, 0]);
});

test("until(true, d.iadd(1)) leaf act body never runs", () => {
  // A flat leaf act body has no body-when to ride along in once
  // the body is dropped: only the reset trip-when is emitted.
  const mod = buildModule(() => {
    const d = Var(0);
    until(true, () => {
      d.iadd(1);
    });
  });
  assert.deepEqual(staticCost(mod.toJson()), [1, 1, 1, 2412]);
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [0, 0]);
});

test("until(false, body) maps to a Never cond", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    until(false, () => {
      count.iadd(1);
    });
  });
  const untils = collectByKind(JSON.parse(mod.toJson()), "until");
  assert.equal(untils.length, 1);
  assert.deepEqual(untils[0].conds, [Never()]);
});

test("until(false, body) is identical to until(Never(), body)", () => {
  const a = buildModule(() => {
    const count = Var(0);
    until(false, () => {
      count.iadd(1);
    });
  });
  const b = buildModule(() => {
    const count = Var(0);
    until(Never(), () => {
      count.iadd(1);
    });
  });
  assert.equal(a.toJson(), b.toJson());
});

test("until(false, body) runs the body every loop (latch never trips)", () => {
  const mod = buildModule(() => {
    const count = Var(0);
    until(false, () => {
      count.iadd(1);
    });
  });
  // Vars: [count, latch]. The trip-when never fires, so the latch
  // stays 1 and the body-when runs every loop.
  assert.deepEqual(runSim(mod.toJson(), 1).var_values, [1, 1]);
  assert.deepEqual(runSim(mod.toJson(), 4).var_values, [4, 1]);
});

test("until with a normal cond still works", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.ge(2), () => {
      count.iadd(1);
    });
  });
  // Loop 1: frame 1 < 2, body runs. Loop 2: trip fires, body skipped.
  // Vars: [frame, count, latch].
  assert.deepEqual(runSim(mod.toJson(), 3).var_values, [3, 1, 0]);
});
