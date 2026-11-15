// Periodic BGM helper (`playBgm`) and the general MPQ embed surface.
//
// Covers the euddraft `plugins/bgmplayer.py` port: the file is embedded as
// `bgm`, the first loop plays immediately, replays wait `lengthSeconds`
// (minus eudplib's 42 ms start-latency compensation), the idle loop never
// reads the clock, and CurrentPlayer is restored after PlayWAV.
import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  AtMost,
  BGM_ARCHIVE_NAME,
  BGM_CLOCK_ADDR,
  CP_ADDR,
  EudslError,
  SetMemory,
  SetTo,
  START_LATENCY_MS,
  Trigger,
  USER_PLAYER_ADDR,
  Var,
  buildModule,
  embedFile,
  emitVarStore,
  once,
  playBgm,
} from "../index.ts";
import { EPD_BASE, readDword, runSim, runStatic } from "./helpers.ts";

const SENTINEL_ADDR = 0x600000;
const CLOCK_START = 1_000_000;
const LOCAL_PLAYER = 3;
const SAVED_CP = 7;

function epd(addr: number): number {
  return ((addr - EPD_BASE) / 4) >>> 0;
}

interface HirCond {
  condtype?: number;
  player?: number;
  comparison?: number;
  amount?: number;
  [key: string]: unknown;
}

interface HirStmt {
  kind: string;
  id?: number;
  conds?: HirCond[];
  acts?: Array<Record<string, unknown>>;
  body?: HirStmt[];
  func?: number;
  dst_addr?: number;
  dst_trigger?: number;
  dst_offset?: number;
  dst_var?: number;
  src_var?: number;
  modifier?: number;
  value?: number;
  wav?: string;
  acttype?: number;
  relocs?: Array<{ target_var?: number; target_offset?: number }>;
  [key: string]: unknown;
}

interface HirFunc {
  id: number;
  body: HirStmt[];
}

interface HirDoc {
  module: {
    stmts: HirStmt[];
    vars: Array<{ id: number }>;
    funcs?: HirFunc[];
    files?: Array<{ name: string; path: string }>;
  };
}

function moduleJson(body: () => void): HirDoc {
  return JSON.parse(buildModule(body).toJson()) as HirDoc;
}

function whenBody(doc: HirDoc): HirStmt[] {
  const when = doc.module.stmts.find((stmt) => stmt.kind === "when");
  assert.ok(when, "expected a when gate");
  return when.body ?? [];
}

function onceBody(doc: HirDoc): HirStmt[] {
  const node = doc.module.stmts.find((stmt) => stmt.kind === "once");
  assert.ok(node, "expected a one-time init block");
  return node.body ?? [];
}

/**
 * True when a read of `addr` appears in `stmts` or a subroutine they call.
 *
 * Full-dword reads emit one reader body per distinct EPD (32 bit tests whose
 * `player` field is that EPD) and the caller reaches it through a `call`.
 * Partial reads stay inline and keep the bit-test condition shape.
 */
function readsAddr(
  stmts: HirStmt[],
  addr: number,
  funcs: HirFunc[] = [],
): boolean {
  const target = epd(addr);
  const byId = new Map(funcs.map((func) => [func.id, func]));
  for (const stmt of stmts) {
    if (
      (stmt.conds ?? []).some(
        (cond) => cond.condtype === 15 && cond.player === target,
      )
    ) {
      return true;
    }
    if (stmt.kind === "call" && stmt.func !== undefined) {
      const body = byId.get(stmt.func)?.body ?? [];
      if (readsAddr(body, addr)) return true;
    }
  }
  return false;
}

/** The top-level clock probe trigger (one AtMost condition on 0x51CE8C). */
function probe(doc: HirDoc): HirStmt {
  for (const stmt of doc.module.stmts) {
    if (stmt.kind !== "trigger") continue;
    for (const cond of stmt.conds ?? []) {
      if (cond.player === epd(BGM_CLOCK_ADDR)) return stmt;
    }
  }
  throw new Error("no clock probe trigger found");
}

