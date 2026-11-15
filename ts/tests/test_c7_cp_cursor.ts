/**
 * Issue #87 (C7): CP-cursor byte-lane writer for variable-width bare `{}`.
 *
 * The variable-width decimal writer borrows `CurrentPlayer` (`curpl`) for the
 * duration of a format run: it seeds the register with the dword holding the
 * next content byte, keeps `curpl = base + (pos >> 2)` while it emits digits,
 * and restores 0 at the end of the run (ADR 0004). These tests render real
 * values through the in-memory simulator and pin the borrow shape in HIR; the
 * Python mirror is `py/tests/test_c7_cp_cursor.py` and covers the same cases.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { FixedString, Var, buildModule } from "../index.ts";
import { EPD_CURRENT_PLAYER } from "../view.ts";
import { flattenStmts, readText, runSim, type HirActNode, type HirStmtNode } from "./helpers.ts";

const CP_ADDR = 0x6509b0;
const SET_DEATHS = 45;
const SET_TO = 7;

function moduleFor(fmt: string, values: number[], size = 32): ReturnType<typeof buildModule> {
  return buildModule(() => {
    const s = new FixedString(size);
    s.write(fmt, ...values.map((value) => Var(value)));
  });
}

function render(fmt: string, values: number[], size = 32): string {
  return readText(runSim(moduleFor(fmt, values, size).toJson()), 0);
}

function acts(stmts: HirStmtNode[]): HirActNode[] {
  const out: HirActNode[] = [];
  for (const stmt of flattenStmts(stmts)) {
    if (stmt.acts) out.push(...stmt.acts);
    if (stmt.kind === "act") out.push(stmt);
  }
  return out;
}

function cpBorrowCounts(stmts: HirStmtNode[]): [number, number] {
  const seeds = flattenStmts(stmts).filter(
    (stmt) => stmt.kind === "varstore" && stmt.dst_addr === CP_ADDR,
  ).length;
  const resets = acts(stmts).filter(
    (act) =>
      act.acttype === SET_DEATHS &&
      act.dest === EPD_CURRENT_PLAYER &&
      act.value === 0 &&
      act.modifier === SET_TO,
  ).length;
  return [seeds, resets];
}

function topStmts(fmt: string, values: number[]): HirStmtNode[] {
  // Variable-width decimal writes now go through one shared `func` body per
  // module, so its seed is emitted once in `module.funcs` rather than at each
  // call site. Counting func bodies too keeps the contract this test pins:
  // one seed site and one reset per format run, regardless of placeholder
  // count.
  const hir = JSON.parse(moduleFor(fmt, values).toJson()) as {
    module: { stmts: HirStmtNode[]; funcs?: { body: HirStmtNode[] }[] };
  };
  const stmts = [...hir.module.stmts];
  for (const fn of hir.module.funcs ?? []) stmts.push(...fn.body);
  return stmts;
}

test("C7 value sweep renders every width", () => {
  // 0, 1, 2 and 10 digits: the cursor must survive every lane wrap.
  for (const value of [0, 7, 42, 999, 1234567890]) {
    assert.equal(render("{}", [value]), `${value}`, `value ${value}`);
  }
});

test("C7 adjacent placeholders do not collide across lanes", () => {
  assert.equal(render("{} {}", [42, 7]), "42 7");
  assert.equal(render("a={} b={}", [42, 7]), "a=42 b=7");
});

test("C7 literal around a placeholder keeps the cursor synced", () => {
  assert.equal(render("ab{}cd", [1234]), "ab1234cd");
});

test("C7 long static prefix uses the constant cursor index", () => {
  // A static prefix longer than one dword seeds the cursor at index 1 instead
  // of zero (the constant-index seed path).
  assert.equal(render("abcd{}", [1234]), "abcd1234");
});

test("C7 bare decimal borrows once and restores curpl", () => {
  // One seed and one reset per run, even with several placeholders: the
  // lane-3 advance keeps the cursor valid for the whole run.
  for (const [fmt, values] of [
    ["{}", [42]],
    ["{} {}", [42, 7]],
    ["a={} b={}", [42, 7]],
  ] as const) {
    assert.deepEqual(cpBorrowCounts(topStmts(fmt, [...values])), [1, 1], fmt);
  }

  // The known-lane path (`{:x}`) never touches curpl.
  assert.deepEqual(cpBorrowCounts(topStmts("{:x}", [42])), [0, 0]);
});
