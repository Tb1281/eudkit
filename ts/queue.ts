/**
 * Fixed-capacity semantic containers: `Queue`, `Deque`, `Stack`, `UnitGroup`.
 *
 * `Queue`/`Deque` are ring buffers built from `Trigger (16C, 64A)`
 * **slot triggers**. Each slot trigger is both storage and reader: its
 * `act[0..K-1].dest` fields are bound statically to a container-owned
 * `out_box`, so a slot execution copies a complete scalar (`K = 1`) or
 * `TriggerStruct` (`K <= 64`) element in one trigger. `pop`/`popleft`
 * therefore never patch per-slot destinations; their site cost is
 * independent of capacity (16 and 128 cost the same).
 *
 * Writes patch the selected slot trigger's action *value* fields. Constant
 * elements need one site trigger per ring slot (exactly one fires because the
 * ring index is unique). Variable elements are written by one pointer trigger
 * plus one `VarStore` per element field: the pointer's `.dest` walks the
 * contiguous slot block at `TRIGGER_STRIDE_EPDS` and the store's out-of-band
 * `TriggerPage` firing delivers the value, instead of 32 bit-test triggers per
 * value bit per slot.
 *
 * `Stack` is the simpler `FixedArray` + `pos` form required by the storage
 * model. `UnitGroup` is a page-backed unit-EPD set with an optional
 * per-frame `autoPrune` sweep.
 *
 * Semantics:
 * - Fixed capacity; no dynamic growth.
 * - A full queue/deque keeps accepting appends by dropping the element
 *   at the opposite end (legacy ring behaviour).
 * - `pop`/`popleft` on empty is unchecked; guard with `empty()`.
 * - `clear()` resets pointers/length, not slot payloads.
 * - Ring slot reads happen at the start of a loop, so an element
 *   appended in a loop becomes visible to a pop in the next loop.
 */

import {
  Deaths,
  DeathsX,
  Exactly,
  SetDeaths,
  SetDeathsX,
  SetTo,
  TriggerFieldReloc,
  emitVarStore,
  type HirAct,
  type HirCond,
} from "./hir.ts";
import {
  TRIGGER_STRIDE_EPDS,
  Trigger,
  _registerTrigger,
} from "./trigger.ts";
import { LazyBinding } from "./lazy-binding.ts";
import { Var, VarImpl } from "./var.ts";
import {
  type AnyTriggerStructInstance,
  type AnyTriggerStructSchema,
} from "./struct.ts";
import {
  FixedArray,
  arrayDword,
  type FixedArrayElemType,
  type FixedArrayImpl,
} from "./array.ts";
import { ELEM_MASK, pageSlotBitCond } from "./backing.ts";
import { when } from "./temporal.ts";
import { cpEmpty, cpLive, emitCpReadInto, emitCpSweep } from "./cp.ts";
import {
  CURRENT_PLAYER,
  EudslError,
  POOL_NULL,
  describeValue,
  requirePlainInt,
  warnLazyInBuilder,
  type TriggerFieldRef,
} from "./view.ts";

export { POOL_NULL };

export type ContainerElem = "byte" | "word" | "dword" | "var" | AnyTriggerStructSchema;

function isStructSchema(elem: unknown): elem is AnyTriggerStructSchema {
  return (
    typeof elem === "function" &&
    typeof (elem as { instantiate?: unknown }).instantiate === "function"
  );
}

function checkCapacity(capacity: number, what: string): void {
  if (!Number.isInteger(capacity) || capacity <= 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `${what} capacity must be a positive integer, got ${String(capacity)}`,
    );
  }
}

interface ElementLayout {
  readonly make: () => VarImpl | AnyTriggerStructInstance;
}

function elementLayout(elem: ContainerElem): ElementLayout {
  if (isStructSchema(elem)) {
    return { make: () => elem.instantiate() };
  }
  if (typeof elem !== "string" || ELEM_MASK[elem] === undefined) {
    throw new EudslError("EUDSL-BOUNDS", `Unknown container element type: ${String(elem)}`);
  }
  const mask = ELEM_MASK[elem];
  return { make: () => Var(0, undefined, mask, "page") };
}

function boxFields(box: VarImpl | AnyTriggerStructInstance): VarImpl[] {
  return "fields" in box ? (box.fields as VarImpl[]) : [box as VarImpl];
}

/** One slot trigger per ring position; its action dests are the out-box. */
function makeSlotTriggers(
  elem: ContainerElem,
  capacity: number,
  selector: VarImpl,
  outBox: VarImpl | AnyTriggerStructInstance,
): Trigger[] {
  const fields = boxFields(outBox);
  return Array.from({ length: capacity }, (_, i) => {
    const acts = fields.map((f) => SetDeathsX(f, SetTo, 0, 0, f.bitmask));
    return new Trigger({ conds: [selector.eq(i)], acts });
  });
}

