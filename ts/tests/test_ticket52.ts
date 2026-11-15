/**
 * Ticket #52: equality dunders reject the rejected scalar domain.
 *
 * TS has no operator overloading, so there are no `__eq__` dunders to change.
 * This sibling is an equivalent, not a row-by-row mirror: it pins that the
 * existing `Var.eq/ne`, `Expr.eq/ne`, and `TriggerFieldRef.eq/ne` surfaces
 * already reject the rejected scalar domain (`1.5`, `"abc"`, `true`), that the
 * condition-record residuals are unchanged, and that the shared eager
 * condition-list guard rejects a boolean item.
 *
 * One documented divergence is pinned rather than papered over: `u.hp == 1.5`
 * is a plain JS value comparison (`false`, no throw), because `MemView` fields
 * are `any` and JS has no equality hook (ADR 0019 §6).
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  CUnit,
  EudslError,
  TriggerFieldRef,
  Var,
  buildModule,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
} from "../index.ts";
import { REPO_ROOT } from "./helpers.ts";

const REJECTED: Array<[string, unknown]> = [
  ["float", 1.5],
  ["str", "abc"],
  ["bool", true],
];

const OPS = ["eq", "ne"] as const;
type Op = (typeof OPS)[number];

/** The `Var`-operand shape message, matching the py sibling's two rows. */
function varOperandMessage(value: unknown): string {
  return typeof value === "number"
    ? "must be a plain integer"
    : "integer constant, a Var, or an Expr";
}

function expectEudslType(
  fn: () => unknown,
  label: string,
  expected: string,
): void {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  assert.ok(
    caught instanceof EudslError,
    `${label}: expected EUDSL-TYPE, got ${String(caught)}`,
  );
  assert.equal(caught.code, "EUDSL-TYPE", `${label}: ${caught.message}`);
  assert.ok(
    caught.message.includes(expected),
    `${label}: message must include ${expected}, got ${caught.message}`,
  );
}

function collectByKind(node: unknown, kind: string, out: any[] = []): any[] {
  if (Array.isArray(node)) {
    for (const x of node) collectByKind(x, kind, out);
  } else if (node && typeof node === "object") {
    const rec = node as Record<string, unknown>;
    if (rec.kind === kind) out.push(rec);
    for (const value of Object.values(rec)) collectByKind(value, kind, out);
  }
  return out;
}

test("test_var_eq_ne_reject_float_str_bool", () => {
  buildModule(() => {
    const v = Var(0);
    for (const [label, value] of REJECTED) {
      for (const op of OPS) {
        expectEudslType(
          () => (v as any)[op](value),
          `v.${op}(${label})`,
          varOperandMessage(value),
        );
      }
    }
  });
});

test("test_expr_eq_ne_reject_float_str_bool", () => {
  buildModule(() => {
    const expr = Var(0).add(1);
    for (const [label, value] of REJECTED) {
      for (const op of OPS) {
        expectEudslType(
          () => (expr as any)[op](value),
          `expr.${op}(${label})`,
          varOperandMessage(value),
        );
      }
    }
  });
});

test("test_trigger_field_ref_eq_ne_reject_str", () => {
  for (const [label, value] of REJECTED) {
    for (const op of OPS) {
      expectEudslType(
        () => (new TriggerFieldRef(0, 0, 4) as any)[op](value),
        `TriggerFieldRef.${op}(${label})`,
        "field comparison must be a plain integer",
      );
    }
  }
});

test("test_cond_dict_residuals", () => {
  buildModule(() => {
    const cond = Var(0).eq(1) as any;
    assert.equal(cond == true, false, "cond == true stays false");
    assert.equal(cond == null, false, "cond == null stays false");
    assert.deepEqual({ ...cond }, cond, "the plain record API is unchanged");
  });
});

test("test_bare_cond_eq_true_still_drops", () => {
  const mod = buildModule(() => {
    const hit = Var(0);
    const cond = Var(0).eq(1) as any;
    when(cond == true, () => {
      hit.iadd(1);
    });
  });
  assert.equal(
    collectByKind(JSON.parse(mod.toJson()), "when").length,
    0,
    "the bare cond == true form still drops the block",
  );
});

const GUARD_MESSAGE = "condition list must not contain a boolean";

type CondControl = (conds: any, body: () => void) => void;

const COND_CONTROLS: Array<[string, CondControl]> = [
  ["when", when],
  ["once", once],
  ["until", until],
  ["while_loop", whileLoop],
  ["on_rising", onRising],
  ["on_falling", onFalling],
];

const BOOLEAN_ITEMS: Array<[string, () => any]> = [
  ["literal-true", () => true],
  ["literal-false", () => false],
  ["cond-eq-true", () => (Var(0).eq(1) as any) == true],
];

test("test_cond_list_boolean_is_eager_type_error", () => {
  buildModule(() => {
    for (const [label, control] of COND_CONTROLS) {
      for (const [itemLabel, makeItem] of BOOLEAN_ITEMS) {
        expectEudslType(
          () =>
            control([makeItem()], () => {
              // body must never run
            }),
          `${label}([${itemLabel}])`,
          GUARD_MESSAGE,
        );
      }
    }
  });
});

test("test_bare_boolean_while_loop_is_eager_type_error", () => {
  buildModule(() => {
    for (const value of [true, false]) {
      expectEudslType(
        () => whileLoop(value as any, () => {}),
        `whileLoop(${value})`,
        GUARD_MESSAGE,
      );
    }
  });
});

const PY_BARE_BOOLEAN_SCRIPT = `
from py import EudslError, build_module, on_rising, on_falling, while_loop

rows = [
    ("onRising", lambda: on_rising(False, lambda: None)),
    ("onFalling", lambda: on_falling(False, lambda: None)),
    ("whileLoopFalse", lambda: while_loop(False, lambda: None)),
    ("whileLoopTrue", lambda: while_loop(True, lambda: None)),
]
results = []
for label, call in rows:
    try:
        build_module(call)
        results.append(label + ":ok")
    except EudslError as error:
        results.append(label + ":" + error.code)
import sys
sys.stdout.write("\\n".join(results))
`;

test("test_bare_false_edge_and_while_forms_match_py", () => {
  const rows: Array<[string, () => void]> = [
    ["onRising", () => onRising(false as any, () => {})],
    ["onFalling", () => onFalling(false as any, () => {})],
    ["whileLoopFalse", () => whileLoop(false as any, () => {})],
    ["whileLoopTrue", () => whileLoop(true as any, () => {})],
  ];
  const tsCodes = rows.map(([label, call]) => {
    try {
      buildModule(call);
      return `${label}:ok`;
    } catch (error) {
      return `${label}:${error instanceof EudslError ? error.code : "non-eudsl"}`;
    }
  });
  const pyCodes = execFileSync("python3", ["-c", PY_BARE_BOOLEAN_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).split("\n");
  assert.deepEqual(tsCodes, pyCodes);
});

test("test_memview_field_equality_is_a_js_value_comparison", () => {
  // Decision pin, not parity: TS `MemView` fields are `any`, and JS has no
  // equality hook, so this is an ordinary value comparison outside the
  // operand domain (ADR 0019 §6).
  const u = CUnit(1000);
  assert.equal((u.hp as any) == 1.5, false);
});
