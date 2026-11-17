/**
 * Zero-storage typed layout views (`MemView`, `CondView`, `ActView`, `CUnit`, `CSprite`),
 * StarCraft `EPD` address helpers, and `MemCond` in-place comparisons.
 * Fields are accessed directly as properties (`u.hp`, `u.shield`, `trig.act(0).value`).
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { EudslError } from "./errors.ts";
import {
  Add,
  Always,
  AtLeast,
  AtMost,
  CurrentPlayer,
  Deaths,
  DeathsX,
  Exactly,
  Never,
  SetDeaths,
  SetDeathsX,
  SetTo,
  Subtract,
  TriggerFieldReloc,
  emitVarStore,
  type HirAct,
  type HirCond,
} from "./hir.ts";
import {
  Trigger,
  _getActiveModuleBuilder,
  _registerTrigger,
  type CondLike,
  type MaterializableCond,
} from "./trigger.ts";
import { when, whileLoop } from "./temporal.ts";
import { Var, VarImpl, saturatingSubInto } from "./var.ts";
import { Expr, ReadonlyVar, toVar, type VarOperand } from "./var.ts";
import { registerDetachedPlan, type GatePlan } from "./conditions.ts";

export const EPD_BASE = 0x58a364;
export const EPD_CURRENT_PLAYER = 203155;
export const CURRENT_PLAYER = 13;

/**
 * Compute the signed StarCraft EPD index for a 4-byte-aligned address:
 * `EPD(addr) = (addr - 0x58A364) / 4`.
 */
export function EPD(addr: number): number {
  return Math.floor(((addr >>> 0) - EPD_BASE) / 4);
}

export const epd = EPD;

// ---------------------------------------------------------------------------
// CUnit slot pool
// ---------------------------------------------------------------------------
//
// The engine's CUnit array is 1700 slots of 336 bytes each. Slots are packed
// back-to-back, so consecutive CUnits start 84 dwords apart. In the shifted
// pointer space `S = A >>> 2` the slot starts therefore span
// `[0x16732a, 0x18a0a6]` and the last slot starts at byte `0x628298`.
//
// Pool size per BWAPI (unit indices `0 <= i < 1700`) and the euddraft wiki
// (1700 units spanning `0x59cca8`-`0x6283e7`); the previous 1699-slot
// model was wrong by one (it excluded the last valid unit).

/** Byte address of the first CUnit slot. */
export const CUNIT_POOL_BASE = 0x59cca8;
/** Bytes per CUnit slot (`schema/layouts/cunit.json`). */
export const CUNIT_SLOT_SIZE = 336;
/** Dwords per CUnit slot: **the stride that matters** for the EPD bound. */
export const CUNIT_SLOT_EPDS = 84;
/** Number of CUnit slots in the engine pool. */
export const CUNIT_SLOT_COUNT = 1700;
/** EPD of the first CUnit slot (`EPD(0x59cca8)`). */
export const CUNIT_BASE_EPD = EPD(CUNIT_POOL_BASE);
/**
 * EPD of the last CUnit slot's first dword (`EPD(0x628298)`).
 *
 * Derived, not `CUNIT_BASE_EPD + CUNIT_SLOT_COUNT - 1`: each slot is 84
 * dwords, so a naive `+1699` lands at EPD 20724 and rejects every real unit
 * after the 21st slot.
 */
export const CUNIT_LAST_EPD =
  CUNIT_BASE_EPD + CUNIT_SLOT_EPDS * (CUNIT_SLOT_COUNT - 1);
/** Last byte of the CUnit pool, inclusive. */
export const CUNIT_POOL_END = 0x6283e7;

/** Seed restoring the invariant low bits of the shifted pointer space. */
const CUNIT_POINTER_SEED = 0x100002;
/** One-shot bias from the shifted pointer space to the EPD space. */
const CUNIT_POINTER_BIAS = 0xffe9d727;
/** CUnit-slot address bits 4-21, read as shifted-pointer bits 2-19. */
const CUNIT_POINTER_MASK = 0x3ffff0;

/**
 * Loud structured DSL error carrying a canonical error code (e.g. `EUDSL-BOUNDS`).
 *
 * Re-exported from the leaf `errors` module so condition combinators can raise
 * it without a `conditions` -> `view` cycle.
 */
export { EudslError };

/**
 * Render an arbitrary operand for an error message.
 *
 * This is the pre-dedupe leaf rendering: strings are JSON-quoted, numbers
 * render bare, and everything else is named by `typeof` (`a boolean`,
 * `a object`, `a undefined`, ...). FE0 briefly routed every operand through
 * a `null`/`undefined`-aware variant, changing the messages callers saw.
 */
export function describeValue(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") return String(value);
  return `a ${typeof value}`;
}

/**
 * Validate an integer operand domain (ADR 0019): a plain integer is the only
 * numeric domain; magnitude is not part of the check (32-bit normalisation
 * happens downstream). Everything else raises `EUDSL-TYPE` at the call.
 */
export function requirePlainInt(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new EudslError("EUDSL-TYPE", `${what} must be a plain integer, got ${describeValue(value)}`);
  }
  return value;
}

/**
 * Reject an absolute dword address that is not 4-byte aligned.
 *
 * A whole-dword read/write names one of `EPD_BASE + 4 * epd`. An unaligned
 * address silently rounds to the containing dword, and the two conversions in
 * the tree round in different directions: `EPD()` floors while
 * `eudkit_lir::epd_u32` truncates (they agree only on aligned input, which is
 * why alignment is the seam's job). Measured with `0x57F0B2`: the gate read
 * the dword at `0x57F0B0` and the store wrote the dword at `0x57F0B4` with no
 * error anywhere.
 *
 * This is *not* a guard for byte-lane access. `DatFieldHandle`,
 * `SetStringDword` and mode-1 string relocations deliberately floor an
 * unaligned field address and mask the byte lane; that names a byte inside
 * the containing dword, not a whole-dword access, so it must not be routed
 * through this check (and `EPD()` itself stays unguarded).
 */
export function requireAlignedDwordAddress(address: number, subject: string): void {
  const folded = address >>> 0;
  if (folded & 3) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `${subject} 0x${folded.toString(16)} is not 4-byte aligned`,
    );
  }
}

/**
 * Structured DSL warning carrying a canonical warning code (e.g. `EUDSL-WARN-PERF`).
 */
export class EudslWarning {
  readonly code: string;
  readonly message: string;

  constructor(code: string, message: string) {
    this.code = code;
    this.message = `${code}: ${message}`;
  }

  toString(): string {
    return this.message;
  }
}

export type WarningListener = (warning: { code: string; message: string }) => void;

let warningListeners: WarningListener[] = [];

/**
 * Register a listener for non-fatal DSL compiler warnings.
 * Returns an unsubscribe function.
 */
export function onWarning(fn: WarningListener): () => void {
  warningListeners.push(fn);
  return () => {
    warningListeners = warningListeners.filter((l) => l !== fn);
  };
}

export function emitWarning(code: string, message: string): void {
  const formatted = `${code}: ${message}`;
  for (const listener of [...warningListeners]) {
    listener({ code, message: formatted });
  }
  if (typeof process !== "undefined" && typeof process.emitWarning === "function") {
    process.emitWarning(formatted, { code });
  }
}

/**
 * One canonical EUDSL-WARN-SCOPE message for every lazy* factory, quoted
 * verbatim by ADR 0005, GLOSSARY.md, and the cross-frontend tests.
 */
export const WARN_SCOPE_CODE = "EUDSL-WARN-SCOPE";
export const WARN_SCOPE_MESSAGE =
  "lazy* declared inside buildModule; use eager Var for in-module declarations";

/**
 * Emit the canonical `EUDSL-WARN-SCOPE` warning when a `lazy*` factory is
 * called inside `buildModule`.
 *
 * Warning dispatch lives in this module, next to `EudslWarning` and
 * `emitWarning`, so every declaration module can import it without a cycle
 * (the string module used to own it, but string is itself a lazy factory
 * module).
 */
export function warnLazyInBuilder(): void {
  if (_getActiveModuleBuilder()) {
    emitWarning(WARN_SCOPE_CODE, WARN_SCOPE_MESSAGE);
  }
}

export type FieldWidth = 1 | 2 | 4;

export interface FieldSpec {
  readonly offset: number;
  readonly size: FieldWidth;
  readonly mask?: number;
  readonly pointer?: string;
  readonly composite?: string;
  readonly aliases?: readonly string[];
}

export interface MemViewSchema<K extends string = string> {
  readonly name: string;
  readonly size: number;
  readonly fields: Readonly<Record<K, FieldSpec>>;
}

export function defineMemView<K extends string>(
  name: string,
  size: number,
  fields: Record<K, FieldSpec>,
): MemViewSchema<K> {
  return Object.freeze({
    name,
    size,
    fields: Object.freeze({ ...fields }),
  });
}

