/**
 * Ticket #14: comparisons are intent until their evaluation site.
 *
 * A `whileLoop` check trigger re-evaluates its stored condition list every
 * pass, so a construction-time temp froze the comparison; a `withCP`-backed
 * field condition carried the CP hop and read a stale cursor (or aliased a
 * later field). These tests pin the fixed behavior: the temp-building acts are
 * emitted at the evaluation site, and CP-backed conditions resolve to an
 * absolute EPD.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import test from "node:test";

import { REPO_ROOT, runSim } from "./helpers.ts";

import * as eudkit from "../index.ts";
import {
  Always,
  CUnit,
  EudslError,
  MemView,
  Never,
  SetDeaths,
  SetTo,
  Trigger,
  TriggerFieldRef,
  Var,
  buildModule,
  defineMemView,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
  withCP,
  type CondLike,
  type HirCond,
  type VarImpl,
} from "../index.ts";

// Loop-pass counts for the multi-pass matrices, and the raw-Trigger gate static
// cost — one pin shared by the tests below (review nit #6).
const PASS_COUNTS = [1, 2, 3, 5] as const;
const RETAINED_FALLBACK_TRIGGERS = 5;
const RETAINED_FALLBACK_CONDITIONS = 3;
const RETAINED_FALLBACK_ACTIONS = 5;
const RETAINED_FALLBACK_TOTAL_BYTES = 12_048;

interface LoopIds {
  i: number;
  n: number;
  guard: number;
  iters: number;
}

function loopCounts(
  makeCond: (i: any, n: any) => any,
  step = 1,
): { ids: LoopIds; values: number[] } {
  const ids = { i: -1, n: -1, guard: -1, iters: -1 };
  const mod = buildModule(() => {
    const i = Var(0);
    const n = Var(3);
    const guard = Var(10);
    const iters = Var(0);
    ids.i = i.id;
    ids.n = n.id;
    ids.guard = guard.id;
    ids.iters = iters.id;

    whileLoop([makeCond(i, n), guard.ge(1)], () => {
      i.iadd(step);
      iters.iadd(1);
      guard.isub(1);
    });
  });
  const report = runSim(mod.toJson());
  return { ids, values: report.var_values };
}

test("whileLoop variable-RHS comparisons recompute each pass", () => {
  const rows: Array<{
    name: string;
    make: (i: any, n: any) => any;
    wantI: number;
    wantIters: number;
  }> = [
    { name: "lt", make: (i, n) => i.lt(n), wantI: 3, wantIters: 3 },
    { name: "le", make: (i, n) => i.le(n), wantI: 4, wantIters: 4 },
    { name: "ne", make: (i, n) => i.ne(n), wantI: 3, wantIters: 3 },
    { name: "ne_const", make: (i, n) => i.ne(3), wantI: 3, wantIters: 3 },
    { name: "gt", make: (i, n) => i.gt(n), wantI: 0, wantIters: 0 },
    { name: "ge", make: (i, n) => i.ge(n), wantI: 0, wantIters: 0 },
    { name: "eq", make: (i, n) => i.eq(n), wantI: 0, wantIters: 0 },
  ];
  for (const row of rows) {
    const { ids, values } = loopCounts(row.make);
    assert.equal(values[ids.i], row.wantI, row.name);
    assert.equal(values[ids.iters], row.wantIters, row.name);
  }
});

test("whileLoop expression-RHS comparison recomputes each pass", () => {
  const { ids, values } = loopCounts((i, n) => i.lt(n.add(0)));
  assert.equal(values[ids.i], 3);
  assert.equal(values[ids.iters], 3);
});

test("whileLoop tail rebuilds after body mutation", () => {
  const { ids, values } = loopCounts((i, n) => i.lt(n), 2);
  assert.equal(values[ids.i], 4);
  assert.equal(values[ids.iters], 2);
});

test("CP condition built inside withCP and used after fires", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(7002, SetTo, 777, 0);
    const hit = Var(0);
    ids.hit = hit.id;
    let cond: any;
    withCP(7000, (cp) => {
      cond = cp.view(CUnit).hp.eq(777);
    });
    when([cond], () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.hit], 1);
});

test("two CP conditions in one gate do not alias", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(7000, SetTo, 0x11, 0);
    SetDeaths(7005, SetTo, 0x22, 0);
    const hit = Var(0);
    ids.hit = hit.id;
    let c1: any;
    let c2: any;
    withCP(7000, (cp) => {
      c1 = cp.at(0).eq(0x11);
      c2 = cp.at(5).eq(0x22);
    });
    when([c1, c2], () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.hit], 1);
});

test("single CP condition control fires", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(7000, SetTo, 0x11, 0);
    const hit = Var(0);
    ids.hit = hit.id;
    let c1: any;
    withCP(7000, (cp) => {
      c1 = cp.at(0).eq(0x11);
    });
    when([c1], () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.hit], 1);
});

function buildParityModule() {
  return buildModule(() => {
    const a = Var(1);
    const b = Var(5);
    const out = Var(0);
    when([a.lt(b)], () => out.iadd(1));

    const v = Var(7);
    const w = Var(7);
    const eqOut = Var(0);
    when([v.eq(w)], () => eqOut.iadd(1));

    const rawHit = Var(0);
    new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [rawHit.act_add(1)] });

    const c = Var(0);
    const n = Var(3);
    const cg = Var(2);
    whileLoop([c.lt(n), cg.ge(1)], () => {
      c.iadd(1);
      cg.isub(1);
    });

    const d = Var(0);
    const m = Var(4);
    const dg = Var(2);
    whileLoop([d.lt(m.add(0)), dg.ge(1)], () => {
      d.iadd(1);
      dg.isub(1);
    });

    SetDeaths(7002, SetTo, 777, 0);
    const hit = Var(0);
    let cond: any;
    withCP(7000, (cp) => {
      cond = cp.view(CUnit).hp.eq(777);
    });
    when([cond], () => hit.iadd(1));

    SetDeaths(7000, SetTo, 0x11, 0);
    SetDeaths(7005, SetTo, 0x22, 0);
    let c1: any;
    let c2: any;
    withCP(7000, (pair) => {
      c1 = pair.at(0).eq(0x11);
      c2 = pair.at(5).eq(0x22);
    });
    when([c1, c2], () => hit.iadd(1));
  });
}

const PY_PARITY_SCRIPT = `
from py import (
    CUnit, SetDeaths, SetTo, Trigger, Var, build_module, when, while_loop, with_cp,
)

def prog():
    a = Var(1)
    b = Var(5)
    out = Var(0)
    when([a.lt(b)], lambda: out.iadd(1))

    v = Var(7)
    w = Var(7)
    eq_out = Var(0)
    when([v.eq(w)], lambda: eq_out.iadd(1))

    raw_hit = Var(0)
    Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[raw_hit.act_add(1)])

    c = Var(0)
    n = Var(3)
    cg = Var(2)
    def body_var_rhs():
        c.iadd(1)
        cg.isub(1)
    while_loop([c.lt(n), cg.ge(1)], body_var_rhs)

    d = Var(0)
    m = Var(4)
    dg = Var(2)
    def body_expr_rhs():
        d.iadd(1)
        dg.isub(1)
    while_loop([d.lt(m + 0), dg.ge(1)], body_expr_rhs)

    SetDeaths(7002, SetTo, 777, 0)
    hit = Var(0)
    with with_cp(7000) as cp:
        cond = cp.view(CUnit).hp.eq(777)
    when([cond], lambda: hit.iadd(1))

    SetDeaths(7000, SetTo, 0x11, 0)
    SetDeaths(7005, SetTo, 0x22, 0)
    with with_cp(7000) as pair:
        c1 = pair.at(0).eq(0x11)
        c2 = pair.at(5).eq(0x22)
    when([c1, c2], lambda: hit.iadd(1))

import sys
sys.stdout.write(build_module(prog).to_json())
`;

test("HIR parity for every deferred-condition case", () => {
  const tsHir = buildParityModule().toJson();
  const pyHir = execFileSync("python3", ["-c", PY_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});

// ---------------------------------------------------------------------------
// Ticket #15: `MemFieldHandle.ne` middle constants defer to their use site.
// ---------------------------------------------------------------------------

const MASKED_SCHEMA = defineMemView("Masked", 4, {
  v: { offset: 0, size: 1, mask: 0xff00 },
});
const LOW_MASK_SCHEMA = defineMemView("LowMask", 4, {
  v: { offset: 2, size: 2, mask: 0x00ff },
});

function maskedNeHits(raw: number, rhs: number): number {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(MASKED_SCHEMA, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(view.v.ne(rhs), () => hit.iadd(1));
  });
  return runSim(mod.toJson()).var_values[ids.hit];
}

function cpExtremeHits(rhs: number, fieldValue: number): number {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    // A non-zero base dword distinguishes the absolute target (7005) from the
    // re-anchored live CP (7000) after the scope exits.
    SetDeaths(7000, SetTo, 0x99, 0);
    SetDeaths(7005, SetTo, fieldValue, 0);
    const hit = Var(0);
    ids.hit = hit.id;
    let cond: any;
    withCP(7000, (cp) => {
      cond = cp.at(5).ne(rhs);
    });
    when([cond], () => hit.iadd(1));
  });
  return runSim(mod.toJson()).var_values[ids.hit];
}

test("whileLoop memory-field ne recomputes each pass", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(0);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
      CUnit(1000).hp.set(500);
      iters.iadd(1);
      guard.isub(1);
    });
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.iters], 1);
  // One shared gate flag per site; the tail rebuild must not allocate a
  // second.
  assert.equal(JSON.parse(mod.toJson()).module.vars.length, 3);
});

test("whileLoop CP-backed field ne recomputes each pass", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    SetDeaths(7005, SetTo, 0x11, 0);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    let cond: any;
    withCP(7000, (cp) => {
      cond = cp.at(5).ne(0x99);
    });
    whileLoop([cond, guard.ge(1)], () => {
      SetDeaths(7005, SetTo, 0x99, 0);
      iters.iadd(1);
      guard.isub(1);
    });
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.iters], 1);
});

test("CP-backed field ne extremes used after the scope", () => {
  assert.equal(cpExtremeHits(0, 0x0), 0);
  assert.equal(cpExtremeHits(0xffffffff, 0xffffffff), 0);
  assert.equal(cpExtremeHits(0, 0x1), 1);
});

test("masked field ne honours the FieldSpec mask", () => {
  // `v` is the second byte (mask 0xFF00) of the dword at 3000.
  assert.equal(maskedNeHits(0x00001200, 0x1200), 0);
  assert.equal(maskedNeHits(0x00001201, 0x1200), 0);
  assert.equal(maskedNeHits(0x00003400, 0x1200), 1);
  assert.equal(maskedNeHits(0x00000100, 0x100), 0);
  // Same convention as `eq`: outside-mask bits never matter.
  assert.equal(maskedNeHits(0x00000000, 0x100), 1);
  assert.equal(maskedNeHits(0x00000200, 0x100), 1);
});

test("the DeferredNe marker is not part of the public API", () => {
  assert.equal("DeferredNe" in eudkit, false);
});

test("masked field ne rejects mask bits below shift", () => {
  const mod = () =>
    buildModule(() => {
      const view = MemView(LOW_MASK_SCHEMA, 3000);
      when(view.v.ne(5), () => SetDeaths(9000, SetTo, 1, 0));
    });
  assert.throws(mod, (e: any) => {
    assert.ok(e instanceof EudslError);
    assert.equal(e.code, "EUDSL-BOUNDS");
    return true;
  });
});

test("whileLoop memory-field ne extremes stay frozen-free", () => {
  const rows: Array<[number, number, number]> = [
    [0, 1, 0],
    [0xffffffff, 0, 0xffffffff],
  ];
  for (const [rhs, start, end] of rows) {
    const ids = { iters: -1 };
    const mod = buildModule(() => {
      CUnit(1000).hp.set(start);
      const guard = Var(10);
      const iters = Var(0);
      ids.iters = iters.id;
      whileLoop([CUnit(1000).hp.ne(rhs), guard.ge(1)], () => {
        CUnit(1000).hp.set(end);
        iters.iadd(1);
        guard.isub(1);
      });
    });
    assert.equal(runSim(mod.toJson()).var_values[ids.iters], 1, `rhs=${rhs}`);
  }
});

test("memory-field ne extremes are direct 1T conditions", () => {
  for (const rhs of [0, 0xffffffff]) {
    const mod = buildModule(() => {
      when(CUnit(1000).hp.ne(rhs), () => SetDeaths(9000, SetTo, 1, 0));
    });
    assert.equal(runSim(mod.toJson()).static_cost.triggers, 1, `rhs=${rhs}`);
  }
});

// ---------------------------------------------------------------------------
// Ticket #18: `MemFieldHandle.ne` derives its extreme constant from `bitMask`,
// not from the field width. A custom-narrow mask can never hold the width-max
// constant, so `ne(width_max)` is a tautology and must always fire.
// ---------------------------------------------------------------------------

const MASKED_WIDE_SCHEMA = defineMemView("MaskedWide", 4, {
  v: { offset: 0, size: 2, mask: 0x1f0000 },
});
const MASKED_FILLING_SCHEMA = defineMemView("MaskedFilling", 4, {
  v: { offset: 1, size: 1, mask: 0xff00 },
});

function memviewCondProbe(
  schema: any,
  raw: number,
  makeCond: (v: any) => CondLike,
): { mod: any; report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(schema, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(makeCond(view.v), () => hit.iadd(1));
  });
  return { mod, report: runSim(mod.toJson()), hit: ids.hit };
}

function maskedNeUnionHits(raw: number): number {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(MASKED_SCHEMA, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(view.v.lt(0xff), () => hit.iadd(1));
    when(view.v.gt(0xff), () => hit.iadd(1));
  });
  return runSim(mod.toJson()).var_values[ids.hit];
}

test("masked ne extreme is always unequal", () => {
  // 0xff is not representable inside mask 0xff00, so no raw can equal it.
  for (const raw of [0x0000, 0x0100, 0xff00]) {
    const { report, hit } = memviewCondProbe(MASKED_SCHEMA, raw, (v) => v.ne(0xff));
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
});

test("masked ne extreme is the complement of eq", () => {
  for (const raw of [0x0000, 0x0100, 0xff00]) {
    const eq = memviewCondProbe(MASKED_SCHEMA, raw, (v) => v.eq(0xff));
    const ne = memviewCondProbe(MASKED_SCHEMA, raw, (v) => v.ne(0xff));
    const eqHits = eq.report.var_values[eq.hit];
    const neHits = ne.report.var_values[ne.hit];
    assert.equal(eqHits, 0, `eq raw=${raw.toString(16)}`);
    assert.equal(eqHits + neHits, 1, `complement raw=${raw.toString(16)}`);
  }
});

test("masked ne extreme agrees with lt and gt flag form", () => {
  for (const raw of [0x0000, 0x0100, 0xff00]) {
    const { report, hit } = memviewCondProbe(MASKED_SCHEMA, raw, (v) => v.ne(0xff));
    assert.equal(report.var_values[hit], 1, `ne raw=${raw.toString(16)}`);
    assert.equal(maskedNeUnionHits(raw), 1, `union raw=${raw.toString(16)}`);
    assert.equal(
      maskedNeUnionHits(raw),
      report.var_values[hit],
      `ne vs union raw=${raw.toString(16)}`,
    );
  }
});

test("masked ne mask-width extreme second shape", () => {
  for (const raw of [0x00000000, 0x00100000, 0x001f0000]) {
    const { report, hit } = memviewCondProbe(MASKED_WIDE_SCHEMA, raw, (v) =>
      v.ne(0xffff),
    );
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
});

test("masked ne mask-derived max stays eager", () => {
  for (const [raw, want] of [
    [0x0000, 1],
    [0x0100, 1],
    [0xff00, 0],
  ] as Array<[number, number]>) {
    const { mod, report, hit } = memviewCondProbe(MASKED_SCHEMA, raw, (v) =>
      v.ne(0xff00),
    );
    assert.equal(report.var_values[hit], want, `raw=${raw.toString(16)}`);
    // Direct eager extreme: setup act + the gate. The flag form would add
    // three more triggers.
    assert.equal(report.static_cost.triggers, 2, `static raw=${raw.toString(16)}`);
    const doc = JSON.parse(mod.toJson()).module;
    const whenStmts = doc.stmts.filter((stmt: any) => stmt.kind === "when");
    assert.equal(whenStmts.length, 1);
    assert.equal(whenStmts[0].conds.length, 1);
    // The mask-derived max (0xff00) takes the eager extreme branch:
    // a direct `DeathsX(AtMost, 0xfeff, mask=0xff00)`.
    const cond = whenStmts[0].conds[0];
    assert.equal(cond.condtype, 15);
    assert.equal(cond.comparison, 1);
    assert.equal(cond.amount, 0xfeff);
    assert.equal(cond.locid, 0xff00);
  }
});

test("masked ne width-filling mask unchanged", () => {
  for (const [raw, want] of [
    [0xfe00, 1],
    [0xff00, 0],
  ] as Array<[number, number]>) {
    const { mod, report, hit } = memviewCondProbe(
      MASKED_FILLING_SCHEMA,
      raw,
      (v) => v.ne(0xff),
    );
    assert.equal(report.var_values[hit], want, `raw=${raw.toString(16)}`);
    assert.equal(report.static_cost.triggers, 2);
    const doc = JSON.parse(mod.toJson()).module;
    const whenStmts = doc.stmts.filter((stmt: any) => stmt.kind === "when");
    assert.equal(whenStmts.length, 1);
    assert.equal(whenStmts[0].conds.length, 1);
    // The pre-change shape: a direct `DeathsX(AtMost, 0xfe00, mask=0xff00)`,
    // and no extra trigger.
    const cond = whenStmts[0].conds[0];
    assert.equal(cond.condtype, 15);
    assert.equal(cond.comparison, 1);
    assert.equal(cond.amount, 0xfe00);
    assert.equal(cond.locid, 0xff00);
  }
});

const PY_CANONICAL_SCRIPT = `
from py import CUnit, SetDeaths, SetTo, build_module, when

def prog():
    when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

import sys
sys.stdout.write(build_module(prog).to_json())
`;

test("when(memory-field ne) uses the flag form", () => {
  const mod = buildModule(() => {
    when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 4);
  assert.equal(report.static_cost.trigger_bytes, 9_632);
  assert.equal(report.static_cost.total_bytes, 9_636);
  assert.equal(report.executed_cost.triggers, 4);

  const doc = JSON.parse(mod.toJson()).module;
  const kinds: string[] = doc.stmts.map((stmt: any) => stmt.kind);
  assert.equal(kinds.filter((k) => k === "trigger").length, 1);
  assert.equal(kinds.filter((k) => k === "when").length, 3);
  assert.deepEqual(
    doc.vars.map((v: any) => v.init),
    [0],
  );

  const pyHir = execFileSync("python3", ["-c", PY_CANONICAL_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(mod.toJson(), pyHir);
});

const PY_NE_PARITY_SCRIPT = `
from py import (
    CUnit, FieldSpec, SetDeaths, SetTo, Var, build_module, defineMemView, when,
    while_loop, with_cp,
)
from py.view import make_memview

MASKED = defineMemView("Masked", 4, {"v": FieldSpec(0, 1, mask=0xFF00)})
MASKED_WIDE = defineMemView("MaskedWide", 4, {"v": FieldSpec(0, 2, mask=0x1F0000)})
MASKED_FILLING = defineMemView("MaskedFilling", 4, {"v": FieldSpec(1, 1, mask=0xFF00)})

def prog():
    when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))

    CUnit(1000).hp.set(0)
    guard = Var(10)
    iters = Var(0)
    def body():
        CUnit(1000).hp.set(500)
        iters.iadd(1)
        guard.isub(1)
    while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], body)

    SetDeaths(7005, SetTo, 0x11, 0)
    guard2 = Var(10)
    iters2 = Var(0)
    with with_cp(7000) as cp:
        cond = cp.at(5).ne(0x99)
    def body2():
        SetDeaths(7005, SetTo, 0x99, 0)
        iters2.iadd(1)
        guard2.isub(1)
    while_loop([cond, guard2.ge(1)], body2)

    SetDeaths(7000, SetTo, 0x99, 0)
    SetDeaths(7005, SetTo, 0x0, 0)
    hit = Var(0)
    with with_cp(7000) as cp2:
        c0 = cp2.at(5).ne(0)
        c_max = cp2.at(5).ne(0xFFFFFFFF)
    when([c0, c_max], lambda: hit.iadd(1))

    SetDeaths(3000, SetTo, 0x00001200, 0)
    view = make_memview(MASKED, 3000)
    when(view.v.ne(0x1200), lambda: hit.iadd(1))

    # Ticket #18: masked extremes are derived from the field mask, so the
    # width-max constant defers (flag form) while the mask-derived max
    # stays a direct eager extreme, in both frontends.
    narrow = make_memview(MASKED, 3100)
    when(narrow.v.ne(0xFF), lambda: hit.iadd(1))
    when(narrow.v.ne(0xFF00), lambda: hit.iadd(1))
    wide = make_memview(MASKED_WIDE, 3200)
    when(wide.v.ne(0xFFFF), lambda: hit.iadd(1))
    filling = make_memview(MASKED_FILLING, 3300)
    when(filling.v.ne(0xFF), lambda: hit.iadd(1))

import sys
sys.stdout.write(build_module(prog).to_json())
`;

function buildNeParityModule() {
  return buildModule(() => {
    when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));

    CUnit(1000).hp.set(0);
    const guard = Var(10);
    const iters = Var(0);
    whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
      CUnit(1000).hp.set(500);
      iters.iadd(1);
      guard.isub(1);
    });

    SetDeaths(7005, SetTo, 0x11, 0);
    const guard2 = Var(10);
    const iters2 = Var(0);
    let cond: any;
    withCP(7000, (cp) => {
      cond = cp.at(5).ne(0x99);
    });
    whileLoop([cond, guard2.ge(1)], () => {
      SetDeaths(7005, SetTo, 0x99, 0);
      iters2.iadd(1);
      guard2.isub(1);
    });

    SetDeaths(7000, SetTo, 0x99, 0);
    SetDeaths(7005, SetTo, 0x0, 0);
    const hit = Var(0);
    let c0: any;
    let cMax: any;
    withCP(7000, (cp2) => {
      c0 = cp2.at(5).ne(0);
      cMax = cp2.at(5).ne(0xffffffff);
    });
    when([c0, cMax], () => hit.iadd(1));

    SetDeaths(3000, SetTo, 0x00001200, 0);
    const view = MemView(MASKED_SCHEMA, 3000);
    when(view.v.ne(0x1200), () => hit.iadd(1));

    const narrow = MemView(MASKED_SCHEMA, 3100);
    when(narrow.v.ne(0xff), () => hit.iadd(1));
    when(narrow.v.ne(0xff00), () => hit.iadd(1));
    const wide = MemView(MASKED_WIDE_SCHEMA, 3200);
    when(wide.v.ne(0xffff), () => hit.iadd(1));
    const filling = MemView(MASKED_FILLING_SCHEMA, 3300);
    when(filling.v.ne(0xff), () => hit.iadd(1));
  });
}

test("HIR parity for every new memory-field ne case", () => {
  const tsHir = buildNeParityModule().toJson();
  const pyHir = execFileSync("python3", ["-c", PY_NE_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});

// ---------------------------------------------------------------------------
// Ticket #16: `.ne` expands to the 4T deferred-flag form at gate sites.
// ---------------------------------------------------------------------------

function whenNeHits(seed: number, mutateHp?: number): { report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(seed);
    const hit = Var(0);
    ids.hit = hit.id;
    when(CUnit(1000).hp.ne(500), () => {
      hit.iadd(1);
      if (mutateHp !== undefined) {
        CUnit(1000).hp.set(mutateHp);
      }
    });
  });
  const report = runSim(mod.toJson());
  return { report, hit: report.var_values[ids.hit] };
}

test("when ne uses the flag form", () => {
  const mod = buildModule(() => {
    when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 4);
  assert.equal(report.static_cost.trigger_bytes, 9_632);
  assert.equal(report.static_cost.total_bytes, 9_636);
  assert.equal(report.executed_cost.triggers, 4);

  const doc = JSON.parse(mod.toJson()).module;
  const kinds: string[] = doc.stmts.map((stmt: any) => stmt.kind);
  assert.equal(kinds.filter((k) => k === "trigger").length, 1);
  assert.equal(kinds.filter((k) => k === "when").length, 3);
  assert.equal(doc.vars.length, 1);
});

test("flag-form truth table", () => {
  const rows: Array<[number, number]> = [
    [0, 1],
    [499, 1],
    [500, 0],
    [501, 1],
    [0xffffffff, 1],
  ];
  for (const [seed, want] of rows) {
    assert.equal(whenNeHits(seed).hit, want, `seed=${seed}`);
  }
  // A body that mutates the compared field still fires once.
  assert.equal(whenNeHits(499, 600).hit, 1);
});

function twoCondHits(
  hp: number,
  shield: number,
  fallback: boolean,
): { report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(hp);
    CUnit(1000).shield.set(shield);
    const hit = Var(0);
    ids.hit = hit.id;
    if (fallback) {
      const marker = CUnit(1000).hp.ne(500);
      new Trigger({
        conds: [marker, CUnit(1000).shield.eq(7)],
        acts: [hit.act_add(1)],
      });
    } else {
      when([CUnit(1000).hp.ne(500), CUnit(1000).shield.eq(7)], () => {
        hit.iadd(1);
      });
    }
  });
  const report = runSim(mod.toJson());
  return { report, hit: report.var_values[ids.hit] };
}

test("two-cond gate with flag form", () => {
  let gateReport: any;
  for (const [hp, shield, want] of [
    [499, 7, 1],
    [499, 8, 0],
    [500, 7, 0],
  ] as Array<[number, number, number]>) {
    const gate = twoCondHits(hp, shield, false);
    const fallback = twoCondHits(hp, shield, true);
    assert.equal(gate.hit, want, `hp=${hp} shield=${shield}`);
    assert.equal(fallback.hit, want, `fallback hp=${hp} shield=${shield}`);
    assert.equal(gate.hit, fallback.hit);
    assert.equal(fallback.report.static_cost.triggers, RETAINED_FALLBACK_TRIGGERS);
    gateReport = gate.report;
  }
  assert.equal(gateReport.static_cost.triggers, 5);
});

test("whileLoop expands ne(middle) to the 4T flag gate", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(0);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
      CUnit(1000).hp.set(500);
      iters.iadd(1);
      guard.isub(1);
    });
  });
  // Ticket #17: exactly one body run per pass at every pass count — each pass
  // re-establishes the accumulator before the entry check, so the loop never
  // spins a second time on a stale value.
  for (const passes of PASS_COUNTS) {
    const report = runSim(mod.toJson(), passes);
    assert.equal(report.var_values[ids.iters], passes, `passes=${passes}`);
    // Ticket #64: same 4T flag gate as `when`, emitted at entry and as the
    // last body statements; the tail jump lands on a refreshed gate.
    assert.equal(report.static_cost.triggers, 10);
    assert.equal(report.static_cost.conditions, 6);
    assert.equal(report.static_cost.actions, 12);
    assert.equal(report.static_cost.total_bytes, 24_092);
    assert.equal(report.executed_cost.triggers, 11);
    assert.equal(report.executed_cost.conditions, 7);
    assert.equal(report.executed_cost.actions, 9);
  }

  const doc = JSON.parse(mod.toJson()).module;
  const kinds: string[] = doc.stmts.map((stmt: any) => stmt.kind);
  // One reset trigger + two `when`s per emission point (entry, tail).
  assert.equal(kinds.filter((k) => k === "when").length, 2);
  assert.equal(kinds.filter((k) => k === "trigger").length, 2);

  const gate = buildModule(() => {
    when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));
  });
  assert.equal(runSim(gate.toJson()).static_cost.triggers, 4);
});

test("whileLoop two gate conds each get one flag", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(0);
    CUnit(1000).shield.set(0);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    whileLoop(
      [CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7), guard.ge(1)],
      () => {
        CUnit(1000).hp.set(500);
        CUnit(1000).shield.set(7);
        iters.iadd(1);
        guard.isub(1);
      },
    );
  });
  // One shared flag per gate site: guard + iters + 2 flags.
  assert.equal(JSON.parse(mod.toJson()).module.vars.length, 4);
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 16);
  assert.equal(report.static_cost.conditions, 11);
  assert.equal(report.static_cost.actions, 20);
  assert.equal(report.static_cost.total_bytes, 38_540);
  for (const passes of PASS_COUNTS) {
    const r = runSim(mod.toJson(), passes);
    assert.equal(r.var_values[ids.iters], passes, `passes=${passes}`);
  }
});

test("whileLoop gate nested in gate recomputes", () => {
  const ids = { outs: -1, iters: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(0);
    CUnit(1000).shield.set(0);
    const outerGuard = Var(10);
    const innerGuard = Var(3);
    const outs = Var(0);
    const iters = Var(0);
    ids.outs = outs.id;
    ids.iters = iters.id;

    const innerBody = () => {
      CUnit(1000).shield.set(7);
      iters.iadd(1);
      innerGuard.isub(1);
    };

    whileLoop([CUnit(1000).hp.ne(500), outerGuard.ge(1)], () => {
      CUnit(1000).hp.set(500);
      CUnit(1000).shield.set(0);
      innerGuard.set(3);
      outs.iadd(1);
      whileLoop([CUnit(1000).shield.ne(7), innerGuard.ge(1)], innerBody);
      outerGuard.isub(1);
    });
  });
  // Two gate sites (outer, inner) share one flag each: 2 guards + 2 counters
  // + 2 flags.
  assert.equal(JSON.parse(mod.toJson()).module.vars.length, 6);
  for (const passes of PASS_COUNTS) {
    const report = runSim(mod.toJson(), passes);
    assert.equal(report.var_values[ids.outs], passes);
    assert.equal(report.var_values[ids.iters], passes);
  }
});

test("raw Trigger uses the gate", () => {
  const raw =
    (seed: number): BodyBuilder =>
    (ids) => {
      CUnit(1000).hp.set(seed);
      const hit = Var(0);
      ids.hit = hit.id;
      new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [hit.act_add(1)] });
    };
  const rows: Array<[number, number[]]> = [
    [499, [1, 2, 3, 5]],
    [500, [0, 0, 0, 0]],
    [501, [1, 2, 3, 5]],
  ];
  for (const [seed, want] of rows) {
    assert.deepEqual(hitsOverPasses(raw(seed)), want, `seed=${seed}`);
  }

  const ids: Record<string, number> = {};
  const staticCost = runSim(buildModule(() => raw(499)(ids)).toJson()).static_cost;
  assert.equal(staticCost.triggers, RETAINED_FALLBACK_TRIGGERS);
  assert.equal(staticCost.actions, RETAINED_FALLBACK_ACTIONS);

  const gate = buildModule(() => {
    when(CUnit(1000).hp.ne(500), () => SetDeaths(9000, SetTo, 1, 0));
  });
  assert.equal(runSim(gate.toJson()).static_cost.triggers, 4);
});

test("masked and CP-backed ne use the flag form correctly", () => {
  const masked =
    (raw: number): BodyBuilder =>
    (ids) => {
      SetDeaths(3000, SetTo, raw, 0);
      const view = MemView(MASKED_SCHEMA, 3000);
      const hit = Var(0);
      ids.hit = hit.id;
      when(view.v.ne(0x100), () => hit.iadd(1));
    };
  for (const [raw, want] of [
    [0x100, [0, 0, 0, 0]],
    [0x000, [1, 2, 3, 5]],
    [0x200, [1, 2, 3, 5]],
  ] as Array<[number, number[]]>) {
    assert.deepEqual(hitsOverPasses(masked(raw)), want, `raw=${raw}`);
  }
  const maskedGate = buildModule(() => {
    SetDeaths(3000, SetTo, 0x100, 0);
    const view = MemView(MASKED_SCHEMA, 3000);
    when(view.v.ne(0x100), () => SetDeaths(9000, SetTo, 1, 0));
  });
  assert.equal(runSim(maskedGate.toJson()).static_cost.triggers, 5);

  const cp =
    (fieldValue: number): BodyBuilder =>
    (ids) => {
      SetDeaths(7000, SetTo, 0x99, 0);
      SetDeaths(7005, SetTo, fieldValue, 0);
      const hit = Var(0);
      ids.hit = hit.id;
      let cond: any;
      withCP(7000, (cpv) => {
        cond = cpv.at(5).ne(0x99);
      });
      when([cond], () => hit.iadd(1));
    };
  for (const [fieldValue, want] of [
    [0x11, [1, 2, 3, 5]],
    [0x99, [0, 0, 0, 0]],
  ] as Array<[number, number[]]>) {
    assert.deepEqual(hitsOverPasses(cp(fieldValue)), want, `fieldValue=${fieldValue}`);
  }
  const cpGate = buildModule(() => {
    SetDeaths(7000, SetTo, 0x99, 0);
    SetDeaths(7005, SetTo, 0x11, 0);
    let cond: any;
    withCP(7000, (cpv) => {
      cond = cpv.at(5).ne(0x99);
    });
    when([cond], () => SetDeaths(9000, SetTo, 1, 0));
  });
  assert.equal(runSim(cpGate.toJson()).static_cost.triggers, 5);
});

test("multiple markers in one gate", () => {
  for (const [hp, shield, want] of [
    [499, 0, 1],
    [499, 7, 0],
    [500, 0, 0],
    [500, 7, 0],
  ] as Array<[number, number, number]>) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      CUnit(1000).hp.set(hp);
      CUnit(1000).shield.set(shield);
      const hit = Var(0);
      ids.hit = hit.id;
      when([CUnit(1000).hp.ne(500), CUnit(1000).shield.ne(7)], () => {
        hit.iadd(1);
      });
    });
    const report = runSim(mod.toJson());
    assert.equal(report.var_values[ids.hit], want, `hp=${hp} shield=${shield}`);
    assert.equal(report.static_cost.triggers, 8);
  }
});

const PY_NE_GATE_PARITY_SCRIPT = `
from py import (
    CUnit, SetDeaths, SetTo, Trigger, Var, build_module, on_falling, on_rising,
    once, until, when, while_loop,
)

def prog():
    body = lambda: SetDeaths(9000, SetTo, 1, 0)
    when(CUnit(1000).hp.ne(500), body)
    once(CUnit(1000).hp.ne(500), body)
    until(CUnit(1000).hp.ne(500), body)
    on_rising(CUnit(1000).hp.ne(500), body)
    on_falling(CUnit(1000).hp.ne(500), body)

    CUnit(1000).hp.set(0)
    guard = Var(10)
    iters = Var(0)
    def loop_body():
        CUnit(1000).hp.set(500)
        iters.iadd(1)
        guard.isub(1)
    while_loop([CUnit(1000).hp.ne(500), guard.ge(1)], loop_body)

    hit = Var(0)
    Trigger(conds=[CUnit(1000).hp.ne(500)], acts=[hit.act_add(1)])

import sys
sys.stdout.write(build_module(prog).to_json())
`;

test("HIR parity for every expansion case", () => {
  const body = () => SetDeaths(9000, SetTo, 1, 0);
  const constructs: Array<[string, () => void]> = [
    ["when", () => when(CUnit(1000).hp.ne(500), body)],
    ["once", () => once(CUnit(1000).hp.ne(500), body)],
    ["until", () => until(CUnit(1000).hp.ne(500), body)],
    ["onRising", () => onRising(CUnit(1000).hp.ne(500), body)],
    ["onFalling", () => onFalling(CUnit(1000).hp.ne(500), body)],
  ];
  for (const [name, build] of constructs) {
    assert.ok(
      runSim(buildModule(build).toJson()).static_cost.triggers < 38,
      `${name} must not use the 38T fallback`,
    );
  }

  const mod = buildModule(() => {
    when(CUnit(1000).hp.ne(500), body);
    once(CUnit(1000).hp.ne(500), body);
    until(CUnit(1000).hp.ne(500), body);
    onRising(CUnit(1000).hp.ne(500), body);
    onFalling(CUnit(1000).hp.ne(500), body);

    CUnit(1000).hp.set(0);
    const guard = Var(10);
    const iters = Var(0);
    whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
      CUnit(1000).hp.set(500);
      iters.iadd(1);
      guard.isub(1);
    });

    const hit = Var(0);
    new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [hit.act_add(1)] });
  });
  const pyHir = execFileSync("python3", ["-c", PY_NE_GATE_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(mod.toJson(), pyHir);
});

// ---------------------------------------------------------------------------
// Ticket #17: the deferred-comparison accumulator is reset on EVERY
// materialization, including the one-shot build-time control sites
// (`when` / `once` / `until` / `onRising` / `onFalling`, false-at-entry
// `whileLoop`). The one-shot sites reset the accumulator; `whileLoop`
// rebuilds a gate flag via the entry/tail prelude (#64). Raw `Trigger` now
// uses the same gate when a builder is active (#66); the `rawSite` Var-RHS
// `DeferredCond` rows below still materialize.
// ---------------------------------------------------------------------------

type BodyBuilder = (ids: Record<string, number>) => void;

/**
 * Cumulative `key` counter after 1 / 2 / 3 / 5 whole-module loop passes.
 *
 * The module is built once (deferred temporaries are allocated at build time)
 * and then simulated for each pass count, so a stale accumulator shows up as a
 * rising tail instead of hiding behind a single pass.
 */
