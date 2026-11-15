// Loop-sound player (`ts/soundlooper.ts`), a port of euddraft's
// `lib/soundlooper.py`. Mirror of `py/tests/test_soundlooper.py`.
//
// Covers the build-time segment scanner (`SoundLoops.addLoop` /
// `manualAddLoop`), the emitted loop structure (intro -> bar -> bridge ->
// `goto`), the bar/digit machinery, and the runtime schedule driven by the
// `0x51CE8C` countdown clock.
//
// The fixtures under `tests/fixtures/sounds/theme` are the tiny hand-built Ogg
// streams from the length-reader tests: `theme000` is 1.0 s, `theme001`
// 1.5 s and `theme002` 2.0 s, so the loop is intro=1.0, bar=1.5,
// bridge=2.0, count=2.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  Add,
  AtLeast,
  AtMost,
  CP_ADDR,
  DEFAULT_SPEED_X,
  Exactly,
  EudslError,
  GAME_SPEED_ADDR,
  SOUND_CLOCK_ADDR,
  SPEED_84_ADDR,
  SPEED_88_ADDR,
  SPEED_MAP,
  SPEED_SENTINEL,
  SetMemory,
  SetTo,
  SoundLoop,
  SoundLooper,
  SoundLoops,
  Trigger,
  USER_PLAYER_ADDR,
  Var,
  type VarImpl,
  buildModule,
  calculateSpeedError,
  emitVarStore,
  loopIdentifier,
  once,
  packBarDigits,
  soundLooper,
  when,
} from "../index.ts";
import { EPD_BASE, readDword, runSim, runStatic, type SimReport } from "./helpers.ts";

const FIXTURES = path.resolve(import.meta.dirname, "..", "..", "tests", "fixtures");
const SOUNDS = path.join(FIXTURES, "sounds");
const THEME = path.join(SOUNDS, "theme");

const SENTINEL = 0x600000;
const CLOCK_START = 1_000_000;
const LOCAL_PLAYER = 3;
const SAVED_CP = 7;
const TICK_MS = 100;

const ACT_TYPE_SET_MEMORY = 45;
const ACT_TYPE_PLAY_WAV = 8;
const ACT_VALUE_OFFSET = 20;

interface HirCond {
  condtype?: number;
  player?: number;
  comparison?: number;
  amount?: number;
  locid?: number;
  relocs?: Array<{ target_var?: number; target_offset?: number }>;
  [key: string]: unknown;
}

interface HirAct {
  acttype?: number;
  modifier?: number;
  value?: number;
  wav?: string;
  relocs?: Array<{ target_var?: number; target_offset?: number }>;
  [key: string]: unknown;
}

interface HirStmt {
  kind?: string;
  id?: number;
  conds?: HirCond[];
  acts?: HirAct[];
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

function epd(addr: number): number {
  return ((addr - EPD_BASE) / 4) >>> 0;
}

function moduleJson(body: () => void): HirDoc {
  return JSON.parse(buildModule(body).toJson()) as HirDoc;
}

function buildJson(doc: HirDoc): string {
  return JSON.stringify(doc);
}

/** Every statement and action, descending into block bodies and trigger acts. */
function walk(stmts: HirStmt[]): HirStmt[] {
  const out: HirStmt[] = [];
  for (const stmt of stmts) {
    out.push(stmt);
    if (stmt.kind === "when" || stmt.kind === "once" || stmt.kind === "until" || stmt.kind === "func") {
      out.push(...walk(stmt.body ?? []));
    }
    if (stmt.kind === "trigger") {
      for (const act of stmt.acts ?? []) out.push({ kind: "act", ...act });
    }
  }
  return out;
}

/**
 * True when a bit-test read of `addr` appears under `stmts`.
 *
 * Full-dword reads emit one reader body per distinct EPD and the caller reaches
 * it through a `call`; `funcs` lets the walk follow those edges.
 */
function readsAddr(stmts: HirStmt[], addr: number, funcs: HirFunc[] = []): boolean {
  const target = epd(addr);
  const byId = new Map(funcs.map((func) => [func.id, func]));
  for (const stmt of walk(stmts)) {
    if (
      (stmt.conds ?? []).some(
        (cond) => cond.condtype === 15 && cond.player === target,
      )
    ) {
      return true;
    }
    if (stmt.kind === "call") {
      const body = byId.get(stmt.func as number)?.body ?? [];
      if (readsAddr(body, addr)) return true;
    }
  }
  return false;
}

function playsIn(stmts: HirStmt[]): string[] {
  return walk(stmts)
    .filter((stmt) => stmt.kind === "act" && stmt.acttype === ACT_TYPE_PLAY_WAV)
    .map((stmt) => stmt.wav as string);
}

function probe(doc: HirDoc): HirStmt {
  for (const stmt of doc.module.stmts) {
    if (stmt.kind !== "trigger") continue;
    for (const cond of stmt.conds ?? []) {
      if (cond.player === epd(SOUND_CLOCK_ADDR)) return stmt;
    }
  }
  throw new Error("no clock probe trigger found");
}

/** The top-level `when` whose body plays a segment. */
function tick(doc: HirDoc): HirStmt {
  for (const stmt of doc.module.stmts) {
    if (stmt.kind === "when" && playsIn(stmt.body ?? []).length > 0) return stmt;
  }
  throw new Error("no player tick body found");
}

function varstores(stmts: HirStmt[], match: Record<string, unknown>): HirStmt[] {
  return walk(stmts).filter(
    (stmt) =>
      stmt.kind === "varstore" &&
      Object.entries(match).every(([key, value]) => stmt[key] === value),
  );
}

function assignedActs(
  stmts: HirStmt[],
  varId: number,
  modifier?: number,
): HirStmt[] {
  const out: HirStmt[] = [];
  for (const stmt of walk(stmts)) {
    if (stmt.kind !== "act" || stmt.acttype !== ACT_TYPE_SET_MEMORY) continue;
    if (modifier !== undefined && stmt.modifier !== modifier) continue;
    if (
      (stmt.relocs ?? []).some(
        (reloc) =>
          reloc.target_var === varId &&
          reloc.target_offset === ACT_VALUE_OFFSET,
      )
    ) {
      out.push(stmt);
    }
  }
  return out;
}

function themeModule(
  goto = 1,
  bar: number | null = null,
): { doc: HirDoc; loops: SoundLoops; looper: SoundLooper } {
  const holder: { loops?: SoundLoops; looper?: SoundLooper } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops(SOUNDS);
    holder.loops = loops;
    loops.addLoop("theme", goto);
    const looper = new SoundLooper(loops);
    holder.looper = looper;

    once(() => {
      looper.play("theme", bar);
    });
    looper.player();
  });
  return { doc, loops: holder.loops!, looper: holder.looper! };
}

