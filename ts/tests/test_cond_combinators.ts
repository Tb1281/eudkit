/**
 * ADR 0028: `condAll` / `condAny` / `condNot` and the unified GatePlan
 * prelude.
 *
 * Covers splicing (`condAll`), flag-gated disjunction (`condAny`), structural
 * complements (`condNot`), raw `Trigger` integration, canonical pre-order
 * flag allocation, and `whileLoop`'s entry/tail refresh.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { checkSource } from "../scripts/check-truthiness.ts";

import {
  Always,
  CUnit,
  EudslError,
  Never,
  SetDeaths,
  SetTo,
  Trigger,
  Var,
  buildModule,
  condAll,
  condAny,
  condNot,
  cond_all,
  cond_any,
  cond_not,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
  type CondLike,
} from "../index.ts";

function isEudslError(code: string) {
  return (error: unknown): boolean => {
    assert.ok(error instanceof EudslError);
    assert.equal((error as EudslError).code, code);
    return true;
  };
}

function moduleOf(json: string): any {
  return JSON.parse(json).module;
}

function whenConditions(json: string): any[] {
  const stmt = moduleOf(json).stmts.find((item: any) => item.kind === "when");
  assert.ok(stmt, "expected a when statement");
  return stmt.conds;
}

function condtypeOf(cond: CondLike): number {
  return (cond as any).condtype;
}

// ---------------------------------------------------------------------------
// condAll: transparent conjunction
// ---------------------------------------------------------------------------

test("condAll() splices to zero conditions everywhere", () => {
  const withAll = buildModule(() => {
    const hit = Var(0);
    when(condAll(), () => hit.iadd(1));
  });
  const withEmpty = buildModule(() => {
    const hit = Var(0);
    when([], () => hit.iadd(1));
  });
  assert.equal(withAll.toJson(), withEmpty.toJson());

  const triggerAll = buildModule(() => {
    new Trigger({ conds: [condAll()], acts: [] });
  });
  const triggerEmpty = buildModule(() => {
    new Trigger({ conds: [], acts: [] });
  });
  assert.equal(triggerAll.toJson(), triggerEmpty.toJson());
});

test("condAll(single) is the same gate as the bare condition", () => {
  const wrapped = buildModule(() => {
    const value = Var(0);
    const hit = Var(0);
    when(condAll(value.eq(1)), () => hit.iadd(1));
  });
  const bare = buildModule(() => {
    const value = Var(0);
    const hit = Var(0);
    when(value.eq(1), () => hit.iadd(1));
  });
  assert.equal(wrapped.toJson(), bare.toJson());
});

test("empty nesting identities collapse before planning", () => {
  const build = (make: () => CondLike) =>
    buildModule(() => {
      const hit = Var(0);
      when(make(), () => hit.iadd(1));
    }).toJson();
  const zero = build(() => condAll());
  const never = build(() => Never());

  assert.equal(build(() => condAny(condAll())), zero);
  assert.equal(build(() => condNot(condAny())), zero);
  assert.equal(build(() => condAll(condAll())), zero);
  assert.equal(build(() => condNot(condAll())), never);
  assert.equal(build(() => condNot(condAll(condAll()))), never);
});

test("condNot(condAll(single DeferredNe)) reduces to the direct eq", () => {
  const build = (make: () => CondLike) =>
    buildModule(() => {
      const hit = Var(0);
      when(make(), () => hit.iadd(1));
    }).toJson();
  assert.equal(
    build(() => condNot(condAll(CUnit(1000).hp.ne(500)))),
    build(() => CUnit(1000).hp.eq(500)),
  );

  const mod = buildModule(() => {
    const hit = Var(0);
    when(condNot(condAll(CUnit(1000).hp.ne(500))), () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 1);
  assert.equal(report.static_cost.conditions, 1);
});

test("condAll carries 16 conditions without a prelude", () => {
  const mod = buildModule(() => {
    const values = Array.from({ length: 16 }, () => Var(1));
    const hit = Var(0);
    when(
      condAll(...values.map((value) => value.eq(1))),
      () => hit.iadd(1),
    );
  });
  assert.equal(whenConditions(mod.toJson()).length, 16);
  assert.equal(runSim(mod.toJson()).static_cost.triggers, 1);
  assert.equal(runSim(mod.toJson()).static_cost.conditions, 16);
});

test("condAll(17) is spliced before raw Trigger bounds are checked", () => {
  const structured = buildModule(() => {
    const values = Array.from({ length: 17 }, () => Var(1));
    const hit = Var(0);
    when(
      condAll(...values.map((value) => value.eq(1))),
      () => hit.iadd(1),
    );
  });
  assert.equal(whenConditions(structured.toJson()).length, 17);
  const ids = { hit: 16 };
  assert.equal(runSim(structured.toJson()).var_values[ids.hit], 1);

  assert.throws(
    () =>
      buildModule(() => {
        const values = Array.from({ length: 17 }, () => Var(1));
        new Trigger({
          conds: [condAll(...values.map((value) => value.eq(1)))],
          acts: [],
        });
      }),
    isEudslError("EUDSL-BOUNDS"),
  );
});

test("Trigger splices condAll into its condition slots", () => {
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(1);
    new Trigger({
      conds: [condAll(a.eq(1), b.eq(1))],
      acts: [],
    });
  });
  const trigger = moduleOf(mod.toJson()).stmts.find(
    (stmt: any) => stmt.kind === "trigger",
  );
  assert.equal(trigger.conds.length, 2);
});

// ---------------------------------------------------------------------------
// condAny: flag-gated disjunction
// ---------------------------------------------------------------------------

function whenAnyHits(values: readonly number[]): number {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const vars = values.map((value) => Var(value));
    const hit = Var(0);
    ids.hit = hit.id;
    when(
      condAny(...vars.map((value) => value.eq(1))),
      () => hit.iadd(1),
    );
  });
  return runSim(mod.toJson()).var_values[ids.hit];
}

test("condAny() expands to Never()", () => {
  const mod = buildModule(() => {
    const hit = Var(0);
    when(condAny(), () => hit.iadd(1));
  });
  const conds = whenConditions(mod.toJson());
  assert.equal(conds.length, 1);
  assert.equal(conds[0].condtype, 23);
  assert.equal(runSim(mod.toJson()).static_cost.triggers, 1);

  const raw = new Trigger({ conds: [condAny()], acts: [] });
  assert.equal(condtypeOf(raw.conds[0]), 23);
});

test("condAny truth table for two plain conditions", () => {
  const rows: Array<[number, number, number]> = [
    [0, 0, 0],
    [1, 0, 1],
    [0, 1, 1],
    [1, 1, 1],
  ];
  for (const [a, b, want] of rows) {
    assert.equal(whenAnyHits([a, b]), want, `a=${a} b=${b}`);
  }
});

test("condAny(x) is the identity gate condAny(x) -> x", () => {
  const build = (make: (value: any) => CondLike) =>
    buildModule(() => {
      const value = Var(1);
      const hit = Var(0);
      when(make(value), () => hit.iadd(1));
    }).toJson();
  assert.equal(
    build((value) => condAny(value.eq(1))),
    build((value) => value.eq(1)),
  );

  const mod = buildModule(() => {
    const value = Var(1);
    const hit = Var(0);
    when(condAny(value.eq(1)), () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 1);
  assert.equal(report.static_cost.conditions, 1);
  const kinds = moduleOf(mod.toJson()).stmts.map((stmt: any) => stmt.kind);
  assert.deepEqual(kinds, ["when"]);
});

test("condAny exact costs for N=2/3/16", () => {
  const rows: Array<[number, number, number]> = [
    [2, 4, 3],
    [3, 5, 4],
    [16, 18, 17],
  ];
  for (const [n, triggers, conditions] of rows) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      const values = Array.from({ length: n }, () => Var(1));
      const hit = Var(0);
      ids.hit = hit.id;
      when(
        condAny(...values.map((value) => value.eq(1))),
        () => hit.iadd(1),
      );
    });
    const report = runSim(mod.toJson());
    assert.equal(report.static_cost.triggers, triggers, `n=${n}`);
    assert.equal(report.static_cost.conditions, conditions, `n=${n}`);
    assert.equal(report.var_values[ids.hit], 1, `n=${n}`);
  }
});

test("condAny(condAll(20), c) splices the branch without a second flag", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const values = Array.from({ length: 20 }, () => Var(1));
    const c = Var(1);
    const hit = Var(0);
    ids.hit = hit.id;
    when(
      condAny(condAll(...values.map((value) => value.eq(1))), c.eq(1)),
      () => hit.iadd(1),
    );
  });
  const stmts = moduleOf(mod.toJson()).stmts;
  const whens = stmts.filter((stmt: any) => stmt.kind === "when");
  assert.equal(whens.length, 3);
  assert.equal(whens[0].conds.length, 20);
  assert.equal(whens[1].conds.length, 1);
  assert.equal(whens[2].conds.length, 1);
  const report = runSim(mod.toJson());
  // reset + chunked 20-condition branch + 1-condition branch + caller gate;
  // 20 branch conds + the other branch + the flag gate.
  assert.equal(report.static_cost.triggers, 7);
  assert.equal(report.static_cost.conditions, 22);
  assert.equal(report.var_values[ids.hit], 1);
});

test("condAny flattens same-kind nesting onto one flag", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(1);
    const c = Var(1);
    const hit = Var(0);
    ids.hit = hit.id;
    when(
      condAny(condAny(a.eq(1), b.eq(1)), c.eq(1)),
      () => hit.iadd(1),
    );
  });
  const doc = moduleOf(mod.toJson());
  // a, b, c, hit, then exactly one shared flag (no nested second flag).
  assert.equal(doc.vars.length, 5);
  const resets = doc.stmts
    .filter((stmt: any) => stmt.kind === "trigger")
    .map((stmt: any) => stmt.acts[0].relocs[0].target_var);
  assert.deepEqual(resets, [4]);
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 5); // N=3 -> N+2T
  assert.equal(report.static_cost.conditions, 4); // N clauses + flag gate
  assert.equal(report.var_values[ids.hit], 1);
});

test("condAll(condAny(), b) is statically false", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const b = Var(1);
    const hit = Var(0);
    ids.hit = hit.id;
    when(condAll(condAny(), b.eq(1)), () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.static_cost.triggers, 1);
  assert.equal(report.static_cost.conditions, 2); // Never + b
  assert.equal(report.var_values[ids.hit], 0);
});

test("condAny(condAll(), b) is statically true", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const b = Var(0);
    const hit = Var(0);
    ids.hit = hit.id;
    when(condAny(condAll(), b.eq(1)), () => hit.iadd(1));
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.hit], 1);
});

test("condAny() is inert in whileLoop/until/onRising", () => {
  const ids = { loop: -1, until: -1, edge: -1 };
  const mod = buildModule(() => {
    const loop = Var(0);
    const untilHit = Var(0);
    const edge = Var(0);
    ids.loop = loop.id;
    ids.until = untilHit.id;
    ids.edge = edge.id;
    whileLoop(condAny(), () => loop.iadd(1));
    until(condAny(), () => untilHit.iadd(1));
    onRising(condAny(), () => edge.iadd(1));
  });
  const values = runSim(mod.toJson(), 3).var_values;
  assert.equal(values[ids.loop], 0);
  // until(condAny()) is until(Never): the body runs every loop.
  assert.equal(values[ids.until], 3);
  assert.equal(values[ids.edge], 0);
});

test("a reused combinator variable gets one flag per when site", () => {
  const ids = { first: -1, second: -1 };
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const first = Var(0);
    const second = Var(0);
    ids.first = first.id;
    ids.second = second.id;
    const condition = condAny(a.eq(1), b.eq(1));
    when(condition, () => first.iadd(1));
    when(condition, () => second.iadd(1));
  });
  const doc = moduleOf(mod.toJson());
  // a, b, first, second + one independent gate flag per evaluation site.
  assert.equal(doc.vars.length, 6);
  const resets = doc.stmts
    .filter((stmt: any) => stmt.kind === "trigger")
    .map((stmt: any) => stmt.acts[0].relocs[0].target_var);
  assert.equal(resets.length, 2);
  assert.notEqual(resets[0], resets[1]);
  const values = runSim(mod.toJson()).var_values;
  assert.equal(values[ids.first], 1);
  assert.equal(values[ids.second], 1);
});

test("until chunks a 17-condition condAll like the flat list", () => {
  const build = (wrapped: boolean) =>
    buildModule(() => {
      const values = Array.from({ length: 17 }, () => Var(1));
      const conds = values.map((value) => value.eq(1));
      const hit = Var(0);
      until(wrapped ? condAll(...conds) : conds, () => hit.iadd(1));
    }).toJson();
  assert.equal(build(true), build(false));
});

// ---------------------------------------------------------------------------
// condNot: structural complements
// ---------------------------------------------------------------------------

test("condNot(DeferredNe) is a direct 1C / 0T complement", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    CUnit(1000).hp.set(500);
    const hit = Var(0);
    ids.hit = hit.id;
    when(condNot(CUnit(1000).hp.ne(500)), () => hit.iadd(1));
  });
  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.hit], 1);
  assert.equal(report.static_cost.triggers, 2); // hp.set(500) + when
  assert.equal(report.static_cost.conditions, 1);

  const bare = buildModule(() => {
    when(condNot(CUnit(1000).hp.ne(500)), () =>
      SetDeaths(9000, SetTo, 1, 0),
    );
  });
  const bareCost = runSim(bare.toJson()).static_cost;
  assert.equal(bareCost.triggers, 1); // no flag prelude
  assert.equal(bareCost.conditions, 1);
  const direct = buildModule(() => {
    when(CUnit(1000).hp.eq(500), () => SetDeaths(9000, SetTo, 1, 0));
  });
  assert.equal(bare.toJson(), direct.toJson());
});

test("condNot(DeferredNe) truth table is the eq truth table", () => {
  for (const [seed, want] of [
    [499, 0],
    [500, 1],
    [501, 0],
  ] as const) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      CUnit(1000).hp.set(seed);
      const hit = Var(0);
      ids.hit = hit.id;
      when(condNot(CUnit(1000).hp.ne(500)), () => hit.iadd(1));
    });
    assert.equal(runSim(mod.toJson()).var_values[ids.hit], want, `seed=${seed}`);
  }
});

test("DeferredNe expansion is byte-identical to an explicit lt/gt or", () => {
  const body = () => SetDeaths(9000, SetTo, 1, 0);
  const marker = buildModule(() => when(CUnit(1000).hp.ne(500), body));
  const explicit = buildModule(() =>
    when(condAny(CUnit(1000).hp.lt(500), CUnit(1000).hp.gt(500)), body),
  );
  assert.equal(marker.toJson(), explicit.toJson());

  const rawMarker = buildModule(() => {
    const hit = Var(0);
    new Trigger({
      conds: [CUnit(1000).hp.ne(500)],
      acts: [hit.act_add(1)],
    });
  });
  const rawExplicit = buildModule(() => {
    const hit = Var(0);
    new Trigger({
      conds: [condAny(CUnit(1000).hp.lt(500), CUnit(1000).hp.gt(500))],
      acts: [hit.act_add(1)],
    });
  });
  assert.equal(rawMarker.toJson(), rawExplicit.toJson());
});

function deferredComplementPair(
  left: (a: any, b: any) => CondLike,
  right: (a: any, b: any) => CondLike,
): boolean {
  const build = (make: (a: any, b: any) => CondLike) =>
    buildModule(() => {
      const a = Var(1);
      const b = Var(5);
      const hit = Var(0);
      when(make(a, b), () => hit.iadd(1));
    }).toJson();
  return build(left) === build(right);
}

test("DeferredCond complements map to their structural opposite", () => {
  const rows: Array<[string, (a: any, b: any) => CondLike, (a: any, b: any) => CondLike]> = [
    ["eq -> ne", (a, b) => condNot(a.eq(b)), (a, b) => a.ne(b)],
    ["ne -> eq", (a, b) => condNot(a.ne(b)), (a, b) => a.eq(b)],
    ["ge -> lt", (a, b) => condNot(a.ge(b)), (a, b) => a.lt(b)],
    ["gt -> le", (a, b) => condNot(a.gt(b)), (a, b) => a.le(b)],
    ["le -> gt", (a, b) => condNot(a.le(b)), (a, b) => a.gt(b)],
    ["lt -> ge", (a, b) => condNot(a.lt(b)), (a, b) => a.ge(b)],
  ];
  for (const [name, left, right] of rows) {
    assert.ok(deferredComplementPair(left, right), name);
  }
});

test("condNot(ne(middle constant)) is the direct eq", () => {
  const build = (make: (a: any) => CondLike) =>
    buildModule(() => {
      const a = Var(5);
      const hit = Var(0);
      when(make(a), () => hit.iadd(1));
    }).toJson();
  assert.equal(
    build((a) => condNot(a.ne(5))),
    build((a) => a.eq(5)),
  );
});

test("condNot(Always)/condNot(Never) swap the constants", () => {
  const never = buildModule(() => {
    const hit = Var(0);
    when(condNot(Always()), () => hit.iadd(1));
  });
  assert.equal(whenConditions(never.toJson())[0].condtype, 23);

  const always = buildModule(() => {
    const hit = Var(0);
    when(condNot(Never()), () => hit.iadd(1));
  });
  assert.equal(whenConditions(always.toJson())[0].condtype, 22);
});

test("condNot(opaque) uses one generic flag gate", () => {
  // `hp.ne(0)` folds to `AtLeast 1`, which has no structural complement.
  const direct = buildModule(() => {
    when(CUnit(1000).hp.ne(0), () => SetDeaths(9000, SetTo, 1, 0));
  });
  const directCond = whenConditions(direct.toJson())[0];
  assert.equal(directCond.condtype, 15);
  assert.equal(directCond.comparison, 0); // AtLeast
  assert.equal(directCond.amount, 1);

  for (const [seed, want] of [
    [0, 1],
    [1, 0],
  ] as const) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      CUnit(1000).hp.set(seed);
      const hit = Var(0);
      ids.hit = hit.id;
      when(condNot(CUnit(1000).hp.ne(0)), () => hit.iadd(1));
    });
    const report = runSim(mod.toJson());
    assert.equal(report.var_values[ids.hit], want, `seed=${seed}`);
    // `hp.set(seed)` + reset + set-when + caller gate.
    assert.equal(report.static_cost.triggers, 4, `seed=${seed}`);
    assert.equal(report.static_cost.conditions, 2, `seed=${seed}`);
  }
});

test("De Morgan: condNot distributes over condAll/condAny", () => {
  const complementAll = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const c = Var(1);
    const hit = Var(0);
    when(
      condNot(condAll(a.eq(1), b.eq(0), c.eq(1))),
      () => hit.iadd(1),
    );
  }).toJson();
  const complementAllExplicit = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const c = Var(1);
    const hit = Var(0);
    when(
      condAny(condNot(a.eq(1)), condNot(b.eq(0)), condNot(c.eq(1))),
      () => hit.iadd(1),
    );
  }).toJson();
  assert.equal(complementAll, complementAllExplicit);

  const complementAny = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const c = Var(1);
    const hit = Var(0);
    when(
      condNot(condAny(a.eq(1), b.eq(0), c.eq(1))),
      () => hit.iadd(1),
    );
  }).toJson();
  const complementAnyExplicit = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const c = Var(1);
    const hit = Var(0);
    when(
      condAll(condNot(a.eq(1)), condNot(b.eq(0)), condNot(c.eq(1))),
      () => hit.iadd(1),
    );
  }).toJson();
  assert.equal(complementAny, complementAnyExplicit);
});

test("condNot(condNot(x)) returns x unchanged", () => {
  const condition = Always();
  assert.equal(condNot(condNot(condition)), condition);
});

test("composite markers expose the GatePlan protocol", () => {
  const all = condAll(Always(), Never()) as any;
  assert.equal(all.gatePlan().kind, "and");
  const any = condAny(Always(), Never()) as any;
  assert.equal(any.gatePlan().kind, "or");
  assert.equal(any.gatePlan().nodes.length, 2);

  const notAlways = condNot(Always()) as any;
  const plan = notAlways.gatePlan();
  assert.equal(plan.kind, "lit");
  assert.equal(plan.cond.condtype, 23);

  const opaque = condNot(CUnit(1000).hp.ne(0)) as any;
  assert.equal(opaque.gatePlan().kind, "not_opaque");
});

test("composite markers reject numeric coercion", () => {
  // One-argument forms are identities, so build a real two-branch marker.
  const marker = condAny(Always(), Never());
  assert.throws(() => Number(marker), isEudslError("EUDSL-TYPE"));
});

// ---------------------------------------------------------------------------
// Deep nesting across every structured form
// ---------------------------------------------------------------------------

test("deep nested plans build and run in all six structured forms", () => {
  type Form = "when" | "once" | "until" | "whileLoop" | "onRising" | "onFalling";
  const forms: Form[] = [
    "when",
    "once",
    "until",
    "whileLoop",
    "onRising",
    "onFalling",
  ];
  const expected: Record<Form, number> = {
    when: 2,
    once: 1,
    until: 0,
    whileLoop: 0,
    onRising: 1,
    onFalling: 0,
  };
  for (const form of forms) {
    const ids = { hit: -1 };
    const mod = buildModule(() => {
      const a = Var(0);
      const b = Var(0);
      const c = Var(form === "whileLoop" ? 1 : 0);
      const d = Var(0);
      const hit = Var(0);
      ids.hit = hit.id;
      const cond = condAny(
        condAll(a.eq(1), condNot(b.eq(1))),
        condNot(condAny(c.eq(1), d.eq(1))),
      );
      const body = () => hit.iadd(1);
      switch (form) {
        case "when":
          when(cond, body);
          break;
        case "once":
          once(cond, body);
          break;
        case "until":
          until(cond, body);
          break;
        case "whileLoop":
          whileLoop(cond, body);
          break;
        case "onRising":
          onRising(cond, body);
          break;
        case "onFalling":
          onFalling(cond, body);
          break;
      }
    });
    const report = runSim(mod.toJson(), 2);
    assert.equal(report.var_values[ids.hit], expected[form], form);
    assert.ok(
      report.static_cost.triggers > 2,
      `${form}: expected nested preludes`,
    );
    const kinds = moduleOf(mod.toJson()).stmts.map((stmt: any) => stmt.kind);
    assert.ok(kinds.includes("trigger"), `${form}: expected a reset trigger`);
  }
});

test("deep nested plan that is true hits exactly once in a when", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(1);
    const c = Var(0);
    const d = Var(0);
    const hit = Var(0);
    ids.hit = hit.id;
    when(
      condAny(
        condAll(a.eq(1), condNot(b.eq(1))),
        condNot(condAny(c.eq(1), d.eq(1))),
      ),
      () => hit.iadd(1),
    );
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.hit], 1);
});

// ---------------------------------------------------------------------------
// whileLoop entry/tail refresh
// ---------------------------------------------------------------------------

test("whileLoop(condAny) refreshes its flag at the loop tail", () => {
  const ids = { iters: -1, c: -1 };
  const mod = buildModule(() => {
    const c = Var(0);
    const iters = Var(0);
    ids.c = c.id;
    ids.iters = iters.id;
    whileLoop(condAny(c.lt(3), c.eq(10)), () => {
      iters.iadd(1);
      c.iadd(1);
    });
  });
  const json = mod.toJson();
  // c, iters + one shared gate flag.
  assert.equal(moduleOf(json).vars.length, 3);
  for (const passes of [1, 2, 3, 5]) {
    const values = runSim(json, passes).var_values;
    assert.equal(values[ids.iters], 3, `passes=${passes}`);
    assert.equal(values[ids.c], 3, `passes=${passes}`);
  }
});

test("whileLoop condAny sees body mutations before the next check", () => {
  const ids = { iters: -1 };
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const iters = Var(0);
    ids.iters = iters.id;
    whileLoop(condAny(a.eq(1), b.eq(1)), () => {
      iters.iadd(1);
      a.set(0);
      b.set(0);
    });
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.iters], 1);
});

// ---------------------------------------------------------------------------
// Raw Trigger integration and scope
// ---------------------------------------------------------------------------

test("Trigger(condAny) emits its prelude immediately before the trigger", () => {
  const ids = { hit: -1 };
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    const hit = Var(0);
    ids.hit = hit.id;
    new Trigger({
      conds: [condAny(a.eq(1), b.eq(1))],
      acts: [hit.act_add(1)],
    });
  });
  const stmts = moduleOf(mod.toJson()).stmts;
  assert.deepEqual(
    stmts.map((stmt: any) => stmt.kind),
    ["trigger", "when", "when", "trigger"],
  );
  assert.equal(stmts[3].conds.length, 1);
  assert.equal(runSim(mod.toJson()).var_values[ids.hit], 1);
});

test("detached condAny needs a builder", () => {
  assert.throws(
    () =>
      new Trigger({
        conds: [
          condAny(CUnit(1000).hp.eq(1), CUnit(1000).hp.eq(2)),
        ],
        acts: [],
      }),
    isEudslError("EUDSL-SCOPE"),
  );
});

test("detached DeferredNe keeps its scope error", () => {
  assert.throws(
    () =>
      new Trigger({
        conds: [CUnit(1000).hp.ne(500)],
        acts: [],
      }),
    isEudslError("EUDSL-SCOPE"),
  );
});

// ---------------------------------------------------------------------------
// Argument validation
// ---------------------------------------------------------------------------

test("combinators reject non-condition arguments with EUDSL-TYPE", () => {
  const badCalls: Array<() => unknown> = [
    () => condAll(123 as any),
    () => condAll(false as any),
    () => condAny(true as any),
    () => condAny(null as any),
    () => condNot(undefined as any),
    () => condNot({} as any),
    () => (condNot as (...args: any[]) => unknown)(),
    () => (condNot as (...args: any[]) => unknown)(Always(), Always()),
    () => (condAll as (...args: any[]) => unknown)(Never(), 7),
  ];
  for (const call of badCalls) {
    assert.throws(call, isEudslError("EUDSL-TYPE"));
  }
});

test("a bad trailing cond does not leak a prelude into the builder", () => {
  const mod = buildModule(() => {
    const a = Var(1);
    const b = Var(0);
    assert.throws(
      () =>
        when(
          [condAny(a.eq(1), b.eq(1)), true as unknown as CondLike],
          () => {},
        ),
      isEudslError("EUDSL-TYPE"),
    );
    // The failed call must not leave a flag Var or reset/set-when behind.
    when(a.eq(1), () => {});
  });
  const doc = moduleOf(mod.toJson());
  assert.equal(doc.vars.length, 2); // a, b
  assert.equal(doc.stmts.length, 1); // only the second when
  assert.equal(doc.stmts[0].kind, "when");
});

test("snake_case aliases are the same functions", () => {
  assert.equal(cond_all, condAll);
  assert.equal(cond_any, condAny);
  assert.equal(cond_not, condNot);
});

// ---------------------------------------------------------------------------
// Truthiness checker
// ---------------------------------------------------------------------------

test("truthiness checker flags condition combinators used as booleans", () => {
  for (const call of [
    "condAny(v.eq(1), v.eq(2))",
    "cond_all(v.eq(1))",
    "cond_any(v.eq(1))",
    "condNot(v.eq(1))",
    "cond_not(v.eq(1))",
  ]) {
    const diagnostics = checkSource("fixture.ts", `if (${call}) {}`);
    assert.equal(diagnostics.length, 1, call);
    assert.equal(diagnostics[0].code, "EUDSL-TS-TRUTHIFY", call);
  }

  assert.deepEqual(
    checkSource(
      "fixture.ts",
      "when(condAny(v.eq(1), v.eq(2)), () => {});",
    ),
    [],
  );
});
