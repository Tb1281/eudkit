/**
 * T-ABS-VIEW-POOL: raw bit helpers, flag vocabularies, and `CUnitView`.
 *
 * Mirrors `py/tests/test_view_ext.py`; the native simulator proof lives in
 * `crates/eudkit-cli/tests/view_pool_runtime.rs`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { EPD, Var, buildModule, when } from "../index.ts";
import {
  AvailabilityFlags,
  CUnitView,
  EudslError,
  GROUP_FLAGS_TARGET,
  GroupFlags,
  PathingFlags,
  StatusFlags,
  readBit,
  readBitVar,
  readCunitPointer,
  splitDword,
  splitWord,
  writeBit,
  writeMask,
  POOL_NULL as VIEW_POOL_NULL,
} from "../view.ts";
import { POOL_NULL } from "../queue.ts";
import { flattenActs, flattenStmts, runSim, runStatic, type HirStmtNode } from "./helpers.ts";

const BASE = EPD(0x59cca8);
const ADDR = 0x58dc4c;
const LOCATION_EPD = EPD(0x58dc4c);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = { [key: string]: any };
type Stmt = HirStmtNode & { [key: string]: any };

function moduleOf(fn: () => void): Json {
  return JSON.parse(buildModule(fn).toJson()).module as Json;
}

function stmtsOf(fn: () => void): Stmt[] {
  return flattenStmts(moduleOf(fn).stmts as Stmt[]) as Stmt[];
}

function actsOf(fn: () => void): Json[] {
  return flattenActs(moduleOf(fn).stmts as Stmt[]) as Json[];
}

function codeOf(err: unknown): string {
  return err instanceof EudslError ? err.code : "";
}

test("splitDword constant folds to two constant Vars", () => {
  let hi: any;
  let lo: any;
  const module = moduleOf(() => {
    [hi, lo] = splitDword(0x12345678);
  });
  assert.equal(hi.init, 0x1234);
  assert.equal(lo.init, 0x5678);
  assert.deepEqual(module.stmts, []);
});

test("splitWord splits a word into two bytes", () => {
  let hi: any;
  let lo: any;
  moduleOf(() => {
    [hi, lo] = splitWord(0x1234);
  });
  assert.equal(hi.init, 0x12);
  assert.equal(lo.init, 0x34);
});

test("runtime split stays under the ceiling", () => {
  for (const split of [splitDword, splitWord]) {
    const stmts = stmtsOf(() => {
      split(Var(0));
    });
    assert.ok(stmts.length > 0);
    assert.ok(stmts.length <= 48);
  }
});

test("split rejects bool", () => {
  for (const split of [splitDword, splitWord]) {
    assert.throws(
      () => moduleOf(() => split(true as unknown as number)),
      (err) => codeOf(err) === "EUDSL-TYPE",
    );
  }
});

test("readBit is one condition and zero acts", () => {
  const module = moduleOf(() => {
    when(readBit(ADDR, 0), () => {});
  });
  assert.equal(module.stmts.length, 1);
  const cond = module.stmts[0].conds[0];
  assert.equal(cond.condtype, 15);
  assert.equal(cond.player, EPD(ADDR));
  assert.equal(cond.amount, 1);
  assert.equal(cond.locid, 1);
  assert.deepEqual(module.stmts[0].body, []);
});

test("readBit bit 31 uses the top mask", () => {
  const module = moduleOf(() => {
    when(readBit(ADDR, 31), () => {});
  });
  const cond = module.stmts[0].conds[0];
  assert.equal(cond.locid, 0x80000000);
  assert.equal(cond.amount, 0x80000000);
});

test("readBit validates address and index", () => {
  assert.throws(
    () => moduleOf(() => readBit(ADDR + 1, 0)),
    (err) => codeOf(err) === "EUDSL-BOUNDS",
  );
  for (const bit of [32, -1]) {
    assert.throws(
      () => moduleOf(() => readBit(ADDR, bit)),
      (err) => codeOf(err) === "EUDSL-RANGE",
    );
  }
  assert.throws(
    () => moduleOf(() => readBit(ADDR, true as unknown as number)),
    (err) => codeOf(err) === "EUDSL-TYPE",
  );
});

test("readBitVar is two triggers", () => {
  const module = moduleOf(() => {
    readBitVar(ADDR, 4);
  });
  assert.equal(module.stmts.length, 2);
  const cond = module.stmts[1].conds[0];
  assert.equal(cond.locid, 0x10);
  assert.equal(cond.player, EPD(ADDR));
});

test("writeBit emits one masked act", () => {
  let acts = actsOf(() => writeBit(ADDR, 4, true));
  assert.equal(acts.length, 1);
  assert.equal(acts[0].dest, EPD(ADDR));
  assert.equal(acts[0].value, 0x10);
  assert.equal(acts[0].mask, 0x10);
  assert.equal(acts[0].modifier, 7);

  acts = actsOf(() => writeBit(ADDR, 4, false));
  assert.equal(acts[0].value, 0);
});

test("writeBit rejects non-bool", () => {
  assert.throws(
    () => moduleOf(() => writeBit(ADDR, 4, 1 as unknown as boolean)),
    (err) => codeOf(err) === "EUDSL-TYPE",
  );
});

test("writeMask validates mask and value", () => {
  const acts = actsOf(() => writeMask(ADDR, 0x1200, 0xff00));
  assert.equal(acts[0].value, 0x1200);
  assert.equal(acts[0].mask, 0xff00);

  assert.throws(
    () => moduleOf(() => writeMask(ADDR, 0x12, 0xff00)),
    (err) => codeOf(err) === "EUDSL-BOUNDS",
  );
  assert.throws(
    () => moduleOf(() => writeMask(ADDR, 0, 0)),
    (err) => codeOf(err) === "EUDSL-RANGE",
  );
});

test("status flags are distinct single bits", () => {
  const values = Object.values(StatusFlags);
  assert.equal(values.length, 32);
  assert.equal(new Set(values).size, 32);
  for (const value of values) {
    assert.equal(value & (value - 1), 0);
  }
});

test("pathing and unit-type flag tables match Python", () => {
  assert.deepEqual(PathingFlags, {
    HasCollision: 0x01,
    IsStacked: 0x02,
    Decollide: 0x04,
  });
  assert.deepEqual(GroupFlags, {
    Zerg: 0x01,
    Terran: 0x02,
    Protoss: 0x04,
    Men: 0x08,
    Building: 0x10,
    Factory: 0x20,
    Independent: 0x40,
    Neutral: 0x80,
  });
  assert.deepEqual(AvailabilityFlags, {
    NonNeutral: 0x001,
    UnitListing: 0x002,
    MissionBriefing: 0x004,
    PlayerSettings: 0x008,
    AllRaces: 0x010,
    SetDoodadState: 0x020,
    NonLocationTriggers: 0x040,
    UnitHeroSettings: 0x080,
    LocationTriggers: 0x100,
    BroodWarOnly: 0x200,
  });
  assert.equal(GROUP_FLAGS_TARGET, "unit-type");
});

test("CUnitView resolves mem and fields", () => {
  let view: CUnitView;
  let owner: any;
  let status: any;
  moduleOf(() => {
    view = new CUnitView(BASE);
    owner = view.field("owner");
    status = view.field("statusFlags");
  });
  assert.equal(view!.epd, BASE);
  assert.equal(view!.mem.schema.name, "CUnit");
  assert.equal(owner.epd, BASE + 19);
  assert.equal(status.epd, BASE + 55);
});

test("CUnitView unknown field", () => {
  assert.throws(
    () => moduleOf(() => new CUnitView(BASE).field("nope")),
    (err) => codeOf(err) === "EUDSL-UNKNOWN-FIELD",
  );
});

test("giveTo writes only the owner byte", () => {
  const acts = actsOf(() => new CUnitView(BASE).giveTo(3));
  assert.equal(acts.length, 1);
  assert.equal(acts[0].dest, BASE + 19);
  assert.equal(acts[0].value, 3);
  assert.equal(acts[0].mask, 0xff);
});

test("die and remove act order", () => {
  const die = actsOf(() => new CUnitView(BASE).die());
  assert.deepEqual(
    [die[0].dest, die[0].value, die[0].mask],
    [BASE + 19, 0, 0xff00],
  );

  const remove = actsOf(() => new CUnitView(BASE).remove());
  assert.equal(remove.length, 2);
  assert.deepEqual(
    [remove[0].dest, remove[0].value, remove[0].mask],
    [BASE + 36, 0x04000000, 0xff000000],
  );
  assert.deepEqual(
    [remove[1].dest, remove[1].value, remove[1].mask],
    [BASE + 19, 0, 0xff00],
  );
});

test("setInvincible sets and clears the status bit", () => {
  let acts = actsOf(() => new CUnitView(BASE).setInvincible());
  assert.deepEqual(
    [acts[0].dest, acts[0].value, acts[0].mask],
    [BASE + 55, 0x04000000, 0x04000000],
  );
  acts = actsOf(() => new CUnitView(BASE).setInvincible(false));
  assert.equal(acts[0].value, 0);
});

test("isBurrowed reads bit four of statusFlags", () => {
  const module = moduleOf(() => {
    new CUnitView(BASE).isBurrowed();
  });
  assert.equal(module.stmts.length, 2);
  const cond = module.stmts[1].conds[0];
  assert.equal(cond.player, BASE + 55);
  assert.equal(cond.locid, 0x10);
});

test("links use the reader at the expected dword", () => {
  assert.equal(
    buildModule(() => {
      new CUnitView(BASE).nextUnit();
    }).toJson(),
    buildModule(() => {
      readCunitPointer(BASE + 1);
    }).toJson(),
  );
  assert.equal(
    buildModule(() => {
      new CUnitView(BASE).prevUnit();
    }).toJson(),
    buildModule(() => {
      readCunitPointer(BASE);
    }).toJson(),
  );
});

test("setColor writes the player lane through the sprite dword", () => {
  const acts = actsOf(() => new CUnitView(BASE).setColor(5));
  const masked = acts.filter((act) => act.mask === 0x00ff0000);
  assert.equal(masked.length, 1);
  assert.equal(masked[0].value, 5 << 16);
});

test("validation guards", () => {
  const cases: [() => void, string][] = [
    [() => new CUnitView(BASE).giveTo(12), "EUDSL-RANGE"],
    [() => new CUnitView(BASE).giveTo(true as unknown as number), "EUDSL-TYPE"],
    [() => new CUnitView(BASE).setColor(-1), "EUDSL-RANGE"],
    [() => new CUnitView(BASE).setLocation(256), "EUDSL-RANGE"],
    [() => new CUnitView(BASE).setLocation(true as unknown as number), "EUDSL-TYPE"],
    [() => new CUnitView(true as unknown as number), "EUDSL-TYPE"],
  ];
  for (const [fn, code] of cases) {
    assert.throws(() => moduleOf(fn), (err) => codeOf(err) === code);
  }
});

test("runtime masked write uses a pointer and varstore", () => {
  const module = moduleOf(() => {
    new CUnitView(Var(BASE)).giveTo(2);
  });
  const statements = flattenStmts(module.stmts as Stmt[]) as Json[];
  const varstores = statements.filter((stmt) => stmt.kind === "varstore");
  assert.ok(varstores.length > 0);
  assert.equal(varstores[0].dst_offset, 344);
  const masked = actsOf(() => {
    new CUnitView(Var(BASE)).giveTo(2);
  }).filter((act) => act.mask === 0xff);
  assert.equal(masked.length, 1);
  assert.equal(masked[0].value, 2);
});

test("runtime isBurrowed binds CP and stays under the ceiling", () => {
  const module = moduleOf(() => {
    new CUnitView(Var(BASE)).isBurrowed();
  });
  const conditions = (flattenStmts(module.stmts as Stmt[]) as Json[]).flatMap(
    (stmt) => (stmt.conds ?? []) as Json[],
  );
  assert.ok(conditions.some((cond) => cond.player === 13));

  const hir = buildModule(() => {
    new CUnitView(Var(BASE)).isBurrowed();
  }).toJson();
  assert.ok(runStatic(hir).static_cost.triggers <= 80);
  assert.ok(runSim(hir).executed_cost.triggers <= 70);
});

test("runtime setLocation reads location record and fills pos", () => {
  const module = moduleOf(() => {
    new CUnitView(Var(BASE)).setLocation(7);
  });
  const functionConds = (module.funcs as Json[]).flatMap((func) =>
    (flattenStmts(func.body as Stmt[]) as Json[]).flatMap(
      (stmt) => (stmt.conds ?? []) as Json[],
    ),
  );
  const players = new Set(functionConds.map((cond) => cond.player));
  assert.ok(players.has(LOCATION_EPD + 7 * 5));
  assert.ok(players.has(LOCATION_EPD + 7 * 5 + 1));
  assert.equal(
    (flattenStmts(module.stmts as Stmt[]) as Json[]).filter(
      (stmt) => stmt.kind === "while",
    ).length,
    1,
  );
});

test("runtime costs are under the ceilings", () => {
  const sites: [() => void, number][] = [
    [() => new CUnitView(Var(BASE)).giveTo(2), 40],
    [() => new CUnitView(Var(BASE)).die(), 40],
    [() => new CUnitView(Var(BASE)).setInvincible(), 40],
    [() => new CUnitView(Var(BASE)).setLocation(7), 180],
  ];
  for (const [fn, ceiling] of sites) {
    const hir = buildModule(fn).toJson();
    assert.ok(runStatic(hir).static_cost.triggers <= ceiling);
    assert.ok(runSim(hir).executed_cost.triggers <= ceiling);
  }
});

test("constant reader costs stay pinned", () => {
  assert.equal(
    stmtsOf(() => {
      new CUnitView(BASE).nextUnit();
    }).length,
    20,
  );
  assert.equal(
    stmtsOf(() => {
      new CUnitView(BASE).prevUnit();
    }).length,
    20,
  );
  assert.equal(
    stmtsOf(() => {
      new CUnitView(BASE).spriteEpd();
    }).length,
    32,
  );
  assert.equal(
    stmtsOf(() => {
      new CUnitView(BASE).isBurrowed();
    }).length,
    2,
  );
});

test("POOL_NULL is shared with queue", () => {
  assert.equal(POOL_NULL, 0xffffffff);
  assert.equal(POOL_NULL, VIEW_POOL_NULL);
});
