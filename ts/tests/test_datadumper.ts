/**
 * The data dumper core helper (issue #114): a port of eudplib's
 * `_add_datadumper` / `_f_datadumper` (`eudlib/utilf/datadumper.py`) and
 * euddraft's `plugins/dataDumper.py`. Mirror of
 * `py/tests/test_datadumper.py`.
 *
 * The helper injects a raw byte payload as a module data blob (issue #99's
 * `Db`) and teaches the target memory about it:
 *
 * - default / `unpatchable`: `SetMemory(outOffset, SetTo, db)` per target,
 *   inside a `once` gate (the source's `onPluginStart`);
 * - `copy`: each target holds the destination's *byte address*; the helper
 *   reads it at run time (converting to an EPD like eudplib's `f_epdread_epd`)
 *   and copies the payload there with `memcpyEPD`, `(len + 3) / 4` dwords.
 *
 * What is pinned here: the one-blob-per-call layout and its dword padding, the
 * emitted HIR for each mode, the flag/argument validation, the extended
 * `memcpyEPD` blob source, and a simulated `copy` run proving the bytes land
 * at the destination. Cross-frontend byte-identical HIR and the lowered
 * runtime proof live in `crates/eudkit-lower/tests/datadumper_parity.rs`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  Db,
  EPD,
  EPD_CURRENT_PLAYER,
  EudslError,
  FLAG_COPY,
  FLAG_UNPATCHABLE,
  SetMemory,
  SetTo,
  Subtract,
  buildModule,
  dataDumper,
  memcpyEPD,
  once,
  readEpd,
  readEpdPointer,
} from "../index.ts";
import { CP_ADDR } from "../cp.ts";
import { EPD_BASE, readDword, runSim, runStatic } from "./helpers.ts";

/** eudplib's own test copies a `RequireData` file to the address stored here. */
const PTR = 0x58d740;
/** An external scratch address (`copy` writes the payload here at run time). */
const DEST = 0x58e000;
const PAYLOAD = new TextEncoder().encode("ABCDEFGHIJ");
const DWORDS = 3; // ceil(10 / 4)

const ACT_TYPE_SET_MEMORY = 45;
const ACT_DEST_OFFSET = 16;
const ACT_VALUE_OFFSET = 20;
/** Byte offsets of the copy trigger's destination/value fields inside its page. */
const TRIGGER_DEST_BYTE_OFFSET = 344;
const TRIGGER_VALUE_BYTE_OFFSET = 348;

type ActNode = {
  kind?: string;
  acttype?: number;
  modifier?: number;
  dest?: number;
  value?: number;
  relocs?: Array<Record<string, number>>;
};
type StmtNode = {
  kind: string;
  id?: number;
  conds?: Array<Record<string, number>>;
  acts?: ActNode[];
  body?: StmtNode[];
  func?: number;
  dst_var?: number;
  src_var?: number;
  modifier?: number;
  dst_trigger?: number;
  dst_offset?: number;
  dst_addr?: number;
};

type FuncNode = {
  id: number;
  body: StmtNode[];
};

function epd(addr: number): number {
  return Math.floor((addr - EPD_BASE) / 4) >>> 0;
}

function moduleOf(body: () => void): {
  blobs?: Array<{ size: number; data?: string }>;
  stmts: StmtNode[];
  funcs?: FuncNode[];
} {
  return JSON.parse(buildModule(body).toJson()).module;
}

function varReloc(varId: number, fieldOffset = ACT_DEST_OFFSET) {
  return {
    field_offset: fieldOffset,
    target_var: varId,
    target_offset: ACT_VALUE_OFFSET,
    mode: 1,
  };
}

function blobReloc(offset: number, mode: number) {
  return {
    field_offset: offset,
    target_blob: 0,
    target_offset: 0,
    mode,
  };
}

test("flag names match the source", () => {
  assert.equal(FLAG_COPY, "copy");
  assert.equal(FLAG_UNPATCHABLE, "unpatchable");
});

