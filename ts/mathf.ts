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
  Db,
  DeathsX,
  SetDeathsX,
  epdOfDb,
} from "./hir.ts";
import { _setCpFromVar, withEpd } from "./cp.ts";
import { when } from "./temporal.ts";
import { _getActiveModuleBuilder, Trigger, type HirCond } from "./trigger.ts";
import {
  divmod as unsignedDivmod,
  Expr,
  ReadonlyVar,
  saturatingSubInto,
  toVar,
  Var,
  VarImpl,
  type VarOperand,
} from "./var.ts";
import { CURRENT_PLAYER, EudslError, readEpd, requirePlainInt } from "./view.ts";

/** Full operand domain for the signed-math helpers. */
export type MathOperand = VarOperand;

export const TRIG_TABLE_Q = 16;
export const TRIG_TABLE_SCALE = 1 << TRIG_TABLE_Q;


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
  value: MathOperand,
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

  const source = mathOperand("isqrt", "value", value);
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

// ---------------------------------------------------------------------------
// Signed division and modulo
// ---------------------------------------------------------------------------

const DWORD = 0xffffffff;

/** Interpret a build-time integer as a signed 32-bit dword. */
function signed(value: number): number {
  return value | 0;
}

/** Materialize one `MathOperand`, naming the function and operand. */
function mathOperand(name: string, operand: string, value: unknown): VarImpl {
  if (typeof value === "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `${name}(): ${operand} must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
    );
  }
  try {
    return toVar(value as VarOperand);
  } catch (err) {
    if (err instanceof EudslError) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${name}(): ${operand} must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
      );
    }
    throw err;
  }
}

