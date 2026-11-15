/**
 * `epdOfDb`: materialize a payload blob's runtime EPD into a `Var`.
 *
 * A `Db` payload address (and therefore its EPD) is only known at assembly
 * time, so the helper emits the same shape `ts/blockio.ts` uses privately:
 * one zero-initialized `Var`, one preserved zero-condition trigger, and one
 * mode-1 relocation into the fresh `Var`'s value field (ADR 0029).
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Db,
  DbRef,
  EudslError,
  VarImpl,
  buildModule,
  epdOfDb,
  epd_of_db,
} from "../index.ts";

const ACT_DEST_OFFSET = 16;
const ACT_VALUE_OFFSET = 20;

function moduleOf(body: () => void): Record<string, any> {
  return JSON.parse(buildModule(body).toJson()).module;
}

function triggersOf(mod: Record<string, any>): Record<string, any>[] {
  return (mod.stmts as Record<string, any>[]).filter(
    (stmt) => stmt.kind === "trigger",
  );
}

test("epdOfDb emits one preserved zero-condition trigger", () => {
  const mod = moduleOf(() => {
    epdOfDb(new Db(8));
  });

  assert.deepEqual(mod.vars, [{ id: 0, init: 0 }]);
  const triggers = triggersOf(mod);
  assert.equal(triggers.length, 1);

  const trig = triggers[0]!;
  assert.equal(trig.preserved, true);
  assert.deepEqual(trig.conds, []);
  assert.equal(trig.acts.length, 1);
});

test("epdOfDb writes a mode-1 blob reloc into the value field", () => {
  const mod = moduleOf(() => {
    epdOfDb(new Db(8));
  });

  const act = triggersOf(mod)[0]!.acts[0];
  assert.deepEqual(act.relocs, [
    {
      field_offset: ACT_DEST_OFFSET,
      target_var: 0,
      target_offset: ACT_VALUE_OFFSET,
      mode: 1,
    },
    {
      field_offset: ACT_VALUE_OFFSET,
      target_blob: 0,
      target_offset: 0,
      mode: 1,
    },
  ]);
});

test("epdOfDb carries a DbRef offset through unchanged", () => {
  const mod = moduleOf(() => {
    epdOfDb(new Db(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])).at(4));
  });

  const reloc = triggersOf(mod)[0]!.acts[0].relocs[1];
  assert.equal(reloc.target_blob, 0);
  assert.equal(reloc.target_offset, 4);
  assert.equal(reloc.mode, 1);
});

test("epdOfDb returns a VarImpl", () => {
  let out: VarImpl | undefined;
  buildModule(() => {
    out = epdOfDb(new Db(8));
  });
  assert.ok(out instanceof VarImpl);
  assert.equal(out.id, 0);
});

test("non-Db operands are EUDSL-TYPE", () => {
  for (const bad of ["x", 0, null, 1.5]) {
    assert.throws(
      () => epdOfDb(bad as unknown as DbRef),
      (err: unknown) =>
        err instanceof EudslError &&
        err.code === "EUDSL-TYPE" &&
        err.message.includes("epd_of_db(): expected Db or DbRef"),
      `expected EUDSL-TYPE for ${String(bad)}`,
    );
  }
});

test("outside buildModule is EUDSL-SCOPE", () => {
  assert.throws(
    () => epdOfDb(new DbRef(0)),
    (err: unknown) => err instanceof EudslError && err.code === "EUDSL-SCOPE",
  );
});

test("epd_of_db is the same helper as epdOfDb", () => {
  assert.equal(epd_of_db, epdOfDb);
});