/** Arm the helper, tick the countdown clock, optionally expose the threshold. */
function playModule(
  periodSeconds = 1.0,
  options: {
    clockStart?: number;
    localPlayer?: number;
    savedCp?: number;
    tickMs?: number;
    sentinel?: boolean;
  } = {},
): HirDoc {
  const {
    clockStart = CLOCK_START,
    localPlayer = LOCAL_PLAYER,
    savedCp = SAVED_CP,
    tickMs = 100,
    sentinel = true,
  } = options;
  return moduleJson(() => {
    once(() => {
      SetMemory(BGM_CLOCK_ADDR, SetTo, clockStart);
      SetMemory(USER_PLAYER_ADDR, SetTo, localPlayer);
      SetMemory(CP_ADDR, SetTo, savedCp);
    });
    const threshold = playBgm("assets/bgm.wav", periodSeconds);
    if (sentinel) {
      emitVarStore(threshold.id, { addr: SENTINEL_ADDR });
    }
    new Trigger({
      conds: [],
      acts: [SetMemory(BGM_CLOCK_ADDR, Add, -tickMs)],
      preserved: true,
    });
  });
}

// --- HIR shape -------------------------------------------------------------

test("embeds the file as bgm and returns the threshold", () => {
  const doc = moduleJson(() => {
    playBgm("audio/track.wav", 1.25);
  });
  assert.deepEqual(doc.module.files, [
    { name: BGM_ARCHIVE_NAME, path: "audio/track.wav" },
  ]);
});

test("probe is always due on the first loop", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const p = probe(doc);
  assert.equal(p.conds?.length, 1);
  const cond = p.conds![0];
  assert.equal(cond.condtype, 15); // Deaths / Memory wire form
  assert.equal(cond.player, epd(BGM_CLOCK_ADDR));
  assert.equal(cond.comparison, AtMost);
  assert.equal(cond.amount, 0xffffffff);
});

test("PlayWAV runs between the CP switch and restore", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const body = whenBody(doc);
  const cpStores = body
    .map((stmt, index) => [index, stmt] as const)
    .filter(
      ([, stmt]) => stmt.kind === "varstore" && stmt.dst_addr === CP_ADDR,
    );
  const plays = body
    .map((stmt, index) => [index, stmt] as const)
    .filter(([, stmt]) => stmt.kind === "act" && stmt.wav === BGM_ARCHIVE_NAME);
  assert.equal(cpStores.length, 2, "one CP switch and one restore");
  assert.equal(plays.length, 1);
  const switchAt = cpStores[0][0];
  const restoreAt = cpStores[1][0];
  const playAt = plays[0][0];
  assert.ok(switchAt < playAt, "CP is switched before PlayWAV");
  assert.ok(playAt < restoreAt, "CP is restored after PlayWAV");
  assert.equal(plays[0][1].acttype, 8); // PlayWAV
});

test("threshold is patched into the probe condition", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const p = probe(doc);
  const body = whenBody(doc);
  // Condition slot base (+8) plus the amount field offset (+8).
  const amountOffset = 16;
  const patches = body.filter(
    (stmt) =>
      stmt.kind === "varstore" &&
      stmt.dst_trigger === p.id &&
      stmt.dst_offset === amountOffset,
  );
  assert.equal(patches.length, 1, "the next threshold patches the probe");
});

test("timer arithmetic includes the 42 ms adjustment", () => {
  const periodMs = 1000;
  const doc = moduleJson(() => {
    playBgm("bgm.wav", periodMs / 1000);
  });
  const thresholdId = doc.module.vars[0].id;
  const body = whenBody(doc);
  const subtracts = body.filter(
    (stmt) =>
      stmt.kind === "act" &&
      stmt.modifier === Add && // Add two's complement == Subtract
      (stmt.relocs ?? []).some((r) => r.target_var === thresholdId),
  );
  assert.equal(subtracts.length, 1);
  const adjustment = periodMs - 42;
  assert.equal(subtracts[0].value, (-adjustment) >>> 0);
});