/** Type-check a `MathOperand` without materializing it. */
function checkMathOperand(name: string, operand: string, value: unknown): void {
  const accepted =
    (typeof value === "number" && Number.isInteger(value)) ||
    value instanceof VarImpl ||
    value instanceof Expr ||
    value instanceof ReadonlyVar;
  if (typeof value === "boolean" || !accepted) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${name}(): ${operand} must be a Var, ReadonlyVar, Expr, or integer constant, got ${String(value)}`,
    );
  }
}

/** Return `(|value|, value < 0)` as two fresh Vars (flag is 0/1). */
function absWithFlag(value: VarOperand): [VarImpl, VarImpl] {
  const source = value instanceof VarImpl ? value : toVar(value);
  const mag = Var(0);
  const neg = Var(0);
  mag.set(source);
  when(source.ge(0x80000000), () => {
    neg.set(1);
    mag.set(0);
    mag.isub(source);
  });
  return [mag, neg];
}

/** Emit `value = -value` through `scratch` (wrapping subtraction). */
function negateSelf(value: VarImpl, scratch: VarImpl): void {
  scratch.set(0);
  scratch.isub(value);
  value.set(scratch);
}

function bigFloorDiv(a: bigint, b: bigint): bigint {
  const quotient = a / b;
  const rem = a % b;
  return rem !== 0n && rem < 0n !== b < 0n ? quotient - 1n : quotient;
}

function bigFloorMod(a: bigint, b: bigint): bigint {
  return a - bigFloorDiv(a, b) * b;
}

/** Fold one signed division result at build time (`b != 0`). */
function foldSigned(
  a: number,
  b: number,
  mode: "towards_zero" | "floor" | "euclid",
  part: "quot" | "rem",
): VarImpl {
  const bigA = BigInt(a);
  const bigB = BigInt(b);
  let value: bigint;
  if (mode === "floor") {
    value = part === "quot" ? bigFloorDiv(bigA, bigB) : bigFloorMod(bigA, bigB);
  } else if (mode === "euclid") {
    const absB = bigB < 0n ? -bigB : bigB;
    const rem = bigFloorMod(bigA, absB);
    value = part === "quot" ? (bigA - rem) / bigB : rem;
  } else {
    const quotient = bigA / bigB;
    value = part === "quot" ? quotient : bigA - quotient * bigB;
  }
  const out = Var(0);
  out.set(Number(value & 0xffffffffn));
  return out;
}

/** Shared sign-magnitude fixup over the unsigned restoring ladder. */
function signedDivmod(
  a: MathOperand,
  b: MathOperand,
  mode: "towards_zero" | "floor" | "euclid",
  part: "quot" | "rem",
  name: string,
): VarImpl {
  const aConst = typeof a === "number" && Number.isInteger(a);
  const bConst = typeof b === "number" && Number.isInteger(b);
  // A constant zero divisor must fail before any emission.
  if (bConst && signed(b as number) === 0) {
    throw new EudslError("EUDSL-DIV-ZERO", `${name}(): division by zero`);
  }
  if (aConst && bConst) {
    return foldSigned(signed(a as number), signed(b as number), mode, part);
  }

  const [ua, na] = absWithFlag(mathOperand(name, "a", a));
  let ub: number | VarImpl;
  let nb: number | VarImpl;
  let bValue: number | VarImpl;
  let nonzero: HirCond[] = [];
  let zeroDivisor: HirCond[] | null = null;
  if (bConst) {
    const sb = signed(b as number);
    nb = sb < 0 ? 1 : 0;
    ub = Math.abs(sb) >>> 0;
    bValue = sb >>> 0;
  } else {
    const bVar = mathOperand(name, "b", b);
    [ub, nb] = absWithFlag(bVar);
    bValue = bVar;
    nonzero = [ub.ge(1)];
    zeroDivisor = [ub.eq(0), na.eq(1)];
  }
  const [q, r] = unsignedDivmod(ua, ub);

  const scratch = Var(0);
  const signDiff = Var(0);
  signDiff.set(na);
  signDiff.iadd(nb);

  if (mode === "towards_zero") {
    when(signDiff.eq(1), () => negateSelf(q, scratch));
    when(na.eq(1), () => negateSelf(r, scratch));
    return part === "quot" ? q : r;
  }

  if (mode === "floor") {
    // Truncated remainder takes the dividend's sign.
    when(na.eq(1), () => negateSelf(r, scratch));
    when(signDiff.eq(1), () => negateSelf(q, scratch));
    when([...nonzero, signDiff.eq(1), r.ne(0)], () => {
      q.isub(1);
      r.iadd(bValue);
    });
    return part === "quot" ? q : r;
  }

  // Euclidean: the remainder is always non-negative. `q` starts truncated;
  // when the truncated remainder was negative it gains |b|, so `q` moves by
  // `-sign(b)` (the ticket's `q -= nb` only holds for a positive divisor).
  when(signDiff.eq(1), () => negateSelf(q, scratch));
  when([...nonzero, na.eq(1), r.ne(0)], () => {
    if (typeof bValue === "number") {
      q.iadd(2 * (nb as number) - 1);
    } else {
      q.iadd(nb);
      q.iadd(nb);
      q.isub(1);
    }
  });
  if (zeroDivisor !== null) {
    when(zeroDivisor, () => negateSelf(r, scratch));
  }
  return part === "quot" ? q : r;
}

/** Emit C/JS truncating signed division (`-7 / 2 === -3`). */
export function divTowardsZero(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "towards_zero", "quot", "divTowardsZero");
}

/** Emit C/JS truncating signed remainder (sign follows the dividend). */
export function modTowardsZero(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "towards_zero", "rem", "modTowardsZero");
}

/** Emit Python `//` signed division (the floor quotient). */
export function divFloor(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "floor", "quot", "divFloor");
}

/** Emit Python `%` signed remainder (sign follows the divisor). */
export function modFloor(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "floor", "rem", "modFloor");
}

/** Emit Euclidean division (remainder always in `[0, |b|)`). */
export function divEuclid(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "euclid", "quot", "divEuclid");
}

/** Emit the non-negative Euclidean remainder. */
export function modEuclid(a: MathOperand, b: MathOperand): VarImpl {
  return signedDivmod(a, b, "euclid", "rem", "modEuclid");
}

