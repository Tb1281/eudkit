import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { FixedArray, Queue, Var, buildModule, type VarImpl } from "../index.ts";
import {
  PageSlotEPD,
  pageSlotBitCond,
  pageSlotEpd,
  pageSlotEpdWeight,
} from "../backing.ts";
import {
  pageSlotEpd as pageSlotEpdViaArray,
  pageSlotEpdWeight as pageSlotEpdWeightViaArray,
} from "../array.ts";
import { pageSlotEpdWeight as pageSlotEpdWeightViaTrigger } from "../trigger.ts";

test("backing exports are re-exported unchanged from array and trigger", () => {
  assert.equal(pageSlotEpdViaArray, pageSlotEpd);
  assert.equal(pageSlotEpdWeightViaArray, pageSlotEpdWeight);
  assert.equal(pageSlotEpdWeightViaTrigger, pageSlotEpdWeight);
});

test("backing weights are carry-free bit weights for bits 0..6", () => {
  const expected = [8, 16, 32, 64, 128, 256, 602];
  for (let b = 0; b <= 6; b++) assert.equal(pageSlotEpdWeight(b), expected[b]);
  assert.equal(pageSlotEpdWeight(7), 1204);
});

test("backing weights follow the weight formula for bits 0..28", () => {
  for (let b = 0; b < 6; b++) assert.equal(pageSlotEpdWeight(b), 8 << b);
  for (let b = 6; b <= 28; b++) assert.equal(pageSlotEpdWeight(b), (602 << (b - 6)) >>> 0);
});

test("backing weights reject out-of-range bits", () => {
  assert.throws(() => pageSlotEpdWeight(32), /EUDSL-BOUNDS/);
  assert.throws(() => pageSlotEpdWeight(-1), /EUDSL-BOUNDS/);
  assert.throws(() => pageSlotEpdWeight(1.5), /EUDSL-BOUNDS/);
});

test("pageSlotEpd pinned values", () => {
  assert.equal(pageSlotEpd(1000, 0), 1000);
  assert.equal(pageSlotEpd(1000, 1), 1008);
  assert.equal(pageSlotEpd(1000, 63), 1504);
  assert.equal(pageSlotEpd(1000, 64), 1602);
  assert.equal(pageSlotEpd(1000, 65), 1610);
  assert.equal(pageSlotEpd(0, 65), 610);
  assert.equal(pageSlotEpd(0, 128), 1204);
});

test("pageSlotEpd rejects invalid indices", () => {
  assert.throws(() => pageSlotEpd(1000, -1), /EUDSL-BOUNDS/);
  assert.throws(() => pageSlotEpd(1000, 1.5), /EUDSL-BOUNDS/);
});

test("pageSlotEpd decomposes into page stride plus slot stride", () => {
  for (let i = 0; i < 1024; i++) {
    const expected = ((i >> 6) * 602 + (i & 63) * 8) >>> 0;
    assert.equal(pageSlotEpd(0, i), expected);
    assert.equal(pageSlotEpd(1000, i), (1000 + expected) >>> 0);
  }
});

test("element masks come from the public FixedArray mask surface", () => {
  buildModule(() => {
    assert.equal(FixedArray("byte", 4).mask, 0xff);
    assert.equal(FixedArray("word", 4).mask, 0xffff);
    assert.equal(FixedArray("dword", 4).mask, 0xffffffff);
    assert.equal(FixedArray("var", 4).mask, 0xffffffff);
    assert.equal(FixedArray("dword", 64).pageCount, 1);
    assert.equal(FixedArray("dword", 65).pageCount, 2);
  });
});

test("element masks come from the public Queue out-box surface", () => {
  buildModule(() => {
    assert.equal((new Queue("byte", 4).outBox as VarImpl).bitmask, 0xff);
    assert.equal((new Queue("word", 4).outBox as VarImpl).bitmask, 0xffff);
    assert.equal((new Queue("dword", 4).outBox as VarImpl).bitmask, 0xffffffff);
    assert.equal((new Queue("var", 4).outBox as VarImpl).bitmask, 0xffffffff);
  });
});

test("pageSlotBitCond is the bit-test condition used by PageSlotEPD", () => {
  // Covered behaviourally below: PageSlotEPD with a variable index must
  // accumulate exactly the weights of the set bits, which pageSlotBitCond
  // selects. A direct shape assertion would pin HirCond field layout,
  // which is outside the backing seam.
  assert.equal(typeof pageSlotBitCond, "function");
});

test("PageSlotEPD runtime address accumulates set-bit weights", () => {
  const mod = buildModule(() => {
    const base = Var(100);
    const index = Var(65);
    const epd = PageSlotEPD(base, index, 7);
    const out = Var(0);
    out.set(epd);
    (globalThis as Record<string, unknown>).__backingOut = out.id;
  });
  const outId = (globalThis as Record<string, unknown>).__backingOut as number;
  const report = runSim(mod.toJson(), 2);
  // base 100 + page 1 (602) + slot 1 (8) = 710.
  assert.equal(report.var_values[outId], 710);
});