test("start-latency compensation is 42 ms", () => {
  // Pins the eudplib literal; every schedule subtracts exactly this.
  assert.equal(START_LATENCY_MS, 42);
});

test("local player id is read once at start", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  assert.ok(
    readsAddr(onceBody(doc), USER_PLAYER_ADDR, doc.module.funcs),
    "the local player id must be read by the one-time init block",
  );
  assert.ok(
    !readsAddr(whenBody(doc), USER_PLAYER_ADDR, doc.module.funcs),
    "the play path must reuse the cached id, not re-read 0x512684",
  );
});

test("init read precedes the probe and gate", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const kinds = doc.module.stmts.map((stmt) => stmt.kind);
  assert.ok(kinds.indexOf("once") < kinds.indexOf("trigger"));
  assert.ok(kinds.indexOf("trigger") < kinds.indexOf("when"));
});

test("exact period skips the zero adjustment act", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 42 / 1000);
  });
  const thresholdId = doc.module.vars[0].id;
  const body = whenBody(doc);
  const subtracts = body.filter(
    (stmt) =>
      stmt.kind === "act" &&
      (stmt.relocs ?? []).some((r) => r.target_var === thresholdId),
  );
  assert.deepEqual(subtracts, [], "period == 42 ms needs no adjustment");
});

test("a period below the start latency adds the deficit", () => {
  // period < 42 ms leaves a negative adjustment; `isub` emits the
  // two's-complement Add of `42 - period`, so the threshold is pushed *up*
  // and the next loop is already due -- eudplib's `f_starttimer(< 42)`
  // behaviour.
  const periodMs = 20;
  const doc = moduleJson(() => {
    playBgm("bgm.wav", periodMs / 1000);
  });
  const thresholdId = doc.module.vars[0].id;
  const adds = whenBody(doc).filter(
    (stmt) =>
      stmt.kind === "act" &&
      stmt.modifier === Add && // two's-complement Add
      (stmt.relocs ?? []).some((r) => r.target_var === thresholdId),
  );
  assert.equal(adds.length, 1, "one adjustment act, as an Add");
  assert.equal(adds[0].value, (START_LATENCY_MS - periodMs) >>> 0);
});

test("clock read uses the shared reader body", () => {
  // The raw clock is a full 32-bit dword read, so it must share the
  // per-address reader body (a `call`) instead of inlining 32 bit tests at the
  // play site. Inlining regressed executed actions by 134.
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const body = whenBody(doc);
  assert.ok(readsAddr(body, BGM_CLOCK_ADDR, doc.module.funcs ?? []));
  const target = epd(BGM_CLOCK_ADDR);
  const inline = body.filter((stmt) =>
    (stmt.conds ?? []).some((cond) => cond.player === target),
  );
  assert.deepEqual(inline, [], "the reader body must not be inlined");
  assert.ok(
    body.some((stmt) => stmt.kind === "call"),
    "the play path reaches the reader through a call",
  );
});

test("local player read uses the shared reader body", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const init = onceBody(doc);
  assert.ok(readsAddr(init, USER_PLAYER_ADDR, doc.module.funcs ?? []));
  const target = epd(USER_PLAYER_ADDR);
  const inline = init.filter((stmt) =>
    (stmt.conds ?? []).some((cond) => cond.player === target),
  );
  assert.deepEqual(inline, [], "the reader body must not be inlined");
  assert.ok(
    init.some((stmt) => stmt.kind === "call"),
    "the init block reaches the reader through a call",
  );
});

test("the CP switch copies the cached local player id", () => {
  // The switch source is the *value* the init block cached; switching to any
  // other variable (e.g. the hit counter) plays the track for the wrong
  // player while every ordering and restore test still passes.
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const transfers = onceBody(doc).filter((stmt) => stmt.kind === "vartransfer");
  assert.equal(transfers.length, 1, "the init block caches one value");
  const cachedLocalPlayer = transfers[0].dst_var;
  assert.ok(
    readsAddr(onceBody(doc), USER_PLAYER_ADDR, doc.module.funcs),
    "the cached value must come from the local-player dword",
  );
  const switches = whenBody(doc).filter(
    (stmt) => stmt.kind === "varstore" && stmt.dst_addr === CP_ADDR,
  );
  assert.equal(switches.length, 2, "one CP switch and one restore");
  assert.equal(
    switches[0].src_var,
    cachedLocalPlayer,
    "the CP switch must copy the cached local-player id",
  );
});

