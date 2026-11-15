/**
 * Contracts for the SC/BW tech and upgrade state tables (`T-ABS-SCDATA`).
 *
 * Mirrors `py/tests/test_scstate.py`. The slot arithmetic is checked against
 * an independent reference that spells out the §4.1 formulas, the constant
 * fast path is pinned to one masked `DeathsX`/`SetDeathsX`, and the runtime
 * shape is pinned to a `withEpd` binding plus the four-arm upgrade dispatch.
 * A source grep keeps the absorbed cache machinery and runtime `Math.*`
 * helpers out of the module.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  CP_ADDR,
  EPD,
  EudslError,
  SetDeaths,
  SetTo,
  Var,
  asReadonly,
  buildModule,
  when,
  type HirCond,
  type VarImpl,
} from "../index.ts";
import {
  MAX_PLAYER_ID,
  TECH_COUNT,
  TECH_SC_COUNT,
  TECH_STATE_BW_BASE,
  TECH_STATE_BW_STRIDE,
  TECH_STATE_SC_BASE,
  TECH_STATE_SC_STRIDE,
  UPGRADE_COUNT,
  UPGRADE_SC_COUNT,
  UPGRADE_STATE_BW_BASE,
  UPGRADE_STATE_BW_STRIDE,
  UPGRADE_STATE_SC_BASE,
  UPGRADE_STATE_SC_STRIDE,
  setTechResearched,
  setTechResearched as set_tech_researched,
  set_upgrade_level,
  setUpgradeLevel,
  techResearched,
  techResearched as tech_researched,
  techResearchedVar,
  techResearchedVar as tech_researched_var,
  techStateSlot,
  tech_state_slot,
  upgradeLevel,
  upgradeStateSlot,
  upgradeStateSlot as upgrade_state_slot,
  upgrade_level,
} from "../scstate.ts";
import { REPO_ROOT, flattenStmts as flattenAll, runSim } from "./helpers.ts";

const SCSTATE_SOURCE = path.join(REPO_ROOT, "ts", "scstate.ts");
const PLAYERS = [0, 1, 7, 11];

type Module = ReturnType<typeof buildModule>;
type Stmt = Record<string, unknown> & {
  kind: string;
  body?: Stmt[];
  else_body?: Stmt[];
  acts?: Record<string, unknown>[];
  conds?: Record<string, unknown>[];
};

function stmtsOf(module: Module): Stmt[] {
  return module.toHir().module.stmts as unknown as Stmt[];
}

function techAddr(tech: number, player: number): number {
  if (tech < TECH_SC_COUNT) {
    return TECH_STATE_SC_BASE + tech + TECH_STATE_SC_STRIDE * player;
  }
  return (
    TECH_STATE_BW_BASE +
    (tech - TECH_SC_COUNT) +
    TECH_STATE_BW_STRIDE * player
  );
}

function upgradeAddr(upgrade: number, player: number): number {
  if (upgrade < UPGRADE_SC_COUNT) {
    return (
      UPGRADE_STATE_SC_BASE +
      upgrade +
      UPGRADE_STATE_SC_STRIDE * player
    );
  }
  return (
    UPGRADE_STATE_BW_BASE +
    (upgrade - UPGRADE_SC_COUNT) +
    UPGRADE_STATE_BW_STRIDE * player
  );
}

function slot(addr: number): [number, number] {
  return [EPD(addr), 8 * (addr & 3)];
}

function whenNodes(stmts: Stmt[]): Stmt[] {
  return stmts.filter((stmt) => stmt.kind === "when");
}

/** Every action node, including bare `act` nodes inside a `when` body. */
function actNodes(stmts: Stmt[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const stmt of stmts) {
    if (stmt.kind === "act") out.push(stmt);
    out.push(...(stmt.acts ?? []));
    out.push(...actNodes(stmt.body ?? []));
    out.push(...actNodes(stmt.else_body ?? []));
  }
  return out;
}