function hitsOverPasses(build: BodyBuilder, key = "hit"): number[] {
  const ids: Record<string, number> = {};
  const hir = buildModule(() => build(ids)).toJson();
  return PASS_COUNTS.map((passes) => runSim(hir, passes).var_values[ids[key]]);
}

function rawSite(
  makeCond: (a: VarImpl, b: VarImpl) => CondLike,
  aInit: number,
  bInit: number,
): BodyBuilder {
  return (ids) => {
    const a = Var(aInit);
    const b = Var(bInit);
    const hit = Var(0);
    ids.hit = hit.id;
    new Trigger({ conds: [makeCond(a, b)], acts: [hit.act_add(1)] });
  };
}

function whenSite(
  makeCond: (a: VarImpl, b: VarImpl) => CondLike,
  aInit: number,
  bInit: number,
): BodyBuilder {
  return (ids) => {
    const a = Var(aInit);
    const b = Var(bInit);
    const hit = Var(0);
    ids.hit = hit.id;
    when([makeCond(a, b)], () => hit.iadd(1));
  };
}

test("deferred conditions reset at every materialization across loop passes", () => {
  const varRows: Array<[string, BodyBuilder, number[]]> = [
    // raw Trigger, Var operands
    ["raw eq equal", rawSite((a, b) => a.eq(b), 5, 5), [1, 2, 3, 5]],
    ["raw eq unequal", rawSite((a, b) => a.eq(b), 5, 6), [0, 0, 0, 0]],
    ["raw ne equal", rawSite((a, b) => a.ne(b), 5, 5), [0, 0, 0, 0]],
    ["raw ne unequal", rawSite((a, b) => a.ne(b), 5, 6), [1, 2, 3, 5]],
    // when, Var operands
    ["when eq equal", whenSite((a, b) => a.eq(b), 5, 5), [1, 2, 3, 5]],
    ["when eq unequal", whenSite((a, b) => a.eq(b), 5, 6), [0, 0, 0, 0]],
    ["when ne equal", whenSite((a, b) => a.ne(b), 5, 5), [0, 0, 0, 0]],
    ["when ne unequal", whenSite((a, b) => a.ne(b), 5, 6), [1, 2, 3, 5]],
    // Expression operand
    ["raw expr equal", rawSite((a, b) => a.eq(b.add(1)), 5, 4), [1, 2, 3, 5]],
    ["raw expr unequal", rawSite((a, b) => a.eq(b.add(1)), 5, 5), [0, 0, 0, 0]],
    ["when expr equal", whenSite((a, b) => a.eq(b.add(1)), 5, 4), [1, 2, 3, 5]],
    ["when expr unequal", whenSite((a, b) => a.eq(b.add(1)), 5, 5), [0, 0, 0, 0]],
    // Middle constant (neConst path)
    ["raw mid const equal", rawSite((a) => a.ne(5), 5, 5), [0, 0, 0, 0]],
    ["raw mid const unequal", rawSite((a) => a.ne(5), 6, 5), [1, 2, 3, 5]],
    ["when mid const equal", whenSite((a) => a.ne(5), 5, 5), [0, 0, 0, 0]],
    ["when mid const unequal", whenSite((a) => a.ne(5), 6, 5), [1, 2, 3, 5]],
  ];
  for (const [name, build, want] of varRows) {
    assert.deepEqual(hitsOverPasses(build), want, name);
  }

  // Cost pin for the one-shot `when` site: the reset adds exactly one act
  // (76 -> 77) and nothing else moves.
  const ids: Record<string, number> = {};
  const staticCost = runSim(
    buildModule(() => whenSite((a, b) => a.eq(b), 7, 7)(ids)).toJson(),
  ).static_cost;
  assert.equal(staticCost.triggers, 5);
  assert.equal(staticCost.conditions, 1);
  assert.equal(staticCost.actions, 77);
  assert.equal(staticCost.total_bytes, 12_048);
});

