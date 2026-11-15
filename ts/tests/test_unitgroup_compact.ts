import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import test from "node:test";

import { runSim, REPO_ROOT } from "./helpers.ts";

import {
  DeathsX,
  EPD_CURRENT_PLAYER,
  Exactly,
  MemFieldHandle,
  Never,
  SetDeaths,
  SetTo,
  UnitGroup,
  Var,
  buildModule,
  defineMemView,
  lazyUnitGroup,
  when,
  withCP,
  type FieldSpec,
  type VarImpl,
} from "../index.ts";

interface StmtNode {
  kind: string;
  body?: StmtNode[];
  else_body?: StmtNode[];
  dst_var?: number;
}

const CP_REGISTER_SCHEMA = defineMemView("CurrentPlayerRegister", 4, {
  value: { offset: 0, size: 4 },
});
const CP_REGISTER_FIELD: FieldSpec = { offset: 0, size: 4 };

function readCpRegister(): VarImpl {
  return new MemFieldHandle(
    CP_REGISTER_SCHEMA,
    CP_REGISTER_FIELD,
    EPD_CURRENT_PLAYER,
    "value",
  ).read();
}

function setSlots(group: UnitGroup, values: Record<number, number>): void {
  for (const [index, value] of Object.entries(values)) {
    group.slots[Number(index)].set(value);
  }
}

function flattenStmts(stmts: readonly StmtNode[]): StmtNode[] {
  const out: StmtNode[] = [];
  for (const stmt of stmts) {
    out.push(stmt);
    out.push(...flattenStmts(stmt.body ?? []));
    out.push(...flattenStmts(stmt.else_body ?? []));
  }
  return out;
}

// ---------------------------------------------------------------------------
// Iteration semantics
// ---------------------------------------------------------------------------

test("compact for_each visits every live slot once in slot order", () => {
  const ids: Record<string, number> = {};
  let bodyCalls = 0;

  const mod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    const phase = Var(0);
    const count = Var(0);
    const total = Var(0);

    const body = (epd: VarImpl): void => {
      bodyCalls++;
      count.iadd(1);
      total.iadd(epd);
    };

    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
      g.add(3000);
    });
    when([phase.eq(1)], () => g.for_each(body));
    phase.iadd(1);
    ids.count = count.id;
    ids.total = total.id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.count], 3);
  assert.equal(report.var_values[ids.total], 6000);
  assert.equal(bodyCalls, 1, "body must run once at build time");
});

test("compact for_each on an empty group does not run and restores CP", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    const before = Var(0);
    const after = Var(0);
    const count = Var(0);
    withCP(1234, (cp) => {
      before.set(readCpRegister());
      g.for_each(() => count.iadd(1));
      after.set(readCpRegister());
      void cp;
    });
    ids.before = before.id;
    ids.after = after.id;
    ids.count = count.id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.count], 0);
  assert.equal(report.var_values[ids.before], 1234);
  assert.equal(report.var_values[ids.after], 1234);
});

test("compact for_each crosses page boundaries", () => {
  for (const capacity of [1, 63, 64, 65, 127, 128, 1000]) {
    const indexes = [...new Set([0, Math.min(63, capacity - 1), capacity - 1])]
      .sort((a, b) => a - b);
    const expected = indexes.reduce((sum, i) => sum + 1000 + i, 0);
    const ids: Record<string, number> = {};

    const mod = buildModule(() => {
      const g = new UnitGroup(capacity, { autoPrune: false, compact: true });
      setSlots(g, Object.fromEntries(indexes.map((i) => [i, 1000 + i])));
      const count = Var(0);
      const total = Var(0);
      g.for_each((epd) => {
        count.iadd(1);
        total.iadd(epd);
      });
      ids.count = count.id;
      ids.total = total.id;
    });

    const report = runSim(mod.toJson(), 1);
    assert.equal(report.var_values[ids.count], indexes.length, `capacity ${capacity}`);
    assert.equal(report.var_values[ids.total], expected, `capacity ${capacity}`);
  }
});

