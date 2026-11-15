/**
 * Fixture for P1-02 cross-file tests: a lazy Var declared at module scope
 * in a separate file, used from the main test module.
 */
import { lazyVar } from "../index.ts";

export const g = lazyVar(5);