test("temporal constructs see a fresh accumulator on every pass", () => {
  const constructs: Array<
    [string, (conds: CondLike[], body: () => void) => void]
  > = [
    ["once", (conds, body) => once(conds, body)],
    ["until", (conds, body) => until(conds, body)],
    ["onRising", (conds, body) => onRising(conds, body)],
    ["onFalling", (conds, body) => onFalling(conds, body)],
  ];
  const site =
    (
      apply: (conds: CondLike[], body: () => void) => void,
      makeCond: (a: VarImpl, b: VarImpl) => CondLike,
    ): BodyBuilder =>
    (ids) => {
      const a = Var(5);
      const b = Var(5);
      const hit = Var(0);
      ids.hit = hit.id;
      apply([makeCond(a, b)], () => hit.iadd(1));
    };

  const rows: Array<[string, BodyBuilder, number[]]> = [
    // `once` latches after its first fire; an always-false condition must
    // never fire.
    ["once eq equal", site(constructs[0][1], (a, b) => a.eq(b)), [1, 1, 1, 1]],
    ["once ne equal", site(constructs[0][1], (a, b) => a.ne(b)), [0, 0, 0, 0]],
    // `until` runs the body while the condition is false.
    ["until ne equal", site(constructs[1][1], (a, b) => a.ne(b)), [1, 2, 3, 5]],
    // Edges must not see a stale accumulator on later passes.
    ["onRising eq equal", site(constructs[2][1], (a, b) => a.eq(b)), [1, 1, 1, 1]],
    ["onRising ne equal", site(constructs[2][1], (a, b) => a.ne(b)), [0, 0, 0, 0]],
    ["onFalling eq equal", site(constructs[3][1], (a, b) => a.eq(b)), [0, 0, 0, 0]],
    ["onFalling ne equal", site(constructs[3][1], (a, b) => a.ne(b)), [0, 0, 0, 0]],
  ];
  for (const [name, build, want] of rows) {
    assert.deepEqual(hitsOverPasses(build), want, name);
  }
});