test("withCP inside the body does not break the compact cursor", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    setSlots(g, { 0: 10, 1: 20 });
    const count = Var(0);
    const total = Var(0);
    const seen = Var(0);

    g.for_each((epd) => {
      when([epd.eq(10)], () => {
        withCP(999, (cp) => {
          seen.set(cp.at(0).read());
        });
      });
      count.iadd(1);
      total.iadd(epd);
    });
    ids.count = count.id;
    ids.total = total.id;
    ids.seen = seen.id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.count], 2);
  assert.equal(report.var_values[ids.total], 30);
  assert.equal(report.var_values[ids.seen], 0);
});

test("nested compact loops restore each cursor", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const outer = new UnitGroup(2, { autoPrune: false, compact: true });
    const inner = new UnitGroup(2, { autoPrune: false, compact: true });
    setSlots(outer, { 0: 10, 1: 20 });
    setSlots(inner, { 0: 30, 1: 40 });
    const outerCount = Var(0);
    const innerCount = Var(0);
    const innerTotal = Var(0);

    outer.for_each(() => {
      outerCount.iadd(1);
      inner.for_each((epd) => {
        innerCount.iadd(1);
        innerTotal.iadd(epd);
      });
    });
    ids.outerCount = outerCount.id;
    ids.innerCount = innerCount.id;
    ids.innerTotal = innerTotal.id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.outerCount], 2);
  assert.equal(report.var_values[ids.innerCount], 4);
  assert.equal(report.var_values[ids.innerTotal], 140);
});

test("mutation during iteration keeps hole-based slot order", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    const phase = Var(0);
    const count = Var(0);
    const total = Var(0);

    const body = (epd: VarImpl): void => {
      count.iadd(1);
      total.iadd(epd);
      when([epd.eq(20)], () => g.remove(20));
    };

    when([phase.eq(0)], () => {
      g.add(10);
      g.add(20);
      g.add(30);
    });
    when([phase.eq(1)], () => g.for_each(body));
    phase.iadd(1);
    ids.count = count.id;
    ids.total = total.id;
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
    ids.s2 = g.slots[2].id;
  });

  const report = runSim(mod.toJson(), 2);
  assert.equal(report.var_values[ids.count], 3);
  assert.equal(report.var_values[ids.total], 60);
  assert.equal(report.var_values[ids.length], 2);
  assert.equal(report.var_values[ids.s0], 10);
  assert.equal(report.var_values[ids.s1], 0);
  assert.equal(report.var_values[ids.s2], 30);
});

test("add during iteration is visible to later slots", () => {
  const results: Record<string, Record<string, number>> = {};

  for (const compact of [false, true]) {
    const ids: Record<string, number> = {};

    const mod = buildModule(() => {
      const g = new UnitGroup(3, { autoPrune: false, compact });
      g.add(10);
      const count = Var(0);
      const total = Var(0);

      g.for_each((epd) => {
        count.iadd(1);
        total.iadd(epd);
        when([epd.eq(10)], () => g.add(20));
      });
      ids.count = count.id;
      ids.total = total.id;
      ids.length = g.length.id;
      ids.s0 = g.slots[0].id;
      ids.s1 = g.slots[1].id;
      ids.s2 = g.slots[2].id;
    });

    const report = runSim(mod.toJson(), 1);
    results[String(compact)] = Object.fromEntries(
      Object.entries(ids).map(([key, value]) => [key, report.var_values[value]]),
    );
  }

  assert.deepEqual(results["true"], results["false"]);
  assert.equal(results["true"].count, 2);
  assert.equal(results["true"].total, 30);
  assert.equal(results["true"].length, 2);
  assert.deepEqual(
    [results["true"].s0, results["true"].s1, results["true"].s2],
    [10, 20, 0],
  );
});

