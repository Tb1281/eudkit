import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  EudslWarning,
  FixedString,
  Var,
  buildModule,
  onWarning,
  parseFormat,
} from "../index.ts";
import { runSim, stringEpd } from "./helpers.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function readBytes(report: any, strIndex = 0): number[] {
  const epds = report.external_epds ?? {};
  const epdBase = stringEpd(strIndex);
  const out: number[] = [];
  for (let dwordIdx = 0; dwordIdx < 64; dwordIdx++) {
    const val = (epds[String(epdBase + dwordIdx)] ?? 0) >>> 0;
    out.push(val & 0xff);
    out.push((val >>> 8) & 0xff);
    out.push((val >>> 16) & 0xff);
    out.push((val >>> 24) & 0xff);
  }
  return out;
}

function readText(report: any, strIndex = 0): string {
  const raw = readBytes(report, strIndex);
  const zeroIdx = raw.indexOf(0);
  const content = zeroIdx >= 0 ? raw.slice(0, zeroIdx) : raw;
  return Buffer.from(content).toString("latin1");
}

function captureWarnings(action: () => void): Array<{ code: string; message: string }> {
  const captured: Array<{ code: string; message: string }> = [];
  const unsubscribe = onWarning((w) => captured.push(w));
  try {
    action();
  } finally {
    unsubscribe();
  }
  return captured;
}

test("Ticket 85: 2+ bare decimals emit EUDSL-WARN-PERF warning", () => {
  const cases = [
    "{} {}",
    "{:d} {:d}",
    "{} {:d}",
    "{:d} {}",
    "wave={} hp={}",
    "a={} b={} c={}",
    "val1={:d} val2={:d} val3={:d}",
    "prefix {} middle {:d} suffix",
  ];

  for (const fmt of cases) {
    const warnings = captureWarnings(() => {
      parseFormat(fmt, "test");
    });
    assert.equal(
      warnings.length,
      1,
      `Expected exactly 1 warning for ${JSON.stringify(fmt)}, got ${warnings.length}`,
    );
    assert.equal(warnings[0].code, "EUDSL-WARN-PERF");
    assert.ok(
      warnings[0].message.includes("EUDSL-WARN-PERF"),
      `Warning message must contain EUDSL-WARN-PERF: ${warnings[0].message}`,
    );
    assert.ok(
      warnings[0].message.includes("~688T to ~51T"),
      `Warning message must advise overhead reduction: ${warnings[0].message}`,
    );
    assert.ok(
      warnings[0].message.includes("{:\\x1a>4d}"),
      `Warning message must mention fixed-width example: ${warnings[0].message}`,
    );
  }
});

test("Ticket 85: 0 or 1 bare decimal does NOT emit warning", () => {
  const cases = [
    "hello world",
    "{}",
    "{:d}",
    "wave={}",
    "hp={:d}",
    "score: {}",
  ];

  for (const fmt of cases) {
    const warnings = captureWarnings(() => {
      parseFormat(fmt, "test");
    });
    assert.equal(
      warnings.length,
      0,
      `Expected 0 warnings for ${JSON.stringify(fmt)}, got ${warnings.length}`,
    );
  }
});

test("Ticket 85: hex placeholders do NOT emit warning", () => {
  const cases = [
    "{:x}",
    "{:x} {:x}",
    "{:08x} {:08x}",
    "addr1={:x} addr2={:x}",
    "{:x} {}",
    "{} {:x}",
  ];

  for (const fmt of cases) {
    const warnings = captureWarnings(() => {
      parseFormat(fmt, "test");
    });
    assert.equal(
      warnings.length,
      0,
      `Expected 0 warnings for hex case ${JSON.stringify(fmt)}, got ${warnings.length}`,
    );
  }
});

test("Ticket 85: fixed-width decimal specs do NOT emit warning", () => {
  const cases = [
    "{:4d} {:4d}",
    "{:04d} {:04d}",
    "{:\x1a>4d} {:\x1a>4d}",
    "wave={:\x1a>4d} hp={:\x1a>4d}",
    "{:04d} {}",
    "{} {:4d}",
    "{:>4d} {:>4d}",
    "{:10d} {:10d}",
  ];

  for (const fmt of cases) {
    const warnings = captureWarnings(() => {
      parseFormat(fmt, "test");
    });
    assert.equal(
      warnings.length,
      0,
      `Expected 0 warnings for fixed-width case ${JSON.stringify(fmt)}, got ${warnings.length}`,
    );
  }
});

test("Ticket 85: warning is emitted via process.emitWarning on Node", async () => {
  await new Promise((r) => setImmediate(r));
  const nodeWarnings: any[] = [];
  const listener = (w: any) => {
    if (w.code === "EUDSL-WARN-PERF") {
      nodeWarnings.push(w);
    }
  };
  process.on("warning", listener);
  try {
    parseFormat("wave={} hp={}", "test");
    await new Promise((r) => setImmediate(r));
    assert.equal(nodeWarnings.length, 1);
    assert.equal(nodeWarnings[0].code, "EUDSL-WARN-PERF");
    assert.ok(
      nodeWarnings[0].message.includes("~688T to ~51T"),
      `Node warning message must advise overhead reduction: ${nodeWarnings[0].message}`,
    );
  } finally {
    process.off("warning", listener);
  }
});

test("Ticket 85: FixedString.write with 2+ bare decimals compiles and simulates successfully", () => {
  const warnings = captureWarnings(() => {
    const mod = buildModule(() => {
      const hud = new FixedString(32);
      const wave = Var(42);
      const hp = Var(100);
      hud.write("wave={} hp={}", wave, hp);
    });

    const report = runSim(mod.toJson());
    const rendered = readText(report);
    assert.equal(rendered, "wave=42 hp=100");
  });

  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].code, "EUDSL-WARN-PERF");
});

test("Ticket 85: TS and Python frontends emit identical warnings and byte-identical HIR", () => {
  let modTs: any;
  const tsWarnings = captureWarnings(() => {
    modTs = buildModule(() => {
      const hud = new FixedString(32);
      const wave = Var(42);
      const hp = Var(100);
      hud.write("wave={} hp={}", wave, hp);
    });
  });

  const pyCode = `
import json
from py import FixedString, Var, build_module, on_warning
captured = []
on_warning(lambda code, msg: captured.append((code, msg)))
def prog():
    hud = FixedString(32)
    wave = Var(42)
    hp = Var(100)
    hud.write("wave={} hp={}", wave, hp)
mod = build_module(prog)
print(json.dumps({"warnings": captured, "hir": json.loads(mod.to_json())}))
`;
  const repoRoot = path.resolve(__dirname, "../..");
  const out = execFileSync(
    "python3",
    ["-c", pyCode],
    {
      cwd: repoRoot,
      env: { ...process.env, PYTHONPATH: "." },
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    },
  );
  const parsed = JSON.parse(out);
  assert.equal(tsWarnings.length, 1);
  assert.equal(parsed.warnings.length, 1);
  assert.equal(tsWarnings[0].code, parsed.warnings[0][0]);
  assert.equal(tsWarnings[0].message, parsed.warnings[0][1]);
  assert.deepEqual(JSON.parse(modTs.toJson()), parsed.hir);
});