test("whileLoop with a false entry condition never runs its body", () => {
  const build: BodyBuilder = (ids) => {
    CUnit(1000).hp.set(500);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    whileLoop([CUnit(1000).hp.ne(500), guard.ge(1)], () => {
      iters.iadd(1);
      guard.isub(1);
    });
  };
  assert.deepEqual(hitsOverPasses(build, "iters"), [0, 0, 0, 0]);
});

test("raw Trigger gate stays correct across loop passes", () => {
  const raw =
    (seed: number): BodyBuilder =>
    (ids) => {
      CUnit(1000).hp.set(seed);
      const hit = Var(0);
      ids.hit = hit.id;
      new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [hit.act_add(1)] });
    };
  const rows: Array<[number, number[]]> = [
    [499, [1, 2, 3, 5]],
    [500, [0, 0, 0, 0]],
    [501, [1, 2, 3, 5]],
  ];
  for (const [seed, want] of rows) {
    assert.deepEqual(hitsOverPasses(raw(seed)), want, `seed=${seed}`);
  }

  const ids: Record<string, number> = {};
  const staticCost = runSim(buildModule(() => raw(500)(ids)).toJson()).static_cost;
  assert.equal(staticCost.triggers, RETAINED_FALLBACK_TRIGGERS);
  assert.equal(staticCost.conditions, RETAINED_FALLBACK_CONDITIONS);
  // The gate replaces 35–36 prelude triggers and 3 vars with
  // 3 triggers + 1 flag.
  assert.equal(staticCost.actions, RETAINED_FALLBACK_ACTIONS);
  assert.equal(staticCost.total_bytes, RETAINED_FALLBACK_TOTAL_BYTES);
});