// The shared `readEpd` / `readEpdPointer` primitives. The pointer read is
// eudplib's `f_epdread_epd`: the reset is owned by the read site, one bit test
// per set bit of `0xFFFFFFFC`, then the one `EPD_BASE / 4` subtraction. Both
// helpers live in `view` so the data dumper and the collision scrubber share
// one body.
test("shared readEpd resets then tests the masked bits", () => {
  const mod = moduleOf(() => {
    readEpd(epd(PTR), 30, 2, 0xfffffffc);
  });
  const stmts = mod.stmts;

  assert.equal(stmts.length, 31);
  const reset = stmts[0]!;
  assert.equal(reset.kind, "trigger");
  assert.equal(reset.acts![0]!.value, 0);
  assert.deepEqual(reset.acts![0]!.relocs, [varReloc(0)]);

  for (let bit = 2; bit < 32; bit += 1) {
    const trigger = stmts[1 + bit - 2]!;
    assert.equal(trigger.kind, "trigger");
    const cond = trigger.conds![0]!;
    assert.equal(cond.player, epd(PTR));
    assert.equal(cond.amount, 2 ** bit);
    assert.equal(trigger.acts![0]!.modifier, Add);
    assert.equal(trigger.acts![0]!.value, 2 ** (bit - 2));
  }
});

test("shared readEpdPointer subtracts the EPD base", () => {
  const mod = moduleOf(() => {
    once(() => {
      readEpdPointer(epd(PTR));
    });
  });
  const stmts = mod.stmts[0]!.body!;

  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    [...Array(31).fill("trigger"), "act"],
  );
  const subtract = stmts[31]! as ActNode;
  assert.equal(subtract.modifier, Add);
  assert.equal(subtract.value, (0x100000000 - (EPD_BASE >>> 2)) >>> 0);
  assert.deepEqual(subtract.relocs, [varReloc(0)]);
});

test("default mode writes the blob address into the target", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR]));

  // One blob per call, padded to a whole dword so the `copy` mode's last
  // rep-movsd dword stays in-payload.
  assert.deepEqual(mod.blobs, [
    { size: DWORDS * 4, data: "QUJDREVGR0hJSgAA" },
  ]);

  assert.equal(mod.stmts.length, 1);
  const once = mod.stmts[0]!;
  assert.equal(once.kind, "once");
  assert.deepEqual(once.conds, []);
  assert.equal(once.body!.length, 1);
  const act = once.body![0]! as ActNode;
  assert.equal(act.kind, "act");
  assert.equal(act.acttype, ACT_TYPE_SET_MEMORY);
  assert.equal(act.modifier, SetTo);
  assert.equal(act.dest, epd(PTR));
  assert.equal(act.value, 0);
  assert.deepEqual(act.relocs, [blobReloc(ACT_VALUE_OFFSET, 4)]);
});

test("every target shares the one blob", () => {
  const second = 0x58d744;
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR, second]));

  assert.equal(mod.blobs!.length, 1);
  const acts = mod.stmts[0]!.body! as ActNode[];
  assert.equal(acts.length, 2);
  assert.deepEqual(
    acts.map((act) => act.dest),
    [epd(PTR), epd(second)],
  );
  assert.deepEqual(
    acts.map((act) => act.relocs![0]!.target_blob),
    [0, 0],
  );
});

test("dword-aligned payload is not padded", () => {
  const mod = moduleOf(() => dataDumper(new TextEncoder().encode("ABCD"), [PTR]));
  assert.deepEqual(mod.blobs, [{ size: 4, data: "QUJDRA==" }]);
});

test("unpatchable mode emits the default write", () => {
  // eudkit never patches memory at build time, so `unpatchable` is the same
  // run-time write as the default mode (documented deviation).
  const defaultMod = moduleOf(() => dataDumper(PAYLOAD, [PTR]));
  const unpatchable = moduleOf(() =>
    dataDumper(PAYLOAD, [PTR], [FLAG_UNPATCHABLE]),
  );
  assert.deepEqual(defaultMod, unpatchable);
});

test("copy mode: blob is padded to whole dwords", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  assert.deepEqual(mod.blobs, [
    { size: DWORDS * 4, data: "QUJDREVGR0hJSgAA" },
  ]);
});

test("copy mode: once body is read, then seed, then copy loop", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  const once = mod.stmts[0]!;
  assert.equal(once.kind, "once");
  assert.deepEqual(once.conds, []);
  // `f_epdread_epd`: one accumulator reset + one bit test per bit 2..31, then
  // the EPD_BASE subtraction, the counter setup, the blob-cursor seed trigger,
  // the gated destination/cursor seed and the loop.
  assert.equal(once.body!.length, 36);
  assert.deepEqual(
    once.body!.map((stmt) => stmt.kind),
    [...Array(31).fill("trigger"), "act", "act", "trigger", "when", "while"],
  );
});

