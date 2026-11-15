/**
 * Ticket #121: validate `VarStore` targets before queueing HIR.
 *
 * Mirror of `py/tests/test_ticket121.py`.
 *
 * A handler for a trigger field is a whole-dword write. A malformed
 * `{ triggerId, offset }` target used to be lowered as a mode-1 4-byte field
 * relocation even when `offset` was 14, silently writing
 * `unit`/`comparison`/`condtype`; numeric strings survived until late Rust
 * serde and floats were kept raw. `emitVarStore` now validates the target
 * eagerly at the call, before any HIR node is queued:
 *
 * - `addr` is a plain integer (the #111 alignment guard still applies);
 * - `triggerId` and `offset` are plain integers, and `offset % 4 === 0`.
 *
 * The failures are eager `EUDSL-TYPE` / `EUDSL-BOUNDS`; the type-error text is
 * frontend-specific, so the tests pin the code plus the stable operand
 * substring only (ADR 0019 §1). Legal offsets (including 0 and 24) and aligned
 * addresses keep the same HIR and static cost as before the change.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  EudslError,
  SetTo,
  Trigger,
  Var,
  buildModule,
  emitVarStore,
  type VarStoreTarget,
} from "../index.ts";
import { runStatic } from "./helpers.ts";

const TRIGGER_TARGET_STATIC = [4, 0, 67, 9_632];
const ADDR_TARGET_STATIC = [3, 0, 67, 7_224];

const ADDR_CONTROLS = [0x600000, 0x57f0b4, 0x6509b0];
const LEGAL_OFFSETS = [0, 4, 8, 16, 20, 24];

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return [cost.triggers, cost.conditions, cost.actions, cost.total_bytes];
}

function varStoreNode(
  module: ReturnType<typeof buildModule>,
): Record<string, unknown> {
  const hir = JSON.parse(module.toJson()) as {
    module: { stmts: Array<Record<string, unknown>> };
  };
  const node = hir.module.stmts.find((stmt) => stmt.kind === "varstore");
  assert.ok(node);
  return node;
}

function expectError(
  targetFor: (t: Trigger) => VarStoreTarget,
  code: string,
  substring: string,
): void {
  buildModule(() => {
    const t = new Trigger({ conds: [], acts: [] });
    const v = Var(3);
    assert.throws(
      () => {
        emitVarStore(v.id, targetFor(t));
      },
      (e: unknown) => {
        assert.ok(e instanceof EudslError);
        assert.equal(e.code, code);
        assert.ok(e.message.includes(substring));
        return true;
      },
    );
  });
}

test("a float addr is rejected before queueing", () => {
  expectError(
    () => ({ addr: 600000.5 }),
    "EUDSL-TYPE",
    "VarStore address must be a plain integer",
  );
});

test("a numeric-string addr is rejected before queueing", () => {
  expectError(
    () => ({ addr: "6291456" } as unknown as VarStoreTarget),
    "EUDSL-TYPE",
    "VarStore address must be a plain integer",
  );
});

test("a float triggerId is rejected before queueing", () => {
  expectError(
    (t) => ({ triggerId: t.id + 0.5, offset: 0 }),
    "EUDSL-TYPE",
    "VarStore trigger_id must be a plain integer",
  );
});

test("a float offset is rejected before queueing", () => {
  expectError(
    (t) => ({ triggerId: t.id, offset: 20.7 }),
    "EUDSL-TYPE",
    "VarStore offset must be a plain integer",
  );
});

test("NaN offset is rejected before queueing", () => {
  expectError(
    (t) => ({ triggerId: t.id, offset: Number.NaN }),
    "EUDSL-TYPE",
    "VarStore offset must be a plain integer",
  );
});

test("Infinity offset is rejected before queueing", () => {
  expectError(
    (t) => ({ triggerId: t.id, offset: Number.POSITIVE_INFINITY }),
    "EUDSL-TYPE",
    "VarStore offset must be a plain integer",
  );
});

test("an explicit undefined offset is rejected, not defaulted", () => {
  expectError(
    (t) =>
      ({ triggerId: t.id, offset: undefined } as unknown) as VarStoreTarget,
    "EUDSL-TYPE",
    "VarStore offset must be a plain integer",
  );
});

test("an explicit null offset is rejected, not defaulted", () => {
  expectError(
    (t) => ({ triggerId: t.id, offset: null } as unknown) as VarStoreTarget,
    "EUDSL-TYPE",
    "VarStore offset must be a plain integer",
  );
});

test("an unaligned offset is rejected with bounds", () => {
  buildModule(() => {
    const t = new Trigger({ conds: [], acts: [] });
    const v = Var(3);
    assert.throws(
      () => {
        emitVarStore(v.id, { triggerId: t.id, offset: 14 });
      },
      (e: unknown) => {
        assert.ok(e instanceof EudslError);
        assert.equal(e.code, "EUDSL-BOUNDS");
        assert.equal(
          e.message,
          "EUDSL-BOUNDS: VarStore offset 14 is not a multiple of 4",
        );
        return true;
      },
    );
  });
});

test("legal offsets are stored exactly", () => {
  for (const offset of LEGAL_OFFSETS) {
    const triggerIds: number[] = [];
    const module = buildModule(() => {
      const t = new Trigger({ conds: [], acts: [] });
      triggerIds.push(t.id);
      const v = Var(3);
      emitVarStore(v.id, { triggerId: t.id, offset });
    });
    const node = varStoreNode(module);
    assert.equal(node.dst_trigger, triggerIds[0]);
    assert.equal(node.dst_offset, offset);
    assert.deepEqual(staticCost(module.toJson()), TRIGGER_TARGET_STATIC);
  }
});

test("an omitted offset defaults to zero", () => {
  const triggerIds: number[] = [];
  const module = buildModule(() => {
    const t = new Trigger({ conds: [], acts: [] });
    triggerIds.push(t.id);
    const v = Var(3);
    emitVarStore(v.id, { triggerId: t.id } as unknown as VarStoreTarget);
  });
  const node = varStoreNode(module);
  assert.equal(node.dst_trigger, triggerIds[0]);
  assert.equal(node.dst_offset, 0);
  assert.deepEqual(staticCost(module.toJson()), TRIGGER_TARGET_STATIC);
});

test("aligned addresses keep HIR and static cost", () => {
  for (const addr of ADDR_CONTROLS) {
    const module = buildModule(() => {
      const v = Var(3);
      emitVarStore(v.id, { addr });
    });
    const node = varStoreNode(module);
    assert.equal(node.dst_addr, addr);
    assert.deepEqual(staticCost(module.toJson()), ADDR_TARGET_STATIC);
  }
});
