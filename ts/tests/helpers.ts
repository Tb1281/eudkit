/**
 * Shared test harness helpers: simulate HIR through the native binary and
 * decode the synthetic STR buffer through EPD reads.
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..");
export const EUDKIT_COMPILE = path.join(
  REPO_ROOT,
  "target",
  "debug",
  "eudkit-compile",
);
export const EUDKIT = path.join(REPO_ROOT, "target", "debug", "eudkit");

export const EPD_BASE = 0x58a364;
export const SYNTHETIC_STRING_BASE = 0x20000000;
const DWORD_BYTES = 4;

export interface SimStaticCost {
  triggers: number;
  conditions: number;
  actions: number;
  trigger_bytes: number;
  blob_bytes: number;
  total_bytes: number;
}

export interface SimExecutedCost {
  triggers: number;
  conditions: number;
  actions: number;
  visited_items: number[];
}

export interface SimReport {
  static_cost: SimStaticCost;
  executed_cost: SimExecutedCost;
  pre_assembly: boolean;
  var_values: number[];
  self_targeted_invariant_holds: boolean;
  external_epds: Record<string, number>;
}

export interface HirActNode {
  acttype?: number;
  dest?: number;
  value?: number;
  mask?: number;
  eudx?: number;
  str_index?: number;
  relocs?: {
    target_string?: number;
    field_offset?: number;
    mode?: number;
    target_var?: number;
    target_offset?: number;
  }[];
  kind?: string;
  [key: string]: unknown;
}

export interface HirStmtNode {
  kind: string;
  body?: HirStmtNode[];
  else_body?: HirStmtNode[];
  acts?: HirActNode[];
  conds?: HirActNode[];
  [key: string]: unknown;
}

/** Run the native `sim` command against HIR JSON and return its report. */
export function runSim(hirJson: string, loops = 1): SimReport {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-sim-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf8");
    const out = execFileSync(
      EUDKIT_COMPILE,
      ["sim", hirPath, `${loops}`],
      { encoding: "utf8" },
    );
    return JSON.parse(out) as SimReport;
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

/** Static cost only, with no simulation (`dump-ir --json --no-exec`). */
export function runStatic(hirJson: string): {
  static_cost: SimStaticCost;
  packing: { item: number; conditions: number }[];
  pre_assembly: boolean;
} {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-static-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf8");
    const out = execFileSync(
      EUDKIT,
      ["dump-ir", hirPath, "--json", "--no-exec"],
      { encoding: "utf8" },
    );
    return JSON.parse(out) as {
      static_cost: SimStaticCost;
      packing: { item: number; conditions: number }[];
      pre_assembly: boolean;
    };
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

/** EPD index of synthetic string `index`. */
export function stringEpd(index: number): number {
  return ((SYNTHETIC_STRING_BASE + index * 0x10000 - EPD_BASE) >>> 0) / 4;
}

export function readDword(report: SimReport, epd: number): number {
  return report.external_epds[`${epd}`] ?? 0;
}

/** Raw bytes of synthetic string `index`, stopping at the first NUL. */
export function readStringBytes(
  report: SimReport,
  index: number,
  max = 256,
): number[] {
  const start = stringEpd(index);
  const bytes: number[] = [];
  for (let i = 0; i < max; i++) {
    const dword = readDword(report, start + (i >> 2));
    const byte = (dword >>> (8 * (i & 3))) & 0xff;
    if (byte === 0) break;
    bytes.push(byte);
  }
  return bytes;
}

export function asciiBytes(text: string): number[] {
  return Array.from(text, (ch) => ch.charCodeAt(0));
}

/** Render a synthetic string, dropping the documented 0x1A alignment pads. */
export function readText(report: SimReport, index: number): string {
  return String.fromCharCode(
    ...readStringBytes(report, index).filter((byte) => byte !== 0x1a),
  );
}

/**
 * Raw byte lanes from synthetic string 0, including NUL/past-NUL bytes.
 * Unlike [`readStringBytes`] this has no string index and does not stop at
 * the terminator; use it when the test asserts the whole lane window.
 */
export function rawStringBytes(
  epds: Record<string, number>,
  count: number,
): number[] {
  const epdBase = (SYNTHETIC_STRING_BASE - EPD_BASE) >>> 2;
  return Array.from({ length: count }, (_, i) => {
    const dword = epds[String(epdBase + (i >> 2))] ?? 0;
    return (dword >>> (8 * (i & 3))) & 0xff;
  });
}

/** Whole dwords of synthetic string 0, for multi-dword text assertions. */
export function readDwords(report: SimReport, count: number): number[] {
  const epdBase = (SYNTHETIC_STRING_BASE - EPD_BASE) >>> 2;
  return Array.from(
    { length: count },
    (_, dword) => report.external_epds[String(epdBase + dword)] ?? 0,
  );
}

/** Little-endian bytes of the first `count` dwords of synthetic string 0. */
export function stringDwordBytes(report: SimReport, count: number): Buffer {
  const buf = Buffer.alloc(count * DWORD_BYTES);
  for (const [index, value] of readDwords(report, count).entries()) {
    buf.writeUInt32LE(value, index * DWORD_BYTES);
  }
  return buf;
}

/** Recursively flatten statements, including temporal bodies and else arms. */
export function flattenStmts(stmts: HirStmtNode[]): HirStmtNode[] {
  const out: HirStmtNode[] = [];
  for (const stmt of stmts) {
    out.push(stmt);
    if (stmt.body) out.push(...flattenStmts(stmt.body));
    if (stmt.else_body) out.push(...flattenStmts(stmt.else_body));
  }
  return out;
}

/**
 * Actions only. Unlike [`flattenStmts`] this does not recurse into else arms
 * and only descends through `body`, preserving the original action-order
 * contract of the ticket #9 assertions.
 */
export function flattenActs(stmts: HirStmtNode[]): HirActNode[] {
  const out: HirActNode[] = [];
  for (const stmt of stmts) {
    if (stmt.acts) out.push(...stmt.acts);
    if (stmt.body) out.push(...flattenActs(stmt.body));
  }
  return out;
}

/** Dword offsets inside the synthetic STR buffer that were written. */
export function touchedDwordOffsets(
  report: SimReport,
  index: number,
): number[] {
  const base = stringEpd(index);
  return Object.keys(report.external_epds)
    .map(Number)
    .filter((epd) => base <= epd && epd < base + 0x4000)
    .sort((a, b) => a - b)
    .map((epd) => (epd - base) * 4);
}