/**
 * Validate one element write and expand it to one value per slot field.
 *
 * One classification at the seam, before any trigger is emitted (ADR 0019):
 * the element domain is a plain integer, a `VarImpl`, or a struct instance.
 * Anything else used to fall through as a `Var` and build a silent value.
 * The slot has `count` field actions and the element must supply exactly that
 * many: a scalar expands to one value, so it only fits a scalar slot — do not
 * broadcast it into every field.
 */
function classifyElement(
  slots: readonly Trigger[],
  value: number | VarImpl | AnyTriggerStructInstance,
): (number | VarImpl)[] {
  const count = slots[0].acts.length;
  let values: (number | VarImpl)[];
  if (typeof value === "number" && Number.isInteger(value)) {
    values = [value];
  } else if (value instanceof VarImpl) {
    values = [value];
  } else if (typeof value === "object" && value !== null && "fields" in value) {
    values = (value as AnyTriggerStructInstance).fields as VarImpl[];
  } else {
    throw new EudslError(
      "EUDSL-TYPE",
      `container element must be a plain integer, a Var, or a struct; got ${describeValue(value)}`,
    );
  }
  if (values.length !== count) {
    throw new EudslError(
      "EUDSL-TYPE",
      `container element shape must match the element: expected ${count} field(s), got ${values.length}`,
    );
  }
  return values;
}

/**
 * Check the ring slot triggers form a contiguous trigger-id block.
 *
 * The pointer write addresses slot `n` as `EPD(slot 0) + TRIGGER_STRIDE_EPDS
 * * n`. That is only valid while the ring slots are adjacent in the emitted
 * trigger array, which holds because {@link makeSlotTriggers} creates them
 * back to back with no other trigger in between. Trigger ids are the
 * build-time witness; the emitted byte offsets are pinned by the slot-block
 * layout test.
 */
function requireContiguousSlotBlock(slots: readonly Trigger[]): void {
  const base = slots[0].id;
  for (let i = 0; i < slots.length; i++) {
    if (slots[i].id !== base + i) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        "container ring slots must be a contiguous trigger block " +
          `(slot ${i} is trigger ${slots[i].id}, expected ${base + i}); ` +
          `the VarStore slot pointer assumes ${TRIGGER_STRIDE_EPDS}-EPD trigger adjacency`,
      );
    }
  }
}

/**
 * Reset act: point `destField` at the absolute EPD of a slot field.
 *
 * Mirrors `rawEpdReset` in `array.ts`; the source is a field of the first
 * ring slot trigger instead of a page-backed var slot.
 */
function rawSlotEpdReset(
  destField: TriggerFieldRef,
  slot: Trigger,
  fieldOffset: number,
): HirAct {
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
        field_offset: 20,
        target_trigger: slot.id,
        target_offset: fieldOffset,
        mode: 1,
      },
    ],
  };
}

/**
 * Write one element field with one pointer trigger plus one VarStore.
 *
 * Address: pointer `.dest` starts at `EPD(slot 0.act[k].value)`; one
 * carry-free trigger per `selector` bit adds `TRIGGER_STRIDE_EPDS << b` so it
 * lands on the selected slot's field. Value: the VarStore points the source
 * var's TriggerPage slot at pointer `.value`, so one out-of-band page firing
 * delivers the whole dword; the pointer copies it into the selected slot.
 *
 * Two distinct offsets are in play and must not be conflated: the reset reads
 * the *slot's* field `k` (`field0.alignedByteOffset`, which is
 * `0x15c + 32 * k`), while the store writes the *pointer's* action 0 value
 * field (always `pointer.act(0).value`). Using the slot offset for the store
 * would hit the pointer's later actions (`flag.act_set(0)` for `k > 0`) and
 * silently drop every field but the first.
 */
function emitSlotFieldStore(
  slots: readonly Trigger[],
  selector: VarImpl,
  k: number,
  src: VarImpl,
): void {
  const field0 = slots[0].act(k).value;
  const flag = Var(0);
  const pointer = new Trigger({
    conds: [flag.eq(1)],
    // Same full-dword mask as the const slot write: the destination is the
    // slot's 32-byte action field, so no neighbouring field is at risk;
    // element width is applied by the slot's own read action.
    acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff), flag.act_set(0)],
    register: false,
  });
  const destField = pointer.act(0).dest;
  new Trigger({
    acts: [rawSlotEpdReset(destField, slots[0], field0.alignedByteOffset), flag.act_set(1)],
  });
  const bits = Math.max(1, Math.ceil(Math.log2(slots.length)));
  for (let b = 0; b < bits; b++) {
    new Trigger({
      conds: [flag.eq(1), pageSlotBitCond(selector, b)],
      acts: [destField.act_add(TRIGGER_STRIDE_EPDS << b)],
    });
  }
  emitVarStore(
    src.id,
    TriggerFieldReloc(pointer.id, pointer.act(0).value.byteOffset),
  );
  _registerTrigger(pointer);
}

/**
 * Write one element into the ring slot selected by `selector`.
 *
 * Constants keep one site trigger per slot (exactly one fires because the
 * write index is unique). Variable fields are written by one pointer plus
 * one VarStore per field, not by 32 bit-test triggers per slot.
 */