function cpStores(stmts: Stmt[]): number {
  return stmts.filter(
    (stmt) => stmt.kind === "varstore" && stmt.dst_addr === CP_ADDR,
  ).length;
}

function errorOf(fn: () => void): EudslError {
  try {
    buildModule(fn);
  } catch (error) {
    assert.ok(error instanceof EudslError, `unexpected error: ${String(error)}`);
    return error;
  }
  assert.fail("expected an EudslError");
}

test("tech slots match the independent reference", () => {
  for (let tech = 0; tech < TECH_COUNT; tech += 1) {
    for (const player of PLAYERS) {
      assert.deepEqual(
        techStateSlot(tech, player),
        slot(techAddr(tech, player)),
        `tech=${tech} player=${player}`,
      );
    }
  }
});

test("tech slot spot checks the SC/BW split", () => {
  // `tech 0 / p1` is `0x58CF44 + 0 + 24`; the ticket's `+ 1` names the
  // neighbouring entry, covered by the `tech 1` spot check.
  assert.deepEqual(techStateSlot(0, 0), slot(0x58cf44));
  assert.deepEqual(techStateSlot(0, 1), slot(0x58cf44 + 24));
  assert.deepEqual(techStateSlot(1, 1), slot(0x58cf5d));
  assert.equal(techStateSlot(1, 1)[1], 8);
  assert.deepEqual(techStateSlot(24, 1), slot(0x58f154));
  assert.equal(techStateSlot(24, 1)[1], 0);
});

test("upgrade slots match the independent reference", () => {
  for (let upgrade = 0; upgrade < UPGRADE_COUNT; upgrade += 1) {
    for (const player of PLAYERS) {
      assert.deepEqual(
        upgradeStateSlot(upgrade, player),
        slot(upgradeAddr(upgrade, player)),
        `upgrade=${upgrade} player=${player}`,
      );
    }
  }
});

test("upgrade lanes follow the two- and three-mod-four strides", () => {
  for (let upgrade = 0; upgrade < UPGRADE_SC_COUNT; upgrade += 1) {
    for (const player of PLAYERS) {
      assert.equal(
        upgradeStateSlot(upgrade, player)[1],
        8 * ((upgrade + 2 * player) & 3),
        `sc upgrade=${upgrade} player=${player}`,
      );
    }
  }
  for (let upgrade = UPGRADE_SC_COUNT; upgrade < UPGRADE_COUNT; upgrade += 1) {
    const offset = upgrade - UPGRADE_SC_COUNT;
    for (const player of PLAYERS) {
      assert.equal(
        upgradeStateSlot(upgrade, player)[1],
        8 * ((offset + 3 * player) & 3),
        `bw upgrade=${upgrade} player=${player}`,
      );
    }
  }
});

test("slot helpers accept plain player ids only", () => {
  assert.equal(
    errorOf(() => techStateSlot(0, Var(0) as unknown as number)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => upgradeStateSlot(0, Var(0) as unknown as number)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => techStateSlot(0, 1.5)).code,
    "EUDSL-TYPE",
  );
});

test("techResearched is one condition with no actions", () => {
  const module = buildModule(() => {
    when(techResearched(5, 3), () => {});
  });
  const stmts = stmtsOf(module);
  const whens = whenNodes(stmts);
  assert.equal(whens.length, 1);
  const conds = whens[0].conds ?? [];
  assert.equal(conds.length, 1);
  const [epd, shift] = techStateSlot(5, 3);
  const cond = conds[0];
  assert.equal(cond.condtype, 15);
  assert.equal(cond.player, epd);
  assert.equal(cond.comparison, 0); // AtLeast
  assert.equal(cond.amount, 1);
  assert.equal(cond.locid, (0xff << shift) >>> 0);
  assert.ok(!whens[0].body || whens[0].body.length === 0);
  assert.deepEqual(actNodes(stmts), []);
});

