/**
 * T-WP10-T1: `freeListEmpty` is the one-condition CCMU capacity truth.
 *
 * `0x628438` is the head of the CUnit free list. `head == 0` is the exact
 * answer to "the engine has no free CUnit slot right now", and it lowers to
 * one condition 15 on `EPD(0x628438)` with comparison `Exactly` and amount 0.
 * The helper is a predicate, not a trigger: the caller's `when`/`once`/`until`
 * owns the gate and keeps its 16-condition budget.
 *
 * The same probes live in `py/tests/test_capacity.py`.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  EPD,
  Exactly,
  Memory,
  SetDeaths,
  SetMemory,
  SetTo,
  buildModule,
  when,
} from "../index.ts";
import {
  CUNIT_FREE_HEAD,
  CUNIT_FREE_TAIL,
  freeListEmpty,
  free_list_empty,
} from "../capacity.ts";
import { REPO_ROOT, runSim, runStatic } from "./helpers.ts";

const BODY_EPD = 9000;
const BODY_VALUE = 1;

interface HirCondNode {
  condtype?: number;
  player?: number;
  comparison?: number;
  amount?: number;
  [key: string]: unknown;
}

interface HirActNode {
  dest?: number;
  value?: number;
  [key: string]: unknown;
}

interface HirStmtNode {
  kind: string;
  conds?: HirCondNode[];
  body?: HirActNode[];
  [key: string]: unknown;
}

interface HirDoc {
  module: { stmts: HirStmtNode[] };
}

/** Only the gated statement: one trigger, one condition, one body act. */
function gateHir(): string {
  return buildModule(() => {
    when([freeListEmpty()], () => {
      SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0);
    });
  }).toJson();
}

/** Write `headValue` to the free-list head, then run the gate. */
function headValueHir(headValue: number): string {
  return buildModule(() => {
    SetMemory(CUNIT_FREE_HEAD, SetTo, headValue);
    when([freeListEmpty()], () => {
      SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0);
    });
  }).toJson();
}

/** The helper composes with another external condition in one gate. */
function composedHir(): string {
  return buildModule(() => {
    when(
      [freeListEmpty(), Memory(CUNIT_FREE_TAIL, Exactly, 0)],
      () => {
        SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0);
      },
    );
  }).toJson();
}

function stmts(hirJson: string): HirStmtNode[] {
  return (JSON.parse(hirJson) as HirDoc).module.stmts;
}

function onlyWhen(hirJson: string): HirStmtNode {
  const gates = stmts(hirJson).filter((stmt) => stmt.kind === "when");
  assert.equal(gates.length, 1, JSON.stringify(gates));
  return gates[0];
}

test("condition is one Exactly-zero memory read", () => {
  const cond = freeListEmpty() as unknown as HirCondNode;
  assert.equal(cond.condtype, 15);
  assert.equal(cond.player, 161845);
  assert.equal(cond.comparison, 10);
  assert.equal(cond.amount, 0);
});

test("a pure predicate builds outside buildModule", () => {
  const cond = freeListEmpty() as unknown as HirCondNode;
  assert.equal(cond.condtype, 15);
});

test("addresses pin head and tail arithmetic", () => {
  assert.equal(CUNIT_FREE_HEAD, 0x628438);
  assert.equal(CUNIT_FREE_TAIL, 0x62843c);
  assert.equal(EPD(CUNIT_FREE_HEAD), 161845);
  assert.equal(EPD(CUNIT_FREE_TAIL), 161846);
});

test("snake-case alias is the same function", () => {
  assert.equal(free_list_empty, freeListEmpty);
});

test("when emits one gate trigger with one condition", () => {
  const gate = onlyWhen(gateHir());
  assert.equal(gate.conds?.length, 1);
  assert.deepEqual(gate.conds?.[0], {
    locid: 0,
    player: 161845,
    amount: 0,
    unit: 0,
    comparison: 10,
    condtype: 15,
    restype: 0,
    flags: 0,
    eudx: 0,
  });
  assert.equal(gate.body?.[0].dest, BODY_EPD);
  assert.equal(gate.body?.[0].value, BODY_VALUE);
});

test("static and executed cost are one trigger", () => {
  const hirJson = gateHir();
  const stat = runStatic(hirJson).static_cost;
  const executed = runSim(hirJson, 1).executed_cost;
  assert.deepEqual(
    [stat.triggers, stat.conditions, stat.actions],
    [1, 1, 1],
  );
  assert.deepEqual(
    [executed.triggers, executed.conditions, executed.actions],
    [1, 1, 1],
  );
});

test("two-condition gate stays one trigger", () => {
  const gate = onlyWhen(composedHir());
  assert.deepEqual(
    gate.conds?.map((cond) => cond.player),
    [161845, 161846],
  );
  const stat = runStatic(composedHir()).static_cost;
  assert.deepEqual([stat.triggers, stat.conditions], [1, 2]);
});

test("empty head runs the body", () => {
  const report = runSim(headValueHir(0), 1);
  assert.equal(report.external_epds[`${BODY_EPD}`] ?? 0, BODY_VALUE);
});

test("nonempty head skips the body", () => {
  const report = runSim(headValueHir(5), 1);
  assert.equal(report.external_epds[`${BODY_EPD}`] ?? 0, 0);
  // The write itself proves the test observed the same dword the gate reads,
  // not an alias.
  assert.equal(report.external_epds[`${EPD(CUNIT_FREE_HEAD)}`] ?? 0, 5);
});

test("HIR is byte-identical with Python", () => {
  const tsHir = buildModule(() => {
    SetMemory(CUNIT_FREE_HEAD, SetTo, 0);
    when([freeListEmpty()], () => {
      SetDeaths(BODY_EPD, SetTo, BODY_VALUE, 0);
    });
  }).toJson();

  const pyScript = `
from py import SetDeaths, SetMemory, SetTo, build_module, when
from py.capacity import CUNIT_FREE_HEAD, free_list_empty

def prog():
    SetMemory(CUNIT_FREE_HEAD, SetTo, 0)
    when([free_list_empty()], lambda: SetDeaths(${BODY_EPD}, SetTo, ${BODY_VALUE}, 0))

print(build_module(prog).to_json(), end="")
`;

  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});
