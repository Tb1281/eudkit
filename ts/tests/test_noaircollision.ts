/**
 * The air-collision scrubber (issue #119): a port of euddraft's
 * `plugins/noAirCollision.py`. Mirror of `py/tests/test_noaircollision.py`.
 *
 * The source's `beforeTriggerExec` emits 115 triggers whose 7,311 actions all
 * `SetMemory(dummy, SetTo, 0)`, and its `onPluginStart` rewrites every action
 * destination to the collision table: `P, P + 1, ... P + 7,310`, where `P` is
 * the EPD read from `0x6D5CD8`. The trigger loop then zeroes the table.
 *
 * What is pinned here: the constants (115 triggers, `64 x 114 + 15` actions,
 * dest at `+86` EPDs, `+8` action stride, bounds `504`/`120`, cross-trigger
 * `+90`); the 115 condition-free zeroing triggers and their placeholder
 * actions; and the patch block (the `f_epdread_epd` read, the mode-1 seed at
 * `EPD(t[0]) + 86`, the stored table EPD, and the nested loop's `+8` / `+1` /
 * `+90` field bumps with the last-trigger bound of `120`).
 *
 * The runtime proof that the patched pass zeroes exactly `P .. P+7310` lives
 * in `crates/eudkit-lower/tests/noaircollision_parity.rs`, which also pins
 * Python/TypeScript HIR equality.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  ACTION_EPD_STRIDE,
  Add,
  CROSS_TRIGGER_EPD_STEP,
  FIRST_ACTION_DEST_EPD_OFFSET,
  FULL_TRIGGER_ACTIONS,
  FULL_TRIGGER_EPD_LIMIT,
  LAST_TRIGGER_ACTIONS,
  LAST_TRIGGER_EPD_LIMIT,
  REPH_EPD_ADDR,
  SetTo,
  TOTAL_ACTIONS,
  TRIGGER_COUNT,
  TRIGGER_EPD_STRIDE,
  buildModule,
  noAirCollision,
  no_air_collision,
} from "../index.ts";
import { CP_ADDR } from "../cp.ts";

const EPD_BASE = 0x58a364;
const ACT_BASE = 328;
const ACT_DEST_OFFSET = 16;
const ACT_VALUE_OFFSET = 20;
const ACT_STRIDE = 32;

function moduleJson(body: () => void): any {
  return JSON.parse(buildModule(body).toJson()).module;
}

test("noAirCollision constants match the source", () => {
  assert.equal(REPH_EPD_ADDR, 0x6d5cd8);
  assert.equal(TRIGGER_COUNT, 115);
  assert.equal(FULL_TRIGGER_ACTIONS, 64);
  assert.equal(LAST_TRIGGER_ACTIONS, 15);
  assert.equal(TOTAL_ACTIONS, 7_311);
  assert.equal(TOTAL_ACTIONS, 114 * 64 + 15);

  // Action 0's dest: 328 byte action base + 16 byte dest field.
  assert.equal(FIRST_ACTION_DEST_EPD_OFFSET, 86);
  assert.equal(FIRST_ACTION_DEST_EPD_OFFSET, (ACT_BASE + ACT_DEST_OFFSET) / 4);
  // Actions are 32 bytes = 8 EPDs apart (`k.AddNumber(8)`).
  assert.equal(ACTION_EPD_STRIDE, 8);
  assert.equal(ACTION_EPD_STRIDE, ACT_STRIDE / 4);
  // `k <= 63 * 8`, and the last trigger breaks at `15 * 8`.
  assert.equal(FULL_TRIGGER_EPD_LIMIT, 504);
  assert.equal(LAST_TRIGGER_EPD_LIMIT, 120);
  // Consecutive trigger records are 602 EPDs apart; the walk has covered
  // 64 * 8 of that, so it skips 90.
  assert.equal(TRIGGER_EPD_STRIDE, 602);
  assert.equal(CROSS_TRIGGER_EPD_STEP, 90);
  assert.equal(CROSS_TRIGGER_EPD_STEP, TRIGGER_EPD_STRIDE - 64 * 8);
});

test("the camelCase helper is the same function as the snake_case alias", () => {
  assert.equal(noAirCollision, no_air_collision);
});

test("the patch block is a once before 115 condition-free triggers", () => {
  const mod = moduleJson(noAirCollision);
  const once = mod.stmts[0];
  assert.equal(once.kind, "once");
  assert.deepEqual(once.conds, []);
  const zeros = mod.stmts.slice(1);
  assert.equal(zeros.length, TRIGGER_COUNT);
  assert.ok(zeros.every((stmt: any) => stmt.kind === "trigger"));
});

test("the action split is 64 x 114 plus 15 and every action is the placeholder", () => {
  const mod = moduleJson(noAirCollision);
  const zeros = mod.stmts.slice(1);
  const counts = zeros.map((stmt: any) => stmt.acts.length);
  assert.deepEqual(counts, [...Array.from({ length: 114 }, () => 64), 15]);
  assert.equal(counts.reduce((a: number, b: number) => a + b, 0), TOTAL_ACTIONS);

  for (const stmt of zeros) {
    assert.deepEqual(stmt.conds, []);
    assert.equal(stmt.preserved, true);
    for (const act of stmt.acts) {
      assert.equal(act.acttype, 45); // SetMemory wire form
      assert.equal(act.modifier, SetTo);
      // `SetMemory` addresses memory by EPD.
      assert.equal(act.dest, Math.floor((CP_ADDR - EPD_BASE) / 4) >>> 0);
      assert.equal(act.value, 0);
      assert.equal(act.relocs, undefined);
    }
  }
});

function seedTrigger(body: any[]): any {
  for (const stmt of body) {
    if (stmt.kind !== "trigger" || stmt.acts.length !== 1) continue;
    const relocs = stmt.acts[0].relocs ?? [];
    if (relocs.length === 2 && relocs[0].target_trigger !== undefined) {
      return stmt;
    }
  }
  throw new Error("no field-seeding trigger found");
}

test("the table pointer read is f_epdread_epd", () => {
  const mod = moduleJson(noAirCollision);
  const body = mod.stmts[0].body;

  // Accumulator reset, then one bit test per set bit of 0xFFFFFFFC, then the
  // single EPD base subtraction.
  const reset = body[0];
  assert.equal(reset.kind, "trigger");
  assert.equal(reset.acts[0].value, 0);
  assert.deepEqual(reset.acts[0].relocs, [
    {
      field_offset: ACT_DEST_OFFSET,
      target_var: 0,
      target_offset: ACT_VALUE_OFFSET,
      mode: 1,
    },
  ]);
  const bitTests = body.slice(1, 31);
  assert.equal(bitTests.length, 30); // bits 2..31 of 0xFFFFFFFC
  for (const trigger of bitTests) {
    assert.equal(trigger.conds.length, 1);
    assert.equal(trigger.conds[0].condtype, 15);
  }
  const subtract = body[31];
  assert.equal(subtract.kind, "act");
  assert.equal(subtract.modifier, Add);
  assert.equal(subtract.value, (-(EPD_BASE >>> 2)) >>> 0);
});

test("the seed points the scratch dest at EPD(t[0].act(0).dest)", () => {
  const mod = moduleJson(noAirCollision);
  const zeroIds = mod.stmts.slice(1).map((stmt: any) => stmt.id);
  const seed = seedTrigger(mod.stmts[0].body);
  assert.deepEqual(seed.conds, []);
  assert.equal(seed.preserved, true);

  const act = seed.acts[0];
  const relocs = act.relocs;
  assert.equal(act.dest, 0);
  assert.equal(act.value, 0);
  assert.equal(relocs[0].field_offset, ACT_DEST_OFFSET);
  assert.equal(relocs[0].mode, 1);
  assert.equal(relocs[0].target_offset, ACT_BASE + ACT_DEST_OFFSET);
  assert.equal(relocs[1].field_offset, ACT_VALUE_OFFSET);
  assert.equal(relocs[1].mode, 1);
  assert.equal(relocs[1].target_offset, ACT_BASE + ACT_DEST_OFFSET);
  assert.equal(relocs[1].target_trigger, zeroIds[0]);
});

test("the table EPD is stored into the scratch value field", () => {
  const mod = moduleJson(noAirCollision);
  const body = mod.stmts[0].body;
  const writerId = seedTrigger(body).acts[0].relocs[0].target_trigger;
  const stores = body.filter((stmt: any) => stmt.kind === "varstore");
  assert.equal(stores.length, 1);
  assert.equal(stores[0].src_var, 0); // the table EPD variable
  assert.equal(stores[0].dst_trigger, writerId);
  assert.equal(stores[0].dst_offset, ACT_BASE + ACT_VALUE_OFFSET);
  assert.equal(stores[0].modifier, SetTo);

  const outer = body.at(-1);
  const inner = outer.body[5];
  assert.equal(inner.kind, "while");
  const innerIds = inner.body
    .filter((stmt: any) => stmt.kind === "trigger")
    .map((stmt: any) => stmt.id);
  assert.ok(innerIds.includes(writerId));
});

test("the outer loop walks all 115 triggers", () => {
  const mod = moduleJson(noAirCollision);
  const outer = mod.stmts[0].body.at(-1);
  assert.equal(outer.kind, "while");
  assert.deepEqual(outer.conds, [
    {
      locid: 0,
      player: 0,
      amount: TRIGGER_COUNT - 1,
      unit: 0,
      comparison: 1,
      condtype: 15,
      restype: 0,
      flags: 0,
      eudx: 0,
      relocs: [
        {
          field_offset: 4,
          target_var: 1,
          target_offset: ACT_VALUE_OFFSET,
          mode: 1,
        },
      ],
    },
  ]);
});

test("the outer body sets the bounds then steps i and the dest", () => {
  const mod = moduleJson(noAirCollision);
  const body = mod.stmts[0].body.at(-1).body;
  assert.deepEqual(
    body.map((stmt: any) => stmt.kind),
    ["act", "when", "act", "vartransfer", "vartransfer", "while", "act", "act"],
  );
  // limit = 63 * 8
  assert.equal(body[0].value, FULL_TRIGGER_EPD_LIMIT);
  // when(i == 114): limit = 15 * 8
  assert.equal(body[1].conds[0].amount, TRIGGER_COUNT - 1);
  assert.equal(body[1].conds[0].comparison, 10); // Exactly
  assert.equal(body[1].body[0].value, LAST_TRIGGER_EPD_LIMIT);
  // k = 0
  assert.equal(body[2].value, 0);
  // i += 1
  assert.equal(body[6].value, 1);
  assert.equal(body[6].relocs[0].target_var, 1);
  // dest += 90 (602 - 64 * 8) for the next trigger's action 0.
  assert.equal(body[7].value, CROSS_TRIGGER_EPD_STEP);
  assert.equal(body[7].relocs[0].field_offset, ACT_DEST_OFFSET);
  assert.equal(body[7].relocs[0].target_offset, ACT_BASE + ACT_DEST_OFFSET);
});

test("the inner loop emits the write then advances dest and value", () => {
  const mod = moduleJson(noAirCollision);
  const inner = mod.stmts[0].body.at(-1).body[5];
  const writers = inner.body.filter((stmt: any) => stmt.kind === "trigger");
  assert.equal(writers.length, 1);
  const writer = writers[0];
  // The scratch action is `SetMemoryEPD(s[i] + k, SetTo, reph_epd)`.
  assert.equal(writer.acts.length, 1);
  assert.equal(writer.acts[0].acttype, 45);
  assert.equal(writer.acts[0].modifier, SetTo);
  assert.equal(writer.acts[0].value, 0);

  // k <= limit
  assert.equal(inner.conds[0].comparison, 10);
  // dest += 8, then value += 1, then k += 8.
  const bumps = inner.body.filter(
    (stmt: any) =>
      stmt.kind === "act" &&
      stmt.relocs !== undefined &&
      stmt.relocs[0].target_trigger !== undefined,
  );
  assert.deepEqual(
    bumps.map((bump: any) => [bump.value, bump.relocs[0].target_offset]),
    [
      [ACTION_EPD_STRIDE, ACT_BASE + ACT_DEST_OFFSET],
      [1, ACT_BASE + ACT_VALUE_OFFSET],
    ],
  );
  const k_bump = inner.body[3];
  assert.equal(k_bump.value, ACTION_EPD_STRIDE);
  assert.equal(k_bump.relocs[0].target_var, 2);
});
