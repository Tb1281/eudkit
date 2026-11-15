/**
 * `lazyStruct` public API tests (globals P2-02).
 *
 * `lazyStruct` is the thin public factory over a deferred struct handle:
 * a handle declared anywhere (no builder needed) that resolves to a fully
 * eager binding on first use inside `buildModule`, one binding per
 * builder. Eager `TriggerStruct.instantiate` is unchanged; `lazyStruct`
 * is opt-in only. Binding creation is atomic: one full eager
 * `instantiate` (page fit plus ALL K field vars together).
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { fileURLToPath } from "node:url";

import {
  TriggerStruct,
  Var,
  bit,
  buildModule,
  byte,
  dword,
  lazyStruct,
  when,
  word,
} from "../index.ts";
import { s as fixtureS } from "./lazy_struct_fixture.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

const Point = TriggerStruct("Point", { x: dword(), y: dword() });
const Single = TriggerStruct("Single", { v: dword() });
const Small = TriggerStruct("Small", {
  b: byte(),
  w: word(),
  f: bit(3, 5),
});

function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p202-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf-8",
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

function decls(mod: { toJson(): string }): Array<[number, number]> {
  return hirJson(mod).module.vars.map((v: any) => [v.id, v.init]);
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

test("P2-02: lazyStruct declaration outside buildModule allocates nothing", () => {
  const handle = lazyStruct(Point, { x: 7 });
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandle = buildModule(body);
  const withoutHandle = buildModule(body);
  // Identical output proves the unused handle consumed no ids, emitted no
  // decls, and emitted no statement.
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.deepEqual(decls(withHandle), [[0, 0]]);
});

test("P2-02: pure reads never resolve; resolving reads outside throw EUDSL-SCOPE", () => {
  const handle = lazyStruct(Point, { x: 3, y: 4 });
  // Pure reads: schema, field names, field count — no builder needed.
  assert.equal(handle._schema, Point);
  assert.deepEqual([...handle._fieldNames], ["x", "y"]);
  assert.equal(handle.fieldCount, 2);
  // Resolving reads outside a builder throw.
  assertCode(() => handle.pageIndex, "EUDSL-SCOPE");
  assertCode(() => handle.slotIndex, "EUDSL-SCOPE");
  assertCode(() => handle.x, "EUDSL-SCOPE");
  assertCode(() => handle.fields, "EUDSL-SCOPE");
});

test("P2-02: builder-time fields/slotIndex reads resolve like the eager twin", () => {
  const slotLazy = lazyStruct(Point);
  let lazySlot = -1;
  const lazySlotMod = buildModule(() => {
    // Reading slotIndex is the first resolving use: it must create the
    // binding before any field write.
    lazySlot = slotLazy.slotIndex;
    slotLazy.x.set(1);
  });

  let eagerSlot = -1;
  const eagerSlotMod = buildModule(() => {
    const e = Point.instantiate();
    eagerSlot = e.slotIndex;
    e.x.set(1);
  });

  assert.deepEqual(hirJson(lazySlotMod), hirJson(eagerSlotMod));
  assert.equal(lazySlot, eagerSlot);

  const fieldsLazy = lazyStruct(Point);
  let lazyFieldIds: number[] = [];
  const lazyFieldsMod = buildModule(() => {
    // Reading fields is the first resolving use: it must create the
    // binding before any field write.
    lazyFieldIds = fieldsLazy.fields.map((field) => field.id);
    fieldsLazy.x.set(1);
  });

  let eagerFieldIds: number[] = [];
  const eagerFieldsMod = buildModule(() => {
    const e = Point.instantiate();
    eagerFieldIds = e.fields.map((field) => field.id);
    e.x.set(1);
  });

  assert.deepEqual(hirJson(lazyFieldsMod), hirJson(eagerFieldsMod));
  assert.deepEqual(lazyFieldIds, eagerFieldIds);
});

test("P2-02: first-use binding equals the eager twin", () => {
  const lazy = lazyStruct(Point);
  const mod = buildModule(() => {
    lazy.x.set(5);
    lazy.y.iadd(1);
  });
  const twin = buildModule(() => {
    const e = Point.instantiate();
    e.x.set(5);
    e.y.iadd(1);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
  assert.deepEqual(decls(mod), [
    [0, 0],
    [1, 0],
  ]);
});

test("P2-02: inits dict and per-field masking match the eager twin", () => {
  // Note: eager masking applies the raw value against the shifted mask,
  // so the bit(3,5) init must already be shifted (0b111 << 5) to survive.
  const lazy = lazyStruct(Small, { b: 0x1ff, w: 0x12345, f: 0b111 << 5 });
  const mod = buildModule(() => {
    lazy.b.set(1);
  });
  const twin = buildModule(() => {
    const e = Small.instantiate({ b: 0x1ff, w: 0x12345, f: 0b111 << 5 });
    e.b.set(1);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
  // Masked inits: byte keeps 0xff, word keeps 0x2345, bit(3,5) keeps 0xe0.
  assert.deepEqual(decls(mod), [
    [0, 0xff],
    [1, 0x2345],
    [2, 0xe0],
  ]);
});

test("P2-02: non-number, instance, and unknown-field inits throw eagerly", () => {
  // Unknown-field validation is pure: it throws at the call, outside any
  // builder, with no allocation attempted.
  assertCode(() => lazyStruct(Point, { z: 1 } as any), "EUDSL-UNKNOWN-FIELD");
  // Var/Expr/instance values need a builder to exist, so those throws are
  // asserted inside a build; the throw is still at the lazyStruct call,
  // before any binding is created.
  buildModule(() => {
    const v = Var(1);
    assertCode(() => lazyStruct(Point, { x: v as any }), "EUDSL-TYPE");
    assertCode(() => lazyStruct(Point, { x: v.add(1) as any }), "EUDSL-TYPE");
    const eager = Point.instantiate();
    assertCode(() => lazyStruct(Point, eager as any), "EUDSL-TYPE");
  });
});

test("P2-02: set and copyFrom paths match eager twins", () => {
  const lazy = lazyStruct(Point, { x: 1, y: 2 });
  const mod = buildModule(() => {
    lazy.set({ x: 10 });
    const e = Point.instantiate({ x: 20, y: 30 });
    lazy.copyFrom(e);
    e.copyFrom(lazy);
    const target = Point.instantiate();
    target.set(lazy);
  });
  const twin = buildModule(() => {
    const l = Point.instantiate({ x: 1, y: 2 });
    l.set({ x: 10 });
    const e = Point.instantiate({ x: 20, y: 30 });
    l.copyFrom(e);
    e.copyFrom(l);
    const target = Point.instantiate();
    target.set(l);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
});

test("P2-02: field use in when conditions matches the eager twin", () => {
  const lazy = lazyStruct(Point);
  const mod = buildModule(() => {
    const out = Var(0);
    when(lazy.x.eq(6), () => {
      out.set(1);
    });
  });
  // Eager twin in first-use order: `out` is declared before the binding
  // is first touched (inside the when cond), matching the lazy order.
  const twin = buildModule(() => {
    const out = Var(0);
    const e = Point.instantiate();
    when(e.x.eq(6), () => {
      out.set(1);
    });
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
});

test("P2-02: out-of-order first use follows first-use order (dump-ir)", () => {
  const a = lazyStruct(Point, { x: 11, y: 12 });
  const b = lazyStruct(Point, { x: 21, y: 22 });
  const mod = buildModule(() => {
    b.x.set(1);
    a.y.set(2);
  });
  const reordered = buildModule(() => {
    const bb = Point.instantiate({ x: 21, y: 22 });
    const aa = Point.instantiate({ x: 11, y: 12 });
    bb.x.set(1);
    aa.y.set(2);
  });
  const declared = buildModule(() => {
    const aa = Point.instantiate({ x: 11, y: 12 });
    const bb = Point.instantiate({ x: 21, y: 22 });
    bb.x.set(1);
    aa.y.set(2);
  });
  assert.equal(
    runDumpIr(mod.toJson()),
    runDumpIr(reordered.toJson()),
  );
  assert.notEqual(
    runDumpIr(mod.toJson()),
    runDumpIr(declared.toJson()),
  );
});

test("P2-02: conditional first use with const init matches the eager twin (dump-ir)", () => {
  const lazy = lazyStruct(Point, { x: 5, y: 6 });
  const mod = buildModule(() => {
    const flag = Var(0);
    when(flag.eq(0), () => {
      lazy.x.set(9);
    });
  });
  const twin = buildModule(() => {
    const flag = Var(0);
    const e = Point.instantiate({ x: 5, y: 6 });
    when(flag.eq(0), () => {
      e.x.set(9);
    });
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-02: never-used lazyStruct emits nothing", () => {
  const handle = lazyStruct(Point, { x: 1, y: 2 });
  void handle;
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandle = buildModule(body);
  const withoutHandle = buildModule(body);
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.ok(runDumpIr(withHandle.toJson()).includes("Total Variables: 1"));
});

test("P2-02: two-builder use gives separate bindings with different ids", () => {
  const handle = lazyStruct(Point);
  const mod1 = buildModule(() => {
    handle.x.set(1);
  });
  const mod2 = buildModule(() => {
    const other = Var(9);
    other.set(2);
    handle.y.set(3);
  });
  const twin1 = buildModule(() => {
    const e = Point.instantiate();
    e.x.set(1);
  });
  const twin2 = buildModule(() => {
    const other = Var(9);
    other.set(2);
    const e = Point.instantiate();
    e.y.set(3);
  });
  assert.deepEqual(hirJson(mod1), hirJson(twin1));
  assert.deepEqual(hirJson(mod2), hirJson(twin2));
  // Different ids prove the bindings are per-builder, not shared.
  assert.notDeepEqual(decls(mod1), decls(mod2).slice(1));
});

test("P2-02: cross-file fixture use matches the eager twin", () => {
  const mod = buildModule(() => {
    fixtureS.x.iadd(1);
    fixtureS.y.set(8);
  });
  const twin = buildModule(() => {
    const e = Point.instantiate({ x: 3, y: 4 });
    e.x.iadd(1);
    e.y.set(8);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
});

test("P2-02: post-build single-record read; multi-builder read is ambiguous", () => {
  const single = lazyStruct(Point);
  let captured = -1;
  buildModule(() => {
    single.x.set(1);
    captured = single.pageIndex;
  });
  // Resolved under exactly one builder: the cached record reads post-build.
  assert.equal(single.pageIndex, captured);
  assert.equal(single.x.id, 0);

  const multi = lazyStruct(Point);
  buildModule(() => {
    multi.x.set(1);
  });
  buildModule(() => {
    multi.y.set(2);
  });
  assertCode(() => multi.pageIndex, "EUDSL-SCOPE");
});

test("P2-02: then/toJSON/symbol reads do not resolve", () => {
  const handle = lazyStruct(Point, { x: 1, y: 2 });
  // Outside a builder these must not throw: no binding is created.
  assert.equal((handle as any).then, undefined);
  assert.equal((handle as any).toJSON, undefined);
  assert.equal((handle as any)[Symbol.iterator], undefined);
  // And a build that never uses the handle allocates nothing for it.
  const mod = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  assert.deepEqual(decls(mod), [[0, 0]]);
});

test("P2-02: byte-identity corpus (dump-ir)", () => {
  const cases: Array<{
    name: string;
    lazyBody: () => void;
    eagerBody: () => void;
  }> = [];
  {
    const s = lazyStruct(Single);
    cases.push({
      name: "single-field",
      lazyBody: () => {
        s.v.set(42);
      },
      eagerBody: () => {
        const e = Single.instantiate();
        e.v.set(42);
      },
    });
  }
  {
    const s = lazyStruct(Point, { x: 3, y: 4 });
    cases.push({
      name: "multi-field inits",
      lazyBody: () => {
        s.x.iadd(1);
        s.y.set(9);
      },
      eagerBody: () => {
        const e = Point.instantiate({ x: 3, y: 4 });
        e.x.iadd(1);
        e.y.set(9);
      },
    });
  }
  {
    const s = lazyStruct(Small, { b: 0xab, w: 0x1234, f: 0b101 });
    cases.push({
      name: "masked/shifted specs",
      lazyBody: () => {
        s.f.set(0b010);
      },
      eagerBody: () => {
        const e = Small.instantiate({ b: 0xab, w: 0x1234, f: 0b101 });
        e.f.set(0b010);
      },
    });
  }
  {
    const s = lazyStruct(Point);
    cases.push({
      name: "set/copyFrom paths",
      lazyBody: () => {
        const e = Point.instantiate({ x: 7, y: 8 });
        s.copyFrom(e);
        s.set({ y: 10 });
      },
      eagerBody: () => {
        const e = Point.instantiate({ x: 7, y: 8 });
        const l = Point.instantiate();
        l.copyFrom(e);
        l.set({ y: 10 });
      },
    });
  }
  {
    const s = lazyStruct(Point);
    cases.push({
      name: "field in when",
      lazyBody: () => {
        const out = Var(0);
        when(s.y.eq(2), () => {
          out.set(1);
        });
      },
      eagerBody: () => {
        // First-use order: `out` before the binding (touched in the cond).
        const out = Var(0);
        const e = Point.instantiate();
        when(e.y.eq(2), () => {
          out.set(1);
        });
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

test("P2-02: sim output matches the eager twin", () => {
  const lazy = lazyStruct(Point, { x: 5, y: 7 });
  const lazyMod = buildModule(() => {
    const out = Var(0);
    out.set(lazy.x);
    out.iadd(lazy.y);
    lazy.x.set(11);
  });
  const eagerMod = buildModule(() => {
    // First-use order: `out` before the binding (touched by out.set).
    const out = Var(0);
    const e = Point.instantiate({ x: 5, y: 7 });
    out.set(e.x);
    out.iadd(e.y);
    e.x.set(11);
  });
  assert.deepEqual(runSim(lazyMod.toJson()), runSim(eagerMod.toJson()));
});