export const div_towards_zero = divTowardsZero;
export const div_floor = divFloor;
export const div_euclid = divEuclid;
export const mod_towards_zero = modTowardsZero;
export const mod_floor = modFloor;
export const mod_euclid = modEuclid;

// ---------------------------------------------------------------------------
// Integer powers
// ---------------------------------------------------------------------------

/**
 * Emit `(base ** exp) & ((1 << bits) - 1)` for `0 <= exp <= 31`.
 *
 * `exp` is build-time so the exponentiation-by-squaring chain is fully
 * unrolled: `(bitLength - 1) + (popcount - 1)` `Var` multiplications. A
 * runtime base is copied into a fresh result, so the operand is never
 * mutated. `exp === 1` is one value copy and no multiplication.
 */
export function pow(base: MathOperand, exp: number, bits = 32): VarImpl {
  const exponent = requirePlainInt(exp, "pow(): exp");
  if (exponent < 0 || exponent > 31) {
    throw new EudslError("EUDSL-RANGE", `pow(): exp must be in [0, 31], got ${exponent}`);
  }
  const width = requirePlainInt(bits, "pow(): bits");
  if (width < 1 || width > 32) {
    throw new EudslError("EUDSL-RANGE", `pow(): bits must be in [1, 32], got ${width}`);
  }
  const mask = (1n << BigInt(width)) - 1n;

  if (typeof base === "number" && Number.isInteger(base)) {
    const out = Var(0);
    out.set(Number((BigInt(base) ** BigInt(exponent)) & mask));
    return out;
  }

  checkMathOperand("pow", "base", base);
  if (exponent === 0) {
    const out = Var(0);
    out.set(1);
    return out;
  }

  const source = toVar(base as VarOperand);
  const out = Var(0);
  out.set(source);
  const bitLength = exponent.toString(2).length;
  for (let bit = bitLength - 2; bit >= 0; bit--) {
    out.imul(out);
    if (((exponent >>> bit) & 1) !== 0) {
      out.imul(source);
    }
  }
  if (width < 32) {
    out.iand(Number(mask));
  }
  return out;
}

export const powInt = pow;
export const pow_int = pow;

// ---------------------------------------------------------------------------
// Table-driven trigonometry
// ---------------------------------------------------------------------------

function q16(value: number): number {
  return Math.floor(value * TRIG_TABLE_SCALE + 0.5);
}

/** `round(atan(t / 256) / (2*pi) * 65536)` for `t = 0..256` (Q16 turns). */
const ATAN256_Q16: readonly number[] = Array.from({ length: 257 }, (_, t) =>
  q16(Math.atan(t / 256.0) / (2.0 * Math.PI)),
);
/** `floor(cos(pi/180 * i) * 65536 + 0.5)` for `i = 0..90`. */
const COS_DEG_Q16: readonly number[] = Array.from({ length: 91 }, (_, i) =>
  q16(Math.cos((Math.PI / 180.0) * i)),
);
/** `floor(sin(pi/180 * i) * 65536 + 0.5)` for `i = 0..90`. */
const SIN_DEG_Q16: readonly number[] = Array.from({ length: 91 }, (_, i) =>
  q16(Math.sin((Math.PI / 180.0) * i)),
);
/** `floor(cos(2*pi * i / 256) * 65536 + 0.5)` for `i = 0..64`. */
const COS_256_Q16: readonly number[] = Array.from({ length: 65 }, (_, i) =>
  q16(Math.cos((2.0 * Math.PI * i) / 256.0)),
);
/** `floor(sin(2*pi * i / 256) * 65536 + 0.5)` for `i = 0..64`. */
const SIN_256_Q16: readonly number[] = Array.from({ length: 65 }, (_, i) =>
  q16(Math.sin((2.0 * Math.PI * i) / 256.0)),
);