function emitSlotWrites(
  slots: readonly Trigger[],
  selector: VarImpl,
  values: readonly (number | VarImpl)[],
): void {
  const count = slots[0].acts.length;
  requireContiguousSlotBlock(slots);
  for (let i = 0; i < slots.length; i++) {
    const slot = slots[i];
    const constActs: HirAct[] = [];
    for (let k = 0; k < count; k++) {
      const src = values[k];
      if (typeof src === "number") {
        const field = slot.act(k).value;
        constActs.push(field.act_set((src & field.bitMask) >>> 0));
      }
    }
    if (constActs.length > 0) {
      new Trigger({ conds: [selector.eq(i)], acts: constActs });
    }
  }
  for (let k = 0; k < count; k++) {
    const src = values[k];
    if (typeof src !== "number") emitSlotFieldStore(slots, selector, k, src);
  }
}

/**
 * Advance a ring index by `+1 mod capacity`.
 *
 * The increment runs unconditionally and the wrap subtracts `capacity`
 * *after* it, so the wrap condition is evaluated on the post-increment
 * value. Two independent conditional triggers would both fire when
 * `v == capacity - 1` (the first resets to 0, then the second sees 0 and
 * increments again), which silently skips ring slots.
 */
function advanceUp(v: VarImpl, capacity: number): void {
  new Trigger({ conds: [], acts: [v.act_add(1)] });
  new Trigger({ conds: [v.ge(capacity)], acts: [v.act_sub(capacity)] });
}

/**
 * Advance a ring index by `-1 mod capacity`.
 *
 * Subtracting `capacity - 1` and wrapping on the post-add value avoids
 * relying on unsigned underflow: StarCraft's masked `Subtract` saturates
 * at zero, so `0 - 1` would stay `0` instead of wrapping.
 */
function advanceDown(v: VarImpl, capacity: number): void {
  new Trigger({ conds: [], acts: [v.act_add(capacity - 1)] });
  new Trigger({ conds: [v.ge(capacity)], acts: [v.act_sub(capacity)] });
}

/** FIFO queue backed by statically-bound ring slot triggers. */
export class Queue {
  readonly capacity: number;
  // Eager state. Deferred handles leave these null and resolve on reads.
  private readonly _deferred: LazyBinding<Queue> | null;
  private readonly _elem: ContainerElem;
  private _length: VarImpl | null = null;
  /** @internal Oldest live element index. */
  private _head: VarImpl | null = null;
  /** @internal Next write index. */
  private _tail: VarImpl | null = null;
  /** @internal Shared read box bound into every slot trigger. */
  private _outBox: VarImpl | AnyTriggerStructInstance | null = null;
  /** @internal */
  private _slots: readonly Trigger[] | null = null;

  constructor(elem: ContainerElem, capacity: number, deferred = false) {
    checkCapacity(capacity, "Queue()");
    this.capacity = capacity;
    this._deferred = deferred
      ? new LazyBinding<Queue>("Queue", "buildModule")
      : null;
    this._elem = elem;
    if (deferred) {
      // Eager element-type validation without any allocation; the eager
      // constructor validates the same input after its vars exist.
      elementLayout(elem);
      return;
    }
    const length = Var(0);
    const head = Var(0);
    const tail = Var(0);
    this._length = length;
    this._head = head;
    this._tail = tail;
    const layout = elementLayout(elem);
    const outBox = layout.make();
    this._outBox = outBox;
    this._slots = makeSlotTriggers(elem, capacity, head, outBox);
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving container reads: this for eager handles,
   * the active builder's binding for deferred ones.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * atomic eager `Queue` construction varies here (R3).
   */
  private resolve(): Queue {
    if (!this._deferred) {
      return this;
    }
    return this._deferred.resolve(() => new Queue(this._elem, this.capacity));
  }

  get length(): VarImpl {
    return this.resolve()._length!;
  }

  /** @internal Oldest live element index. */
  get head(): VarImpl {
    return this.resolve()._head!;
  }

  /** @internal Next write index. */
  get tail(): VarImpl {
    return this.resolve()._tail!;
  }

  /** @internal Shared read box bound into every slot trigger. */
  get outBox(): VarImpl | AnyTriggerStructInstance {
    return this.resolve()._outBox!;
  }

  /** @internal */
  get slots(): readonly Trigger[] {
    return this.resolve()._slots!;
  }

  empty(): ReturnType<VarImpl["eq"]> {
    return this.length.eq(0);
  }

  full(): ReturnType<VarImpl["ge"]> {
    return this.length.ge(this.capacity);
  }

  /** Enqueue at the tail; a full queue drops the oldest element. */
  append(value: number | VarImpl | AnyTriggerStructInstance): this {
    const values = classifyElement(this.slots, value);
    when([this.length.ge(this.capacity)], () => {
      advanceUp(this.head, this.capacity);
    });
    emitSlotWrites(this.slots, this.tail, values);
    advanceUp(this.tail, this.capacity);
    when([this.length.le(this.capacity - 1)], () => {
      this.length.iadd(1);
    });
    return this;
  }

  /** Alias for {@link append}. */
  push(value: number | VarImpl | AnyTriggerStructInstance): this {
    return this.append(value);
  }

  /** Dequeue the oldest element. Unchecked on empty. */
  popleft(): VarImpl | AnyTriggerStructInstance {
    const ret = this.outBox;
    advanceUp(this.head, this.capacity);
    this.length.isub(1);
    return ret;
  }

  /** Alias for {@link popleft}. */
  pop(): VarImpl | AnyTriggerStructInstance {
    return this.popleft();
  }

  /** Reset pointers and length (slot payloads are not cleared). */
  clear(): this {
    this.head.set(0);
    this.tail.set(0);
    this.length.set(0);
    return this;
  }
}

/** Double-ended queue backed by two statically-bound ring read views. */
export class Deque {
  readonly capacity: number;
  // Eager state. Deferred handles leave these null and resolve on reads.
  private readonly _deferred: LazyBinding<Deque> | null;
  private readonly _elem: ContainerElem;
  private _length: VarImpl | null = null;
  /** @internal Oldest live element index. */
  private _left: VarImpl | null = null;
  /** @internal Next right-write index. */
  private _right: VarImpl | null = null;
  private _outLeft: VarImpl | AnyTriggerStructInstance | null = null;
  private _outRight: VarImpl | AnyTriggerStructInstance | null = null;
  /** @internal */
  private _leftSlots: readonly Trigger[] | null = null;
  /** @internal */
  private _rightSlots: readonly Trigger[] | null = null;