// --- Embed surface ---------------------------------------------------------

test("embed is general and deduplicates identical requests", () => {
  const doc = moduleJson(() => {
    embedFile("bgm", "a.wav");
    embedFile("bgm", "a.wav");
    embedFile("data", "blob.bin");
  });
  assert.deepEqual(doc.module.files, [
    { name: "bgm", path: "a.wav" },
    { name: "data", path: "blob.bin" },
  ]);
});

test("duplicate name with a different path is rejected", () => {
  assert.throws(
    () =>
      buildModule(() => {
        embedFile("bgm", "a.wav");
        embedFile("bgm", "b.wav");
      }),
    (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-BOUNDS");
      return true;
    },
  );
});

test("empty name or path is rejected", () => {
  for (const [name, path] of [
    ["", "a.wav"],
    ["bgm", ""],
  ] as const) {
    assert.throws(
      () => buildModule(() => {
        embedFile(name, path);
      }),
      (err: unknown) => {
        assert.ok(err instanceof EudslError);
        assert.equal(err.code, "EUDSL-BOUNDS");
        return true;
      },
      `name=${JSON.stringify(name)} path=${JSON.stringify(path)}`,
    );
  }
});

test("embed outside buildModule is rejected", () => {
  assert.throws(
    () => embedFile("bgm", "a.wav"),
    (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-SCOPE");
      return true;
    },
  );
});

// --- Runtime ---------------------------------------------------------------

test("first play is immediate", () => {
  const mod = playModule(1.0);
  const report = runSim(buildJson(mod), 1);
  assert.equal(
    readDword(report, epd(SENTINEL_ADDR)),
    CLOCK_START - (1000 - 42),
    "the first loop always plays and schedules the next threshold",
  );
});

test("no replay before the timer hits", () => {
  const mod = playModule(1.0);
  const first = CLOCK_START - (1000 - 42);
  for (const loops of [2, 10]) {
    const report = runSim(buildJson(mod), loops);
    assert.equal(
      readDword(report, epd(SENTINEL_ADDR)),
      first,
      `loop ${loops} must not replay before the 1000 ms period`,
    );
  }
});

test("replay after the period", () => {
  const mod = playModule(1.0);
  // 100 ms per loop: the probe hits when the raw clock has fallen by
  // 1000 ms minus the 42 ms compensation, i.e. on loop 11.
  let report = runSim(buildJson(mod), 11);
  assert.equal(
    readDword(report, epd(SENTINEL_ADDR)),
    CLOCK_START - 1000 - (1000 - 42),
  );
  report = runSim(buildJson(mod), 21);
  assert.equal(
    readDword(report, epd(SENTINEL_ADDR)),
    CLOCK_START - 2000 - (1000 - 42),
  );
});

test("CP is restored after PlayWAV", () => {
  for (const savedCp of [0, 7, 9]) {
    const mod = playModule(1.0, { savedCp });
    const report = runSim(buildJson(mod), 1);
    assert.equal(
      readDword(report, epd(CP_ADDR)),
      savedCp,
      "PlayWAV must leave CurrentPlayer as it found it",
    );
  }
});

test("exact period threshold needs no adjustment", () => {
  const mod = playModule(42 / 1000);
  const report = runSim(buildJson(mod), 1);
  assert.equal(
    readDword(report, epd(SENTINEL_ADDR)),
    CLOCK_START,
    "a 42 ms period schedules the raw clock verbatim",
  );
});