/** Little-endian dword payload for a constant table. */
function payload(values: readonly number[]): Uint8Array {
  const bytes = new Uint8Array(values.length * 4);
  const view = new DataView(bytes.buffer);
  values.forEach((value, index) => view.setUint32(index * 4, value >>> 0, true));
  return bytes;
}

/** One build's lazily materialized trig payloads and runtime base EPDs. */
class TrigTables {
  private readonly epds = new Map<string, VarImpl>();

  private tableEpd(name: string, values: readonly number[]): VarImpl {
    let epd = this.epds.get(name);
    if (epd === undefined) {
      epd = epdOfDb(new Db(payload(values)));
      this.epds.set(name, epd);
    }
    return epd;
  }

  get atanEpd(): VarImpl {
    return this.tableEpd("atan", ATAN256_Q16);
  }

  get cosEpd(): VarImpl {
    return this.tableEpd("cos", COS_DEG_Q16);
  }

  get sinEpd(): VarImpl {
    return this.tableEpd("sin", SIN_DEG_Q16);
  }

  get cos256Epd(): VarImpl {
    return this.tableEpd("cos256", COS_256_Q16);
  }

  get sin256Epd(): VarImpl {
    return this.tableEpd("sin256", SIN_256_Q16);
  }
}

const tableCache = new WeakMap<object, TrigTables>();

/** Return (and lazily build) the active builder's shared trig tables. */
function tables(): TrigTables {
  const builder = _getActiveModuleBuilder();
  if (builder === null) {
    throw new EudslError("EUDSL-SCOPE", "mathf trig tables must be built inside buildModule()");
  }
  let cached = tableCache.get(builder);
  if (cached === undefined) {
    cached = new TrigTables();
    tableCache.set(builder, cached);
  }
  return cached;
}

/** Read `baseEpd[index]` through a temporary CurrentPlayer binding. */
function readTable(baseEpd: VarImpl, index: VarImpl): VarImpl {
  const cursor = Var(0);
  cursor.set(baseEpd);
  cursor.iadd(index);
  return withEpd(cursor, () => readEpd(CURRENT_PLAYER, 32, 0));
}

/** Read two table rows inside one ambient CurrentPlayer save/restore. */
function readTablePair(
  firstEpd: VarImpl,
  secondEpd: VarImpl,
  index: VarImpl,
): [VarImpl, VarImpl] {
  const firstCursor = Var(0);
  firstCursor.set(firstEpd);
  firstCursor.iadd(index);
  const secondCursor = Var(0);
  secondCursor.set(secondEpd);
  secondCursor.iadd(index);
  return withEpd(firstCursor, () => {
    const first = readEpd(CURRENT_PLAYER, 32, 0);
    _setCpFromVar(secondCursor);
    const second = readEpd(CURRENT_PLAYER, 32, 0);
    return [first, second] as [VarImpl, VarImpl];
  });
}

/** Shared octant reduction returning a 1/256-turn angle (0..255). */
function atan256Core(y: MathOperand, x: MathOperand, name: string): VarImpl {
  const yv = mathOperand(name, "y", y);
  const xv = mathOperand(name, "x", x);
  const [ax, nx] = absWithFlag(xv);
  const [ay, ny] = absWithFlag(yv);

  const swap = Var(0);
  when(ay.gt(ax), () => swap.set(1));

  const maximum = Var(0);
  const minimum = Var(0);
  maximum.set(ax);
  minimum.set(ay);
  when(swap.eq(1), () => {
    maximum.set(ay);
    minimum.set(ax);
  });

  const scaled = Var(0);
  scaled.set(minimum);
  scaled.ishl(8);
  const [index] = unsignedDivmod(scaled, maximum);
  const raw = readTable(tables().atanEpd, index);
  const octant = Var(0);
  octant.set(raw);
  octant.ishr(8);

  const quadrant = Var(0);
  quadrant.set(nx);
  quadrant.iadd(nx);
  quadrant.iadd(ny);
  const half = Var(0);
  half.set(quadrant);
  half.ishr(1);
  const odd = Var(0);
  odd.set(quadrant);
  odd.iand(1);
  const flip = Var(0);
  flip.set(half);
  flip.ixor(odd);

  const signedOctant = Var(0);
  signedOctant.set(octant);
  when(flip.eq(1), () => negateSelf(signedOctant, Var(0)));

  // Non-swap: off = 128*half, angle = off + signed_octant.
  // Swap:     off = 64 + 128*odd, angle = off - signed_octant.
  const normal = Var(0);
  normal.set(half);
  normal.ishl(7);
  normal.iadd(signedOctant);
  const swapped = Var(0);
  swapped.set(odd);
  swapped.ishl(7);
  swapped.iadd(64);
  swapped.isub(signedOctant);

  const angle = Var(0);
  angle.set(normal);
  when(swap.eq(1), () => angle.set(swapped));
  angle.iand(0xff);
  return angle;
}

