/**
 * FE3: typed relocation descriptors for `emitVarStore`.
 *
 * Mirror of `py/tests/test_reloc_descriptors.py`.
 *
 * `AddrReloc(addr)` and `TriggerFieldReloc(triggerId, offset = 0)` describe
 * the two relocation shapes the HIR wire knows as a discriminated union. The
 * descriptors normalise to the legacy `{ addr }` / `{ triggerId, offset }`
 * objects at the `emitVarStore` boundary, so HIR JSON and static cost stay
 * byte-identical. A mixed shape is unrepresentable at the type level; a
 * foreign target is rejected eagerly with `EUDSL-TYPE`. Numeric validation
 * keeps the same messages and timing as the legacy form.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import {
  AddrReloc,
  EudslError,
  SetTo,
  Trigger,
  TriggerFieldReloc,
  Var,
  buildModule,
  emitVarStore,
  type VarStoreReloc,
  type VarStoreTarget,
} from "../index.ts";

const ADDR = 0x6509b0;
const UNALIGNED_ADDR = 0x6509b2;
const TRIGGER_OFFSET = 20;
const UNALIGNED_OFFSET = 14;

type AnyTarget = VarStoreReloc | VarStoreTarget;

function moduleJson(targetFor: (trigger: Trigger) => AnyTarget): string {
  return buildModule(() => {
    const trigger = new Trigger({ conds: [], acts: [] });
    const value = Var(3);
    emitVarStore(value.id, targetFor(trigger), SetTo);
  }).toJson();
}

function varStoreNode(hirJson: string): Record<string, unknown> {
  const hir = JSON.parse(hirJson) as {
    module: { stmts: Array<Record<string, unknown>> };
  };
  const node = hir.module.stmts.find((stmt) => stmt.kind === "varstore");
  assert.ok(node);
  return node;
}

function errorOf(targetFor: (trigger: Trigger) => AnyTarget): {
  code: string;
  message: string;
} {
  let caught: unknown;
  buildModule(() => {
    const trigger = new Trigger({ conds: [], acts: [] });
    const value = Var(3);
    try {
      emitVarStore(value.id, targetFor(trigger), SetTo);
    } catch (error) {
      caught = error;
    }
  });
  assert.ok(caught instanceof EudslError);
  return { code: caught.code, message: caught.message };
}

const _mixedAddrShape: VarStoreReloc = {
  kind: "addr",
  addr: ADDR,
  // @ts-expect-error - an address descriptor cannot carry a trigger field
  triggerId: 0,
};

const _mixedTriggerShape: VarStoreReloc = {
  kind: "trigger",
  triggerId: 0,
  // @ts-expect-error - a trigger-field descriptor cannot carry a byte address
  addr: ADDR,
};

test("AddrReloc matches the legacy literal HIR", () => {
  const legacy = moduleJson(() => ({ addr: ADDR }));
  const descriptor = moduleJson(() => AddrReloc(ADDR));

  assert.equal(descriptor, legacy);
  const node = varStoreNode(descriptor);
  assert.equal(node.dst_addr, ADDR);
  assert.equal("dst_trigger" in node, false);
  assert.equal("dst_offset" in node, false);
});

test("TriggerFieldReloc matches the legacy literal HIR", () => {
  const legacy = moduleJson((trigger) => ({
    triggerId: trigger.id,
    offset: TRIGGER_OFFSET,
  }));
  const descriptor = moduleJson((trigger) =>
    TriggerFieldReloc(trigger.id, TRIGGER_OFFSET),
  );

  assert.equal(descriptor, legacy);
  const node = varStoreNode(descriptor);
  assert.equal(node.dst_trigger, 0);
  assert.equal(node.dst_offset, TRIGGER_OFFSET);
  assert.equal("dst_addr" in node, false);
});

test("TriggerFieldReloc defaults the offset to zero", () => {
  const legacy = moduleJson((trigger) => ({
    triggerId: trigger.id,
    offset: 0,
  }));
  const descriptor = moduleJson((trigger) => TriggerFieldReloc(trigger.id));

  assert.equal(descriptor, legacy);
  assert.equal(varStoreNode(descriptor).dst_offset, 0);
});

test("an unaligned AddrReloc matches the legacy error", () => {
  const legacy = errorOf(() => ({ addr: UNALIGNED_ADDR }));
  const descriptor = errorOf(() => AddrReloc(UNALIGNED_ADDR));

  assert.deepEqual(descriptor, legacy);
  assert.equal(descriptor.code, "EUDSL-BOUNDS");
  assert.equal(
    descriptor.message,
    "EUDSL-BOUNDS: VarStore address 0x6509b2 is not 4-byte aligned",
  );
});

test("a non-integer AddrReloc matches the legacy error", () => {
  const legacy = errorOf(() => ({ addr: "6291456" } as unknown as VarStoreTarget));
  const descriptor = errorOf(() =>
    AddrReloc("6291456" as unknown as number),
  );

  assert.deepEqual(descriptor, legacy);
  assert.equal(descriptor.code, "EUDSL-TYPE");
});

test("an unaligned TriggerFieldReloc matches the legacy error", () => {
  const legacy = errorOf((trigger) => ({
    triggerId: trigger.id,
    offset: UNALIGNED_OFFSET,
  }));
  const descriptor = errorOf((trigger) =>
    TriggerFieldReloc(trigger.id, UNALIGNED_OFFSET),
  );

  assert.deepEqual(descriptor, legacy);
  assert.equal(descriptor.code, "EUDSL-BOUNDS");
  assert.equal(
    descriptor.message,
    "EUDSL-BOUNDS: VarStore offset 14 is not a multiple of 4",
  );
});

test("a non-integer TriggerFieldReloc matches the legacy error", () => {
  const legacy = errorOf((trigger) => ({
    triggerId: trigger.id + 0.5,
    offset: 0,
  }));
  const descriptor = errorOf((trigger) =>
    TriggerFieldReloc(trigger.id + 0.5),
  );

  assert.deepEqual(descriptor, legacy);
  assert.equal(descriptor.code, "EUDSL-TYPE");
});

test("an empty legacy target is rejected eagerly", () => {
  const error = errorOf(() => ({}) as unknown as VarStoreTarget);

  assert.equal(error.code, "EUDSL-TYPE");
  assert.ok(error.message.includes("VarStore trigger_id"));
});

test("a hand-cast malformed descriptor is rejected eagerly", () => {
  const error = errorOf(
    () => ({ kind: "trigger" }) as unknown as VarStoreReloc,
  );

  assert.equal(error.code, "EUDSL-TYPE");
  assert.ok(error.message.includes("VarStore trigger_id"));
});

test("a legacy plain object is never reinterpreted as a descriptor", () => {
  // Mirrors `py/tests/test_reloc_descriptors.py`: a mapping stays on the
  // legacy path even when it carries an unrelated `kind` key. Only the
  // factory-built descriptors are normalised.
  const legacy = moduleJson(() => ({ addr: ADDR }));
  const shaped = moduleJson(
    () => ({ kind: "trigger", addr: ADDR }) as unknown as AnyTarget,
  );

  assert.equal(shaped, legacy);
});

test("all in-tree production modules and examples use typed descriptors", () => {
  const thisFile = fileURLToPath(import.meta.url);
  const tsDir = path.resolve(path.dirname(thisFile), "..");
  const repoRoot = path.resolve(tsDir, "..");
  const files = [
    ...fs
      .readdirSync(tsDir)
      .filter((name) => name.endsWith(".ts"))
      .map((name) => path.join(tsDir, name)),
    ...fs
      .readdirSync(path.join(repoRoot, "examples", "ts"))
      .filter((name) => name.endsWith(".ts"))
      .map((name) => path.join(repoRoot, "examples", "ts", name)),
  ].sort();

  const rawCalls: string[] = [];
  for (const filePath of files) {
    const sourceText = fs.readFileSync(filePath, "utf-8");
    const sourceFile = ts.createSourceFile(
      filePath,
      sourceText,
      ts.ScriptTarget.Latest,
      true,
    );
    const visit = (node: ts.Node): void => {
      if (ts.isCallExpression(node)) {
        const expr = node.expression;
        const name = ts.isIdentifier(expr)
          ? expr.text
          : ts.isPropertyAccessExpression(expr)
            ? expr.name.text
            : null;
        if (name === "emitVarStore" && node.arguments.length >= 2) {
          if (ts.isObjectLiteralExpression(node.arguments[1])) {
            const { line } = sourceFile.getLineAndCharacterOfPosition(
              node.getStart(sourceFile),
            );
            rawCalls.push(`${path.relative(repoRoot, filePath)}:${line + 1}`);
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  assert.deepEqual(rawCalls, []);
});
