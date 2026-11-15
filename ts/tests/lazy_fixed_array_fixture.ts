/**
 * Fixture for P2-01 cross-file tests: a lazy FixedArray declared at module
 * scope in a separate file, used from the main test module.
 */
import { lazyFixedArray } from "../index.ts";

export const arr = lazyFixedArray("dword", 3);