/** Built-in 20-byte (5-EPD) condition slot view (`IIIHBBBBH`). */
export const CondView = defineMemView("CondView", 20, {
  mask: { offset: 0, size: 4 },
  player: { offset: 4, size: 4 },
  amount: { offset: 8, size: 4 },
  unit: { offset: 12, size: 2 },
  comparison: { offset: 14, size: 1 },
  condtype: { offset: 15, size: 1 },
  restype: { offset: 16, size: 1 },
  flags: { offset: 17, size: 1 },
  eudx: { offset: 18, size: 2 },
});

/** Built-in 32-byte (8-EPD) action slot view (`IIIIIIHBBBBH`). */
export const ActView = defineMemView("ActView", 32, {
  mask: { offset: 0, size: 4 },
  strid: { offset: 4, size: 4 },
  wavid: { offset: 8, size: 4 },
  time: { offset: 12, size: 4 },
  dest: { offset: 16, size: 4 },
  value: { offset: 20, size: 4 },
  unit: { offset: 24, size: 2 },
  acttype: { offset: 26, size: 1 },
  modifier: { offset: 27, size: 1 },
  flags: { offset: 28, size: 1 },
  pad: { offset: 29, size: 1 },
  eudx: { offset: 30, size: 2 },
});

// ---------------------------------------------------------------------------
// TriggerFieldRef: bound field in Trigger / TriggerPage
// ---------------------------------------------------------------------------

export class TriggerFieldRef {
  readonly triggerId: number;
  readonly varId?: number;
  readonly byteOffset: number;
  readonly alignedByteOffset: number;
  readonly epdOffset: number;
  readonly subp: 0 | 1 | 2 | 3;
  readonly shift: 0 | 8 | 16 | 24;
  readonly size: FieldWidth;
  readonly bitMask: number;
  readonly initialValue: number;
  /** No symbolic name: a raw field reference is addressed by offset. */
  readonly fieldName?: undefined;

  constructor(
    triggerId: number,
    byteOffset: number,
    size: FieldWidth,
    initialValue = 0,
    varId?: number,
    customMask?: number,
  ) {
    this.triggerId = triggerId;
    if (varId !== undefined) {
      this.varId = varId;
    }
    this.byteOffset = byteOffset;
    const subp = (byteOffset & 3) as 0 | 1 | 2 | 3;
    this.alignedByteOffset = byteOffset - subp;
    this.epdOffset = Math.floor(this.alignedByteOffset / 4);
    this.subp = subp;
    this.shift = (subp * 8) as 0 | 8 | 16 | 24;
    this.size = size;
    const rawMask = size === 4 ? 0xffffffff : size === 2 ? 0xffff : 0xff;
    this.bitMask = customMask !== undefined ? customMask >>> 0 : ((rawMask << this.shift) >>> 0);
    this.initialValue = initialValue >>> 0;
  }

  static forVar(
    varId: number,
    byteOffset: number,
    size: FieldWidth,
    initialValue = 0,
  ): TriggerFieldRef {
    return new TriggerFieldRef(-1, byteOffset, size, initialValue, varId);
  }

  /** This field's comparison kernel (the one source of comparison truth). */
  valueSpace(): FieldValueSpace {
    return new FieldValueSpace(this.bitMask, this.shift, this.size);
  }

  /**
   * `act_*` here shares the dual-channel contract documented on `Var`
   * (`var.ts`): the returned action is ALSO queued into the active builder
   * by `SetDeathsX` (`buildAct`), and a bare call outside `buildModule`
   * queues nowhere (see `ts/tests/test_dat.ts`).
   */
  act_set(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    return SetDeathsX(this, SetTo, val, 0, this.bitMask);
  }

  act_add(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    return SetDeathsX(this, Add, val, 0, this.bitMask);
  }

  act_sub(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    return SetDeathsX(this, Subtract, val, 0, this.bitMask);
  }

  set(rhs: number): HirAct {
    return this.act_set(rhs);
  }

  iadd(rhs: number): HirAct {
    return this.act_add(rhs);
  }

  isub(rhs: number): HirAct {
    return this.act_sub(rhs);
  }

  eq(rhs: number): HirCond {
    return applyComparison(this.valueSpace(), "eq", rhs, () => this, {
      what: "field comparison",
    }) as HirCond;
  }

  ge(rhs: number): HirCond {
    return applyComparison(this.valueSpace(), "ge", rhs, () => this.condTarget(), {
      what: "field comparison",
    }) as HirCond;
  }

  le(rhs: number): HirCond {
    return applyComparison(this.valueSpace(), "le", rhs, () => this.condTarget(), {
      what: "field comparison",
    }) as HirCond;
  }

  gt(rhs: number): HirCond {
    return applyComparison(this.valueSpace(), "gt", rhs, () => this.condTarget(), {
      what: "field comparison",
    }) as HirCond;
  }

  lt(rhs: number): HirCond {
    return applyComparison(this.valueSpace(), "lt", rhs, () => this.condTarget(), {
      what: "field comparison",
    }) as HirCond;
  }

  ne(rhs: number): CondLike {
    return applyComparison(
      this.valueSpace(),
      "ne",
      rhs,
      () => this.condTarget(),
      {
        what: "field comparison",
        fieldName: this.fieldName,
        defer: (constant) => new DeferredNe(this, constant),
      },
    );
  }

  /**
   * Condition target for this surface — the reference itself.
   *
   * `MemFieldHandle.condTarget()` resolves an absolute EPD; a raw field
   * reference already names its slot directly, so the deferred marker can
   * treat both surfaces uniformly.
   *
   * @internal Seam helper: the `TriggerFieldRef` analogue of
   * {@link MemFieldHandle.condTarget}.
   */
  condTarget(): TriggerFieldRef {
    return this;
  }

  read(): VarImpl {
    const numBits = this.size * 8;
    const res = Var(0);
    // Reset the accumulator every time this read site executes (reads inside
    // loops would otherwise accumulate into the previous result).
    new Trigger({
      conds: [],
      acts: [res.act_set(0)],
      preserved: true,
    });
    emitBitTestRead(this, numBits, this.shift, res, this.bitMask);
    return res;
  }

  valueOf(): number {
    return this.initialValue;
  }

  [Symbol.toPrimitive](): number {
    return this.initialValue;
  }
}

/**
 * Reject a custom mask whose set bits fall below the field's byte shift.
 */
function validateMaskShift(mask: number, shift: number, fieldName?: string): void {
  if (shift > 0 && (mask & ((1 << shift) - 1)) !== 0) {
    const label = fieldName ?? "field";
    throw new EudslError(
      "EUDSL-BOUNDS",
      `mask ${mask.toString(16)} on ${label} has set bits below shift ${shift}; ` +
        "the accumulator cannot represent them in field units",
    );
  }
}

/**
 * Fold an ordering comparison no value in the field's space can satisfy.
 *
 * `c` is the constant normalised to 32 bits. Returns `"never"` (the
 * comparison can never hold), `"always"` (it holds for every value), or
 * `undefined` (emit the comparison). Only constants *outside* the space fold,
 * so well-formed HIR stays byte-identical.
 *
 * The folds compare numerically (`c > maxVal`), not with `c & ~maxVal`: on a
 * non-contiguous mask a constant naming a representable threshold must stay
 * eager.
 */
function foldOrdering(
  op: "ge" | "le" | "gt" | "lt",
  c: number,
  maxVal: number,
): "never" | "always" | undefined {
  if (op === "ge") return c > maxVal ? "never" : undefined;
  if (op === "le") return c > maxVal ? "always" : undefined;
  if (op === "gt") return c >= maxVal ? "never" : undefined;
  if (op === "lt") {
    if (c === 0) return "never";
    return c > maxVal ? "always" : undefined;
  }
  throw new Error(`not an ordering op: ${op}`);
}

/**
 * Largest value representable in the field's units, derived from the mask.
 *
 * A field's value space is the set of bit patterns inside `bitMask`,
 * interpreted as field units (`raw >> shift`). Because any bit-subset of the
 * mask is representable, its largest member is the whole mask shifted down —
 * including for *straddling* fields, where that is smaller than the nominal
 * width.
 *
 * `size` is accepted for signature symmetry with `FieldSpec`; the mask alone
 * determines the space.
 */
export function fieldValueSpace(
  bitMask: number,
  shift: number,
  size: FieldWidth,
): number {
  void size;
  return ((bitMask >>> 0) >>> shift) >>> 0;
}

export type ComparisonOp = "eq" | "ne" | "ge" | "le" | "gt" | "lt";

/**
 * One comparison decided in a field's value space.
 *
 * `fold` is `"never"`/`"always"` when no value in the space can satisfy the
 * comparison, `"defer"` when a middle `ne` constant must be materialized at
 * the use site, and `undefined` when the adapter should emit `comparison`
 * against `value` (already shifted into the field's lane).
 */
