import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ACT_COUNT,
  Add,
  Always,
  AtLeast,
  COND_COUNT,
  Deaths,
  DeathsX,
  DisplayText,
  EPD,
  EudslError,
  Exactly,
  Memory,
  P1,
  SetDeaths,
  SetDeathsX,
  SetMemory,
  SetMemoryX,
  SetNextPtr,
  SetTo,
  TRIGGER_SIZE_BYTES,
  TRIGGER_STRIDE_EPDS,
  Trigger,
  buildModule,
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

test("Trigger slot inspection and 16C/64A geometry", () => {
  const mod = buildModule(() => {
    const t0 = new Trigger({
      conds: [Always(), DeathsX(P1, Exactly, 42, 0, 0xff00ff00)],
      acts: [
        SetDeathsX(EPD(0x58a368), SetTo, 0x12345678, 0, 0x00ff00ff),
        DisplayText("Hello from TypeScript!"),
      ],
      preserved: true,
    });

    assert.equal(t0.prev.byteOffset, 0);
    assert.equal(t0.prev.epdOffset, 0);
    assert.equal(t0.next.byteOffset, 4);
    assert.equal(t0.next.epdOffset, 1);
    assert.equal(t0.flagsField.byteOffset, 2376);

    const c0 = t0.cond(0);
    assert.equal(c0.byteOffset, 8);
    assert.equal(c0.epdOffset, 2);
    assert.equal(c0.condtype.initialValue, 22);

    const c1 = t0.cond(1);
    assert.equal(c1.byteOffset, 28);
    assert.equal(c1.epdOffset, 7);
    assert.equal(c1.mask.byteOffset, 28);
    assert.equal(c1.mask.initialValue, 0xff00ff00);
    assert.equal(c1.player.byteOffset, 32);
    assert.equal(c1.amount.byteOffset, 36);
    assert.equal(c1.amount.initialValue, 42);
    assert.equal(c1.comparison.byteOffset, 42);
    assert.equal(c1.condtype.byteOffset, 43);
    assert.equal(c1.eudx.byteOffset, 46);
    assert.equal(c1.eudx.initialValue, 0x4353);

    const c15 = t0.cond(15);
    assert.equal(c15.byteOffset, 8 + 15 * 20);
    assert.equal(c15.epdOffset, 2 + 15 * 5);

    const a0 = t0.act(0);
    assert.equal(a0.byteOffset, 328);
    assert.equal(a0.epdOffset, 82);
    assert.equal(a0.mask.byteOffset, 328);
    assert.equal(a0.mask.initialValue, 0x00ff00ff);
    assert.equal(a0.dest.byteOffset, 344);
    assert.equal(a0.dest.initialValue, 1);
    assert.equal(a0.value.byteOffset, 348);
    assert.equal(a0.value.initialValue, 0x12345678);
    assert.equal(a0.acttype.byteOffset, 354);
    assert.equal(a0.acttype.initialValue, 45);
    assert.equal(a0.modifier.byteOffset, 355);
    assert.equal(a0.modifier.initialValue, 7);
    assert.equal(a0.flags.byteOffset, 356);
    assert.equal(a0.flags.initialValue, 20);
    assert.equal(a0.eudx.byteOffset, 358);
    assert.equal(a0.eudx.initialValue, 0x4353);

    const a63 = t0.act(63);
    assert.equal(a63.byteOffset, 328 + 63 * 32);
    assert.equal(a63.epdOffset, 82 + 63 * 8);
  });

  assert.equal(mod.triggers.length, 1);
  assert.equal(TRIGGER_SIZE_BYTES, 2408);
  assert.equal(TRIGGER_STRIDE_EPDS, 602);
});

