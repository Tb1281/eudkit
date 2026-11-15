/**
 * Scoped CurrentPlayer (`0x6509B0`) pointer views and absolute offset hops.
 * `withCP(epd, (cp) => ...)`
 *
 * A constant EPD base is the only supported form: the CP register holds an
 * **EPD**, not a byte address, so a dynamic base would silently write a byte
 * address into it. Hops are absolute anchors (`SetTo baseEpd + target`), so a
 * hop inside a replayed runtime loop body re-anchors the cursor on every pass
 * instead of drifting.
 */

import {
  Add,
  AddrReloc,
  AtLeast,
  CurrentPlayer,
  Db,
  DbRef,
  Deaths,
  Exactly,
  SetDeaths,
  SetTo,
  emitVarStore,
  epdOfDb,
  type HirAct,
} from "./hir.ts";
import {
  ACT_COUNT,
  ACT_STRIDE_EPDS,
  TRIGGER_STRIDE_EPDS,
  Trigger,
  _getActiveModuleBuilder,
} from "./trigger.ts";
import { when, whileLoop } from "./temporal.ts";
import { ACT_FIELD_OFFSETS } from "./stock-spec.ts";
import {
  Expr,
  ReadonlyVar,
  Var,
  VarImpl,
  toVar,
  type VarOperand,
} from "./var.ts";
import {
  EPD_CURRENT_PLAYER,
  EudslError,
  MemFieldHandle,
  MemViewInstance,
  _registerCpFactories,
  defineMemView,
  emitBitTestRead,
  type FieldSpec,
  type MemViewSchema,
} from "./view.ts";

/** Byte address of the runtime `CurrentPlayer` register. */
export const CP_ADDR = 0x6509b0;

/** `CP_ADDR` viewed as a dword field. */
export const CP_REGISTER_VIEW = defineMemView("CurrentPlayerRegister", 4, {
  value: { offset: 0, size: 4 },
});
export const CP_REGISTER_FIELD: FieldSpec = { offset: 0, size: 4 };

/** `CurrentPlayer` page-slot stride in EPDs (one 32-byte action slot). */
export const SLOT_STRIDE_EPDS = ACT_STRIDE_EPDS;
/** Extra EPDs needed to hop from the last slot of a page to the next page. */
export const PAGE_HOP_EPDS = TRIGGER_STRIDE_EPDS - ACT_COUNT * ACT_STRIDE_EPDS;
/** Slots per `TriggerPage`. */
export const PAGE_SLOTS = ACT_COUNT;

export function readCurrentPlayerRegister(): VarImpl {
  return new MemFieldHandle(
    CP_REGISTER_VIEW,
    CP_REGISTER_FIELD,
    EPD_CURRENT_PLAYER,
    "value",
  ).read();
}

/**
 * Point CurrentPlayer at the runtime value field of `slot0`.
 *
 * The mode-1 reloc materialises `EPD(slot0.value)` into the action's value
 * field. The action then writes that EPD into the CurrentPlayer register
 * itself, so `Deaths(CurrentPlayer, ...)` reads the slot value.
 */
export function emitSeedCpFromSlot(slot0: VarImpl): void {
  const base = SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
  const act: HirAct = {
    ...base,
    relocs: [
      {
        field_offset: ACT_FIELD_OFFSETS.value,
        target_var: slot0.id,
        target_offset: 20,
        mode: 1,
      },
    ],
  };
  // `SetDeaths` queued `base` as a standalone top-level act. The relocated
  // copy is a different object, so consume the original explicitly before
  // `Trigger` owns the copy.
  _getActiveModuleBuilder()?.consumeQueuedActs([base]);
  new Trigger({ conds: [], acts: [act], preserved: true });
}

/** CurrentPlayer slot holds a non-zero member EPD. */
export function cpLive(): ReturnType<typeof Deaths> {
  return Deaths(CurrentPlayer, AtLeast, 1, 0);
}

/** CurrentPlayer slot is the zero sentinel. */
export function cpEmpty(): ReturnType<typeof Deaths> {
  return Deaths(CurrentPlayer, Exactly, 0, 0);
}