export interface ComparisonPlan {
  readonly fold?: "never" | "always" | "defer";
  readonly comparison?: number;
  readonly value?: number;
}

/**
 * The value space of one masked field and its six comparison decisions.
 *
 * A field's value space is the set of bit patterns inside `bitMask`,
 * interpreted as field units (`raw >> shift`). Because any bit-subset of the
 * mask is representable, its largest member is the whole mask shifted down —
 * including for *straddling* fields, where that is smaller than the nominal
 * width.
 *
 * `size` is carried for symmetry with `FieldSpec`; the mask alone determines
 * the space.
 *
 * The three comparison surfaces — `TriggerFieldRef`, `MemFieldHandle` and
 * `Var(explicitMask)` — all ask this object, so the fold table and constant
 * normalisation exist once. Writes, lane resolution and the concrete HIR
 * record stay with the adapters.
 */
export class FieldValueSpace {
  readonly bitMask: number;
  readonly shift: number;
  readonly size: FieldWidth;

  constructor(bitMask: number, shift: number, size: FieldWidth = 4) {
    this.bitMask = bitMask >>> 0;
    this.shift = shift;
    this.size = size;
  }

  get maxValue(): number {
    return fieldValueSpace(this.bitMask, this.shift, this.size);
  }

  /** Normalise a field-unit constant to its in-lane bit pattern. */
  lane(constant: number): number {
    return (constant << this.shift) >>> 0;
  }

  /** Reject a mask whose set bits sit below the field's byte shift. */
  validate(fieldName?: string): void {
    validateMaskShift(this.bitMask, this.shift, fieldName);
  }

  /** Decide `op` against `constant` (already normalised to 32 bits). */
  compare(op: ComparisonOp, constant: number, fieldName?: string): ComparisonPlan {
    const maxVal = this.maxValue;
    switch (op) {
      case "eq":
        if (((constant & ~maxVal) >>> 0) !== 0) {
          // A constant the field cannot hold can never equal it.
          return { fold: "never" };
        }
        return { comparison: Exactly, value: this.lane(constant) };
      case "ne":
        // Validate before folding: an invalid mask must stay a loud error
        // rather than silently becoming an always-true condition.
        this.validate(fieldName);
        if (((constant & ~maxVal) >>> 0) !== 0) {
          // Unrepresentable constant ⇒ "always unequal".
          return { fold: "always" };
        }
        if (constant === 0) {
          return { comparison: AtLeast, value: (1 << this.shift) >>> 0 };
        }
        if (constant === maxVal) {
          return {
            comparison: AtMost,
            value: ((maxVal - 1) << this.shift) >>> 0,
          };
        }
        return { fold: "defer" };
      case "ge": {
        const fold = foldOrdering("ge", constant, maxVal);
        return fold !== undefined
          ? { fold }
          : { comparison: AtLeast, value: this.lane(constant) };
      }
      case "le": {
        const fold = foldOrdering("le", constant, maxVal);
        return fold !== undefined
          ? { fold }
          : { comparison: AtMost, value: this.lane(constant) };
      }
      case "gt": {
        const fold = foldOrdering("gt", constant, maxVal);
        return fold !== undefined
          ? { fold }
          : {
              comparison: AtLeast,
              value: this.lane((constant + 1) >>> 0),
            };
      }
      case "lt": {
        const fold = foldOrdering("lt", constant, maxVal);
        return fold !== undefined
          ? { fold }
          : {
              comparison: AtMost,
              value: this.lane((constant - 1) >>> 0),
            };
      }
      default:
        throw new Error(`not a comparison op: ${op}`);
    }
  }
}

/**
 * Emit (or fold/defer) one comparison through the shared kernel.
 *
 * `target` is lazy so a folded comparison never resolves a condition target —
 * an important no-move guard for CP-backed fields. `defer` is the adapter's
 * use-site marker factory; only `ne` can decide to defer.
 */
export function applyComparison(
  space: FieldValueSpace,
  op: ComparisonOp,
  rhs: unknown,
  target: () => number | TriggerFieldRef,
  options: {
    readonly what: string;
    readonly fieldName?: string;
    readonly fullMaskDeaths?: boolean;
    readonly defer?: (constant: number) => CondLike;
  },
): CondLike {
  if (op === "ne") {
    // Preserve the historical precedence: an invalid mask is a loud
    // EUDSL-BOUNDS before the operand guard names a bad constant.
    space.validate(options.fieldName);
  }
  const constant = requirePlainInt(rhs, options.what) >>> 0;
  const plan = space.compare(op, constant, options.fieldName);
  if (plan.fold === "never") return Never();
  if (plan.fold === "always") return Always();
  if (plan.fold === "defer") {
    if (options.defer === undefined) {
      throw new Error(`${op} deferred without a marker factory`);
    }
    return options.defer(constant);
  }
  const comparison = plan.comparison as number;
  const value = plan.value as number;
  // A whole-dword `Var` keeps the compact `Deaths` row; the field adapters
  // always use `DeathsX`, exactly as before the kernel existed.
  if (options.fullMaskDeaths === true && space.bitMask === 0xffffffff) {
    return Deaths(target(), comparison, value, 0);
  }
  return DeathsX(target(), comparison, value, 0, space.bitMask);
}


/**
 * Append one bit-test trigger per bit of `target` into `dst`.
 *
 * Each trigger adds `1 << bit` to the accumulator when the corresponding bit
 * of the source field is set. The caller owns the accumulator reset, so reset
 * ordering (including any queued CP move) stays at the read site.
 *
 * With `extraConds` given, every bit-test trigger also carries those
 * conditions, prepended before the bit condition. The `FixedArray.get` reader
 * gates its 32 tests with `index < length` so an out-of-range index leaves the
 * result at 0, matching the per-element unroll. The default `undefined` path is
 * unchanged.
 */
export function emitBitTestRead(
  target: number | TriggerFieldRef,
  numBits: number,
  shift: number,
  dst: VarImpl,
  mask?: number,
  fieldName?: string,
  extraConds?: readonly HirCond[],
): void {
  let bitPositions: number[];
  if (mask === undefined) {
    bitPositions = [];
    for (let b = 0; b < numBits; b++) {
      bitPositions.push(shift + b);
    }
  } else {
    validateMaskShift(mask, shift, fieldName);
    bitPositions = [];
    for (let p = shift; p < 32; p++) {
      if (((mask >>> p) & 1) !== 0) {
        bitPositions.push(p);
      }
    }
  }

  for (const bitPos of bitPositions) {
    const maskBit = (1 << bitPos) >>> 0;
    const addVal = (1 << (bitPos - shift)) >>> 0;
    new Trigger({
      conds: [...(extraConds ?? []), DeathsX(target, AtLeast, maskBit, 0, maskBit)],
      acts: [SetDeathsX(dst.value, Add, addVal, 0, 0xffffffff)],
    });
  }
}

/**
 * Read `bits` bits from the dword at `epd` into a fresh `Var`.
 *
 * The accumulator reset is owned by this read site (never by
 * {@link emitBitTestRead}), so a caller that queued a CP move keeps its
 * ordering. This helper itself is for direct reads with no queued CP move.
 *
 * `mask` follows {@link emitBitTestRead}: with a mask, one trigger per set bit
 * of the mask at or above `shift`; without it, the exact `bits` window starting
 * at `shift`.
 *
 * A full 32-bit dword read (`bits === 32`, `shift === 0`, full mask) is the
 * shared-reader shape: it goes through {@link sharedDwordReader} exactly like
 * `MemFieldHandle.read()`. Inlining the 32 bit tests here would make a direct
 * caller (e.g. `bgm`) pay the per-site reader again.
 */
export function readEpd(
  epd: number,
  bits: number,
  shift: number,
  mask?: number,
): VarImpl {
  if (
    bits === 32 &&
    shift === 0 &&
    (mask === undefined || (mask >>> 0) === 0xffffffff)
  ) {
    return sharedDwordReader()(epd);
  }
  const res = Var(0);
  // Reset the accumulator at the read site: a re-entered body must not
  // accumulate onto the previous pass's result.
  new Trigger({ conds: [], acts: [res.act_set(0)], preserved: true });
  emitBitTestRead(epd, bits, shift, res, mask);
  return res;
}

/**
 * Read the dword at `epd` and return its *EPD* (`f_epdread_epd`).
 *
 * The dword holds a byte address `D` (the game's own pointer, e.g. the table
 * pointer at `0x58D740`). `EPD(D) = (D - 0x58A364) / 4`, so dropping bits 0-1
 * (addresses are dword aligned) and subtracting `EPD_BASE / 4` folds the
 * conversion into the bit-test accumulator the read already pays for.
 */
