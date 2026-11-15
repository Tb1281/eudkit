/**
 * Ticket #51: expose the runtime `else_body` arm on `when`.
 *
 * The HIR field, lowering and opt pass already exist; these pins cover the TS
 * three-argument surface and its dispatch:
 *
 * - `when(conds, then, other)` attaches `else_body`;
 * - `when(false, then, other)` queues `when([], other)` unconditionally,
 *   while the two-argument `when(false, then)` still drops the block;
 * - `when(true, then, other)` / `when([], then, other)` emit the then-only
 *   node and never run `other`;
 * - an empty other body omits the `else_body` key, so then-only HIR stays
 *   byte-identical;
 * - the else arm is the false path of the gate trigger, so
 *   `when([a, b], A, B)` runs `B` iff `NOT(a AND b)`.
 *
 * Cost pins (one page transfer per arm, four vars):
 *
 * - then-only and the unconditional-then/unconditional-else forms: 4T/9,640 B;
 * - both one-transfer arms: 6T/14,460 B;
 * - empty then body with a non-empty else arm: 5T/12,048 B.
 *
 * The same probes live in `py/tests/test_ticket51.py`.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import test from "node:test";

import { REPO_ROOT, runSim, runStatic } from "./helpers.ts";
import {
  EudslError,
  Var,
  buildModule,
  when,
  type Var as VarType,
} from "../index.ts";

const THEN_ONLY_STATIC = [4, 1, 69, 9_640] as const;
const BOTH_ARMS_STATIC = [6, 1, 72, 14_460] as const;
const UNCONDITIONAL_STATIC = [4, 0, 69, 9_640] as const;
const EMPTY_THEN_STATIC = [5, 1, 69, 12_048] as const;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [
    cost.triggers,
    cost.conditions,
    cost.actions,
    cost.total_bytes,
  ];
}

function whenNodes(mod: any): any[] {
  return JSON.parse(mod.toJson()).module.stmts.filter(
    (stmt: any) => stmt.kind === "when",
  );
}

interface CaseIds {
  thenHit: number;
  elseHit: number;
}

interface CaseRun {
  mod: any;
  ids: CaseIds;
  values: CaseIds;
}

function runCase(
  makeCall: (
    gate: VarType,
    src: VarType,
    thenHit: VarType,
    elseHit: VarType,
  ) => void,
  gateInit = 0,
): CaseRun {
  const ids: CaseIds = { thenHit: -1, elseHit: -1 };
  const mod = buildModule(() => {
    const gate = Var(gateInit);
    const src = Var(1);
    const thenHit = Var(0);
    const elseHit = Var(0);
    ids.thenHit = thenHit.id;
    ids.elseHit = elseHit.id;
    makeCall(gate, src, thenHit, elseHit);
  });
  const values = runSim(mod.toJson()).var_values;
  return {
    mod,
    ids,
    values: {
      thenHit: values[ids.thenHit],
      elseHit: values[ids.elseHit],
    },
  };
}

const callback = (
  gate: VarType,
  src: VarType,
  thenHit: VarType,
  elseHit: VarType,
): void => {
  when(
    [gate.eq(1)],
    () => thenHit.set(src),
    () => elseHit.set(src),
  );
};

test("Ticket 51: callback else runs when the gate is false", () => {
  const { values } = runCase(callback, 0);
  assert.equal(values.thenHit, 0);
  assert.equal(values.elseHit, 1);
});

test("Ticket 51: callback else is skipped when the gate is true", () => {
  const { values } = runCase(callback, 1);
  assert.equal(values.thenHit, 1);
  assert.equal(values.elseHit, 0);
});

test("Ticket 51: callback else HIR has else_body", () => {
  const { mod, ids } = runCase(callback, 0);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const node = nodes[0];
  assert.ok("else_body" in node);
  assert.deepEqual(
    node.body.map((stmt: any) => stmt.dst_var),
    [ids.thenHit],
  );
  assert.deepEqual(
    node.else_body.map((stmt: any) => stmt.dst_var),
    [ids.elseHit],
  );
  assert.deepEqual(staticCost(mod.toJson()), [...BOTH_ARMS_STATIC]);
});

test("Ticket 51: empty then body with an else arm costs five triggers", () => {
  const { mod, ids, values } = runCase(
    (gate, src, _thenHit, elseHit) =>
      when(
        [gate.eq(1)],
        () => {},
        () => elseHit.set(src),
      ),
    0,
  );
  const node = whenNodes(mod)[0];
  assert.deepEqual(node.body, []);
  assert.ok("else_body" in node);
  assert.deepEqual(
    node.else_body.map((stmt: any) => stmt.dst_var),
    [ids.elseHit],
  );
  assert.deepEqual(staticCost(mod.toJson()), [...EMPTY_THEN_STATIC]);
  assert.equal(values.elseHit, 1);
});

test("Ticket 51: when(false, then, other) runs other unconditionally", () => {
  const { mod, ids, values } = runCase(
    (_gate, src, thenHit, elseHit) =>
      when(
        false,
        () => thenHit.set(src),
        () => elseHit.set(src),
      ),
    0,
  );
  assert.equal(values.thenHit, 0);
  assert.equal(values.elseHit, 1);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const node = nodes[0];
  assert.deepEqual(node.conds, []);
  assert.equal("else_body" in node, false);
  assert.deepEqual(
    node.body.map((stmt: any) => stmt.dst_var),
    [ids.elseHit],
  );
  assert.deepEqual(staticCost(mod.toJson()), [...UNCONDITIONAL_STATIC]);
});

test("Ticket 51: when(true, then, other) ignores other", () => {
  const { mod, ids, values } = runCase(
    (_gate, src, thenHit, elseHit) =>
      when(
        true,
        () => thenHit.set(src),
        () => elseHit.set(src),
      ),
    0,
  );
  assert.equal(values.thenHit, 1);
  assert.equal(values.elseHit, 0);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const node = nodes[0];
  assert.deepEqual(node.conds, []);
  assert.equal("else_body" in node, false);
  assert.deepEqual(
    node.body.map((stmt: any) => stmt.dst_var),
    [ids.thenHit],
  );
  assert.deepEqual(staticCost(mod.toJson()), [...UNCONDITIONAL_STATIC]);
});

test("Ticket 51: when([], then, other) ignores other", () => {
  const { mod, ids, values } = runCase(
    (_gate, src, thenHit, elseHit) =>
      when(
        [],
        () => thenHit.set(src),
        () => elseHit.set(src),
      ),
    0,
  );
  assert.equal(values.thenHit, 1);
  assert.equal(values.elseHit, 0);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const node = nodes[0];
  assert.deepEqual(node.conds, []);
  assert.equal("else_body" in node, false);
  assert.deepEqual(
    node.body.map((stmt: any) => stmt.dst_var),
    [ids.thenHit],
  );
  assert.deepEqual(staticCost(mod.toJson()), [...UNCONDITIONAL_STATIC]);
});

test("Ticket 51: two-arg when(false, then) still drops", () => {
  const { mod, values } = runCase(
    (_gate, src, thenHit, _elseHit) =>
      when(false, () => thenHit.set(src)),
    0,
  );
  assert.equal(values.thenHit, 0);
  assert.equal(values.elseHit, 0);
  assert.deepEqual(whenNodes(mod), []);
});

test("Ticket 51: when(false, then, empty other) still drops", () => {
  const { mod, values } = runCase(
    (_gate, src, thenHit, _elseHit) =>
      when(
        false,
        () => thenHit.set(src),
        () => {},
      ),
    0,
  );
  assert.equal(values.thenHit, 0);
  assert.deepEqual(whenNodes(mod), []);
});

test("Ticket 51: false drop outside buildModule is a scope error", () => {
  for (const call of [
    () => when(false, () => {}),
    () => when(false, () => {}, () => {}),
  ]) {
    assert.throws(call, (error: any) => {
      assert.ok(error instanceof EudslError);
      assert.equal(error.code, "EUDSL-SCOPE");
      return true;
    });
  }
});

test("Ticket 51: empty other body is then-only", () => {
  const withElse = runCase(
    (gate, src, thenHit, _elseHit) =>
      when(
        [gate.eq(1)],
        () => thenHit.set(src),
        () => {},
      ),
    0,
  );
  const thenOnly = runCase(
    (gate, src, thenHit, _elseHit) =>
      when([gate.eq(1)], () => thenHit.set(src)),
    0,
  );
  assert.equal(withElse.mod.toJson(), thenOnly.mod.toJson());
  assert.equal("else_body" in whenNodes(withElse.mod)[0], false);
  assert.deepEqual(staticCost(withElse.mod.toJson()), [...THEN_ONLY_STATIC]);
});

test("Ticket 51: a non-callable other is a type error", () => {
  assert.throws(
    () =>
      buildModule(() => {
        when([Var(1).eq(1)], () => {}, 1 as any);
      }),
    (error: any) => {
      assert.ok(error instanceof EudslError);
      assert.equal(error.code, "EUDSL-TYPE");
      return true;
    },
  );
});

test("Ticket 51: multi-cond other is NOT(all conds)", () => {
  const rows: Array<[number, number, number, number]> = [
    [0, 1, 0, 1],
    [1, 0, 0, 1],
    [0, 0, 0, 1],
    [1, 1, 1, 0],
  ];
  for (const [aInit, bInit, wantThen, wantElse] of rows) {
    const ids = { thenHit: -1, elseHit: -1 };
    const mod = buildModule(() => {
      const a = Var(aInit);
      const b = Var(bInit);
      const src = Var(1);
      const thenHit = Var(0);
      const elseHit = Var(0);
      ids.thenHit = thenHit.id;
      ids.elseHit = elseHit.id;
      when(
        [a.eq(1), b.eq(1)],
        () => thenHit.set(src),
        () => elseHit.set(src),
      );
    });
    const values = runSim(mod.toJson()).var_values;
    assert.equal(values[ids.thenHit], wantThen, `a=${aInit} b=${bInit}`);
    assert.equal(values[ids.elseHit], wantElse, `a=${aInit} b=${bInit}`);
  }
});

function buildParityModule(): any {
  return buildModule(() => {
    const gate = Var(1);
    const src = Var(1);
    const thenHit = Var(0);
    const elseHit = Var(0);
    when(
      [gate.eq(1)],
      () => thenHit.set(src),
      () => elseHit.set(src),
    );

    const gate2 = Var(0);
    const src2 = Var(2);
    const then2 = Var(0);
    when([gate2.eq(1)], () => then2.set(src2));

    const gate3 = Var(0);
    const src3 = Var(3);
    const then3 = Var(0);
    const else3 = Var(0);
    when(
      false,
      () => then3.set(src3),
      () => else3.set(src3),
    );

    const gate4 = Var(1);
    const src4 = Var(4);
    const then4 = Var(0);
    const else4 = Var(0);
    when(
      true,
      () => then4.set(src4),
      () => else4.set(src4),
    );

    const gate5 = Var(1);
    const src5 = Var(5);
    const then5 = Var(0);
    const else5 = Var(0);
    when(
      [gate5.eq(1)],
      () => then5.set(src5),
      () => else5.set(src5),
    );

    const gate6 = Var(1);
    const src6 = Var(6);
    const then6 = Var(0);
    when([gate6.eq(1)], () => then6.set(src6), () => {});

    const gate7 = Var(1);
    const src7 = Var(7);
    const then7 = Var(0);
    when([gate7.eq(1)], () => then7.set(src7));

    const gate8 = Var(0);
    const src8 = Var(8);
    const then8 = Var(0);
    when(
      false,
      () => then8.set(src8),
      () => {},
    );
  });
}

const PY_PARITY_SCRIPT = `
from py import Var, build_module, when

def prog():
    gate = Var(1)
    src = Var(1)
    then_hit = Var(0)
    else_hit = Var(0)
    when(
        [gate.eq(1)],
        lambda: then_hit.set(src),
        lambda: else_hit.set(src),
    )

    gate2 = Var(0)
    src2 = Var(2)
    then2 = Var(0)
    when([gate2.eq(1)], lambda: then2.set(src2))

    gate3 = Var(0)
    src3 = Var(3)
    then3 = Var(0)
    else3 = Var(0)
    when(
        False,
        lambda: then3.set(src3),
        lambda: else3.set(src3),
    )

    gate4 = Var(1)
    src4 = Var(4)
    then4 = Var(0)
    else4 = Var(0)
    when(
        True,
        lambda: then4.set(src4),
        lambda: else4.set(src4),
    )

    gate5 = Var(1)
    src5 = Var(5)
    then5 = Var(0)
    else5 = Var(0)
    with when([gate5.eq(1)]) as w:
        then5.set(src5)
        w.otherwise(lambda: else5.set(src5))

    gate6 = Var(1)
    src6 = Var(6)
    then6 = Var(0)
    when([gate6.eq(1)], lambda: then6.set(src6), lambda: None)

    gate7 = Var(1)
    src7 = Var(7)
    then7 = Var(0)
    when([gate7.eq(1)], lambda: then7.set(src7))

    gate8 = Var(0)
    src8 = Var(8)
    then8 = Var(0)
    when(
        False,
        lambda: then8.set(src8),
        lambda: None,
    )

import sys
sys.stdout.write(build_module(prog).to_json())
`;

test("Ticket 51: py/ts HIR parity for else forms", () => {
  const tsHir = buildParityModule().toJson();
  const pyHir = execFileSync("python3", ["-c", PY_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});
