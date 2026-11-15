/**
 * First-class `Trigger (16C, 64A)` and `TriggerPage` primitives.
 */

import { EudslError, TriggerFieldRef } from "./view.ts";
import { ACT_FIELD_OFFSETS } from "./stock-spec.ts";
import {
  buildCondPlans,
  emitCondPlans,
  makePlanHooks,
  PlanContext,
  plannedCondCount,
  type GatePlan,
  type PlanHooks,
} from "./conditions.ts";
import { when } from "./temporal.ts";
import { Var } from "./var.ts";

export const COND_COUNT = 16;
export const COND_BASE_BYTES = 8;
export const COND_STRIDE_BYTES = 20;
export const COND_STRIDE_EPDS = 5;

export const ACT_COUNT = 64;
export const ACT_BASE_BYTES = 328;
export const ACT_STRIDE_BYTES = 32;
export const ACT_STRIDE_EPDS = 8;

export const TRIGGER_FLAGS_OFFSET_BYTES = 2376;
export const TRIGGER_SIZE_BYTES = 2408;
export const TRIGGER_STRIDE_EPDS = 602;

/**
 * Carry-free EPD weight of bit `b` when indexing a `64A` `TriggerPage` array:
 * - `b < 6`: intra-page action slot `slot = i & 63` (`8 << b` EPDs)
 * - `b >= 6`: page index `page = i >> 6` (`602 << (b - 6)` EPDs)
 */
export function pageSlotEpdWeight(bit: number): number {
  if (!Number.isInteger(bit) || bit < 0 || bit >= 32) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `pageSlotEpdWeight: bit must be in 0..31, got ${bit}`,
    );
  }
  return (bit < 6 ? ACT_STRIDE_EPDS << bit : TRIGGER_STRIDE_EPDS << (bit - 6)) >>> 0;
}

export interface ActionSlotInit {
  readonly slot: number;
  readonly mask: number;
  readonly value: number;
  /** True when `dest == EPD(self.act[slot].value)` at rest (Self-Targeted Action Invariant). */
  readonly selfTargeted: boolean;
}

export class TriggerPage {
  readonly slots: ActionSlotInit[];

  constructor(initialValues: readonly number[] = [], masks?: readonly number[]) {
    if (initialValues.length > ACT_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `TriggerPage holds at most ${ACT_COUNT} action slots, got ${initialValues.length}`,
      );
    }
    this.slots = Array.from({ length: ACT_COUNT }, (_, k) => ({
      slot: k,
      mask: (masks?.[k] ?? 0xffffffff) >>> 0,
      value: (initialValues[k] ?? 0) >>> 0,
      selfTargeted: true,
    }));
  }
}

export interface HirReloc {
  readonly field_offset: number;
  readonly target_trigger?: number;
  readonly target_var?: number;
  /** 0-based module string index; lowering folds it to an absolute address/EPD. */
  readonly target_string?: number;
  /** 0-based module blob index; lowering folds it to the blob's payload address/EPD. */
  readonly target_blob?: number;
  readonly target_offset: number;
  readonly mode: 1 | 4;
}

/** A 4-byte pointer field inside a HIR data blob (`Db`). */
export interface HirBlobRelocNode {
  readonly offset: number;
  readonly target_blob?: number;
  readonly target_trigger?: number;
  readonly target_offset: number;
  readonly mode: 1 | 4;
}

/**
 * Author-facing blob relocation: every field is optional except `offset`, and
 * `target` may name a `Db`/`DbRef` or a trigger handle instead of a raw index.
 */
export interface HirBlobRelocInput {
  readonly offset: number;
  readonly target_blob?: number;
  readonly target_trigger?: number;
  readonly target_offset?: number;
  readonly mode?: 1 | 4;
}

export interface HirCond {
  readonly locid: number;
  readonly player: number;
  readonly amount: number;
  readonly unit: number;
  readonly comparison: number;
  readonly condtype: number;
  readonly restype: number;
  readonly flags: number;
  readonly eudx: number;
  readonly relocs?: readonly HirReloc[];
}

/** A condition recorded as intent and expanded at its evaluation site. */
export interface MaterializableCond {
  materialize(): HirCond;
}

