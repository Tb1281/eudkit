/**
 * Allocation tests for page-aligned containers (Layer 1).
 *
 * Alignment must count page slots, not raw variable ids: non-page
 * variables consume ids but no page slots, so padding on the raw counter
 * would place page-aligned containers at the wrong pinned rank.
 *
 * Pinned rank: the rank of a variable among page-pinned variables only,
 * in allocation order (the rank that decides TriggerPage placement). It is
 * derived from the module's var declarations, not from the raw variable id,
 * because non-page variables consume ids but no page slots.
 *
 * Layer 2, option C: page-pinned variables take final TriggerPage ranks in
 * id order first, then unpinned transfer/store sources in id order, so
 * every page-pinned variable's final rank equals its DSL pinned rank.
 * The transfer-before-array case that Layer 1 left deferred (S1) is now a
 * passing lock test, and T1/T2/T3 cover the non-crossing, mixed-scalar,
 * and FixedString-coexistence cases.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  FixedArray,
  FixedString,
  TriggerStruct,
  Var,
  arrayDword,
  buildModule,
  dword,
  getNextPageSlot,
  getNextVarSlot,
} from "../index.ts";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-alloc-dump-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf8");
    return execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf8",
    });
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function allocationLines(dumpIr: string): string[] {
  const lines = dumpIr.split("\n");
  const start = lines.indexOf("Allocations:");
  assert.notEqual(start, -1, "dump-ir output has no Allocations block");
  const out: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (!line.startsWith("  var[")) break;
    out.push(line);
  }
  return out;
}

const Point = TriggerStruct("Point", {
  x: dword(),
  y: dword(),
  z: dword(),
  w: dword(),
});

interface VarDecl {
  id: number;
  storage?: string;
}

interface ModuleLike {
  vars: readonly VarDecl[];
}

function pinnedRanks(mod: ModuleLike, ids: number[]): number[] {
  const pageIds = mod.vars
    .filter((v) => v.storage === "page")
    .map((v) => v.id);
  const rank = new Map(pageIds.map((id, i) => [id, i]));
  return ids.map((id) => {
    const r = rank.get(id);
    assert.notEqual(r, undefined, `var ${id} is not page-pinned`);
    return r as number;
  });
}

function pinnedCount(mod: ModuleLike): number {
  return mod.vars.filter((v) => v.storage === "page").length;
}

function pageOf(rank: number): number {
  return Math.floor(rank / 64);
}

test("allocation counters diverge by exactly the non-page variables", () => {
  buildModule(() => {
    Var(0);
    Var(0);
    Var(0);
    assert.equal(getNextVarSlot(), 3);
    assert.equal(getNextPageSlot(), 0);
    assert.equal(getNextVarSlot() - getNextPageSlot(), 3);
  });
});

test("scalar array after demoted cells starts at pinned rank 0", () => {
  let elemIds: number[] = [];
  const mod = buildModule(() => {
    Var(0);
    Var(0);
    Var(0);
    const arr = FixedArray(arrayDword, 4);
    elemIds = arr.vars.map((v) => v.id);
  });
  assert.deepEqual(pinnedRanks(mod, elemIds), [0, 1, 2, 3]);
});

test("struct array after demoted cells is page-aligned per element", () => {
  let instFields: number[][] = [];
  const mod = buildModule(() => {
    Var(0);
    Var(0);
    Var(0);
    const arr = FixedArray(Point, 2);
    instFields = arr.instances.map((inst) => inst.fields.map((f) => f.id));
  });
  // One TriggerPage per element: element 0 at slots 0..3, element 1 at 64..67.
  assert.deepEqual(pinnedRanks(mod, instFields[0]), [0, 1, 2, 3]);
  assert.deepEqual(pinnedRanks(mod, instFields[1]), [64, 65, 66, 67]);
  // 3 demoted + 2 elements x 4 fields + 60 pads before element 1.
  assert.equal(mod.vars.length, 71);
  assert.equal(pinnedCount(mod), 68);
  for (const fields of instFields) {
    const ranks = pinnedRanks(mod, fields);
    assert.ok(ranks.every((r) => pageOf(r) === pageOf(ranks[0])));
  }
});

test("standalone struct after 62 page and 2 demoted cells does not cross a page", () => {
  let fieldIds: number[] = [];
  const mod = buildModule(() => {
    for (let i = 0; i < 62; i++) Var(0, undefined, undefined, "page");
    Var(0);
    Var(0);
    const inst = Point.instantiate();
    fieldIds = inst.fields.map((f) => f.id);
  });
  // 62 page + 2 demoted, then 2 pads to the next page boundary: fields at 64..67.
  assert.deepEqual(pinnedRanks(mod, fieldIds), [64, 65, 66, 67]);
  assert.equal(mod.vars.length, 70);
  assert.equal(pinnedCount(mod), 68);
});

test("standalone struct after demoted cells packs without padding", () => {
  let fieldIds: number[] = [];
  const mod = buildModule(() => {
    Var(0);
    Var(0);
    Var(0);
    const inst = Point.instantiate();
    fieldIds = inst.fields.map((f) => f.id);
  });
  // 4 fields fit in the current page, so no padding is added.
  assert.deepEqual(pinnedRanks(mod, fieldIds), [0, 1, 2, 3]);
  assert.equal(mod.vars.length, 7);
  assert.equal(pinnedCount(mod), 4);
});

test("struct array allocation is identical in TypeScript and Python HIR", () => {
  let tsVars: VarDecl[] = [];
  const tsMod = buildModule(() => {
    Var(0);
    Var(0);
    Var(0);
    FixedArray(Point, 2);
  });
  tsVars = [...tsMod.vars];

  const pyScript = `
import json, sys
from py import FixedArray, TriggerStruct, Var, build_module, dword

Point = TriggerStruct("Point", {"x": dword(), "y": dword(), "z": dword(), "w": dword()})

def prog():
    Var(0)
    Var(0)
    Var(0)
    FixedArray(Point, 2)

mod = build_module(prog)
sys.stdout.write(json.dumps([{"id": v["id"], "storage": v.get("storage")} for v in mod.vars]))
`;

  const pyVars = JSON.parse(
    execFileSync("python3", ["-c", pyScript], {
      cwd: REPO_ROOT,
      encoding: "utf-8",
    }),
  ) as VarDecl[];
  const norm = (vars: VarDecl[]) =>
    vars.map((v) => ({ id: v.id, storage: v.storage ?? null }));
  assert.deepEqual(norm(tsVars), norm(pyVars));
});

test("S1 (Layer 2, option C): transfer-source scalar before a 64-length array keeps dynamic read-back at lowering", () => {
  // Formerly the Layer 1 deferred case: a preceding unpinned transfer
  // source shifted final TriggerPage ranks, so the dynamic set of element
  // 63 landed nowhere observable and the read-back was 0. With
  // pinned-first ordering the array keeps its DSL page alignment and the
  // read-back is 999, while the transfer itself still delivers 1234.
  let outId = -1;
  let dstId = -1;
  const mod = buildModule(() => {
    const src = Var(1234);
    const arr = FixedArray(arrayDword, 64);
    const dst = Var(0);
    dst.set(src);
    const idx = Var(63);
    arr.set(idx, 999);
    const out = Var(0);
    out.set(arr.get(63));
    outId = out.id;
    dstId = dst.id;
  });
  const report = runSim(mod.toJson(), 2);
  assert.equal(report.var_values[dstId], 1234);
  assert.equal(report.var_values[outId], 999);
});

test("T1 (Layer 2, option C): non-crossing 32-length array with a later transfer source keeps baseline dump-ir assignments", () => {
  // The transfer source is allocated after the array, so it already ranks
  // after every pinned variable in id order: pinned-first ordering must
  // not change any assignment. The expected lines below are the baseline
  // (pre-fix) assignments for this program, pinned line by line.
  const mod = buildModule(() => {
    const arr = FixedArray(arrayDword, 32);
    const src = Var(1234);
    const dst = Var(0);
    dst.set(src);
    void arr;
  });
  const lines = allocationLines(runDumpIr(mod.toJson()));
  const expected: string[] = [];
  for (let i = 0; i < 32; i++) {
    expected.push(`  var[${i}] init=0x0: TriggerPage(page=0, slot=${i})`);
  }
  expected.push("  var[32] init=0x4d2: TriggerPage(page=0, slot=32)");
  expected.push("  var[33] init=0x0: DwordCell(dword=0)");
  assert.deepEqual(lines, expected);
});

test("T2 (Layer 2, option C): mixed scalars between two arrays keep the second array page-aligned", () => {
  // Array A (32), an unpinned transfer source, a pinned scalar that is
  // itself a transfer source, then array B (64) with DSL page padding.
  // Under the old id ordering the unpinned source pushed B across a page
  // boundary; under pinned-first ordering B starts at final page 1,
  // slot 0, and its dynamic set/read-back works, as do both transfers.
  let dstId = -1;
  let dst2Id = -1;
  let outId = -1;
  const mod = buildModule(() => {
    const arrA = FixedArray(arrayDword, 32);
    const src = Var(1234);
    const pinnedScalar = Var(777, undefined, undefined, "page");
    const arrB = FixedArray(arrayDword, 64);
    const dst = Var(0);
    dst.set(src);
    const dst2 = Var(0);
    dst2.set(pinnedScalar);
    const idx = Var(63);
    arrB.set(idx, 999);
    const out = Var(0);
    out.set(arrB.get(63));
    dstId = dst.id;
    dst2Id = dst2.id;
    outId = out.id;
    void arrA;
  });
  const report = runSim(mod.toJson(), 2);
  assert.equal(report.var_values[dstId], 1234);
  assert.equal(report.var_values[dst2Id], 777);
  assert.equal(report.var_values[outId], 999);
});

test("T3 (Layer 2, option C): FixedString constructed before an array coexists with a preceding transfer source", () => {
  // A FixedString (STR-backed, outside the page-backing seam) constructed
  // before the array, plus an unpinned transfer source before the array,
  // must not shift the array: the array still reads back 999 and the
  // transfer still delivers 1234. FixedString correctness itself remains
  // covered by the ticket 9 lock, which is not edited here.
  let dstId = -1;
  let outId = -1;
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("hi");
    const src = Var(1234);
    const arr = FixedArray(arrayDword, 64);
    const dst = Var(0);
    dst.set(src);
    const idx = Var(63);
    arr.set(idx, 999);
    const out = Var(0);
    out.set(arr.get(63));
    dstId = dst.id;
    outId = out.id;
  });
  const report = runSim(mod.toJson(), 2);
  assert.equal(report.var_values[dstId], 1234);
  assert.equal(report.var_values[outId], 999);
});
