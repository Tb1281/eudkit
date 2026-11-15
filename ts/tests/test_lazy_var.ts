/**
 * `lazyVar` public API tests (globals P1-02).
 *
 * `lazyVar` is the thin public factory over the P1-01 deferred base: a
 * handle declared anywhere (no builder needed) that resolves to a fully
 * eager binding on first use inside `buildModule`, one binding per
 * builder. Eager `Var` is unchanged; `lazyVar` is opt-in only.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { Var, VarImpl, buildModule, lazyVar, when } from "../index.ts";
import { g as fixtureG } from "./lazy_var_fixture.ts";

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

test("P1-02: lazyVar declaration outside buildModule allocates nothing", () => {
  const handle = lazyVar(7);
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandle = buildModule(body);
  const withoutHandle = buildModule(body);
  // Identical output proves the unused handle consumed no id, emitted no
  // decl, and emitted no statement.
  assert.deepEqual(hirJson(withHandle), hirJson(withoutHandle));
  assert.deepEqual(decls(withHandle), [[0, 0]]);
  assert.equal(handle.init, 7);
});

test("P1-02: lazyVar mirrors Var call shape: mask and page storage", () => {
  const masked = lazyVar(3, 0xff);
  const paged = lazyVar(0, undefined, "page");
  // Pure reads never resolve.
  assert.equal(masked.bitmask, 0xff);
  assert.equal(masked.init, 3);
  let twinPaged: VarImpl;
  const mod = buildModule(() => {
    masked.iadd(1);
    paged.iadd(1);
  });
  const twin = buildModule(() => {
    const m = Var(3, undefined, 0xff);
    const p = Var(0, undefined, undefined, "page");
    twinPaged = p;
    m.iadd(1);
    p.iadd(1);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
  // Page coordinates match the eager page var (single-record post-build
  // read on the lazy handle).
  assert.equal(paged.pageIndex, twinPaged!.pageIndex);
  assert.equal(paged.slotIndex, twinPaged!.slotIndex);
});

test("P1-02: lazyVar non-const init throws EUDSL-TYPE eagerly (D4)", () => {
  // Fail fast at the declaration site, outside any builder.
  assertCode(() => lazyVar("nope" as unknown as number), "EUDSL-TYPE");
  buildModule(() => {
    const e = Var(0);
    assertCode(() => lazyVar(e), "EUDSL-TYPE");
    assertCode(() => lazyVar(e.add(1)), "EUDSL-TYPE");
  });
});

test("P1-02: lazyVar works everywhere an eager Var works", () => {
  const g = lazyVar(5);
  assert.ok(g instanceof VarImpl);
  const mod = buildModule(() => {
    g.iadd(1);
    const h = Var(0);
    h.set(g);
    const h2 = Var(0);
    h2.set(g.add(1));
    when(g.eq(6), () => {
      h.iadd(10);
    });
  });
  const twin = buildModule(() => {
    const v = Var(5);
    v.iadd(1);
    const h = Var(0);
    h.set(v);
    const h2 = Var(0);
    h2.set(v.add(1));
    when(v.eq(6), () => {
      h.iadd(10);
    });
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
});

test("P1-02: out-of-order first use equals reordered eager, differs from declaration-order eager (R1)", () => {
  const a = lazyVar(11);
  const b = lazyVar(22);
  const mod = buildModule(() => {
    b.iadd(1);
    a.iadd(1);
  });
  const reordered = buildModule(() => {
    const bb = Var(22);
    const aa = Var(11);
    bb.iadd(1);
    aa.iadd(1);
  });
  const declarationOrder = buildModule(() => {
    const aa = Var(11);
    const bb = Var(22);
    bb.iadd(1);
    aa.iadd(1);
  });
  assert.deepEqual(hirJson(mod), hirJson(reordered));
  assert.notDeepEqual(hirJson(mod), hirJson(declarationOrder));
  // Ids follow first use: B first.
  assert.deepEqual(decls(mod), [[0, 22], [1, 11]]);
});

test("P1-02: never-used lazyVar emits nothing (D5)", () => {
  const unused = lazyVar(9);
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withUnused = buildModule(body);
  const withoutUnused = buildModule(body);
  assert.deepEqual(hirJson(withUnused), hirJson(withoutUnused));
  assert.deepEqual(decls(withUnused), [[0, 0]]);
});

test("P1-02: same handle under two builders gets separate bindings (D3)", () => {
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
  // Neither module references the other's ids: mod1 binds the handle at
  // id 0, mod2 at id 1.
  assert.deepEqual(hirJson(mod1), hirJson(twin1));
  assert.deepEqual(hirJson(mod2), hirJson(twin2));
  assert.deepEqual(decls(mod1), [[0, 4]]);
  assert.deepEqual(decls(mod2), [[0, 0], [1, 4]]);
});

test("P1-02: conditional first use with const init equals eager (D4)", () => {
  const g = lazyVar(3);
  const mod = buildModule(() => {
    const flag = Var(0);
    when(flag.eq(0), () => {
      g.iadd(1);
    });
  });
  const twin = buildModule(() => {
    const flag = Var(0);
    const v = Var(3);
    when(flag.eq(0), () => {
      v.iadd(1);
    });
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
});

test("P1-02: cross-file use via a fixture module", () => {
  const mod = buildModule(() => {
    fixtureG.iadd(1);
  });
  const twin = buildModule(() => {
    const v = Var(5);
    v.iadd(1);
  });
  assert.deepEqual(hirJson(mod), hirJson(twin));
  assert.deepEqual(decls(mod), [[0, 5]]);
});

test("P1-02: lazyVar offers no caller-chosen id: ids are builder-assigned in use order", () => {
  const first = lazyVar(7);
  const second = lazyVar(7);
  const mod = buildModule(() => {
    second.iadd(1);
    first.iadd(1);
  });
  assert.deepEqual(decls(mod), [[0, 7], [1, 7]]);
});

test("P1-02: lazy program runs like its eager twin (sim)", () => {
  const g = lazyVar(10);
  const mod = buildModule(() => {
    g.iadd(5);
    const out = Var(0);
    out.set(g);
  });
  const twin = buildModule(() => {
    const v = Var(10);
    v.iadd(5);
    const out = Var(0);
    out.set(v);
  });
  const a = runSim(mod.toJson());
  const b = runSim(twin.toJson());
  assert.deepEqual(a.var_values, b.var_values);
  assert.deepEqual(a.executed_cost, b.executed_cost);
});
