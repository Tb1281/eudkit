/**
 * Block memory primitives: `memcpyEPD` / `memsetEPD`.
 *
 * EPD-primary C-like semantics: copy `count` dwords forward (overlapping
 * ranges are undefined, no memmove), or fill `count` dwords with a 32-bit
 * value. Constant counts fold at build time: `count == 0` is a no-op that
 * emits nothing, a constant `(dst, value, count)` fill with
 * `ceil(count/64) <= MAX_FOLD_CHUNKS` emits packed `SetDeaths` triggers
 * instead of the runtime loop, and a constant `count == 1` copy emits the loop
 * body once with no loop scaffolding. Constant counts above `0xFFFFFFFF` are
 * rejected with `EUDSL-RANGE`.
 *
 * Runtime loops use a check-first `whileLoop` on a page-backed remaining
 * counter. `memcpyEPD` binds CurrentPlayer (`0x6509B0`) to the source EPD and
 * reads through a CP cursor; both primitives land each dword through a
 * scratch pointer whose destination/value fields are patched from runtime
 * vars.
 *
 * `memcpyEPD`'s source also accepts a module data blob (a `Db` / `DbRef`):
 * the blob's runtime EPD is only known at assembly time, so the cursor
 * variable is seeded through a mode-1 blob relocation instead of a build-time
 * constant.
 */

import {
  Add,
  AddrReloc,
  DbRef,
  SetDeaths,
  SetDeathsX,
  SetTo,
  Subtract,
  TriggerFieldReloc,
  emitVarStore,
} from "./hir.ts";
import type { HirAct } from "./hir.ts";
import { ACT_FIELD_OFFSETS } from "./stock-spec.ts";
import { CP_ADDR, CPCursor } from "./cp.ts";
import { Trigger, _getActiveModuleBuilder, _registerTrigger } from "./trigger.ts";
import { when, whileLoop } from "./temporal.ts";
import { Var, VarImpl, Expr, toVar } from "./var.ts";
import { EPD_CURRENT_PLAYER, EudslError, describeValue as describe } from "./view.ts";

type EpdArg = number | VarImpl | Expr;
type CountArg = number | VarImpl | Expr;

/**
 * Fill folds emit ceil(count/64) packed SetDeaths triggers. The cap is a static
 * *byte* budget, not a crossover with the runtime loop: at 512 chunks the fold
 * costs 512T static / 1,232,896 B (~1.18 MiB, the ADR 0015 advisory ceiling)
 * but 512T executed instead of ~196,610T. ADR 0001 ranks executed cost first,
 * static second, bytes last, so do not lower this back to the old 7T static
 * crossover.
 */
export const MAX_FOLD_CHUNKS = 512;

/**
 * Materialize a block operand; the caller narrowed the domain first.
 *
 * `epdValue` / `countValue` / `memsetEPD` keep the block operand domain
 * (`Var | Expr`; a `ReadonlyVar`, float, etc. is `EUDSL-TYPE` there with the
 * surface-specific message). This only performs the shared "constant or
 * handle -> Var" step, delegated to the canonical kernel.
 */
function materialize(value: VarImpl | Expr): VarImpl {
  return toVar(value);
}