function themeLoops(goto = 1): SoundLoops {
  const loops = new SoundLoops(SOUNDS);
  loops.addLoop("theme", goto);
  return loops;
}

function errCode(fn: () => void, expected: string, message?: string): void {
  assert.throws(
    fn,
    (err: unknown) => {
      assert.ok(err instanceof EudslError, `expected EudslError, got ${String(err)}`);
      assert.equal(err.code, expected, `${message ?? ""}: ${err.message}`);
      return true;
    },
    message,
  );
}

// --- identifiers and digit packing -----------------------------------------

test("loop identifier is five alnum characters and deterministic", () => {
  const first = loopIdentifier("theme", 0);
  assert.equal(first.length, 5);
  assert.match(first, /^[a-zA-Z0-9]{5}$/);
  assert.equal(first, loopIdentifier("theme", 0));
  assert.notEqual(first, loopIdentifier("theme", 1));
  assert.notEqual(first, loopIdentifier("other", 0));
  // Cross-frontend literal: `py/soundlooper.py` produces the same id, so the
  // embedded MPQ names match between the two frontends.
  assert.equal(first, "idwNy");
});

test("packBarDigits packs three little-endian decimal digits", () => {
  assert.equal(packBarDigits(0), 0);
  assert.equal(packBarDigits(1), 0x10000); // "001"
  assert.equal(packBarDigits(10), 0x100); // "010"
  assert.equal(packBarDigits(123), 0x030201); // "123"
  assert.equal(packBarDigits(999), 0x090909); // "999"
});

test("packBarDigits rejects out-of-range bars", () => {
  for (const value of [-1, 1000, 70000]) {
    errCode(() => packBarDigits(value), "EUDSL-RANGE", `value=${value}`);
  }
});

// --- registration -----------------------------------------------------------

test("addLoop scans the numbered segments", () => {
  const holder: { loop?: SoundLoop | null } = {};
  moduleJson(() => {
    holder.loop = new SoundLoops(SOUNDS).addLoop("theme");
  });
  const loop = holder.loop!;
  assert.ok(loop instanceof SoundLoop);
  assert.equal(loop.title, "theme");
  assert.equal(loop.index, 0);
  assert.equal(loop.count, 2);
  assert.equal(loop.barCount, 3);
  assert.deepEqual([loop.intro, loop.bar, loop.bridge], [1.0, 1.5, 2.0]);
  assert.equal(loop.goto, 1);
  assert.equal(loop.identifier.length, 5);
});

test("addLoop embeds each segment under id plus three digits", () => {
  const { doc, loops } = themeModule();
  const identifier = loops.get("theme")!.identifier;
  assert.deepEqual(
    doc.module.files,
    [0, 1, 2].map((number) => ({
      name: `${identifier}${String(number).padStart(3, "0")}`,
      path: path.join(THEME, `theme${String(number).padStart(3, "0")}.ogg`),
    })),
  );
});