/** Read the dword at CurrentPlayer into `dst` with 32 bit tests. */
export function emitCpReadInto(dst: VarImpl): void {
  dst.set(0);
  emitBitTestRead(CurrentPlayer, 32, 0, dst);
}

/**
 * Drive `iteration` once per storage slot with a CP cursor.
 *
 * The actual CurrentPlayer register is saved once per site execution and
 * restored after the loop, so nesting is path-complete even when the
 * iteration body clobbers CurrentPlayer via `withCP`. The slot pointer is
 * carried in `offset` (EPDs relative to `slot0`'s value field) and re-seeded
 * into CurrentPlayer before every slot; a body clobber therefore cannot
 * corrupt the cursor.
 */
export function emitCpSweep(
  slot0: VarImpl,
  capacity: number,
  iteration: () => void,
): void {
  const saved = readCurrentPlayerRegister();
  const remaining = Var(0);
  const pageRemaining = Var(0);
  const offset = Var(0);

  remaining.set(capacity);
  pageRemaining.set(Math.min(PAGE_SLOTS, capacity));
  offset.set(0);

  whileLoop([remaining.ge(1)], () => {
    emitSeedCpFromSlot(slot0);
    emitVarStore(offset.id, AddrReloc(CP_ADDR), Add);
    iteration();
    offset.iadd(SLOT_STRIDE_EPDS);
    remaining.isub(1);
    pageRemaining.isub(1);
    when([pageRemaining.eq(0)], () => {
      offset.iadd(PAGE_HOP_EPDS);
      pageRemaining.set(PAGE_SLOTS);
    });
  });
  emitVarStore(saved.id, AddrReloc(CP_ADDR));
}

/**
 * Store `src`'s value into the runtime CurrentPlayer register.
 *
 * The one CP-seed shape shared by `withEpd` and `blockio`'s private seeders.
 * `memcpyEPD` still leaves CP clobbered at its end value; only `withEpd`
 * pairs this store with an unconditional ambient restore.
 */
export function _setCpFromVar(src: VarImpl): void {
  emitVarStore(src.id, AddrReloc(CP_ADDR), SetTo);
}

function requireIntBase(baseEpd: unknown): asserts baseEpd is number {
  if (typeof baseEpd !== "number" || !Number.isInteger(baseEpd)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `withCP base must be an int EPD; got ${String(baseEpd)}`,
    );
  }
}

export class CPCursor {
  readonly baseEpd: number;
  currentOffset: number;

  constructor(baseEpd: number | any) {
    requireIntBase(baseEpd);
    this.baseEpd = baseEpd;
    this.currentOffset = 0;
  }

  /**
   * Anchor CurrentPlayer at `baseEpd + targetDwordOffset`.
   *
   * The act is absolute (`SetTo`), not a delta from build-time state: inside a
   * replayed runtime loop body it re-anchors the cursor on every pass, so the
   * register cannot drift. Repeating the current offset emits nothing.
   */
  moveTo(targetDwordOffset: number): void {
    if (targetDwordOffset !== this.currentOffset) {
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, this.baseEpd + targetDwordOffset, 0);
    }
    this.currentOffset = targetDwordOffset;
  }

  at(dwordOffset: number): MemFieldHandle {
    const spec = { offset: dwordOffset * 4, size: 4 as const };
    const schema: MemViewSchema = {
      name: "CP",
      size: (dwordOffset + 1) * 4,
      fields: { val: spec },
    };
    return new MemFieldHandle(schema, spec, this, `at(${dwordOffset})`);
  }

  view<T = any>(schemaOrCtor: any): T {
    if (typeof schemaOrCtor === "function") {
      if (schemaOrCtor.schema) {
        return new MemViewInstance(schemaOrCtor.schema, this) as unknown as T;
      }
      return new schemaOrCtor(this) as T;
    }
    if (typeof schemaOrCtor === "object" && schemaOrCtor !== null && "fields" in schemaOrCtor) {
      return new MemViewInstance(schemaOrCtor, this) as unknown as T;
    }
    throw new Error("Invalid schema or constructor passed to cp.view()");
  }

  /** Re-anchor CurrentPlayer at `baseEpd` and reset the build-time offset. */
  restore(): void {
    if (this.currentOffset !== 0) {
      SetDeaths(EPD_CURRENT_PLAYER, SetTo, this.baseEpd, 0);
    }
    this.currentOffset = 0;
  }
}

