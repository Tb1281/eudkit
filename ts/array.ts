/**
 * Fixed-capacity `FixedArray` container for primitives and `TriggerStruct`s.
 *
 * Writable arrays are backed by contiguous, page-aligned `TriggerPage` action
 * slots: element `i` lives at page `i >> 6`, slot `i & 63`. Constant-index
 * access is pure address math (`0T`); comparisons use a single `1C` `MemCond`.
 *
 * Dynamic indexing uses the carry-free `PageSlotEPD` decomposition:
 * `weight(b) = 8 << b` for `b < 6`, and `602 << (b - 6)` for `b >= 6`.
 * One trigger per index bit patches a runtime pointer, so the address
 * computation costs `ceil(log2(N))` triggers instead of a multiply chain.
 *
 * `FixedArray.const([...])` is a build-time table: it allocates zero TriggerPages and
 * folds constant-index reads (and their comparisons) at build time.
 */

import {
  Add,
  Always,
  Never,
  SetDeaths,
  SetDeathsX,
  SetTo,
  TriggerFieldReloc,
  emitVarStore,
} from "./hir.ts";
import {
  ACT_COUNT,
  Trigger,
  _registerTrigger,
  type HirAct,
  type HirCond,
} from "./trigger.ts";
import { LazyBinding } from "./lazy-binding.ts";
import {
  ELEM_MASK,
  PageSlotEPD,
  pageSlotBitCond,
  pageSlotEpd,
  pageSlotEpdWeight,
} from "./backing.ts";
import { emitSeedCpFromSlot } from "./cp.ts";
import { Var, VarImpl, alignPageBoundary } from "./var.ts";
import { when } from "./temporal.ts";
import { ACT_FIELD_OFFSETS } from "./stock-spec.ts";
import {
  type AnyTriggerStructInstance,
  type AnyTriggerStructSchema,
  type TriggerStructInstanceOf,
} from "./struct.ts";
import {
  CURRENT_PLAYER,
  EPD_CURRENT_PLAYER,
  EudslError,
  describeValue,
  emitBitTestRead,
  requirePlainInt,
  warnLazyInBuilder,
  type TriggerFieldRef,
} from "./view.ts";

export { ELEM_MASK, PageSlotEPD, pageSlotBitCond, pageSlotEpd, pageSlotEpdWeight };

/**
 * Minimum `length` at which a dynamic `get` switches from the O(N)
 * per-element unroll to the PageSlotEPD pointer reader. The measured static
 * crossover for the shipped `zero` restore is N=14 (executed N=39); 64 is
 * comfortably past both and keeps the cost-gate fixture's N=4 dynamic read on
 * the unroll, so the frozen goldens do not move. Mirrored in `py/array.py` as
 * `DYNAMIC_GET_MIN_LENGTH` (there is no shared constants module); both sides
 * pin the value in a test.
 */
export const DYNAMIC_GET_MIN_LENGTH = 64;

export type FixedArrayElemType =
  | "byte"
  | "word"
  | "dword"
  | "var"
  | AnyTriggerStructSchema;

function rawEpdReset(destField: TriggerFieldRef, srcVar: VarImpl): HirAct {
  return {
    mask: 0xffffffff,
    strid: 0,
    wavid: 0,
    time: 0,
    dest: 0,
    value: 0,
    unit: 0,
    acttype: 45,
    modifier: SetTo,
    flags: 20,
    padding: 0,
    eudx: 0x4353,
    relocs: [
      {
        field_offset: 16,
        target_trigger: destField.triggerId,
        target_offset: destField.alignedByteOffset,
        mode: 1,
      },
      {
        field_offset: ACT_FIELD_OFFSETS.value,
        target_var: srcVar.id,
        target_offset: 20,
        mode: 1,
      },
    ],
  };
}

function isStructSchema(elem: unknown): elem is AnyTriggerStructSchema {
  return (
    typeof elem === "function" &&
    typeof (elem as { instantiate?: unknown }).instantiate === "function"
  );
}

