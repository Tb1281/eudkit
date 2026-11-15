/**
 * Fixture for P3 cross-file tests: lazy `Queue`, `Deque`, `Stack`, and
 * `UnitGroup` declarations at module scope in a separate file, used from
 * the main test module.
 */
import { lazyDeque, lazyQueue, lazyStack, lazyUnitGroup } from "../index.ts";

export const q = lazyQueue("dword", 3);
export const dq = lazyDeque("word", 2);
export const st = lazyStack(2);
export const ug = lazyUnitGroup(2, { autoPrune: false });