/**
 * Bind CurrentPlayer (`0x6509B0`) to `epd` for the duration of `fn`.
 *
 * `epd` must be a constant int EPD. Hops are absolute (`SetTo base + target`),
 * so a hop inside a replayed loop body re-anchors instead of drifting;
 * `restore` re-anchors to the base. A dynamic base throws `EUDSL-TYPE` before
 * any act is queued, because the CP register holds an EPD, not a byte address.
 */
export function withCP<R = void>(
  epd: number | any,
  fn: (cp: CPCursor) => R,
): R {
  requireIntBase(epd);
  SetDeaths(EPD_CURRENT_PLAYER, SetTo, epd, 0);
  const cp = new CPCursor(epd);
  try {
    return fn(cp);
  } finally {
    cp.restore();
  }
}

export const with_cp = withCP;

/** Public documentation name for the accepted `withEpd` operand domain. */
export type EpdOperand = number | VarImpl | ReadonlyVar | Expr | Db | DbRef;

function requireEpdOperand(epd: unknown): asserts epd is EpdOperand {
  if (typeof epd === "boolean") {
    throw new EudslError(
      "EUDSL-TYPE",
      `withEpd epd must be an EPD-bearing value; got ${String(epd)}`,
    );
  }
  if (typeof epd === "number") {
    if (!Number.isInteger(epd)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `withEpd epd must be an EPD-bearing value; got ${String(epd)}`,
      );
    }
    return;
  }
  if (epd instanceof DbRef || epd instanceof VarImpl || epd instanceof Expr) {
    return;
  }
  throw new EudslError(
    "EUDSL-TYPE",
    `withEpd epd must be an EPD-bearing value (int, Var, ReadonlyVar, Expr, Db, or DbRef); got ${String(epd)}`,
  );
}

function requireEpdOffset(offset: unknown): asserts offset is number {
  if (
    typeof offset !== "number" ||
    !Number.isInteger(offset) ||
    offset < -(2 ** 31) ||
    offset > 2 ** 31 - 1
  ) {
    throw new EudslError(
      "EUDSL-RANGE",
      `withEpd offset must be a signed 32-bit integer; got ${String(offset)}`,
    );
  }
}

function materializeEpdBase(epd: EpdOperand): VarImpl {
  if (epd instanceof DbRef) return epdOfDb(epd);
  if (typeof epd === "number") return new Var(epd >>> 0);
  return toVar(epd as VarOperand);
}

function bindEpd(base: VarImpl, offset: number): { cursor: EpdCursor; saved: VarImpl } {
  const cursorBase = new Var(0);
  cursorBase.set(base);
  if (offset !== 0) cursorBase.iadd(offset);
  const saved = readCurrentPlayerRegister();
  _setCpFromVar(cursorBase);
  return { cursor: new EpdCursor(cursorBase, 0), saved };
}

/**
 * CP cursor over a **runtime** EPD base, with relative dword hops.
 *
 * Unlike {@link CPCursor}, the base lives in a `Var`, so hops must be relative
 * (`Add`) rather than absolute (`SetTo base + target`). Each call owns its own
 * base copy; the public constructor is {@link withEpd}, which also saves and
 * restores the ambient CP.
 */
export class EpdCursor {
  readonly base: VarImpl;
  currentOffset: number;

