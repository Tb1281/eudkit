/**
 * Scalar `Var` and arithmetic/bitwise `Expr` backed by `64A` `TriggerPage` action slots
 * enforcing the self-targeted action invariant (`act[k].dest == EPD(act[k].value)` at rest).
 */

import {
  Add,
  AtLeast,
  Deaths,
  DeathsX,
  Exactly,
  SetDeathsX,
  SetTo,
  Subtract,
  TriggerFieldReloc,
  emitVarStore,
} from "./hir.ts";
import { when } from "./temporal.ts";
import { LazyBinding } from "./lazy-binding.ts";
import {
  ACT_COUNT,
  type CondLike,
  type HirAct,
  type HirCond,
  type MaterializableCond,
  Trigger,
  _getActiveModuleBuilder,
  _registerTrigger,
} from "./trigger.ts";
import {
  EudslError,
  FieldValueSpace,
  TriggerFieldRef,
  applyComparison,
  describeValue,
  requirePlainInt,
  warnLazyInBuilder,
} from "./view.ts";

export type BinOpKind =
  | "add"
  | "sub"
  | "mul"
  | "div"
  | "mod"
  | "bitand"
  | "bitor"
  | "bitxor"
  | "shl"
  | "shr";

export type UnaryOpKind = "bitnot" | "neg";

export type ExprOpKind = BinOpKind | UnaryOpKind;

export type VarOperand = number | VarImpl | Expr | ReadonlyVar;

export function allocVarSlot(
  init: number,
  mask = 0xffffffff,
  storage?: "page",
): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.allocVar === "function") {
    return builder.allocVar(init >>> 0, mask >>> 0, storage);
  }
  // No fallback: the builder owns the id space, so a Var created outside
  // buildModule has no id to receive. A silent fallback id would collide
  // with the first in-module var (see the aligned-string-padding bug).
  throw new EudslError(
    "EUDSL-SCOPE",
    "Var() must be created inside buildModule()",
  );
}

export function getNextVarSlot(): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.getNextVarId === "function") {
    return builder.getNextVarId();
  }
  throw new EudslError(
    "EUDSL-SCOPE",
    "Var ids are only available inside buildModule()",
  );
}

/**
 * Next free `TriggerPage` action slot.
 *
 * Scalars demoted to dword/bit cells do not consume a page slot, so page
 * alignment must count page-pinned variables rather than raw variable ids.
 */
/** Reserve a module string table entry and return its 0-based index. */
export function allocString(text: string): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.allocString === "function") {
    return builder.allocString(text);
  }
  throw new EudslError(
    "EUDSL-SCOPE",
    "FixedString buffers must be allocated inside buildModule()",
  );
}

export function getNextStringIndex(): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.getNextStringIndex === "function") {
    return builder.getNextStringIndex();
  }
  return 0;
}

/** Builder scope mutation serial used to invalidate static string positions. */
export function getScopeSerial(): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.getScopeSerial === "function") {
    return builder.getScopeSerial();
  }
  return 0;
}

/**
 * Allocate a page-pinned scalar initialized to the absolute EPD of module
 * string `index` + `offset` bytes. Lowering folds this against the basemap
 * STR offset table.
 */
export function allocStringVar(
  index: number,
  offset = 0,
  mask = 0xffffffff,
): number {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.allocStringVar === "function") {
    return builder.allocStringVar(index, offset, mask);
  }
  throw new EudslError(
    "EUDSL-SCOPE",
    "FixedString buffers must be allocated inside buildModule()",
  );
}

export function getNextPageSlot(): number {
  const builder = _getActiveModuleBuilder() as
    | (ReturnType<typeof _getActiveModuleBuilder> & {
        getNextPageSlot?: () => number;
      })
    | null;
  if (builder && typeof builder.getNextPageSlot === "function") {
    return builder.getNextPageSlot();
  }
  throw new EudslError(
    "EUDSL-SCOPE",
    "Page slots are only available inside buildModule()",
  );
}

/**
 * Pad with page-pinned variables until the next page slot is page-aligned.
 *
 * Alignment must count page slots, not raw variable ids: non-page variables
 * consume ids but no page slots, so padding on the raw counter would place
 * page-aligned containers at the wrong pinned rank. Returns the number of
 * pads added (zero when already aligned).
 */
export function alignPageBoundary(): number {
  const rem = getNextPageSlot() % ACT_COUNT;
  if (rem === 0) return 0;
  const pads = ACT_COUNT - rem;
  for (let i = 0; i < pads; i++) {
    allocVarSlot(0, 0xffffffff, "page");
  }
  return pads;
}

/**
 * Pad to the next page boundary only when `fieldCount` fields would not fit
 * in the current page. When the fields fit, nothing is padded, so packing is
 * preserved. Returns the number of pads added.
 */
export function ensurePageFit(fieldCount: number): number {
  const rem = getNextPageSlot() % ACT_COUNT;
  if (rem !== 0 && rem + fieldCount > ACT_COUNT) {
    return alignPageBoundary();
  }
  return 0;
}

export function emitVarTransfer(dstVar: number, srcVar: number, modifier: number): void {
  const builder = _getActiveModuleBuilder();
  if (builder && typeof builder.queueVarTransfer === "function") {
    builder.queueVarTransfer(dstVar, srcVar, modifier);
  }
}

export class Expr {
  readonly op: ExprOpKind;
  readonly lhs: VarOperand;
  readonly rhs?: VarOperand;

  constructor(op: ExprOpKind, lhs: VarOperand, rhs?: VarOperand) {
    this.op = op;
    this.lhs = lhs;
    this.rhs = rhs;
  }

  add(rhs: VarOperand): Expr {
    return new Expr("add", this, rhs);
  }

  sub(rhs: VarOperand): Expr {
    return new Expr("sub", this, rhs);
  }

  mul(rhs: VarOperand): Expr {
    return new Expr("mul", this, rhs);
  }

  div(rhs: VarOperand): Expr {
    return new Expr("div", this, rhs);
  }

  mod(rhs: VarOperand): Expr {
    return new Expr("mod", this, rhs);
  }

  bitAnd(rhs: VarOperand): Expr {
    return new Expr("bitand", this, rhs);
  }