test("the last segment sets the bridge length", () => {
  const { loops } = themeModule();
  const loop = loops.get("theme")!;
  assert.equal(loop.bridge, 2.0);
  assert.equal(loop.baseCorrectionMs, -1500);
  assert.equal(loop.introCorrectionMs, 500);
  assert.equal(loop.bridgeCorrectionMs, -500);
});

test("shortest zero padding is tried first", () => {
  const tmpdir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-sound-"));
  try {
    const folder = path.join(tmpdir, "song");
    fs.mkdirSync(folder);
    fs.copyFileSync(path.join(THEME, "theme000.ogg"), path.join(folder, "song0.ogg"));
    moduleJson(() => {
      const loops = new SoundLoops(tmpdir);
      const loop = loops.addLoop("song")!;
      assert.equal(loop.count, 0);
      assert.equal(loop.intro, 1.0);
      assert.equal(loop.bar, 0.0);
      assert.equal(loop.bridge, 1.0);
    });
  } finally {
    fs.rmSync(tmpdir, { recursive: true, force: true });
  }
});

test("a gap ends the scan", () => {
  const tmpdir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-sound-"));
  try {
    const folder = path.join(tmpdir, "gap");
    fs.mkdirSync(folder);
    fs.copyFileSync(path.join(THEME, "theme000.ogg"), path.join(folder, "gap000.ogg"));
    fs.copyFileSync(path.join(THEME, "theme002.ogg"), path.join(folder, "gap002.ogg"));
    moduleJson(() => {
      const loops = new SoundLoops(tmpdir);
      const loop = loops.addLoop("gap")!;
      // Segment 001 is missing: the scan stops after 000, so the intro is the
      // only measured segment and there is no bar.
      assert.equal(loop.count, 0);
      assert.equal(loop.intro, 1.0);
      assert.equal(loop.bar, 0.0);
      assert.equal(loop.bridge, 1.0);
    });
  } finally {
    fs.rmSync(tmpdir, { recursive: true, force: true });
  }
});

test("no segments is an error", () => {
  const tmpdir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-sound-"));
  try {
    errCode(
      () =>
        buildModule(() => {
          new SoundLoops(tmpdir).addLoop("absent");
        }),
      "EUDSL-RANGE",
    );
  } finally {
    fs.rmSync(tmpdir, { recursive: true, force: true });
  }
});

test("unknown loop title is rejected by play", () => {
  const { looper } = themeModule();
  errCode(() => looper.play("absent"), "EUDSL-RANGE");
});

test("duplicate title is rejected", () => {
  errCode(
    () =>
      moduleJson(() => {
        const loops = new SoundLoops(SOUNDS);
        loops.addLoop("theme");
        loops.addLoop("theme");
      }),
    "EUDSL-BOUNDS",
  );
});

test("registration is frozen after the player is used", () => {
  errCode(
    () =>
      moduleJson(() => {
        const loops = new SoundLoops(SOUNDS);
        loops.addLoop("theme");
        const looper = new SoundLooper(loops);
        looper.play("theme");
        loops.addLoop("theme");
      }),
    "EUDSL-SCOPE",
  );
});

// --- manual registration ----------------------------------------------------

test("manualAddLoop registers a pre-embedded loop", () => {
  const loops = new SoundLoops();
  const loop = loops.manualAddLoop("ABCDE", 4, 1.0, 2.5, 3.0, 2);
  assert.equal(loop.identifier, "ABCDE");
  assert.equal(loop.count, 4);
  assert.equal(loop.barCount, 5);
  assert.deepEqual([loop.intro, loop.bar, loop.bridge], [1.0, 2.5, 3.0]);
  assert.equal(loop.goto, 2);
});

test("manualAddLoop title must be five single-byte characters", () => {
  for (const title of ["ABCD", "ABCDEF", "가나다라마"]) {
    errCode(
      () => new SoundLoops().manualAddLoop(title, 1, 1.0, 1.0, 1.0),
      "EUDSL-RANGE",
      `title=${JSON.stringify(title)}`,
    );
  }
});

test("five ASCII characters are accepted", () => {
  // The source checks five cp949 *bytes* only, so a space is a legal
  // archive-name character; the port keeps that contract.
  const loop = new SoundLoops().manualAddLoop("A BCD", 1, 1.0, 1.0, 1.0);
  assert.equal(loop.identifier, "A BCD");
});

test("manualAddLoop count and goto ranges", () => {
  for (const overrides of [{ count: -1 }, { count: 1000 }, { goto: -1 }, { goto: 1000 }]) {
    const args = { count: 1, intro: 1.0, bar: 1.0, bridge: 1.0, goto: 1, ...overrides };
    errCode(
      () =>
        new SoundLoops().manualAddLoop(
          "ABCDE",
          args.count,
          args.intro,
          args.bar,
          args.bridge,
          args.goto,
        ),
      "EUDSL-RANGE",
      JSON.stringify(overrides),
    );
  }
});