test("detached raw Trigger keeps the scope error", () => {
  // The gate needs an active module builder; a detached Trigger keeps the
  // pre-existing `materialize()` path, whose Var allocation needs a builder.
  // This is HEAD-compatible, not a new success path.
  assert.throws(
    () => new Trigger({ conds: [CUnit(1000).hp.ne(500)], acts: [] }),
    (e: any) => {
      assert.ok(e instanceof EudslError);
      assert.equal(e.code, "EUDSL-SCOPE");
      return true;
    },
  );

  const empty = new Trigger({ conds: [], acts: [] });
  assert.equal(typeof empty.id, "number");
});

test("masked deferred ne resets across loop passes (gate in raw Trigger and whileLoop)", () => {
  const raw =
    (seed: number): BodyBuilder =>
    (ids) => {
      SetDeaths(3000, SetTo, seed, 0);
      const view = MemView(MASKED_SCHEMA, 3000);
      const hit = Var(0);
      ids.hit = hit.id;
      new Trigger({ conds: [view.v.ne(0x100)], acts: [hit.act_add(1)] });
    };
  assert.deepEqual(hitsOverPasses(raw(0x0100)), [0, 0, 0, 0]);
  assert.deepEqual(hitsOverPasses(raw(0x0000)), [1, 2, 3, 5]);

  const loop =
    (seed: number, rhs = 0x100, settle?: number): BodyBuilder =>
    (ids) => {
      SetDeaths(3000, SetTo, seed, 0);
      const view = MemView(MASKED_SCHEMA, 3000);
      const guard = Var(10);
      const iters = Var(0);
      ids.iters = iters.id;
      whileLoop([view.v.ne(rhs), guard.ge(1)], () => {
        if (settle !== undefined) {
          SetDeaths(3000, SetTo, settle, 0);
        }
        iters.iadd(1);
        guard.isub(1);
      });
    };
  // The mask lane decides the comparison: the dirty bit outside the mask must
  // not start the loop, a settled lane stops it after one pass, and a lane
  // that never settles stays guard-capped at 10 per pass.
  assert.deepEqual(hitsOverPasses(loop(0x1201, 0x1200, 0x1200), "iters"), [0, 0, 0, 0]);
  assert.deepEqual(hitsOverPasses(loop(0x1200, 0x1200, 0x1200), "iters"), [0, 0, 0, 0]);
  assert.deepEqual(hitsOverPasses(loop(0x3400, 0x1200, 0x1200), "iters"), [1, 2, 3, 5]);
  assert.deepEqual(hitsOverPasses(loop(0x0000, 0x0100, 0x0000), "iters"), [10, 10, 10, 10]);
  assert.deepEqual(hitsOverPasses(loop(0x0100), "iters"), [0, 0, 0, 0]);
});