  constructor(elem: ContainerElem, capacity: number, deferred = false) {
    checkCapacity(capacity, "Deque()");
    this.capacity = capacity;
    this._deferred = deferred
      ? new LazyBinding<Deque>("Deque", "buildModule")
      : null;
    this._elem = elem;
    if (deferred) {
      // Eager element-type validation without any allocation.
      elementLayout(elem);
      return;
    }
    const length = Var(0);
    const left = Var(0);
    const right = Var(0);
    this._length = length;
    this._left = left;
    this._right = right;
    const layout = elementLayout(elem);
    const outLeft = layout.make();
    const outRight = layout.make();
    this._outLeft = outLeft;
    this._outRight = outRight;
    this._leftSlots = makeSlotTriggers(elem, capacity, left, outLeft);
    // Right reads select slot i when right == i + 1 (i.e. the element
    // immediately before the next write position).
    this._rightSlots = Array.from({ length: capacity }, (_, i) => {
      const fields = boxFields(outRight);
      const acts = fields.map((f) => SetDeathsX(f, SetTo, 0, 0, f.bitmask));
      return new Trigger({ conds: [right.eq((i + 1) % capacity)], acts });
    });
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving container reads.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * atomic eager `Deque` construction varies here (R3).
   */
  private resolve(): Deque {
    if (!this._deferred) {
      return this;
    }
    return this._deferred.resolve(() => new Deque(this._elem, this.capacity));
  }

  get length(): VarImpl {
    return this.resolve()._length!;
  }

  /** @internal Oldest live element index. */
  get left(): VarImpl {
    return this.resolve()._left!;
  }

  /** @internal Next right-write index. */
  get right(): VarImpl {
    return this.resolve()._right!;
  }

  get outLeft(): VarImpl | AnyTriggerStructInstance {
    return this.resolve()._outLeft!;
  }

  get outRight(): VarImpl | AnyTriggerStructInstance {
    return this.resolve()._outRight!;
  }

  /** @internal */
  get leftSlots(): readonly Trigger[] {
    return this.resolve()._leftSlots!;
  }

  /** @internal */
  get rightSlots(): readonly Trigger[] {
    return this.resolve()._rightSlots!;
  }

  empty(): ReturnType<VarImpl["eq"]> {
    return this.length.eq(0);
  }

  full(): ReturnType<VarImpl["ge"]> {
    return this.length.ge(this.capacity);
  }

  /** Enqueue at the right/newest end; a full deque drops the oldest. */
  append(value: number | VarImpl | AnyTriggerStructInstance): this {
    const values = classifyElement(this.leftSlots, value);
    when([this.length.ge(this.capacity)], () => {
      advanceUp(this.left, this.capacity);
    });
    emitSlotWrites(this.leftSlots, this.right, values);
    emitSlotWrites(this.rightSlots, this.right, values);
    advanceUp(this.right, this.capacity);
    when([this.length.le(this.capacity - 1)], () => {
      this.length.iadd(1);
    });
    return this;
  }

  /** Enqueue at the left/oldest end; a full deque drops the newest. */
  appendleft(value: number | VarImpl | AnyTriggerStructInstance): this {
    const values = classifyElement(this.leftSlots, value);
    advanceDown(this.left, this.capacity);
    this.emitLeftWrite(values);
    when([this.length.ge(this.capacity)], () => {
      advanceDown(this.right, this.capacity);
    });
    when([this.length.le(this.capacity - 1)], () => {
      this.length.iadd(1);
    });
    return this;
  }

  private emitLeftWrite(values: readonly (number | VarImpl)[]): void {
    emitSlotWrites(this.leftSlots, this.left, values);
    emitSlotWrites(this.rightSlots, this.left, values);
  }

  /** Remove and return the newest element. Unchecked on empty. */
  pop(): VarImpl | AnyTriggerStructInstance {
    const ret = this.outRight;
    advanceDown(this.right, this.capacity);
    this.length.isub(1);
    return ret;
  }

  /** Remove and return the oldest element. Unchecked on empty. */
  popleft(): VarImpl | AnyTriggerStructInstance {
    const ret = this.outLeft;
    advanceUp(this.left, this.capacity);
    this.length.isub(1);
    return ret;
  }
}

/** LIFO stack backed by `FixedArray` + a game-time `pos` index. */
export class Stack {
  readonly capacity: number;
  // Eager state. Deferred handles leave these null and resolve on reads.
  private readonly _deferred: LazyBinding<Stack> | null;
  private _storage: FixedArrayImpl<VarImpl> | null = null;
  private _pos: VarImpl | null = null;