test("manualAddLoop lengths must be finite and non-negative", () => {
  const cases: Array<[Record<string, unknown>, string]> = [
    [{ intro: "1.0" }, "EUDSL-TYPE"],
    [{ bar: Number.NaN }, "EUDSL-TYPE"],
    [{ bridge: -1.0 }, "EUDSL-RANGE"],
  ];
  for (const [overrides, code] of cases) {
    const args = { count: 1, intro: 1.0, bar: 1.0, bridge: 1.0, ...overrides };
    errCode(
      () =>
        new SoundLoops().manualAddLoop(
          "ABCDE",
          args.count,
          args.intro as number,
          args.bar as number,
          args.bridge as number,
        ),
      code,
      JSON.stringify(overrides),
    );
  }
});

// --- progress array ---------------------------------------------------------

test("bars are allocated once and shared", () => {
  moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    loops.manualAddLoop("FGHIJ", 1, 1.0, 2.0, 2.0);
    const bars = loops.bars();
    assert.equal(bars.length, 2);
    assert.equal(bars, loops.bars());
  });
});

test("no loops before bars is an error", () => {
  errCode(() => new SoundLoops().bars(), "EUDSL-RANGE");
});

// --- player surface ---------------------------------------------------------

test("soundLooper constructs the same class", () => {
  const holder: { looper?: SoundLooper } = {};
  moduleJson(() => {
    holder.looper = soundLooper(new SoundLoops());
  });
  assert.ok(holder.looper instanceof SoundLooper);
});

test("play tracks current and previous loop", () => {
  const { doc, looper } = themeModule();
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const transfers = onceBody.filter((stmt) => stmt.kind === "vartransfer");
  assert.ok(
    transfers.some(
      (stmt) =>
        stmt.dst_var === looper.previousLoop.id &&
        stmt.src_var === looper.currentLoop.id,
    ),
  );
  const sets = assignedActs(onceBody, looper.currentLoop.id, SetTo);
  assert.ok(
    sets.some((act) => act.value === 0),
    "play(const index) sets currentLoop to that index",
  );
});

test("pause moves current to previous and clears it", () => {
  const holder: { looper?: SoundLooper } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    holder.looper = looper;
    once(() => {
      looper.play("ABCDE");
      looper.pause();
    });
  });
  const looper = holder.looper!;
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const transfers = onceBody.filter((stmt) => stmt.kind === "vartransfer");
  assert.ok(
    transfers.some(
      (stmt) =>
        stmt.dst_var === looper.previousLoop.id &&
        stmt.src_var === looper.currentLoop.id,
    ),
  );
  // The -1 write is a `SetMemory` act or a vartransfer from a constant.
  const acts = assignedActs(onceBody, looper.currentLoop.id);
  assert.ok(acts.length > 0, "currentLoop must be cleared on pause");
});

test("toggle emits both branches", () => {
  const holder: { looper?: SoundLooper } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    holder.looper = looper;
    once(() => {
      looper.play("ABCDE");
      looper.toggle();
    });
  });
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const whens = onceBody.filter((stmt) => stmt.kind === "when");
  assert.ok(whens.length >= 2);
  const pairs = whens.flatMap((stmt) =>
    (stmt.conds ?? []).map((cond) => [cond.comparison, cond.amount] as const),
  );
  assert.ok(pairs.some(([comparison, amount]) => comparison === Exactly && amount === 0xffffffff));
});

test("sendbar copies between progress slots", () => {
  const holder: { loops?: SoundLoops } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    loops.manualAddLoop("FGHIJ", 2, 1.0, 2.0, 3.0);
    holder.loops = loops;
    const looper = new SoundLooper(loops);
    looper.sendbar("FGHIJ", "ABCDE");
  });
  const bars = holder.loops!.bars();
  const transfers = doc.module.stmts.filter((stmt) => stmt.kind === "vartransfer");
  assert.ok(
    transfers.some(
      (stmt) =>
        stmt.dst_var === bars.get(1).id && stmt.src_var === bars.get(0).id,
    ),
  );
});

test("setloopbar and sendcurrentbar write the slot", () => {
  const holder: { loops?: SoundLoops; looper?: SoundLooper } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    holder.loops = loops;
    holder.looper = looper;
    looper.setloopbar("ABCDE", 2);
    looper.sendcurrentbar("ABCDE");
  });
  const looper = holder.looper!;
  const bars = holder.loops!.bars();
  const acts = assignedActs(doc.module.stmts, bars.get(0).id);
  assert.ok(acts.length > 0, "setloopbar must write the progress slot");
  assert.ok(
    doc.module.stmts.some(
      (stmt) =>
        stmt.kind === "vartransfer" &&
        stmt.dst_var === bars.get(0).id &&
        stmt.src_var === looper.currentBar.id,
    ),
    "sendcurrentbar must copy currentBar into the slot",
  );
});