test("setTechResearched masks exactly one lane", () => {
  for (const [on, value] of [
    [true, 1],
    [false, 0],
  ] as Array<[boolean, number]>) {
    const module = buildModule(() => {
      setTechResearched(5, 3, on);
    });
    const stmts = stmtsOf(module);
    assert.deepEqual(
      stmts.map((stmt) => stmt.kind),
      ["trigger"],
    );
    const acts = stmts[0].acts ?? [];
    assert.equal(acts.length, 1);
    const [epd, shift] = techStateSlot(5, 3);
    const act = acts[0];
    assert.equal(act.acttype, 45);
    assert.equal(act.dest, epd);
    assert.equal(act.value, value << shift);
    assert.equal(act.mask, (0xff << shift) >>> 0);
    assert.equal(act.modifier, SetTo);
  }
});

test("upgradeLevel uses the BW base and reads the byte window", () => {
  const module = buildModule(() => {
    const out = Var(0);
    out.set(upgradeLevel(46, 0));
  });
  const [epd, shift] = upgradeStateSlot(46, 0);
  assert.equal(epd, EPD(UPGRADE_STATE_BW_BASE));
  const stmts = stmtsOf(module);
  const triggers = stmts.filter((stmt) => stmt.kind === "trigger");
  // One accumulator reset plus one trigger per bit in the byte window.
  assert.equal(triggers.length, 9);
  const masks = new Set(
    triggers.flatMap((trigger) =>
      (trigger.conds ?? []).map((cond) => cond.locid as number),
    ),
  );
  const expected = new Set(
    Array.from({ length: 8 }, (_, bit) => 1 << (shift + bit)),
  );
  assert.deepEqual(masks, expected);
});

test("setUpgradeLevel shifts the value into the lane", () => {
  const module = buildModule(() => {
    setUpgradeLevel(46, 0, 3);
  });
  const stmts = stmtsOf(module);
  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    ["trigger"],
  );
  const acts = stmts[0].acts ?? [];
  assert.equal(acts.length, 1);
  const [epd, shift] = upgradeStateSlot(46, 0);
  assert.equal(acts[0].dest, epd);
  assert.equal(acts[0].value, 3 << shift);
  assert.equal(acts[0].mask, (0xff << shift) >>> 0);
  assert.equal(acts[0].modifier, SetTo);
});

test("range errors name the documented domains", () => {
  const factories: Array<() => void> = [
    () => techStateSlot(44, 0),
    () => techResearched(44, 0),
    () => techResearchedVar(44, 0),
    () => setTechResearched(44, 0, true),
    () => upgradeStateSlot(61, 0),
    () => upgradeLevel(61, 0),
    () => setUpgradeLevel(61, 0, 0),
    () => techStateSlot(0, 12),
    () => upgradeStateSlot(0, 12),
    () => techResearched(0, 12),
    () => techResearchedVar(0, 12),
    () => upgradeLevel(0, 12),
    () => setTechResearched(0, 12, true),
    () => setUpgradeLevel(0, 12, 0),
    () => setUpgradeLevel(0, 0, 256),
    () => setUpgradeLevel(0, 0, -1),
  ];
  for (const factory of factories) {
    assert.equal(errorOf(factory).code, "EUDSL-RANGE");
  }
});

test("type errors cover bools, floats, and wrong operands", () => {
  assert.equal(
    errorOf(() => techResearched(true as unknown as number, 0)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => techResearchedVar(0, 1.5)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => setTechResearched(0, 0, 1 as unknown as boolean)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => setUpgradeLevel(0, 0, true as unknown as number)).code,
    "EUDSL-TYPE",
  );
  assert.equal(
    errorOf(() => techResearchedVar(0, "p" as unknown as VarImpl)).code,
    "EUDSL-TYPE",
  );
});

test("techResearched rejects a runtime player and names the var form", () => {
  const error = errorOf(() =>
    techResearched(1, Var(0) as unknown as number),
  );
  assert.equal(error.code, "EUDSL-TYPE");
  assert.match(error.message, /techResearchedVar/);
});