export interface FixedArrayLike<T = VarImpl> {
  readonly length: number;
  get(index: number | VarImpl): T;
}

export class FixedArrayImpl<T = VarImpl> implements FixedArrayLike<T> {
  readonly length: number;
  readonly elemType: FixedArrayElemType;
  readonly mask: number;
  readonly bits: number;
  readonly pageCount: number;
  readonly pages: number;
  readonly isConst = false;

  // Eager backing arrays. The resolving getters below return them verbatim
  // for eager handles, so the eager path is unchanged; for deferred handles
  // they stay null and every read delegates to the active builder's binding.
  private _vars: VarImpl[] | null = null;
  private _instances: AnyTriggerStructInstance[] | null = null;

  // Deferred-mode state (null for eager handles). The shared LazyBinding
  // owns the binding map, resolution count, and first-binding capture.
  private readonly deferred: LazyBinding<FixedArrayImpl<T>> | null;

  constructor(
    elem: FixedArrayElemType,
    length: number,
    // INTERNAL deferred flag (globals P2-01; not public API, not exported
    // as a user option — the public path is `lazyFixedArray`): when set,
    // skip page alignment, element allocation, and struct instantiation.
    // The handle resolves its arrays on first use through a per-builder
    // binding (see `resolve`).
    deferred = false,
  ) {
    if (!Number.isInteger(length) || length <= 0) {
      throw new EudslError("EUDSL-BOUNDS", `FixedArray length must be a positive integer, got ${length}`);
    }
    this.length = length;
    this.elemType = elem;
    this.bits = Math.max(1, Math.ceil(Math.log2(length)));
    this.mask = typeof elem === "string" ? (ELEM_MASK[elem] ?? 0xffffffff) : 0xffffffff;
    this.deferred = deferred
      ? new LazyBinding<FixedArrayImpl<T>>("FixedArray", "buildModule")
      : null;
    if (deferred) {
      // Eager type validation: a bad element type throws at declaration
      // with the eager error code, before any binding exists.
      if (!isStructSchema(elem) && (typeof elem !== "string" || ELEM_MASK[elem] === undefined)) {
        throw new EudslError("EUDSL-BOUNDS", `Unknown FixedArray element type: ${String(elem)}`);
      }
      this.pageCount = isStructSchema(elem) ? length : Math.ceil(length / ACT_COUNT);
      this.pages = this.pageCount;
      return;
    }

    const vars: VarImpl[] = [];
    const instances: AnyTriggerStructInstance[] = [];
    if (isStructSchema(elem)) {
      // One TriggerPage per struct element, with the K fields at slots 0..K-1.
      for (let i = 0; i < length; i++) {
        alignPageBoundary();
        instances.push(elem.instantiate());
      }
      for (const inst of instances) vars.push(...inst.fields);
      this.pageCount = length;
    } else {
      if (typeof elem !== "string" || ELEM_MASK[elem] === undefined) {
        throw new EudslError("EUDSL-BOUNDS", `Unknown FixedArray element type: ${String(elem)}`);
      }
      alignPageBoundary();
      for (let i = 0; i < length; i++) {
        vars.push(Var(0, undefined, this.mask, "page"));
      }
      this.pageCount = Math.ceil(length / ACT_COUNT);
    }
    this.pages = this.pageCount;
    this._vars = vars;
    this._instances = instances;
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving array reads: this for eager handles, the
   * active builder's binding for deferred ones.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * atomic eager `FixedArray` construction varies here (never per-element,
   * R3).
   */
  private resolve(): FixedArrayImpl<T> {
    if (!this.deferred) {
      return this;
    }
    return this.deferred.resolve(
      () => new FixedArrayImpl<T>(this.elemType, this.length),
    );
  }

  get vars(): VarImpl[] {
    return this.resolve()._vars!;
  }

  get instances(): AnyTriggerStructInstance[] {
    return this.resolve()._instances!;
  }