test("cond and loopis are conditions", () => {
  const holder: { loops?: SoundLoops; looper?: SoundLooper } = {};
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    holder.loops = loops;
    holder.looper = looper;

    once(() => {
      when([looper.cond("ABCDE", AtLeast, 2), looper.loopis("ABCDE")], () => {});
    });
  });
  const looper = holder.looper!;
  const bars = holder.loops!.bars();
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const conds = onceBody.flatMap((stmt) => stmt.conds ?? []);
  assert.ok(
    conds.some((cond) =>
      (cond.relocs ?? []).some(
        (reloc) => reloc.target_var === looper.currentLoop.id && reloc.target_offset === 20,
      ),
    ),
    "loopis reads the currentLoop slot",
  );
  assert.ok(
    conds.some(
      (cond) =>
        cond.comparison === AtLeast &&
        cond.amount === 2 &&
        (cond.relocs ?? []).some((reloc) => reloc.target_var === bars.get(0).id),
    ),
  );
});

test("play bar argument is packed into digits", () => {
  const { doc, looper } = themeModule(1, 2);
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const acts = assignedActs(onceBody, looper.barDigits.id, SetTo);
  assert.ok(
    acts.some((act) => act.value === packBarDigits(2)),
    "setbar(2) must pack the three digits",
  );
});

test("dynamic index methods need build-time constants", () => {
  const holder: { calls?: Array<() => void> } = {};
  moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    const index = Var(0);
    holder.calls = [
      () => looper.sendcurrentbar(index),
      () => looper.sendbar("ABCDE", index),
      () => looper.setloopbar(index, 1),
      () => looper.cond(index, Exactly, 1),
    ];
  });
  for (const call of holder.calls!) errCode(call, "EUDSL-BOUNDS");
});

test("play, setbar and cond validate their arguments", () => {
  moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    errCode(() => looper.play(5), "EUDSL-RANGE", "index out of range");
    errCode(() => looper.play("ABCDE", 1000), "EUDSL-RANGE", "bar out of range");
    errCode(() => looper.setbar(1000), "EUDSL-RANGE", "setbar out of range");
    errCode(() => looper.cond("ABCDE", 12345, 1), "EUDSL-RANGE", "bad comparison");
    errCode(() => looper.play(1.5), "EUDSL-TYPE", "non-integer index");
  });
});

// --- loop structure ---------------------------------------------------------

test("probe is a clock read gated on a playing loop", () => {
  const { doc } = themeModule();
  const p = probe(doc);
  assert.equal(p.conds?.length, 2);
  const [clock, playing] = p.conds!;
  assert.equal(clock.condtype, 15);
  assert.equal(clock.comparison, AtMost);
  assert.equal(clock.amount, 0xfffffffe);
  assert.equal(playing.condtype, 15);
  assert.equal(playing.comparison, AtMost);
  assert.equal(playing.amount, 0xfffffffe);
});

test("tick plays every segment of the loop", () => {
  const { doc, loops } = themeModule();
  const identifier = loops.get("theme")!.identifier;
  assert.deepEqual(
    playsIn(tick(doc).body ?? []),
    [0, 1, 2].map((number) => `${identifier}${String(number).padStart(3, "0")}`),
  );
});

test("tick patches the probe threshold", () => {
  const { doc, looper } = themeModule();
  const p = probe(doc);
  // The amount field offset is 16 within the condition slot.
  const patches = varstores(tick(doc).body ?? [], { dst_trigger: p.id, dst_offset: 16 });
  assert.equal(patches.length, 1);
  assert.equal(patches[0].src_var, looper.threshold.id);
});

test("schedule constants match the source", () => {
  const { doc, loops, looper } = themeModule();
  const onceBody = doc.module.stmts.find((stmt) => stmt.kind === "once")!.body ?? [];
  const loop = loops.get("theme")!;
  const expected = new Map<number, number>([
    [looper.introAdd.id, loop.introCorrectionMs],
    [looper.bridgeAdd.id, loop.bridgeCorrectionMs],
    [looper.lastBar.id, loop.barCount],
    [looper.gotoBar.id, loop.goto],
    [looper.gotoDigits.id, packBarDigits(loop.goto)],
  ]);
  for (const [varId, value] of expected) {
    const acts = assignedActs(onceBody, varId, SetTo);
    assert.ok(
      acts.some((act) => act.value === value >>> 0),
      `var ${varId} must be set to ${value}`,
    );
  }
  // `segmentVar` holds the negative base correction before `err` is added.
  const bases = assignedActs(onceBody, looper.segmentVar.id, SetTo);
  assert.ok(bases.some((act) => act.value === loop.baseCorrectionMs >>> 0));
});

test("tick applies intro and bridge corrections", () => {
  const { doc, looper } = themeModule();
  const body = tick(doc).body ?? [];
  const adds = walk(body).filter(
    (stmt) =>
      stmt.kind === "vartransfer" &&
      stmt.dst_var === looper.threshold.id &&
      stmt.modifier === Add,
  );
  const sources = new Set(adds.map((stmt) => stmt.src_var));
  assert.ok(sources.has(looper.introAdd.id));
  assert.ok(sources.has(looper.bridgeAdd.id));
  assert.ok(sources.has(looper.segmentVar.id));
});