/**
 * A composite condition marker: it is expanded through a {@link GatePlan} and
 * is deliberately *not* `MaterializableCond` (it has no `materialize()`).
 */
export interface CompositeCond {
  gatePlan(): GatePlan;
  complement(): CondLike;
}

export type CondLike = HirCond | MaterializableCond | CompositeCond;

/** Hooks for one raw-`Trigger` evaluation site. */
function triggerPlanHooks(): PlanHooks {
  return makePlanHooks(
    () => activeModuleBuilder !== null,
    () => Var(0),
    (flag, value) => {
      new Trigger({
        conds: [],
        acts: [flag.act_set(value)],
        preserved: true,
      });
    },
    (conds, flag, value) => {
      when(conds, () => {
        flag.set(value);
      });
    },
  );
}

export interface HirAct {
  readonly mask: number;
  readonly strid: number;
  readonly wavid: number;
  readonly time: number;
  readonly dest: number;
  readonly value: number;
  readonly unit: number;
  readonly acttype: number;
  readonly modifier: number;
  readonly flags: number;
  readonly padding: number;
  readonly eudx: number;
  readonly text?: string;
  /** Optional WAV sound name, resolved to `wavid` during lowering. */
  readonly wav?: string;
  /** Normalized 20-byte UPRP record resolved during map assembly. */
  readonly uprp?: readonly number[];
  /** 0-based module string index displayed by a `DisplayText` action. */
  readonly str_index?: number;
  readonly relocs?: readonly HirReloc[];
}

export type HirTriggerPtr =
  | { readonly kind: "const"; readonly value: number }
  | { readonly kind: "trigger"; readonly id: number; readonly offset: number };

/**
 * Bound 20-byte (`5`-EPD) `CondView` slot returned by `trig.cond(i)`.
 */
export class CondSlotView {
  static readonly BYTE_SIZE = COND_STRIDE_BYTES;
  static readonly EPD_SIZE = COND_STRIDE_EPDS;

  readonly triggerId: number;
  readonly index: number;
  readonly byteOffset: number;
  readonly epdOffset: number;
  readonly data?: HirCond;

  readonly mask: TriggerFieldRef;
  readonly locid: TriggerFieldRef;
  readonly player: TriggerFieldRef;
  readonly amount: TriggerFieldRef;
  readonly unit: TriggerFieldRef;
  readonly comparison: TriggerFieldRef;
  readonly condtype: TriggerFieldRef;
  readonly restype: TriggerFieldRef;
  readonly flags: TriggerFieldRef;
  readonly eudx: TriggerFieldRef;

  constructor(triggerId: number, index: number, data?: HirCond) {
    this.triggerId = triggerId;
    this.index = index;
    this.byteOffset = COND_BASE_BYTES + index * COND_STRIDE_BYTES;
    this.epdOffset = Math.floor(this.byteOffset / 4);
    this.data = data;

    const base = this.byteOffset;
    this.mask = new TriggerFieldRef(triggerId, base + 0, 4, data?.locid ?? 0);
    this.locid = this.mask;
    this.player = new TriggerFieldRef(triggerId, base + 4, 4, data?.player ?? 0);
    this.amount = new TriggerFieldRef(triggerId, base + 8, 4, data?.amount ?? 0);
    this.unit = new TriggerFieldRef(triggerId, base + 12, 2, data?.unit ?? 0);
    this.comparison = new TriggerFieldRef(triggerId, base + 14, 1, data?.comparison ?? 0);
    this.condtype = new TriggerFieldRef(triggerId, base + 15, 1, data?.condtype ?? 0);
    this.restype = new TriggerFieldRef(triggerId, base + 16, 1, data?.restype ?? 0);
    this.flags = new TriggerFieldRef(triggerId, base + 17, 1, data?.flags ?? 0);
    this.eudx = new TriggerFieldRef(triggerId, base + 18, 2, data?.eudx ?? 0);
  }
}

/**
 * Bound 32-byte (`8`-EPD) `ActView` slot returned by `trig.act(k)`.
 */
export class ActSlotView {
  static readonly BYTE_SIZE = ACT_STRIDE_BYTES;
  static readonly EPD_SIZE = ACT_STRIDE_EPDS;

  readonly triggerId: number;
  readonly index: number;
  readonly byteOffset: number;
  readonly epdOffset: number;
  readonly data?: HirAct;