  constructor(capacity: number, deferred = false) {
    checkCapacity(capacity, "Stack()");
    this.capacity = capacity;
    this._deferred = deferred
      ? new LazyBinding<Stack>("Stack", "buildModule")
      : null;
    if (deferred) {
      return;
    }
    this._storage = FixedArray<VarImpl>(arrayDword, capacity) as FixedArrayImpl<VarImpl>;
    this._pos = Var(0);
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving container reads.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * atomic eager `Stack` construction varies here (R3).
   */
  private resolve(): Stack {
    if (!this._deferred) {
      return this;
    }
    return this._deferred.resolve(() => new Stack(this.capacity));
  }

  get storage(): FixedArrayImpl<VarImpl> {
    return this.resolve()._storage!;
  }

  get pos(): VarImpl {
    return this.resolve()._pos!;
  }

  get length(): VarImpl {
    return this.pos;
  }

  empty(): ReturnType<VarImpl["eq"]> {
    return this.pos.eq(0);
  }

  full(): ReturnType<VarImpl["ge"]> {
    return this.pos.ge(this.capacity);
  }

  push(value: number | VarImpl): this {
    this.storage.set(this.pos, value);
    this.pos.iadd(1);
    return this;
  }

  pop(): VarImpl {
    this.pos.isub(1);
    const out = this.storage.get(this.pos) as VarImpl;
    const ret = Var(0);
    ret.set(out);
    return ret;
  }

  clear(): this {
    this.pos.set(0);
    return this;
  }
}

/** Strategy for the automatic dead-unit sweep. */
export interface UnitGroupOptions {
  /** Use the unlimiter order field (`+0x4C`, mask `0xFF00`) instead of sprite. */
  readonly unlimiter?: boolean;
  /** Emit a per-frame sweep that removes members whose check field reads 0. */
  readonly autoPrune?: boolean;
  /**
   * Optional build-time dead predicate for custom layouts:
   * `(checkEpd) => Cond`. Compact mode calls it once per group, inside the
   * per-live-slot gate; the unroll calls it once per slot.
   */
  readonly deadCheck?: (checkEpd: VarImpl) => unknown;
  /**
   * Emit `for_each` once and drive it with a runtime CurrentPlayer cursor.
   * This is a static-size optimization; the default unroll stays the
   * executed-cost choice. The callback receives a reused read-only snapshot
   * Var, not the storage slot.
   */
  readonly compact?: boolean;
}

/**
 * Fixed-capacity set of unit EPDs backed by page slots.
 *
 * `add` fills the first empty slot, `remove` clears matching slots, and
 * `for_each` visits non-empty slots. With `autoPrune` (default `true`) a
 * per-frame sweep clears dead members using the vanilla sprite field or
 * the unlimiter order field.
 *
 * With `compact: true` the same storage is driven by a runtime
 * CurrentPlayer cursor loop: `for_each` emits its callback body once, and
 * `autoPrune`/`add`/`remove`/`clear` each emit a single runtime sweep
 * instead of unrolling one trigger per slot. Compact mode is a
 * *static-size* optimization:
 * executed cost is higher because every live EPD is materialised with a
 * 32-bit reader and every site saves/restores the actual CurrentPlayer
 * register. The default (`compact` unset/false) unroll is unchanged and
 * remains the executed-cost choice.
 *
 * Callback contract: the unroll passes the actual slot `Var`, so a body that
 * writes it mutates storage. Compact `for_each` instead passes one reused,
 * read-only snapshot `Var`; writes affect only that scratch copy, and the
 * callback must not retain or alias it across iterations.
 *
 * `deadCheck` runs once at build time per group in compact mode (the unroll
 * runs it once per slot) and receives `slotValue + checkOffset`. The returned
 * *dead* condition is reused for every live slot; HIR the predicate emits in
 * compact mode is placed inside the per-slot live gate.
 */
export class UnitGroup {
  readonly capacity: number;
  readonly autoPrune: boolean;
  readonly unlimiter: boolean;
  readonly compact: boolean;
  // Eager state. Deferred handles leave these null and resolve on reads.
  private readonly _deferred: LazyBinding<UnitGroup> | null;
  private _length: VarImpl | null = null;
  /** @internal Backing page slots (0 = empty). */
  private _slots: readonly VarImpl[] | null = null;
  private readonly _deadCheck?: (checkEpd: VarImpl) => unknown;