test("tick switches CP around the play", () => {
  const { doc } = themeModule();
  const body = tick(doc).body ?? [];
  const cpStores = body
    .map((stmt, index) => [index, stmt] as const)
    .filter(([, stmt]) => stmt.kind === "varstore" && stmt.dst_addr === CP_ADDR);
  const playAt = body.findIndex(
    (stmt) => stmt.kind === "when" && playsIn(stmt.body ?? []).length > 0,
  );
  assert.equal(cpStores.length, 2);
  assert.ok(cpStores[0][0] < playAt);
  assert.ok(playAt < cpStores[1][0]);
});

test("local player is read once", () => {
  const { doc } = themeModule();
  const onceOnly = doc.module.stmts.filter((stmt) => stmt.kind === "once");
  assert.ok(readsAddr(onceOnly, USER_PLAYER_ADDR, doc.module.funcs));
  assert.ok(
    !readsAddr(tick(doc).body ?? [], USER_PLAYER_ADDR, doc.module.funcs),
  );
});

test("digit carries follow the source", () => {
  const { doc } = themeModule();
  const body = tick(doc).body ?? [];
  // The mask lives in the condition's `locid` field (DeathsX row).
  const onesToTens = body.filter(
    (stmt) =>
      stmt.kind === "when" &&
      (stmt.conds ?? []).some((cond) => cond.locid === 0xff0000),
  );
  assert.equal(onesToTens.length, 1, "one ones->tens carry");
  const tensToHundreds = body.filter(
    (stmt) =>
      stmt.kind === "when" &&
      (stmt.conds ?? []).some((cond) => cond.locid === 0xff00),
  );
  assert.equal(tensToHundreds.length, 1, "one tens->hundreds carry");
});

test("last bar resets to goto", () => {
  const { doc, looper } = themeModule(1);
  const body = tick(doc).body ?? [];
  const resets = body.filter(
    (stmt) =>
      stmt.kind === "when" &&
      (stmt.body ?? []).some(
        (inner) =>
          inner.kind === "vartransfer" &&
          inner.dst_var === looper.currentBar.id &&
          inner.src_var === looper.gotoBar.id,
      ),
  );
  assert.equal(resets.length, 1);
  assert.deepEqual(playsIn(resets[0].body ?? []), []);
});

test("player may only be emitted once", () => {
  errCode(
    () =>
      moduleJson(() => {
        const loops = new SoundLoops();
        loops.manualAddLoop("ABCDE", 1, 1.0, 2.0, 2.0);
        const looper = new SoundLooper(loops);
        looper.player();
        looper.player();
      }),
    "EUDSL-SCOPE",
  );
});

// --- speed error ------------------------------------------------------------

test("speed-error constants match the source", () => {
  assert.equal(DEFAULT_SPEED_X, (-41) >>> 0);
  assert.equal(SPEED_SENTINEL, 42);
  assert.deepEqual(SPEED_MAP[0], [[2047, 2047], 29]);
  assert.deepEqual(SPEED_MAP[SPEED_MAP.length - 1], [[71429, 71429], 1]);
});

function speedError(
  sentinelValue = 0,
  pair: readonly [number, number] = [0, 0],
): number {
  const report = runSim(
    buildJson(
      moduleJson(() => {
        SetMemory(GAME_SPEED_ADDR, SetTo, sentinelValue);
        SetMemory(SPEED_84_ADDR, SetTo, pair[0]);
        SetMemory(SPEED_88_ADDR, SetTo, pair[1]);
        const err = calculateSpeedError();
        emitVarStore(err.id, { addr: SENTINEL });
      }),
    ),
    1,
  );
  return readDword(report, epd(SENTINEL));
}

test("default correction is 19", () => {
  assert.equal(speedError(), 19);
});

test("sentinel 42 uses the dword value", () => {
  // x = 42 -> (493 - 294) / 41 = 4
  assert.equal(speedError(42), 4);
});

test("speed pair overrides the reading", () => {
  // x = 29 -> (493 - 203) / 41 = 7
  assert.equal(speedError(0, [2047, 2047]), 7);
  // x = 36 -> (493 - 252) / 41 = 5
  assert.equal(speedError(0, [1157, 1157]), 5);
});

// --- runtime schedule -------------------------------------------------------

type PlayHook = (loops: SoundLoops, looper: SoundLooper) => void;

