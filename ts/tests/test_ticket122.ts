/**
 * Ticket #122: dynamic `div` / `mod` / `divmod` are re-entrant.
 *
 * `divmodLadder` allocates `rem` and `quot` as `new VarImpl(0)`. `Var(0)` is a
 * module initial value written once by lowering, not a per-invocation reset,
 * so a second trigger-loop pass starts from pass one's accumulators. The
 * ladder must zero both accumulators on every call. The general
 * constant-divisor `divmod` ladder has the same defect for `quot`.
 *
 * The pins below are the ticket's measured values and costs on baseline
 * `4525769`. The red rows must use a `Var` divisor; `d.div(7)` takes the
 * already-resetting constant fast path and is a green control.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  REPO_ROOT,
  runSim,
  type SimStaticCost,
} from "./helpers.ts";

import { Var, buildModule, divmod, whileLoop } from "../index.ts";

const FIXTURES = [
  "dynamic_div",
  "dynamic_mod",
  "dynamic_divmod",
  "const_divmod",
  "while_dynamic_div",
  "while_expression_divisor",
  "mul_control",
  "literal_divisor_control",
  "divisor_zero_control",
] as const;

type FixtureName = (typeof FIXTURES)[number];

function buildFixture(name: FixtureName): {
  mod: ReturnType<typeof buildModule>;
  ids: number[];
} {
  const ids: number[] = [];
  const mod = buildModule(() => {
    switch (name) {
      case "dynamic_div": {
        const d = Var(1000000);
        const e = Var(7);
        const f = Var(0);
        f.set(d.div(e));
        ids.push(f.id);
        break;
      }
      case "dynamic_mod": {
        const d = Var(1000000);
        const e = Var(7);
        const f = Var(0);
        f.set(d.mod(e));
        ids.push(f.id);
        break;
      }
      case "dynamic_divmod": {
        const d = Var(1000000);
        const e = Var(7);
        const [q, r] = divmod(d, e);
        const qCopy = Var(0);
        const rCopy = Var(0);
        qCopy.set(q);
        rCopy.set(r);
        ids.push(q.id, r.id, qCopy.id, rCopy.id);
        break;
      }
      case "const_divmod": {
        const d = Var(1000000);
        const [q, r] = divmod(d, 7);
        const qCopy = Var(0);
        const rCopy = Var(0);
        qCopy.set(q);
        rCopy.set(r);
        ids.push(q.id, r.id, qCopy.id, rCopy.id);
        break;
      }
      case "while_dynamic_div": {
        const d = Var(1000000);
        const e = Var(7);
        const n = Var(0);
        const f = Var(0);
        const acc = Var(0);
        whileLoop(n.lt(5), () => {
          f.set(d.div(e));
          acc.iadd(f);
          n.iadd(1);
        });
        ids.push(f.id, acc.id);
        break;
      }
      case "while_expression_divisor": {
        const d = Var(1000000);
        const e = Var(7);
        const n = Var(0);
        const f = Var(0);
        const acc = Var(0);
        whileLoop(n.lt(5), () => {
          f.set(d.add(n).div(e));
          acc.iadd(f);
          n.iadd(1);
        });
        ids.push(f.id, acc.id);
        break;
      }
      case "mul_control": {
        const d = Var(1000000);
        const e = Var(7);
        const f = Var(0);
        f.set(d.mul(e));
        ids.push(f.id);
        break;
      }
      case "literal_divisor_control": {
        const d = Var(1000000);
        const f = Var(0);
        f.set(d.div(7));
        ids.push(f.id);
        break;
      }
      case "divisor_zero_control": {
        const d = Var(1000000);
        const e = Var(0);
        const f = Var(0);
        const g = Var(0);
        f.set(d.div(e));
        g.set(d.mod(e));
        ids.push(f.id, g.id);
        break;
      }
      default: {
        throw new Error(`unknown fixture ${name}`);
      }
    }
  });
  return { mod, ids };
}

function valuesAtLoops(
  fixture: FixtureName,
  loops: readonly number[] = [1, 2, 5],
): { loops: number; values: number[] }[] {
  const { mod, ids } = buildFixture(fixture);
  const hir = mod.toJson();
  return loops.map((n) => {
    const varValues = runSim(hir, n).var_values;
    return { loops: n, values: ids.map((id) => varValues[id]) };
  });
}

function cost(
  fixture: FixtureName,
): [
  [number, number, number, number],
  [number, number, number],
] {
  const report = runSim(buildFixture(fixture).mod.toJson(), 1);
  const staticCost: SimStaticCost = report.static_cost;
  const executed = report.executed_cost;
  return [
    [
      staticCost.triggers,
      staticCost.conditions,
      staticCost.actions,
      staticCost.total_bytes,
    ],
    [executed.triggers, executed.conditions, executed.actions],
  ];
}

const COST_PINS: Record<
  Extract<
    FixtureName,
    | "dynamic_div"
    | "dynamic_mod"
    | "dynamic_divmod"
    | "const_divmod"
    | "while_dynamic_div"
    | "while_expression_divisor"
  >,
  [[number, number, number, number], [number, number, number]]
> = {
  // One dynamic `div` site: the reset adds two acts but no bytes.
  dynamic_div: [
    [259, 96, 741, 623684],
    [280, 71, 5010],
  ],
  dynamic_mod: [
    [259, 96, 741, 623688],
    [280, 71, 5010],
  ],
  dynamic_divmod: [
    [260, 96, 752, 626096],
    [282, 71, 5085],
  ],
  const_divmod: [
    [36, 30, 144, 86696],
    [38, 30, 226],
  ],
  while_dynamic_div: [
    [261, 97, 749, 628504],
    [1416, 361, 25410],
  ],
  while_expression_divisor: [
    [261, 97, 758, 628500],
    [1416, 361, 25460],
  ],
};

test("Ticket 122: dynamic div is re-entrant", () => {
  for (const { loops, values } of valuesAtLoops("dynamic_div")) {
    assert.deepEqual(values, [142857], `loops=${loops}`);
  }
});

test("Ticket 122: dynamic mod is re-entrant", () => {
  for (const { loops, values } of valuesAtLoops("dynamic_mod")) {
    assert.deepEqual(values, [1], `loops=${loops}`);
  }
});

test("Ticket 122: dynamic divmod is re-entrant", () => {
  for (const { loops, values } of valuesAtLoops("dynamic_divmod")) {
    // q, r, qCopy, rCopy: both the ladder results and their copies.
    assert.deepEqual(values, [142857, 1, 142857, 1], `loops=${loops}`);
  }
});

test("Ticket 122: const-divisor divmod is re-entrant", () => {
  for (const { loops, values } of valuesAtLoops("const_divmod")) {
    // q, r, qCopy, rCopy.
    assert.deepEqual(values, [142857, 1, 142857, 1], `loops=${loops}`);
  }
});

test("Ticket 122: while-loop dynamic div is re-entrant", () => {
  const [{ values }] = valuesAtLoops("while_dynamic_div", [1]);
  assert.deepEqual(values, [142857, 714285]);
});

test("Ticket 122: while-loop expression divisor is re-entrant", () => {
  const [{ values }] = valuesAtLoops("while_expression_divisor", [1]);
  assert.deepEqual(values, [142857, 714285]);
});

test("Ticket 122: static and executed cost pins", () => {
  for (const [fixture, pin] of Object.entries(COST_PINS)) {
    assert.deepEqual(cost(fixture as FixtureName), pin, fixture);
  }
});

test("Ticket 122: mul control is unchanged", () => {
  for (const { loops, values } of valuesAtLoops("mul_control")) {
    assert.deepEqual(values, [7000000], `loops=${loops}`);
  }
});

test("Ticket 122: literal divisor control is already green", () => {
  for (const { loops, values } of valuesAtLoops("literal_divisor_control")) {
    assert.deepEqual(values, [142857], `loops=${loops}`);
  }
});

test("Ticket 122: divisor-zero contract is unchanged", () => {
  for (const { loops, values } of valuesAtLoops("divisor_zero_control")) {
    assert.deepEqual(values, [0, 1000000], `loops=${loops}`);
  }
});

const PY_PARITY_SCRIPT = `
import json
from py.tests.test_ticket122 import FIXTURES, _build

print(json.dumps({name: _build(name)[0].to_json() for name in FIXTURES}))
`;

test("Ticket 122: py and ts HIR are byte-identical for every fixture", () => {
  const pyHir = JSON.parse(
    execFileSync("python3", ["-c", PY_PARITY_SCRIPT], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    }),
  ) as Record<string, string>;
  const tsHir = Object.fromEntries(
    FIXTURES.map((name) => [name, buildFixture(name).mod.toJson()]),
  ) as Record<string, string>;
  assert.deepEqual(Object.keys(pyHir).sort(), [...FIXTURES].sort());
  for (const name of FIXTURES) {
    assert.equal(tsHir[name], pyHir[name], name);
  }
});
