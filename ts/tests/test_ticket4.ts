import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { fileURLToPath } from "node:url";

import {
  ACT_COUNT,
  DisplayText,
  EudslError,
  Trigger,
  TriggerStruct,
  Var,
  bit,
  buildModule,
  byte,
  defineStruct,
  dword,
  masked,
  word,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");
const BASEMAP_SCX = path.join(
  REPO_ROOT,
  "tests",
  "outputmap",
  "basemap",
  "basemap.scx",
);

test("TriggerStruct schema definition, field types, and 1 <= K <= 64 bounds", () => {
  // Field type constructors and specifiers
  const Hero = TriggerStruct("Hero", {
    hp: dword(),
    mp: word(),
    armor: byte(),
    flags: bit(4),
    custom: masked(0x0f00, 0),
    level: "byte",
  });

  assert.equal(Hero.name, "Hero");
  assert.equal(Hero.fieldCount, 6);
  assert.equal(Hero.fields.hp.mask, 0xffffffff);
  assert.equal(Hero.fields.mp.mask, 0x0000ffff);
  assert.equal(Hero.fields.armor.mask, 0x000000ff);
  assert.equal(Hero.fields.flags.mask, 0x0000000f);
  assert.equal(Hero.fields.custom.mask, 0x00000f00);
  assert.equal(Hero.fields.level.mask, 0x000000ff);

  // K = 0 rejected with EUDSL-BOUNDS
  assert.throws(
    () => {
      TriggerStruct("Empty", {} as any);
    },
    (err: any) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
  );

  // K = 65 rejected with EUDSL-BOUNDS
  const bigSpec: Record<string, string> = {};
  for (let i = 0; i < 65; i++) {
    bigSpec[`f${i}`] = "dword";
  }
  assert.throws(
    () => {
      defineStruct("TooBig", bigSpec as any);
    },
    (err: any) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
  );

  // K = 64 accepted
  const maxSpec: Record<string, string> = {};
  for (let i = 0; i < 64; i++) {
    maxSpec[`f${i}`] = "dword";
  }
  const MaxStruct = defineStruct("MaxStruct", maxSpec as any);
  assert.equal(MaxStruct.fieldCount, 64);
});

test("Direct property access, in-place mutations, conditions, and unknown field error", () => {
  const Unit = TriggerStruct({
    hp: "dword",
    mp: "word",
    armor: "byte",
  });

  buildModule(() => {
    const u = Unit({ hp: 1000, mp: 50, armor: 5 });

    // Direct property access
    assert.equal(typeof u.hp.set, "function");
    assert.equal(typeof u.mp.iadd, "function");
    assert.equal(typeof u.armor.isub, "function");

    // In-place mutations emit actions
    u.hp.set(1200);
    u.mp.iadd(10);
    u.armor.isub(2);

    // Constant comparisons emit single conditions
    const cond1 = u.hp.ge(500);
    assert.equal(cond1.comparison, 0); // AtLeast
    const cond2 = u.mp.eq(60);
    assert.equal(cond2.comparison, 10); // Exactly

    // Unknown field raises loud EUDSL error
    assert.throws(
      () => {
        // @ts-expect-error test invalid field
        const _bad = u.shield;
      },
      (err: any) => err instanceof EudslError && err.code === "EUDSL-UNKNOWN-FIELD",
    );
  });
});

test("Contiguous TriggerPage slot packing without splitting instances across pages", () => {
  const Small = TriggerStruct("Small", {
    a: "dword",
    b: "word",
    c: "byte",
    d: "byte",
  });

  const Wide = TriggerStruct("Wide", {
    w1: "dword",
    w2: "dword",
    w3: "dword",
    w4: "dword",
    w5: "dword",
    w6: "dword",
    w7: "dword",
    w8: "dword",
  });

  buildModule(() => {
    // 1. Pack two 4-field structs into slots 0..3 and 4..7 of Page 0
    const s1 = Small();
    assert.equal(s1.pageIndex, 0);
    assert.equal(s1.slotIndex, 0);
    assert.equal(s1.a.id, 0);
    assert.equal(s1.b.id, 1);
    assert.equal(s1.c.id, 2);
    assert.equal(s1.d.id, 3);

    const s2 = Small();
    assert.equal(s2.pageIndex, 0);
    assert.equal(s2.slotIndex, 4);
    assert.equal(s2.a.id, 4);
    assert.equal(s2.b.id, 5);
    assert.equal(s2.c.id, 6);
    assert.equal(s2.d.id, 7);

    // 2. Allocate 52 standalone scalar Vars. These are cells, not page
    // slots: at lowering they become 2 BitCells (Var(0), Var(1)) and
    // 50 DwordCells, so only the 8 page slots of s1/s2 are used on Page 0.
    // The next var id is 60, but the next page slot is 8.
    for (let i = 0; i < 52; i++) {
      Var(i);
    }

    // Wide struct needs 8 contiguous slots, and 8 + 8 = 16 <= 64, so it
    // fits on Page 0 without padding and must NOT split across pages.
    // It packs at pinned ranks 8..15 (page 0, slots 8..15), with ids 60..67.
    const w = Wide();
    assert.equal(w.pageIndex, 0);
    assert.equal(w.slotIndex, 8);
    assert.equal(w.w1.id, 60);
    assert.equal(w.w8.id, 67);
  });
});

test("Hardware bitmasks, sub-dword masking, cross-mask transfers, and dirty dwords", () => {
  const Hero = TriggerStruct("Hero", {
    hp: dword(),
    mp: word(),
    armor: byte(),
    flags: bit(4),
  });

  const tsMod = buildModule(() => {
    const s1 = Hero({ hp: 1000, mp: 0x1234, armor: 0xab, flags: 0x0d });
    const s2 = Hero({ hp: 0, mp: 0, armor: 0, flags: 0 });
    const raw = Var(0x12345678);
    const outArmor = Var(0xdead0000);

    // 1) Whole-struct copy s2.set(s1)
    s2.set(s1);

    // 2) Cross-mask transfer: s2.armor.set(raw) -> masks 0x12345678 to 0x78
    s2.armor.set(raw);

    // 3) Read sub-dword field into dirty dword: outArmor.set(s2.armor) -> produces 0x78
    outArmor.set(s2.armor);
  });

  const report = runSim(tsMod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);
  // s2 fields at vars 4..7: hp=1000, mp=0x1234, armor=0x78, flags=0x0D
  assert.equal(report.var_values[4], 1000);
  assert.equal(report.var_values[5], 0x1234);
  assert.equal(report.var_values[6], 0x78);
  assert.equal(report.var_values[7], 0x0d);
  // outArmor at var 9: 0x78 (dirty 0xDEAD0000 cleared by full dword target mask)
  assert.equal(report.var_values[9], 0x78);
});

test("1T whole-struct copy fires source TriggerPage exactly once for 64 fields", () => {
  const spec: Record<string, string> = {};
  for (let i = 0; i < 64; i++) {
    spec[`f${i}`] = "dword";
  }
  const FullStruct = TriggerStruct("FullStruct", spec as any);

  const tsMod = buildModule(() => {
    const inits: Record<string, number> = {};
    for (let i = 0; i < 64; i++) {
      inits[`f${i}`] = 500 + i;
    }
    const src = FullStruct(inits);
    const dst = FullStruct();

    // Whole-struct copy dst.copyFrom(src)
    dst.copyFrom(src);
  });

  const report = runSim(tsMod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);

  // Source page (page 0) must be visited ONLY ONCE during whole-struct copy!
  const page0Item = report.static_cost.triggers - 2; // num_exec + 0
  const page0Visits = report.executed_cost.visited_items.filter((i) => i === page0Item).length;
  assert.equal(page0Visits, 1);

  // Verify all 64 fields copied correctly
  for (let i = 0; i < 64; i++) {
    assert.equal(report.var_values[64 + i], 500 + i);
  }
});

test("Byte-identical HIR parity between TypeScript and Python for TriggerStruct", () => {
  const Hero = TriggerStruct("Hero", {
    hp: dword(),
    mp: word(),
    armor: byte(),
    flags: bit(4),
  });

  const tsMod = buildModule(() => {
    const s1 = Hero({ hp: 1000, mp: 0x1234, armor: 0xab, flags: 0x0d });
    const s2 = Hero();
    const outArmor = Var(0);

    s2.set(s1);
    s2.armor.iadd(5);
    outArmor.set(s2.armor);

    new Trigger({
      conds: [s2.hp.ge(500), s2.mp.eq(0x1234), s2.armor.eq(0xb0)],
      acts: [DisplayText("Ticket 4 Parity!")],
    });
  });

  const pyScript = `
from py import DisplayText, Trigger, TriggerStruct, Var, bit, byte, dword, word, buildModule

Hero = TriggerStruct(
    "Hero",
    hp=dword(),
    mp=word(),
    armor=byte(),
    flags=bit(4),
)

def prog():
    s1 = Hero(hp=1000, mp=0x1234, armor=0xAB, flags=0x0D)
    s2 = Hero()
    out_armor = Var(0)

    s2.set(s1)
    s2.armor += 5
    out_armor.set(s2.armor)

    Trigger(
        conds=[s2.hp >= 500, s2.mp == 0x1234, s2.armor == 0xB0],
        acts=[DisplayText("Ticket 4 Parity!")],
    )

print(buildModule(prog).to_json(), end="")
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  const tsHirJson = tsMod.toJson();
  assert.equal(tsHirJson, pyHirJson);

  const report = runSim(tsHirJson);
  assert.equal(report.self_targeted_invariant_holds, true);
  assert.equal(report.var_values[4], 1000);
  assert.equal(report.var_values[5], 0x1234);
  assert.equal(report.var_values[6], 0xb0);
  assert.equal(report.var_values[7], 0x0d);
  assert.equal(report.var_values[8], 0xb0);

  // Also verify map compilation
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-scx4-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    const outScx = path.join(tmpDir, "out.scx");
    fs.writeFileSync(hirPath, tsHirJson, "utf-8");
    execFileSync(EUDKIT_COMPILE, ["map", hirPath, BASEMAP_SCX, outScx], {
      encoding: "utf-8",
    });
    assert.ok(fs.existsSync(outScx));
    assert.ok(fs.statSync(outScx).size > 0);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
