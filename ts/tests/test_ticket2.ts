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
  Trigger,
  Var,
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

test("64 scalar Vars pack into 1 self-targeted TriggerPage (2,408 bytes)", () => {
  const mod = buildModule(() => {
    for (let i = 0; i < ACT_COUNT; i++) {
      const v = Var(100 + i, undefined, undefined, "page");
      assert.equal(v.id, i);
      assert.equal(v.pageIndex, 0);
      assert.equal(v.slotIndex, i);
    }
  });

  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 1);
  assert.equal(report.static_cost.trigger_bytes, 2408);
  assert.equal(report.self_targeted_invariant_holds, true);
  assert.equal(report.var_values.length, 64);
  for (let i = 0; i < 64; i++) {
    assert.equal(report.var_values[i], 100 + i);
  }
});

test("1A in-place constant mutations (set, iadd, isub) and 1C conditions (eq, ne, lt, le, gt, ge)", () => {
  const mod = buildModule(() => {
    const x = Var(10, undefined, undefined, "page");
    const y = Var(20, undefined, undefined, "page");
    const z = Var(0, undefined, undefined, "page");
    const ok = Var(0, undefined, undefined, "page");

    // 3 in-place constant mutations -> 3 SetDeathsX actions in 1 Trigger (0T VProc dispatch)
    x.set(100);
    x.iadd(25);
    y.isub(5);
    z.set(42);

    new Trigger({
      conds: [
        x.eq(125),
        x.ge(125),
        x.le(125),
        x.gt(124),
        x.lt(126),
        y.eq(15),
        z.ne(0),
      ],
      acts: [ok.act_set(1)],
    });
  });

  // 1 packed constant-update trigger + 1 conditional trigger + 1 TriggerPage = 3 static triggers
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 3);
  // Only the 2 execution triggers run; the TriggerPage is not fired for constant updates!
  assert.equal(report.executed_cost.triggers, 2);
  assert.deepEqual(report.executed_cost.visited_items, [0, 1]);
  assert.deepEqual(report.var_values, [125, 15, 42, 1]);
  assert.equal(report.self_targeted_invariant_holds, true);
});

test("Coalesced multi-variable VProc batch fires shared TriggerPage once (1T) and restores invariant", () => {
  const mod = buildModule(() => {
    const x = Var(10, undefined, undefined, "page");
    const y = Var(20, undefined, undefined, "page");
    const z = Var(30, undefined, undefined, "page");
    const a = Var(0, undefined, undefined, "page");
    const b = Var(0, undefined, undefined, "page");
    const c = Var(5, undefined, undefined, "page");

    a.set(x);
    b.set(y);
    c.iadd(z);
  });

  const report = runSim(mod.toJson());
  // T_setup (0), T_cont (1), TriggerPage 0 (2) -> 3 triggers static, 3 triggers executed
  assert.equal(report.static_cost.triggers, 3);
  assert.equal(report.executed_cost.triggers, 3);
  // TriggerPage 0 (item 2) is visited ONLY ONCE for all 3 variable reads!
  assert.deepEqual(report.executed_cost.visited_items, [0, 2, 1]);
  assert.deepEqual(report.var_values, [10, 20, 30, 10, 20, 35]);
  assert.equal(report.self_targeted_invariant_holds, true);
});

test("Arithmetic, bitwise expressions, and variable-to-variable comparisons", () => {
  const mod = buildModule(() => {
    const x = Var(12);
    const y = Var(5);
    const sum = Var(0);
    const diff = Var(0);
    const prod = Var(0);
    const quot = Var(0);
    const rem = Var(0);
    const band = Var(0);
    const bor = Var(0);
    const bxor = Var(0);
    const shl = Var(0);
    const shr = Var(0);
    const cmpOk = Var(0);

    sum.set(x.add(y)); // 17
    diff.set(x.sub(y)); // 7
    prod.set(x.mul(3)); // 36
    quot.set(x.div(5)); // 2
    rem.set(x.mod(5)); // 2
    band.set(x.bitAnd(0b1010)); // 12 & 10 = 8
    bor.set(x.bitOr(0b0011)); // 12 | 3 = 15
    bxor.set(x.bitXor(0b0110)); // 12 ^ 6 = 10
    shl.set(y.shl(2)); // 5 << 2 = 20
    shr.set(x.shr(2)); // 12 >> 2 = 3

    new Trigger({
      conds: [
        sum.eq(17),
        diff.eq(7),
        x.gt(y),
        y.lt(x),
        x.ge(y),
        y.le(x),
        x.ne(y),
      ],
      acts: [cmpOk.act_set(999)],
    });
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(
    report.var_values.slice(0, 13),
    [12, 5, 17, 7, 36, 2, 2, 8, 15, 10, 20, 3, 999],
  );
  assert.equal(report.self_targeted_invariant_holds, true);
});

test("Byte-identical HIR/LIR parity between TypeScript and Python for Ticket #2", () => {
  const tsMod = buildModule(() => {
    const x = Var(10, undefined, undefined, "page");
    const y = Var(20, undefined, undefined, "page");
    const z = Var(30, undefined, undefined, "page");
    const a = Var(0, undefined, undefined, "page");
    const b = Var(0, undefined, undefined, "page");
    const c = Var(5, undefined, undefined, "page");

    x.iadd(5);
    y.isub(2);
    a.set(x);
    b.set(y);
    c.iadd(z);

    new Trigger({
      conds: [a.eq(15), b.ge(18), c.le(35)],
      acts: [DisplayText("Ticket 2 Parity!")],
    });
  });

  const pyScript = `
from py import DisplayText, Trigger, Var, buildModule

def prog():
    x = Var(10, storage="page")
    y = Var(20, storage="page")
    z = Var(30, storage="page")
    a = Var(0, storage="page")
    b = Var(0, storage="page")
    c = Var(5, storage="page")

    x += 5
    y -= 2
    a.set(x)
    b.set(y)
    c += z

    Trigger(
        conds=[a == 15, b >= 18, c <= 35],
        acts=[DisplayText("Ticket 2 Parity!")],
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
  assert.deepEqual(report.var_values, [15, 18, 30, 15, 18, 35]);
  assert.equal(report.self_targeted_invariant_holds, true);

  // Also verify .scx compilation
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-scx2-"));
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
