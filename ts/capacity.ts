/**
 * CUnit capacity predicates and the tier-3 free-slot scan (ADR 0015 §9).
 *
 * `0x628438` is the dword holding the head of the CUnit free list.
 * `head == 0` is the exact answer to "the engine has no free CUnit slot right
 * now", and it costs one condition 15 and no pointer reads. `0x62843c` is the
 * documented free-list tail; no helper is built on it, because the head alone
 * carries the one-condition answer.
 *
 * Tier 1 (`freeListEmpty`) is a predicate: it returns an ordinary `HirCond`,
 * not a wrapped `Trigger`, so it composes into the caller's
 * `when`/`once`/`until` gate and consumes one of that gate's 16 condition
 * slots. It is advisory, not authoritative: the engine's allocator can still
 * refuse a creation for reasons this dword cannot see.
 *
 * Tier 3 (`freeListWalk` / `freeSlotCount`) is the exact count: a one-shot
 * forward walk of the free list from the same head through each node's `next`
 * link (`ptr + 1`). It costs ~37T executed per free node, so the API
 * contract is "once per module, never per tick" and is enforced at build
 * time — a second call site in one module is `EUDSL-BOUNDS`.
 *
 * Python mirror: `py/capacity.py`.
 */

import { CP_ADDR, readCurrentPlayerRegister } from "./cp.ts";
import {
  AddrReloc,
  Exactly,
  Memory,
  emitVarStore,
  type HirCond,
} from "./hir.ts";
import { when, whileLoop } from "./temporal.ts";
import { _getActiveModuleBuilder } from "./trigger.ts";
import { BREAK } from "./units.ts";
import { Var, VarImpl } from "./var.ts";
import {
  CUNIT_SLOT_COUNT,
  EPD,
  EudslError,
  inCunitPool,
  readCunitPointerSaved,
} from "./view.ts";

/** Dword holding the head of the CUnit free list. */
export const CUNIT_FREE_HEAD = 0x628438;
/** Dword holding the tail of the CUnit free list (documented, no helper). */
export const CUNIT_FREE_TAIL = 0x62843c;
/**
 * Hard stop for the tier-3 walk: the pool has 1699 slots, so a cycle can
 * never legitimately outlast it.
 */
export const CUNIT_FREE_SCAN_MAX = CUNIT_SLOT_COUNT;

/** One-shot bookkeeping: builder -> number of tier-3 call sites claimed. */
const tier3CallSites = new WeakMap<object, number>();

const ONE_SHOT_MESSAGE =
  "freeListWalk()/freeSlotCount() is a tier-3 one-shot scan; " +
  "one call site per module (ADR 0015 §9)";

/** 1C, 0 reads: the CUnit free list has no node right now. */
export function freeListEmpty(): HirCond {
  return Memory(CUNIT_FREE_HEAD, Exactly, 0);
}

function describe(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  return String(value);
}

function claimTier3CallSite(): void {
  const builder = _getActiveModuleBuilder();
  if (builder === null) return;
  const count = (tier3CallSites.get(builder) ?? 0) + 1;
  tier3CallSites.set(builder, count);
  if (count > 1) {
    throw new EudslError("EUDSL-BOUNDS", ONE_SHOT_MESSAGE);
  }
}

function requireScanLimit(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new EudslError(
      "EUDSL-TYPE",
      `freeListWalk maxNodes must be a plain integer; got ${describe(value)}`,
    );
  }
  if (value < 1 || value > CUNIT_FREE_SCAN_MAX) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `freeListWalk maxNodes must be in 1..${CUNIT_FREE_SCAN_MAX}; got ${value}`,
    );
  }
  return value;
}

/**
 * Walk the free list once, calling `body()` per free node.
 *
 * This is tier 3: the exact count, independent of any tracker, at ~37T
 * executed per free node. Use it once per module (map start or on author
 * request); a per-frame capacity check is `freeListEmpty()`.
 *
 * `maxNodes` is a plain integer in `1..1699` and is also the cycle guard: the
 * walk stops on a null pointer, a pointer outside the CUnit pool, or
 * `maxNodes` iterations — whichever comes first — so a corrupted list costs
 * at most `1699 × 37T` and never hangs the game. Return `BREAK` from `body`
 * to stop after the current node.
 */
export function freeListWalk(
  body: () => void,
  { maxNodes = CUNIT_FREE_SCAN_MAX }: { maxNodes?: number } = {},
): void {
  if (typeof body !== "function") {
    throw new EudslError(
      "EUDSL-TYPE",
      `freeListWalk body must be a function; got ${describe(body)}`,
    );
  }
  const limit = requireScanLimit(maxNodes);
  claimTier3CallSite();
  emitFreeListWalk(body as () => unknown, limit);
}

/** Emit the tier-3 walk; the caller owns validation and the call-site claim. */
function emitFreeListWalk(body: () => unknown, limit: number): void {
  const ptr = Var(0);
  const done = Var(0);
  const remaining = Var(0);
  const saved = readCurrentPlayerRegister();

  remaining.set(limit);
  ptr.set(readCunitPointerSaved(saved, EPD(CUNIT_FREE_HEAD)));

  whileLoop([...inCunitPool(ptr), done.eq(0)], () => {
    // Read the successor before the body: the free list mutates when a unit
    // is created, so a body that allocates the current node must still reach
    // the next one.
    const nxt = readCunitPointerSaved(saved, ptr.add(1));
    const result = body();
    if (result === BREAK) done.set(1);
    remaining.isub(1);
    when([remaining.eq(0)], () => done.set(1));
    when([done.eq(0)], () => ptr.set(nxt));
  });
  emitVarStore(saved.id, AddrReloc(CP_ADDR));
}

/**
 * Exact number of free CUnit nodes right now (tier 3, one-shot).
 *
 * Delegates to `freeListWalk`, so it shares that call site: calling both in
 * one module is `EUDSL-BOUNDS`. The call-site claim runs **before** the count
 * `Var` is allocated, so a rejected call leaves no HIR.
 */
export function freeSlotCount(): VarImpl {
  claimTier3CallSite();
  const count = Var(0);
  emitFreeListWalk(() => {
    count.iadd(1);
  }, CUNIT_FREE_SCAN_MAX);
  return count;
}

export const free_list_empty = freeListEmpty;
export const free_list_walk = freeListWalk;
export const free_slot_count = freeSlotCount;