test("compact for_each passes a read-only snapshot", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    g.add(10);
    g.add(20);
    const count = Var(0);
    const total = Var(0);

    g.for_each((epd) => {
      count.iadd(1);
      total.iadd(epd);
      // Compact bodies get a scratch snapshot, not the slot Var.
      epd.set(0);
    });
    ids.count = count.id;
    ids.total = total.id;
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.count], 2);
  assert.equal(report.var_values[ids.total], 30);
  assert.equal(report.var_values[ids.length], 2);
  assert.deepEqual(
    [report.var_values[ids.s0], report.var_values[ids.s1]],
    [10, 20],
  );
});

// ---------------------------------------------------------------------------
// Mutation semantics
// ---------------------------------------------------------------------------

test("compact add/remove/clear preserve length, duplicates, and first-free fill", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    const phase = Var(0);
    const snap = Array.from({ length: 8 }, () => Var(0));

    const capture = (offset: number): void => {
      snap[offset].set(g.length);
      snap[offset + 1].set(g.slots[0]);
      snap[offset + 2].set(g.slots[1]);
      snap[offset + 3].set(g.slots[2]);
    };

    when([phase.eq(0)], () => {
      g.add(10);
      g.add(20);
      g.add(20);
      g.add(30);
    });
    when([phase.eq(1)], () => capture(0));
    when([phase.eq(2)], () => g.remove(20));
    when([phase.eq(3)], () => capture(4));
    when([phase.eq(4)], () => g.clear());
    phase.iadd(1);
    ids.length = snap[0].id;
    ids.s0 = snap[1].id;
    ids.s1 = snap[2].id;
    ids.s2 = snap[3].id;
    ids.rLength = snap[4].id;
    ids.rS0 = snap[5].id;
    ids.rS1 = snap[6].id;
    ids.rS2 = snap[7].id;
  });

  const report = runSim(mod.toJson(), 5);
  assert.equal(report.var_values[ids.length], 3);
  assert.deepEqual(
    [report.var_values[ids.s0], report.var_values[ids.s1], report.var_values[ids.s2]],
    [10, 20, 20],
  );
  assert.equal(report.var_values[ids.rLength], 1);
  assert.deepEqual(
    [
      report.var_values[ids.rS0],
      report.var_values[ids.rS1],
      report.var_values[ids.rS2],
    ],
    [10, 0, 0],
  );
});

test("compact add fills the first empty slot and is a no-op when full", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    g.add(10);
    g.add(20);
    g.add(30);
    g.remove(10);
    g.add(40);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.length], 2);
  assert.equal(report.var_values[ids.s0], 40);
  assert.equal(report.var_values[ids.s1], 20);
});

test("compact add and remove accept runtime Var EPDs", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    const epd = Var(4000);
    const phase = Var(0);
    when([phase.eq(0)], () => g.add(epd));
    when([phase.eq(1)], () => {
      g.add(5000);
      g.add(epd);
    });
    when([phase.eq(2)], () => g.remove(epd));
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
    ids.s2 = g.slots[2].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 1);
  assert.deepEqual(
    [
      report.var_values[ids.s0],
      report.var_values[ids.s1],
      report.var_values[ids.s2],
    ],
    [0, 5000, 0],
  );
});

test("compact remove clears every duplicate and compact clear zeroes storage", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    g.add(5);
    g.add(5);
    g.add(7);
    g.remove(5);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
    ids.s2 = g.slots[2].id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.length], 1);
  assert.deepEqual(
    [report.var_values[ids.s0], report.var_values[ids.s1], report.var_values[ids.s2]],
    [0, 0, 7],
  );

  const cleared: Record<string, number> = {};
  const clearMod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    g.add(1000);
    g.add(2000);
    g.clear();
    cleared.length = g.length.id;
    cleared.s0 = g.slots[0].id;
    cleared.s1 = g.slots[1].id;
  });
  const clearReport = runSim(clearMod.toJson(), 1);
  assert.equal(clearReport.var_values[cleared.length], 0);
  assert.equal(clearReport.var_values[cleared.s0], 0);
  assert.equal(clearReport.var_values[cleared.s1], 0);
});

