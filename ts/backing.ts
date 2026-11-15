/**
 * Backing module for TriggerPage-backed containers.
 *
 * This module is the single home for the indexing arithmetic that FixedArray
 * and Queue previously re-derived per container: carry-free page/slot
 * EPD weights, EPD decomposition, element masks, and the runtime
 * PageSlotEPD address computation. Containers import all of it from
 * here and nowhere else.
 *
 * Scope is indexing arithmetic only. Allocation (page-slot assignment)
 * stays per container, and the builder hooks in `trigger.ts` are
 * untouched. Page-boundary alignment helpers live in the var module, not
 * here, because alignment is allocation, not indexing arithmetic. `pageSlotEpdWeight` itself is defined in `trigger.ts`
 * (it is Trigger layout, not container arithmetic) and is re-exported
 * here so containers have one import source; re-defining it here would
 * create a trigger/backing import cycle.
 *
 * STR-backed FixedString is not a client of this module: its dynamic byte
 * path uses STR layout, not page/slot decomposition, and stays outside
 * this seam.
 */

import { AtLeast, DeathsX, SetTo } from "./hir.ts";
import { Trigger, type HirCond, pageSlotEpdWeight } from "./trigger.ts";
import { Var, type VarImpl, emitVarTransfer } from "./var.ts";
import { EudslError } from "./view.ts";

export { pageSlotEpdWeight };

/** Element width mask by element type name. */
export const ELEM_MASK: Record<string, number> = {
  byte: 0xff,
  word: 0xffff,
  dword: 0xffffffff,
  var: 0xffffffff,
};

/** Constant carry-free page/slot EPD offset for a build-time index. */
export function pageSlotEpd(base: number, index: number): number {
  if (!Number.isInteger(index) || index < 0) {
    throw new EudslError("EUDSL-BOUNDS", `FixedArray index must be a non-negative integer, got ${index}`);
  }
  let epd = base >>> 0;
  for (let b = 0; b < 32; b++) {
    if (((index >>> b) & 1) !== 0) {
      epd = (epd + pageSlotEpdWeight(b)) >>> 0;
    }
  }
  return epd;
}

/** Bit-test condition for `(value & (1 << b)) != 0`. */
export function pageSlotBitCond(value: VarImpl, bit: number): HirCond {
  const mask = (1 << bit) >>> 0;
  return DeathsX(value.value, AtLeast, mask, 0, mask);
}

/**
 * Compute `base + PageSlotEPD(0, index)` into a `Var`.
 *
 * For a constant index this folds to a single `Var(base + offset)` (`0T`).
 * For a variable index this emits one trigger per active index bit
 * (`ceil(log2(N))` for an `N`-element array).
 */
export function PageSlotEPD(
  base: number | VarImpl,
  index: number | VarImpl,
  bits = 32,
): VarImpl {
  const baseVal = typeof base === "number" ? base >>> 0 : 0;
  if (typeof index === "number") {
    return Var(pageSlotEpd(baseVal, index));
  }
  if (bits < 1 || bits > 32) {
    throw new EudslError("EUDSL-BOUNDS", `PageSlotEPD bits must be in 1..32, got ${bits}`);
  }
  const out = typeof base === "number" ? Var(baseVal) : Var(0);
  if (typeof base !== "number") {
    emitVarTransfer(out.id, base.id, SetTo);
  }
  for (let b = 0; b < bits; b++) {
    const weight = pageSlotEpdWeight(b);
    new Trigger({ conds: [pageSlotBitCond(index, b)], acts: [out.act_add(weight)] });
  }
  return out;
}
