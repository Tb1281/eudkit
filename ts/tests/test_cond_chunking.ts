/**
 * ADR 0027: `>16` condition auto-chunking for structured control flow.
 *
 * A raw `Trigger (16C, 64A)` stays capped at 16 conditions (`EUDSL-BOUNDS`).
 * The structured forms below must split their condition list into 16-condition
 * chunks so 17/32/33-condition gates lower and assemble cleanly:
 *
 *   when (with/without else, empty then + else), once, until,
 *   whileLoop, onRising, onFalling.
 *
 * The `<=16` shape is pinned in `crates/eudkit-lower/src/control.rs`; these
 * tests cover lowering, assembly, and runtime behaviour for the chunked path.
 *
 * `once` with an empty body cannot be exercised through the frontend: opt
 * drops it before lowering. That direct-lowering case is pinned in
 * `crates/eudkit-lower/tests/cond_chunking.rs`.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim, runStatic } from "./helpers.ts";

import {
  Var,
  buildModule,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
} from "../index.ts";

type Form =
  | "when"
  | "when-else"
  | "when-empty-else"
  | "once"
  | "until"
  | "while"
  | "onRising"
  | "onFalling";

interface PackingReport {
  static_cost: { conditions: number };
  packing: { item: number; conditions: number }[];
}

/** Distinct `Var`s per condition defeat condition fusion in opt. */
function condVars(n: number, init: number) {
  return Array.from({ length: n }, () => Var(init));
}

/**
 * Build one structured form with `n` conditions.
 *
 * `marker` is always `var_values[0]`; every condition is `v.eq(1)` and the
 * optional `init` chooses whether the gate is true or false on loop 1.
 */
function makeModule(form: Form, n: number, init: number) {
  return buildModule(() => {
    const marker = Var(0);
    const conditionVars = condVars(n, init);
    const conds = conditionVars.map((v) => v.eq(1));

    switch (form) {
      case "when":
        when(conds, () => marker.iadd(1));
        break;
      case "when-else":
        // Distinguish the arms so a false gate cannot pass by running then.
        when(
          conds,
          () => marker.iadd(10),
          () => marker.iadd(1),
        );
        break;
      case "when-empty-else":
        when(
          conds,
          () => {},
          () => marker.iadd(1),
        );
        break;
      case "once":
        once(conds, () => marker.iadd(1));
        break;
      case "until":
        until(conds, () => marker.iadd(1));
        break;
      case "while":
        whileLoop(conds, () => {
          // Break the loop after the first body pass so `sim` terminates.
          conditionVars[0].set(0);
          marker.iadd(1);
        });
        break;
      case "onRising":
        onRising(conds, () => marker.iadd(1));
        break;
      case "onFalling": {
        const frame = Var(0);
        frame.iadd(1);
        when(frame.eq(2), () => {
          for (const v of conditionVars) v.set(0);
        });
        onFalling(conds, () => marker.iadd(1));
        break;
      }
    }
  });
}

function reportFor(form: Form, n: number, init: number): PackingReport {
  const report = runStatic(makeModule(form, n, init).toJson());
  return {
    static_cost: { conditions: report.static_cost.conditions },
    packing: report.packing,
  };
}

/**
 * Frame-ordered module: frame 1 is T/F, frame 2 F/T, frames 3-4 T/T.
 * The setup `when`s run before the chunked gate in the same trigger loop.
 */
function makeSequenceModule(form: "when" | "once", n: number) {
  return buildModule(() => {
    const marker = Var(0);
    const frame = Var(0);
    frame.iadd(1);
    const conditionVars = condVars(n, 1);
    const conds = conditionVars.map((v) => v.eq(1));

    when(frame.eq(1), () => conditionVars[n - 1].set(0));
    when(frame.eq(2), () => {
      conditionVars[0].set(0);
      conditionVars[n - 1].set(1);
    });
    when(frame.eq(3), () => conditionVars[0].set(1));

    if (form === "when") when(conds, () => marker.iadd(1));
    else once(conds, () => marker.iadd(1));
  });
}

function assertAllTriggersChunked(report: PackingReport, label: string, n: number) {
  assert.ok(
    report.static_cost.conditions >= n,
    `${label} n=${n}: all ${n} conditions must survive opt; saw ${report.static_cost.conditions}`,
  );
  for (const entry of report.packing) {
    assert.ok(
      entry.conditions <= 16,
      `${label} n=${n}: trigger[${entry.item}] carries ${entry.conditions} conditions; ADR 0027 requires <= 16`,
    );
  }
  const conditionBearing = report.packing.filter((entry) => entry.conditions > 0);
  assert.ok(
    conditionBearing.length >= Math.ceil(n / 16),
    `${label} n=${n}: expected at least ${Math.ceil(n / 16)} condition-bearing triggers, saw ${conditionBearing.length}`,
  );
}

