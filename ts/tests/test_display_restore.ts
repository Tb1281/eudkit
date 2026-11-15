// displayAt relative-offset and cursor restore (eudplib parity, issue #44 resolved).
//
// The sim executes SetDeaths acts only and does NOT model the game's
// synchronous DisplayText line increment, so the sim-visible pointer after a
// displayAt is the RESTORE value. displayAt restores the saved pointer
// verbatim, making the call transparent to the line pointer: the pointer
// after the call equals the pointer before it.
import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  SetMemory,
  SetTo,
  FixedString,
  Var,
  buildModule,
  once,
} from "../index.ts";

const EPD_BASE = 0x58a364;
const DISPLAY_LINE_ADDR = 0x640b58;
const DISPLAY_LINE_EPD = (DISPLAY_LINE_ADDR - EPD_BASE) >>> 2;

interface SimReport {
  external_epds: Record<string, number>;
}

function readDword(report: SimReport, epd: number): number {
  return report.external_epds[`${epd}`] ?? 0;
}

const RESTORE_CASES: Array<[number, number]> = [
  [0, 0],
  [1, 1],
  [5, 5],
  [11, 11],
  [15, 15],
];

for (const [saved, restored] of RESTORE_CASES) {
  test(`displayAt restores saved verbatim in sim (saved=${saved} -> ${restored})`, () => {
    const mod = buildModule(() => {
      const s = new FixedString(8);
      s.write("hi");
      SetMemory(DISPLAY_LINE_ADDR, SetTo, saved);
      s.displayAt(3);
    });
    const report = runSim(mod.toJson(), 3);
    assert.equal(
      readDword(report, DISPLAY_LINE_EPD) >>> 0,
      restored >>> 0,
      `sim-visible restore must equal saved=${saved} (displayAt is transparent to the line pointer)`,
    );
  });
}

test("two displayAt calls chain: sim pointer stays saved", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("hi");
    SetMemory(DISPLAY_LINE_ADDR, SetTo, 5);
    s.displayAt(3);
    s.displayAt(4);
  });
  const report = runSim(mod.toJson(), 3);
  assert.equal(
    readDword(report, DISPLAY_LINE_EPD),
    5,
    "second displayAt saves the first restore (5) and restores it unchanged; displayAt neither accumulates nor leaks",
  );
});

test("printAt shares the displayAt restore (saved verbatim)", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    SetMemory(DISPLAY_LINE_ADDR, SetTo, 7);
    s.printAt(3, "x");
  });
  const report = runSim(mod.toJson(), 3);
  assert.equal(
    readDword(report, DISPLAY_LINE_EPD),
    7,
    "printAt delegates to displayAt, so it restores saved verbatim too",
  );
});

test("displayAt+display across loops: pointer transparent, no per-loop drift", () => {
  const mod = buildModule(() => {
    const a = new FixedString(8);
    const b = new FixedString(8);
    once(() => {
      SetMemory(DISPLAY_LINE_ADDR, SetTo, 5);
    });
    a.write("AAA");
    b.write("BBB");
    a.displayAt(1);
    b.display();
  });
  const report = runSim(mod.toJson(), 4);
  assert.equal(
    readDword(report, DISPLAY_LINE_EPD),
    5,
    "displayAt must restore the saved pointer verbatim across loops",
  );
});

test("displayAt emits relative add and restore", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("hi");
    s.displayAt(3);
  });
  const parsed = JSON.parse(mod.toJson()) as {
    module: { stmts: Array<{ kind: string; dst_addr?: number; modifier?: number; body?: unknown[] }> };
  };
  const varstores = parsed.module.stmts.filter(
    (stmt) => stmt.kind === "varstore" && stmt.dst_addr === DISPLAY_LINE_ADDR,
  );
  assert.equal(
    varstores.length,
    1,
    "displayAt emits one varstore to restore the line pointer",
  );
});

test("displayAt with a runtime line emits relative add and restore", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("hi");
    s.displayAt(Var(2));
  });
  const parsed = JSON.parse(mod.toJson()) as {
    module: { stmts: Array<{ kind: string; dst_addr?: number; modifier?: number }> };
  };
  const varstores = parsed.module.stmts.filter(
    (stmt) => stmt.kind === "varstore" && stmt.dst_addr === DISPLAY_LINE_ADDR,
  );
  assert.equal(
    varstores.length,
    2,
    "runtime-line displayAt writes the pointer twice (Add relative offset + SetTo restore)",
  );
});