export function readEpdPointer(epd: number): VarImpl {
  const res = readEpd(epd, 30, 2, 0xfffffffc);
  res.isub(EPD_BASE >>> 2);
  return res;
}

/**
 * Read the CUnit byte pointer at `ptrEpd` and return its EPD (20T inline).
 *
 * `ptrEpd` is the EPD of the dword that holds the CUnit byte pointer: one of
 * the list-head globals or a node's own `next`/`prev` link. The result is a
 * `VarImpl` holding the target CUnit's EPD in the absolute eudkit space
 * (`EPD(0x59cca8) = 19025`), ready for `withEpd`, `DeathsX` or `CUnit(epd)`.
 *
 * A numeric target inlines the 18 bit tests — **20T executed**, no call
 * sequence. A runtime target binds CP via {@link withEpd} and calls the shared
 * CP-relative body; prefer {@link readCunitPointerSaved} inside a loop, because
 * a runtime target pays the 33T ambient CP read per call (about 60T) instead of
 * hoisting it outside the loop.
 *
 * The reader does **not** validate its result. A null pointer reads as
 * `0xfff9d729` (seed plus bias); gate the walk with {@link inCunitPool}, whose
 * upper bound rejects that sentinel, instead of raising here.
 */
export function readCunitPointer(
  ptrEpd: number | VarImpl | ReadonlyVar | Expr,
): VarImpl {
  if (
    typeof ptrEpd === "boolean" ||
    !(
      typeof ptrEpd === "number" ||
      ptrEpd instanceof VarImpl ||
      ptrEpd instanceof ReadonlyVar ||
      ptrEpd instanceof Expr
    )
  ) {
    throw new EudslError(
      "EUDSL-TYPE",
      `CUnit pointer EPD must be a plain integer or EPD value; got ${describeValue(ptrEpd)}`,
    );
  }

  if (typeof ptrEpd === "number") {
    const target = requirePlainInt(ptrEpd, "CUnit pointer EPD") >>> 0;
    const res = Var(0);
    // Seeded reset, not `res.set(0)`: the invariant bits of the shifted
    // pointer space (0, 1 and 20) are folded into the seed so the body is
    // exactly 1 reset + 18 tests + 1 bias = 20T.
    new Trigger({
      conds: [],
      acts: [res.act_set(CUNIT_POINTER_SEED)],
      preserved: true,
    });
    return cunitPointerBody(res, target);
  }
  return requireWithEpdFactory()(ptrEpd, () => _declareCunitReader()()) as VarImpl;
}

/**
 * Loop form: the caller already holds the ambient CP value in `savedCp`.
 *
 * Hoist the 33T ambient CP read outside the loop, then call this once per
 * node. It never reads the ambient CP: one store, the shared call, one
 * restore, plus the caller's own advance — about 28T per node — instead of the
 * ~60T {@link readCunitPointer} pays for a runtime target.
 */
export function readCunitPointerSaved(
  savedCp: VarImpl,
  ptrEpd: number | VarImpl | ReadonlyVar | Expr,
): VarImpl {
  return requireWithEpdSavedFactory()(
    savedCp,
    ptrEpd,
    () => _declareCunitReader()(),
  ) as VarImpl;
}

/**
 * `CUNIT_BASE_EPD <= epd <= CUNIT_LAST_EPD` as two splicable conditions.
 *
 * Returns `[epd.ge(CUNIT_BASE_EPD), epd.le(CUNIT_LAST_EPD)]` so a loop gate can
 * do `[...inCunitPool(ptr), done.eq(0)]` without wrapping a trigger.
 *
 * A null pointer reads as `0xfff9d729`, which is **above** `CUNIT_LAST_EPD`
 * (161,657): the upper bound, not the lower bound, is what rejects end-of-list.
 * This is a predicate, never a raise, so a hijacked list stays observable and
 * diagnosable.
 */
export function inCunitPool(
  epd: number | VarImpl | ReadonlyVar | Expr,
): HirCond[] {
  const value = toVar(epd as VarOperand);
  return [value.ge(CUNIT_BASE_EPD), value.le(CUNIT_LAST_EPD)];
}

export const read_cunit_pointer = readCunitPointer;
export const read_cunit_pointer_saved = readCunitPointerSaved;
export const in_cunit_pool = inCunitPool;

// ---------------------------------------------------------------------------
// Shared full-dword reader (one subroutine per build)
// ---------------------------------------------------------------------------
//
// The inline reader is 32 bit-test triggers per call site. Declaring it once
// and calling it makes a site pay the call sequence instead, so K sites of an
// M-trigger reader cost O(K + M) triggers instead of O(K x M).
//
// The body takes the EPD as a *parameter variable*: each bit-test condition's
// `player` field is a relocation into that variable's slot, so the engine
// reads the address when the condition fires. A call site therefore binds one
// argument (a constant store into the parameter slot, one act) instead of
// rewriting 32 condition fields, and the body stays reentrant per call.

/**
 * Late-bound `func()` factory.
 *
 * `view` cannot import `temporal` (temporal imports view), so temporal
 * registers its public `Func` here at module load. The shared reader then
 * declares its body through the same code path user subroutines use.
 */
type FuncFactory = (body: (...params: VarImpl[]) => VarImpl | void) => unknown;
// `var`, not `let`: `view` -> `var` -> `temporal` -> `view` is a module cycle,
// so temporal's registration call can run while view is still evaluating its
// body. `var` is hoisted and initialized before evaluation, so the write
// lands; a `let` would be in its TDZ. Deliberately no initializer: a later
// `= null` on this line would clobber the registration.
// eslint-disable-next-line no-var
var funcFactory: FuncFactory | null | undefined;

export function _registerFuncFactory(factory: FuncFactory): void {
  funcFactory = factory;
}

/**
 * Late-bound CP-binding factories.
 *
 * `view` cannot import `cp` (cp imports view), so cp registers `withEpd` and
 * `_withEpdSaved` here at module load. The CUnit reader resolves them at call
 * time, exactly like the `func` factory above.
 */
type WithEpdFactory = (epd: any, fn: () => any) => any;
type WithEpdSavedFactory = (
  savedCp: VarImpl,
  epd: any,
  fn: () => any,
) => any;

// `var` for the same cycle reason as `funcFactory`: cp's registration can run
// while view is still evaluating in an entry point that loads cp first.
// eslint-disable-next-line no-var
var withEpdFactory: WithEpdFactory | null | undefined;
// eslint-disable-next-line no-var
var withEpdSavedFactory: WithEpdSavedFactory | null | undefined;

export function _registerCpFactories(
  withEpd: WithEpdFactory,
  withEpdSaved: WithEpdSavedFactory,
): void {
  withEpdFactory = withEpd;
  withEpdSavedFactory = withEpdSaved;
}

function requireWithEpdFactory(): WithEpdFactory {
  if (withEpdFactory === null || withEpdFactory === undefined) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "readCunitPointer() requires the cp frontend to be loaded",
    );
  }
  return withEpdFactory;
}

function requireWithEpdSavedFactory(): WithEpdSavedFactory {
  if (withEpdSavedFactory === null || withEpdSavedFactory === undefined) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "readCunitPointerSaved() requires the cp frontend to be loaded",
    );
  }
  return withEpdSavedFactory;
}

function requireFuncFactory(what: string): FuncFactory {
  if (funcFactory === null || funcFactory === undefined) {
    throw new EudslError(
      "EUDSL-SCOPE",
      `${what} requires the temporal frontend to be loaded`,
    );
  }
  return funcFactory;
}

/**
 * Read the 18-bit CUnit pointer at `target` into `res` and bias it.
 *
 * Shared by the inline numeric-target reader and the CP-relative kernel; the
 * caller owns the seed reset so each path keeps its intentional act shape.
 */
function cunitPointerBody(res: VarImpl, target: number = CurrentPlayer): VarImpl {
  emitBitTestRead(target, 18, 2, res, CUNIT_POINTER_MASK);
  res.iadd(CUNIT_POINTER_BIAS);
  return res;
}

/** Callable handle for the shared full-dword reader. */
export interface SharedDwordReader {
  (epd: number): VarImpl;
}

const sharedDwordReaders = new WeakMap<object, Map<number, () => VarImpl>>();

/**
 * Return this build's shared full-dword reader, declaring bodies lazily.
 *
 * The engine reads a condition's `player` field as a literal EPD, so a shared
 * body cannot take the address as a runtime variable (the field would name the
 * variable's slot, not the dword it holds). Patching the 32 fields per call
 * would regress executed cost, so each distinct address gets its own body:
 * sites that read the same field share one body, and the body keeps literal
 * `player` fields.
 */