test("all structured forms lower and assemble at 17/32/33 conditions", () => {
  const forms: { form: Form; init: number }[] = [
    { form: "when", init: 1 },
    { form: "when-else", init: 1 },
    { form: "when-empty-else", init: 0 },
    { form: "once", init: 1 },
    { form: "until", init: 0 },
    { form: "while", init: 1 },
    { form: "onRising", init: 1 },
    { form: "onFalling", init: 1 },
  ];

  for (const n of [17, 32, 33]) {
    for (const { form, init } of forms) {
      assertAllTriggersChunked(reportFor(form, n, init), form, n);
    }
  }
});

test("<=16 conditions keep the same single-gate shape", () => {
  // 16 conditions must remain one 16C trigger plus body/restore, not get
  // pre-chunked. Any chunking rewrite that splits at <=16 shows up here.
  for (const n of [0, 1, 16]) {
    const report = reportFor("when", n, 1);
    const conditionBearing = report.packing.filter((entry) => entry.conditions > 0);
    const expectedGates = n === 0 ? 0 : 1;
    assert.equal(
      conditionBearing.length,
      expectedGates,
      `n=${n}: <=16 when must keep a single condition-bearing gate`,
    );
    assert.deepEqual(
      conditionBearing.map((entry) => entry.conditions),
      n === 0 ? [] : [n],
      `n=${n}: the gate must carry exactly ${n} conditions`,
    );
  }
});

interface BehaviourCase {
  label: string;
  form: Form;
  init: number;
  loops: number;
  expected: number;
}

test("chunked gates keep their runtime semantics at 17 conditions", () => {
  const cases: BehaviourCase[] = [
    { label: "when true", form: "when", init: 1, loops: 2, expected: 2 },
    { label: "when false", form: "when", init: 0, loops: 2, expected: 0 },
    { label: "when true/else", form: "when-else", init: 1, loops: 2, expected: 20 },
    { label: "when false/else", form: "when-else", init: 0, loops: 2, expected: 2 },
    {
      label: "when empty-then/else",
      form: "when-empty-else",
      init: 0,
      loops: 2,
      expected: 2,
    },
    { label: "once", form: "once", init: 1, loops: 3, expected: 1 },
    { label: "until (conds false)", form: "until", init: 0, loops: 2, expected: 2 },
    { label: "whileLoop", form: "while", init: 1, loops: 2, expected: 1 },
    { label: "onRising", form: "onRising", init: 1, loops: 2, expected: 1 },
  ];

  for (const c of cases) {
    const report = runSim(makeModule(c.form, 17, c.init).toJson(), c.loops);
    assert.equal(
      report.var_values[0],
      c.expected,
      `${c.label}: marker after ${c.loops} loop(s)`,
    );
  }
});

test("an all-false chunked gate costs exactly 1T", () => {
  for (const n of [17, 33]) {
    const report = runSim(makeModule("when", n, 0).toJson(), 1);
    assert.equal(
      report.executed_cost.triggers,
      1,
      `n=${n}: an all-false chunked gate must cost exactly 1T`,
    );
    assert.deepEqual(
      report.executed_cost.visited_items,
      [0],
      `n=${n}: only g0 may be evaluated on an all-false pass`,
    );
  }
});

test("chunked when/once keep T/F -> F/T -> T/T -> T/T frame order", () => {
  for (const form of ["when", "once"] as const) {
    const mod = makeSequenceModule(form, 17);
    const markers = [1, 2, 3, 4].map(
      (loops) => runSim(mod.toJson(), loops).var_values[0],
    );
    assert.deepEqual(
      markers,
      form === "when" ? [0, 0, 1, 2] : [0, 0, 1, 1],
      `${form}: marker after T/F, F/T, T/T, T/T frames`,
    );
  }
});

test("chunked while executes multiple body passes before chunk 1 fails", () => {
  const n = 33;
  const mod = buildModule(() => {
    const marker = Var(0);
    const conditionVars = Array.from({ length: n }, (_, i) =>
      Var(i === 16 ? 2 : 1),
    );
    const conds = conditionVars.map((v, i) =>
      i === 16 ? v.ge(1) : v.eq(1),
    );
    whileLoop(conds, () => {
      marker.iadd(1);
      conditionVars[16].isub(1);
    });
  });

  const report = runSim(mod.toJson(), 1);
  assert.equal(
    report.var_values[0],
    2,
    "the while body must execute twice before chunk 1 fails",
  );
  assert.equal(
    report.var_values[17],
    0,
    "the chunk-1 countdown must reach zero",
  );
});

test("onFalling fires only on the all-false loop after a 17-condition true loop", () => {
  const mod = makeModule("onFalling", 17, 1);
  assert.equal(
    runSim(mod.toJson(), 1).var_values[0],
    0,
    "a true loop is not a falling edge",
  );
  assert.equal(
    runSim(mod.toJson(), 2).var_values[0],
    1,
    "the first all-false loop must fire exactly once",
  );
});
