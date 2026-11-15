import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import test from "node:test";

import {
  EPD_BASE,
  asciiBytes,
  flattenActs,
  readStringBytes,
  readDword,
  readText,
  runSim,
  runStatic,
  stringEpd,
  touchedDwordOffsets,
  type HirStmtNode,
} from "./helpers.ts";

import {
  Func,
  Always,
  SetMemory,
  SetTo,
  FixedString,
  Var,
  buildModule,
  memcpyEPD,
  memsetEPD,
  once,
  when,
} from "../index.ts";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..");
const DISPLAY_LINE_ADDR = 0x640b58;
const DISPLAY_LINE_EPD = (DISPLAY_LINE_ADDR - EPD_BASE) >>> 2;

function parseModule(hirJson: string): {
  module: {
    stmts: HirStmtNode[];
    strings?: string[];
    vars?: {
      id: number;
      storage?: string;
      init_string?: { index: number; offset: number };
    }[];
  };
} {
  return JSON.parse(hirJson) as ReturnType<typeof parseModule>;
}

test("FixedString(size) reserves an STR marker and static write packs constant dwords", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("Hi", 0x41);
  });

  const hirJson = mod.toJson();
  const parsed = parseModule(hirJson);
  assert.equal(parsed.module.strings?.length, 1);
  assert.match(parsed.module.strings?.[0] ?? "", /EUDString#0/);
  assert.equal(
    parsed.module.stmts.filter((stmt) => stmt.kind === "when").length,
    0,
    "a static write must not need lane-selection whens",
  );

  const report = runSim(hirJson);
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("Hi00000041"));
  assert.equal(report.static_cost.triggers, 1, "static write should be one trigger");
});

test("runtime-position FixedString writes allocate a page-pinned StrAddr variable with init_string", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    const pos = Var(0);
    s.writeFrom(pos, "{}", Var(1));
  });

  const parsed = parseModule(mod.toJson());
  const addr = parsed.module.vars?.find((v) => v.init_string !== undefined);
  assert.ok(
    addr,
    "runtime-position writes must allocate the buffer address variable",
  );
  assert.equal(addr.storage, "page");
  assert.deepEqual(addr.init_string, { index: 0, offset: 0 });
});

test("write is statically anchored at 0 inside when() and Func()", () => {
  const mod = buildModule(() => {
    const flag = Var(1);
    const s = new FixedString(8);
    when(flag.eq(1), () => {
      s.write("AB");
    });
    const f = Func(() => {
      s.append("C");
    });
    f();
  });

  const hirJson = mod.toJson();
  const parsed = parseModule(hirJson);
  const whenStmt = parsed.module.stmts.find((stmt) => stmt.kind === "when");
  assert.ok(whenStmt?.body);
  assert.ok(
    whenStmt.body.every(
      (stmt) => stmt.kind === "act" || stmt.kind === "trigger",
    ),
    "a static write inside when() must lower to plain acts, not lane selection",
  );
  assert.equal(
    whenStmt.body.filter((stmt) => stmt.kind === "when").length,
    0,
    "a static write must not emit lane-selection whens",
  );

  const report = runSim(hirJson);
  assert.equal(readText(report, 0), "ABC");
});

test("append continues from lastpos and writeFrom overwrites an explicit position", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("abcdefgh");
    s.append("IJ");
    s.writeFrom(2, "XY");
  });

  const report = runSim(mod.toJson());
  const base = stringEpd(0);
  assert.equal(readDword(report, base), 0x59586261, "dword 0 must be 'abXY'");
  assert.equal(readDword(report, base + 1), 0x68676600, "NUL at byte 4");
  assert.equal(readDword(report, base + 2), 0x00004a49, "append content at bytes 8-9");
});

test("static writes defer their terminator and pack across append calls", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("ab");
    s.append("cd");
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("abcd"));
  assert.equal(
    report.static_cost.actions,
    2,
    "the full content dword and one terminator dword should be the only writes",
  );
});

