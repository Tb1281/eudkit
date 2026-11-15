import test from "node:test";
import assert from "node:assert/strict";

import {
  EudslError,
  FixedString,
  Var,
  buildModule,
  parseFormat,
} from "../index.ts";
import type { FormatSpec } from "../index.ts";
import { runSim, stringEpd } from "./helpers.ts";

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

test("Ticket 74: default placeholder {} defaults to decimal", () => {
  const { parts, placeholders } = parseFormat("hello {}", "test");
  assert.equal(placeholders, 1);
  assert.equal(parts.length, 2);
  assert.deepEqual(parts[0], { kind: "lit", text: "hello " });
  assert.equal(parts[1].kind, "arg");
  assert.deepEqual(parts[1].spec, {
    fill: undefined,
    align: undefined,
    width: undefined,
    radix: "dec",
  });
  assert.equal(parts[1].radix, "dec");
});

test("Ticket 74: explicit {:d} and {:x}", () => {
  const { parts, placeholders } = parseFormat("{:d} {:x}", "test");
  assert.equal(placeholders, 2);
  assert.equal(parts[0].kind, "arg");
  assert.deepEqual(parts[0].spec, {
    fill: undefined,
    align: undefined,
    width: undefined,
    radix: "dec",
  });
  assert.deepEqual(parts[1], { kind: "lit", text: " " });
  assert.equal(parts[2].kind, "arg");
  assert.deepEqual(parts[2].spec, {
    fill: undefined,
    align: undefined,
    width: undefined,
    radix: "hex",
  });
});

test("Ticket 74: hex with explicit width 8", () => {
  const cases: [string, FormatSpec][] = [
    ["{:08x}", { fill: "0", align: ">", width: 8, radix: "hex" }],
    ["{:8x}", { fill: " ", align: ">", width: 8, radix: "hex" }],
    ["{:>8x}", { fill: " ", align: ">", width: 8, radix: "hex" }],
    ["{:\x1a>8x}", { fill: "\x1a", align: ">", width: 8, radix: "hex" }],
  ];
  for (const [fmt, expected] of cases) {
    const { parts, placeholders } = parseFormat(fmt, "test");
    assert.equal(placeholders, 1);
    assert.equal(parts[0].kind, "arg");
    assert.deepEqual(parts[0].spec, expected, `Failed for ${fmt}`);
  }
});

test("Ticket 74: decimal shorthands", () => {
  const cases: [string, FormatSpec][] = [
    ["{:04d}", { fill: "0", align: ">", width: 4, radix: "dec" }],
    ["{:4d}", { fill: " ", align: ">", width: 4, radix: "dec" }],
    ["{:\x1a>4d}", { fill: "\x1a", align: ">", width: 4, radix: "dec" }],
    ["{:>4d}", { fill: " ", align: ">", width: 4, radix: "dec" }],
    ["{:04}", { fill: "0", align: ">", width: 4, radix: "dec" }],
    ["{:4}", { fill: " ", align: ">", width: 4, radix: "dec" }],
    ["{:>4}", { fill: " ", align: ">", width: 4, radix: "dec" }],
    ["{:\x1a>4}", { fill: "\x1a", align: ">", width: 4, radix: "dec" }],
  ];
  for (const [fmt, expected] of cases) {
    const { parts, placeholders } = parseFormat(fmt, "test");
    assert.equal(placeholders, 1);
    assert.equal(parts[0].kind, "arg");
    assert.deepEqual(parts[0].spec, expected, `Failed for ${fmt}`);
  }
});

test("Ticket 74: width boundaries", () => {
  const p1 = parseFormat("{:1d}", "test");
  assert.equal(p1.parts[0].kind === "arg" && p1.parts[0].spec.width, 1);

  const p10 = parseFormat("{:10d}", "test");
  assert.equal(p10.parts[0].kind === "arg" && p10.parts[0].spec.width, 10);

  const p010 = parseFormat("{:010d}", "test");
  assert.equal(p010.parts[0].kind === "arg" && p010.parts[0].spec.width, 10);
  assert.equal(p010.parts[0].kind === "arg" && p010.parts[0].spec.fill, "0");
});

test("Ticket 74: rejections with EUDSL-FORMAT", () => {
  const badSpecs = [
    "{:<4d}",
    "{:0<4d}",
    "{:<}",
    "{:<d}",
    "{:<8x}",
    "{:^4d}",
    "{:=4d}",
    "{:>}",
    "{:>d}",
    "{:4x}",
    "{:04x}",
    "{:1x}",
    "{:7x}",
    "{:9x}",
    "{:10x}",
    "{:010x}",
    "{:0d}",
    "{:0x}",
    "{:0}",
    "{:11d}",
    "{:99d}",
    "{:00d}",
    "{:004d}",
    "{:q}",
    "{:s}",
    "{:04s}",
    "{:4dd}",
    "{:d4}",
    "{:x8}",
    "{:foo}",
    "{",
    "}",
    "{:4d",
  ];
  for (const fmt of badSpecs) {
    assert.throws(
      () => parseFormat(fmt, "test"),
      (err: any) => err instanceof EudslError && err.code === "EUDSL-FORMAT",
      `Expected EUDSL-FORMAT for ${fmt}`,
    );
  }
});

test("Ticket 74: FixedString.write default decimal", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("val={}", 42);
  });
  const rep = runSim(mod.toJson());
  assert.equal(readText(rep, 0), "val=42");
});

test("Ticket 74: FixedString.write explicit hex", () => {
  const mod = buildModule(() => {
    const s = new FixedString(32);
    s.write("{:x}|{:08x}", 0x2a, 0x2a);
  });
  const rep = runSim(mod.toJson());
  assert.equal(readText(rep, 0), "0000002a|0000002a");
});

test("Ticket 74: FixedString.write static padded decimal", () => {
  const mod = buildModule(() => {
    const s = new FixedString(32);
    s.write("{:04d}|{:4d}|{:\x1a>4d}|{:04}", 7, 7, 7, 7);
  });
  const rep = runSim(mod.toJson());
  assert.equal(readText(rep, 0), "0007|   7|\x1a\x1a\x1a7|0007");
});

test("Ticket 74: capacity respects placeholder width", () => {
  assert.doesNotThrow(() => {
    buildModule(() => {
      const s = new FixedString(4);
      const v = Var(1);
      s.write("{:04d}", v);
    });
  });

  assert.throws(
    () => {
      buildModule(() => {
        const s = new FixedString(3);
        const v = Var(1);
        s.write("{:04d}", v);
      });
    },
    (err: any) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
  );

  assert.throws(
    () => {
      buildModule(() => {
        const s = new FixedString(9);
        const v = Var(1);
        s.write("{}", v);
      });
    },
    (err: any) => err instanceof EudslError && err.code === "EUDSL-BOUNDS",
  );
});