  readonly mask: TriggerFieldRef;
  readonly locid1: TriggerFieldRef;
  readonly strid: TriggerFieldRef;
  readonly wavid: TriggerFieldRef;
  readonly time: TriggerFieldRef;
  readonly dest: TriggerFieldRef;
  readonly player1: TriggerFieldRef;
  readonly value: TriggerFieldRef;
  readonly player2: TriggerFieldRef;
  readonly unit: TriggerFieldRef;
  readonly acttype: TriggerFieldRef;
  readonly modifier: TriggerFieldRef;
  readonly amount: TriggerFieldRef;
  readonly flags: TriggerFieldRef;
  readonly pad: TriggerFieldRef;
  readonly padding: TriggerFieldRef;
  readonly eudx: TriggerFieldRef;

  constructor(triggerId: number, index: number, data?: HirAct) {
    this.triggerId = triggerId;
    this.index = index;
    this.byteOffset = ACT_BASE_BYTES + index * ACT_STRIDE_BYTES;
    this.epdOffset = Math.floor(this.byteOffset / 4);
    this.data = data;

    const base = this.byteOffset;
    this.mask = new TriggerFieldRef(triggerId, base + 0, 4, data?.mask ?? 0);
    this.locid1 = this.mask;
    this.strid = new TriggerFieldRef(triggerId, base + 4, 4, data?.strid ?? 0);
    this.wavid = new TriggerFieldRef(triggerId, base + 8, 4, data?.wavid ?? 0);
    this.time = new TriggerFieldRef(triggerId, base + 12, 4, data?.time ?? 0);
    this.dest = new TriggerFieldRef(triggerId, base + 16, 4, data?.dest ?? 0);
    this.player1 = this.dest;
    this.value = new TriggerFieldRef(triggerId, base + 20, 4, data?.value ?? 0);
    this.player2 = this.value;
    this.unit = new TriggerFieldRef(triggerId, base + 24, 2, data?.unit ?? 0);
    this.acttype = new TriggerFieldRef(triggerId, base + 26, 1, data?.acttype ?? 0);
    this.modifier = new TriggerFieldRef(triggerId, base + 27, 1, data?.modifier ?? 0);
    this.amount = this.modifier;
    this.flags = new TriggerFieldRef(triggerId, base + 28, 1, data?.flags ?? 0);
    this.pad = new TriggerFieldRef(triggerId, base + 29, 1, data?.padding ?? 0);
    this.padding = this.pad;
    this.eudx = new TriggerFieldRef(triggerId, base + 30, 2, data?.eudx ?? 0);
  }
}

export interface TriggerOptions {
  readonly id?: number;
  readonly conds?: readonly CondLike[];
  readonly acts?: readonly HirAct[];
  readonly preserved?: boolean;
  readonly flags?: number;
  readonly prev?: TriggerImpl | number | null;
  readonly next?: TriggerImpl | number | null;
  /** Internal: allocate the ID and field refs but defer chain registration. */
  readonly register?: boolean;
}

/**
 * Builder seam contract (candidate 8, shape C).
 *
 * The hook interface is the ONLY channel through which DSL values reach the
 * active `buildModule` closure: allocation, registration, scope entry/exit,
 * and scope-exit flushing all go through here, and every method is optional
 * except the trigger methods so callers must guard with `typeof` checks.
 * The implementation and all mutable state (ids, counters, scope stack,
 * flusher set) live with `buildModule` in `hir.ts`; this module owns only
 * the interface plus the module-global active-builder slot below. Do NOT
 * move the slot into `hir.ts`: the `Trigger` constructor needs the getter,
 * and a trigger -> hir runtime import would create a real top-level cycle
 * in the Python mirror and invert the TypeScript graph.
 *
 * Ordering guarantees callers may rely on:
 * - `registerTrigger` appends to the CURRENT scope's entries, so a trigger's
 *   HIR position is its registration position, not its construction
 *   position (see `_registerTrigger` below).
 * - `pushScope`/`popScope` (on `ScopeBuilderHook`) run every registered
 *   flusher BEFORE the scope change and bump the scope serial, so values
 *   cached against a serial (e.g. `FixedString` static positions) go stale at
 *   every boundary; `buildModule` also runs all flushers once at module end.
 *   Scope-boundary flushes call each flusher with align=true; the
 *   module-end flush calls it with align=false.
 * - Flushers are held in a `Set` in this mirror, so registering the same
 *   function twice runs it once per flush; the Python mirror uses a list
 *   and runs it twice. That divergence is pinned as-is by the seam tests
 *   (`test_builder_seam.ts`); do NOT unify it without a defect-level reason.
 */
