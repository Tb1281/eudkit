/**
 * Callable subroutine (`func`) frontend tests.
 *
 * A subroutine body is emitted once per module: call sites bind arguments,
 * jump into the shared body, and copy the result back into a fresh var. These
 * tests cover the capture/placement contract (body statements must land in
 * `module.funcs` and not leak into the caller's scope) and the call-site
 * errors.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { AtLeast, DeathsX, Var, buildModule, func, when } from "../index.ts";

interface HirDoc {
  module: {
    stmts: Array<{ kind: string }>;
    funcs?: Array<{ params: number[]; body: Array<{ kind: string }> }>;
  };
}

test("func body is emitted once and call sites share it", () => {
  const mod = buildModule(() => {
    const acc = Var(0);
    const seen = Var(0);

    const inc = func((v: Var) => {
      v.iadd(1);
      return v;
    });
    const a = inc(acc);
    assert.notEqual(a, null);
    acc.set(a!);
    const b = inc(5);
    acc.set(b!);
    const c = inc(acc);
    acc.set(c!);
    seen.iadd(1);
  });

  const doc = JSON.parse(mod.toJson()) as HirDoc;
  assert.equal(doc.module.funcs?.length, 1);
  assert.equal(doc.module.funcs?.[0].params.length, 1);
  // Raw HIR is pre-optimization: body statements stay in the func. The only
  // root trigger is the trailing `seen.iadd(1)` act. One call passes a
  // constant argument, so the all-or-nothing multi-site rule keeps every call
  // shared instead of partially inlining the function.
  const topKinds = doc.module.stmts.map((s) => s.kind);
  assert.equal(topKinds.filter((k) => k === "trigger").length, 1);
  assert.equal(topKinds.filter((k) => k === "call").length, 3);

  const rep = runSim(mod.toJson(), 1);
  // acc: 0 -> 1 -> 6 -> 7; seen: 1.
  assert.deepEqual(rep.var_values.slice(0, 2), [7, 1]);
});

test("body capture keeps nested scopes inside the body", () => {
  const mod = buildModule(() => {
    const hits = Var(0);
    const flag = Var(0);

    const record = func(() => {
      hits.iadd(1);
      when(flag.eq(0), () => {
        hits.iadd(10);
      });
    });
    record();
    record();
  });

  const doc = JSON.parse(mod.toJson()) as HirDoc;
  const body = doc.module.funcs?.[0].body ?? [];
  assert.deepEqual(
    body.map((s) => s.kind),
    ["trigger", "when"],
  );
  assert.deepEqual(
    doc.module.stmts.map((s) => s.kind),
    ["call", "call"],
  );

  const rep = runSim(mod.toJson(), 1);
  // Two sites of a `Trigger + When` body: the static estimate ties (shared
  // `2 + 2` against two duplicated copies), and `INLINE_EXTRA_TRIGGERS = 0`
  // accepts the tie. The gate is evaluated once per site rather than once for
  // the shared body, which is the extra condition the static tie accepts.
  assert.equal(rep.static_cost.triggers, 4);
  assert.equal(rep.static_cost.conditions, 2);
  assert.equal(rep.static_cost.actions, 4);
  assert.equal(rep.executed_cost.triggers, 4);
  assert.equal(rep.executed_cost.conditions, 2);
  assert.equal(rep.executed_cost.actions, 4);
  assert.equal(rep.var_values[0], 22);
});

test("a destination-bound argument reads the pre-call value", () => {
  const mod = buildModule(() => {
    const x = Var(7);
    const y = Var(11);
    const k = func((p: Var) => {
      const r = Var(0);
      r.set(0);
      when([DeathsX(p.value, AtLeast, 1, 0, 1)], () => {
        r.iadd(5);
      });
      return r;
    });
    x.set(k(x)!);
    y.set(k(y)!);
  });
  const rep = runSim(mod.toJson(), 1);
  // Both sites bind the destination to the argument, and the body writes its
  // result before reading the parameter. Inlining substitutes `x` for `p`, so
  // the gate would read the freshly written 0 result instead of the pre-call
  // value; the call must stay shared and see the original argument (7 and 11
  // are both non-zero), yielding 5 at each site.
  assert.equal(rep.var_values[0], 5);
  assert.equal(rep.var_values[1], 5);
});

test("small bodies inline and large bodies share", () => {
  const measure = (acts: number, sites: number) => {
    const mod = buildModule(() => {
      const target = Var(0);
      const kernel = func(() => {
        for (let i = 0; i < acts; i++) target.iadd(1);
      });
      for (let i = 0; i < sites; i++) kernel();
    });
    return runSim(mod.toJson(), 1);
  };

  // A 12-act leaf run at one site has always been single-site inlined.
  const one = measure(12, 1);
  assert.equal(one.static_cost.triggers, 1);
  assert.equal(one.static_cost.actions, 12);
  // At three sites the copies pack into one trigger: 36 actions, no shared
  // call machinery. That is the cost-based win over `4T` shared.
  const three = measure(12, 3);
  assert.equal(three.static_cost.triggers, 1);
  assert.equal(three.static_cost.actions, 36);
  assert.equal(three.executed_cost.triggers, 1);
  assert.equal(three.executed_cost.actions, 36);
  assert.equal(three.var_values[0], 36);

  // A 200-act body needs 7 packed triggers when duplicated against a shared
  // 6, so `INLINE_EXTRA_TRIGGERS = 0` keeps it shared.
  const large = measure(200, 2);
  assert.equal(large.static_cost.triggers, 6);
  assert.equal(large.static_cost.actions, 202);
  assert.equal(large.executed_cost.triggers, 10);
  assert.equal(large.executed_cost.actions, 402);
  assert.equal(large.var_values[0], 400);
});

test("inline crossover tracks body size and sites", () => {
  const measure = (acts: number, sites: number, separated = false) => {
    const mod = buildModule(() => {
      const target = Var(0);
      const kernel = func(() => {
        for (let i = 0; i < acts; i++) target.iadd(1);
      });
      for (let site = 0; site < sites; site++) {
        // The frontend emits this as an unconditional `Trigger`, which splits
        // the scheduler's act run: the copies cannot pack across it.
        if (separated && site === 1) target.iadd(1);
        kernel();
      }
    });
    const rep = runSim(mod.toJson(), 1);
    return {
      static: [
        rep.static_cost.triggers,
        rep.static_cost.conditions,
        rep.static_cost.actions,
      ],
      executed: [
        rep.executed_cost.triggers,
        rep.executed_cost.conditions,
        rep.executed_cost.actions,
      ],
      value: rep.var_values[0],
    };
  };

  const cases: Array<{
    acts: number;
    sites: number;
    separated: boolean;
    static: number[];
    executed: number[];
    value: number;
  }> = [
    { acts: 1, sites: 2, separated: false, static: [1, 0, 2], executed: [1, 0, 2], value: 2 },
    { acts: 8, sites: 2, separated: false, static: [1, 0, 16], executed: [1, 0, 16], value: 16 },
    { acts: 40, sites: 2, separated: false, static: [2, 0, 80], executed: [2, 0, 80], value: 80 },
    { acts: 100, sites: 2, separated: false, static: [4, 0, 200], executed: [4, 0, 200], value: 200 },
    { acts: 100, sites: 3, separated: false, static: [5, 0, 300], executed: [5, 0, 300], value: 300 },
    { acts: 100, sites: 2, separated: true, static: [5, 0, 201], executed: [5, 0, 201], value: 201 },
  ];
  for (const c of cases) {
    const got = measure(c.acts, c.sites, c.separated);
    const label = `acts=${c.acts} sites=${c.sites} separated=${c.separated}`;
    assert.deepEqual(got.static, c.static, `${label} static`);
    assert.deepEqual(got.executed, c.executed, `${label} executed`);
    assert.equal(got.value, c.value, `${label} value`);
  }
});

test("boundary bodies stay shared", () => {
  const measure = (acts: number, sites: number) => {
    const mod = buildModule(() => {
      const target = Var(0);
      const kernel = func(() => {
        for (let i = 0; i < acts; i++) target.iadd(1);
      });
      for (let i = 0; i < sites; i++) kernel();
    });
    const rep = runSim(mod.toJson(), 1);
    return {
      static: [
        rep.static_cost.triggers,
        rep.static_cost.conditions,
        rep.static_cost.actions,
      ],
      executed: [
        rep.executed_cost.triggers,
        rep.executed_cost.conditions,
        rep.executed_cost.actions,
      ],
    };
  };

  // `ceil(sites * acts / 64)` exceeds `ceil(acts / 64) + sites` in every case,
  // so the default rejects the extra static trigger.
  assert.deepEqual(measure(200, 2), {
    static: [6, 0, 202],
    executed: [10, 0, 402],
  });
  assert.deepEqual(measure(100, 4), {
    static: [6, 0, 104],
    executed: [12, 0, 404],
  });
});

test("call arity is checked", () => {
  assert.throws(
    () =>
      buildModule(() => {
        const kernel = func((a: Var, b: Var) => {
          a.iadd(b);
        });
        (kernel as (...args: (Var | number)[]) => unknown)(1);
      }),
    /expects 2 argument\(s\), got 1/,
  );
});

test("call arguments must be a Var or an integer", () => {
  assert.throws(
    () =>
      buildModule(() => {
        const kernel = func((a: Var) => {
          a.iadd(1);
        });
        (kernel as (...args: unknown[]) => unknown)("nope");
      }),
    /Var or a constant integer/,
  );
});

test("body must return a Var or nothing", () => {
  assert.throws(
    () =>
      buildModule(() => {
        func(() => 5 as unknown as void);
      }),
    /return a Var or None/,
  );
});

test("func requires buildModule", () => {
  assert.throws(() => func(() => {}), /inside buildModule\(\)/);
});