function sharedDwordReader(): SharedDwordReader {
  const builder = _getActiveModuleBuilder();
  if (builder === null) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "field.read() must be called inside buildModule()",
    );
  }
  let kernels = sharedDwordReaders.get(builder);
  if (kernels === undefined) {
    kernels = new Map<number, () => VarImpl>();
    sharedDwordReaders.set(builder, kernels);
  }
  const declared = kernels;

  const declare = (epd: number): (() => VarImpl) => {
    const kernelBody = (): VarImpl => {
      const res = Var(0);
      res.set(0);
      for (let bit = 0; bit < 32; bit++) {
        const maskBit = (1 << bit) >>> 0;
        new Trigger({
          conds: [DeathsX(epd, AtLeast, maskBit, 0, maskBit)],
          acts: [SetDeathsX(res.value, Add, maskBit, 0, 0xffffffff)],
        });
      }
      return res;
    };
    return requireFuncFactory("field.read()")(kernelBody) as () => VarImpl;
  };

  return ((epd: number): VarImpl => {
    const target = requirePlainInt(epd, "field read target") >>> 0;
    let handle = declared.get(target);
    if (handle === undefined) {
      handle = declare(target);
      declared.set(target, handle);
    }
    return handle();
  }) as SharedDwordReader;
}

/**
 * One CUnit reader body per builder.
 *
 * Unlike the shared dword reader there is no per-address cache: the body's
 * condition target is always the literal `CURRENT_PLAYER` EPD, and
 * `withEpd`/`_withEpdSaved` bind CP around the call.
 */
const cunitReaders = new WeakMap<object, () => VarImpl>();

/** Declare (once per builder) the shared CP-relative CUnit pointer body. */
export function _declareCunitReader(): () => VarImpl {
  const builder = _getActiveModuleBuilder();
  if (builder === null) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "readCunitPointer() must be called inside buildModule()",
    );
  }
  let handle = cunitReaders.get(builder);
  if (handle === undefined) {
    const kernelBody = (): VarImpl => {
      const res = Var(0);
      res.set(CUNIT_POINTER_SEED);
      return cunitPointerBody(res);
    };
    handle = requireFuncFactory("readCunitPointer()")(kernelBody) as () => VarImpl;
    cunitReaders.set(builder, handle);
  }
  return handle;
}

// ---------------------------------------------------------------------------
// MemFieldHandle: field handle on a MemView instance
// ---------------------------------------------------------------------------

export interface CursorLike {
  moveTo(dwordOffset: number): void;
}

export class MemFieldHandle {
  readonly schema: MemViewSchema;
  readonly spec: FieldSpec;
  readonly baseEpd: number | CursorLike | any;
  readonly fieldName: string;

  readonly byteOffset: number;
  readonly size: FieldWidth;
  readonly subp: 0 | 1 | 2 | 3;
  readonly shift: 0 | 8 | 16 | 24;
  readonly alignedByteOffset: number;
  readonly dwordOffset: number;
  readonly bitMask: number;
  readonly epd?: number;

  constructor(
    schema: MemViewSchema,
    spec: FieldSpec,
    baseEpd: number | CursorLike | any,
    fieldName: string,
  ) {
    this.schema = schema;
    this.spec = spec;
    this.baseEpd = baseEpd;
    this.fieldName = fieldName;

    this.byteOffset = spec.offset;
    this.size = spec.size;
    const subp = (spec.offset & 3) as 0 | 1 | 2 | 3;
    this.subp = subp;
    this.shift = (subp * 8) as 0 | 8 | 16 | 24;
    this.alignedByteOffset = spec.offset - subp;
    this.dwordOffset = Math.floor(spec.offset / 4);

    const rawMask = spec.size === 4 ? 0xffffffff : spec.size === 2 ? 0xffff : 0xff;
    this.bitMask = spec.mask !== undefined ? spec.mask >>> 0 : ((rawMask << this.shift) >>> 0);

    if (typeof baseEpd === "number") {
      this.epd = baseEpd + this.dwordOffset;
    }
  }

  private isCP(): boolean {
    return typeof this.baseEpd === "object" && this.baseEpd !== null && "moveTo" in this.baseEpd;
  }

  protected prepareTarget(): number {
    if (this.isCP()) {
      (this.baseEpd as CursorLike).moveTo(this.dwordOffset);
      return CurrentPlayer; // 13
    }
    if (typeof this.baseEpd === "number") {
      return this.baseEpd + this.dwordOffset;
    }
    throw new EudslError(
      "EUDSL-TARGET",
      `Dynamic Var EPD access on field '${this.fieldName}' requires withCP scope`,
    );
  }

  /**
   * Absolute EPD for a condition: no act, so no scope/alias state.
   *
   * `withCP(base)` rejects a dynamic base, so `base + dwordOffset` names the
   * same dword at every evaluation site. `base === 0` is the internal
   * "follow the live CP" sentinel (`CPCursor(0)` from `read()`), so keep the
   * hop path for it.
   *
   * @internal Seam helper: the condition-only analogue of `prepareTarget`.
   */
  condTarget(): number {
    if (this.isCP()) {
      const base = (this.baseEpd as { baseEpd?: unknown }).baseEpd;
      if (typeof base === "number" && base !== 0) {
        return base + this.dwordOffset;
      }
    }
    return this.prepareTarget();
  }

  /** This field's comparison kernel (the one source of comparison truth). */
  valueSpace(): FieldValueSpace {
    return new FieldValueSpace(this.bitMask, this.shift, this.size);
  }

  // Validate the operand *before* `prepareTarget()`: on a CP base the target
  // step queues a move act, and a rejected write must leave no stray act.
  // `act_*` here shares the dual-channel contract documented on `Var`
  // (`var.ts`): the returned action is ALSO queued into the active builder
  // by `SetDeathsX` (`buildAct`), and a bare call outside `buildModule`
  // queues nowhere (see `ts/tests/test_dat.ts`).
  act_set(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    const target = this.prepareTarget();
    return SetDeathsX(target, SetTo, val, 0, this.bitMask);
  }

  act_add(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    const target = this.prepareTarget();
    return SetDeathsX(target, Add, val, 0, this.bitMask);
  }

  act_sub(rhs: number): HirAct {
    const val = (requirePlainInt(rhs, "field write") << this.shift) >>> 0;
    const target = this.prepareTarget();
    return SetDeathsX(target, Subtract, val, 0, this.bitMask);
  }

  set(rhs: number): void {
    this.act_set(rhs);
  }

  iadd(rhs: number): void {
    this.act_add(rhs);
  }

  isub(rhs: number): void {
    this.act_sub(rhs);
  }

  eq(rhs: number): HirCond {
    return applyComparison(
      this.valueSpace(),
      "eq",
      rhs,
      () => this.condTarget(),
      { what: "field comparison" },
    ) as HirCond;
  }

  ge(rhs: number): HirCond {
    return applyComparison(
      this.valueSpace(),
      "ge",
      rhs,
      () => this.condTarget(),
      { what: "field comparison" },
    ) as HirCond;
  }

  le(rhs: number): HirCond {
    return applyComparison(
      this.valueSpace(),
      "le",
      rhs,
      () => this.condTarget(),
      { what: "field comparison" },
    ) as HirCond;
  }

  gt(rhs: number): HirCond {
    return applyComparison(
      this.valueSpace(),
      "gt",
      rhs,
      () => this.condTarget(),
      { what: "field comparison" },
    ) as HirCond;
  }

  lt(rhs: number): HirCond {
    return applyComparison(
      this.valueSpace(),
      "lt",
      rhs,
      () => this.condTarget(),
      { what: "field comparison" },
    ) as HirCond;
  }

  ne(rhs: number): CondLike {
    return applyComparison(
      this.valueSpace(),
      "ne",
      rhs,
      () => this.condTarget(),
      {
        what: "field comparison",
        fieldName: this.fieldName,
        defer: (constant) => new DeferredNe(this, constant),
      },
    );
  }

  read(): VarImpl {
    const target = this.prepareTarget();
    if (this.size === 4 && this.shift === 0 && this.bitMask === 0xffffffff) {
      // Full dword: one shared kernel serves every site (see
      // `sharedDwordReader`). Wider/partial fields keep the inline reader.
      return sharedDwordReader()(target);
    }
    const numBits = this.size * 8;
    const res = Var(0);
    // `prepareTarget()` may queue a CP move as an immediate act; this
    // unconditional trigger consumes it and resets the accumulator, so
    // repeated reads (loops) start from zero.
    new Trigger({
      conds: [],
      acts: [res.act_set(0)],
      preserved: true,
    });
    emitBitTestRead(target, numBits, this.shift, res, this.bitMask, this.fieldName);
    return res;
  }
}

/**
 * Any field surface whose `ne` can defer.
 *
 * `MemFieldHandle` and `TriggerFieldRef` share the field geometry, the two
 * bounding comparisons and a condition target, so both defer through one
 * marker.
 */