test("Trigger bounds errors raise loud EUDSL-BOUNDS", () => {
  assert.throws(
    () =>
      new Trigger({
        conds: Array.from({ length: COND_COUNT + 1 }, () => Always()),
        acts: [],
      }),
    (err: unknown) =>
      err instanceof EudslError &&
      err.code === "EUDSL-BOUNDS" &&
      err.message.includes("EUDSL-BOUNDS"),
  );

  assert.throws(
    () =>
      new Trigger({
        conds: [Always()],
        acts: Array.from({ length: ACT_COUNT + 1 }, (_, i) =>
          SetDeaths(P1, SetTo, i, 0),
        ),
      }),
    (err: unknown) =>
      err instanceof EudslError &&
      err.code === "EUDSL-BOUNDS" &&
      err.message.includes("EUDSL-BOUNDS"),
  );

  const t = new Trigger({
    conds: [Always()],
    acts: [SetDeaths(P1, SetTo, 1, 0)],
  });
  for (const badCond of [-1, 16]) {
    assert.throws(
      () => t.cond(badCond),
      (err: unknown) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
    );
  }
  for (const badAct of [-1, 64]) {
    assert.throws(
      () => t.act(badAct),
      (err: unknown) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
    );
  }
});

test("Top-level unconditional actions bin-pack up to 64 per Trigger", () => {
  const mod = buildModule(() => {
    for (let i = 0; i < 130; i++) {
      SetDeaths(P1, Add, i, 0);
    }
  });
  assert.equal(mod.triggers.length, 3);
  assert.deepEqual(
    mod.triggers.map((t) => t.acts.length),
    [64, 64, 2],
  );
});

test("Byte-identical HIR 3.0 parity with Python DSL and e2e .scx compilation", () => {
  const tsMod = buildModule(() => {
    const t0 = new Trigger({
      conds: [Always(), Deaths(P1, AtLeast, 1, 0)],
      acts: [
        SetMemory(0x58a364, SetTo, 100),
        SetMemoryX(0x58a368, SetTo, 0xabcd, 0xffff),
        DisplayText("Tracer bullet SCX test"),
      ],
      preserved: true,
    });
    new Trigger({
      conds: [Memory(0x58a364, Exactly, 100)],
      acts: [SetDeaths(t0.act(0).value, SetTo, 200, 0), SetNextPtr(t0, t0)],
      preserved: true,
    });
  });

  const pyScript = `
from py import (
    Always, AtLeast, Deaths, DisplayText, Exactly, Memory, P1,
    SetDeaths, SetMemory, SetMemoryX, SetNextPtr, SetTo, Trigger, buildModule
)
def prog():
    t0 = Trigger(
        conds=[Always(), Deaths(P1, AtLeast, 1, 0)],
        acts=[
            SetMemory(0x58A364, SetTo, 100),
            SetMemoryX(0x58A368, SetTo, 0xABCD, 0xFFFF),
            DisplayText("Tracer bullet SCX test"),
        ],
        preserved=True,
    )
    Trigger(
        conds=[Memory(0x58A364, Exactly, 100)],
        acts=[
            SetDeaths(t0.act(0).value, SetTo, 200, 0),
            SetNextPtr(t0, t0),
        ],
        preserved=True,
    )
print(buildModule(prog).to_json(), end="")
`;
  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  const tsHirJson = tsMod.toJson();
  assert.equal(tsHirJson, pyHirJson, "TS and Python must emit byte-identical HIR 3.0 JSON");

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-test-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    const outScx = path.join(tmpDir, "out.scx");
    fs.writeFileSync(hirPath, tsHirJson, "utf-8");

    const lirJson = execFileSync(EUDKIT_COMPILE, ["lower", hirPath], {
      encoding: "utf-8",
    }).trim();
    const lirDoc = JSON.parse(lirJson);
    assert.equal(lirDoc.version, "3.0");
    assert.equal(lirDoc.module.items.length, 2);
    for (const item of lirDoc.module.items) {
      assert.equal(item.kind, "trigger");
      assert.equal(item.size, 2408);
    }

    const costJson = execFileSync(
      EUDKIT_COMPILE,
      ["map", hirPath, BASEMAP_SCX, outScx],
      { encoding: "utf-8" },
    ).trim();
    const cost = JSON.parse(costJson);
    // 2 program triggers + the 35-trigger once-per-tick gate. The gate
    // contributes 33 conditions (branch + 32 bit copies) and 35 actions.
    assert.deepEqual(cost, {
      triggers: 37,
      conditions: 36,
      actions: 40,
      trigger_bytes: 37 * 2408,
      blob_bytes: 0,
      total_bytes: 37 * 2408,
    });
    assert.ok(fs.existsSync(outScx));
    assert.ok(fs.statSync(outScx).size > 0);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