test("skipped branches keep the deferred tail flushed at scope entry", () => {
  const mod = buildModule(() => {
    const flag = Var(0);
    const s = new FixedString(8);
    s.write("v=");
    when(flag.eq(1), () => {
      s.write("AB");
    });
    s.append("!");
    s.display();
  });

  const report = runSim(mod.toJson());
  assert.equal(
    readText(report, 0),
    "v=!",
    "a skipped branch must not discard the outer deferred tail",
  );
});

test("writeFrom flushes a deferred tail before repositioning", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("abc");
    s.writeFrom(1, "Z");
    s.display();
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("aZ"));
});

test("module end flushes a static write that is never displayed", () => {
  const mod = buildModule(() => {
    new FixedString(8).write("tail");
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("tail"));
});

test("deferred terminator never adds alignment padding past content", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("12345678");
    s.display();
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("12345678"));
  assert.deepEqual(
    touchedDwordOffsets(report, 0),
    [0, 4, 8],
    "a full buffer may touch only its content dwords and the single NUL dword",
  );
});

test("writeFrom accepts a game-time position", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("abcdefgh");
    const pos = Var(6);
    s.writeFrom(pos, "Z");
  });

  const report = runSim(mod.toJson());
  const base = stringEpd(0);
  assert.equal(readDword(report, base + 1), 0x005a6665, "byte 6 must be 'Z' and byte 7 NUL");
});

test("format specs render hex, decimal, and escaped braces", () => {
  const mod = buildModule(() => {
    const s = new FixedString(64);
    s.write("{:x}|{:x}|{:d}", 0xab, 0xab, 1234);
    s.append("|{{x}}|{:d}", 7);
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(
    readStringBytes(report, 0),
    asciiBytes("000000ab|000000ab|1234|{x}|7"),
  );
});

test("runtime format placeholders render through the game values", () => {
  const mod = buildModule(() => {
    const s = new FixedString(64);
    const hex = Var(0x12345678);
    const dec = Var(4321);
    s.write("{:x}", hex);
    s.append(":{:d}", dec);
  });

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("12345678:4321"));
});

test("malformed format strings fail loudly with EUDSL-FORMAT", () => {
  const cases: (() => void)[] = [
    () =>
      buildModule(() => {
        new FixedString(8).write("{");
      }),
    () =>
      buildModule(() => {
        new FixedString(8).write("{:q}", 1);
      }),
    () =>
      buildModule(() => {
        new FixedString(8).write("{} {}", 1);
      }),
    () =>
      buildModule(() => {
        new FixedString(8).append("}");
      }),
  ];
  for (const build of cases) {
    assert.throws(build, /EUDSL-FORMAT/);
  }
});

test("clear resets the buffer and subsequent writes restart at 0", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("AAAA");
    s.clear();
    s.write("B");
  });

  const report = runSim(mod.toJson());
  const base = stringEpd(0);
  assert.equal(readDword(report, base), 0x42, "byte 0 must be 'B' with NUL at byte 1");
});

test("display, printAt, and displayAt emit display acts and line changes", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("hi");
    s.display();
    s.printAt(3, "yo");
    s.displayAt(Var(2));
    s.clear();
  });

  const parsed = parseModule(mod.toJson());
  const acts = flattenActs(parsed.module.stmts);
  const displays = acts.filter((act) => act.acttype === 9);
  assert.equal(displays.length, 3);
  assert.deepEqual(
    displays.map((act) => act.str_index),
    [0, 0, 0],
  );
  assert.ok(
    acts.some((act) => act.dest === DISPLAY_LINE_EPD && act.value === 3),
    "printAt(3) must write display line 3",
  );
});