// ---------------------------------------------------------------------------
// Ticket #21: one representable-value rule for both field surfaces.
//
// A field's value space is the set of bit patterns representable inside
// `bitMask`, expressed in field units. These tests pin the four symptoms the
// scan grouped as F1 (crash), F3 (width-derived max), F4 (`eq` false positive)
// and F5 (unrepresentable `ne` still paying the flag form), plus the shared
// `fieldValueSpace` helper and the negative-constant decision.
// ---------------------------------------------------------------------------

// A `Var`'s value lives at byte 20 of its `TriggerPage` action slot; byte 22 is
// the high word of that dword (`shift = 16`), the field F4 measured.
const REF_VALUE_OFFSET = 20;
const REF_HIGH_WORD_OFFSET = 22;

const WORD_AT2_SCHEMA = defineMemView("WordAt2", 4, {
  w: { offset: 2, size: 2 },
});

function makeRef(
  varId: number | undefined,
  byteOffset = REF_VALUE_OFFSET,
  size: 1 | 2 | 4 = 4,
  mask?: number,
): TriggerFieldRef {
  return new TriggerFieldRef(-1, byteOffset, size, 0, varId, mask);
}

function refCondHits(
  raw: number,
  makeCond: (ref: TriggerFieldRef) => CondLike,
  rawTrigger = false,
): { mod: any; report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const value = Var(0);
    value.set(raw);
    const hit = Var(0);
    ids.hit = hit.id;
    const cond = makeCond(makeRef(value.id, REF_VALUE_OFFSET, 4));
    if (rawTrigger) {
      new Trigger({ conds: [cond], acts: [hit.act_add(1)] });
    } else {
      when(cond, () => hit.iadd(1));
    }
  });
  return { mod, report: runSim(mod.toJson()), hit: ids.hit };
}

function memWordHits(
  raw: number,
  makeCond: (w: any) => CondLike,
): { mod: any; report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(WORD_AT2_SCHEMA, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(makeCond(view.w), () => hit.iadd(1));
  });
  return { mod, report: runSim(mod.toJson()), hit: ids.hit };
}

test("TriggerFieldRef ne middle constant defers", () => {
  for (const [raw, want] of [
    [500, 0],
    [0, 1],
    [501, 1],
  ] as const) {
    const { report, hit } = refCondHits(raw, (r) => r.ne(500));
    assert.equal(report.var_values[hit], want, `raw=${raw}`);
  }

  const { mod, report } = refCondHits(500, (r) => r.ne(500));
  // The `when` gate flag form: the `value.set(raw)` setup act, the flag reset,
  // `when(lt)`, `when(gt)` and the caller's gate.
  assert.equal(report.static_cost.triggers, 5);
  assert.equal(report.static_cost.conditions, 3);
  assert.equal(report.static_cost.actions, 5);
  assert.equal(report.static_cost.total_bytes, 12_052);

  const stmts = JSON.parse(mod.toJson()).module.stmts as Array<{ kind: string }>;
  const kinds = stmts.map((s) => s.kind);
  assert.equal(kinds.filter((k) => k === "trigger").length, 2);
  assert.equal(kinds.filter((k) => k === "when").length, 3);
});

test("TriggerFieldRef ne raw Trigger gate", () => {
  for (const [raw, want] of [
    [500, 0],
    [501, 1],
  ] as const) {
    const { report, hit } = refCondHits(raw, (r) => r.ne(500), true);
    assert.equal(report.var_values[hit], want, `raw=${raw}`);
  }

  const { report } = refCondHits(500, (r) => r.ne(500), true);
  // The raw-Trigger gate, 4 B over the `MemFieldHandle` pin (12,048) for the
  // field-reference target form.
  assert.equal(report.static_cost.triggers, 5);
  assert.equal(report.static_cost.conditions, 3);
  assert.equal(report.static_cost.actions, 5);
  assert.equal(report.static_cost.total_bytes, 12_052);
});

test("TriggerFieldRef ne rebuilds inside whileLoop", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    const value = Var(0);
    const guard = Var(10);
    const iters = Var(0);
    ids.iters = iters.id;
    const ref = makeRef(value.id, REF_VALUE_OFFSET, 4);
    whileLoop([ref.ne(500), guard.ge(1)], () => {
      value.set(500);
      iters.iadd(1);
      guard.isub(1);
    });
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.iters], 1);
  // One shared gate flag per site (value + guard + iters + flag).
  assert.equal(JSON.parse(mod.toJson()).module.vars.length, 4);
});

function maskedRefProbe(
  raw: number,
  makeCond: (ref: TriggerFieldRef) => CondLike,
): { mod: any; report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const value = Var(0);
    value.set(raw);
    const hit = Var(0);
    ids.hit = hit.id;
    const ref = makeRef(value.id, REF_VALUE_OFFSET, 1, 0xff00);
    when(makeCond(ref), () => hit.iadd(1));
  });
  return { mod, report: runSim(mod.toJson()), hit: ids.hit };
}

test("TriggerFieldRef gt derives max from the mask", () => {
  for (const raw of [0x0100, 0x1200, 0xff00]) {
    const { report, hit } = maskedRefProbe(raw, (r) => r.gt(0xff));
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
  const { report } = maskedRefProbe(0x0100, (r) => r.gt(0xff));
  // One direct `AtLeast(0x100) & 0xFF00` cond: setup act + the gate.
  assert.equal(report.static_cost.triggers, 2);
});

test("masked TriggerFieldRef ne is always unequal", () => {
  // 0xff is not representable inside mask 0xff00: `ne` is a tautology, folded
  // to an always-true direct cond (2T instead of the 5T gate).
  for (const raw of [0x0000, 0x0100, 0x1200, 0xff00]) {
    const { report, hit } = maskedRefProbe(raw, (r) => r.ne(0xff));
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
  const { report } = maskedRefProbe(0x0000, (r) => r.ne(0xff));
  assert.equal(report.static_cost.triggers, 2);
});

test("mask-derived max stays eager on the sibling surface", () => {
  for (const [raw, want] of [
    [0x0000, 1],
    [0x0100, 1],
    [0xff00, 0],
  ] as const) {
    const { report, hit } = maskedRefProbe(raw, (r) => r.ne(0xff00));
    assert.equal(report.var_values[hit], want, `raw=${raw.toString(16)}`);
  }
});

test("eq of an unrepresentable constant is Never on a mem field", () => {
  for (const raw of [0x0000ffff, 0x00000000, 0x0000ff00, 0x00010000]) {
    const { report, hit } = memWordHits(raw, (w) => w.eq(0x10000));
    assert.equal(report.var_values[hit], 0, `raw=${raw.toString(16)}`);
  }
  const { report } = memWordHits(0x0000ffff, (w) => w.eq(0x10000));
  assert.equal(report.static_cost.triggers, 2);
  assert.equal(report.static_cost.total_bytes, 4_820);
});

test("eq of an unrepresentable constant is Never on a TriggerFieldRef", () => {
  for (const raw of [0x0000ffff, 0x00000000, 0x0000ff00, 0x00010000]) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      const value = Var(raw);
      const hit = Var(0);
      ids.hit = hit.id;
      const ref = makeRef(value.id, REF_HIGH_WORD_OFFSET, 2);
      when(ref.eq(0x10000), () => hit.iadd(1));
    });
    const report = runSim(mod.toJson());
    assert.equal(report.var_values[ids.hit], 0, `raw=${raw.toString(16)}`);
  }
});

test("ne of an unrepresentable constant folds to always true", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, 0x0000, 0);
    const view = MemView(MASKED_SCHEMA, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(view.v.ne(0xff), () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.hit], 1);
  // The F5 pin: 2T / 1C / 2A / 4,820 B, not the 5T / 3C / 5A / 12,048 B flag
  // form.
  assert.equal(report.static_cost.triggers, 2);
  assert.equal(report.static_cost.conditions, 1);
  assert.equal(report.static_cost.actions, 2);
  assert.equal(report.static_cost.total_bytes, 4_820);
});

test("ne of an unrepresentable constant on a default-mask word", () => {
  // 0x10000 cannot be held by a 16-bit field: `ne` is a tautology.
  for (const raw of [0x0000ffff, 0x00000000, 0x00010000]) {
    const { report, hit } = memWordHits(raw, (w) => w.ne(0x10000));
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
  const { report } = memWordHits(0x0000ffff, (w) => w.ne(0x10000));
  assert.equal(report.static_cost.triggers, 2);
});

test("negative constant on a narrow field is unrepresentable", () => {
  // `-1` is `0xFFFFFFFF`, which a 16-bit field cannot hold, so `word.eq(-1)`
  // is `Never()` and `word.ne(-1)` is always true. Deliberate break from the
  // old shift-wrap behaviour; migration: write `0xFFFF` for all ones.
  for (const raw of [0xffff0000, 0x0000ffff, 0x00000000]) {
    const { report, hit } = memWordHits(raw, (w) => w.eq(-1));
    assert.equal(report.var_values[hit], 0, `raw=${raw.toString(16)}`);
  }
  for (const raw of [0xffff0000, 0x0000ffff, 0x00000000]) {
    const { report, hit } = memWordHits(raw, (w) => w.ne(-1));
    assert.equal(report.var_values[hit], 1, `raw=${raw.toString(16)}`);
  }
  const { report } = memWordHits(0xffff0000, (w) => w.ne(-1));
  assert.equal(report.static_cost.triggers, 2);
});

test("dword negative constant stays representable", () => {
  const hits = (rhs: number, hp: number): number => {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      CUnit(1000).hp.set(hp);
      const hit = Var(0);
      ids.hit = hit.id;
      when(CUnit(1000).hp.ne(rhs), () => hit.iadd(1));
    });
    return runSim(mod.toJson()).var_values[ids.hit];
  };
  // `hp.ne(-1)` must keep meaning `hp.ne(0xffffffff)`.
  assert.equal(hits(-1, 0xffffffff), 0);
  assert.equal(hits(0xffffffff, 0xffffffff), 0);
  assert.equal(hits(-1, 0), 1);
});

