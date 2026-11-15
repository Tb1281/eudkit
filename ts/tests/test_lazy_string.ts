/**
 * `lazyFixedString` public API tests (globals P4).
 *
 * P4 uses a wrapper plus explicit method delegation (ADR 0005 D8), not a
 * subclass and not a deferred flag on `FixedString`: a handle declared
 * anywhere (no builder needed) that resolves to a fully eager binding on
 * first resolving use inside `buildModule`, one binding per builder. The
 * eager `FixedString` is unchanged; the lazy factory is opt-in only.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  asciiBytes,
  flattenStmts,
  rawStringBytes,
  runSim,
  stringDwordBytes,
} from "./helpers.ts";
import { fileURLToPath } from "node:url";

import {
  FixedString,
  Var,
  buildModule,
  lazyFixedString,
  when,
} from "../index.ts";
import { s as fixtureString } from "./lazy_string_fixture.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

// Full pipeline comparison: `dump-ir` runs optimize_module then
// lowering, so it exercises the storage analysis and the rendered LIR.
// The comparison is the ENTIRE dump-ir output (storage report including
// the per-var Allocations listing, plus the rendered LIR).
function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p4-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    });
    assert.ok(out.includes("LIR v3.0"), "dump-ir output must contain an LIR section");
    assert.ok(
      out.includes("Allocations:"),
      "dump-ir output must contain the Allocations listing",
    );
    return out;
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function hirJson(mod: { toJson(): string }): any {
  return JSON.parse(mod.toJson());
}

function varStoreCount(stmts: any[]): number {
  return flattenStmts(stmts).filter((st) => st.kind === "varstore").length;
}

// Every action that writes through the module string table, regardless of
// whether it sits in a trigger, a temporal body, or at the top level.
function stringWriteActs(stmts: any[]): any[] {
  const acts: any[] = [];
  for (const st of flattenStmts(stmts)) {
    if (Array.isArray(st.acts)) acts.push(...st.acts);
    else if (st.kind === "act") acts.push(st);
  }
  return acts.filter((act: any) =>
    act.relocs?.some((r: any) => r.target_string !== undefined),
  );
}

// The 32 value-bit tests for the fixture's `val` variable (var 0, value
// field offset 20). Shared by the bit-accumulation and lane-folding tests so
// the condition shape is asserted in one place.
function bitTestTriggers(stmts: any[]): any[] {
  return stmts.filter((s: any) => {
    if (s.kind !== "trigger" || !s.conds) return false;
    return s.conds.some(
      (c: any) =>
        c.condtype === 15 &&
        c.relocs?.some((r: any) => r.target_var === 0 && r.target_offset === 20),
    );
  });
}
function decls(mod: { toJson(): string }): Array<[number, number]> {
  return hirJson(mod).module.vars.map((v: any) => [v.id, v.init]);
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

function errorOf(fn: () => unknown): any {
  try {
    fn();
  } catch (e) {
    return e;
  }
  assert.fail("expected the call to throw");
}

function assertDumpEqual(
  lazyBody: () => void,
  eagerBody: () => void,
  label: string,
): void {
  const lazyMod = buildModule(lazyBody);
  const eagerMod = buildModule(eagerBody);
  assert.equal(
    runDumpIr(lazyMod.toJson()),
    runDumpIr(eagerMod.toJson()),
    `${label} must be dump-ir-identical to its eager twin`,
  );
}

test("P4: module-scope declaration allocates nothing; size is pure", () => {
  const s = lazyFixedString(16);
  assert.equal(s.size, 16);
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandle = buildModule(body);
  const withoutHandle = buildModule(body);
  // Identical output proves the unused handle consumed no string index,
  // emitted no decls/flushers, and no statements.
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.deepEqual(decls(withHandle), [[0, 0]]);
  assert.ok(
    runDumpIr(withHandle.toJson()).includes("Total Variables: 1"),
    "an unused handle must not add variables",
  );
});

test("P4: resolving reads and methods outside a builder throw EUDSL-SCOPE", () => {
  const s = lazyFixedString(8);
  assertCode(() => s.messageIndex, "EUDSL-SCOPE");
  assertCode(() => s.write("x"), "EUDSL-SCOPE");
  assertCode(() => s.append("x"), "EUDSL-SCOPE");
  assertCode(() => s.writeFrom(0, "x"), "EUDSL-SCOPE");
  assertCode(() => s.write_from(0, "x"), "EUDSL-SCOPE");
  assertCode(() => s.print("x"), "EUDSL-SCOPE");
  assertCode(() => s.printAt(0, "x"), "EUDSL-SCOPE");
  assertCode(() => s.print_at(0, "x"), "EUDSL-SCOPE");
  assertCode(() => s.display(), "EUDSL-SCOPE");
  assertCode(() => s.displayAt(0), "EUDSL-SCOPE");
  assertCode(() => s.display_at(0), "EUDSL-SCOPE");
  assertCode(() => s.clear(), "EUDSL-SCOPE");
});

test("P4: bounds errors match the eager constructor at declaration", () => {
  const cases: unknown[] = [-1, 0, 1.5, 65536, NaN, "8"];
  for (const size of cases) {
    const lazyError = errorOf(() => lazyFixedString(size as number));
    const eagerError = errorOf(() => new FixedString(size as number));
    assert.equal(lazyError.code, "EUDSL-BOUNDS", `size ${String(size)}: code`);
    assert.equal(
      lazyError.message,
      eagerError.message,
      `size ${String(size)}: message`,
    );
  }
});

test("P4: first-use binding equals the eager twin", () => {
  const lazy = lazyFixedString(16);
  const mod = buildModule(() => {
    lazy.write("Hi");
    const out = Var(0);
    out.set(lazy.messageIndex);
  });
  const twin = buildModule(() => {
    const eager = new FixedString(16);
    eager.write("Hi");
    const out = Var(0);
    out.set(eager.messageIndex);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
  assert.equal(hirJson(mod).module.strings?.length, 1);
});

test("P4: core operations match the eager twin", () => {
  assertDumpEqual(
    () => {
      const s = lazyFixedString(32);
      s.write("ab");
      s.append("cd");
      s.writeFrom(1, "Z");
      s.write("");
      s.clear();
      s.write("tail");
      s.display();
    },
    () => {
      const s = new FixedString(32);
      s.write("ab");
      s.append("cd");
      s.writeFrom(1, "Z");
      s.write("");
      s.clear();
      s.write("tail");
      s.display();
    },
    "static FixedString operations",
  );

  assertDumpEqual(
    () => {
      const s = lazyFixedString(32);
      const pos = Var(3);
      const value = Var(0x41);
      s.write(value);
      s.append("{}", value);
      s.writeFrom(pos, "Q");
      s.printAt(2, "{}", value);
      s.displayAt(pos);
    },
    () => {
      const s = new FixedString(32);
      const pos = Var(3);
      const value = Var(0x41);
      s.write(value);
      s.append("{}", value);
      s.writeFrom(pos, "Q");
      s.printAt(2, "{}", value);
      s.displayAt(pos);
    },
    "runtime FixedString operations",
  );
});

test("P4: out-of-order first use follows first-use order (R1)", () => {
  const a = lazyFixedString(8);
  const b = lazyFixedString(8);

  const lazyIr = runDumpIr(
    buildModule(() => {
      b.write("B");
      a.write("A");
    }).toJson(),
  );
  const bFirstIr = runDumpIr(
    buildModule(() => {
      const bb = new FixedString(8);
      bb.write("B");
      const aa = new FixedString(8);
      aa.write("A");
    }).toJson(),
  );
  const aFirstIr = runDumpIr(
    buildModule(() => {
      const aa = new FixedString(8);
      aa.write("A");
      const bb = new FixedString(8);
      bb.write("B");
    }).toJson(),
  );
  assert.equal(lazyIr, bFirstIr, "B-first eager twin must match");
  assert.notEqual(lazyIr, aFirstIr, "declaration-order twin must differ");
});

test("P4: conditional first use matches the eager twin constructed in the same scope (R4)", () => {
  const lazy = lazyFixedString(8);
  const lazyMod = buildModule(() => {
    const flag = Var(1);
    when(flag.eq(1), () => {
      lazy.write("A");
    });
    lazy.append("B");
    lazy.display();
  });
  const twinMod = buildModule(() => {
    const flag = Var(1);
    let eager: FixedString | null = null;
    when(flag.eq(1), () => {
      eager = new FixedString(8);
      eager.write("A");
    });
    eager!.append("B");
    eager!.display();
  });
  assert.equal(
    runDumpIr(lazyMod.toJson()),
    runDumpIr(twinMod.toJson()),
    "conditional first use must match the same-scope eager twin",
  );
});

test("P4: never-used handles emit nothing and consume no string indices", () => {
  const s = lazyFixedString(8);
  void s;
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandle = buildModule(body);
  const withoutHandle = buildModule(body);
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.deepEqual(decls(withHandle), [[0, 0]]);
});

test("P4: two-builder use creates separate bindings", () => {
  const lazy = lazyFixedString(8);
  let firstIndex = -1;
  let secondIndex = -1;

  const mod1 = buildModule(() => {
    lazy.write("A");
    firstIndex = lazy.messageIndex;
  });
  const mod2 = buildModule(() => {
    new FixedString(8).write("seed");
    lazy.write("B");
    secondIndex = lazy.messageIndex;
  });
  const twin1 = buildModule(() => {
    const eager = new FixedString(8);
    eager.write("A");
  });
  const twin2 = buildModule(() => {
    new FixedString(8).write("seed");
    const eager = new FixedString(8);
    eager.write("B");
  });
  assert.deepEqual(hirJson(mod1), hirJson(twin1), "builder 1 twin");
  assert.deepEqual(hirJson(mod2), hirJson(twin2), "builder 2 twin");
  assert.notEqual(firstIndex, secondIndex, "bindings must have separate string indices");
});

test("P4: post-build read follows the single-record rule; nothing allocates post-build", () => {
  const single = lazyFixedString(8);
  let captured = -1;
  buildModule(() => {
    single.write("A");
    captured = single.messageIndex;
  });
  assert.equal(single.messageIndex, captured, "single record must be cached");

  const multi = lazyFixedString(8);
  buildModule(() => multi.write("A"));
  buildModule(() => multi.write("B"));
  assertCode(() => multi.messageIndex, "EUDSL-SCOPE");

  const never = lazyFixedString(8);
  assertCode(() => never.messageIndex, "EUDSL-SCOPE");

  // A post-build throw and a post-build cached read must both allocate
  // nothing: the next build still starts at id 0 with one var.
  const after = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  assert.deepEqual(decls(after), [[0, 0]]);
  assert.equal(hirJson(after).module.strings?.length ?? 0, 0);
});

test("P4: cross-file handle matches the eager twin", () => {
  assertDumpEqual(
    () => {
      const out = Var(0);
      fixtureString.write("x");
      out.set(fixtureString.messageIndex);
    },
    () => {
      const out = Var(0);
      const eager = new FixedString(8);
      eager.write("x");
      out.set(eager.messageIndex);
    },
    "cross-file FixedString",
  );
});

test("P4: byte-identity corpus (full dump-ir)", () => {
  const cases: Array<{
    name: string;
    lazyBody: () => void;
    eagerBody: () => void;
  }> = [];

  {
    const s = lazyFixedString(32);
    cases.push({
      name: "write/append/writeFrom/clear/messageIndex",
      lazyBody: () => {
        s.write("abc");
        s.append("de");
        s.writeFrom(2, "Z");
        const out = Var(0);
        out.set(s.messageIndex);
        s.clear();
        s.write("done");
        s.display();
      },
      eagerBody: () => {
        const e = new FixedString(32);
        e.write("abc");
        e.append("de");
        e.writeFrom(2, "Z");
        const out = Var(0);
        out.set(e.messageIndex);
        e.clear();
        e.write("done");
        e.display();
      },
    });
  }

  {
    const s = lazyFixedString(32);
    cases.push({
      name: "print and printAt",
      lazyBody: () => {
        s.print("hello");
        s.printAt(3, "{}", 0x2a);
        s.displayAt(0);
      },
      eagerBody: () => {
        const e = new FixedString(32);
        e.print("hello");
        e.printAt(3, "{}", 0x2a);
        e.displayAt(0);
      },
    });
  }

  {
    const s = lazyFixedString(32);
    cases.push({
      name: "runtime values, position, and aliases",
      lazyBody: () => {
        const value = Var(0x41);
        const pos = Var(2);
        s.write("A{}", value);
        s.append(value);
        s.write_from(pos, "Q");
        s.print_at(1, "{}", value);
        s.display_at(pos);
      },
      eagerBody: () => {
        const value = Var(0x41);
        const pos = Var(2);
        const e = new FixedString(32);
        e.write("A{}", value);
        e.append(value);
        e.write_from(pos, "Q");
        e.print_at(1, "{}", value);
        e.display_at(pos);
      },
    });
  }

  {
    const first = lazyFixedString(16);
    const second = lazyFixedString(16);
    cases.push({
      name: "two strings, first-use/flusher order",
      lazyBody: () => {
        second.write("B");
        first.write("A");
        second.append("b");
        first.append("a");
        first.display();
        second.display();
      },
      eagerBody: () => {
        const e2 = new FixedString(16);
        e2.write("B");
        const e1 = new FixedString(16);
        e1.write("A");
        e2.append("b");
        e1.append("a");
        e1.display();
        e2.display();
      },
    });
  }

  {
    const s = lazyFixedString(16);
    cases.push({
      name: "partial static tail then dynamic byte",
      lazyBody: () => {
        const value = Var(0x42);
        s.write("abc");
        s.write(value);
        s.append("f");
        s.display();
      },
      eagerBody: () => {
        const value = Var(0x42);
        const e = new FixedString(16);
        e.write("abc");
        e.write(value);
        e.append("f");
        e.display();
      },
    });
  }

  {
    const s = lazyFixedString(8);
    cases.push({
      name: "full buffer terminator",
      lazyBody: () => {
        s.write("12345678");
        s.display();
      },
      eagerBody: () => {
        const e = new FixedString(8);
        e.write("12345678");
        e.display();
      },
    });
  }

  for (const c of cases) {
    const lazyMod = buildModule(c.lazyBody);
    const eagerMod = buildModule(c.eagerBody);
    assert.equal(
      runDumpIr(lazyMod.toJson()),
      runDumpIr(eagerMod.toJson()),
      `corpus case ${c.name} must be byte-identical`,
    );
  }
});

test("FixedString.write('{:x}', val) direct hex bit-accumulation without shr triggers", () => {
  const mod = buildModule(() => {
    const s = new FixedString(16);
    const v = Var(0xdeadbeef);
    s.write("{:x}", v);
  });

  const data = JSON.parse(mod.toJson());
  const stmts = data.module.stmts;

  const bitTestTrigs = bitTestTriggers(stmts);

  assert.equal(
    bitTestTrigs.length,
    32,
    "Direct bit-accumulation must emit exactly 32 bit-test triggers for val",
  );

  for (const trig of bitTestTrigs) {
    assert.equal(
      trig.acts?.length,
      2,
      "Each bit-test trigger must update both target dword and nibble",
    );
  }

  assert.equal(
    stringDwordBytes(runSim(mod.toJson()), 2).toString("ascii"),
    "deadbeef",
  );
});

test("FixedString.write('W:  {:x} H: {:x}', w, h) multi-placeholder lane-aligned constant folding without shr triggers", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(32);
    captured = s;
    const w = Var(0x12345678);
    const h = Var(0x00000064);
    s.write("W:  {:x} H: {:x}", w, h);
  });

  assert.equal(
    (captured as any).posVar,
    null,
    "Lane-aligned fixed-width format runs must not instantiate runtime pos var",
  );
  assert.equal((captured as any).staticPos, 24);

  const data = JSON.parse(mod.toJson());
  const stmts = data.module.stmts;

  assert.equal(
    varStoreCount(stmts),
    0,
    "constant-offset folding must not patch trigger action fields",
  );
  const initWrites = stringWriteActs(stmts).filter(
    (act: any) =>
      act.acttype === 45 &&
      act.modifier === 7 &&
      act.mask === 0 &&
      act.value === 0x30303030 &&
      act.relocs?.some((r: any) => r.target_string !== undefined),
  );
  assert.deepEqual(
    initWrites
      .map(
        (act: any) =>
          act.relocs.find((r: any) => r.target_string !== undefined).target_offset,
      )
      .sort((a: number, b: number) => a - b),
    [4, 8, 16, 20],
    "Must emit exactly 4 direct dword initializers for two 8-byte hex values",
  );

  assert.equal(
    stringDwordBytes(runSim(mod.toJson()), 6).toString("ascii"),
    "W:  12345678 H: 00000064",
  );
});

test("FixedString multi-placeholder constant folding across byte lanes without mid-string padding", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(32);
    captured = s;
    const w = Var(0xdeadbeef);
    const h = Var(0x0000002a);
    s.write("wave={:x} hp={:x}", w, h);
  });

  assert.equal((captured as any).posVar, null);
  assert.equal((captured as any).staticPos, 25);

  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 26),
    [...asciiBytes("wave=deadbeef hp=0000002a"), 0],
    "content must occupy 0..24 and the NUL must sit at offset 25",
  );
});

test("FixedString unaligned multi-placeholder fallback to dynamic byte writes", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(32);
    captured = s;
    const start = Var(0);
    const w = Var(0x12345678);
    const h = Var(0x00000064);
    s.writeFrom(start, "W:{:x} H:{:x}", w, h);
  });

  assert.notEqual(
    (captured as any).posVar,
    null,
    "Unaligned multi-placeholder must fall back to dynamic position variable",
  );

  assert.equal(
    stringDwordBytes(runSim(mod.toJson()), 6)
      .toString("ascii")
      .slice(0, 21),
    "W:12345678 H:00000064",
  );
});

test("fixed-width decimal capacity uses the declared width", () => {
  buildModule(() => {
    const s = new FixedString(4);
    s.write("{:4d}", Var(1));
  });

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(3);
        s.write("{:4d}", Var(1));
      }),
    "EUDSL-BOUNDS",
  );
});

test("exact-fit placeholder-plus-literal run terminates at capacity", () => {
  const mod = buildModule(() => {
    const s = new FixedString(10);
    s.write("a{:x}b", Var(0xdeadbeef));
  });
  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 11),
    [...asciiBytes("adeadbeefb"), 0],
    "content must occupy 0..9 and the NUL must sit at offset 10",
  );
});

test("exact-fit multi-placeholder run terminates at capacity", () => {
  const mod = buildModule(() => {
    const s = new FixedString(25);
    s.write("wave={:x} hp={:x}", Var(0xdeadbeef), Var(0x2a));
  });
  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 26),
    [...asciiBytes("wave=deadbeef hp=0000002a"), 0],
    "content must occupy 0..24 and the NUL must sit at offset 25",
  );
});

test("unaligned fold fits exact capacity", () => {
  const mod = buildModule(() => {
    const s = new FixedString(10);
    s.write("a{:x}b", Var(0xdeadbeef));
  });
  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 11),
    [...asciiBytes("adeadbeefb"), 0],
    "an unaligned fold must still fit exactly without writing past the NUL",
  );
});

test("unaligned fixed-width hex folds to compile-time byte-lane writes", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(10);
    captured = s;
    s.write("a{:x}b", Var(0xdeadbeef));
  });

  assert.equal(
    (captured as any).posVar,
    null,
    "unaligned fixed-width hex must not instantiate a runtime position var",
  );

  const stmts = hirJson(mod).module.stmts;
  assert.equal(
    varStoreCount(stmts),
    0,
    "fixed-width hex must not patch trigger action fields at runtime",
  );

  const bitTestTrigs = bitTestTriggers(stmts);
  assert.equal(
    bitTestTrigs.length,
    32,
    "direct folding must emit exactly the 32 value bit tests, with no pos>>2 triggers",
  );

  const offsets = new Set(
    stringWriteActs(stmts).map(
      (act: any) =>
        act.relocs.find((r: any) => r.target_string !== undefined).target_offset,
    ),
  );
  assert.deepEqual(
    [...offsets].sort((a, b) => a - b),
    [0, 4, 8],
    "an 8-byte value starting at lane 1 must cover dwords 0, 1, and 2",
  );

  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 11),
    [...asciiBytes("adeadbeefb"), 0],
    "unaligned direct writes must render the value at its byte lane",
  );
});

test("fixed-width hex folds at every byte lane without runtime shifts", () => {
  for (const lane of [0, 1, 2, 3]) {
    let captured: FixedString | null = null;
    const mod = buildModule(() => {
      const s = new FixedString(lane + 8);
      captured = s;
      s.writeFrom(lane, "{:x}", Var(0xdeadbeef));
    });

    assert.equal(
      (captured as any).posVar,
      null,
      `lane ${lane}: fixed-width hex must not instantiate a runtime position var`,
    );

    const stmts = hirJson(mod).module.stmts;
    assert.equal(
      varStoreCount(stmts),
      0,
      `lane ${lane}: fixed-width hex must not patch trigger action fields`,
    );

    const bitTestTrigs = bitTestTriggers(stmts);
    assert.equal(
      bitTestTrigs.length,
      32,
      `lane ${lane}: exactly the 32 value bit tests, with no pos>>2 triggers`,
    );

    const initOffsets = stringWriteActs(stmts)
      .filter((act: any) => act.value === 0x30303030)
      .map(
        (act: any) =>
          act.relocs.find((r: any) => r.target_string !== undefined).target_offset,
      );
    assert.deepEqual(
      [...new Set(initOffsets)].sort((a, b) => a - b),
      lane === 0 ? [0, 4] : [0, 4, 8],
      `lane ${lane}: 8 bytes must cover the expected dwords`,
    );

    const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
    assert.deepEqual(
      rawStringBytes(epds, lane + 9),
      [...new Array<number>(lane).fill(0), ...asciiBytes("deadbeef"), 0],
      `lane ${lane}: value must render at its byte lane`,
    );
  }
});

test("write('a{:x}') folds without dynamic fallback", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(9);
    captured = s;
    s.write("a{:x}", Var(0xdeadbeef));
  });

  assert.equal((captured as any).posVar, null);
  const stmts = hirJson(mod).module.stmts;
  assert.equal(varStoreCount(stmts), 0);
  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(rawStringBytes(epds, 10), [...asciiBytes("adeadbeef"), 0]);
});

test("mixed placeholders in one run fold without mid-string padding", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(22);
    captured = s;
    s.write("a{:x}b{:x}c", Var(0xdeadbeef), Var(0x0000002a));
  });

  assert.equal((captured as any).posVar, null);
  assert.equal((captured as any).staticPos, 19);
  const stmts = hirJson(mod).module.stmts;
  assert.equal(varStoreCount(stmts), 0);

  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 20),
    [...asciiBytes("adeadbeefb0000002ac"), 0],
    "both placeholders fold at their compile-time byte lanes",
  );
});

test("{:d} keeps the dynamic fallback and remains position-tracked", () => {
  let captured: FixedString | null = null;
  const mod = buildModule(() => {
    const s = new FixedString(16);
    captured = s;
    s.write("{:d}", Var(1234));
  });

  assert.notEqual(
    (captured as any).posVar,
    null,
    "decimal formatting still needs the runtime position variable",
  );
  const module = hirJson(mod).module;
  // Variable-width decimal now goes through the one shared `func` body per
  // module, so its `pos >> 2` cursor seed (the dynamic patching fallback) is
  // emitted in `module.funcs` rather than at the call site.
  const kernelStmts = (module.funcs ?? []).flatMap((fn: any) => fn.body ?? []);
  assert.ok(
    varStoreCount(module.stmts) + varStoreCount(kernelStmts) > 0,
    "decimal formatting keeps the documented dynamic patching fallback",
  );

  const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 5),
    [...asciiBytes("1234"), 0],
    "decimal formatting must still render variable-width digits",
  );
});

test("Byte-identical HIR parity for capacity-checked format runs", () => {
  const tsMod = buildModule(() => {
    const a = new FixedString(10);
    a.write("a{:x}b", Var(0xdeadbeef));
    const b = new FixedString(25);
    b.write("wave={:x} hp={:x}", Var(0xdeadbeef), Var(0x2a));
  });

  const pyScript = [
    "import sys",
    "from py import FixedString, Var, build_module",
    "def prog():",
    "    a = FixedString(10)",
    "    a.write('a{:x}b', Var(0xDEADBEEF))",
    "    b = FixedString(25)",
    "    b.write('wave={:x} hp={:x}', Var(0xDEADBEEF), Var(0x2A))",
    "sys.stdout.write(build_module(prog).to_json())",
  ].join("\n");

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  assert.equal(tsMod.toJson(), pyHirJson);
});

test("Byte-identical HIR parity for unaligned fixed-width hex folds", () => {
  const tsMod = buildModule(() => {
    const a = new FixedString(9);
    a.write("a{:x}", Var(0xdeadbeef));
    const b = new FixedString(11);
    b.writeFrom(3, "{:x}", Var(0x0000002a));
    const c = new FixedString(22);
    c.write("a{:x}b{:x}c", Var(0xdeadbeef), Var(0x0000002a));
  });

  const pyScript = [
    "import sys",
    "from py import FixedString, Var, build_module",
    "def prog():",
    "    a = FixedString(9)",
    "    a.write('a{:x}', Var(0xDEADBEEF))",
    "    b = FixedString(11)",
    "    b.writeFrom(3, '{:x}', Var(0x0000002A))",
    "    c = FixedString(22)",
    "    c.write('a{:x}b{:x}c', Var(0xDEADBEEF), Var(0x0000002A))",
    "sys.stdout.write(build_module(prog).to_json())",
  ].join("\n");

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  assert.equal(tsMod.toJson(), pyHirJson);
});

test("Byte-identical HIR parity for multi-placeholder constant-offset folded format strings", () => {
  const tsMod = buildModule(() => {
    const s = new FixedString(32);
    const w = Var(0x12345678);
    const h = Var(0x00000064);
    s.write("W:  {:x} H: {:x}", w, h);
  });

  const pyScript = [
    "import sys",
    "from py import FixedString, Var, build_module",
    "def prog():",
    "    s = FixedString(32)",
    "    w = Var(0x12345678)",
    "    h = Var(0x00000064)",
    "    s.write('W:  {:x} H: {:x}', w, h)",
    "sys.stdout.write(build_module(prog).to_json())",
  ].join("\n");

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  assert.equal(tsMod.toJson(), pyHirJson);
});

test("FixedString capacity is monotonic: write('a{:x}b', v) succeeds for 10, 11, and 12", () => {
  for (const size of [10, 11, 12]) {
    const mod = buildModule(() => {
      const s = new FixedString(size);
      s.write("a{:x}b", Var(0xdeadbeef));
    });
    const epds = runSim(mod.toJson()).external_epds as Record<string, number>;
    assert.deepEqual(
      rawStringBytes(epds, 11),
      [...asciiBytes("adeadbeefb"), 0],
      `content must fit without padding in FixedString(${size})`,
    );
  }

  // size 9 must fail because 1 + 8 + 1 = 10 content bytes exceeds capacity
  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(9);
        s.write("a{:x}b", Var(0xdeadbeef));
      }),
    "EUDSL-BOUNDS",
  );
});

test("FixedString capacity is monotonic for multi-placeholder runs without mid-string padding", () => {
  // "wave={:x} hp={:x}" is 5 + 8 + 4 + 8 = 25 content bytes.
  // FixedString(25) is an exact fit; FixedString(24) fails.
  const mod25 = buildModule(() => {
    const s = new FixedString(25);
    s.write("wave={:x} hp={:x}", Var(0xdeadbeef), Var(0x2a));
  });
  const epds = runSim(mod25.toJson()).external_epds as Record<string, number>;
  assert.deepEqual(
    rawStringBytes(epds, 26),
    [...asciiBytes("wave=deadbeef hp=0000002a"), 0],
    "content must occupy 0..24 and NUL must sit at offset 25",
  );

  assertCode(
    () =>
      buildModule(() => {
        const s = new FixedString(24);
        s.write("wave={:x} hp={:x}", Var(0xdeadbeef), Var(0x2a));
      }),
    "EUDSL-BOUNDS",
  );
});

test("Byte-identical HIR parity for unpadded multi-lane hex placeholders", () => {
  const tsMod = buildModule(() => {
    const a = new FixedString(10);
    a.write("a{:x}b", Var(0xdeadbeef));
    const b = new FixedString(25);
    b.write("wave={:x} hp={:x}", Var(0xdeadbeef), Var(0x2a));
  });

  const pyScript = [
    "import sys",
    "from py import FixedString, Var, build_module",
    "def prog():",
    "    a = FixedString(10)",
    "    a.write('a{:x}b', Var(0xDEADBEEF))",
    "    b = FixedString(25)",
    "    b.write('wave={:x} hp={:x}', Var(0xDEADBEEF), Var(0x2A))",
    "sys.stdout.write(build_module(prog).to_json())",
  ].join("\n");

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });

  assert.equal(tsMod.toJson(), pyHirJson);
});