test("memcpyEPD copies and memsetEPD fills constant and variable dword counts", () => {
  const mod = buildModule(() => {
    memsetEPD(1000, 0xaabbccdd, 3);
    memcpyEPD(2000, 1000, 3);
    const n = Var(2);
    memsetEPD(3000, 0x11223344, n);
  });

  const report = runSim(mod.toJson(), 2);
  for (let i = 0; i < 3; i++) {
    assert.equal(readDword(report, 1000 + i), 0xaabbccdd, `src dword ${i}`);
    assert.equal(readDword(report, 2000 + i), 0xaabbccdd, `copy dword ${i}`);
  }
  assert.equal(readDword(report, 3000), 0x11223344);
  assert.equal(readDword(report, 3001), 0x11223344);
  assert.equal(readDword(report, 3002), 0, "variable count must stop after 2 dwords");
});

test("memcpyEPD and memsetEPD fold a zero count to 0T", () => {
  const mod = buildModule(() => {
    memcpyEPD(9000, 1000, 0);
    memsetEPD(9001, 1, 0);
  });

  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 0);
  assert.equal(report.static_cost.total_bytes, 0);
});

test("block primitives validate their arguments", () => {
  assert.throws(
    () =>
      buildModule(() => {
        memcpyEPD(1, 2, -1);
      }),
    /EUDSL-RANGE/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        memsetEPD(-1, 0, 1);
      }),
    /EUDSL-TYPE/,
  );
});

test("Byte-identical HIR parity between TypeScript and Python strings", () => {
  const tsMod = buildModule(() => {
    const s = new FixedString(32);
    s.write("Hi", 0x41);
    s.append(":", Var(0x12345678));
    s.writeFrom(2, "x");
    s.print("{}", Var(7));
    s.clear();

    const t = new FixedString(8);
    t.displayAt(3);

    const aligned = new FixedString(16);
    aligned.write("AB");
    when(Always(), () => {});
    aligned.append("CD", Var(1));

    const tight = new FixedString(7);
    tight.write("ABCDEFG");
    when(Always(), () => {});

    memcpyEPD(2000, 1000, 3);
    memsetEPD(3000, 0xaabbccdd, 3);
    const n = Var(2);
    memsetEPD(4000, 0x11223344, n);
  });

  const pyScript = `
import sys
from py import Always, FixedString, Var, build_module, memcpyEPD, memsetEPD, when

def prog():
    s = FixedString(32)
    s.write("Hi", 0x41)
    s.append(":", Var(0x12345678))
    s.writeFrom(2, "x")
    s.print("{}", Var(7))
    s.clear()

    t = FixedString(8)
    t.displayAt(3)

    aligned = FixedString(16)
    aligned.write("AB")
    when(Always(), lambda: None)
    aligned.append("CD", Var(1))

    tight = FixedString(7)
    tight.write("ABCDEFG")
    when(Always(), lambda: None)

    memcpyEPD(2000, 1000, 3)
    memsetEPD(3000, 0xAABBCCDD, 3)
    n = Var(2)
    memsetEPD(4000, 0x11223344, n)

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    maxBuffer: 10 * 1024 * 1024,
  });
  assert.equal(tsMod.toJson(), pyHirJson);
});

test("FixedString rejects invalid sizes at build time", () => {
  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(0);
      }),
    /EUDSL-BOUNDS/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(65536);
      }),
    /EUDSL-BOUNDS/,
  );
});

test("capacity exact fit writes content and reserves one terminator byte", () => {
  const mod = buildModule(() => {
    new FixedString(4).write("abcd");
  });

  const parsed = parseModule(mod.toJson());
  assert.equal(
    parsed.module.strings?.[0],
    `${" ".repeat(5)}\x7fEUDString#0\x7f`,
    "the marker must start after the content region and its terminator slot",
  );

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("abcd"));
  assert.deepEqual(
    touchedDwordOffsets(report, 0),
    [0, 4],
    "an exact-fit write may touch only its content and terminator dwords",
  );
});