test("fieldValueSpace derives the field value space", async () => {
  const { fieldValueSpace } = await import("../view.ts");
  assert.equal(fieldValueSpace(0xffffffff, 0, 4), 0xffffffff);
  assert.equal(fieldValueSpace(0xffff0000, 16, 2), 0xffff);
  assert.equal(fieldValueSpace(0xff00, 0, 1), 0xff00);
  assert.equal(fieldValueSpace(0x1f0000, 0, 2), 0x1f0000);
  // Invalid mask (bits below the shift); callers validate before use.
  assert.equal(fieldValueSpace(0x00ff, 16, 2), 0);
});

test("TriggerFieldRef ne rejects mask bits below shift", () => {
  assert.throws(
    () =>
      buildModule(() => {
        const value = Var(0);
        const ref = makeRef(value.id, REF_HIGH_WORD_OFFSET, 2, 0x00ff);
        when(ref.ne(5), () => SetDeaths(9000, SetTo, 1, 0));
      }),
    (e: any) => {
      assert.ok(e instanceof EudslError);
      assert.equal(e.code, "EUDSL-BOUNDS");
      return true;
    },
  );
});

test("TriggerFieldRef eq does not validate", () => {
  // Only `ne` and `read()` validate; `eq` stays representability-only.
  buildModule(() => {
    const value = Var(0);
    const ref = makeRef(value.id, REF_HIGH_WORD_OFFSET, 2, 0x00ff);
    when(ref.eq(5), () => SetDeaths(9000, SetTo, 1, 0));
  });
});

// ---------------------------------------------------------------------------
// Ticket #23: finish the field value-space rule for ordering comparisons.
//
// `ge`/`le`/`gt`/`lt` fold by direction after 32-bit normalisation, on both
// field surfaces and both frontends, and `fieldValueSpace` derives the space
// from the mask alone (no default-mask shortcut), so straddling fields stop
// reporting the nominal width.
// ---------------------------------------------------------------------------

const DWORD_AT2_SCHEMA = defineMemView("DwordAt2", 4, {
  d: { offset: 2, size: 4 },
});
const DWORD_AT1_SCHEMA = defineMemView("DwordAt1", 4, {
  d: { offset: 1, size: 4 },
});
const WORD_AT3_SCHEMA = defineMemView("WordAt3", 4, {
  w: { offset: 3, size: 2 },
});
const CP_MASKED_SCHEMA = defineMemView("CpMasked", 4, {
  v: { offset: 4, size: 1, mask: 0xff00 },
});

const ORDERING_OPS = ["ge", "le", "gt", "lt"] as const;
type OrderingOp = (typeof ORDERING_OPS)[number];

// The scan's probe constants: two above the 16-bit space, then the in-space
// max, then negatives (which normalise to 0xffffffff / 0xfffffffe), then 0.
const ORDERING_CONSTANTS = [0x10000, 0x10001, 0xffff, -1, -2, 0] as const;
// Raws whose high word is the field: 0, 1, 0, 0x7fff, 0xffff.
const ORDERING_RAWS = [
  0x00000000, 0x00010000, 0x0000ffff, 0x7fff0000, 0xffff0000,
] as const;

function expectedOrdering(op: OrderingOp, v: number, rawConst: number): number {
  const c = rawConst >>> 0;
  switch (op) {
    case "ge":
      return v >= c ? 1 : 0;
    case "le":
      return v <= c ? 1 : 0;
    case "gt":
      return v > c ? 1 : 0;
    case "lt":
      return v < c ? 1 : 0;
  }
}

function memSchemaHits(
  schema: any,
  raw: number,
  makeCond: (view: any) => CondLike,
): { report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, raw, 0);
    const view = MemView(schema, 3000);
    const hit = Var(0);
    ids.hit = hit.id;
    when(makeCond(view), () => hit.iadd(1));
  });
  return { report: runSim(mod.toJson()), hit: ids.hit };
}

function refHits(
  raw: number,
  makeCond: (ref: TriggerFieldRef) => CondLike,
  byteOffset = REF_HIGH_WORD_OFFSET,
  size: 1 | 2 | 4 = 2,
  mask?: number,
): { report: any; hit: number } {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const value = Var(0);
    value.set(raw);
    const hit = Var(0);
    ids.hit = hit.id;
    when(makeCond(makeRef(value.id, byteOffset, size, mask)), () => hit.iadd(1));
  });
  return { report: runSim(mod.toJson()), hit: ids.hit };
}

test("ordering value-space truth table", () => {
  // 4 ops x 6 constants x 5 raws = 120 rows per surface. The scan measured
  // 35/120 wrong on the base commit for both surfaces.
  for (const op of ORDERING_OPS) {
    for (const rawConst of ORDERING_CONSTANTS) {
      for (const raw of ORDERING_RAWS) {
        const value = (raw >>> 16) & 0xffff;
        const want = expectedOrdering(op, value, rawConst);

        const mem = memSchemaHits(WORD_AT2_SCHEMA, raw, (view) =>
          (view.w as any)[op](rawConst),
        );
        assert.equal(
          mem.report.var_values[mem.hit],
          want,
          `MemFieldHandle ${op}(${rawConst}) raw=${raw.toString(16)}`,
        );

        const ref = refHits(raw, (r) => (r as any)[op](rawConst));
        assert.equal(
          ref.report.var_values[ref.hit],
          want,
          `TriggerFieldRef ${op}(${rawConst}) raw=${raw.toString(16)}`,
        );
      }
    }
  }
});

test("ordering straddle shortcut removed", () => {
  // A straddling field's width is nominal but its space is not: the helper
  // must return `bitMask >>> shift`, not the full width.
  const memCases = [
    ["FieldSpec(2, 4)", DWORD_AT2_SCHEMA, "d", 0x10000],
    ["FieldSpec(1, 4)", DWORD_AT1_SCHEMA, "d", 0x1000000],
    ["FieldSpec(3, 2)", WORD_AT3_SCHEMA, "w", 0x100],
  ] as const;
  for (const [label, schema, field, rawConst] of memCases) {
    const { report, hit } = memSchemaHits(schema, 0, (view) =>
      (view[field] as any).eq(rawConst),
    );
    assert.equal(report.var_values[hit], 0, label);
  }

  const refCases = [
    ["TriggerFieldRef(-1, 22, 4)", 22, 4, 0x10000],
    ["TriggerFieldRef(-1, 23, 2)", 23, 2, 0x100],
  ] as const;
  for (const [label, offset, size, rawConst] of refCases) {
    const { report, hit } = refHits(
      0,
      (r) => r.eq(rawConst),
      offset,
      size,
    );
    assert.equal(report.var_values[hit], 0, label);
  }
});

test("ordering straddle helper unit rows", async () => {
  const { fieldValueSpace } = await import("../view.ts");
  assert.equal(fieldValueSpace(0xffff0000, 16, 4), 0xffff);
  assert.equal(fieldValueSpace(0xffffff00, 8, 4), 0xffffff);
  assert.equal(fieldValueSpace(0xff000000, 24, 2), 0xff);
});

test("ordering folds are target-free", () => {
  const word = MemView(WORD_AT2_SCHEMA, 3000).w;
  const ref = makeRef(0, REF_HIGH_WORD_OFFSET, 2);

  for (const surface of [word, ref]) {
    assert.deepEqual(surface.ge(0x10000), Never());
    assert.deepEqual(surface.le(0x10000), Always());
    assert.deepEqual(surface.gt(-1), Never());
    assert.deepEqual(surface.lt(-1), Always());
    assert.deepEqual(surface.lt(0), Never());
    // Already-folded directions keep their target-free form too.
    assert.deepEqual(surface.gt(0xffff), Never());
    assert.deepEqual(surface.le(-2), Always());

    // Anti-over-fold: representable constants stay eager.
    for (const cond of [
      surface.ge(0),
      surface.le(0xffff),
      surface.gt(0xfffe),
      surface.lt(0xffff),
    ]) {
      assert.notDeepEqual(cond, Never());
      assert.notDeepEqual(cond, Always());
    }
  }
});

test("ordering non-contiguous mask does not over-fold", () => {
  // The ordering folds ask a numeric question, not the `c & ~max` bit test:
  // on mask 0x0f0f, `ge(0x10)` is a valid threshold.
  const ref = makeRef(0, REF_VALUE_OFFSET, 1, 0x0f0f);
  assert.notDeepEqual(ref.ge(0x10), Never());

  for (const [raw, want] of [
    [0x0000, 0],
    [0x0010, 0],
    [0x0100, 1],
    [0x0f0f, 1],
  ] as const) {
    const { report, hit } = refHits(
      raw,
      (r) => r.ge(0x10),
      REF_VALUE_OFFSET,
      1,
      0x0f0f,
    );
    assert.equal(report.var_values[hit], want, `raw=${raw.toString(16)}`);
  }
});