/** Emit `atan2(y, x)` in 1/256-turn units (0..255). */
export function atan2_256(y: MathOperand, x: MathOperand): VarImpl {
  return atan256Core(y, x, "atan2_256");
}

/** Emit `atan2(y, x)` in degrees (0..359), from the shared turn table. */
export function atan2(y: MathOperand, x: MathOperand): VarImpl {
  const angle = atan256Core(y, x, "atan2");
  const degrees = Var(0);
  degrees.set(angle);
  degrees.imul(360);
  degrees.ishr(8);
  return degrees;
}

export const atan2Fixed8 = atan2_256;

/** Shared degree/256-turn length-and-direction kernel. */
function lengthdirKernel(
  length: MathOperand,
  angle: MathOperand,
  period: number,
  quadrantSize: number,
  turn256: boolean,
  name: string,
): [VarImpl, VarImpl] {
  const angleVar = mathOperand(name, "angle", angle);
  const [, normalised] = unsignedDivmod(angleVar, period);
  const [quadrant, remainder] = unsignedDivmod(normalised, quadrantSize);

  const cache = tables();
  const cosEpd = turn256 ? cache.cos256Epd : cache.cosEpd;
  const sinEpd = turn256 ? cache.sin256Epd : cache.sinEpd;
  const [cosine, sine] = readTablePair(cosEpd, sinEpd, remainder);

  const odd = Var(0);
  odd.set(quadrant);
  odd.iand(1);
  const cosMag = Var(0);
  cosMag.set(cosine);
  const sinMag = Var(0);
  sinMag.set(sine);
  when(odd.eq(1), () => {
    cosMag.set(sine);
    sinMag.set(cosine);
  });

  const scratch = Var(0);
  let lengthNeg: number | VarImpl;
  let dx: VarImpl;
  let dy: VarImpl;
  if (typeof length === "number" && Number.isInteger(length)) {
    const signedLength = signed(length);
    const lengthMag = Math.abs(signedLength);
    lengthNeg = signedLength < 0 ? 1 : 0;
    dx = Var(0);
    dx.set(cosMag);
    dx.imul(lengthMag);
    dy = Var(0);
    dy.set(sinMag);
    dy.imul(lengthMag);
  } else {
    const lengthVar = mathOperand(name, "length", length);
    const [lengthMagVar, lengthNegVar] = absWithFlag(lengthVar);
    lengthNeg = lengthNegVar;
    dx = Var(0);
    dx.set(cosMag);
    dx.imul(lengthMagVar);
    dy = Var(0);
    dy.set(sinMag);
    dy.imul(lengthMagVar);
  }

  dx.ishr(16);
  dy.ishr(16);

  if (typeof lengthNeg === "number") {
    if (lengthNeg !== 0) {
      negateSelf(dx, scratch);
      negateSelf(dy, scratch);
    }
  } else {
    when(lengthNeg.eq(1), () => {
      negateSelf(dx, scratch);
      negateSelf(dy, scratch);
    });
  }

  const half = Var(0);
  half.set(quadrant);
  half.ishr(1);
  const combined = Var(0);
  combined.set(half);
  combined.iadd(odd);
  when(combined.eq(1), () => negateSelf(dx, scratch));
  when(half.eq(1), () => negateSelf(dy, scratch));
  return [dx, dy];
}