// ---------------------------------------------------------------------------
// autoPrune
// ---------------------------------------------------------------------------

test("compact autoPrune checks the default sprite field", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    // 1000 is alive: sprite field EPD 1000 + 3 is non-zero.
    SetDeaths(1003, SetTo, 1, 0);
    const g = new UnitGroup(2, { compact: true });
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 1);
  assert.equal(report.var_values[ids.s0], 1000);
  assert.equal(report.var_values[ids.s1], 0);
});

test("compact autoPrune checks the unlimiter order field", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    SetDeaths(1000 + 0x13, SetTo, 0x0100, 0);
    const g = new UnitGroup(2, { compact: true, unlimiter: true });
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 1);
  assert.equal(report.var_values[ids.s0], 1000);
  assert.equal(report.var_values[ids.s1], 0);
});

test("compact autoPrune calls a custom deadCheck exactly once", () => {
  const ids: Record<string, number> = {};
  let checks = 0;

  const mod = buildModule(() => {
    const g = new UnitGroup(2, {
      compact: true,
      deadCheck: () => {
        checks++;
        return Never();
      },
    });
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 2);
  assert.equal(report.var_values[ids.s0], 1000);
  assert.equal(report.var_values[ids.s1], 2000);
  assert.equal(checks, 1);
});

test("compact autoPrune custom deadCheck can kill members", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, {
      compact: true,
      // Reads EPD 0, which is zero in the simulator.
      deadCheck: () => DeathsX(0, Exactly, 0, 0, 0xffffffff),
    });
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 0);
  assert.equal(report.var_values[ids.s0], 0);
  assert.equal(report.var_values[ids.s1], 0);
});

test("compact custom deadCheck HIR is live-gated", () => {
  // A predicate that emits HIR must be emitted inside the per-slot live gate,
  // matching the unroll. With no live slots its HIR never runs.
  const emptyIds: Record<string, number> = {};
  const emptyMod = buildModule(() => {
    const hits = Var(0);
    new UnitGroup(3, {
      compact: true,
      deadCheck: () => {
        hits.iadd(1);
        return Never();
      },
    });
    emptyIds.hits = hits.id;
  });
  assert.equal(runSim(emptyMod.toJson(), 2).var_values[emptyIds.hits], 0);

  // One live slot: the predicate HIR runs once per trigger loop for the live
  // slot, not once per storage slot.
  const liveIds: Record<string, number> = {};
  const liveMod = buildModule(() => {
    const hits = Var(0);
    const g = new UnitGroup(3, {
      compact: true,
      deadCheck: () => {
        hits.iadd(1);
        return Never();
      },
    });
    g.slots[0].set(1000);
    liveIds.hits = hits.id;
  });
  assert.equal(runSim(liveMod.toJson(), 3).var_values[liveIds.hits], 2);
});

test("compact autoPrune can be disabled", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(2, { autoPrune: false, compact: true });
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
  });

  const report = runSim(mod.toJson(), 3);
  assert.equal(report.var_values[ids.length], 2);
  assert.equal(report.var_values[ids.s0], 1000);
  assert.equal(report.var_values[ids.s1], 2000);
});

// ---------------------------------------------------------------------------
// Lazy handles and the default path
// ---------------------------------------------------------------------------

test("lazy compact flags are pure reads", () => {
  const ug = lazyUnitGroup(3, { compact: true });
  assert.equal(ug.capacity, 3);
  assert.equal(ug.compact, true);
  assert.equal(ug.autoPrune, true);
  assert.equal(ug.unlimiter, false);
  assert.equal(lazyUnitGroup(2).compact, false);

  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const count = Var(0);
    ug.for_each(() => count.iadd(1));
    ids.count = count.id;
  });
  assert.equal(runSim(mod.toJson(), 1).var_values[ids.count], 0);
});

