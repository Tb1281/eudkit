/**
 * CUnit capacity predicates: the one-condition free-list truth (ADR 0015 §9).
 *
 * `0x628438` is the dword holding the head of the CUnit free list.
 * `head == 0` is the exact answer to "the engine has no free CUnit slot right
 * now", and it costs one condition 15 and no pointer reads. `0x62843c` is the
 * documented free-list tail; no helper is built on it, because the head alone
 * carries the one-condition answer.
 *
 * The result is an ordinary `HirCond`, not a wrapped `Trigger`: it composes
 * into the caller's `when`/`once`/`until` gate and consumes one of that gate's
 * 16 condition slots. This is tier 1 of the capacity vocabulary in ADR 0015
 * §9. It is advisory, not authoritative: the engine's allocator can still
 * refuse a creation for reasons this dword cannot see.
 *
 * Python mirror: `py/capacity.py`.
 */

import { Exactly, Memory, type HirCond } from "./hir.ts";

/** Dword holding the head of the CUnit free list. */
export const CUNIT_FREE_HEAD = 0x628438;
/** Dword holding the tail of the CUnit free list (documented, no helper). */
export const CUNIT_FREE_TAIL = 0x62843c;

/** 1C, 0 reads: the CUnit free list has no node right now. */
export function freeListEmpty(): HirCond {
  return Memory(CUNIT_FREE_HEAD, Exactly, 0);
}

export const free_list_empty = freeListEmpty;