  constructor(capacity: number, opts: UnitGroupOptions = {}, deferred = false) {
    checkCapacity(capacity, "UnitGroup()");
    this.capacity = capacity;
    this.autoPrune = opts.autoPrune ?? true;
    this.unlimiter = opts.unlimiter ?? false;
    this.compact = opts.compact ?? false;
    this._deadCheck = opts.deadCheck;
    this._deferred = deferred
      ? new LazyBinding<UnitGroup>("UnitGroup", "buildModule")
      : null;
    if (deferred) {
      return;
    }
    this._length = Var(0);
    const storage = FixedArray<VarImpl>(arrayDword, capacity) as FixedArrayImpl<VarImpl>;
    this._slots = storage.vars;
    if (this.autoPrune) this.emitAutoPrune();
  }

  // -------------------------------------------------------------------------
  // Deferred resolution
  // -------------------------------------------------------------------------

  /**
   * The eager instance serving container reads.
   *
   * The shared `LazyBinding` state machine owns the builder lookup, cache,
   * first-binding capture, and post-build zero/one/many policy; only the
   * atomic eager `UnitGroup` construction varies here (R3, including the
   * auto-prune triggers).
   */
  private resolve(): UnitGroup {
    if (!this._deferred) {
      return this;
    }
    return this._deferred.resolve(
      () =>
        new UnitGroup(this.capacity, {
          unlimiter: this.unlimiter,
          autoPrune: this.autoPrune,
          deadCheck: this._deadCheck,
          compact: this.compact,
        }),
    );
  }

  get length(): VarImpl {
    return this.resolve()._length!;
  }

  /** @internal Backing page slots (0 = empty). */
  get slots(): readonly VarImpl[] {
    return this.resolve()._slots!;
  }

  private emitAutoPrune(): void {
    if (this.compact) {
      this.emitAutoPruneCompact();
      return;
    }
    const checkOffset: number = this.unlimiter ? 0x13 : 3;
    const mask = this.unlimiter ? 0xff00 : 0xffffffff;
    for (const slot of this.slots) {
      when([slot.ne(0)], () => {
        const check = Var(0);
        check.set(slot);
        if (checkOffset !== 0) check.iadd(checkOffset);

        // The engine reads `cond.player` as an EPD, so a dead-membership
        // test needs the *value* of `check` written into the condition's
        // player field at runtime. `VarStore` performs that copy through
        // `check`'s TriggerPage slot. The probe trigger is registered after
        // the store so it executes with the freshly patched field.
        const cond = this._deadCheck
          ? (this._deadCheck(check) as HirCond)
          : DeathsX(0, Exactly, 0, 0, mask);
        const probe = new Trigger({
          conds: [cond],
          acts: [slot.act_set(0), this.length.act_sub(1)],
          register: false,
        });
        if (!this._deadCheck) {
          emitVarStore(
            check.id,
            TriggerFieldReloc(probe.id, probe.cond(0).player.byteOffset),
          );
        }
        _registerTrigger(probe);
      });
    }
  }

  /** One shared sweep: read the live EPD, test its death field, clear. */
  private emitAutoPruneCompact(): void {
    const checkOffset: number = this.unlimiter ? 0x13 : 3;
    const mask = this.unlimiter ? 0xff00 : 0xffffffff;
    const value = Var(0);
    const check = Var(0);
    const custom = this._deadCheck;

    const iteration = (): void => {
      when([cpLive()], () => {
        emitCpReadInto(value);
        check.set(value);
        if (checkOffset !== 0) check.iadd(checkOffset);
        const acts = [
          SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
          this.length.act_sub(1),
        ];
        if (!custom) {
          const probe = new Trigger({
            conds: [DeathsX(0, Exactly, 0, 0, mask)],
            acts,
            register: false,
          });
          emitVarStore(
            check.id,
            TriggerFieldReloc(probe.id, probe.cond(0).player.byteOffset),
          );
          _registerTrigger(probe);
        } else {
          // The predicate runs once at build time for the whole group,
          // inside the per-slot live gate, so HIR it emits runs only for
          // live members. The returned condition is reused for every live
          // slot.
          new Trigger({ conds: [custom(check) as HirCond], acts });
        }
      });
    };

    emitCpSweep(this.slots[0], this.capacity, iteration);
  }

  empty(): ReturnType<VarImpl["eq"]> {
    return this.length.eq(0);
  }