test("lazy compact group exercises prune, add, remove, and clear", () => {
  const ids: Record<string, number> = {};
  const g = lazyUnitGroup(3, { compact: true });

  const mod = buildModule(() => {
    // Resolve at top level so the deferred autoPrune sweep is not emitted
    // inside a conditional branch.
    void g.length;
    // 1000 and 3000 stay alive through autoPrune; 2000 dies.
    SetDeaths(1003, SetTo, 1, 0);
    SetDeaths(3003, SetTo, 1, 0);
    const phase = Var(0);
    const seen = Var(0);
    const total = Var(0);

    const body = (epd: VarImpl): void => {
      seen.iadd(1);
      total.iadd(epd);
    };

    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    when([phase.eq(1)], () => g.for_each(body));
    when([phase.eq(2)], () => g.add(3000));
    when([phase.eq(3)], () => g.remove(1000));
    when([phase.eq(4)], () => g.for_each(body));
    when([phase.eq(5)], () => g.clear());
    phase.iadd(1);
    ids.seen = seen.id;
    ids.total = total.id;
    ids.length = g.length.id;
    ids.s0 = g.slots[0].id;
    ids.s1 = g.slots[1].id;
    ids.s2 = g.slots[2].id;
  });

  const report = runSim(mod.toJson(), 6);
  // autoPrune removed 2000 before the first for_each; remove(1000) left
  // 3000; clear zeroed the group.
  assert.equal(report.var_values[ids.seen], 2);
  assert.equal(report.var_values[ids.total], 4000);
  assert.equal(report.var_values[ids.length], 0);
  assert.deepEqual(
    [
      report.var_values[ids.s0],
      report.var_values[ids.s1],
      report.var_values[ids.s2],
    ],
    [0, 0, 0],
  );
});

test("default UnitGroup HIR is unchanged", () => {
  const mod = buildModule(() => {
    const g = new UnitGroup(2);
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
  });
  const digest = createHash("sha256").update(mod.toJson()).digest("hex");
  assert.equal(
    digest,
    "7522123dc49c7a0a3ad2f8f43dd49f04a868b36564bb53ccb9028970f69b21e0",
  );
});

test("compact body is emitted once at static level", () => {
  const ids: Record<string, number> = {};

  const compactMod = buildModule(() => {
    const g = new UnitGroup(4, { autoPrune: false, compact: true });
    setSlots(g, { 0: 1000, 1: 2000, 2: 3000, 3: 4000 });
    const out = Var(0);
    g.for_each((epd) => out.iadd(epd));
    ids.out = out.id;
  });
  const unrollMod = buildModule(() => {
    const g = new UnitGroup(4, { autoPrune: false });
    setSlots(g, { 0: 1000, 1: 2000, 2: 3000, 3: 4000 });
    const out = Var(0);
    g.for_each((epd) => out.iadd(epd));
  });

  const countTransfers = (hirJson: string, dst: number): number => {
    const parsed = JSON.parse(hirJson) as { module: { stmts: StmtNode[] } };
    return flattenStmts(parsed.module.stmts).filter(
      (stmt) => stmt.kind === "vartransfer" && stmt.dst_var === dst,
    ).length;
  };

  assert.equal(countTransfers(compactMod.toJson(), ids.out), 1);
  assert.ok(countTransfers(unrollMod.toJson(), ids.out) > 1);
});

// ---------------------------------------------------------------------------
// Cost shape
// ---------------------------------------------------------------------------

function costProgram(capacity: number, compact: boolean): ReturnType<typeof runSim> {
  const mod = buildModule(() => {
    const g = new UnitGroup(capacity, { autoPrune: false, compact });
    setSlots(g, { 0: 1000, 1: 2000, 2: 3000 });
    const out = Var(0);
    g.for_each((epd) => {
      out.iadd(epd);
      when([epd.eq(1000)], () => out.iadd(1));
      when([epd.ge(2000)], () => out.iadd(2));
      when([epd.le(3000)], () => out.iadd(3));
    });
  });
  return runSim(mod.toJson(), 1);
}