test("capacity rejects static writes and appends that exceed size", () => {
  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(4).write("abcde");
      }),
    /EUDSL-BOUNDS/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        const s = new FixedString(4);
        s.write("abcd");
        s.append("e");
      }),
    /EUDSL-BOUNDS/,
  );
});

test("writeFrom enforces the capacity boundary for constant positions", () => {
  const ok = buildModule(() => {
    const s = new FixedString(4);
    s.write("abc");
    s.writeFrom(3, "X");
  });
  const report = runSim(ok.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("abcX"));

  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(4).writeFrom(3, "XY");
      }),
    /EUDSL-BOUNDS/,
  );
});

test("dynamic hex content is bounded by capacity", () => {
  const ok = buildModule(() => {
    new FixedString(8).write(Var(1));
  });
  assert.deepEqual(readStringBytes(runSim(ok.toJson()), 0), asciiBytes("00000001"));

  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(7).write(Var(1));
      }),
    /EUDSL-BOUNDS/,
  );
});

test("dynamic decimal and format-part content use conservative bounds", () => {
  const dec = buildModule(() => {
    new FixedString(10).write("{:d}", Var(1));
  });
  assert.deepEqual(readStringBytes(runSim(dec.toJson()), 0), asciiBytes("1"));

  const mixed = buildModule(() => {
    new FixedString(10).write("ab{:x}", Var(1));
  });
  assert.deepEqual(
    readStringBytes(runSim(mixed.toJson()), 0),
    asciiBytes("ab00000001"),
  );

  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(9).write("{:d}", Var(1));
      }),
    /EUDSL-BOUNDS/,
    "decimal content must reserve 10 bytes even when the value is short",
  );
  assert.throws(
    () =>
      buildModule(() => {
        new FixedString(9).write("ab{:x}", Var(1));
      }),
    /EUDSL-BOUNDS/,
  );
});

test("decimal digit suppression restarts on every trigger loop", () => {
  const mod = buildModule(() => {
    new FixedString(16).print("{:d}", Var(42));
  });
  const report = runSim(mod.toJson(), 3);
  assert.equal(
    readText(report, 0),
    "42",
    "the leading-zero suppression state must not leak into the next loop",
  );
});

test("displayAt restores saved on every call and loop", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("hi");
    SetMemory(DISPLAY_LINE_ADDR, SetTo, 2);
    s.displayAt(3);
    SetMemory(DISPLAY_LINE_ADDR, SetTo, 2);
    s.displayAt(3);
  });
  const report = runSim(mod.toJson(), 3);
  assert.equal(
    readDword(report, DISPLAY_LINE_EPD),
    2,
    "sim-visible restore is saved verbatim; each displayAt must still save only the current line counter, not accumulate",
  );
});

test("conditional branches are capacity-checked as if they execute", () => {
  const ok = buildModule(() => {
    const s = new FixedString(8);
    s.write("abcd");
    when(Var(0).eq(1), () => {
      s.append("efgh");
    });
  });
  assert.deepEqual(readStringBytes(runSim(ok.toJson()), 0), asciiBytes("abcd"));

  assert.throws(
    () =>
      buildModule(() => {
        const s = new FixedString(8);
        s.write("abcd");
        when(Var(0).eq(1), () => {
          s.append("efghi");
        });
      }),
    /EUDSL-BOUNDS/,
  );
});

test("standalone empty when/once gates cost nothing", () => {
  const cases: [string, () => void][] = [
    ["when(Always())", () => when(Always(), () => {})],
    ["once(Always())", () => once(Always(), () => {})],
  ];
  for (const [label, prog] of cases) {
    const mod = buildModule(prog);
    const staticCost = runStatic(mod.toJson()).static_cost;
    assert.deepEqual(
      [
        staticCost.triggers,
        staticCost.conditions,
        staticCost.actions,
        staticCost.total_bytes,
      ],
      [0, 0, 0, 0],
      `${label} must not emit a dead trigger`,
    );
    const executed = runSim(mod.toJson()).executed_cost;
    assert.deepEqual(
      [executed.triggers, executed.conditions, executed.actions],
      [0, 0, 0],
      `${label} must not evaluate conditions`,
    );
  }
});