function runtimeModule(factory: () => SoundLoops, play: PlayHook): HirDoc {
  return moduleJson(() => {
    const loops = factory();
    const looper = new SoundLooper(loops);

    once(() => {
      SetMemory(SOUND_CLOCK_ADDR, SetTo, CLOCK_START);
      SetMemory(USER_PLAYER_ADDR, SetTo, LOCAL_PLAYER);
      SetMemory(CP_ADDR, SetTo, SAVED_CP);
      play(loops, looper);
    });
    looper.player();

    emitVarStore(looper.currentBar.id, { addr: SENTINEL + 0 });
    emitVarStore(looper.currentLoop.id, { addr: SENTINEL + 4 });
    emitVarStore(looper.barDigits.id, { addr: SENTINEL + 8 });
    emitVarStore(looper.threshold.id, { addr: SENTINEL + 12 });
    emitVarStore(looper.previousLoop.id, { addr: SENTINEL + 16 });
    for (let index = 0; index < loops.length; index++) {
      emitVarStore(loops.bars().get(index).id, { addr: SENTINEL + 20 + 4 * index });
    }

    new Trigger({
      conds: [],
      acts: [SetMemory(0x51ce8c, Add, -TICK_MS)],
      preserved: true,
    });
  });
}

function themeRuntime(goto = 1, bar: number | null = null): HirDoc {
  return runtimeModule(
    () => themeLoops(goto),
    (_loops, looper) => looper.play("theme", bar),
  );
}

function sentinel(report: SimReport, offset: number): number {
  return readDword(report, epd(SENTINEL + offset));
}

test("first pass plays the intro bar", () => {
  const report = runSim(buildJson(themeRuntime()), 1);
  assert.equal(sentinel(report, 0), 1); // bar 1
  assert.equal(sentinel(report, 4), 0); // loop index 0
  assert.equal(sentinel(report, 8), packBarDigits(1));
  assert.equal(sentinel(report, 20), 1); // bars[0]
  // raw + err(19) + ceil(-1.5s) + intro add(500) - 1
  assert.equal(sentinel(report, 12), CLOCK_START + 19 - 1500 + 500 - 1);
});

test("second bar comes after the intro length", () => {
  const doc = themeRuntime();
  // 1.0 s intro at 100 ms/loop: due on loop 11.
  for (let loops = 2; loops <= 10; loops++) {
    const report = runSim(buildJson(doc), loops);
    assert.equal(sentinel(report, 0), 1, `loop ${loops}`);
  }
  const report = runSim(buildJson(doc), 11);
  assert.equal(sentinel(report, 0), 2);
  assert.equal(sentinel(report, 8), packBarDigits(2));
  assert.equal(sentinel(report, 20), 2);
});

test("bridge bar resets to goto", () => {
  const doc = themeRuntime();
  // bar 2 is due at loop 11; bar 3 (bridge) 1.5 s later, loop 26.
  const report = runSim(buildJson(doc), 26);
  // The last-bar pass plays the bridge and resets in the same pass, so the
  // observable state is the loop's `goto` bar.
  assert.equal(sentinel(report, 0), 1);
  assert.equal(sentinel(report, 8), packBarDigits(1));
  assert.equal(sentinel(report, 20), 1);
});

test("loop returns to the goto sound", () => {
  const doc = themeRuntime();
  // Bar 3 (the bridge, reset in its own pass) is due at loop 26 with a 2.0 s
  // bridge, so the next bar is due 20 loops later, at loop 46.
  const report = runSim(buildJson(doc), 46);
  assert.equal(sentinel(report, 0), 2);
});

test("CP is restored after PlayWAV", () => {
  const report = runSim(buildJson(themeRuntime()), 1);
  assert.equal(readDword(report, epd(CP_ADDR)), SAVED_CP);
});

test("paused player does not advance", () => {
  const doc = moduleJson(() => {
    const loops = themeLoops();
    const looper = new SoundLooper(loops);

    once(() => {
      SetMemory(SOUND_CLOCK_ADDR, SetTo, CLOCK_START);
      SetMemory(USER_PLAYER_ADDR, SetTo, LOCAL_PLAYER);
      SetMemory(CP_ADDR, SetTo, SAVED_CP);
      looper.play("theme");
      looper.pause();
    });
    looper.player();
    emitVarStore(looper.currentBar.id, { addr: SENTINEL + 0 });
    emitVarStore(looper.currentLoop.id, { addr: SENTINEL + 4 });
    new Trigger({
      conds: [],
      acts: [SetMemory(0x51ce8c, Add, -TICK_MS)],
      preserved: true,
    });
  });
  const report = runSim(buildJson(doc), 20);
  assert.equal(sentinel(report, 0), 0, "a paused player never advances");
  assert.equal(sentinel(report, 4), 0xffffffff);
});