test("copy mode: the destination read tests bits two and up", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  const body = mod.stmts[0]!.body!;

  const reset = body[0]!.acts![0]!;
  assert.equal(reset.value, 0);
  assert.deepEqual(reset.relocs, [varReloc(0)]);

  // The read result is variable 0; bit `bit` of the dword at PTR adds
  // `1 << (bit - 2)`, i.e. the read accumulates `D >> 2`. Bits 0-1 are
  // dropped because the dword holds a dword-aligned byte address.
  for (let bit = 2; bit < 32; bit += 1) {
    const trigger = body[1 + bit - 2]!;
    assert.equal(trigger.kind, "trigger");
    const cond = trigger.conds![0]!;
    assert.equal(cond.condtype, 15); // Deaths / Memory wire form
    assert.equal(cond.player, epd(PTR));
    assert.equal(cond.amount, 2 ** bit);
    assert.equal(cond.comparison, 0); // AtLeast
    assert.equal(trigger.acts![0]!.modifier, Add);
    assert.equal(trigger.acts![0]!.value, 2 ** (bit - 2));
    assert.deepEqual(trigger.acts![0]!.relocs, [varReloc(0)]);
  }
});

test("copy mode: the EPD_BASE subtraction turns the read into an EPD", () => {
  // `EPD(D) = (D - 0x58A364) / 4` is `(D >> 2) - (0x58A364 >> 2)`, with
  // wrapping subtraction emitted as an add of the two's complement.
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  const subtract = mod.stmts[0]!.body![31]! as ActNode;
  assert.equal(subtract.kind, "act");
  assert.equal(subtract.modifier, Add);
  assert.equal(subtract.value, (0x100000000 - (EPD_BASE >>> 2)) >>> 0);
  assert.deepEqual(subtract.relocs, [varReloc(0)]);
});

test("copy mode: the blob seeds the source cursor through a mode-1 reloc", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  // Cycle 27 #01: the blob EPD is only known at assembly time, so the source
  // cursor stays a var (id 3) seeded by a mode-1 blob relocation; the
  // destination seed rides in the same trigger's dest field.
  const seed = mod.stmts[0]!.body![33]!.acts![0]!;
  assert.equal(seed.modifier, SetTo);
  assert.deepEqual(seed.relocs, [varReloc(3), blobReloc(ACT_VALUE_OFFSET, 1)]);
});

test("copy mode: destination and count are seeded before the loop", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  const body = mod.stmts[0]!.body!;
  // Cycle 27 #01: the counter is a real dword var seeded before the loop,
  // while the destination/cursor seed runs inside a `when (cnt >= 1)` gate so
  // a runtime zero count leaves CP and the scratch destination untouched.
  const count = body[32]! as ActNode;
  assert.equal(count.kind, "act");
  assert.equal(count.value, DWORDS);
  assert.deepEqual(count.relocs, [varReloc(1)]);

  const gate = body[34]!;
  assert.equal(gate.kind, "when");
  assert.deepEqual(gate.conds![0]!.relocs, [varReloc(1, 4)]);
  const [destination, cursor, decrement] = gate.body!;
  // The EPD read result (var 0) seeds the copy trigger's dest field.
  assert.equal(destination!.kind, "varstore");
  assert.equal(destination!.src_var, 0);
  assert.equal(destination!.dst_offset, TRIGGER_DEST_BYTE_OFFSET);
  // The blob cursor (var 3) lands in CurrentPlayer, stepped back one dword
  // because the loop advances CP before every read.
  assert.equal(cursor!.kind, "varstore");
  assert.equal(cursor!.src_var, 3);
  assert.equal(cursor!.dst_addr, CP_ADDR);
  assert.equal(decrement!.kind, "act");
  assert.equal((decrement as ActNode).modifier, Subtract);
  assert.equal((decrement as ActNode).dest, EPD_CURRENT_PLAYER);
  assert.equal((decrement as ActNode).value, 1);
});