test("standalone empty var gate keeps only the unused var cell", () => {
  const mod = buildModule(() => {
    when(Var(0).eq(1), () => {});
  });
  const staticCost = runStatic(mod.toJson()).static_cost;
  assert.deepEqual(
    [
      staticCost.triggers,
      staticCost.conditions,
      staticCost.actions,
      staticCost.total_bytes,
    ],
    [0, 0, 0, 4],
    "the empty gate is dead; only the unused 4-byte variable remains",
  );
  const executed = runSim(mod.toJson()).executed_cost;
  assert.deepEqual(
    [executed.triggers, executed.conditions, executed.actions],
    [0, 0, 0],
  );
});

test("append after a scope boundary re-installs its runtime position every loop", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("AB");
    when(Always(), () => {});
    s.append("CD");
  });

  const staticCost = runStatic(mod.toJson()).static_cost;
  assert.deepEqual(
    [
      staticCost.triggers,
      staticCost.conditions,
      staticCost.actions,
      staticCost.total_bytes,
    ],
    [27, 2, 332, 65016],
    "the empty scope-boundary marker loses its dead trigger and condition; " +
      "removing that statement also lets the two adjacent act runs pack into " +
      "one trigger downstream, without moving the string layout",
  );

  const report = runSim(mod.toJson(), 3);
  assert.equal(
    readText(report, 0),
    "ABCD",
    "the runtime append position must not drift past the static terminator",
  );
});

test("aligned boundaries pad partial dwords with 0x1A", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("AB");
    when(Always(), () => {});
    s.append("CD");
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(
    readStringBytes(report, 0),
    [...asciiBytes("AB"), 0x1a, 0x1a, ...asciiBytes("CD")],
    "a lane-2 boundary must pad to the next dword before the dynamic append",
  );
  assert.equal(readText(report, 0), "ABCD");
  assert.ok(
    touchedDwordOffsets(report, 0).every((offset) => offset <= 4),
    "padding must stay inside the string's own terminator slot",
  );
});

test("aligned boundaries do not pad content that already ends on a dword", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("ABCD");
    when(Always(), () => {});
    s.append("EF");
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("ABCDEF"));
  assert.equal(readText(report, 0), "ABCDEF");
});

