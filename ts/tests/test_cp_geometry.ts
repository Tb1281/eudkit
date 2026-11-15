/**
 * CP page geometry is derived from the trigger layout (FE4).
 *
 * `SLOT_STRIDE_EPDS`, `PAGE_SLOTS`, and `PAGE_HOP_EPDS` are views of the
 * `Trigger (16C, 64A)` layout, not independent literals:
 *
 * - one action slot is 32 bytes = 8 EPDs (`ACT_STRIDE_EPDS`);
 * - one `TriggerPage` holds 64 slots (`ACT_COUNT`);
 * - a whole trigger is 602 EPDs, so hopping past the 64 slots leaves
 *   `602 - 64 * 8 = 90` EPDs (`TRIGGER_STRIDE_EPDS - ACT_COUNT *
 *   ACT_STRIDE_EPDS`).
 *
 * The Python sibling (`py/tests/test_cp_geometry.py`) pins the same numbers
 * and additionally reloads `cp` with patched `trigger` constants in a
 * subprocess. ESM bindings cannot be reassigned, so the TS side pins the same
 * derivation through the imported constants: a restated literal stops matching
 * as soon as the trigger layout moves.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { PAGE_HOP_EPDS, PAGE_SLOTS, SLOT_STRIDE_EPDS } from "../cp.ts";
import {
  ACT_COUNT,
  ACT_STRIDE_EPDS,
  TRIGGER_STRIDE_EPDS,
} from "../trigger.ts";

test("cp geometry pins the stock trigger layout", () => {
  assert.equal(SLOT_STRIDE_EPDS, 8);
  assert.equal(PAGE_SLOTS, 64);
  assert.equal(PAGE_HOP_EPDS, 90);
});

test("cp geometry is expressed over the trigger constants", () => {
  assert.equal(SLOT_STRIDE_EPDS, ACT_STRIDE_EPDS);
  assert.equal(PAGE_SLOTS, ACT_COUNT);
  assert.equal(
    PAGE_HOP_EPDS,
    TRIGGER_STRIDE_EPDS - ACT_COUNT * ACT_STRIDE_EPDS,
  );
  // The page hop is "one trigger page minus the slots themselves", expressed
  // through cp's own public names.
  assert.equal(
    PAGE_HOP_EPDS,
    TRIGGER_STRIDE_EPDS - PAGE_SLOTS * SLOT_STRIDE_EPDS,
  );
});