export interface ModuleBuilderHook {
  allocTriggerId(): number;
  registerTrigger(trig: TriggerImpl): void;
  consumeQueuedActs(acts: readonly HirAct[]): void;
  allocVar?(init: number, mask?: number, storage?: "page"): number;
  allocStringVar?(index: number, offset: number, mask?: number): number;
  allocString?(text: string): number;
  getNextStringIndex?(): number;
  getScopeSerial?(): number;
  registerFlusher?(fn: (align: boolean) => void): void;
  queueVarTransfer?(dstVar: number, srcVar: number, modifier: number): void;
  getNextVarId?(): number;
  getNextPageSlot?(): number;
  allocBlob?(
    payload: number | Uint8Array,
    relocs?: readonly HirBlobRelocInput[],
  ): { readonly id: number; readonly size: number };
}

let activeModuleBuilder: ModuleBuilderHook | null = null;
let fallbackTriggerCounter = 0;

export function _setActiveModuleBuilder(
  builder: ModuleBuilderHook | null,
): ModuleBuilderHook | null {
  const prev = activeModuleBuilder;
  activeModuleBuilder = builder;
  return prev;
}

export function _getActiveModuleBuilder(): ModuleBuilderHook | null {
  return activeModuleBuilder;
}

function normalizePtr(
  ptr: TriggerImpl | number | null | undefined,
): HirTriggerPtr | undefined {
  if (ptr === undefined || ptr === null) {
    return undefined;
  }
  if (typeof ptr === "number") {
    return { kind: "const", value: ptr };
  }
  return { kind: "trigger", id: ptr.id, offset: 0 };
}

class TriggerImpl {
  static readonly COND_COUNT = COND_COUNT;
  static readonly ACT_COUNT = ACT_COUNT;
  static readonly BYTE_SIZE = TRIGGER_SIZE_BYTES;
  static readonly EPD_SIZE = TRIGGER_STRIDE_EPDS;

  readonly id: number;
  readonly conds: readonly HirCond[];
  readonly acts: readonly HirAct[];
  readonly preserved: boolean;
  readonly flags: number;
  readonly prevPtr?: HirTriggerPtr;
  readonly nextPtr?: HirTriggerPtr;
  readonly prev: TriggerFieldRef;
  readonly next: TriggerFieldRef;
  readonly flagsField: TriggerFieldRef;

  constructor(opts: TriggerOptions = {}) {
    // A raw Trigger is itself an evaluation site. Plan (validate) the whole
    // condition list first and count the expanded slots BEFORE emitting any
    // prelude, so an over-cap trigger cannot leak flag Vars or reset/set-when
    // triggers into the enclosing buildModule.
    const rawConds = opts.conds ?? [];
    const plans = buildCondPlans(rawConds);
    const planned = plans.reduce(
      (total, plan) => total + plannedCondCount(plan),
      0,
    );
    const acts = opts.acts ? [...opts.acts] : [];

    if (planned > COND_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger condition count ${planned} exceeds maximum ${COND_COUNT}`,
      );
    }
    if (acts.length > ACT_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger action count ${acts.length} exceeds maximum ${ACT_COUNT}`,
      );
    }
    const conds = emitCondPlans(
      plans,
      new PlanContext(triggerPlanHooks()),
    );

    const builder = activeModuleBuilder;
    if (opts.id !== undefined) {
      this.id = opts.id;
      if (builder) {
        builder.consumeQueuedActs(acts);
      }
    } else if (builder) {
      builder.consumeQueuedActs(acts);
      this.id = builder.allocTriggerId();
    } else {
      this.id = fallbackTriggerCounter++;
    }