interface FieldSurface {
  readonly size: FieldWidth;
  readonly shift: 0 | 8 | 16 | 24;
  readonly bitMask: number;
  readonly fieldName?: string;
  condTarget(): number | TriggerFieldRef;
  eq(rhs: number): HirCond;
  lt(rhs: number): HirCond;
  gt(rhs: number): HirCond;
}

/**
 * `field.ne(k)` for a middle constant, materialized at the use site.
 *
 * The eager path built `diff = k; diff.isub(read())` at construction, so a
 * `whileLoop` gate re-checked a stale `diff` every pass. This marker records
 * intent instead: every {@link materialize} resets `diff` to `k` and then
 * queues exactly the acts the eager path queued into the same temporaries.
 * The first call allocates those temporaries and later calls reuse them; both
 * emit the reset, so the loop tail rebuilds the comparison from live memory
 * and a one-shot site (`when`, a raw `Trigger`) starts every pass clean.
 *
 * The read target is resolved through the surface's absolute-EPD
 * {@link MemFieldHandle.condTarget}, so a CP-backed field needs no hop.
 */
class DeferredNe implements MaterializableCond {
  readonly surface: FieldSurface;
  readonly neConst: number;
  private diff?: VarImpl;
  private res?: VarImpl;
  private inv?: VarImpl;

  constructor(surface: FieldSurface, neConst: number) {
    this.surface = surface;
    this.neConst = neConst;
  }

  /**
   * `field != k` is the union `field < k || field > k`, matching the
   * historical 4T deferred gate byte for byte.
   */
  gatePlan(): GatePlan {
    validateMaskShift(
      this.surface.bitMask,
      this.surface.shift,
      this.surface.fieldName,
    );
    return registerDetachedPlan(
      {
        kind: "or",
        nodes: [
          { kind: "lit", cond: this.surface.lt(this.neConst) },
          { kind: "lit", cond: this.surface.gt(this.neConst) },
        ],
      },
      this,
    );
  }

  /** `!(field != k)` is the direct 1C `field == k`. */
  complement(): CondLike {
    return this.surface.eq(this.neConst);
  }

  materialize(): HirCond {
    const h = this.surface;
    const target = h.condTarget(); // absolute EPD; no act, no hop
    // Allocation order must match the eager path: diff, res, inv.
    let diff = this.diff;
    if (!diff) {
      diff = Var(this.neConst);
      this.diff = diff;
    }
    let res = this.res;
    if (!res) {
      res = Var(0);
      this.res = res;
    }
    let inv = this.inv;
    if (!inv) {
      inv = Var(0);
      this.inv = inv;
    }

    // Reset the accumulator, then one bit test per in-mask bit.
    new Trigger({ conds: [], acts: [res.act_set(0)], preserved: true });
    emitBitTestRead(target, h.size * 8, h.shift, res, h.bitMask, h.fieldName);
    // diff = k - res, wrapping, exactly as Var.isub(Var) emits it — but into
    // cached temporaries, so a loop rebuild allocates nothing.
    inv.set(0xffffffff);
    saturatingSubInto(inv, res);
    // The declaration init runs once; every evaluation site needs the reset
    // re-emitted, including the first (one-shot) one.
    diff.set(this.neConst);
    diff.iadd(1);
    diff.iadd(inv);
    return Deaths(diff.value, AtLeast, 1, 0);
  }
}

// ---------------------------------------------------------------------------
// Schema Normalization & Resolution
// ---------------------------------------------------------------------------

function normalizeName(name: string): string {
  return name.replace(/_/g, "").toLowerCase();
}

const schemaIndexCache = new WeakMap<MemViewSchema, Map<string, { name: string; spec: FieldSpec }>>();

function buildSchemaIndex(schema: MemViewSchema): Map<string, { name: string; spec: FieldSpec }> {
  let idx = schemaIndexCache.get(schema);
  if (idx) return idx;

  idx = new Map();
  for (const [key, spec] of Object.entries(schema.fields) as [string, FieldSpec][]) {
    const item = { name: key, spec };
    idx.set(key, item);
    idx.set(normalizeName(key), item);

    if (spec.aliases) {
      for (const alias of spec.aliases) {
        idx.set(alias, item);
        idx.set(normalizeName(alias), item);
      }
    }
  }

  // Common StarCraft domain aliases
  const addAlias = (canonical: string, ...aliases: string[]) => {
    const found = idx!.get(canonical) ?? idx!.get(normalizeName(canonical));
    if (found) {
      for (const a of aliases) {
        idx!.set(a, found);
        idx!.set(normalizeName(a), found);
      }
    }
  };

  addAlias("hp", "hitPoints", "hit_points");
  addAlias("shield", "shields");
  addAlias("order", "orderID", "order_id");
  addAlias("owner", "playerID", "player_id");
  addAlias("posX", "pos_x");
  addAlias("posY", "pos_y");

  schemaIndexCache.set(schema, idx);
  return idx;
}

function resolveField(schema: MemViewSchema, prop: string): { name: string; spec: FieldSpec } | undefined {
  const index = buildSchemaIndex(schema);
  return index.get(prop) ?? index.get(normalizeName(prop));
}

// ---------------------------------------------------------------------------
// MemViewInstance: Proxy-based typed memory view
// ---------------------------------------------------------------------------

export class MemViewInstance {
  readonly schema: MemViewSchema;
  readonly baseEpd: number | any;

  constructor(schema: MemViewSchema, baseEpd: number | any) {
    this.schema = schema;
    this.baseEpd = baseEpd;

    return new Proxy(this, {
      get(target, prop, receiver) {
        if (typeof prop === "symbol" || prop in target) {
          return Reflect.get(target, prop, receiver);
        }
        if (typeof prop === "string") {
          const resolved = resolveField(target.schema, prop);
          if (resolved) {
            return new MemFieldHandle(target.schema, resolved.spec, target.baseEpd, resolved.name);
          }
          throw new EudslError(
            "EUDSL-BOUNDS",
            `Unknown field '${prop}' on layout '${target.schema.name}'`,
          );
        }
        return Reflect.get(target, prop, receiver);
      },
      set(target, prop, value, receiver) {
        if (typeof prop === "symbol" || prop in target) {
          return Reflect.set(target, prop, value, receiver);
        }
        if (typeof prop === "string") {
          const resolved = resolveField(target.schema, prop);
          if (resolved) {
            new MemFieldHandle(
              target.schema,
              resolved.spec,
              target.baseEpd,
              resolved.name,
            ).set(value);
            return true;
          }
          throw new EudslError(
            "EUDSL-BOUNDS",
            `Unknown field '${prop}' on layout '${target.schema.name}'`,
          );
        }
        return Reflect.set(target, prop, value, receiver);
      },
    });
  }
}

// ---------------------------------------------------------------------------
// Layout Loaders & Pre-loaded Schemas
// ---------------------------------------------------------------------------

const __dirname_view = path.dirname(fileURLToPath(import.meta.url));

export function locateSchemaFile(name: string): string {
  // Packaged copy first: it ships inside the installed `eudkit` package
  // (ts/schema/layouts, included by the `files` allowlist in package.json),
  // so an installed import works outside the repo tree. The repo-tree and
  // cwd candidates remain as dev fallbacks; the packaged copy is kept
  // byte-identical to the repo canonical files by test_schema_packaging.ts.
  // Note: from dist/ the first candidate (dist/schema/layouts) misses and
  // the second (dist/../schema/layouts) hits — that is the same shipped
  // copy, so no dist/schema duplication is needed.
  const candidates = [
    path.join(__dirname_view, "schema", "layouts", `${name}.json`),
    path.join(__dirname_view, "..", "schema", "layouts", `${name}.json`),
    path.join(process.cwd(), "schema", "layouts", `${name}.json`),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) {
      return c;
    }
  }
  throw new Error(`Schema file not found for '${name}'`);
}

export function loadLayout(name: string): MemViewSchema {
  const filePath = locateSchemaFile(name);
  const data = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  const fields: Record<string, FieldSpec> = {};
  for (const [k, v] of Object.entries(data.fields) as [string, any][]) {
    fields[k] = {
      offset: v.offset,
      size: v.size as FieldWidth,
      mask: v.mask,
      pointer: v.pointer,
      composite: v.composite,
      aliases: v.aliases,
    };
  }
  return defineMemView(data.struct ?? name, data.size, fields);
}

export const CUnitSchema = loadLayout("cunit");
export const CSpriteSchema = loadLayout("csprite");

// ---------------------------------------------------------------------------
// Constructors & Factories: CUnit, CSprite, MemView, MemCond
// ---------------------------------------------------------------------------

export interface MemViewFactory<S extends MemViewSchema = MemViewSchema> {
  (epd: number | any): MemViewInstance & any;
  new (epd: number | any): MemViewInstance & any;
  readonly schema: S;
}