/**
 * Return `(dx, dy)` for `length` at `angle` degrees.
 *
 * `length` is read as a signed 32-bit value and must satisfy
 * `|length| <= 65535` so the Q16 product stays below `2**32`. `angle` is an
 * unsigned degree value normalised modulo 360.
 */
export function lengthdir(length: MathOperand, angle: MathOperand): [VarImpl, VarImpl] {
  return lengthdirKernel(length, angle, 360, 90, false, "lengthdir");
}

/** Like {@link lengthdir} with a 256-unit turn and Q16 quarter table. */
export function lengthdir_256(length: MathOperand, angle: MathOperand): [VarImpl, VarImpl] {
  return lengthdirKernel(length, angle, 256, 64, true, "lengthdir_256");
}

export const lengthdirFixed8 = lengthdir_256;

// ---------------------------------------------------------------------------
// Binary classification search
// ---------------------------------------------------------------------------

function bitLength(value: number): number {
  return value === 0 ? 0 : value.toString(2).length;
}

/** Shared `ceil(log2(span))` halving search over a runtime predicate. */
function binarySearch(
  lo: number,
  hi: number,
  predicate: (x: VarImpl) => HirCond,
  last: boolean,
  name: string,
): VarImpl {
  lo = requirePlainInt(lo, `${name}(): lo`);
  hi = requirePlainInt(hi, `${name}(): hi`);
  if (lo < 0 || hi < 0 || lo > hi || hi >= DWORD) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${name}(): lo/hi must satisfy 0 <= lo <= hi < 0xFFFFFFFF, got lo=${lo}, hi=${hi}`,
    );
  }
  if (typeof predicate !== "function") {
    throw new EudslError("EUDSL-TYPE", `${name}(): predicate must be callable`);
  }

  const span = hi - lo;
  const steps = bitLength(span + 1);
  const left = Var(0);
  const right = Var(0);
  left.set(0);
  right.set(span);

  for (let step = 0; step < steps; step++) {
    const mid = Var(0);
    const width = Var(0);
    mid.set(left);
    width.set(right);
    width.isub(left);
    width.ishr(1);
    mid.iadd(width);
    let condition: HirCond;
    if (last) {
      const probe = Var(0);
      probe.set(hi);
      probe.isub(mid);
      condition = predicate(probe);
    } else {
      const probe = Var(0);
      probe.set(lo);
      probe.iadd(mid);
      condition = predicate(probe);
    }
    when(
      condition,
      () => right.set(mid),
      () => {
        left.set(mid);
        left.iadd(1);
      },
    );
  }

  const out = Var(0);
  if (last) {
    out.set(hi);
    out.isub(left);
  } else {
    out.set(lo);
    out.iadd(left);
  }
  return out;
}

/**
 * First `x` in `[lo, hi]` with `predicate(x)`, else `hi + 1`.
 *
 * The range is assumed to be `False … False True … True`. The predicate is
 * evaluated at its emission site, so it must be condition-only (an action
 * would run unconditionally).
 */
export function binaryMin(
  lo: number,
  hi: number,
  predicate: (x: VarImpl) => HirCond,
): VarImpl {
  return binarySearch(lo, hi, predicate, false, "binaryMin");
}

/**
 * Last `x` in `[lo, hi]` with `predicate(x)`, else `lo - 1`.
 *
 * The range is assumed to be `True … True False … False`. The predicate is
 * evaluated at its emission site, so it must be condition-only (an action
 * would run unconditionally).
 */
export function binaryMax(
  lo: number,
  hi: number,
  predicate: (x: VarImpl) => HirCond,
): VarImpl {
  return binarySearch(lo, hi, predicate, true, "binaryMax");
}

export const binary_min = binaryMin;
export const binary_max = binaryMax;