test("rejections emit no partial statements", () => {
  // The runtime operand is allocated in both programs: the assertion is
  // about emitted statements, not the variable table.
  const baseline = () =>
    buildModule(() => {
      const value = Var(0);
      value.set(1);
      const player = Var(2);
      void player;
    });
  const factories: Array<(player: VarImpl) => void> = [
    () => setUpgradeLevel(0, 0, 256),
    () => setTechResearched(44, 0, true),
    (player) => techResearched(1, player as unknown as number),
    () => upgradeLevel(0, 12),
  ];
  const want = baseline().toJson();
  for (const factory of factories) {
    const failing = () =>
      buildModule(() => {
        const value = Var(0);
        value.set(1);
        const player = Var(2);
        try {
          factory(player);
        } catch (error) {
          assert.ok(error instanceof EudslError);
        }
      });
    assert.equal(failing().toJson(), want);
  }
});

test("tech runtime read binds the EPD and reads the byte window", () => {
  const module = buildModule(() => {
    const player = Var(0);
    techResearchedVar(0, player);
  });
  const stmts = stmtsOf(module);
  assert.equal(cpStores(stmts), 2, "one bind and one restore");
  const shift = 8 * (TECH_STATE_SC_BASE & 3);
  const masks = new Set(
    stmts.flatMap((stmt) =>
      (stmt.conds ?? [])
        .filter((cond) => cond.condtype === 15 && cond.locid)
        .map((cond) => cond.locid as number),
    ),
  );
  assert.deepEqual(
    masks,
    new Set(Array.from({ length: 8 }, (_, bit) => 1 << (shift + bit))),
  );
  const whens = whenNodes(stmts);
  assert.equal(whens.length, 1, "one 0/1 normalisation");
  assert.equal(whens[0].conds?.[0].comparison, 0); // AtLeast
  assert.equal(whens[0].conds?.[0].amount, 1);
});

test("tech runtime write is one masked action inside withEpd", () => {
  const module = buildModule(() => {
    const player = Var(0);
    setTechResearched(0, player, true);
  });
  const stmts = stmtsOf(module);
  assert.equal(cpStores(stmts), 2, "one bind and one restore");
  const shift = 8 * (TECH_STATE_SC_BASE & 3);
  const masked = actNodes(stmts).filter(
    (act) => act.acttype === 45 && act.dest === 13,
  );
  assert.equal(masked.length, 1);
  assert.equal(masked[0].mask, (0xff << shift) >>> 0);
  assert.equal(masked[0].value, 1 << shift);
});

test("upgrade runtime read dispatches four lane arms", () => {
  const module = buildModule(() => {
    const player = Var(0);
    upgradeLevel(0, player);
  });
  const stmts = stmtsOf(module);
  assert.equal(cpStores(stmts), 2, "one bind and one restore");
  const whens = whenNodes(stmts);
  assert.equal(whens.length, 4);
  assert.deepEqual(
    whens.map((arm) => arm.conds?.[0].amount),
    [0, 1, 2, 3],
  );
  whens.forEach((arm, k) => {
    assert.equal(arm.conds?.[0].comparison, 10); // Exactly
    const masks = new Set(
      flattenAll(arm.body ?? [])
        .flatMap((stmt) => (stmt.conds ?? []).map((cond) => cond.locid))
        .filter((locid) => locid),
    );
    assert.deepEqual(
      masks,
      new Set(
        Array.from({ length: 8 }, (_, bit) => (1 << (8 * k + bit)) >>> 0),
      ),
      `lane ${k} byte window`,
    );
  });
});

test("upgrade runtime write dispatches four lane arms", () => {
  const module = buildModule(() => {
    const player = Var(0);
    setUpgradeLevel(0, player, 7);
  });
  const stmts = stmtsOf(module);
  assert.equal(cpStores(stmts), 2, "one bind and one restore");
  const whens = whenNodes(stmts);
  assert.equal(whens.length, 4);
  whens.forEach((arm, k) => {
    const body = arm.body ?? [];
    assert.deepEqual(
      body.map((node) => node.kind),
      ["act"],
    );
    const act = body[0];
    assert.equal(act.acttype, 45);
    assert.equal(act.dest, 13); // CurrentPlayer
    assert.equal(act.mask, (0xff << (8 * k)) >>> 0);
    assert.equal(act.value, (7 << (8 * k)) >>> 0);
  });
});

