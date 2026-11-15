/**
 * FE1: one field-value-space comparison kernel for all three surfaces.
 *
 * Mirror of `py/tests/test_field_surface_kernel.py`.
 *
 * The kernel is asked in the field's **value space**: the bit patterns inside
 * a field's mask, read as field units (`raw >> shift`). Its largest member is
 * `(mask & 0xFFFFFFFF) >>> shift`, including for *straddling* fields where
 * that is smaller than the nominal width. From that one number the six
 * comparison decisions follow:
 *
 * - a constant outside the space can never be equal (`eq` folds to `Never`)
 *   and can never be unequal (`ne` folds to `Always`);
 * - an ordering threshold beyond the space folds by direction;
 * - an in-space constant is emitted un-masked, shifted into the field's byte
 *   lane;
 * - a middle `ne` constant defers to the surface's use-site marker.
 *
 * The expectation column is derived from that definition, not from the
 * kernel's implementation: `maxValue` is computed independently in
 * `expected` from the width/sub-offset geometry.
 *
 * The matrix covers widths `1/2/4`, byte sub-offsets `0..3` and the boundary
 * constants `0, 1, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFFFF` for
 * the kernel itself and for all three surfaces (`TriggerFieldRef`,
 * `MemFieldHandle`, `Var(explicit_mask=...)`).
 */

import assert from "node:assert/strict";
import test from "node:test";
import { isDeepStrictEqual } from "node:util";

import {
  Always,
  AtLeast,
  AtMost,
  Exactly,
  FieldValueSpace,
  MemView,
  Never,
  TriggerFieldRef,
  Var,
  VarImpl,
  buildModule,
  defineMemView,
} from "../index.ts";
import type { CondLike } from "../index.ts";
import type { FieldSpec } from "../view.ts";

const OPS = ["eq", "ne", "ge", "le", "gt", "lt"] as const;
type Op = (typeof OPS)[number];
const WIDTHS = [1, 2, 4] as const;
const SUBOFFSETS = [0, 1, 2, 3] as const;
const CONSTS = [0, 1, 0x7f, 0x80, 0xff, 0x100, 0xffff, 0x10000, 0xffffffff] as const;

type Plan = readonly unknown[];

function rawMask(width: 1 | 2 | 4): number {
  return width === 1 ? 0xff : width === 2 ? 0xffff : 0xffffffff;
}

function fieldMask(width: 1 | 2 | 4, suboffset: number): number {
  return (rawMask(width) << (suboffset * 8)) >>> 0;
}

function expected(op: Op, constant: number, maxValue: number): Plan {
  switch (op) {
    case "eq":
      return ((constant & ~maxValue) >>> 0) !== 0
        ? ["fold", "never"]
        : ["emit", Exactly, constant];
    case "ne":
      if (((constant & ~maxValue) >>> 0) !== 0) return ["fold", "always"];
      if (constant === 0) return ["emit", AtLeast, 1];
      if (constant === maxValue) return ["emit", AtMost, maxValue - 1];
      return ["defer", constant];
    case "ge":
      return constant > maxValue ? ["fold", "never"] : ["emit", AtLeast, constant];
    case "le":
      return constant > maxValue ? ["fold", "always"] : ["emit", AtMost, constant];
    case "gt":
      return constant >= maxValue ? ["fold", "never"] : ["emit", AtLeast, constant + 1];
    case "lt":
      if (constant === 0) return ["fold", "never"];
      return constant > maxValue ? ["fold", "always"] : ["emit", AtMost, constant - 1];
  }
}

function planOf(result: CondLike, shift: number, isVar = false): Plan {
  if (
    typeof result === "object" &&
    result !== null &&
    "materialize" in (result as object)
  ) {
    return ["defer", (result as { neConst?: number }).neConst];
  }
  if (isDeepStrictEqual(result, Always())) return ["fold", "always"];
  if (isDeepStrictEqual(result, Never())) return ["fold", "never"];
  const cond = result as unknown as { comparison: number; amount: number };
  return [
    "emit",
    cond.comparison,
    (isVar ? cond.amount : cond.amount >>> shift) >>> 0,
  ];
}

test("FieldValueSpace matrix decisions", () => {
  let rows = 0;
  for (const width of WIDTHS) {
    for (const suboffset of SUBOFFSETS) {
      const shift = suboffset * 8;
      const mask = fieldMask(width, suboffset);
      const space = new FieldValueSpace(mask, shift, width);
      const maxValue = mask >>> shift;
      for (const op of OPS) {
        for (const constant of CONSTS) {
          rows += 1;
          const label = `${width}/${suboffset} ${op}(${constant.toString(16)})`;
          assert.equal(space.maxValue, maxValue, label);
          const plan = space.compare(op, constant);
          let got: Plan;
          if (plan.fold === "defer") {
            got = ["defer", constant];
          } else if (plan.fold !== undefined) {
            got = ["fold", plan.fold];
          } else {
            got = ["emit", plan.comparison, (plan.value as number) >>> shift];
          }
          assert.deepEqual(got, expected(op, constant, maxValue), label);
        }
      }
    }
  }
  assert.equal(rows, 648);
});

test("three surfaces share the kernel", () => {
  let rows = 0;
  buildModule(() => {
    for (const width of WIDTHS) {
      for (const suboffset of SUBOFFSETS) {
        const shift = suboffset * 8;
        const mask = fieldMask(width, suboffset);
        const maxValue = mask >>> shift;
        const schema = defineMemView(`Kernel${width}_${suboffset}`, 8, {
          f: { offset: suboffset, size: width, mask },
        } as Record<string, FieldSpec>);
        const surfaces = [
          new TriggerFieldRef(-1, suboffset, width, 0, 5, mask),
          MemView(schema, 3000).f,
          Var(0, undefined, maxValue),
        ];
        for (const op of OPS) {
          for (const constant of CONSTS) {
            const want = expected(op, constant, maxValue);
            for (const surface of surfaces) {
              rows += 1;
              assert.deepEqual(
                planOf(
                  (surface as unknown as Record<string, (rhs: number) => CondLike>)[
                    op
                  ](constant),
                  shift,
                  surface instanceof VarImpl,
                ),
                want,
                `${surface.constructor.name} ${width}/${suboffset} ${op}(${constant.toString(16)})`,
              );
            }
          }
        }
      }
    }
  });
  assert.equal(rows, 3 * 648);
});