test("compact static cost is independent of capacity", () => {
  const small = costProgram(64, true).static_cost;
  const big = costProgram(1000, true).static_cost;
  const unroll = costProgram(64, false).static_cost;

  assert.ok(big.triggers - small.triggers <= 16);
  assert.ok(big.total_bytes - small.total_bytes <= 16 * 2408);
  assert.ok(big.triggers < unroll.triggers);
});

test("compact executed cost stays bounded with body once per live entry", () => {
  const ids: Record<string, number> = {};

  const mod = buildModule(() => {
    const g = new UnitGroup(4, { autoPrune: false, compact: true });
    setSlots(g, { 0: 1000, 1: 2000, 2: 3000, 3: 4000 });
    const count = Var(0);
    const total = Var(0);
    g.for_each((epd) => {
      count.iadd(1);
      total.iadd(epd);
    });
    ids.count = count.id;
    ids.total = total.id;
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.count], 4);
  assert.equal(report.var_values[ids.total], 10000);
  assert.ok(report.executed_cost.triggers > 0);
  assert.equal(report.self_targeted_invariant_holds, true);
});

test("compact whole-group cost at capacity 1000", () => {
  const run = (mutate: boolean): Record<string, number> => {
    const mod = buildModule(() => {
      const g = new UnitGroup(1000, { compact: true });
      g.for_each(() => {});
      if (mutate) {
        g.add(1000);
        g.remove(1000);
        g.clear();
      }
    });
    const report = runSim(mod.toJson(), 1);
    return {
      triggers: report.static_cost.triggers,
      bytes: report.static_cost.total_bytes,
      executed: report.executed_cost.triggers,
    };
  };

  // #97: the compact sweep's two full-dword reads share one module reader,
  // and each single-site body is inlined by the optimizer: 176T -> 144T
  // static (-32T; the second inline 33T read used to shrink only to a 5T call
  // sequence). Executed drops 18076 -> 16081 because the per-member inline
  // read chains were replaced by the inlined body.
  assert.deepEqual(run(false), { triggers: 144, bytes: 346776, executed: 16081 });
  assert.deepEqual(run(true), { triggers: 175, bytes: 421452, executed: 38201 });
});

// ---------------------------------------------------------------------------
// Py/TS parity
// ---------------------------------------------------------------------------

test("compact UnitGroup HIR is byte-identical with Python", () => {
  const tsMod = buildModule(() => {
    const g = new UnitGroup(3, { autoPrune: false, compact: true });
    const phase = Var(0);
    const out = Var(0);

    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
      g.add(2000);
    });
    when([phase.eq(1)], () => {
      g.for_each((epd) => out.iadd(epd));
      g.remove(1000);
      g.add(3000);
    });
    when([phase.eq(2)], () => g.clear());

    const pruned = new UnitGroup(2, { compact: true });
    pruned.add(4000);

    const unlimiter = new UnitGroup(2, { compact: true, unlimiter: true });
    unlimiter.add(5000);

    phase.iadd(1);
  });

  const pyScript = `
from py import UnitGroup, Var, build_module, when

def prog():
    g = UnitGroup(3, autoPrune=False, compact=True)
    phase = Var(0)
    out = Var(0)

    when([phase.eq(0)], lambda: (g.add(1000), g.add(2000), g.add(2000)))
    when(
        [phase.eq(1)],
        lambda: (
            g.for_each(lambda epd: out.iadd(epd)),
            g.remove(1000),
            g.add(3000),
        ),
    )
    when([phase.eq(2)], lambda: g.clear())

    pruned = UnitGroup(2, compact=True)
    pruned.add(4000)

    unlimiter = UnitGroup(2, compact=True, unlimiter=True)
    unlimiter.add(5000)

    phase.iadd(1)

print(build_module(prog).to_json(), end="")
`;

  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsMod.toJson(), pyHir);
});
