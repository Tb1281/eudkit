/**
 * `lazyFixedArray` public API tests (globals P2-01).
 *
 * `lazyFixedArray` is the thin public factory over the P1-01 deferred
 * base: a handle declared anywhere (no builder needed) that resolves to
 * a fully eager binding on first use inside `buildModule`, one binding
 * per builder. Binding creation is atomic (R3): one full eager
 * `FixedArray` construction — page alignment plus ALL element vars, or
 * ALL struct instances. Eager `FixedArray` is unchanged; `lazyFixedArray`
 * is opt-in only.
 *
 * Shape note: the factory returns a genuine deferred-mode
 * `FixedArrayImpl`, not a wrapper (Py `set_all` dispatches on
 * `isinstance(src, FixedArrayImpl)`).
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  FixedArray,
  TriggerStruct,
  Var,
  buildModule,
  dword,
  lazyFixedArray,
  when,
} from "../index.ts";
import { arr as fixtureArr } from "./lazy_fixed_array_fixture.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

// Full pipeline comparison: `dump-ir` runs optimize_module then
// lowering, so it exercises the opt desugar and the storage analysis.
// The comparison is the ENTIRE dump-ir output (storage report
// including the per-var Allocations listing, plus the rendered LIR).
function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p201-"));
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

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

test("P2-01: module-scope declaration never throws; pure reads work outside a builder", () => {
  const a = lazyFixedArray("dword", 3);
  assert.equal(a.length, 3);
  assert.equal(a.elemType, "dword");
  assert.equal(a.mask, 0xffffffff);
  assert.equal(a.bits, 2);
  assert.equal(a.pageCount, 1);
  assert.equal(a.pages, 1);
  assert.equal(a.isConst, false);
  const big = lazyFixedArray("dword", 65);
  assert.equal(big.pageCount, 2);
  const Hero = TriggerStruct("Hero", { hp: dword() });
  const heroes = lazyFixedArray(Hero, 2);
  assert.equal(heroes.pageCount, 2);
});

test("P2-01: bounds/type errors throw eagerly at declaration", () => {
  assertCode(() => lazyFixedArray("dword", 0), "EUDSL-BOUNDS");
  assertCode(() => lazyFixedArray("dword", -1), "EUDSL-BOUNDS");
  assertCode(() => lazyFixedArray("dword", 1.5), "EUDSL-BOUNDS");
  assertCode(() => lazyFixedArray("nope" as any, 3), "EUDSL-BOUNDS");
});

test("P2-01: resolving use outside a builder throws EUDSL-SCOPE", () => {
  const a = lazyFixedArray("dword", 2);
  assertCode(() => a.get(0), "EUDSL-SCOPE");
  assertCode(() => a.set(0, 1), "EUDSL-SCOPE");
  assertCode(() => a.vars, "EUDSL-SCOPE");
  assertCode(() => a.instances, "EUDSL-SCOPE");
});

test("P2-01 corpus: basic lazy program is dump-ir-identical to its eager twin", () => {
  const a = lazyFixedArray("dword", 3);
  const mod = buildModule(() => {
    a.set(0, 11);
    a.set(2, 22);
    const out = Var(0);
    out.set(a.get(0));
    when(a.eq(0, 11), () => {
      out.iadd(1);
    });
  });
  const twin = buildModule(() => {
    const arr = FixedArray("dword", 3);
    arr.set(0, 11);
    arr.set(2, 22);
    const out = Var(0);
    out.set(arr.get(0));
    when(arr.eq(0, 11), () => {
      out.iadd(1);
    });
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01 corpus: byte-element lazy array is dump-ir-identical to its eager twin", () => {
  const a = lazyFixedArray("byte", 5);
  const mod = buildModule(() => {
    a.set(1, 7);
    a.set(4, 9);
    const out = Var(0);
    out.set(a.get(4));
  });
  const twin = buildModule(() => {
    const arr = FixedArray("byte", 5);
    arr.set(1, 7);
    arr.set(4, 9);
    const out = Var(0);
    out.set(arr.get(4));
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01 corpus: struct-element lazy array is dump-ir-identical to its eager twin", () => {
  const Hero = TriggerStruct("Hero", { hp: dword(), mp: "word" });
  const a = lazyFixedArray(Hero, 2);
  const mod = buildModule(() => {
    a.get(0).hp.set(100);
    a.get(1).mp.set(50);
    const out = Var(0);
    out.set(a.get(0).hp);
  });
  const twin = buildModule(() => {
    const arr = FixedArray(Hero, 2);
    arr.get(0).hp.set(100);
    arr.get(1).mp.set(50);
    const out = Var(0);
    out.set(arr.get(0).hp);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01 corpus: dynamic-index get/set on a lazy array is dump-ir-identical to its eager twin", () => {
  const a = lazyFixedArray("dword", 4);
  const mod = buildModule(() => {
    const idx = Var(1);
    a.set(idx, 99);
    const out = Var(0);
    out.set(a.get(idx));
  });
  const twin = buildModule(() => {
    const idx = Var(1);
    const arr = FixedArray("dword", 4);
    arr.set(idx, 99);
    const out = Var(0);
    out.set(arr.get(idx));
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01 corpus: clear/setAll/eq on a lazy array is dump-ir-identical to its eager twin", () => {
  const a = lazyFixedArray("dword", 4);
  const mod = buildModule(() => {
    const src = FixedArray("dword", 4);
    a.clear();
    a.setAll(src);
    const out = Var(0);
    when(a.eq(0, 0), () => {
      out.iadd(1);
    });
  });
  const twin = buildModule(() => {
    const src = FixedArray("dword", 4);
    const arr = FixedArray("dword", 4);
    arr.clear();
    arr.setAll(src);
    const out = Var(0);
    when(arr.eq(0, 0), () => {
      out.iadd(1);
    });
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01: eager array copies from a lazy array, and lazy from eager", () => {
  const a = lazyFixedArray("dword", 3);
  const mod = buildModule(() => {
    const e = FixedArray("dword", 3);
    e.setAll(a);
    a.setAll(e);
    const out = Var(0);
    out.set(e.get(0));
  });
  const twin = buildModule(() => {
    const e = FixedArray("dword", 3);
    const arr = FixedArray("dword", 3);
    e.setAll(arr);
    arr.setAll(e);
    const out = Var(0);
    out.set(e.get(0));
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01: out-of-order first use follows first-use order, not declaration order", () => {
  const A = lazyFixedArray("dword", 2);
  const B = lazyFixedArray("dword", 2);
  const mod = buildModule(() => {
    B.set(0, 22);
    A.set(0, 11);
  });
  const twinBFirst = buildModule(() => {
    const b = FixedArray("dword", 2);
    const a = FixedArray("dword", 2);
    b.set(0, 22);
    a.set(0, 11);
  });
  const twinAFirst = buildModule(() => {
    const a = FixedArray("dword", 2);
    const b = FixedArray("dword", 2);
    b.set(0, 22);
    a.set(0, 11);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twinBFirst.toJson()));
  assert.notEqual(runDumpIr(mod.toJson()), runDumpIr(twinAFirst.toJson()));
});

test("P2-01: conditional first use is dump-ir-identical to its eager twin", () => {
  const a = lazyFixedArray("dword", 2);
  const mod = buildModule(() => {
    const flag = Var(1);
    when(flag.eq(1), () => {
      a.set(0, 5);
    });
    const out = Var(0);
    out.set(a.get(0));
  });
  // Twin in first-use order: flag first, then the array (the lazy binding
  // happens inside the when, after flag allocation).
  const twin = buildModule(() => {
    const flag = Var(1);
    const arr = FixedArray("dword", 2);
    when(flag.eq(1), () => {
      arr.set(0, 5);
    });
    const out = Var(0);
    out.set(arr.get(0));
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01: never-used lazy array emits nothing", () => {
  const _unused = lazyFixedArray("dword", 4);
  const mod = buildModule(() => {
    const out = Var(0);
    out.set(1);
  });
  const twin = buildModule(() => {
    const out = Var(0);
    out.set(1);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01: two-builder use gives separate bindings with different ids", () => {
  const a = lazyFixedArray("dword", 2);
  const mod1 = buildModule(() => {
    a.set(0, 1);
    const v = Var(0);
    v.set(a.get(0));
  });
  const mod2 = buildModule(() => {
    const v = Var(0);
    a.set(0, 1);
    v.set(a.get(0));
  });
  const twin1 = buildModule(() => {
    const arr = FixedArray("dword", 2);
    arr.set(0, 1);
    const v = Var(0);
    v.set(arr.get(0));
  });
  const twin2 = buildModule(() => {
    const v = Var(0);
    const arr = FixedArray("dword", 2);
    arr.set(0, 1);
    v.set(arr.get(0));
  });
  assert.equal(runDumpIr(mod1.toJson()), runDumpIr(twin1.toJson()));
  assert.equal(runDumpIr(mod2.toJson()), runDumpIr(twin2.toJson()));
});

test("P2-01: cross-file lazy array is dump-ir-identical to its eager twin", () => {
  const mod = buildModule(() => {
    fixtureArr.set(0, 5);
    fixtureArr.set(2, 9);
    const out = Var(0);
    out.set(fixtureArr.get(2));
  });
  const twin = buildModule(() => {
    const arr = FixedArray("dword", 3);
    arr.set(0, 5);
    arr.set(2, 9);
    const out = Var(0);
    out.set(arr.get(2));
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

test("P2-01: post-build read follows the single-record rule", () => {
  const single = lazyFixedArray("dword", 2);
  buildModule(() => {
    single.set(0, 7);
  });
  // Resolved under exactly one builder: cached record, no throw.
  assert.equal(single.vars.length, 2);

  const multi = lazyFixedArray("dword", 2);
  buildModule(() => {
    multi.set(0, 1);
  });
  buildModule(() => {
    multi.set(0, 2);
  });
  // Resolved under two builders: post-build read is ambiguous.
  assertCode(() => multi.vars, "EUDSL-SCOPE");
});