test("a period below the start latency stays due", () => {
  // 20 ms period: the -22 ms adjustment pushes the threshold 22 ms past the
  // raw clock, so the very next loop plays again.
  const mod = playModule(20 / 1000);
  let report = runSim(buildJson(mod), 1);
  assert.equal(readDword(report, epd(SENTINEL_ADDR)), CLOCK_START + 22);
  report = runSim(buildJson(mod), 2);
  assert.equal(
    readDword(report, epd(SENTINEL_ADDR)),
    CLOCK_START - 100 + 22,
    "the already-due schedule replays on the next loop",
  );
});

test("idle cycle cost never reads the clock", () => {
  // No sentinel copy: the last-loop executed cost is the helper plus the
  // one-action clock tick that advances the simulation.
  const mod = playModule(1.0, { sentinel: false });
  const report = runSim(buildJson(mod), 2);
  const cost = report.executed_cost;
  assert.deepEqual(
    [cost.triggers, cost.conditions, cost.actions],
    [3, 2, 1],
    "armed/idle loop = probe + when gate + the test's clock tick",
  );
  assert.ok(
    cost.conditions < 32,
    "an idle loop must not pay for the 32-test clock read",
  );
});

test("static cost is reported", () => {
  const mod = playModule(1.0, { sentinel: false });
  const staticCost = runStatic(buildJson(mod)).static_cost;
  assert.ok(staticCost.triggers > 1);
  assert.equal(staticCost.blob_bytes, 4);
});

test("helper cost is pinned to the shared dword reader shape", () => {
  // FE2 routed bgm's direct reads through `readEpd`, which inlined the
  // 32-test reader instead of sharing the full-dword kernel. The helper must
  // keep the pre-FE2 (shared-reader) shape: 2 fewer triggers, 6 fewer actions,
  // and 4,820 fewer bytes than the inline regression.
  const mod = moduleJson(() => {
    playBgm("bgm.wav", 1.0);
  });
  const staticCost = runStatic(buildJson(mod)).static_cost;
  assert.deepEqual(
    [
      staticCost.triggers,
      staticCost.conditions,
      staticCost.actions,
      staticCost.trigger_bytes,
      staticCost.total_bytes,
    ],
    [108, 98, 180, 260_064, 260_068],
  );
  assert.equal(mod.module.funcs?.length ?? 0, 3);

  const report = runSim(buildJson(mod), 1);
  const cost = report.executed_cost;
  assert.deepEqual(
    [cost.triggers, cost.conditions, cost.actions],
    [110, 98, 212],
    "the shared reader must not add executed actions per play",
  );
});

// --- Validation ------------------------------------------------------------

test("rejects non-numbers and non-positive lengths", () => {
  const values: unknown[] = [
    0,
    0.0,
    -1.0,
    -0.001,
    "1.0",
    null,
    true,
    Number.NaN,
    Number.POSITIVE_INFINITY,
  ];
  for (const value of values) {
    assert.throws(
      () => buildModule(() => {
        playBgm("bgm.wav", value as number);
      }),
      (err: unknown) => {
        assert.ok(err instanceof EudslError);
        assert.ok(
          err.code === "EUDSL-TYPE" || err.code === "EUDSL-RANGE",
          `unexpected code ${err.code} for ${String(value)}`,
        );
        return true;
      },
      `value=${String(value)}`,
    );
  }
});

test("truncates fractional milliseconds", () => {
  const doc = moduleJson(() => {
    playBgm("bgm.wav", 1.0009);
  });
  const p = probe(doc);
  assert.equal(p.conds![0].amount, 0xffffffff);
  // The schedule is 1000 ms (truncated), so the subtraction act carries
  // -(1000 - 42).
  const body = whenBody(doc);
  const subtracts = body.filter(
    (stmt) => stmt.kind === "act" && stmt.modifier === Add,
  );
  assert.equal(subtracts.length, 1);
  assert.equal(subtracts[0].value, (-(1000 - 42)) >>> 0);
});

// `moduleJson` already returns a parsed doc; keep a small alias so the runtime
// tests read like the Python mirror while the HIR JSON is what the sim eats.
function buildJson(doc: HirDoc): string {
  return JSON.stringify(doc);
}