  bit_and(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  and(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  band(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  bitOr(rhs: VarOperand): Expr {
    return new Expr("bitor", this, rhs);
  }

  bit_or(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  or(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  bor(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  bitXor(rhs: VarOperand): Expr {
    return new Expr("bitxor", this, rhs);
  }

  bit_xor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  xor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  bxor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  bitNot(): Expr {
    return new Expr("bitnot", this);
  }

  bit_not(): Expr {
    return this.bitNot();
  }

  invert(): Expr {
    return this.bitNot();
  }

  bnot(): Expr {
    return this.bitNot();
  }

  shl(rhs: VarOperand): Expr {
    return new Expr("shl", this, rhs);
  }

  lshift(rhs: VarOperand): Expr {
    return this.shl(rhs);
  }

  shr(rhs: VarOperand): Expr {
    return new Expr("shr", this, rhs);
  }

  rshift(rhs: VarOperand): Expr {
    return this.shr(rhs);
  }

  neg(): Expr {
    return new Expr("neg", this);
  }

  eq(rhs: number): HirCond;
  eq(rhs: VarOperand): CondLike;
  eq(rhs: VarOperand): CondLike {
    return toVar(this).eq(rhs);
  }

  /**
   * Inequality. Deliberately returns `CondLike`: a numeric middle constant
   * yields a `DeferredCond` (and a non-constant RHS always did), because the
   * comparison must be materialized at its evaluation site. Callers that need
   * a `HirCond` value can use the extreme forms (`ne(0)`, `ne(max)`, which are
   * direct 1T conds) or invert `eq`.
   */
  ne(rhs: VarOperand): CondLike {
    return toVar(this).ne(rhs);
  }

  lt(rhs: number): HirCond;
  lt(rhs: VarOperand): CondLike;
  lt(rhs: VarOperand): CondLike {
    return toVar(this).lt(rhs);
  }

  le(rhs: number): HirCond;
  le(rhs: VarOperand): CondLike;
  le(rhs: VarOperand): CondLike {
    return toVar(this).le(rhs);
  }

  gt(rhs: number): HirCond;
  gt(rhs: VarOperand): CondLike;
  gt(rhs: VarOperand): CondLike {
    return toVar(this).gt(rhs);
  }

  ge(rhs: number): HirCond;
  ge(rhs: VarOperand): CondLike;
  ge(rhs: VarOperand): CondLike {
    return toVar(this).ge(rhs);
  }
}

/**
 * Canonical operand materializer: a constant or handle becomes `number |
 * VarImpl`.
 *
 * Plain integers stay constants (validated and masked to 32 bits); every
 * runtime handle becomes the `VarImpl` that carries it. This is the single
 * "operand -> value" kernel shared by `var`, `string`, and `blockio`; each
 * caller narrows the accepted *domain* at its own edge before delegating (see
 * {@link toVar}).
 */
export function toOperand(op: VarOperand): number | VarImpl {
  if (typeof op === "number") {
    return requirePlainInt(op, "Var operand") >>> 0;
  }
  return toVar(op);
}

/**
 * Canonical operand materializer: a constant or handle becomes a `VarImpl`.
 *
 * Accepted operands are a plain integer, `VarImpl`, `ReadonlyVar`, or `Expr` —
 * the full operand domain. Callers with a narrower domain (`blockio` takes
 * `Var | Expr`; `string` takes `Var | Expr | ReadonlyVar`) keep their domain
 * check at the edge and then delegate here, so the materialization step has
 * exactly one implementation per frontend.
 */
export function toVar(op: VarOperand): VarImpl {
  if (op instanceof VarImpl) {
    return op;
  }
  if (typeof op === "number") {
    return new VarImpl(requirePlainInt(op, "Var operand") >>> 0);
  }
  if (!(op instanceof Expr)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `Var operand must be an integer constant, a Var, or an Expr; got ${describeValue(op)}`,
    );
  }
  const tmp = new VarImpl(0);
  evalExprInto(tmp, op);
  return tmp;
}

/**
 * Emit StarCraft's `Subtract`, which saturates at zero within the mask.
 *
 * Public arithmetic subtraction wraps mod 2^32 (issue #42), so the paths
 * that genuinely depend on underflow clamping — comparison helpers,
 * complement kernels, and division's `den >= rem` test — opt in here.
 *
 * @internal Seam helper shared with the deferred-comparison materializers.
 */
export function saturatingSubInto(dst: VarImpl, rhs: VarOperand): void {
  if (typeof rhs === "number") {
    const v = (rhs & dst.bitmask) >>> 0;
    SetDeathsX(dst.value, Subtract, v, 0, dst.bitmask);
    return;
  }
  const src = rhs instanceof VarImpl ? rhs : toVar(rhs);
  emitVarTransfer(dst.id, src.id, Subtract);
}

/**
 * Multiplier bits per early-exit chunk in `mulShiftAdd`.
 *
 * The kernel gates each following chunk on the multiplier's remaining high
 * bits, so a sparse multiplier skips whole chunks while a dense one pays one
 * extra gate. 16 halves sparse work (`0x5`: E 137/32/2,356 -> 77/17/1,351)
 * while bounding the dense regression at +2T/+1C/+2A. Tune here if a profile
 * ever favours a different balance; the kernel does not hard-code two copies.
 */
export const MUL_SHIFT_ADD_CHUNK = 16;

/**
 * `out = a * b` by doubling `a` and adding it on each set bit of `b`.
 *
 * Shared by `Var`'s Var x Var `mul` path and the camera helper (which imports
 * it and keeps its `mulShiftAdd` call sites unchanged): `bits` condition
 * triggers plus one conditional copy per bit, instead of the multiplicative
 * bit-pair kernel's `bits * bits` triggers. The gates are emitted in chunks of
 * `MUL_SHIFT_ADD_CHUNK`; after each chunk but the last, the remaining gates
 * are nested under one condition on `b & ~((1 << end) - 1)`, so a sparse
 * multiplier stops early.
 *
 * The accumulator is cleared on every call: the kernel is emitted inside a
 * per-loop body, so a build-time zero init would let pass 2 accumulate on
 * pass 1. Callers must copy the result back (`dst.set(out)`) rather than pass
 * `dst` in, because `b` is read by every gate and `a` through `cur`: an
 * in-place accumulator would corrupt a later bit test.
 *
 * @internal Seam helper shared with `camera`; not part of the public DSL.
 */
export function mulShiftAdd(
  a: VarImpl,
  b: VarImpl,
  bits = 32,
): VarImpl {
  const out = new VarImpl(0);
  out.set(0);
  const cur = new VarImpl(0);
  cur.set(a);

  const emitChunk = (start: number): void => {
    const end = Math.min(start + MUL_SHIFT_ADD_CHUNK, bits);
    for (let bit = start; bit < end; bit++) {
      const bBit = 1 << bit;
      when(DeathsX(b.value, AtLeast, 1, 0, bBit), () => {
        out.iadd(cur);
      });
      if (bit + 1 < bits) {
        cur.iadd(cur);
      }
    }
    if (end < bits) {
      const highMask = ~((1 << end) - 1) >>> 0;
      when(DeathsX(b.value, AtLeast, 1, 0, highMask), () => {
        emitChunk(end);
      });
    }
  };

  emitChunk(0);
  return out;
}

function u32(value: bigint): number {
  return Number(value & 0xffffffffn);
}

/**
 * In-place `target <<= n` actions, eudplib `vbase.py` parity.
 *
 * `1 <= n <= 31`. The shift is expressed as masked SetTo/Add pairs over
 * alternating bit lanes, so it always fits one Trigger (`<= 63` actions)
 * instead of the old per-source-bit trigger ladder.
 */
function constShlActs(target: VarImpl, n: number): HirAct[] {
  const mask = (1n << BigInt(n + 1)) - 1n;
  const half = mask >> 1n;
  const acts: HirAct[] = [];
  for (let t = 31 - n; t >= 0; t--) {
    acts.push(SetDeathsX(target.value, SetTo, 0, 0, u32(half << BigInt(t + 1))));
    acts.push(
      SetDeathsX(
        target.value,
        Add,
        u32(half << BigInt(t)),
        0,
        u32(mask << BigInt(t)),
      ),
    );
  }
  acts.push(SetDeathsX(target.value, SetTo, 0, 0, u32(half)));
  return acts;
}

/**
 * In-place `target >>= n` actions, eudplib `vbase.py` parity.
 *
 * `1 <= n <= 31`. At most 32 masked actions, so it is always one Trigger.
 */
function constShrActs(target: VarImpl, n: number): HirAct[] {
  const mask = (1n << BigInt(n + 1)) - 1n;
  const half = mask >> 1n;
  const acts: HirAct[] = [SetDeathsX(target.value, SetTo, 0, 0, u32(half))];
  for (let t = 0; t < 32 - n; t++) {
    acts.push(
      SetDeathsX(
        target.value,
        Subtract,
        u32(half << BigInt(t)),
        0,
        u32(mask << BigInt(t)),
      ),
    );
  }
  return acts;
}

/** Emit `dst = ~dst` inside `dst.bitmask` with two alternating-lane adds. */
function complementInto(dst: VarImpl): void {
  const mask = dst.bitmask >>> 0;
  const even = (0x55555555 & mask) >>> 0;
  const odd = (0xaaaaaaaa & mask) >>> 0;
  if (even !== 0) {
    SetDeathsX(dst.value, Add, 0x55555555, 0, even);
  }
  if (odd !== 0) {
    SetDeathsX(dst.value, Add, 0xaaaaaaaa, 0, odd);
  }
}

/** Emit the five-stage barrel shl/shr body plus its `>= 32` zero guard. */
function dynamicShift(
  target: VarImpl,
  amount: VarImpl,
  constActs: (target: VarImpl, n: number) => HirAct[],
): void {
  for (let k = 0; k < 5; k++) {
    const shiftAmt = 1 << k;
    when(DeathsX(amount.value, AtLeast, 1, 0, shiftAmt), () => {
      new Trigger({ conds: [], acts: constActs(target, shiftAmt) });
    });
  }
  new Trigger({
    conds: [Deaths(amount.value, AtLeast, 32, 0)],
    acts: [SetDeathsX(target.value, SetTo, 0, 0, 0xffffffff)],
  });
}

function divmodLadder(num: VarImpl, den: VarImpl): [VarImpl, VarImpl] {
  const rem = new VarImpl(0);
  const quot = new VarImpl(0);
  const diff = new VarImpl(0);
  rem.set(0); // per-invocation reset; Var(0) init runs only once
  quot.set(0);
  for (let b = 31; b >= 0; b--) {
    // rem = (rem << 1) | ((num >> b) & 1)
    // Ticket #55: double `rem` in place. `rem.iadd(rem)` is a self-transfer
    // (`dst_var === src_var`, modifier Add) — an existing lowering path also
    // reached by `v.set(v.add(v))` — so no new shape is introduced.
    // Materialising `rem.shl(1)` into a fresh temporary costs ≈30 static
    // triggers / 31 static conditions per iteration (961 T / 992 C per
    // site), plus a copy-back.
    // NOTE: executed *actions* rise; executed triggers/conditions fall.
    // The gain is map size and trigger-queue pressure, not raw work.
    rem.iadd(rem); // rem <<= 1 (self-transfer)
    // #102: one hoisted `diff` replaces the per-bit `diff` + `subAmt`
    // pair. `diff` is 0 exactly when `rem >= den`; the gated body then
    // takes `den` out of `rem` and sets the quotient bit in one place.
    // `den >= 1` preserves divide-by-zero == 0, the same contract as the
    // old `Deaths(den, AtLeast, 1)` gate.
    //
    // Cycle 4: `diff.set(den)` sits above the numerator-bit trigger. The
    // trigger writes `rem` and reads `num`; the copy writes `diff` and
    // reads `den`, so the two transfers are independent and transfer
    // coalescing folds them into one page firing per bit.
    diff.set(den);
    new Trigger({
      conds: [DeathsX(num.value, AtLeast, 1, 0, (1 << b) >>> 0)],
      acts: [SetDeathsX(rem.value, Add, 1, 0, 0xffffffff)],
    });
    saturatingSubInto(diff, rem); // 0 exactly when rem >= den
    when([diff.eq(0), den.ge(1)], () => {
      saturatingSubInto(rem, den);
      quot.iadd((1 << b) >>> 0);
    });
  }
  return [quot, rem];
}

function constDivmod(num: VarImpl, c: number): [VarImpl, VarImpl] {
  if (c === 0) {
    throw new EudslError("EUDSL-DIV-ZERO", "Division by zero");
  }
  const quot = new VarImpl(0);
  const rem = new VarImpl(0);
  if (c === 1) {
    quot.set(num);
    return [quot, rem];
  }
  if ((c & (c - 1)) === 0) {
    const k = Math.clz32(c) ^ 31;
    evalExprInto(quot, num.shr(k));
    evalExprInto(rem, num.bitAnd(c - 1));
    return [quot, rem];
  }
  rem.set(num);
  quot.set(0);
  let maxB = 0;
  while (maxB < 31 && c * 2 ** (maxB + 1) <= 0xffffffff) {
    maxB++;
  }
  for (let b = maxB; b >= 0; b--) {
    const scaled = (c * 2 ** b) >>> 0;
    const bit = (1 << b) >>> 0;
    new Trigger({
      conds: [Deaths(rem.value, AtLeast, scaled, 0)],
      acts: [
        SetDeathsX(rem.value, Subtract, scaled, 0, 0xffffffff),
        SetDeathsX(quot.value, Add, bit, 0, 0xffffffff),
      ],
    });
  }
  return [quot, rem];
}

function divmodOperands(
  lhs: VarOperand,
  rhs: VarOperand,
): [VarImpl, VarImpl] {
  const num = toVar(lhs);
  const operand = toOperand(rhs);
  const [quot, rem] =
    typeof operand === "number"
      ? constDivmod(num, operand)
      : divmodLadder(num, operand);
  // Return fresh result vars, so `[q, r] = divmod(q, r)` never aliases a
  // ladder temporary even though the ladder already reads both operands.
  const resultQuot = new VarImpl(0);
  const resultRem = new VarImpl(0);
  resultQuot.set(quot);
  resultRem.set(rem);
  return [resultQuot, resultRem];
}

/**
 * Return `[a / b, a % b]` as a pair of fresh Vars from one restoring ladder.
 *
 * `b` may be a runtime `Var` (one 32-step restoring ladder) or a plain
 * integer constant (the constant-divisor ladder). Divisor zero yields
 * `[0, a]` at runtime, matching `v.div` / `v.mod`; a build-time constant zero
 * raises `EUDSL-DIV-ZERO` like those paths.
 */
export function divmod(a: VarOperand, b: VarOperand): [VarImpl, VarImpl] {
  return divmodOperands(a, b);
}

function evalExprInto(dst: VarImpl, expr: Expr): void {
  const op = expr.op;
  if (op === "bitnot") {
    const lhs = toOperand(expr.lhs);
    if (typeof lhs === "number") {
      dst.set((~lhs) >>> 0);
      return;
    }
    if (dst.id !== lhs.id) {
      dst.set(lhs);
    }
    complementInto(dst);
    return;
  }

  if (op === "neg") {
    const lhs = toOperand(expr.lhs);
    if (typeof lhs === "number") {
      dst.set((-lhs) >>> 0);
      return;
    }
    if (dst.bitmask !== 0xffffffff) {
      // The complement and +1 run on a full-mask temporary so the
      // destination's raw lane keeps every bit outside its value space.
      const target = new VarImpl(0);
      target.set(lhs);
      target.iand(lhs.bitmask);
      complementInto(target);
      SetDeathsX(target.value, Add, 1, 0, 0xffffffff);
      dst.set(target);
      return;
    }
    if (dst.id !== lhs.id) {
      dst.set(lhs);
    }
    complementInto(dst);
    SetDeathsX(dst.value, Add, 1, 0, 0xffffffff);
    return;
  }

  const lhs = toOperand(expr.lhs);
  const rhs = toOperand(expr.rhs as VarOperand);

  if (typeof lhs === "number" && typeof rhs === "number") {
    switch (op) {
      case "add":
        dst.set((lhs + rhs) >>> 0);
        return;
      case "sub":
        dst.set((lhs - rhs) >>> 0);
        return;
      case "mul":
        dst.set(Math.imul(lhs, rhs) >>> 0);
        return;
      case "div":
        if (rhs === 0) {
          throw new EudslError("EUDSL-DIV-ZERO", "Division by zero");
        }
        dst.set(Math.floor(lhs / rhs) >>> 0);
        return;
      case "mod":
        if (rhs === 0) {
          throw new EudslError("EUDSL-DIV-ZERO", "Modulo by zero");
        }
        dst.set((lhs % rhs) >>> 0);
        return;
      case "bitand":
        dst.set((lhs & rhs) >>> 0);
        return;
      case "bitor":
        dst.set((lhs | rhs) >>> 0);
        return;
      case "bitxor":
        dst.set((lhs ^ rhs) >>> 0);
        return;
      case "shl":
        dst.set(rhs >= 32 ? 0 : ((lhs << rhs) >>> 0));
        return;
      case "shr":
        dst.set(rhs >= 32 ? 0 : (lhs >>> rhs));
        return;
    }
  }

  switch (op) {
    case "add": {
      if (typeof lhs === "number" && rhs instanceof VarImpl) {
        if (dst.id === rhs.id) {
          dst.iadd(lhs);
        } else {
          dst.set(lhs);
          dst.iadd(rhs);
        }
        return;
      }
      if (lhs instanceof VarImpl && typeof rhs === "number") {
        if (dst.id === lhs.id) {
          dst.iadd(rhs);
        } else {
          dst.set(rhs);
          dst.iadd(lhs);
        }
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      if (dst.id === v1.id) {
        dst.iadd(v2);
      } else if (dst.id === v2.id) {
        dst.iadd(v1);
      } else {
        dst.set(0);
        dst.iadd(v1);
        dst.iadd(v2);
      }
      return;
    }

    case "sub": {
      if (lhs instanceof VarImpl && typeof rhs === "number") {
        if (dst.id === lhs.id) {
          dst.isub(rhs);
        } else {
          dst.set(lhs);
          dst.isub(rhs);
        }
        return;
      }
      if (typeof lhs === "number" && rhs instanceof VarImpl) {
        if (dst.id !== rhs.id) {
          dst.set(lhs);
          dst.isub(rhs);
        } else {
          const tmp = new VarImpl(lhs);
          tmp.isub(rhs);
          dst.set(tmp);
        }
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      if (dst.id === v1.id) {
        dst.isub(v2);
      } else if (dst.id !== v2.id) {
        dst.set(v1);
        dst.isub(v2);
      } else {
        const tmp = new VarImpl(0);
        tmp.set(v1);
        tmp.isub(v2);
        dst.set(tmp);
      }
      return;
    }

    case "bitand": {
      if (typeof lhs === "number" || typeof rhs === "number") {
        const v = (typeof lhs === "number" ? rhs : lhs) as VarImpl;
        const c = (typeof lhs === "number" ? lhs : rhs) as number;
        if (dst.id !== v.id) {
          dst.set(v);
        }
        SetDeathsX(dst.value, SetTo, 0, 0, (~c & dst.bitmask) >>> 0);
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      if (v1.id === v2.id) {
        if (dst.id !== v1.id) {
          dst.set(v1);
        }
        return;
      }
      let target: VarImpl;
      if (dst.bitmask !== 0xffffffff) {
        target = new VarImpl(0);
        target.set(v1);
        target.iand(v1.bitmask);
      } else {
        target = dst.id !== v2.id ? dst : new VarImpl(0);
        if (target.id !== v1.id) {
          target.set(v1);
        }
      }
      // The trigger's action is a normal `target &= rhs` kernel whose mask
      // field is patched at run time by the preceding setup acts: the
      // build-time value is only mask 1 (a non-BitCell placeholder for
      // eudkit-opt). Those acts widen the mask to all bits, then store `rhs`
      // into it with Subtract so the effective mask becomes `~rhs`.
      // Registering after the setup acts keeps them ahead of the trigger.
      const t = new Trigger({
        conds: [],
        acts: [SetDeathsX(target.value, SetTo, 0, 0, 1)],
        register: false,
      });
      SetDeathsX(t.act(0).mask, SetTo, 0xffffffff, 0, 0xffffffff);
      emitVarStore(
        v2.id,
        TriggerFieldReloc(t.id, t.act(0).mask.byteOffset),
        Subtract,
      );
      _registerTrigger(t);
      if (target.id !== dst.id) {
        dst.set(target);
      }
      return;
    }

    case "bitor": {
      if (typeof lhs === "number" || typeof rhs === "number") {
        const v = (typeof lhs === "number" ? rhs : lhs) as VarImpl;
        const c = (typeof lhs === "number" ? lhs : rhs) as number;
        if (dst.id !== v.id) {
          dst.set(v);
        }
        const m = (c & dst.bitmask) >>> 0;
        SetDeathsX(dst.value, SetTo, m, 0, m);
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      if (v1.id === v2.id) {
        if (dst.id !== v1.id) {
          dst.set(v1);
        }
        return;
      }
      let target: VarImpl;
      if (dst.bitmask !== 0xffffffff) {
        target = new VarImpl(0);
        target.set(v1);
        target.iand(v1.bitmask);
      } else {
        target = dst.id !== v2.id ? dst : new VarImpl(0);
        if (target.id !== v1.id) {
          target.set(v1);
        }
      }
      // `target |= rhs`: the build-time placeholder under mask 1 is widened
      // at run time by the preceding setup acts, which store `rhs` into the
      // mask field and so select exactly its set bits.
      const t = new Trigger({
        conds: [],
        acts: [SetDeathsX(target.value, SetTo, 0xffffffff, 0, 1)],
        register: false,
      });
      emitVarStore(
        v2.id,
        TriggerFieldReloc(t.id, t.act(0).mask.byteOffset),
        SetTo,
      );
      _registerTrigger(t);
      if (target.id !== dst.id) {
        dst.set(target);
      }
      return;
    }

    case "bitxor": {
      if (typeof lhs === "number" || typeof rhs === "number") {
        const v = (typeof lhs === "number" ? rhs : lhs) as VarImpl;
        const c = ((typeof lhs === "number" ? lhs : rhs) as number) >>> 0;
        if (dst.id !== v.id) {
          dst.set(v);
        }
        if (c !== 0) {
          const evenMask = (0x55555555 & dst.bitmask) >>> 0;
          const oddMask = (0xaaaaaaaa & dst.bitmask) >>> 0;
          const even = (c & evenMask) >>> 0;
          const odd = (c & oddMask) >>> 0;
          if (even !== 0) {
            SetDeathsX(dst.value, Add, even, 0, evenMask);
          }
          if (odd !== 0) {
            SetDeathsX(dst.value, Add, odd, 0, oddMask);
          }
        }
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      if (v1.id === v2.id) {
        dst.set(0);
        return;
      }
      let target: VarImpl;
      if (dst.bitmask !== 0xffffffff) {
        target = new VarImpl(0);
        target.set(v1);
        target.iand(v1.bitmask);
      } else {
        target = dst.id !== v2.id ? dst : new VarImpl(0);
        if (target.id !== v1.id) {
          target.set(v1);
        }
      }
      // Both action values are patched from `rhs` after construction:
      // adding `rhs` under the even/odd masks toggles exactly its set bits.
      const t = new Trigger({
        conds: [],
        acts: [
          SetDeathsX(target.value, Add, 0, 0, 0x55555555),
          SetDeathsX(target.value, Add, 0, 0, 0xaaaaaaaa),
        ],
        register: false,
      });
      emitVarStore(
        v2.id,
        TriggerFieldReloc(t.id, t.act(0).value.byteOffset),
        SetTo,
      );
      emitVarStore(
        v2.id,
        TriggerFieldReloc(t.id, t.act(1).value.byteOffset),
        SetTo,
      );
      _registerTrigger(t);
      if (target.id !== dst.id) {
        dst.set(target);
      }
      return;
    }

    case "shl": {
      if (lhs instanceof VarImpl && typeof rhs === "number") {
        const k = rhs >>> 0;
        if (k === 0) {
          if (dst.id !== lhs.id) dst.set(lhs);
          return;
        }
        if (k >= 32) {
          dst.set(0);
          return;
        }
        if (dst.bitmask !== 0xffffffff) {
          const target = new VarImpl(0);
          target.set(lhs);
          target.iand(lhs.bitmask);
          new Trigger({ conds: [], acts: constShlActs(target, k) });
          dst.set(target);
          return;
        }
        if (dst.id !== lhs.id) dst.set(lhs);
        new Trigger({ conds: [], acts: constShlActs(dst, k) });
        return;
      }
      const v1 = toVar(lhs);
      const v2 = rhs as VarImpl;
      let target: VarImpl;
      if (dst.bitmask !== 0xffffffff) {
        target = new VarImpl(0);
        target.set(v1);
        target.iand(v1.bitmask);
      } else {
        target = dst.id !== v2.id ? dst : new VarImpl(0);
        if (target.id !== v1.id) {
          target.set(v1);
        }
      }
      dynamicShift(target, v2, constShlActs);
      if (target.id !== dst.id) {
        dst.set(target);
      }
      return;
    }

    case "shr": {
      if (lhs instanceof VarImpl && typeof rhs === "number") {
        const k = rhs >>> 0;
        if (k === 0) {
          if (dst.id !== lhs.id) dst.set(lhs);
          return;
        }
        if (k >= 32) {
          dst.set(0);
          return;
        }
        if (dst.bitmask !== 0xffffffff) {
          const target = new VarImpl(0);
          target.set(lhs);
          target.iand(lhs.bitmask);
          new Trigger({ conds: [], acts: constShrActs(target, k) });
          dst.set(target);
          return;
        }
        if (dst.id !== lhs.id) dst.set(lhs);
        new Trigger({ conds: [], acts: constShrActs(dst, k) });
        return;
      }
      const v1 = toVar(lhs);
      const v2 = rhs as VarImpl;
      let target: VarImpl;
      if (dst.bitmask !== 0xffffffff) {
        target = new VarImpl(0);
        target.set(v1);
        target.iand(v1.bitmask);
      } else {
        target = dst.id !== v2.id ? dst : new VarImpl(0);
        if (target.id !== v1.id) {
          target.set(v1);
        }
      }
      dynamicShift(target, v2, constShrActs);
      if (target.id !== dst.id) {
        dst.set(target);
      }
      return;
    }

    case "mul": {
      if (typeof lhs === "number" || typeof rhs === "number") {
        const v = (typeof lhs === "number" ? rhs : lhs) as VarImpl;
        const c = ((typeof lhs === "number" ? lhs : rhs) as number) >>> 0;
        if (c === 0) {
          dst.set(0);
          return;
        }
        if (c === 1) {
          if (dst.id !== v.id) dst.set(v);
          return;
        }
        if (c === 0xffffffff) {
          evalExprInto(dst, v.neg());
          return;
        }
        if ((c & (c - 1)) === 0) {
          evalExprInto(dst, v.shl(Math.clz32(c) ^ 31));
          return;
        }
        // Cycle 4: general k-term shift-add chain behind a measured cost
        // gate. `bits` is ascending; emit the highest term first, then
        // descend. `2k + 5` is the measured upper bound on the chain's
        // executed triggers (k = 2..14, low/high bit extremes).
        const bits: number[] = [];
        for (let b = 0; b < 32; b++) {
          if ((c >>> b) & 1) bits.push(b);
        }
        const window = 33 - (Math.clz32(c & -c) ^ 31); // 1 + (32 - ctz(c))
        if (2 * bits.length + 5 <= window) {
          const acc = dst.id !== v.id ? dst : new VarImpl(0);
          evalExprInto(acc, v.shl(bits[bits.length - 1]));
          for (let i = bits.length - 2; i >= 0; i--) {
            const b = bits[i];
            acc.iadd(b === 0 ? v : v.shl(b));
          }
          if (acc.id !== dst.id) {
            dst.set(acc);
          }
          return;
        }
        const target = dst.id !== v.id ? dst : new VarImpl(0);
        target.set(0);
        for (let b = 0; b < 32; b++) {
          const bit = (1 << b) >>> 0;
          const weight = Math.imul(c, bit) >>> 0;
          if (weight !== 0) {
            new Trigger({
              conds: [DeathsX(v.value, AtLeast, 1, 0, bit)],
              acts: [SetDeathsX(target.value, Add, weight, 0, 0xffffffff)],
            });
          }
        }
        if (target.id !== dst.id) {
          dst.set(target);
        }
        return;
      }
      const v1 = lhs as VarImpl;
      const v2 = rhs as VarImpl;
      dst.set(mulShiftAdd(v1, v2, 32));
      return;
    }

    case "div":
    case "mod": {
      if (lhs instanceof VarImpl && typeof rhs === "number") {
        const c = rhs >>> 0;
        if (c === 0) {
          throw new EudslError("EUDSL-DIV-ZERO", "Division by zero");
        }
        if (c === 1) {
          if (op === "div") {
            if (dst.id !== lhs.id) dst.set(lhs);
          } else {
            dst.set(0);
          }
          return;
        }
        if ((c & (c - 1)) === 0) {
          const k = Math.clz32(c) ^ 31;
          if (op === "div") {
            evalExprInto(dst, lhs.shr(k));
          } else {
            evalExprInto(dst, lhs.bitAnd(c - 1));
          }
          return;
        }
        const rem = new VarImpl(0);
        rem.set(lhs);
        if (op === "div") {
          dst.set(0);
        }
        let maxB = 0;
        while (maxB < 31 && c * 2 ** (maxB + 1) <= 0xffffffff) {
          maxB++;
        }
        for (let b = maxB; b >= 0; b--) {
          const scaled = (c * 2 ** b) >>> 0;
          const bit = (1 << b) >>> 0;
          if (op === "div") {
            new Trigger({
              conds: [Deaths(rem.value, AtLeast, scaled, 0)],
              acts: [
                SetDeathsX(rem.value, Subtract, scaled, 0, 0xffffffff),
                SetDeathsX(dst.value, Add, bit, 0, 0xffffffff),
              ],
            });
          } else {
            new Trigger({
              conds: [Deaths(rem.value, AtLeast, scaled, 0)],
              acts: [SetDeathsX(rem.value, Subtract, scaled, 0, 0xffffffff)],
            });
          }
        }
        if (op === "mod") {
          dst.set(rem);
        }
        return;
      }
      // General variable division/modulo via binary long division
      const num = toVar(lhs);
      const den = rhs as VarImpl;
      const [quot, rem] = divmodLadder(num, den);
      // `quot`/`rem` are fresh, so aliasing (dst === num or dst === den) is
      // safe: the ladder reads the operands and the result lands once, here.
      if (op === "div") {
        dst.set(quot);
      } else {
        dst.set(rem);
      }
      return;
    }
  }
}

/**
 * A comparison recorded as intent, materialized at its use site.
 *
 * Every {@link materialize} re-emits the whole act sequence into the same
 * temporaries: the accumulator is reset to its init first, then the
 * comparison acts accumulate into it. The first call allocates the
 * temporaries and later calls reuse them — both emit the reset, so a
 * `whileLoop` check slot stays valid when the loop tail rebuilds it and a
 * one-shot site (`when`, a raw `Trigger`) still starts each pass clean.
 */
export class DeferredCond implements MaterializableCond {
  readonly target: VarImpl;
  readonly op: "eq" | "ne" | "ge" | "le" | "gt" | "lt";
  readonly rhs: VarOperand;
  readonly neConst?: number;
  private tmp?: VarImpl;
  private rhsTmp?: VarImpl;

  constructor(
    target: VarImpl,
    op: "eq" | "ne" | "ge" | "le" | "gt" | "lt",
    rhs: VarOperand,
    neConst?: number,
  ) {
    this.target = target;
    this.op = op;
    this.rhs = rhs;
    this.neConst = neConst;
  }

  private operand(): number | VarImpl {
    if (this.rhs instanceof Expr) {
      if (!this.rhsTmp) {
        this.rhsTmp = new VarImpl(0);
      }
      evalExprInto(this.rhsTmp, this.rhs); // re-evaluated every pass
      return this.rhsTmp;
    }
    return toOperand(this.rhs);
  }

  /** Temp whose accumulation is reset at every materialization. */
  private temp(init: number): VarImpl {
    if (!this.tmp) {
      this.tmp = new VarImpl(init);
    }
    // The declaration init runs once; every evaluation site needs the reset
    // re-emitted, including the first (one-shot) one.
    this.tmp.set(init);
    return this.tmp;
  }

  /** Temp whose first act each pass is an explicit `set()`. */
  private freshTemp(init: number): VarImpl {
    if (!this.tmp) {
      this.tmp = new VarImpl(init);
    }
    return this.tmp;
  }

  /**
   * Structural complement, exact at the comparison level:
   * `ne` -> `eq`, `eq` -> `ne`, `ge` <-> `lt`, `gt` <-> `le`.
   *
   * A middle-constant `ne(k)` complements to the direct 1C `target == k`
   * (zero extra triggers) instead of materializing a second temp.
   */
  complement(): CondLike {
    if (this.op === "ne" && this.neConst !== undefined) {
      return this.target.eq(this.neConst);
    }
    switch (this.op) {
      case "ne":
        return new DeferredCond(this.target, "eq", this.rhs);
      case "eq":
        return new DeferredCond(this.target, "ne", this.rhs);
      case "ge":
        return new DeferredCond(this.target, "lt", this.rhs);
      case "lt":
        return new DeferredCond(this.target, "ge", this.rhs);
      case "gt":
        return new DeferredCond(this.target, "le", this.rhs);
      case "le":
        return new DeferredCond(this.target, "gt", this.rhs);
    }
  }

  materialize(): HirCond {
    const op = this.operand();
    const target = this.target;
    if (this.neConst !== undefined) {
      const init = (-this.neConst) >>> 0;
      if (target.bitmask === 0xffffffff) {
        const diff = this.temp(init);        // unchanged full-mask path
        diff.iadd(target);
        return Deaths(diff.value, AtLeast, 1, 0);
      }
      // Read the lhs in its value space: `raw & bitmask`, not the raw lane.
      const diff = this.freshTemp(0);
      evalExprInto(diff, new Expr("bitand", target, target.bitmask));
      diff.iadd(init);
      return Deaths(diff.value, AtLeast, 1, 0);
    }
    if (this.op === "eq" || this.op === "ne") {
      const diff = this.temp(0xffffffff);
      saturatingSubInto(diff, op);
      diff.iadd(1);
      diff.iadd(target);
      if (this.op === "eq") {
        return Deaths(diff.value, Exactly, 0, 0);
      }
      return Deaths(diff.value, AtLeast, 1, 0);
    }
    if (this.op === "lt" || this.op === "ge") {
      const diff = this.freshTemp(0);
      diff.set(op);
      saturatingSubInto(diff, target);
      if (this.op === "lt") {
        return Deaths(diff.value, AtLeast, 1, 0);
      }
      return Deaths(diff.value, Exactly, 0, 0);
    }
    const diff = this.freshTemp(0);
    diff.set(target);
    saturatingSubInto(diff, op);
    if (this.op === "le") {
      return Deaths(diff.value, Exactly, 0, 0);
    }
    return Deaths(diff.value, AtLeast, 1, 0);
  }
}

/**
 * Mutable 32-bit scalar variable.
 *
 * `pageIndex`/`slotIndex` are `TriggerPage` coordinates ONLY for
 * page-pinned variables (constructed with storage `"page"`, either by the
 * constructor allocator or by an explicit-id page site such as a struct
 * field or the String base variable). For such variables they report the
 * pinned rank assigned at allocation, which under pinned-first lowering is
 * also the final rank. For every other variable they keep the legacy
 * id-derived values (`id / 64`, `id % 64`) and must NOT be read as page
 * coordinates: non-page variables consume ids but no page slots, and at
 * lowering they become dword/bit cells rather than page slots.
 */
export class VarImpl {
  readonly init: number;
  readonly initialValue: number;
  readonly bitmask: number;

  // Eager backing values. The resolving getters below return them verbatim
  // for eager handles, so the eager path is unchanged; for deferred handles
  // they stay null and every read delegates to the active builder's binding.
  private _id: number | null = null;
  private _pageIndex: number | null = null;
  private _slotIndex: number | null = null;
  private _mask: TriggerFieldRef | null = null;
  private _dest: TriggerFieldRef | null = null;
  private _value: TriggerFieldRef | null = null;
  private _modifier: TriggerFieldRef | null = null;

  // Deferred-mode state (null for eager handles). The shared LazyBinding
  // owns the binding map, resolution count, and first-binding capture;
  // bindings are weak-keyed by builder object so a finished builder (and
  // its binding) can be collected once the module is built.
  private readonly deferred: LazyBinding<VarImpl> | null;
  private readonly deferredStorage?: "page";

  constructor(
    init: VarOperand = 0,
    explicitId?: number,
    explicitMask?: number,
    storage?: "page",
    // INTERNAL deferred flag (globals P1-01 prefactor; not public API, not
    // exported as a user option — the public path is `lazyVar`): when set,
    // skip allocation, field-ref creation, and any init statement. The
    // handle resolves its identity on first use through a per-builder
    // binding (see `resolve`).
    deferred = false,
  ) {
    const isConst = typeof init === "number";
    const constVal = isConst ? requirePlainInt(init, "Var init") >>> 0 : 0;
    const maskVal = explicitMask !== undefined ? (explicitMask >>> 0) : 0xffffffff;
    this.bitmask = maskVal;
    this.init = constVal;
    this.initialValue = constVal;
    this.deferred = deferred ? new LazyBinding<VarImpl>("Var") : null;
    if (deferred) {
      if (explicitId !== undefined) {
        throw new EudslError(
          "EUDSL-TYPE",
          "deferred Var cannot carry an explicit id",
        );
      }
      if (!isConst) {
        // D4 (const-only init): a deferred `.set()` would be scope-shifted
        // by a conditional first use, so non-const init fails fast here
        // instead of silently dropping the initialization.
        throw new EudslError(
          "EUDSL-TYPE",
          "deferred Var init must be a constant number",
        );
      }
      this.deferredStorage = storage;
      return;
    }
    const id =
      explicitId !== undefined
        ? explicitId
        : allocVarSlot(constVal, maskVal, storage);
    this._id = id;
    if (storage === "page") {
      // The page allocation that produced this variable (the constructor's
      // own allocVarSlot, or the call site's allocVarSlot for an explicit-id
      // page variable) has already incremented the page counter, so the rank
      // just allocated is the current counter minus one. Read it only: this
      // must never allocate a second page slot. Outside buildModule the page
      // counter falls back to the raw counter, so the rank equals the id and
      // behaviour is unchanged there.
      const rank = getNextPageSlot() - 1;
      this._pageIndex = Math.floor(rank / ACT_COUNT);
      this._slotIndex = rank % ACT_COUNT;
    } else {
      this._pageIndex = Math.floor(id / ACT_COUNT);
      this._slotIndex = id % ACT_COUNT;
    }

    this._mask = TriggerFieldRef.forVar(id, 0, 4, maskVal);
    this._dest = TriggerFieldRef.forVar(id, 16, 4, 0);
    this._value = TriggerFieldRef.forVar(id, 20, 4, constVal);
    this._modifier = TriggerFieldRef.forVar(id, 27, 1, SetTo);

    if (!isConst) {
      this.set(init);
    }
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving identity reads: this for eager handles, the
   * active builder's binding for deferred ones.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * eager construction varies here.
   */
  private resolve(): VarImpl {
    if (!this.deferred) {
      return this;
    }
    return this.deferred.resolve(
      () =>
        new VarImpl(
          this.init,
          undefined,
          this.bitmask,
          this.deferredStorage,
        ),
    );
  }

  get id(): number {
    return this.resolve()._id!;
  }

  get pageIndex(): number {
    return this.resolve()._pageIndex!;
  }

  get slotIndex(): number {
    return this.resolve()._slotIndex!;
  }

  get mask(): TriggerFieldRef {
    return this.resolve()._mask!;
  }

  get dest(): TriggerFieldRef {
    return this.resolve()._dest!;
  }

  get value(): TriggerFieldRef {
    return this.resolve()._value!;
  }

  get modifier(): TriggerFieldRef {
    return this.resolve()._modifier!;
  }

  copy(): VarImpl {
    return Var(this);
  }

  // -------------------------------------------------------------------------
  // Declarative Action Constructors
  //
  // Contract (dual channel — read before changing): every `act_*` call does
  // TWO things. It RETURNS the HIR action (so callers can inspect it or pass
  // it to `new Trigger({ acts: [...] })`), AND it queues that same action
  // into the active builder (`buildAct` in `hir.ts`), so a bare statement
  // such as `v.act_add(1)` still emits — via an anonymous trigger — without
  // being passed anywhere. `Trigger` removes the queued copy of any action
  // it receives explicitly (`consumeQueuedActs`), so the same action is never
  // emitted twice.
  //
  // Two consequences follow. First, a bare `act_*` call OUTSIDE `buildModule`
  // queues nowhere: the returned action is the only record and the action
  // never runs. That is intentional — tests inspect actions outside a builder
  // (see `ts/tests/test_dat.ts`) — so no error is raised for it; do not
  // "fix" this by raising here. Second, the imperative forms (`set`/`iadd`/
  // `isub` with a plain number) work by calling `act_*` and DISCARDING the
  // return value, relying on the queue side effect alone. Removing the queue
  // would silently break every statement-form site in the codebase
  // (`blockio.ts`, `string.ts`, `var.ts` itself); splitting the channels is
  // a migration, not a cleanup.
  // -------------------------------------------------------------------------

  act_set(rhs: number): HirAct {
    const v = (requirePlainInt(rhs, "Var.set operand") & this.bitmask) >>> 0;
    return SetDeathsX(this.value, SetTo, v, 0, this.bitmask);
  }

  act_add(rhs: number): HirAct {
    const v = (requirePlainInt(rhs, "Var.iadd operand") & this.bitmask) >>> 0;
    return SetDeathsX(this.value, Add, v, 0, this.bitmask);
  }

  act_sub(rhs: number): HirAct {
    return this.act_add((-requirePlainInt(rhs, "Var.isub operand")) >>> 0);
  }

  // -------------------------------------------------------------------------
  // In-place Mutations
  // -------------------------------------------------------------------------

  set(rhs: VarOperand): void {
    if (typeof rhs === "number") {
      this.act_set(rhs);
      return;
    }
    if (rhs instanceof VarImpl) {
      emitVarTransfer(this.id, rhs.id, SetTo);
      return;
    }
    if (!(rhs instanceof Expr)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `Var.set operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
      );
    }
    evalExprInto(this, rhs);
  }

  iadd(rhs: VarOperand): void {
    if (typeof rhs === "number") {
      this.act_add(rhs);
      return;
    }
    if (rhs instanceof VarImpl) {
      emitVarTransfer(this.id, rhs.id, Add);
      return;
    }
    // A `ReadonlyVar` is an interface-level view of a `VarImpl`; the runtime
    // object was already handled by the branch above, so only `Expr` remains.
    if (!(rhs instanceof Expr)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `Var.iadd operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
      );
    }
    const expr = rhs;
    if (expr.op === "add") {
      this.iadd(expr.lhs);
      this.iadd(expr.rhs as VarOperand);
      return;
    }
    const tmp = toVar(expr);
    emitVarTransfer(this.id, tmp.id, Add);
  }

  isub(rhs: VarOperand): void {
    if (typeof rhs === "number") {
      // eudplib parity: `a - n` is wrapping addition of the two's
      // complement, so negative constants work too.
      this.act_sub(rhs);
      return;
    }
    if (rhs instanceof VarImpl) {
      // eudplib's Var form is `(self + 1) + (0xFFFFFFFF - other)`.
      // Materialize the complement before mutating `self`: `v.isub(v)`
      // and `dst.isub(rhs)` with `dst === rhs` must read the original.
      const inv = new VarImpl(0);
      inv.set(0xffffffff);
      saturatingSubInto(inv, rhs);
      this.iadd(1);
      this.iadd(inv);
      return;
    }
    if (!(rhs instanceof Expr)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `Var.isub operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
      );
    }
    this.isub(toVar(rhs));
  }

  imul(rhs: VarOperand): void {
    evalExprInto(this, this.mul(rhs));
  }

  idiv(rhs: VarOperand): void {
    evalExprInto(this, this.div(rhs));
  }

  imod(rhs: VarOperand): void {
    evalExprInto(this, this.mod(rhs));
  }

  iand(rhs: VarOperand): void {
    if (typeof rhs === "number") {
      SetDeathsX(this.value, SetTo, 0, 0, (~requirePlainInt(rhs, "Var.iand operand")) >>> 0);
      return;
    }
    evalExprInto(this, this.bitAnd(rhs));
  }

  ior(rhs: VarOperand): void {
    if (typeof rhs === "number") {
      const v = requirePlainInt(rhs, "Var.ior operand") >>> 0;
      SetDeathsX(this.value, SetTo, v, 0, v);
      return;
    }
    evalExprInto(this, this.bitOr(rhs));
  }

  ixor(rhs: VarOperand): void {
    evalExprInto(this, this.bitXor(rhs));
  }

  ishl(rhs: VarOperand): void {
    evalExprInto(this, this.shl(rhs));
  }

  ishr(rhs: VarOperand): void {
    evalExprInto(this, this.shr(rhs));
  }

  // -------------------------------------------------------------------------
  // Pure Expressions
  // -------------------------------------------------------------------------

  add(rhs: VarOperand): Expr {
    return new Expr("add", this, rhs);
  }

  sub(rhs: VarOperand): Expr {
    return new Expr("sub", this, rhs);
  }

  mul(rhs: VarOperand): Expr {
    return new Expr("mul", this, rhs);
  }

  div(rhs: VarOperand): Expr {
    return new Expr("div", this, rhs);
  }

  mod(rhs: VarOperand): Expr {
    return new Expr("mod", this, rhs);
  }

  divmod(rhs: VarOperand): [VarImpl, VarImpl] {
    return divmodOperands(this, rhs);
  }

  bitAnd(rhs: VarOperand): Expr {
    return new Expr("bitand", this, rhs);
  }

  bit_and(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  and(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  band(rhs: VarOperand): Expr {
    return this.bitAnd(rhs);
  }

  bitOr(rhs: VarOperand): Expr {
    return new Expr("bitor", this, rhs);
  }

  bit_or(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  or(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  bor(rhs: VarOperand): Expr {
    return this.bitOr(rhs);
  }

  bitXor(rhs: VarOperand): Expr {
    return new Expr("bitxor", this, rhs);
  }

  bit_xor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  xor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  bxor(rhs: VarOperand): Expr {
    return this.bitXor(rhs);
  }

  bitNot(): Expr {
    return new Expr("bitnot", this);
  }

  bit_not(): Expr {
    return this.bitNot();
  }

  invert(): Expr {
    return this.bitNot();
  }

  bnot(): Expr {
    return this.bitNot();
  }

  shl(rhs: VarOperand): Expr {
    return new Expr("shl", this, rhs);
  }

  lshift(rhs: VarOperand): Expr {
    return this.shl(rhs);
  }

  shr(rhs: VarOperand): Expr {
    return new Expr("shr", this, rhs);
  }

  rshift(rhs: VarOperand): Expr {
    return this.shr(rhs);
  }

  neg(): Expr {
    return new Expr("neg", this);
  }

  // -------------------------------------------------------------------------
  // Comparisons (1C direct Deaths for constants; temp diff for variable RHS)
  // -------------------------------------------------------------------------

  /** This `Var`'s comparison kernel: its mask, no byte shift. */
  valueSpace(): FieldValueSpace {
    return new FieldValueSpace(this.bitmask, 0, 4);
  }

  eq(rhs: number): HirCond;
  eq(rhs: VarOperand): CondLike;
  eq(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.eq operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "eq", rhs);
    }
    return applyComparison(this.valueSpace(), "eq", rhs, () => this.value, {
      what: "Var.eq operand",
      fullMaskDeaths: true,
    }) as HirCond;
  }

  /**
   * Inequality. Deliberately returns `CondLike`: a numeric middle constant
   * yields a `DeferredCond` (and a non-constant RHS always did), because the
   * comparison must be materialized at its evaluation site. Callers that need
   * a `HirCond` value can use the extreme forms (`ne(0)`, `ne(max)`, which are
   * direct 1T conds) or invert `eq`.
   */
  ne(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.ne operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "ne", rhs);
    }
    return applyComparison(this.valueSpace(), "ne", rhs, () => this.value, {
      what: "Var.ne operand",
      fullMaskDeaths: true,
      defer: (constant) => new DeferredCond(this, "ne", rhs, constant),
    });
  }

  ge(rhs: number): HirCond;
  ge(rhs: VarOperand): CondLike;
  ge(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.ge operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "ge", rhs);
    }
    return applyComparison(this.valueSpace(), "ge", rhs, () => this.value, {
      what: "Var.ge operand",
      fullMaskDeaths: true,
    }) as HirCond;
  }

  le(rhs: number): HirCond;
  le(rhs: VarOperand): CondLike;
  le(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.le operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "le", rhs);
    }
    return applyComparison(this.valueSpace(), "le", rhs, () => this.value, {
      what: "Var.le operand",
      fullMaskDeaths: true,
    }) as HirCond;
  }

  gt(rhs: number): HirCond;
  gt(rhs: VarOperand): CondLike;
  gt(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.gt operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "gt", rhs);
    }
    return applyComparison(this.valueSpace(), "gt", rhs, () => this.value, {
      what: "Var.gt operand",
      fullMaskDeaths: true,
    }) as HirCond;
  }

  lt(rhs: number): HirCond;
  lt(rhs: VarOperand): CondLike;
  lt(rhs: VarOperand): CondLike {
    if (typeof rhs !== "number") {
      if (!(rhs instanceof VarImpl || rhs instanceof Expr)) {
        throw new EudslError(
          "EUDSL-TYPE",
          `Var.lt operand must be an integer constant, a Var, or an Expr; got ${describeValue(rhs)}`,
        );
      }
      return new DeferredCond(this, "lt", rhs);
    }
    return applyComparison(this.valueSpace(), "lt", rhs, () => this.value, {
      what: "Var.lt operand",
      fullMaskDeaths: true,
    }) as HirCond;
  }

  atLeast(rhs: number): HirCond;
  atLeast(rhs: VarOperand): CondLike;
  atLeast(rhs: VarOperand): CondLike {
    return this.ge(rhs);
  }

  at_least(rhs: number): HirCond;
  at_least(rhs: VarOperand): CondLike;
  at_least(rhs: VarOperand): CondLike {
    return this.ge(rhs);
  }

  atMost(rhs: number): HirCond;
  atMost(rhs: VarOperand): CondLike;
  atMost(rhs: VarOperand): CondLike {
    return this.le(rhs);
  }

  at_most(rhs: number): HirCond;
  at_most(rhs: VarOperand): CondLike;
  at_most(rhs: VarOperand): CondLike {
    return this.le(rhs);
  }

  exactly(rhs: number): HirCond;
  exactly(rhs: VarOperand): CondLike;
  exactly(rhs: VarOperand): CondLike {
    return this.eq(rhs);
  }
}

export const Var = new Proxy(VarImpl, {
  apply(_target, _thisArg, argArray: [VarOperand?, number?, number?, "page"?]) {
    return new VarImpl(...argArray);
  },
}) as unknown as typeof VarImpl &
  ((
    init?: VarOperand,
    id?: number,
    mask?: number,
    storage?: "page",
  ) => VarImpl);

export type Var = VarImpl;

/**
 * Declare a lazy `Var` (globals P1-02): a handle constructible anywhere
 * (no builder needed) that resolves to a fully eager binding on first
 * resolving use inside `buildModule`, one binding per builder.
 *
 * Only constant-number init is accepted: a non-const init (another `Var`,
 * an `Expr`) throws `EUDSL-TYPE` eagerly here, because a deferred `.set()`
 * would be scope-shifted by a conditional first use (design D4). There is
 * no explicit-id form: ids are builder-assigned in first-use order. A
 * never-used handle emits nothing (no decl, no id, no statement, D5).
 * This factory is the only documented path to deferred mode; the
 * internal constructor flag stays undocumented.
 */
export function lazyVar(
  init: VarOperand = 0,
  mask?: number,
  storage?: "page",
): VarImpl {
  warnLazyInBuilder();
  return new VarImpl(init, undefined, mask, storage, true);
}

export abstract class ReadonlyVar {
  static [Symbol.hasInstance](instance: unknown): boolean {
    return instance instanceof VarImpl;
  }
}

/**
 * Compile-time read-only view of a `Var` for the cross-file owner convention
 * (#96/#97).
 *
 * The type exposes scalar reads (`.id`, `.init`, `.bitmask`, ...), conditions,
 * pure expressions, and format/position arguments. It deliberately omits the
 * raw `value`/`dest`/`mask`/`modifier` field refs (their `set`/`iadd`/... methods
 * are mutations), `copy()`, and every in-place mutator.
 *
 * TypeScript enforcement is type-level only: `asReadonly` is a cast and the
 * runtime value remains the owner's mutable `VarImpl`. Python wraps the `Var`
 * and rejects mutation at runtime.
 */
export interface ReadonlyVar {
  readonly id: number;
  readonly pageIndex: number;
  readonly slotIndex: number;
  readonly init: number;
  readonly initialValue: number;
  readonly bitmask: number;

  eq(rhs: number): HirCond;
  eq(rhs: VarOperand): CondLike;
  /**
   * Inequality. Deliberately returns `CondLike`: a numeric middle constant
   * yields a `DeferredCond` (and a non-constant RHS always did), because the
   * comparison must be materialized at its evaluation site. Callers that need
   * a `HirCond` value can use the extreme forms (`ne(0)`, `ne(max)`, which are
   * direct 1T conds) or invert `eq`.
   */
  ne(rhs: VarOperand): CondLike;
  ge(rhs: number): HirCond;
  ge(rhs: VarOperand): CondLike;
  le(rhs: number): HirCond;
  le(rhs: VarOperand): CondLike;
  gt(rhs: number): HirCond;
  gt(rhs: VarOperand): CondLike;
  lt(rhs: number): HirCond;
  lt(rhs: VarOperand): CondLike;
  atLeast(rhs: number): HirCond;
  atLeast(rhs: VarOperand): CondLike;
  at_least(rhs: number): HirCond;
  at_least(rhs: VarOperand): CondLike;
  atMost(rhs: number): HirCond;
  atMost(rhs: VarOperand): CondLike;
  at_most(rhs: number): HirCond;
  at_most(rhs: VarOperand): CondLike;
  exactly(rhs: number): HirCond;
  exactly(rhs: VarOperand): CondLike;

  add(rhs: VarOperand): Expr;
  sub(rhs: VarOperand): Expr;
  mul(rhs: VarOperand): Expr;
  div(rhs: VarOperand): Expr;
  mod(rhs: VarOperand): Expr;
  divmod(rhs: VarOperand): [VarImpl, VarImpl];
  bitAnd(rhs: VarOperand): Expr;
  bit_and(rhs: VarOperand): Expr;
  and(rhs: VarOperand): Expr;
  band(rhs: VarOperand): Expr;
  bitOr(rhs: VarOperand): Expr;
  bit_or(rhs: VarOperand): Expr;
  or(rhs: VarOperand): Expr;
  bor(rhs: VarOperand): Expr;
  bitXor(rhs: VarOperand): Expr;
  bit_xor(rhs: VarOperand): Expr;
  xor(rhs: VarOperand): Expr;
  bxor(rhs: VarOperand): Expr;
  bitNot(): Expr;
  bit_not(): Expr;
  invert(): Expr;
  bnot(): Expr;
  shl(rhs: VarOperand): Expr;
  lshift(rhs: VarOperand): Expr;
  shr(rhs: VarOperand): Expr;
  rshift(rhs: VarOperand): Expr;
  neg(): Expr;
}

/**
 * Wrap a `Var` or imported lazy handle in a compile-time read-only view (#96/#97).
 *
 * Removes mutators, raw field refs (`value`/`dest`/`mask`/`modifier`), and
 * `copy()` from the type while preserving scalar reads, conditions, pure
 * expressions, and format/position arguments. This is a type-only cast: the
 * runtime object is still the mutable `VarImpl` (see ADR 0005 for the
 * TS/Python enforcement difference).
 */
export function asReadonly(v: VarImpl | ReadonlyVar): ReadonlyVar {
  return v as unknown as ReadonlyVar;
}

export const as_readonly = asReadonly;

