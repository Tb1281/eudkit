/**
 * Shared HIR inspection helpers for the unit-list and free-list walk suites.
 *
 * Both suites pin the same loop shape: one `while`, a CP-relative pointer
 * reader, a `done` sentinel and CurrentPlayer stores. Keep the loose wire-shape
 * navigation here so the assertions stay about behaviour.
 */

import assert from "node:assert/strict";

import {
  EudslError,
  UNIT_LIST_D,
  UNIT_LIST_S,
  UNIT_LIST_T,
  UNIT_LIST_U,
  buildModule,
} from "../index.ts";
import { CP_ADDR } from "../cp.ts";

// HIR JSON is intentionally loosely typed here: the tests read the exact wire
// shape, which the typed builder interfaces deliberately do not expose.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = { [key: string]: any };

/** Documented global head dwords for the four CUnit lists (ADR 0015). */
export const UNIT_LIST_HEADS: Record<string, number> = {
  [UNIT_LIST_U]: 161843,
  [UNIT_LIST_T]: 161841,
  [UNIT_LIST_S]: 161844,
  [UNIT_LIST_D]: 161842,
};

/** Link word each list follows: `ptr + 1` for `next`, `ptr + 0` for `prev`. */
export const UNIT_LIST_LINK_OFFSETS: Record<string, number> = {
  [UNIT_LIST_U]: 1,
  [UNIT_LIST_T]: 1,
  [UNIT_LIST_S]: 1,
  [UNIT_LIST_D]: 0,
};

export function moduleOf(fn: () => void): Json {
  return JSON.parse(buildModule(fn).toJson()).module as Json;
}

export function whiles(module: Json): Json[] {
  return (module.stmts as Json[]).filter((s) => s.kind === "while");
}

export function whileOf(module: Json): Json {
  const found = whiles(module);
  assert.equal(found.length, 1, JSON.stringify(found));
  return found[0];
}

/** The pointer var read by the loop's first (pool bound) condition. */
export function ptrOf(whileStmt: Json): number {
  return whileStmt.conds[0].relocs[0].target_var;
}

/** The `done` sentinel read by the loop's third condition. */
export function doneOf(whileStmt: Json): number {
  return whileStmt.conds[2].relocs[0].target_var;
}

export function cpStores(stmts: Json[]): Json[] {
  return stmts.filter((s) => s.kind === "varstore" && s.dst_addr === CP_ADDR);
}

export function varInit(module: Json, varId: number): number {
  return (module.vars as Json[]).find((v) => v.id === varId)!.init;
}

export function actsTargeting(stmts: Json[], varId: number): Json[] {
  return stmts.filter(
    (s) =>
      s.kind === "act" &&
      (s.relocs as Json[] | undefined)?.some((r) => r.target_var === varId),
  );
}

export function flatten(stmts: Json[]): Json[] {
  const out: Json[] = [];
  for (const stmt of stmts) {
    out.push(stmt);
    if (stmt.body) out.push(...flatten(stmt.body as Json[]));
    if (stmt.else_body) out.push(...flatten(stmt.else_body as Json[]));
  }
  return out;
}

export function isEudsl(code: string): (err: unknown) => boolean {
  return (err) => err instanceof EudslError && err.code === code;
}