test("copy mode: the loop copies one dword per pass", () => {
  const mod = moduleOf(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY]));
  const loop = mod.stmts[0]!.body![35]!;
  assert.equal(loop.kind, "while");
  const cond = loop.conds![0]!;
  assert.equal(cond.comparison, 0); // AtLeast
  assert.equal(cond.amount, 1);
  assert.deepEqual(cond.relocs, [varReloc(1, 4)]);
  // #97: the source dword is read through the shared reader body.
  // Cycle 27 #01: the pass advances CurrentPlayer, reads, copies, seeds the
  // trigger's value field and moves the dest field in real memory.
  assert.deepEqual(
    loop.body!.map((stmt) => stmt.kind),
    ["act", "call", "vartransfer", "varstore", "trigger", "act", "act"],
  );
  const call = loop.body!.find((stmt) => stmt.kind === "call")!;
  const reader = mod.funcs!.find((func) => func.id === call.func)!;
  assert.equal(reader.body!.length, 33);
  // The copied dword lands in the copy trigger's value field.
  const valueStore = loop.body![3]!;
  assert.equal(valueStore.kind, "varstore");
  assert.equal(valueStore.src_var, 2);
  assert.equal(valueStore.dst_offset, TRIGGER_VALUE_BYTE_OFFSET);
  // dest field += 1, counter -= 1 (masked add of -1); the source cursor is
  // CurrentPlayer itself, so it moves with the EPD read.
  const [advanceDest, decrement] = loop.body!.slice(-2) as ActNode[];
  assert.equal(advanceDest!.value, 1);
  assert.deepEqual(advanceDest!.relocs, [
    {
      field_offset: ACT_DEST_OFFSET,
      target_trigger: 32,
      target_offset: TRIGGER_DEST_BYTE_OFFSET,
      mode: 1,
    },
  ]);
  assert.equal(decrement!.value, 0xffffffff);
  assert.deepEqual(decrement!.relocs, [varReloc(1)]);
});

test("copy mode runtime: payload lands at the address stored in the target", () => {
  const mod = buildModule(() => {
    // Seed the target dword with a *byte address*, as the game does (e.g. the
    // `stat_txt.tbl` pointer at 0x58D740). The `copy` mode reads it at run
    // time and converts it to its EPD.
    SetMemory(PTR, SetTo, DEST);
    dataDumper(PAYLOAD, [PTR], [FLAG_COPY]);
  });
  const report = runSim(mod.toJson(), 1);
  const start = epd(DEST);
  const words = [0, 1, 2].map((index) => readDword(report, start + index));
  const copied = Buffer.from(
    words.flatMap((word) => [
      word & 0xff,
      (word >>> 8) & 0xff,
      (word >>> 16) & 0xff,
      (word >>> 24) & 0xff,
    ]),
  );
  assert.deepEqual([...copied], [...PAYLOAD, 0, 0]);
});

test("copy mode runtime: low address destination wraps like EPD", () => {
  const mod = buildModule(() => {
    SetMemory(PTR, SetTo, 0x80000);
    dataDumper(PAYLOAD, [PTR], [FLAG_COPY]);
  });
  const report = runSim(mod.toJson(), 1);
  const start = ((0x80000 - EPD_BASE) / 4) >>> 0;
  const words = [0, 1, 2].map((index) => readDword(report, start + index));
  const copied = Buffer.from(
    words.flatMap((word) => [
      word & 0xff,
      (word >>> 8) & 0xff,
      (word >>> 16) & 0xff,
      (word >>> 24) & 0xff,
    ]),
  );
  assert.deepEqual([...copied], [...PAYLOAD, 0, 0]);
});

test("copy mode runtime: each target reads its own destination", () => {
  const other = 0x58e100;
  const mod = buildModule(() => {
    SetMemory(PTR, SetTo, DEST);
    SetMemory(PTR + 4, SetTo, other);
    dataDumper(PAYLOAD, [PTR, PTR + 4], [FLAG_COPY]);
  });
  const report = runSim(mod.toJson(), 1);
  for (const dest of [DEST, other]) {
    const start = epd(dest);
    const words = [0, 1, 2].map((index) => readDword(report, start + index));
    const copied = Buffer.from(
      words.flatMap((word) => [
        word & 0xff,
        (word >>> 8) & 0xff,
        (word >>> 16) & 0xff,
        (word >>> 24) & 0xff,
      ]),
    );
    assert.deepEqual([...copied], [...PAYLOAD, 0, 0], dest.toString(16));
  }
});

