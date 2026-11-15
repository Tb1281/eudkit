import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  buildModule,
  onWarning,
  lazyVar,
  lazyFixedString,
  lazyFixedArray,
  lazyQueue,
  lazyDeque,
  lazyStack,
  lazyUnitGroup,
  lazyStruct,
  defineStruct,
  arrayByte,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SCOPE_CODE = "EUDSL-WARN-SCOPE";
const SCOPE_MESSAGE =
  "EUDSL-WARN-SCOPE: lazy* declared inside buildModule; use eager Var for in-module declarations";

function captureWarnings(action: () => void): Array<{ code: string; message: string }> {
  const captured: Array<{ code: string; message: string }> = [];
  const unsubscribe = onWarning((w) => captured.push(w));
  try {
    action();
  } finally {
    unsubscribe();
  }
  return captured;
}

const Point = defineStruct("Point", {
  x: 4,
  y: 4,
});

const factories: Array<{ name: string; invoke: () => unknown }> = [
  { name: "lazyVar", invoke: () => lazyVar(0) },
  { name: "lazyFixedString", invoke: () => lazyFixedString(32) },
  { name: "lazyFixedArray", invoke: () => lazyFixedArray(arrayByte, 8) },
  { name: "lazyQueue", invoke: () => lazyQueue(arrayByte, 8) },
  { name: "lazyDeque", invoke: () => lazyDeque(arrayByte, 8) },
  { name: "lazyStack", invoke: () => lazyStack(8) },
  { name: "lazyUnitGroup", invoke: () => lazyUnitGroup(8) },
  { name: "lazyStruct", invoke: () => lazyStruct(Point) },
];

test("Ticket 95: all lazy* constructors emit EUDSL-WARN-SCOPE when called inside buildModule", () => {
  for (const { name, invoke } of factories) {
    const warnings = captureWarnings(() => {
      buildModule(() => {
        invoke();
      });
    });

    assert.equal(
      warnings.length,
      1,
      `Expected exactly 1 warning when ${name} is called inside buildModule, got ${warnings.length}`,
    );
    assert.equal(
      warnings[0].code,
      SCOPE_CODE,
      `Expected warning code EUDSL-WARN-SCOPE for ${name}, got ${warnings[0].code}`,
    );
    assert.equal(
      warnings[0].message,
      SCOPE_MESSAGE,
      `Warning message mismatch for ${name}: expected ${SCOPE_MESSAGE}, got ${warnings[0].message}`,
    );
  }
});

test("Ticket 95: all lazy* constructors emit NO warning when called outside buildModule", () => {
  for (const { name, invoke } of factories) {
    const warnings = captureWarnings(() => {
      invoke();
    });

    assert.equal(
      warnings.length,
      0,
      `Expected 0 warnings when ${name} is called outside buildModule, got ${warnings.length}`,
    );
  }
});

test("Ticket 95: resolving externally declared lazy handles emits no warning", () => {
  const variable = lazyVar(0);
  const array = lazyFixedArray(arrayByte, 4);

  const warnings = captureWarnings(() => {
    buildModule(() => {
      variable.set(1);
      array.get(0).set(1);
    });
  });

  assert.deepEqual(warnings, []);
});

test("Ticket 95: TS and Python frontends emit identical warnings for all lazy* factories", () => {
  const repoRoot = path.resolve(__dirname, "../..");
  const pyCode = `
import json
from py import (
    build_module, on_warning, defineStruct, arrayByte,
    lazyVar, lazyFixedString, lazyFixedArray, lazyQueue,
    lazyDeque, lazyStack, lazyUnitGroup, lazyStruct
)
Point = defineStruct("Point", {"x": 4, "y": 4})
factories = [
    ("lazyVar", lambda: lazyVar(0)),
    ("lazyFixedString", lambda: lazyFixedString(32)),
    ("lazyFixedArray", lambda: lazyFixedArray(arrayByte, 8)),
    ("lazyQueue", lambda: lazyQueue(arrayByte, 8)),
    ("lazyDeque", lambda: lazyDeque(arrayByte, 8)),
    ("lazyStack", lambda: lazyStack(8)),
    ("lazyUnitGroup", lambda: lazyUnitGroup(8)),
    ("lazyStruct", lambda: lazyStruct(Point)),
]
results = {}
for name, invoke in factories:
    captured = []
    unsub = on_warning(lambda code, msg: captured.append({"code": code, "message": msg}))
    try:
        build_module(lambda: invoke())
    finally:
        unsub()
    results[name] = captured
print(json.dumps(results))
`;
  const out = execFileSync(
    "python3",
    ["-c", pyCode],
    {
      cwd: repoRoot,
      env: { ...process.env, PYTHONPATH: "." },
      encoding: "utf-8",
    },
  );
  const pyResults = JSON.parse(out);

  for (const { name, invoke } of factories) {
    const tsCaptured = captureWarnings(() => {
      buildModule(() => {
        invoke();
      });
    });
    const pyCaptured = pyResults[name];
    assert.deepEqual(
      tsCaptured,
      pyCaptured,
      `Warning mismatch between TS and Python for ${name}`,
    );
  }
});