  private check(index: number): number {
    if (!Number.isInteger(index) || index < 0 || index >= this.length) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `FixedArray index ${index} out of bounds for length ${this.length}`,
      );
    }
    return index;
  }

  get(index: number): T;
  get(index: VarImpl): T;
  get(index: number | VarImpl): T {
    if (typeof index === "number") {
      const i = this.check(index);
      return (this.instances.length > 0 ? this.instances[i] : this.vars[i]) as unknown as T;
    }
    if (this.instances.length > 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "Dynamic indexing into FixedArray(TriggerStruct) requires a constant page index",
      );
    }
    if (this.length >= DYNAMIC_GET_MIN_LENGTH) {
      return this.emitDynamicRead(index) as unknown as T;
    }
    // Correct materialization fallback: one gated branch per element. The
    // address math for writes below uses the carry-free PageSlotEPD pass.
    const out = Var(0);
    for (let k = 0; k < this.length; k++) {
      const elem = this.vars[k];
      when(index.eq(k), () => out.set(elem));
    }
    return out as unknown as T;
  }

  set(index: number, value: number | VarImpl | AnyTriggerStructInstance): this;
  set(index: VarImpl, value: number | VarImpl): this;
  set(
    index: number | VarImpl,
    value: number | VarImpl | AnyTriggerStructInstance,
  ): this {
    if (typeof index === "number") {
      const i = this.check(index);
      if (this.instances.length > 0) {
        const dst = this.instances[i];
        if (typeof value === "object" && value !== null && "fields" in value) {
          dst.set(value as AnyTriggerStructInstance);
        } else if (typeof value === "number") {
          for (const field of dst.fields) field.set(value);
        } else {
          throw new EudslError("EUDSL-BOUNDS", "Struct array set requires a struct or number");
        }
        return this;
      }
      this.vars[i].set(value as number | VarImpl);
      return this;
    }

    if (this.instances.length > 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "Dynamic indexing into FixedArray(TriggerStruct) requires a constant page index",
      );
    }
    if (!(typeof value === "number" && Number.isInteger(value)) && !(value instanceof VarImpl)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `array element must be a plain integer or a Var; got ${describeValue(value)}`,
      );
    }
    this.emitDynamicWrite(index, value as number | VarImpl);
    return this;
  }

  /** Set all elements to `value` (default `0`). One action per slot: 1T/page. */
  clear(value = 0): this {
    if (this.instances.length > 0) {
      for (const inst of this.instances) {
        for (const field of inst.fields) field.set(value);
      }
    } else {
      for (const v of this.vars) v.set(value);
    }
    return this;
  }

  fill(value = 0): this {
    return this.clear(value);
  }

  /** Copy every element from another array of equal length (batched per page). */
  setAll(src: FixedArrayImpl<T> | readonly number[]): this {
    const srcLen = "length" in src ? src.length : (src as readonly number[]).length;
    if (srcLen !== this.length) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `FixedArray copy length mismatch: ${this.length} vs ${srcLen}`,
      );
    }
    if (this.instances.length > 0) {
      const other = src as FixedArrayImpl<T>;
      for (let i = 0; i < this.length; i++) {
        this.instances[i].set(other.instances[i]);
      }
    } else {
      for (let i = 0; i < this.length; i++) {
        const v = (src as FixedArrayImpl<T>).vars
          ? (src as FixedArrayImpl<T>).vars[i]
          : (src as readonly number[])[i];
        this.vars[i].set(v as number | VarImpl);
      }
    }
    return this;
  }

  copyFrom(src: FixedArrayImpl<T> | readonly number[]): this {
    return this.setAll(src);
  }

  private emitDynamicWrite(index: VarImpl, value: number | VarImpl): void {
    const elem0 = this.vars[0];
    const flag = Var(0);
    const pointer = new Trigger({
      conds: [flag.eq(1)],
      acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff), flag.act_set(0)],
      register: false,
    });
    const destField = pointer.act(0).dest;

    // Reset the runtime element pointer to EPD(element 0) and arm the write.
    const resetActs: HirAct[] = [rawEpdReset(destField, elem0), flag.act_set(1)];
    if (typeof value === "number") {
      resetActs.push(pointer.act(0).value.act_set(value));
    } else {
      resetActs.push(pointer.act(0).value.act_set(0));
    }
    new Trigger({ acts: resetActs });

    // Carry-free PageSlotEPD(0, index) accumulation.
    for (let b = 0; b < this.bits; b++) {
      const weight = pageSlotEpdWeight(b);
      new Trigger({
        conds: [flag.eq(1), pageSlotBitCond(index, b)],
        acts: [destField.act_add(weight)],
      });
    }

    if (typeof value !== "number") {
      // One VarStore replaces the 32 value-bit triggers: the setup act points
      // the source var's TriggerPage slot at this pointer's `.value` field, so
      // the page firing writes the whole value in one out-of-band firing. The
      // flag is always 1 at this point (the reset trigger set it and only the
      // pointer clears it), so no extra gate is emitted.
      emitVarStore(
        value.id,
        TriggerFieldReloc(pointer.id, pointer.act(0).value.byteOffset),
      );
    }

    // Register the write only after the reset and all address/value patch
    // triggers so one loop computes the full address before firing it.
    _registerTrigger(pointer);
  }

  /**
   * Read `this.vars[index]` through the `CurrentPlayer` register.
   *
   * The index bits patch CurrentPlayer from `EPD(elem0.value)` with the same
   * carry-free PageSlotEPD weights the write path uses, so the address costs
   * one trigger per index bit instead of one branch per element. The dword is
   * then unpacked with 32 bit tests.
   *
   * Every bit test carries the `index < length` guard: the unroll leaves `out`
   * at 0 when no branch matches, and the reader must agree. The guard adds
   * conditions only (no triggers, no bytes).
   *
   * CurrentPlayer is left at the canonical loop-start value `0` (ADR 0004, the
   * `zero` policy). A non-zero ambient value is not preserved: treat CP as
   * scratch across a dynamic `get` (see GLOSSARY.md, CP).
   */
  private emitDynamicRead(index: VarImpl): VarImpl {
    emitSeedCpFromSlot(this.vars[0]);
    for (let bit = 0; bit < this.bits; bit++) {
      const act = SetDeaths(EPD_CURRENT_PLAYER, Add, pageSlotEpdWeight(bit), 0);
      new Trigger({ conds: [pageSlotBitCond(index, bit)], acts: [act] });
    }
    const out = Var(0);
    new Trigger({ conds: [], acts: [out.act_set(0)], preserved: true });
    emitBitTestRead(CURRENT_PLAYER, 32, 0, out, undefined, undefined, [
      index.lt(this.length),
    ]);
    const zero = SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
    new Trigger({ conds: [], acts: [zero] });
    return out;
  }

  eq(index: number, rhs: number): HirCond {
    return (this.vars[this.check(index)] as VarImpl).eq(rhs);
  }

  ge(index: number, rhs: number): HirCond {
    return (this.vars[this.check(index)] as VarImpl).ge(rhs);
  }

  le(index: number, rhs: number): HirCond {
    return (this.vars[this.check(index)] as VarImpl).le(rhs);
  }
}