function epdValue(value: EpdArg, what: string, name: string): number | VarImpl {
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value < 0) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: ${name} must be a non-negative integer EPD or a game-time value, got ${describe(
          value,
        )}`,
      );
    }
    return value >>> 0;
  }
  if (value instanceof VarImpl || value instanceof Expr) {
    return materialize(value);
  }
  throw new EudslError(
    "EUDSL-TYPE",
    `${what}: ${name} must be a non-negative integer EPD or a game-time value, got ${describe(
      value,
    )}`,
  );
}

/**
 * Like {@link epdValue}, but also accepts a `Db`/`DbRef` payload as the
 * source: its EPD is resolved by a mode-1 relocation at assembly time.
 */
function srcEpdValue(
  value: EpdArg | DbRef,
  what: string,
  name: string,
): number | VarImpl | DbRef {
  if (value instanceof DbRef) return value;
  return epdValue(value, what, name);
}

/**
 * Seed a source-cursor variable with an EPD.
 *
 * A plain integer or a run-time value is a normal variable store. A `Db`
 * payload EPD is only known at assembly time, so the mode-1 relocation writes
 * it into the variable's own slot; nothing else in the loop differs.
 */
function seedEpdCursor(dst: VarImpl, node: number | VarImpl | DbRef): void {
  if (node instanceof DbRef) {
    const base = dst.act_set(0);
    const act: HirAct = {
      ...base,
      relocs: [
        ...(base.relocs ?? []),
        {
          field_offset: ACT_FIELD_OFFSETS.value,
          target_blob: node.blobId,
          target_offset: node.byteOffset,
          mode: 1,
        },
      ],
    };
    // `act_set` queued `base` as a standalone top-level act. The relocated
    // copy is a different object, so consume the original explicitly before
    // `Trigger` owns the copy.
    _getActiveModuleBuilder()?.consumeQueuedActs([base]);
    new Trigger({ conds: [], acts: [act], preserved: true });
    return;
  }
  dst.set(node);
}

function countValue(
  value: CountArg,
  what: string,
): { zero: true } | { zero: false; value: number | VarImpl } {
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value < 0) {
      throw new EudslError(
        "EUDSL-RANGE",
        `${what}: count must be a non-negative integer (dwords), got ${value}`,
      );
    }
    if (value > 0xffffffff) {
      throw new EudslError(
        "EUDSL-RANGE",
        `${what}: count must be at most 0xFFFFFFFF dwords, got ${value}`,
      );
    }
    if (value === 0) return { zero: true };
    return { zero: false, value: value >>> 0 };
  }
  if (value instanceof VarImpl || value instanceof Expr) {
    return { zero: false, value: materialize(value) };
  }
  throw new EudslError(
    "EUDSL-TYPE",
    `${what}: count must be a non-negative integer or a game-time value, got ${describe(
      value,
    )}`,
  );
}

/**
 * Seed `CurrentPlayer` to `srcEPD - 1` for a forward copy loop.
 *
 * The body advances CP before every read, so the first read lands on the
 * source EPD and a copy of `count` dwords ends at `srcEPD + count - 1` (the
 * observable end value). A constant source folds to one direct `SetDeaths`; a
 * runtime source is materialised into `tmp` and stored through the CP register.
 */
function seedCpFromSource(srcNode: number | VarImpl | DbRef, tmp: VarImpl): void {
  if (srcNode instanceof DbRef) {
    throw new Error("blob sources seed CurrentPlayer from a cursor var");
  }
  if (srcNode instanceof VarImpl) {
    tmp.set(srcNode);
    tmp.isub(1);
    emitVarStore(tmp.id, AddrReloc(CP_ADDR), SetTo);
  } else {
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, (srcNode - 1) >>> 0, 0);
  }
}

/** Emit the constant-count-1 copy body once with no loop scaffolding. */
function memcpyConstantOne(
  dstNode: number | VarImpl,
  srcNode: number | VarImpl | DbRef,
): void {
  const srcCur = new Var(0);
  const dstCur = new Var(0);
  const tmp = new Var(0);
  seedEpdCursor(srcCur, srcNode);
  dstCur.set(dstNode);

  const ptr = new Trigger({
    conds: [],
    acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff)],
    preserved: true,
    register: false,
  });

  emitVarStore(srcCur.id, AddrReloc(CP_ADDR), SetTo);
  tmp.set(new CPCursor(0).at(0).read());
  emitVarStore(
    dstCur.id,
    TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
    SetTo,
  );
  emitVarStore(
    tmp.id,
    TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
    SetTo,
  );
  _registerTrigger(ptr);
}

/**
 * Copy `count` dwords from EPD `src` to EPD `dst` (forward copy; overlapping
 * ranges are undefined). `count == 0` emits nothing.
 *
 * `src` may also be a `Db`/`DbRef`, in which case the copy reads the blob's
 * payload (e.g. `memcpyEPD(addr, db, Math.ceil(data.length / 4))`).
 */
export function memcpyEPD(dst: EpdArg, src: EpdArg | DbRef, count: CountArg): void {
  const what = "memcpyEPD()";
  const dstNode = epdValue(dst, what, "dstEPD");
  const srcNode = srcEpdValue(src, what, "srcEPD");
  const checked = countValue(count, what);
  if (checked.zero) return;

  const countNode = checked.value;

  // A constant count of 1 runs the check-first loop body exactly once, so it
  // needs no counter. Keep that path on the cursor-variable lowering.
  if (typeof count === "number" && count === 1) {
    memcpyConstantOne(dstNode, srcNode);
    return;
  }

  // The counter is set from the source expression once. A constant count still
  // pays one setup store, keeping the loop scaffolding's trigger count
  // identical to the old cursor-variable lowering.
  const cnt = new Var(0);
  const tmp = new Var(0);
  cnt.set(countNode);

  // A blob's EPD is only known at assembly time: seed one temp cursor, then
  // (gated) put it into CurrentPlayer. Constant/runtime sources need no cursor
  // var at all.
  let srcCursor: VarImpl | null = null;
  if (srcNode instanceof DbRef) {
    srcCursor = new Var(0);
    seedEpdCursor(srcCursor, srcNode);
  }

  const ptr = new Trigger({
    conds: [],
    acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff)],
    preserved: true,
    register: false,
  });

  when(cnt.ge(1), () => {
    if (dstNode instanceof VarImpl) {
      emitVarStore(
        dstNode.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
        SetTo,
      );
    } else {
      SetDeaths(ptr.act(0).dest, SetTo, dstNode, 0);
    }
    if (srcCursor !== null) {
      emitVarStore(srcCursor.id, AddrReloc(CP_ADDR), SetTo);
      SetDeaths(EPD_CURRENT_PLAYER, Subtract, 1, 0);
    } else {
      seedCpFromSource(srcNode, tmp);
    }
  });

  whileLoop(cnt.ge(1), () => {
    SetDeaths(EPD_CURRENT_PLAYER, Add, 1, 0);
    tmp.set(new CPCursor(0).at(0).read());
    emitVarStore(
      tmp.id,
      TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
      SetTo,
    );
    _registerTrigger(ptr);
    SetDeaths(ptr.act(0).dest, Add, 1, 0);
    cnt.isub(1);
  });
}

/**
 * Fill `count` dwords at EPD `dst` with the 32-bit `value`. `count == 0`
 * emits nothing.
 */
export function memsetEPD(dst: EpdArg, value: number | VarImpl | Expr, count: CountArg): void {
  const what = "memsetEPD()";
  const dstNode = epdValue(dst, what, "dstEPD");
  const checked = countValue(count, what);
  if (checked.zero) return;

  const countNode = checked.value;
  let valueVar: VarImpl | null = null;
  let valueConst = 0;
  if (typeof value === "number") {
    if (!Number.isInteger(value)) {
      throw new EudslError(
        "EUDSL-TYPE",
        `${what}: value must be an integer or a game-time value, got ${value}`,
      );
    }
    valueConst = value >>> 0;
  } else if (value instanceof VarImpl || value instanceof Expr) {
    valueVar = materialize(value);
  } else {
    throw new EudslError(
      "EUDSL-TYPE",
      `${what}: value must be an integer or a game-time value, got ${describe(value)}`,
    );
  }

  if (typeof dst === "number" && typeof value === "number" && typeof count === "number") {
    // The whole fill is known at build time: emit one packed SetDeaths trigger
    // per 64 dwords. The cap is a static/byte budget, not a crossover with the
    // loop -- ADR 0001 puts executed cost first, so the fold is worth up to
    // 512T static / 1.18 MiB (the ADR 0015 advisory ceiling) to turn
    // ~196,610T executed into 512T. Do not lower it back.
    const baseEpd = dst >>> 0;
    const chunks = Math.ceil(count / 64);
    if (chunks <= MAX_FOLD_CHUNKS) {
      for (let base = 0; base < count; base += 64) {
        const span = Math.min(64, count - base);
        const acts: HirAct[] = [];
        for (let i = 0; i < span; i += 1) {
          acts.push(SetDeaths((baseEpd + base + i) >>> 0, SetTo, valueConst, 0));
        }
        new Trigger({ conds: [], acts });
      }
      return;
    }
  }

  // Always emit a runtime store. A constant count cannot use
  // `new Var(countNode)`: that is a one-time module init, so a non-folded fill
  // inside a loop or a re-entered `whileLoop` would run only on its first pass.
  const cnt = new Var(0);
  cnt.set(countNode);

  const ptr = new Trigger({
    conds: [],
    acts: [SetDeathsX(0, SetTo, 0, 0, 0xffffffff)],
    preserved: true,
    register: false,
  });

  // A runtime zero count must leave the scratch destination untouched for
  // every destination form, including a constant EPD.
  when(cnt.ge(1), () => {
    if (dstNode instanceof VarImpl) {
      emitVarStore(
        dstNode.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
        SetTo,
      );
    } else {
      SetDeaths(ptr.act(0).dest, SetTo, dstNode, 0);
    }
  });

  whileLoop(cnt.ge(1), () => {
    if (valueVar !== null) {
      emitVarStore(
        valueVar.id,
        TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
        SetTo,
      );
    } else {
      SetDeaths(ptr.act(0).value, SetTo, valueConst, 0);
    }
    _registerTrigger(ptr);
    SetDeaths(ptr.act(0).dest, Add, 1, 0);
    cnt.isub(1);
  });
}

export const memcpy_epd = memcpyEPD;
export const memset_epd = memsetEPD;
