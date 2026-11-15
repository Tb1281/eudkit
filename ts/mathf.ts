/**
 * Runtime integer math helpers.
 *
 * `isqrt` is a fixed-point integer square root: a digit-by-digit (restoring)
 * square root of `value` scaled by `2 ** fixedBits`. Each of the
 * `ceil(valueBits / 2) + fixedBits` steps shifts two bits into the remainder,
 * doubles the running root, and subtracts `2 * root + 1` when it fits. The
 * runtime kernel carries `u = 2 * root` beside `root` so the trial `u + 1`
 * comes from one doubling instead of a rebuild from `root` each step. The
 * cost is therefore fixed by the declared bit widths instead of the runtime
 * value, unlike an unrolled binary-search tree over every possible result.
 */

import {
  Add,
  AtLeast,
  DeathsX,
  SetDeathsX,
} from "./hir.ts";
import { when } from "./temporal.ts";
import { Trigger } from "./trigger.ts";
import {
  saturatingSubInto,
  toVar,
  Var,
  VarImpl,
  type VarOperand,
} from "./var.ts";
import { EudslError, requirePlainInt } from "./view.ts";

function plainInt(name: string, value: unknown, minimum: number, maximum: number): number {
  const checked = requirePlainInt(value, `isqrt(): ${name}`);
  if (checked < minimum || checked > maximum) {
    throw new EudslError(
      "EUDSL-RANGE",
      `isqrt(): ${name} must be in [${minimum}, ${maximum}], got ${checked}`,
    );
  }
  return checked;
}

/**
 * Materialize the full operand domain through the canonical `toVar`.
 *
 * `boolean` is checked explicitly because `toVar` would reject it with the
 * generic "Var operand" text, and `isqrt` owns its diagnostic.
 */
function asVar(value: unknown): VarImpl {
  if (typeof value === "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `isqrt(): value must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
    );
  }
  try {
    return toVar(value as VarOperand);
  } catch (err) {
    if (err instanceof EudslError) {
      throw new EudslError(
        "EUDSL-TYPE",
        `isqrt(): value must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
      );
    }
    throw err;
  }
}

/** Exact integer square root of a non-negative bigint. */
function bigintSqrt(value: bigint): bigint {
  if (value < 2n) return value;
  let x = value;
  let y = (x + 1n) / 2n;
  while (y < x) {
    x = y;
    y = (x + value / x) / 2n;
  }
  return x;
}

/**
 * Emit `floor(sqrt(value) * 2 ** fixedBits)` for a runtime value.
 *
 * `value` must satisfy `0 <= value < 2 ** valueBits` at game time; the caller
 * owns that bound because a dword has no sign and no in-band range marker.
 * `fixedBits` adds fractional result bits (`8` gives a `Q8` square root).
 * The result must fit in 32 bits:
 * `ceil(valueBits / 2) + fixedBits <= 32`.
 *
 * A constant `value` folds to a single constant `Var` at build time but still
 * returns a `VarImpl`, so the result composes with runtime arithmetic. `value`
 * is the full operand domain — `number | VarImpl | ReadonlyVar | Expr`
 * (ADR 0019): a `ReadonlyVar` forwards its underlying var without a copy and
 * an `Expr` is materialized through the canonical `toVar`, exactly as a
 * hand-written temp would be. Static
 * cost is one trigger per compared input bit plus five per step
 * (`2 * valueBits + 5 * steps`); executed cost is proportional to `steps`
 * because untaken subtract branches are skipped.
 *
 * The remainder and root accumulators are cleared on every call: the kernel
 * is emitted inline, so it can sit in a per-loop body where a build-time
 * zero init only holds for the first pass.
 */
export function isqrt(
  value: VarOperand,
  valueBits = 32,
  fixedBits = 0,
): VarImpl {
  valueBits = plainInt("valueBits", valueBits, 1, 32);
  fixedBits = plainInt("fixedBits", fixedBits, 0, 16);
  const steps = Math.floor((valueBits + 1) / 2) + fixedBits;
  if (steps > 32) {
    throw new EudslError(
      "EUDSL-RANGE",
      `isqrt(): ceil(valueBits / 2) + fixedBits must be at most 32, got ${steps}`,
    );
  }

  if (typeof value === "number") {
    if (!Number.isInteger(value)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `isqrt(): value must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
      );
    }
    if (value < 0 || value >= 2 ** valueBits) {
      throw new EudslError(
        "EUDSL-RANGE",
        `isqrt(): constant value ${value} is outside [0, 2**${valueBits})`,
      );
    }
    const root = bigintSqrt(BigInt(value) << BigInt(2 * fixedBits));
    if (root >= 1n << 32n) {
      throw new EudslError(
        "EUDSL-RANGE",
        `isqrt(${value}) = ${root} does not fit in 32 bits`,
      );
    }
    const out = Var(0);
    out.set(Number(root));
    return out;
  }

  const source = asVar(value);
  const rem = Var(0);
  const root = Var(0);
  const u = Var(0);
  const diff = Var(0);
  rem.set(0);
  root.set(0);
  u.set(0);
  for (let step = steps - 1; step >= 0; step--) {
    rem.iadd(rem);
    rem.iadd(rem);
    // Two bits of `value` shift in as the integer part; below that the
    // shift-in bits are the fractional zeros of `value << 2 * fixedBits`.
    const hiBit = 2 * step + 1 - 2 * fixedBits;
    const loBit = 2 * step - 2 * fixedBits;
    if (hiBit >= 0 && hiBit < valueBits) {
      new Trigger({
        conds: [DeathsX(source.value, AtLeast, 1, 0, (1 << hiBit) >>> 0)],
        acts: [SetDeathsX(rem.value, Add, 2, 0, 0xffffffff)],
      });
    }
    if (loBit >= 0 && loBit < valueBits) {
      new Trigger({
        conds: [DeathsX(source.value, AtLeast, 1, 0, (1 << loBit) >>> 0)],
        acts: [SetDeathsX(rem.value, Add, 1, 0, 0xffffffff)],
      });
    }
    // Carry `u = 2 * root` beside `root` so each step only doubles both
    // accumulators instead of rebuilding `trial = 2 * root + 1` from `root`.
    root.iadd(root);
    u.iadd(u);
    diff.set(u);
    diff.iadd(1); // trial = u + 1
    saturatingSubInto(diff, rem); // 0 exactly when rem >= trial

    when(diff.eq(0), () => {
      // The gate means `rem >= u + 1`, so wrapping subtraction cannot
      // underflow here (unlike the saturating comparison above).
      rem.isub(u);
      rem.isub(1);
      root.iadd(1);
      u.iadd(2);
    });
  }
  return root;
}