  full(): ReturnType<VarImpl["ge"]> {
    return this.length.ge(this.capacity);
  }

  /** Add a unit EPD to the first free slot. */
  add(epd: number | VarImpl): this {
    if (this._deferred) {
      this.resolve().add(epd);
      return this;
    }
    if (typeof epd === "number") {
      if (!Number.isInteger(epd) || epd < 0) {
        throw new EudslError("EUDSL-BOUNDS", `UnitGroup.add() EPD must be non-negative, got ${epd}`);
      }
    }
    if (this.compact) {
      this.addCompact(epd);
      return this;
    }
    const done = Var(0);
    for (const slot of this.slots) {
      when([slot.eq(0), done.eq(0)], () => {
        slot.set(epd);
        done.set(1);
        this.length.iadd(1);
      });
    }
    return this;
  }

  private addCompact(epd: number | VarImpl): void {
    const done = Var(0);
    if (typeof epd === "number") {
      emitCpSweep(this.slots[0], this.capacity, () => {
        when([cpEmpty(), done.eq(0)], () => {
          SetDeaths(CURRENT_PLAYER, SetTo, epd, 0);
          done.set(1);
          this.length.iadd(1);
        });
      });
    } else {
      const writer = new Trigger({
        conds: [cpEmpty(), done.eq(0)],
        acts: [
          SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
          done.act_set(1),
          this.length.act_add(1),
        ],
        register: false,
      });
      emitVarStore(
        epd.id,
        TriggerFieldReloc(writer.id, writer.act(0).value.byteOffset),
      );

      emitCpSweep(this.slots[0], this.capacity, () => {
        _registerTrigger(writer);
      });
    }
  }

  /** Remove every slot holding `epd`. */
  remove(epd: number | VarImpl): this {
    if (this._deferred) {
      this.resolve().remove(epd);
      return this;
    }
    if (this.compact) {
      this.removeCompact(epd);
      return this;
    }
    for (const slot of this.slots) {
      when([slot.eq(epd)], () => {
        slot.set(0);
        this.length.isub(1);
      });
    }
    return this;
  }

  private removeCompact(epd: number | VarImpl): void {
    if (typeof epd === "number") {
      emitCpSweep(this.slots[0], this.capacity, () => {
        when([Deaths(CURRENT_PLAYER, Exactly, epd, 0)], () => {
          SetDeaths(CURRENT_PLAYER, SetTo, 0, 0);
          this.length.isub(1);
        });
      });
    } else {
      const probe = new Trigger({
        conds: [Deaths(CURRENT_PLAYER, Exactly, 0, 0)],
        acts: [
          SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
          this.length.act_sub(1),
        ],
        register: false,
      });
      emitVarStore(
        epd.id,
        TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
      );

      emitCpSweep(this.slots[0], this.capacity, () => {
        _registerTrigger(probe);
      });
    }
  }

  /** Visit every non-empty member EPD in slot order. */
  for_each(body: (epd: VarImpl) => void): this {
    if (this._deferred) {
      this.resolve().for_each(body);
      return this;
    }
    if (this.compact) {
      this.forEachCompact(body);
      return this;
    }
    for (const slot of this.slots) {
      when([slot.ne(0)], () => body(slot));
    }
    return this;
  }

  private forEachCompact(body: (epd: VarImpl) => void): void {
    const epd = Var(0);

    emitCpSweep(this.slots[0], this.capacity, () => {
      when([cpLive()], () => {
        emitCpReadInto(epd);
        body(epd);
      });
    });
  }

  forEach(body: (epd: VarImpl) => void): this {
    return this.for_each(body);
  }