function makeFactory<S extends MemViewSchema>(schema: S): MemViewFactory<S> {
  const factory = function (epd: number | any) {
    return new MemViewInstance(schema, epd);
  };
  (factory as any).schema = schema;
  return new Proxy(factory as unknown as MemViewFactory<S>, {
    apply(_target, _thisArg, args: [number | any]) {
      return new MemViewInstance(schema, args[0]);
    },
    construct(_target, args: [number | any]) {
      return new MemViewInstance(schema, args[0]);
    },
  });
}

export const CUnit = makeFactory(CUnitSchema);
export const CSprite = makeFactory(CSpriteSchema);

export function MemView(
  schema: MemViewSchema,
  epd: number | any,
): MemViewInstance & any {
  return new MemViewInstance(schema, epd);
}

// Backward-compatible type alias
export type MemView<K extends string = string> = MemViewSchema<K>;

/**
 * Single-condition in-place memory comparison against an `EPD` without
 * materializing into a variable (1C / 1T when the `EPD` is constant).
 */
export function MemCond(
  epdAddr: number | any,
  comparison: number | string,
  amount: number | any,
  sizeOrMask: number = 4,
): HirCond {
  let cmp = comparison;
  if (typeof cmp === "string") {
    switch (cmp.toLowerCase()) {
      case "exactly":
      case "==":
      case "=":
        cmp = Exactly;
        break;
      case "atleast":
      case ">=":
        cmp = AtLeast;
        break;
      case "atmost":
      case "<=":
        cmp = AtMost;
        break;
      default:
        throw new EudslError("EUDSL-CMP", `Unknown comparison: ${comparison}`);
    }
  } else {
    // The two DSL parameters are guarded *here* so the error names the operand
    // the author wrote (`MemCond comparison`), not the record field it would
    // have reached. Only the numeric branch is guarded: a string is a
    // comparison *name*, and an unknown name stays `EUDSL-CMP` (ADR 0019 §6).
    cmp = requirePlainInt(cmp, "MemCond comparison");
  }

  sizeOrMask = requirePlainInt(sizeOrMask, "MemCond size_or_mask");

  if (sizeOrMask === 4 || sizeOrMask === 0xffffffff) {
    return Deaths(epdAddr, cmp as number, amount, 0);
  }
  if (sizeOrMask === 1) {
    return DeathsX(epdAddr, cmp as number, amount, 0, 0xff);
  }
  if (sizeOrMask === 2) {
    return DeathsX(epdAddr, cmp as number, amount, 0, 0xffff);
  }
  return DeathsX(epdAddr, cmp as number, amount, 0, sizeOrMask >>> 0);
}

// ---------------------------------------------------------------------------
// Raw dword/word bit helpers and the CUnitView façade
// ---------------------------------------------------------------------------

/** Sentinel returned by `ObjectPool` when it is exhausted. */
export const POOL_NULL = 0xffffffff;

/** The scenario location table starts at this byte address. */
const LOCATION_TABLE_BASE = 0x58dc4c;
/** Each location record is five dwords; dwords 0 and 1 are `left`/`top`. */
const LOCATION_STRIDE_EPDS = 5;

/** `GroupFlags` are unit-type data, never CUnit field masks. */
export const GROUP_FLAGS_TARGET = "unit-type";

/** Full-dword CUnit `statusFlags` masks (byte offset 220). */
export const StatusFlags = Object.freeze({
  Completed: 0x00000001,
  GroundedBuilding: 0x00000002,
  InAir: 0x00000004,
  Disabled: 0x00000008,
  Burrowed: 0x00000010,
  InBuilding: 0x00000020,
  InTransport: 0x00000040,
  CanBeChased: 0x00000080,
  RequiresDetection: 0x00000100,
  Cloaked: 0x00000200,
  DoodadStatesThing: 0x00000400,
  CloakingForFree: 0x00000800,
  CanNotReceiveOrders: 0x00001000,
  NoBrkCodeStart: 0x00002000,
  UNKNOWN2: 0x00004000,
  CanNotAttack: 0x00008000,
  CanTurnAroundToAttack: 0x00010000,
  IsBuilding: 0x00020000,
  IgnoreTileCollision: 0x00040000,
  Unmovable: 0x00080000,
  IsNormal: 0x00100000,
  NoCollide: 0x00200000,
  UNKNOWN5: 0x00400000,
  IsGathering: 0x00800000,
  SubunitWalking: 0x01000000,
  SubunitFollow: 0x02000000,
  Invincible: 0x04000000,
  HoldingPosition: 0x08000000,
  SpeedUpgrade: 0x10000000,
  CooldownUpgrade: 0x20000000,
  IsHallucination: 0x40000000,
  IsSelfDestructing: 0x80000000,
} as const);

/** CUnit `pathingFlags` byte masks (byte offset 261). */
export const PathingFlags = Object.freeze({
  HasCollision: 0x01,
  IsStacked: 0x02,
  Decollide: 0x04,
} as const);

/**
 * Unit-type `groupFlags` byte masks at byte address `0x6637a0`.
 *
 * These are unit-type dat-array bits, **not** masks for any CUnit field;
 * never write them through `CUnitView`.
 */
export const GroupFlags = Object.freeze({
  Zerg: 0x01,
  Terran: 0x02,
  Protoss: 0x04,
  Men: 0x08,
  Building: 0x10,
  Factory: 0x20,
  Independent: 0x40,
  Neutral: 0x80,
} as const);

/** Unit-type `availabilityFlags` word masks at byte address `0x661518`. */
export const AvailabilityFlags = Object.freeze({
  NonNeutral: 0x001,
  UnitListing: 0x002,
  MissionBriefing: 0x004,
  PlayerSettings: 0x008,
  AllRaces: 0x010,
  SetDoodadState: 0x020,
  NonLocationTriggers: 0x040,
  UnitHeroSettings: 0x080,
  LocationTriggers: 0x100,
  BroodWarOnly: 0x200,
} as const);

function requireDwordAddress(addr: unknown, subject: string): number {
  const value = requirePlainInt(addr, subject);
  requireAlignedDwordAddress(value, subject);
  return value >>> 0;
}

function requireBitIndex(bit: unknown): number {
  const value = requirePlainInt(bit, "bit index");
  if (value < 0 || value > 31) {
    throw new EudslError(
      "EUDSL-RANGE",
      `bit index must be in 0..31, got ${value}`,
    );
  }
  return value;
}