test("default mode runtime: the target holds the blob address", () => {
  const report = runSim(
    buildModule(() => dataDumper(PAYLOAD, [PTR])).toJson(),
    1,
  );
  // The stored value is the blob's payload address (2408 = one 2,408-byte
  // trigger page, the only item before it).
  assert.equal(readDword(report, epd(PTR)), 2408);
});

test("static cost of one payload copy", () => {
  const staticCost = runStatic(
    buildModule(() => dataDumper(PAYLOAD, [PTR], [FLAG_COPY])).toJson(),
  ).static_cost;
  assert.deepEqual(
    [staticCost.triggers, staticCost.conditions, staticCost.actions],
    // 31 EPD-read triggers + counter setup + blob-cursor seed trigger + the
    // gated destination seed + the shared-reader loop. Cycle 27 #01's pointer
    // cursors trade two per-pass actions for the seed trigger/condition.
    [77, 64, 150],
  );
  // Payload plus two cell dwords: the once latch/cursor dword and the
  // reader-result + call-destination bit cells (#97).
  assert.equal(staticCost.blob_bytes, DWORDS * 4 + 8);
});

test("empty offset list allocates nothing", () => {
  assert.equal(dataDumper(PAYLOAD, []), undefined);
  assert.deepEqual(moduleOf(() => dataDumper(PAYLOAD, [])).blobs ?? [], []);
});

test("empty payload is rejected", () => {
  assert.throws(
    () => dataDumper(new Uint8Array(0), [PTR]),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-RANGE",
  );
});

test("payload must be bytes", () => {
  assert.throws(
    // @ts-expect-error — exercise the runtime guard.
    () => dataDumper("ABCD", [PTR]),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-TYPE",
  );
});

test("offsets must be an array", () => {
  assert.throws(
    // @ts-expect-error — exercise the runtime guard.
    () => dataDumper(PAYLOAD, PTR),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-TYPE",
  );
  assert.throws(
    // @ts-expect-error — exercise the runtime guard.
    () => dataDumper(PAYLOAD, "0x58D740"),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-TYPE",
  );
});

test("offsets must be positive dword addresses", () => {
  for (const bad of [0, -4, 0x58d742, 0x58d741, 0x100000000]) {
    assert.throws(
      () => dataDumper(PAYLOAD, [bad]),
      (error: unknown) =>
        error instanceof EudslError && error.code === "EUDSL-BOUNDS",
      `offset ${bad}`,
    );
  }
});

test("flags must be known names", () => {
  assert.throws(
    () => dataDumper(PAYLOAD, [PTR], ["bogus"]),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-RANGE",
  );
  assert.throws(
    () => dataDumper(PAYLOAD, [PTR], "copy"),
    (error: unknown) => error instanceof EudslError && error.code === "EUDSL-TYPE",
  );
});

test("copy and unpatchable are mutually exclusive", () => {
  assert.throws(
    () => dataDumper(PAYLOAD, [PTR], [FLAG_COPY, FLAG_UNPATCHABLE]),
    (error: unknown) =>
      error instanceof EudslError && error.code === "EUDSL-BOUNDS",
  );
});

test("memcpyEPD blob source seeds the cursor through a relocation", () => {
  const mod = moduleOf(() => memcpyEPD(PTR, new Db(new TextEncoder().encode("ABCDEFGH")), 2));
  // Cycle 27 #01: the counter setup comes first (a plain var store), then the
  // blob-cursor seed trigger; the cursor var (id 2) is seeded through a
  // mode-1 relocation because the blob EPD is only known at assembly time.
  const seed = mod.stmts[1]!;
  assert.equal(seed.kind, "trigger");
  assert.deepEqual(seed.conds, []);
  assert.equal(seed.acts!.length, 1);
  assert.deepEqual(seed.acts![0]!.relocs, [
    varReloc(2),
    blobReloc(ACT_VALUE_OFFSET, 1),
  ]);
  assert.deepEqual(mod.blobs, [{ size: 8, data: "QUJDREVGR0g=" }]);
});

test("memcpyEPD constant source keeps the old emission", () => {
  const mod = moduleOf(() => memcpyEPD(PTR, EPD(PTR), 2));
  assert.deepEqual(mod.blobs ?? [], []);
  for (const stmt of mod.stmts) {
    for (const act of stmt.acts ?? []) {
      for (const reloc of act.relocs ?? []) {
        assert.equal(reloc.target_blob, undefined);
      }
    }
  }
});