test("ReadonlyVar and Expr players are accepted", () => {
  const module = buildModule(() => {
    const player = Var(3);
    techResearchedVar(0, asReadonly(player));
    techResearchedVar(0, player.add(1));
  });
  assert.ok(module);
});

test("no cache, VProc, unmasked-memory, or Math tokens", () => {
  const source = fs.readFileSync(SCSTATE_SOURCE, "utf8");
  const forbidden = [
    "SetNextPtr",
    "Switch",
    "Forward",
    "VProc",
    "SetDest",
    "_get_stride_cache",
    "EUDByteReader",
    "EUDLightBool",
    "SetMemory(",
    "Math.random",
    "Math.pow",
  ];
  for (const token of forbidden) {
    assert.ok(!source.includes(token), `ts/scstate.ts must not mention ${token}`);
  }
});

test("aliases are the same objects and constants match the ticket", () => {
  const aliases: Array<[unknown, unknown]> = [
    [tech_state_slot, techStateSlot],
    [upgrade_state_slot, upgradeStateSlot],
    [tech_researched, techResearched],
    [tech_researched_var, techResearchedVar],
    [set_tech_researched, setTechResearched],
    [upgrade_level, upgradeLevel],
    [set_upgrade_level, setUpgradeLevel],
  ];
  for (const [alias, target] of aliases) assert.equal(alias, target);

  assert.equal(TECH_COUNT, 44);
  assert.equal(TECH_SC_COUNT, 24);
  assert.equal(TECH_STATE_SC_BASE, 0x58cf44);
  assert.equal(TECH_STATE_BW_BASE, 0x58f140);
  assert.equal(TECH_STATE_SC_STRIDE, 24);
  assert.equal(TECH_STATE_BW_STRIDE, 20);
  assert.equal(UPGRADE_COUNT, 61);
  assert.equal(UPGRADE_SC_COUNT, 46);
  assert.equal(UPGRADE_STATE_SC_BASE, 0x58d2b0);
  assert.equal(UPGRADE_STATE_BW_BASE, 0x58f32c);
  assert.equal(UPGRADE_STATE_SC_STRIDE, 46);
  assert.equal(UPGRADE_STATE_BW_STRIDE, 15);
  assert.equal(MAX_PLAYER_ID, 11);
});

test("the §3.2 signature block typechecks", () => {
  const signatures: {
    techStateSlot: (tech: number, player: number) => [number, number];
    upgradeStateSlot: (upgrade: number, player: number) => [number, number];
    techResearched: (tech: number, player: number) => HirCond;
    techResearchedVar: (tech: number, player: VarImpl | number) => VarImpl;
    upgradeLevel: (upgrade: number, player: VarImpl | number) => VarImpl;
    setTechResearched: (
      tech: number,
      player: VarImpl | number,
      on: boolean,
    ) => void;
    setUpgradeLevel: (
      upgrade: number,
      player: VarImpl | number,
      level: number,
    ) => void;
  } = {
    techStateSlot,
    upgradeStateSlot,
    techResearched,
    techResearchedVar,
    upgradeLevel,
    setTechResearched,
    setUpgradeLevel,
  };
  for (const fn of Object.values(signatures)) {
    assert.equal(typeof fn, "function");
  }
});

test("constant round trip preserves neighbour lanes", () => {
  const epd = EPD(0x58cf44);
  const box = { out: -1 };
  const module = buildModule(() => {
    SetDeaths(epd, SetTo, 0xaaaaaa00, 0);
    setTechResearched(0, 0, true);
    const out = Var(0);
    box.out = out.id;
    out.set(techResearchedVar(0, 0));
  });
  const report = runSim(module.toJson());
  assert.equal(report.var_values[box.out], 1);
  assert.equal(report.external_epds[String(epd)], 0xaaaaaa01);
});
