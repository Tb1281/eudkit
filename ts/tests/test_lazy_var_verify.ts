/**
 * `lazyVar` verification corpus + differential probes (globals P1-03).
 *
 * P1-02 proved HIR equality between lazy programs and their eager twins.
 * This file proves the full pipeline: every corpus program must compile
 * byte-identical through `dump-ir` (storage report including the
 * per-var Allocations listing, plus the rendered LIR), not just at the
 * HIR level. It also files the §8 differential probes that P1-02 did
 * not cover at pipeline level: never-used `total_vars` via dump-ir,
 * and the single-entry-point probe.
 *
 * Shape note: `lazyVar` is a thin factory over `VarImpl`/`Var`
 * directly (P1-01 deferred base, P1-02 factory). There is no
 * `LazyVarImpl` subclass; docs describe the factory shape.
 *
 * Single-entry-point note: `buildModule` is a single entry point, one
 * per map (user rule, 2026-11-04). A nested `buildModule` call must
 * throw `EUDSL-SCOPE` — the scope-violation code already used for
 * outside-`buildModule` allocation. The nested-call enforcement is a
 * separate item; the probe below pins the contract and fails until
 * that enforcement lands.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { Var, buildModule, lazyVar, when } from "../index.ts";
import { g as fixtureG } from "./lazy_var_fixture.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

// Full pipeline comparison: `dump-ir` runs optimize_module then
// lowering, so it exercises the opt desugar and the storage analysis.
// The comparison is the ENTIRE dump-ir output (storage report
// including the per-var Allocations listing, plus the rendered LIR).
function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p103-"));
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

// Corpus 1: basic value flow (iadd, set, condition on the handle).
test("P1-03 corpus: basic lazy program is dump-ir-identical to its eager twin", () => {
  const g = lazyVar(10);
  const mod = buildModule(() => {
    g.iadd(5);
    const out = Var(0);
    out.set(g);
    when(g.eq(15), () => {
      out.iadd(1);
    });
  });
  const twin = buildModule(() => {
    const v = Var(10);
    v.iadd(5);
    const out = Var(0);
    out.set(v);
    when(v.eq(15), () => {
      out.iadd(1);
    });
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 2: masked var (bitmask flows through the binding).
test("P1-03 corpus: masked lazy var is dump-ir-identical to its eager twin", () => {
  const m = lazyVar(3, 0xff);
  const mod = buildModule(() => {
    m.iadd(1);
    const out = Var(0);
    out.set(m);
  });
  const twin = buildModule(() => {
    const v = Var(3, undefined, 0xff);
    v.iadd(1);
    const out = Var(0);
    out.set(v);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 3: page var (page coordinates flow through the binding).
test("P1-03 corpus: page lazy var is dump-ir-identical to its eager twin", () => {
  const p = lazyVar(0, undefined, "page");
  const mod = buildModule(() => {
    p.iadd(1);
    const out = Var(0);
    out.set(p);
  });
  const twin = buildModule(() => {
    const v = Var(0, undefined, undefined, "page");
    v.iadd(1);
    const out = Var(0);
    out.set(v);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 4: out-of-order first use (first-use order = eager declaration
// order of the reordered twin).
test("P1-03 corpus: out-of-order lazy vars are dump-ir-identical to reordered eager", () => {
  const a = lazyVar(11);
  const b = lazyVar(22);
  const mod = buildModule(() => {
    b.iadd(1);
    a.iadd(1);
    const out = Var(0);
    out.set(a);
    out.set(b);
  });
  const twin = buildModule(() => {
    const bb = Var(22);
    const aa = Var(11);
    bb.iadd(1);
    aa.iadd(1);
    const out = Var(0);
    out.set(aa);
    out.set(bb);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 5: conditional first use with const init (const init has no
// scope, so the binding position inside the `when` is invisible).
test("P1-03 corpus: conditional first use is dump-ir-identical to its eager twin", () => {
  const g = lazyVar(3);
  const mod = buildModule(() => {
    const flag = Var(0);
    when(flag.eq(0), () => {
      g.iadd(1);
    });
    const out = Var(0);
    out.set(g);
  });
  const twin = buildModule(() => {
    const flag = Var(0);
    const v = Var(3);
    when(flag.eq(0), () => {
      v.iadd(1);
    });
    const out = Var(0);
    out.set(v);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 6: cross-file use via the fixture module.
test("P1-03 corpus: cross-file lazy use is dump-ir-identical to its eager twin", () => {
  const mod = buildModule(() => {
    fixtureG.iadd(1);
    const out = Var(0);
    out.set(fixtureG);
  });
  const twin = buildModule(() => {
    const v = Var(5);
    v.iadd(1);
    const out = Var(0);
    out.set(v);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 7: handle used as an Expr operand and in a condition.
test("P1-03 corpus: lazy var in expressions is dump-ir-identical to its eager twin", () => {
  const g = lazyVar(5);
  const mod = buildModule(() => {
    const h = Var(0);
    h.set(g.add(1));
    when(g.eq(6), () => {
      h.iadd(10);
    });
  });
  // Eager twin in first-use order: `h` is allocated before the first
  // resolving use of `g` (inside `set`), so `h` takes id 0.
  const twin = buildModule(() => {
    const h = Var(0);
    const v = Var(5);
    h.set(v.add(1));
    when(v.eq(6), () => {
      h.iadd(10);
    });
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Corpus 8: same handle used in two separate statements (top-level and
// inside a `when`).
test("P1-03 corpus: handle used in two statements is dump-ir-identical to its eager twin", () => {
  const g = lazyVar(7);
  const mod = buildModule(() => {
    g.iadd(1);
    const flag = Var(0);
    when(flag.eq(0), () => {
      g.iadd(2);
    });
    const out = Var(0);
    out.set(g);
  });
  const twin = buildModule(() => {
    const v = Var(7);
    v.iadd(1);
    const flag = Var(0);
    when(flag.eq(0), () => {
      v.iadd(2);
    });
    const out = Var(0);
    out.set(v);
  });
  assert.equal(runDumpIr(mod.toJson()), runDumpIr(twin.toJson()));
});

// Two-builder use at pipeline level: same handle in two modules, each
// dump-ir-identical to its eager twin, bindings at different ids.
test("P1-03: same handle under two builders is dump-ir-identical per module (D3)", () => {
  const g = lazyVar(4);
  const mod1 = buildModule(() => {
    g.iadd(1);
  });
  const mod2 = buildModule(() => {
    const first = Var(0);
    first.set(1);
    g.iadd(1);
  });
  const twin1 = buildModule(() => {
    const v = Var(4);
    v.iadd(1);
  });
  const twin2 = buildModule(() => {
    const first = Var(0);
    first.set(1);
    const v = Var(4);
    v.iadd(1);
  });
  assert.equal(runDumpIr(mod1.toJson()), runDumpIr(twin1.toJson()));
  assert.equal(runDumpIr(mod2.toJson()), runDumpIr(twin2.toJson()));
});

// Never-used global at pipeline level: the whole dump-ir output —
// including Total Variables — is identical with and without the
// unused handle, so it consumed no id and no storage.
test("P1-03: never-used lazy var changes nothing at pipeline level (D5)", () => {
  const unused = lazyVar(9);
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withUnused = buildModule(body);
  const withoutUnused = buildModule(body);
  const a = runDumpIr(withUnused.toJson());
  const b = runDumpIr(withoutUnused.toJson());
  assert.equal(a, b);
  assert.ok(a.includes("Total Variables: 1"), "unused handle must not add a variable");
});

// Single-entry-point probe (supersedes the design's innermost-builder
// probe): a nested `buildModule` call must throw `EUDSL-SCOPE`.
// Blocked on the nested-call enforcement item; this probe fails until
// that enforcement lands.
test("P1-03: nested buildModule throws EUDSL-SCOPE (single entry point)", () => {
  assertCode(
    () =>
      buildModule(() => {
        const a = Var(1);
        a.iadd(1);
        buildModule(() => {
          const b = Var(2);
          b.iadd(1);
        });
      }),
    "EUDSL-SCOPE",
  );
});