/** Build-time-only value returned by `FixedArray.const(...).get(k)`. */
export class ConstFixedArrayValue {
  readonly value: number;
  constructor(value: number) {
    this.value = requirePlainInt(value, "const array value") >>> 0;
  }
  private cmp(rhs: number, kind: "eq" | "ne" | "ge" | "le" | "gt" | "lt"): HirCond {
    const other = requirePlainInt(rhs, "const array comparison") >>> 0;
    const ok =
      kind === "eq"
        ? this.value === other
        : kind === "ne"
          ? this.value !== other
          : kind === "ge"
            ? this.value >= other
            : kind === "le"
              ? this.value <= other
              : kind === "gt"
                ? this.value > other
                : this.value < other;
    return ok ? Always() : Never();
  }
  eq(rhs: number): HirCond {
    return this.cmp(rhs, "eq");
  }
  ne(rhs: number): HirCond {
    return this.cmp(rhs, "ne");
  }
  ge(rhs: number): HirCond {
    return this.cmp(rhs, "ge");
  }
  le(rhs: number): HirCond {
    return this.cmp(rhs, "le");
  }
  gt(rhs: number): HirCond {
    return this.cmp(rhs, "gt");
  }
  lt(rhs: number): HirCond {
    return this.cmp(rhs, "lt");
  }
  read(): VarImpl {
    return Var(this.value);
  }
  valueOf(): number {
    return this.value;
  }
  [Symbol.toPrimitive](): number {
    return this.value;
  }
}