test("aligned boundaries fall back to the unaligned lane when padding does not fit", () => {
  const mod = buildModule(() => {
    const s = new FixedString(7);
    s.write("ABCDEFG");
    when(Always(), () => {});
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(
    readStringBytes(report, 0),
    asciiBytes("ABCDEFG"),
    "a full buffer must not gain padding past its terminator slot",
  );
  assert.equal(readText(report, 0), "ABCDEFG");
});

test("aligned padding still leaves exact-fit content inside capacity", () => {
  const exact = buildModule(() => {
    const s = new FixedString(8);
    s.write("AB");
    when(Always(), () => {});
    s.append("CDEF");
  });
  const report = runSim(exact.toJson());
  assert.deepEqual(
    readStringBytes(report, 0),
    [...asciiBytes("AB"), 0x1a, 0x1a, ...asciiBytes("CDEF")],
  );
  assert.equal(readText(report, 0), "ABCDEF");

  assert.throws(
    () =>
      buildModule(() => {
        const s = new FixedString(7);
        s.write("AB");
        when(Always(), () => {});
        s.append("CDEF");
      }),
    /EUDSL-BOUNDS/,
    "one byte short after padding must fail loudly, not overflow",
  );
});

test("repeated aligned boundaries keep a stable pad-per-boundary layout", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("AB");
    when(Always(), () => {});
    s.append("CD");
    when(Always(), () => {});
    s.append("EF");
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(
    readStringBytes(report, 0),
    [
      ...asciiBytes("AB"),
      0x1a,
      0x1a,
      ...asciiBytes("CD"),
      0x1a,
      0x1a,
      ...asciiBytes("EF"),
    ],
  );
  assert.equal(readText(report, 0), "ABCDEF");
  assert.ok(
    touchedDwordOffsets(report, 0).every((offset) => offset <= 8),
    "repeated boundaries must not walk past the terminator slot",
  );
});

test("dynamic hex after an aligned boundary uses constant byte masks", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    s.write("AB");
    when(Always(), () => {});
    s.append(Var(1));
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(
    readStringBytes(report, 0),
    [
      ...asciiBytes("AB"),
      0x1a,
      0x1a,
      ...asciiBytes("00000001"),
    ],
  );
  assert.equal(readText(report, 0), "AB00000001");

  const acts = flattenActs(parseModule(mod.toJson()).module.stmts);
  const byteMasks = new Set([0xff, 0xff00, 0xff0000, 0xff000000]);
  const laneActs = acts.filter(
    (act) =>
      act.acttype === 45 &&
      act.eudx === 0x4353 &&
      byteMasks.has(act.mask ?? 0),
  );
  assert.ok(
    laneActs.length >= 9,
    "eight hex digits plus the terminator must emit byte-masked writes",
  );
  assert.deepEqual(
    [...new Set(laneActs.map((act) => act.mask))].sort(),
    [...byteMasks].sort(),
    "a known lane must bake every byte mask instead of selecting it at runtime",
  );
});

test("an aligned dword run after a taken boundary stays inside capacity", () => {
  const mod = buildModule(() => {
    const s = new FixedString(8);
    s.write("abcd");
    when(Always(), () => {
      s.append("efgh");
    });
  });

  const report = runSim(mod.toJson(), 2);
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("abcdefgh"));
  assert.equal(readText(report, 0), "abcdefgh");
  assert.ok(
    touchedDwordOffsets(report, 0).every((offset) => offset <= 8),
    "the fast dword run must stop at the terminator slot",
  );
});

test("dynamic tails pad at boundaries only when the pad fits", () => {
  const fits = buildModule(() => {
    const s = new FixedString(12);
    s.write("AB");
    s.append(Var(1));
    when(Always(), () => {});
  });

  const padded = runSim(fits.toJson(), 2);
  assert.deepEqual(
    readStringBytes(padded, 0),
    [...asciiBytes("AB00000001"), 0x1a, 0x1a],
  );
  assert.equal(readText(padded, 0), "AB00000001");

  const fallsBack = buildModule(() => {
    const s = new FixedString(11);
    s.write("AB");
    s.append(Var(1));
    when(Always(), () => {});
    s.append("X");
  });

  const unpadded = runSim(fallsBack.toJson(), 2);
  assert.deepEqual(readStringBytes(unpadded, 0), asciiBytes("AB00000001X"));
  assert.equal(readText(unpadded, 0), "AB00000001X");
});

test("padding keeps adjacent strings isolated", () => {
  const mod = buildModule(() => {
    const a = new FixedString(8);
    a.write("AB");
    const b = new FixedString(4);
    b.write("wxyz");
    when(Always(), () => {});
    a.append("C");
    a.display();
    b.display();
  });

  const report = runSim(mod.toJson(), 2);
  assert.equal(readText(report, 0), "ABC");
  assert.deepEqual(readStringBytes(report, 1), asciiBytes("wxyz"));
  assert.ok(
    touchedDwordOffsets(report, 0).every((offset) => offset <= 8),
    "padding must not reach the next string's marker",
  );
});

