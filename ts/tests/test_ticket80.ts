/**
 * Ticket #80: Direct 4-test digit extraction engine with compile-time lane pre-shifting.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  EudslError,
  FixedString,
  Var,
  buildModule,
} from "../index.ts";
import { runSim, stringEpd } from "./helpers.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "../..");

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

function varStoreCount(stmts: any[]): number {
  return stmts.filter((stmt) => stmt.kind === "varstore").length;
}

test("variable-width decimal edge cases", () => {
  const cases: [number, string][] = [
    [0, "0"],
    [9, "9"],
    [99, "99"],
    [999, "999"],
    [1000, "1000"],
    [4294967295, "4294967295"],
  ];

  for (const [val, expected] of cases) {
    // bare {}
    const modBare = buildModule(() => {
      const s = new FixedString(16);
      s.write("{}", Var(val));
    });
    assert.equal(readText(runSim(modBare.toJson())), expected, `bare {} failed for ${val}`);

    // {:d}
    const modD = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:d}", Var(val));
    });
    assert.equal(readText(runSim(modD.toJson())), expected, `{:d} failed for ${val}`);
  }
});

test("fixed-width zero-pad edge cases", () => {
  const cases: [number, string][] = [
    [0, "0000"],
    [9, "0009"],
    [99, "0099"],
    [999, "0999"],
    [1000, "1000"],
    [4294967295, "7295"], // modulo 10^4 wrap
  ];

  for (const [val, expected] of cases) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:04d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:04d} failed for ${val}`);
  }
});

test("fixed-width space-pad edge cases", () => {
  const cases: [number, string][] = [
    [0, "   0"], // units place NEVER suppresses
    [9, "   9"],
    [99, "  99"],
    [999, " 999"],
    [1000, "1000"],
    [4294967295, "7295"], // modulo 10^4 wrap
  ];

  for (const [val, expected] of cases) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:4d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:4d} failed for ${val}`);
  }
});

test("fixed-width pad-byte edge cases", () => {
  const cases: [number, string][] = [
    [0, "\x1a\x1a\x1a0"], // units place NEVER suppresses
    [9, "\x1a\x1a\x1a9"],
    [99, "\x1a\x1a99"],
    [999, "\x1a999"],
    [1000, "1000"],
    [4294967295, "7295"], // modulo 10^4 wrap
  ];

  for (const [val, expected] of cases) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:\x1a>4d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:\\x1a>4d} failed for ${val}`);
  }
});

test("dynamic fixed-width decimal edge cases", () => {
  const cases: [string, number, string][] = [
    ["{:04d}", 42, "0042"],
    ["{:4d}", 42, "  42"],
    ["{:\x1a>4d}", 42, "\x1a\x1a42"],
    ["{:04d}", 12345, "2345"], // modulo 10^4 wrap
    ["{:4d}", 0, "   0"], // units place NEVER suppresses
  ];
  for (const [fmt, val, expected] of cases) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      const start = Var(2);
      s.writeFrom(start, fmt, Var(val));
    });
    const raw = readBytes(runSim(mod.toJson())).slice(2, 2 + expected.length);
    assert.equal(
      Buffer.from(raw).toString("latin1"),
      expected,
      `${JSON.stringify(fmt)} failed for ${val}`,
    );
  }
});

test("fixed-width width boundaries", () => {
  // Width 1
  const casesW1: [number, string][] = [
    [0, "0"],
    [7, "7"],
    [42, "2"],
  ];
  for (const [val, expected] of casesW1) {
    const mod = buildModule(() => {
      const s = new FixedString(4);
      s.write("{:1d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:1d} failed for ${val}`);
  }

  // Width 10 space pad
  const casesW10Space: [number, string][] = [
    [0, "         0"],
    [4294967295, "4294967295"],
  ];
  for (const [val, expected] of casesW10Space) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:10d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:10d} failed for ${val}`);
  }

  // Width 10 zero pad
  const casesW10Zero: [number, string][] = [
    [0, "0000000000"],
    [4294967295, "4294967295"],
  ];
  for (const [val, expected] of casesW10Zero) {
    const mod = buildModule(() => {
      const s = new FixedString(16);
      s.write("{:010d}", Var(val));
    });
    assert.equal(readText(runSim(mod.toJson())), expected, `{:010d} failed for ${val}`);
  }
});

test("static position preservation and following tokens", () => {
  const mod = buildModule(() => {
    const s = new FixedString(32);
    s.write("hp={:04d}/{}", Var(50), Var(100));
  });
  assert.equal(readText(runSim(mod.toJson())), "hp=0050/100");
});

test("exact capacity fit", () => {
  const mod = buildModule(() => {
    const s = new FixedString(4);
    s.write("{:04d}", Var(42));
  });
  assert.equal(readText(runSim(mod.toJson())), "0042");
});

test("fixed-width constant wraps like Var", () => {
  const cases: [string, number, string][] = [
    ["{:4d}", 0, "   0"],
    ["{:04d}", 0, "0000"],
    ["{:4d}", 12345, "2345"],
    ["{:04d}", 12345, "2345"],
    ["{:*>4d}", 42, "**42"],
    ["{:*>4d}", 0, "***0"],
    ["{:A>4d}", 42, "AA42"],
    ["{:A>4d}", 0, "AAA0"],
    ["{:~>4d}", 7, "~~~7"],
    ["{:10d}", 4294967295, "4294967295"],
  ];
  for (const [fmt, val, expected] of cases) {
    const constMod = buildModule(() => {
      const s = new FixedString(16);
      s.write(fmt, val);
    });
    const varMod = buildModule(() => {
      const s = new FixedString(16);
      s.write(fmt, Var(val));
    });
    assert.equal(readText(runSim(constMod.toJson())), expected, `${fmt} (constant) failed for ${val}`);
    assert.equal(readText(runSim(varMod.toJson())), expected, `${fmt} (Var) failed for ${val}`);
  }
});

test("fixed-width constant capacity fit and bounds", () => {
  const wide = buildModule(() => {
    const s = new FixedString(8);
    s.write("{:4d}", 12345);
  });
  assert.equal(readText(runSim(wide.toJson())), "2345");

  const exact = buildModule(() => {
    const s = new FixedString(4);
    s.write("{:4d}", 12345);
  });
  assert.equal(readText(runSim(exact.toJson())), "2345");

  assert.throws(
    () => buildModule(() => {
      const s = new FixedString(3);
      s.write("{:4d}", 12345);
    }),
    (e: unknown) => e instanceof EudslError && e.code === "EUDSL-BOUNDS",
  );
});

test("multi-loop reset", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("{:04d}", Var(42));
  });
  assert.equal(readText(runSim(mod.toJson(), 3)), "0042");
});

test("static fixed-width decimal has no pointer patching", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(16);
    captured = s;
    s.write("{:\x1a>4d}", Var(42));
  });
  const s = captured as unknown as FixedString;
  assert.equal(
    (s as any).posVar,
    null,
    "static fixed-width decimal must not instantiate a runtime pos var",
  );

  const stmts = JSON.parse(mod.toJson()).module.stmts;
  assert.equal(
    varStoreCount(stmts),
    0,
    "static fixed-width decimal must not patch trigger action fields",
  );
  assert.equal(
    stmts.some((stmt: any) => stmt.kind === "when"),
    false,
    "static fixed-width decimal must not branch on a runtime byte lane",
  );

  assert.equal(readText(runSim(mod.toJson())), "\x1a\x1a42");
});

test("static fixed-width decimal folds at every byte lane", () => {
  const cases: [number, string][] = [
    [42, "0042"],
    [0, "0000"],
    [4294967295, "7295"],
  ];
  for (let lane = 0; lane < 4; lane++) {
    for (const [val, expected] of cases) {
      let captured: FixedString | null = null;
      const mod = buildModule(() => {
        const s = new FixedString(lane + 4);
        captured = s;
        s.writeFrom(lane, "{:04d}", Var(val));
      });
      const s = captured as unknown as FixedString;
      assert.equal(
        (s as any).posVar,
        null,
        `lane ${lane}: static fixed-width decimal must not instantiate a runtime pos var`,
      );
      const stmts = JSON.parse(mod.toJson()).module.stmts;
      assert.equal(
        varStoreCount(stmts),
        0,
        `lane ${lane}: static fixed-width decimal must not patch trigger action fields`,
      );

      const raw = readBytes(runSim(mod.toJson()));
      assert.equal(
        Buffer.from(raw.slice(lane, lane + 4)).toString("latin1"),
        expected,
        `lane ${lane}: digits must render at their compile-time byte lane`,
      );
      assert.equal(
        raw[lane + 4],
        0,
        `lane ${lane}: the terminator must sit at capacity`,
      );
    }
  }
});

test("static fixed-width decimal masks neighbours", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(16);
    captured = s;
    s.write("a{:04d}b{:\x1a>4d}c", Var(42), Var(7));
  });
  const s = captured as unknown as FixedString;
  assert.equal((s as any).posVar, null);
  assert.equal(varStoreCount(JSON.parse(mod.toJson()).module.stmts), 0);

  const raw = readBytes(runSim(mod.toJson()));
  assert.equal(
    Buffer.from(raw.slice(0, 11)).toString("latin1"),
    "a0042b\x1a\x1a\x1a7c",
  );
});

test("Byte-identical HIR parity between TypeScript and Python for Ticket #80", () => {
  const tsMod = buildModule(() => {
    const s1 = new FixedString(64);
    s1.write("hp={:04d}/{}", Var(50), Var(100));
    s1.append("|pad={:\x1a>4d}|space={:4d}", Var(7), Var(9));

    const s2 = new FixedString(16);
    s2.write("{:10d}", Var(12345));

    const s3 = new FixedString(16);
    s3.write("{:d}", Var(0));

    const s4 = new FixedString(16);
    const start = Var(2);
    s4.writeFrom(start, "{:04d}", Var(42));

    const s5 = new FixedString(32);
    s5.write("wrap={:4d}|star={:*>4d}", 12345, 42);
    s5.append("{:04d}", 0);
    s5.append("{:*>4d}", Var(7));
  });

  const pyScript = `
import sys
from py import FixedString, Var, build_module

def prog():
    s1 = FixedString(64)
    s1.write("hp={:04d}/{}", Var(50), Var(100))
    s1.append("|pad={:\\x1a>4d}|space={:4d}", Var(7), Var(9))

    s2 = FixedString(16)
    s2.write("{:10d}", Var(12345))

    s3 = FixedString(16)
    s3.write("{:d}", Var(0))

    s4 = FixedString(16)
    start = Var(2)
    s4.writeFrom(start, "{:04d}", Var(42))

    s5 = FixedString(32)
    s5.write("wrap={:4d}|star={:*>4d}", 12345, 42)
    s5.append("{:04d}", 0)
    s5.append("{:*>4d}", Var(7))

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: repoRoot,
    encoding: "utf-8",
    maxBuffer: 16 * 1024 * 1024,
  });

  assert.equal(tsMod.toJson(), pyHirJson);
});