    this.conds = Object.freeze(conds);
    this.acts = Object.freeze(acts);
    this.preserved = opts.preserved ?? true;
    this.flags = (opts.flags ?? (this.preserved ? 4 : 0)) >>> 0;
    this.prevPtr = normalizePtr(opts.prev);
    this.nextPtr = normalizePtr(opts.next);

    this.prev = new TriggerFieldRef(this.id, 0, 4, 0);
    this.next = new TriggerFieldRef(this.id, 4, 4, 0);
    this.flagsField = new TriggerFieldRef(
      this.id,
      TRIGGER_FLAGS_OFFSET_BYTES,
      4,
      this.flags,
    );

    if (builder && opts.register !== false) {
      builder.registerTrigger(this);
    }
  }

  condByteOffset(index: number): number {
    if (!Number.isInteger(index) || index < 0 || index >= COND_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger.cond index must be in 0..15, got ${index}`,
      );
    }
    return COND_BASE_BYTES + index * COND_STRIDE_BYTES;
  }

  actByteOffset(index: number): number {
    if (!Number.isInteger(index) || index < 0 || index >= ACT_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger.act index must be in 0..63, got ${index}`,
      );
    }
    return ACT_BASE_BYTES + index * ACT_STRIDE_BYTES;
  }

  /**
   * Direct condition slot inspection (`0 <= index < 16`).
   * Returns a bound `CondSlotView` (`5`-EPD / `20`-byte stride starting at `+8`).
   */
  cond(index: number): CondSlotView {
    if (!Number.isInteger(index) || index < 0 || index >= COND_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger.cond index must be in 0..15, got ${index}`,
      );
    }
    return new CondSlotView(this.id, index, this.conds[index]);
  }

  /**
   * Direct action slot inspection (`0 <= index < 64`).
   * Returns a bound `ActSlotView` (`8`-EPD / `32`-byte stride starting at `+328`).
   */
  act(index: number): ActSlotView {
    if (!Number.isInteger(index) || index < 0 || index >= ACT_COUNT) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `Trigger.act index must be in 0..63, got ${index}`,
      );
    }
    return new ActSlotView(this.id, index, this.acts[index]);
  }

  /**
   * Splice through this trigger once and return to the continuation trigger in 1T executed.
   */
  fire(): { dispatch: TriggerImpl; continuation: TriggerImpl } {
    const builder = _getActiveModuleBuilder();
    const contId = builder ? builder.allocTriggerId() : fallbackTriggerCounter++;
    const dispatch = new TriggerImpl({
      acts: [
        {
          mask: 0,
          strid: 0,
          wavid: 0,
          time: 0,
          dest: 0,
          value: 0,
          unit: 0,
          acttype: 45,
          modifier: 7,
          flags: 0,
          padding: 0,
          eudx: 0,
          relocs: [
            {
              field_offset: 16,
              target_trigger: this.id,
              target_offset: 4,
              mode: 1,
            },
            {
              field_offset: ACT_FIELD_OFFSETS.value,
              target_trigger: contId,
              target_offset: 0,
              mode: 4,
            },
          ],
        },
      ],
      next: this,
    });
    const cont = new TriggerImpl({ id: contId, preserved: true });
    return { dispatch, continuation: cont };
  }
}

/**
 * Register a trigger previously created with `{ register: false }`.
 *
 * This is the seam's explicit-registration API, NOT a bypass around the
 * builder: with an active builder it calls `builder.registerTrigger(trig)`,
 * and with no active builder it is a no-op. Call sites (queue probe, array
 * pointer, blockio pointers, string pointer) construct the trigger
 * unregistered, patch its slots (e.g. `emitVarStore` into its fields),
 * then register it explicitly so its HIR position follows its setup acts
 * instead of preceding them. A trigger's HIR position is therefore its
 * registration position; construction alone never places it.
 */
export function _registerTrigger(trig: TriggerImpl): void {
  const builder = _getActiveModuleBuilder();
  if (builder) {
    builder.registerTrigger(trig);
  }
}

export const Trigger = new Proxy(TriggerImpl, {
  apply(_target, _thisArg, argArray: [TriggerOptions?]) {
    return new TriggerImpl(...argArray);
  },
}) as unknown as typeof TriggerImpl & ((opts?: TriggerOptions) => TriggerImpl);

export type Trigger = TriggerImpl;