function splitValue(
  value: number | VarImpl | ReadonlyVar | Expr,
  halfBits: number,
  shift: number,
  what: string,
): [VarImpl, VarImpl] {
  if (typeof value === "number") {
    const constant = requirePlainInt(value, `${what} value`) >>> 0;
    const halfMask = (1 << halfBits) - 1;
    return [Var(constant >>> shift), Var(constant & halfMask)];
  }
  if (!(value instanceof VarImpl || value instanceof ReadonlyVar || value instanceof Expr)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what} value must be an integer, a Var, or an Expr; got ${describeValue(value)}`,
    );
  }
  const source = toVar(value as VarOperand);
  const high = Var(0);
  high.set(source);
  high.ishr(shift);
  const low = Var(0);
  low.set(source);
  low.iand((1 << halfBits) - 1);
  return [high, low];
}

/** Split a 32-bit value into its two 16-bit halves `(hi16, lo16)`. */
export function splitDword(
  value: number | VarImpl | ReadonlyVar | Expr,
): [VarImpl, VarImpl] {
  return splitValue(value, 16, 16, "splitDword");
}

/** Split a **word** into its two bytes `(hi8, lo8)`. */
export function splitWord(
  value: number | VarImpl | ReadonlyVar | Expr,
): [VarImpl, VarImpl] {
  return splitValue(value, 8, 8, "splitWord");
}

export const split_dword = splitDword;
export const split_word = splitWord;

/** Return a 1-condition / 0-action test of bit `bit` at dword `addr`. */
export function readBit(addr: number, bit: number): HirCond {
  const address = requireDwordAddress(addr, "readBit address");
  const index = requireBitIndex(bit);
  const mask = (1 << index) >>> 0;
  return DeathsX(EPD(address), AtLeast, mask, 0, mask);
}

/** Read bit `bit` at dword `addr` into a fresh Var (2T). */
export function readBitVar(addr: number, bit: number): VarImpl {
  const address = requireDwordAddress(addr, "readBitVar address");
  const index = requireBitIndex(bit);
  return readEpd(EPD(address), 1, index);
}

export const read_bit = readBit;
export const read_bit_var = readBitVar;

/** Set or clear bit `bit` at dword `addr` (one masked act). */
export function writeBit(addr: number, bit: number, on: boolean): void {
  const address = requireDwordAddress(addr, "writeBit address");
  const index = requireBitIndex(bit);
  if (typeof on !== "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `writeBit on must be a bool, got ${describeValue(on)}`,
    );
  }
  const mask = (1 << index) >>> 0;
  const act = SetDeathsX(EPD(address), SetTo, on ? mask : 0, 0, mask);
  new Trigger({ conds: [], acts: [act] });
}

/** Write `value` through `mask` at dword `addr` in one act. */
export function writeMask(addr: number, value: number, mask: number): void {
  const address = requireDwordAddress(addr, "writeMask address");
  const maskedValue = requirePlainInt(value, "writeMask value") >>> 0;
  const bitMask = requirePlainInt(mask, "writeMask mask") >>> 0;
  if (bitMask === 0) {
    throw new EudslError("EUDSL-RANGE", "writeMask mask must be non-zero");
  }
  const outside = (maskedValue & (~bitMask >>> 0)) >>> 0;
  if (outside !== 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `writeMask value ${maskedValue.toString(16)} has set bits outside mask ${bitMask.toString(16)}`,
    );
  }
  const act = SetDeathsX(EPD(address), SetTo, maskedValue, 0, bitMask);
  new Trigger({ conds: [], acts: [act] });
}

export const write_bit = writeBit;
export const write_mask = writeMask;

function coerceEpd(epd: number | VarImpl | ReadonlyVar | Expr): number | VarImpl {
  if (typeof epd === "number") {
    return requirePlainInt(epd, "CUnitView epd");
  }
  return toVar(epd as VarOperand);
}

function epdAdd(
  epd: number | VarImpl,
  offset: number,
): number | VarImpl | Expr {
  if (typeof epd === "number") {
    return epd + offset;
  }
  if (offset === 0) {
    return epd;
  }
  return epd.add(offset);
}

function writeMaskedAt(
  epd: number | VarImpl | Expr,
  value: number,
  mask: number,
): void {
  if (typeof epd === "number") {
    SetDeathsX(epd >>> 0, SetTo, value, 0, mask);
    return;
  }
  const target = toVar(epd as VarOperand);
  const ptr = new Trigger({
    conds: [],
    acts: [SetDeathsX(0, SetTo, value, 0, mask)],
    preserved: true,
    register: false,
  });
  emitVarStore(
    target.id,
    TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
  );
  _registerTrigger(ptr);
}

/**
 * One-dword runtime store, byte-identical to `memsetEPD(dst, value, 1)`.
 *
 * `setLocation` needs exactly this body. Importing `blockio` here would close
 * a `view -> blockio -> cp -> trigger -> view` cycle that reads `cp`'s
 * `ACT_STRIDE_EPDS` before `trigger` initializes it, so the body is mirrored
 * locally from the same primitives.
 */
function storeOneDword(dst: number | VarImpl | Expr, value: VarImpl): void {
  const dstNode: number | VarImpl =
    typeof dst === "number" ? dst >>> 0 : toVar(dst as VarOperand);
  const cnt = Var(0);
  cnt.set(1);
  const ptr = new Trigger({
    conds: [],
    acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff)],
    preserved: true,
    register: false,
  });
  when(cnt.ge(1), () => {
    if (typeof dstNode === "number") {
      SetDeaths(ptr.act(0).dest, SetTo, dstNode, 0);
    } else {
      emitVarStore(
        dstNode.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
        SetTo,
      );
    }
  });
  whileLoop(cnt.ge(1), () => {
    emitVarStore(
      value.id,
      TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
      SetTo,
    );
    _registerTrigger(ptr);
    SetDeaths(ptr.act(0).dest, Add, 1, 0);
    cnt.isub(1);
  });
}

function readEpdPointerAt(epd: number | VarImpl | Expr): VarImpl {
  if (typeof epd === "number") {
    return readEpdPointer(epd >>> 0);
  }
  return requireWithEpdFactory()(epd, () => {
    const res = readEpd(CurrentPlayer, 30, 2, 0xfffffffc);
    res.isub(EPD_BASE >>> 2);
    return res;
  }) as VarImpl;
}

function readStatusBitAt(epd: number | VarImpl | Expr, bit: number): VarImpl {
  if (typeof epd === "number") {
    return readEpd(epd >>> 0, 1, bit);
  }
  return requireWithEpdFactory()(epd, () =>
    readEpd(CurrentPlayer, 1, bit),
  ) as VarImpl;
}

function requirePlayerId(player: unknown, subject: string): number {
  const value = requirePlainInt(player, subject);
  if (value < 0 || value > 11) {
    throw new EudslError(
      "EUDSL-RANGE",
      `${subject} must be a player id in 0..11, got ${value}`,
    );
  }
  return value;
}

function requireLocationId(location: unknown): number {
  const value = requirePlainInt(location, "setLocation location");
  if (value < 0 || value > 255) {
    throw new EudslError(
      "EUDSL-RANGE",
      `setLocation location must be an id in 0..255, got ${value}`,
    );
  }
  return value;
}

/**
 * Typed façade over a CUnit slot at a constant or runtime EPD.
 *
 * Not a `MemViewInstance` subclass: its proxy intercepts attribute writes,
 * which would swallow this façade's own state. Field access still resolves
 * through the canonical `CUnit` schema.
 */
export class CUnitView {
  readonly epd: number | VarImpl;
  readonly mem: MemViewInstance;

  constructor(epd: number | VarImpl | ReadonlyVar | Expr) {
    this.epd = coerceEpd(epd);
    this.mem = CUnit(this.epd);
  }

  field(name: string): MemFieldHandle {
    const resolved = resolveField(CUnitSchema, name);
    if (!resolved) {
      throw new EudslError(
        "EUDSL-UNKNOWN-FIELD",
        `Unknown field '${name}' on layout '${CUnitSchema.name}'`,
      );
    }
    return new MemFieldHandle(
      CUnitSchema,
      resolved.spec,
      this.epd,
      resolved.name,
    );
  }

  read(name: string): VarImpl {
    const handle = this.field(name);
    if (typeof this.epd === "number") {
      return handle.read();
    }
    return requireWithEpdFactory()(
      epdAdd(this.epd, handle.dwordOffset),
      () =>
        readEpd(
          CurrentPlayer,
          handle.size * 8,
          handle.shift,
          handle.bitMask,
        ),
    ) as VarImpl;
  }

  nextUnit(): VarImpl {
    return readCunitPointer(epdAdd(this.epd, 1));
  }

  next_unit(): VarImpl {
    return this.nextUnit();
  }

  prevUnit(): VarImpl {
    return readCunitPointer(this.epd);
  }

  prev_unit(): VarImpl {
    return this.prevUnit();
  }

  spriteEpd(): VarImpl {
    return readEpdPointerAt(epdAdd(this.epd, 3));
  }

  sprite_epd(): VarImpl {
    return this.spriteEpd();
  }

  sprite(): MemViewInstance {
    return CSprite(this.spriteEpd());
  }

  isBurrowed(): VarImpl {
    const mask = StatusFlags.Burrowed;
    return readStatusBitAt(epdAdd(this.epd, 55), 31 - Math.clz32(mask));
  }

  is_burrowed(): VarImpl {
    return this.isBurrowed();
  }

  setInvincible(on = true): void {
    if (typeof on !== "boolean") {
      throw new EudslError(
        "EUDSL-TYPE",
        `setInvincible on must be a bool, got ${describeValue(on)}`,
      );
    }
    const mask = StatusFlags.Invincible;
    writeMaskedAt(epdAdd(this.epd, 55), on ? mask : 0, mask);
  }

  set_invincible(on = true): void {
    this.setInvincible(on);
  }

  giveTo(player: number): void {
    const owner = requirePlayerId(player, "giveTo player");
    writeMaskedAt(epdAdd(this.epd, 19), owner, 0x000000ff);
  }

  give_to(player: number): void {
    this.giveTo(player);
  }

  setColor(player: number): void {
    const color = requirePlayerId(player, "setColor player");
    const spriteEpd = this.spriteEpd();
    writeMaskedAt(
      epdAdd(spriteEpd, 2),
      (color << 16) >>> 0,
      0x00ff0000,
    );
  }

  set_color(player: number): void {
    this.setColor(player);
  }

  die(): void {
    writeMaskedAt(epdAdd(this.epd, 19), 0, 0x0000ff00);
  }

  remove(): void {
    writeMaskedAt(epdAdd(this.epd, 36), 0x04000000, 0xff000000);
    this.die();
  }

  setLocation(location: number): void {
    const loc = requireLocationId(location);
    const record = EPD(LOCATION_TABLE_BASE) + loc * LOCATION_STRIDE_EPDS;
    const left = readEpd(record, 32, 0);
    const top = readEpd(record + 1, 32, 0);
    left.iand(0xffff);
    top.ishl(16);
    top.ior(left);
    storeOneDword(epdAdd(this.epd, 10), top);
  }

  set_location(location: number): void {
    this.setLocation(location);
  }
}