test("ordering and ne always folds use Always", () => {
  const masked = MemView(MASKED_SCHEMA, 3000).v;
  const word = MemView(WORD_AT2_SCHEMA, 3000).w;
  assert.deepEqual(masked.ne(0xff), Always());
  assert.deepEqual(word.ne(0x10000), Always());

  // Sentinel `withCP(0)` path: the fold lands before the target is resolved,
  // so no CP hop is materialised. Before the fix this was
  // 3T / 1C / 4A / 7,228 B; `Always()` is 2T / 1C / 2A / 4,820 B (4,816 B
  // plus this harness's 4 B module overhead).
  const cost = (epd: number) => {
    const mod = buildModule(() => {
      const hit = Var(0);
      withCP(epd, (cp) => {
        when(cp.view(CP_MASKED_SCHEMA).v.ne(0xff), () => hit.iadd(1));
      });
    });
    return runSim(mod.toJson()).static_cost;
  };

  for (const epd of [0, 7000]) {
    const staticCost = cost(epd);
    assert.equal(staticCost.triggers, 2, `epd=${epd}`);
    assert.equal(staticCost.conditions, 1, `epd=${epd}`);
    assert.equal(staticCost.actions, 2, `epd=${epd}`);
    assert.equal(staticCost.total_bytes, 4_820, `epd=${epd}`);
  }
});

test("ordering invalid mask hits unchanged", () => {
  // `FieldSpec(2, 2, mask=0x00ff)` has no representable values (its bits sit
  // below the shift), so every ordering constant folds — but the measured hit
  // counts must stay what the eager form returned, and the cond form moves
  // from DeathsX to Never/Always deliberately.
  for (const [op, want] of [
    ["ge", 0],
    ["le", 1],
    ["gt", 0],
    ["lt", 1],
  ] as const) {
    const { report, hit } = memSchemaHits(LOW_MASK_SCHEMA, 0x00050000, (view) =>
      (view.v as any)[op](5),
    );
    assert.equal(report.var_values[hit], want, op);
  }

  assert.throws(
    () =>
      buildModule(() => {
        SetDeaths(3000, SetTo, 0x00050000, 0);
        const view = MemView(LOW_MASK_SCHEMA, 3000);
        when(view.v.ne(5), () => SetDeaths(9000, SetTo, 1, 0));
      }),
    (e: any) => {
      assert.ok(e instanceof EudslError);
      assert.equal(e.code, "EUDSL-BOUNDS");
      return true;
    },
  );
});

function buildValueSpaceParityModule() {
  return buildModule(() => {
    const hit = Var(0);

    // F1: deferred middle-constant `ne` on a `TriggerFieldRef`.
    const value = Var(0);
    value.set(500);
    const ref = new TriggerFieldRef(-1, 20, 4, 0, value.id);
    when(ref.ne(500), () => hit.iadd(1));
    new Trigger({ conds: [ref.ne(501)], acts: [hit.act_add(1)] });

    const guard = Var(10);
    const iters = Var(0);
    whileLoop([ref.ne(500), guard.ge(1)], () => {
      value.set(500);
      iters.iadd(1);
      guard.isub(1);
    });

    // F3: mask-derived max on `gt` and the folded `ne`.
    const maskedRef = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0xff00);
    when(maskedRef.gt(0xff), () => hit.iadd(1));
    when(maskedRef.ne(0xff), () => hit.iadd(1));

    // F4: default-mask overflow `eq` on both surfaces.
    const wordView = MemView(WORD_AT2_SCHEMA, 3000);
    when(wordView.w.eq(0x10000), () => hit.iadd(1));
    const high = new TriggerFieldRef(-1, 22, 2, 0, value.id);
    when(high.eq(0x10000), () => hit.iadd(1));

    // F5: unrepresentable `ne` folds on both surfaces.
    const maskedView = MemView(MASKED_SCHEMA, 3000);
    when(maskedView.v.ne(0xff), () => hit.iadd(1));
    when(wordView.w.ne(0x10000), () => hit.iadd(1));

    // Negative constants normalise before the test.
    when(wordView.w.eq(-1), () => hit.iadd(1));
    when(wordView.w.ne(-1), () => hit.iadd(1));
    when(CUnit(1000).hp.ne(-1), () => hit.iadd(1));

    // Ticket #23: ordering folds after 32-bit normalisation.
    when(wordView.w.ge(0x10000), () => hit.iadd(1));
    when(wordView.w.le(0x10000), () => hit.iadd(1));
    when(wordView.w.gt(-1), () => hit.iadd(1));
    when(wordView.w.lt(-1), () => hit.iadd(1));
    when(wordView.w.lt(0), () => hit.iadd(1));
    when(high.ge(0x10000), () => hit.iadd(1));

    // Straddle: the space comes from the mask, not the nominal width.
    const dwordView = MemView(DWORD_AT2_SCHEMA, 3000);
    when(dwordView.d.eq(0x10000), () => hit.iadd(1));

    // Non-contiguous mask: a numeric threshold below the max stays eager.
    const noncontiguous = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0x0f0f);
    when(noncontiguous.ge(0x10), () => hit.iadd(1));

    // Sentinel `Always()` fold: no CP hop, no target.
    withCP(0, (sentinelCp) => {
      when(sentinelCp.view(CP_MASKED_SCHEMA).v.ne(0xff), () => hit.iadd(1));
    });
  });
}

const PY_VALUE_SPACE_PARITY_SCRIPT = `
from py import (
    CUnit, FieldSpec, SetDeaths, SetTo, Trigger, TriggerFieldRef, Var,
    build_module, defineMemView, when, while_loop, with_cp,
)
from py.view import make_memview

word_at2 = defineMemView("WordAt2", 4, {"w": FieldSpec(2, 2)})
masked = defineMemView("Masked", 4, {"v": FieldSpec(0, 1, mask=0xFF00)})
dword_at2 = defineMemView("DwordAt2", 4, {"d": FieldSpec(2, 4)})
cp_masked = defineMemView("CpMasked", 4, {"v": FieldSpec(4, 1, mask=0xFF00)})

def prog():
    hit = Var(0)

    # F1: deferred middle-constant ne on a TriggerFieldRef.
    value = Var(0)
    value.set(500)
    ref = TriggerFieldRef(-1, 20, 4, 0, value.id)
    when(ref.ne(500), lambda: hit.iadd(1))
    Trigger(conds=[ref.ne(501)], acts=[hit.act_add(1)])

    guard = Var(10)
    iters = Var(0)
    def body():
        value.set(500)
        iters.iadd(1)
        guard.isub(1)
    while_loop([ref.ne(500), guard.ge(1)], body)

    # F3: mask-derived max on gt and the folded ne.
    masked_ref = TriggerFieldRef(-1, 20, 1, 0, value.id, 0xFF00)
    when(masked_ref.gt(0xFF), lambda: hit.iadd(1))
    when(masked_ref.ne(0xFF), lambda: hit.iadd(1))

    # F4: default-mask overflow eq on both surfaces.
    word_view = make_memview(word_at2, 3000)
    when(word_view.w.eq(0x10000), lambda: hit.iadd(1))
    high = TriggerFieldRef(-1, 22, 2, 0, value.id)
    when(high.eq(0x10000), lambda: hit.iadd(1))

    # F5: unrepresentable ne folds on both surfaces.
    masked_view = make_memview(masked, 3000)
    when(masked_view.v.ne(0xFF), lambda: hit.iadd(1))
    when(word_view.w.ne(0x10000), lambda: hit.iadd(1))

    # Negative constants normalise before the test.
    when(word_view.w.eq(-1), lambda: hit.iadd(1))
    when(word_view.w.ne(-1), lambda: hit.iadd(1))
    when(CUnit(1000).hp.ne(-1), lambda: hit.iadd(1))

    # Ticket #23: ordering folds after 32-bit normalisation.
    when(word_view.w.ge(0x10000), lambda: hit.iadd(1))
    when(word_view.w.le(0x10000), lambda: hit.iadd(1))
    when(word_view.w.gt(-1), lambda: hit.iadd(1))
    when(word_view.w.lt(-1), lambda: hit.iadd(1))
    when(word_view.w.lt(0), lambda: hit.iadd(1))
    when(high.ge(0x10000), lambda: hit.iadd(1))

    # Straddle: the space comes from the mask, not the nominal width.
    dword_view = make_memview(dword_at2, 3000)
    when(dword_view.d.eq(0x10000), lambda: hit.iadd(1))

    # Non-contiguous mask: a numeric threshold below the max stays eager.
    noncontiguous = TriggerFieldRef(-1, 20, 1, 0, value.id, 0x0F0F)
    when(noncontiguous.ge(0x10), lambda: hit.iadd(1))

    # Sentinel Always() fold: no CP hop, no target.
    with with_cp(0) as sentinel_cp:
        when(sentinel_cp.view(cp_masked).v.ne(0xFF), lambda: hit.iadd(1))

import sys
sys.stdout.write(build_module(prog).to_json())
`;

test("HIR parity for every value-space case", () => {
  const tsHir = buildValueSpaceParityModule().toJson();
  const pyHir = execFileSync("python3", ["-c", PY_VALUE_SPACE_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});

// Compile-time rider check (run by `npm run typecheck`, which includes tests):
// the five aliases accept `number` and return `HirCond`, while `ne` stays
// `CondLike` because a numeric middle constant is deferred.
function _aliasOverloadsReturnHirCond(v: VarImpl): HirCond[] {
  const ne: CondLike = v.ne(5);
  void ne;
  return [v.atLeast(5), v.at_least(5), v.atMost(5), v.at_most(5), v.exactly(5)];
}

function _neStaysCondLike(v: VarImpl): void {
  // @ts-expect-error a deferred `ne` is not a direct HirCond
  const bad: HirCond = v.ne(5);
  void bad;
}

void _aliasOverloadsReturnHirCond;
void _neStaysCondLike;