  /** Clear membership (slot payloads are zeroed for deterministic reuse). */
  clear(): this {
    if (this._deferred) {
      this.resolve().clear();
      return this;
    }
    if (this.compact) {
      emitCpSweep(this.slots[0], this.capacity, () => {
        SetDeaths(CURRENT_PLAYER, SetTo, 0, 0);
      });
      this.length.set(0);
      return this;
    }
    for (const slot of this.slots) slot.set(0);
    this.length.set(0);
    return this;
  }
}

/**
 * Declare a lazy `Queue` (globals P3): a handle constructible anywhere
 * (no builder needed) that resolves to a fully eager binding on first
 * resolving use inside `buildModule`, one binding per builder.
 *
 * Call shape mirrors `new Queue(elem, capacity)`. Capacity and element
 * type are validated eagerly at the declaration; no var, out box, or
 * slot trigger is allocated until first resolving use. Binding creation
 * is atomic: one full eager `Queue` construction in eager order. A
 * never-used handle emits nothing (D5). This factory is the only
 * documented path to deferred mode; the internal constructor flag stays
 * undocumented.
 */
export function lazyQueue(elem: ContainerElem, capacity: number): Queue {
  warnLazyInBuilder();
  return new Queue(elem, capacity, true);
}

/**
 * Declare a lazy `Deque` (globals P3). See {@link lazyQueue}: same
 * declaration/validation and per-builder binding rules, with call shape
 * `new Deque(elem, capacity)`.
 */
export function lazyDeque(elem: ContainerElem, capacity: number): Deque {
  warnLazyInBuilder();
  return new Deque(elem, capacity, true);
}

/**
 * Declare a lazy `Stack` (globals P3). See {@link lazyQueue}: same
 * declaration/validation and per-builder binding rules, with call shape
 * `new Stack(capacity)`.
 */
export function lazyStack(capacity: number): Stack {
  warnLazyInBuilder();
  return new Stack(capacity, true);
}

/**
 * Declare a lazy `UnitGroup` (globals P3). See {@link lazyQueue}: same
 * declaration/validation and per-builder binding rules, with call shape
 * `new UnitGroup(capacity, opts)`. `autoPrune`, `unlimiter`, and `compact`
 * are pure reads that never resolve; `slots`/`length` and every method do.
 */
export function lazyUnitGroup(
  capacity: number,
  opts: UnitGroupOptions = {},
): UnitGroup {
  warnLazyInBuilder();
  return new UnitGroup(capacity, opts, true);
}

/** Legacy aliases kept for vocabulary continuity. */
export const EUDQueue = Queue;
export const EUDDeque = Deque;
export const EUDStack = Stack;
export const EUDUnitGroup = UnitGroup;

/**
 * Fixed-capacity free-list pool over a `FixedArray`.
 *
 * The free list is a LIFO `Stack` of indices, so a fresh pool hands out
 * `0, 1, 2, ...` deterministically. `POOL_NULL` (`0xffffffff`) is returned
 * when the pool is exhausted and is distinguishable from every legal index
 * because capacity is capped at `0xfffffffe`.
 *
 * `release` performs no bounds check (documented): releasing an index twice
 * or outside `0..capacity-1` corrupts the free list.
 */
export class ObjectPool {
  readonly capacity: number;
  readonly storage: FixedArrayImpl<any>;
  readonly freeList: Stack;
  readonly free_list: Stack;

  constructor(
    capacity: number,
    schemaOrWidth: FixedArrayElemType = arrayDword,
  ) {
    const cap = requirePlainInt(capacity, "ObjectPool capacity");
    if (cap <= 0) {
      throw new EudslError(
        "EUDSL-BOUNDS",
        `ObjectPool capacity must be positive, got ${cap}`,
      );
    }
    if (cap > 0xfffffffe) {
      throw new EudslError(
        "EUDSL-RANGE",
        `ObjectPool capacity must be at most 0xfffffffe, got ${cap}`,
      );
    }
    this.capacity = cap;
    this.storage = FixedArray<any>(schemaOrWidth, cap) as FixedArrayImpl<any>;
    this.freeList = new Stack(cap);
    this.free_list = this.freeList;
    this.fillFree();
  }

  /**
   * Seed the stack body statically so a pop yields `0, 1, 2, ...`.
   *
   * `Stack.push` would emit a dynamic-write trigger per slot; writing the
   * constant body and setting `pos` is the same state with one packed
   * initialization.
   */
  private fillFree(): void {
    for (let index = 0; index < this.capacity; index++) {
      this.freeList.storage.set(index, this.capacity - 1 - index);
    }
    this.freeList.pos.set(this.capacity);
  }

  /** Pop one free index, or return `POOL_NULL` when full. */
  allocate(): VarImpl {
    const out = Var(0);
    // Reset per call, not just at module init: a reused allocation site
    // inside a loop must return POOL_NULL after the pool becomes full.
    out.set(POOL_NULL);
    when(this.freeList.pos.ge(1), () => out.set(this.freeList.pop()));
    return out;
  }

  allocateObject(): VarImpl {
    return this.allocate();
  }

  /** Push `index` back onto the free list. */
  release(index: number | VarImpl): void {
    if (typeof index !== "number" && !(index instanceof VarImpl)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `ObjectPool.release index must be an integer or a Var; got ${describeValue(index)}`,
      );
    }
    if (typeof index === "number") {
      requirePlainInt(index, "ObjectPool.release index");
    }
    this.freeList.push(index);
  }

  releaseObject(index: number | VarImpl): void {
    this.release(index);
  }

  /** A condition that holds when there are no free slots. */
  full(): ReturnType<VarImpl["eq"]> {
    return this.freeList.pos.eq(0);
  }

  /** A condition that holds when every slot is free. */
  empty(): ReturnType<VarImpl["eq"]> {
    return this.freeList.pos.eq(this.capacity);
  }

  /** Reset the free list to its fresh LIFO order; storage is untouched. */
  clear(): void {
    this.freeList.clear();
    this.fillFree();
  }

  /** Read a user-data slot through the backing array. */
  get(index: number | VarImpl): unknown {
    return this.storage.get(index as VarImpl);
  }

  /** Write a user-data slot through the backing array. */
  set(index: number | VarImpl, value: unknown): void {
    this.storage.set(index as VarImpl, value as any);
  }
}