test("runtime writeFrom clamps and guards positions past capacity", () => {
  const outOfRange = buildModule(() => {
    const s = new FixedString(4);
    s.write("AAAA");
    s.writeFrom(Var(999), "XY");
  });
  const outReport = runSim(outOfRange.toJson());
  assert.deepEqual(
    readStringBytes(outReport, 0),
    asciiBytes("AAAA"),
    "an out-of-range runtime position must not overwrite content",
  );
  assert.ok(
    touchedDwordOffsets(outReport, 0).every((offset) => offset <= 4),
    "guarded writes must not touch bytes past the reserved terminator slot",
  );

  const overrun = buildModule(() => {
    const s = new FixedString(4);
    s.write("AAAA");
    s.writeFrom(Var(2), "XYZ");
  });
  const overrunReport = runSim(overrun.toJson());
  assert.deepEqual(readStringBytes(overrunReport, 0), asciiBytes("AAXY"));
  assert.ok(
    touchedDwordOffsets(overrunReport, 0).every((offset) => offset <= 4),
    "a partially fitting runtime write must stop at capacity",
  );
});

test("adjacent strings stay isolated when one is written at capacity", () => {
  const mod = buildModule(() => {
    const a = new FixedString(4);
    a.write("abcd");
    const b = new FixedString(4);
    b.write("wxyz");
    a.display();
    b.display();
  });

  const parsed = parseModule(mod.toJson());
  assert.equal(parsed.module.strings?.[0], `${" ".repeat(5)}\x7fEUDString#0\x7f`);
  assert.equal(parsed.module.strings?.[1], `${" ".repeat(5)}\x7fEUDString#1\x7f`);

  const report = runSim(mod.toJson());
  assert.deepEqual(readStringBytes(report, 0), asciiBytes("abcd"));
  assert.deepEqual(readStringBytes(report, 1), asciiBytes("wxyz"));
  assert.deepEqual(
    touchedDwordOffsets(report, 0),
    [0, 4],
    "the first string's writes must stop at its terminator slot",
  );
});

test("clear on a small FixedString masks its dword write to size + 1 bytes", () => {
  const mod = buildModule(() => {
    const s = new FixedString(1);
    s.write("A");
    s.clear();
  });

  const acts = flattenActs(parseModule(mod.toJson()).module.stmts);
  const clears = acts.filter(
    (act) =>
      act.mask === 0xffff &&
      act.relocs?.some(
        (reloc) =>
          reloc.target_string === 0 &&
          reloc.field_offset === 16 &&
          reloc.mode === 1,
      ),
  );
  assert.equal(clears.length, 1, "FixedString(1).clear() must use a 2-byte mask");

  const report = runSim(mod.toJson());
  assert.deepEqual(touchedDwordOffsets(report, 0), [0]);
  assert.equal(readDword(report, stringEpd(0)) & 0xffff, 0);
});

test("Byte-identical HIR parity for capacity guards and small clears", () => {
  const tsMod = buildModule(() => {
    const s = new FixedString(4);
    s.write("abcd");
    s.writeFrom(Var(999), "XY");

    const tiny = new FixedString(1);
    tiny.clear();
  });

  const pyScript = `
import sys
from py import FixedString, Var, build_module

def prog():
    s = FixedString(4)
    s.write("abcd")
    s.writeFrom(Var(999), "XY")

    tiny = FixedString(1)
    tiny.clear()

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    maxBuffer: 10 * 1024 * 1024,
  });
  assert.equal(tsMod.toJson(), pyHirJson);
});

test("Byte-identical HIR parity for dynamic decimal and displayAt reset", () => {
  const tsMod = buildModule(() => {
    const a = new FixedString(16);
    a.write("n=");
    when(Always(), () => {});
    a.append("{:d}", Var(42));
    a.displayAt(Var(2));
  });

  const pyScript = `
import sys
from py import Always, FixedString, Var, build_module, when

def prog():
    a = FixedString(16)
    a.write("n=")
    when(Always(), lambda: None)
    a.append("{:d}", Var(42))
    a.displayAt(Var(2))

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    maxBuffer: 8 * 1024 * 1024,
  });
  assert.equal(tsMod.toJson(), pyHirJson);
});