/** Read-only payload-backed constant table (zero TriggerPages). */
export class ConstFixedArrayImpl {
  readonly length: number;
  readonly values: readonly number[];
  readonly isConst = true;
  readonly pageCount = 0;
  readonly pages = 0;
  readonly vars: readonly VarImpl[] = [];

  constructor(values: readonly number[]) {
    if (!Array.isArray(values)) {
      throw new EudslError("EUDSL-BOUNDS", "FixedArray.const requires a literal array");
    }
    this.values = values.map((v) => requirePlainInt(v, "const array value") >>> 0);
    this.length = this.values.length;
  }

  get(index: number): ConstFixedArrayValue {
    if (!Number.isInteger(index) || index < 0 || index >= this.length) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `FixedArray.const index ${index} out of bounds for length ${this.length}`,
      );
    }
    return new ConstFixedArrayValue(this.values[index]);
  }

  set(): never {
    throw new EudslError("EUDSL-BOUNDS", "FixedArray.const is read-only");
  }
}

export type FixedArrayConst = ConstFixedArrayImpl;

export interface FixedArrayFactory {
  <S extends AnyTriggerStructSchema>(
    elem: S,
    length: number,
  ): FixedArrayImpl<TriggerStructInstanceOf<S>>;
  <T = VarImpl>(elem: FixedArrayElemType, length: number): FixedArrayImpl<T>;
  const(values: readonly number[]): ConstFixedArrayImpl;
}

function makeFixedArray<T>(elem: FixedArrayElemType, length: number): FixedArrayImpl<T> {
  return new FixedArrayImpl<T>(elem, length);
}

/** Unified fixed-capacity array factory. */
export const FixedArray = Object.assign(makeFixedArray, {
  const: (values: readonly number[]): ConstFixedArrayImpl => new ConstFixedArrayImpl(values),
}) as FixedArrayFactory;

/**
 * Declare a lazy `FixedArray` (globals P2-01): a handle constructible
 * anywhere (no builder needed) that resolves to a fully eager binding on
 * first resolving use inside `buildModule`, one binding per builder.
 *
 * Binding creation is atomic: one full eager `FixedArray` construction —
 * page alignment plus ALL element vars, or ALL struct instances, in one
 * uninterrupted run. There is no per-element laziness and no init form:
 * elements always start at zero, exactly like the eager constructor. A
 * never-used handle emits nothing (no decl, no page slot, no statement,
 * D5). This factory is the only documented path to deferred mode; the
 * internal constructor flag stays undocumented.
 */
export function lazyFixedArray<S extends AnyTriggerStructSchema>(
  elem: S,
  length: number,
): FixedArrayImpl<TriggerStructInstanceOf<S>>;
export function lazyFixedArray<T = VarImpl>(
  elem: FixedArrayElemType,
  length: number,
): FixedArrayImpl<T>;
export function lazyFixedArray<T = VarImpl>(
  elem: FixedArrayElemType,
  length: number,
): FixedArrayImpl<T> {
  warnLazyInBuilder();
  return new FixedArrayImpl<T>(elem, length, true);
}

/** Raw element type tokens (prefixed to avoid colliding with struct width helpers). */
export const arrayByte = "byte" as const;
export const arrayWord = "word" as const;
export const arrayDword = "dword" as const;
export const arrayVar = "var" as const;