  constructor(base: VarImpl, offset = 0) {
    if (!(base instanceof VarImpl)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `EpdCursor base must be a Var; got ${String(base)}`,
      );
    }
    requireEpdOffset(offset);
    this.base = base;
    this.currentOffset = offset;
  }

  get current_offset(): number {
    return this.currentOffset;
  }

  /**
   * Hop CP by the relative delta to `targetDwordOffset`.
   *
   * Repeating the current offset emits nothing. `restore` only resets this
   * build-time offset; the ambient CP restore belongs to {@link withEpd}.
   */
  moveTo(targetDwordOffset: number): void {
    if (targetDwordOffset !== this.currentOffset) {
      SetDeaths(
        EPD_CURRENT_PLAYER,
        Add,
        targetDwordOffset - this.currentOffset,
        0,
      );
      this.currentOffset = targetDwordOffset;
    }
  }

  move_to(targetDwordOffset: number): void {
    this.moveTo(targetDwordOffset);
  }

  /**
   * Hop to `dwordOffset` and return the live-CP dword handle.
   *
   * The returned handle is anchored at CP's current position
   * (`new CPCursor(0).at(0)`), so its `_prepareTarget` hop is a no-op and the
   * relative hop is never emitted twice.
   */
  at(dwordOffset: number): MemFieldHandle {
    this.moveTo(dwordOffset);
    return new CPCursor(0).at(0);
  }

  /** Reset the build-time offset; emit nothing (see {@link withEpd}). */
  restore(): void {
    this.currentOffset = 0;
  }
}

/**
 * Bind CurrentPlayer to a runtime EPD for the duration of `fn`.
 *
 * `epd` may be a plain int, a `Var`/`ReadonlyVar`/`Expr`, or a `Db`/`DbRef`
 * (routed through {@link epdOfDb}). The runtime base is copied into a private
 * scratch `Var`, so a caller's `Var` is never mutated; `offset` pre-hops that
 * copy by a constant dword count.
 *
 * The ambient CurrentPlayer value is saved with the shared 32-bit reader and
 * restored **unconditionally** (including the exception path and when the body
 * clobbers CP). Nesting is LIFO: each call owns its own save and base copy, so
 * an inner call restores the outer binding and the outer call restores the
 * ambient value. This is strictly stronger than {@link CPCursor.restore},
 * which no-ops at offset 0.
 */
export function withEpd<R = void>(
  epd: EpdOperand,
  fn: (cp: EpdCursor) => R,
  offset = 0,
): R {
  requireEpdOperand(epd);
  requireEpdOffset(offset);
  const base = materializeEpdBase(epd);
  const { cursor, saved } = bindEpd(base, offset);
  try {
    return fn(cursor);
  } finally {
    _setCpFromVar(saved);
  }
}

export const with_epd = withEpd;

/**
 * Bind CP to `epd` for `fn()` without reading the ambient CP.
 *
 * The loop contract behind {@link readCunitPointerSaved} in `view`: the caller
 * hoists the 33T ambient read once, passes its `Var` as `savedCp`, then calls
 * this per node. It emits exactly one CP store, `fn()` and one restore, and
 * never reads `CP_ADDR`.
 *
 * `epd` is materialised with {@link toVar} and only read, so a caller's `Var`
 * is never mutated and an `Expr` (e.g. `ptr.add(1)`) is accepted. `savedCp`
 * must be a `Var`; anything else is `EUDSL-TYPE`.
 */
export function _withEpdSaved<R>(
  savedCp: VarImpl,
  epd: number | VarImpl | ReadonlyVar | Expr,
  fn: () => R,
): R {
  if (!(savedCp instanceof VarImpl)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `_withEpdSaved savedCp must be a Var; got ${String(savedCp)}`,
    );
  }
  if (
    typeof epd === "boolean" ||
    !(
      typeof epd === "number" ||
      epd instanceof VarImpl ||
      epd instanceof ReadonlyVar ||
      epd instanceof Expr
    )
  ) {
    throw new EudslError(
      "EUDSL-TYPE",
      `_withEpdSaved epd must be an EPD-bearing value (int, Var, ReadonlyVar, or Expr); got ${String(epd)}`,
    );
  }
  if (typeof fn !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      `_withEpdSaved fn must be a function; got ${String(fn)}`,
    );
  }

  // `toVar` allocates a private scratch Var for an int or Expr; a Var or
  // ReadonlyVar is only read, never mutated.
  const scratch = toVar(epd as VarOperand);
  emitVarStore(scratch.id, AddrReloc(CP_ADDR), SetTo);
  try {
    return fn();
  } finally {
    emitVarStore(savedCp.id, AddrReloc(CP_ADDR), SetTo);
  }
}

// `view` cannot import `cp` (cp imports view), so hand it the runtime CP
// bindings the CUnit pointer reader resolves at call time.
_registerCpFactories(withEpd, _withEpdSaved);