test("pause then resume keeps the loop", () => {
  const doc = moduleJson(() => {
    const loops = themeLoops();
    const looper = new SoundLooper(loops);

    once(() => {
      SetMemory(SOUND_CLOCK_ADDR, SetTo, CLOCK_START);
      SetMemory(USER_PLAYER_ADDR, SetTo, LOCAL_PLAYER);
      SetMemory(CP_ADDR, SetTo, SAVED_CP);
      looper.play("theme");
      looper.pause();
      looper.resume();
    });
    looper.player();
    emitVarStore(looper.currentBar.id, { addr: SENTINEL + 0 });
    emitVarStore(looper.currentLoop.id, { addr: SENTINEL + 4 });
    emitVarStore(looper.previousLoop.id, { addr: SENTINEL + 16 });
    new Trigger({
      conds: [],
      acts: [SetMemory(0x51ce8c, Add, -TICK_MS)],
      preserved: true,
    });
  });
  const report = runSim(buildJson(doc), 1);
  assert.equal(sentinel(report, 4), 0);
  assert.equal(sentinel(report, 16), 0xffffffff);
  assert.equal(sentinel(report, 0), 1);
});

test("setbar starts at the following bar", () => {
  const report = runSim(buildJson(themeRuntime(1, 1)), 1);
  assert.equal(sentinel(report, 0), 2, "bar 1 set -> next pass plays 2");
  assert.equal(sentinel(report, 8), packBarDigits(2));
});

test("digit carry at ten", () => {
  // 200 ms bars keep the sim short: the digits must still carry at 9 -> 10
  // exactly like the source's decimal increments.
  const doc = runtimeModule(
    () => {
      const loops = new SoundLoops();
      loops.manualAddLoop("ABCDE", 12, 1.0, 0.2, 0.2, 1);
      return loops;
    },
    (_loops, looper) => looper.play("ABCDE"),
  );
  const observed = new Map<number, readonly [number, number]>();
  for (let ticks = 1; ticks < 40; ticks++) {
    const report = runSim(buildJson(doc), ticks);
    observed.set(ticks, [sentinel(report, 0), sentinel(report, 8)]);
  }
  const bars = new Set([...observed.values()].map(([bar]) => bar));
  for (let bar = 1; bar <= 12; bar++) {
    assert.ok(bars.has(bar), `bar ${bar} must be reached`);
  }
  for (const [ticks, [bar, digits]] of observed) {
    if (bar === 0) continue;
    assert.equal(digits, packBarDigits(bar), `loop ${ticks}: bar ${bar} digits`);
  }
  assert.equal(packBarDigits(9), 0x90000);
  assert.equal(packBarDigits(10), 0x100);
});

test("setbar Var argument packs the digits at runtime", () => {
  const doc = runtimeModule(
    () => {
      const loops = new SoundLoops();
      loops.manualAddLoop("ABCDE", 12, 1.0, 1.5, 2.0, 1);
      return loops;
    },
    (_loops, looper) => {
      looper.play("ABCDE", Var(7));
    },
  );
  const report = runSim(buildJson(doc), 1);
  // `setbar(7)` sets the *last played* position; the first due pass advances
  // to bar 8 and packs "008".
  assert.equal(sentinel(report, 8), packBarDigits(8));
  assert.equal(sentinel(report, 0), 8);
});

test("bars slot mirrors the current bar", () => {
  const doc = themeRuntime();
  for (const loops of [1, 5, 11]) {
    const report = runSim(buildJson(doc), loops);
    assert.equal(sentinel(report, 20), sentinel(report, 0));
  }
});

// --- cost -------------------------------------------------------------------

interface DigitsKernel {
  digitsFrom(bar: VarImpl): void;
}

test("digits kernel is two divmod ladders", () => {
  // RT2 pins the isolated digits kernel, not a full due pass: the ticket
  // measured 130T/116C for the four div/mod ladders and 69T/58C for two
  // divmod(10) ladders. The action counts (not part of the ticket's T/C
  // table) are 323 static / 597 executed, down from 374 / 657.
  const doc = moduleJson(() => {
    const looper = new SoundLooper(new SoundLoops());
    // The public `setbar` also emits the progress-slot write, so the cost pin
    // targets the isolated kernel it calls.
    (looper as unknown as DigitsKernel).digitsFrom(Var(123));
  });
  const staticCost = runStatic(buildJson(doc)).static_cost;
  assert.deepEqual(
    [staticCost.triggers, staticCost.conditions],
    [69, 58],
    "two constant-divisor divmod ladders (was 130T/116C)",
  );
  const executed = runSim(buildJson(doc), 1).executed_cost;
  assert.deepEqual(
    [executed.triggers, executed.conditions],
    [75, 58],
    "one due pass of the digits kernel (was 137T/116C)",
  );
});

test("idle loop is two conditions", () => {
  const doc = moduleJson(() => {
    const loops = new SoundLoops();
    loops.manualAddLoop("ABCDE", 2, 1.0, 2.0, 3.0);
    const looper = new SoundLooper(loops);
    looper.player();
  });
  const report = runSim(buildJson(doc), 2);
  const cost = report.executed_cost;
  assert.deepEqual(
    [cost.triggers, cost.conditions, cost.actions],
    [2, 3, 0],
    "idle = probe (2 conds) + the when gate, no clock read",
  );
});

test("static cost is reported", () => {
  const staticCost = runStatic(buildJson(themeRuntime())).static_cost;
  assert.ok(staticCost.triggers > 1);
});
