/**
 * Fixture for P2-02 cross-file tests: a lazy struct instance declared at
 * module scope in a separate file, used from the main test module.
 */
import { TriggerStruct, dword, lazyStruct } from "../index.ts";

const Point = TriggerStruct("Point", { x: dword(), y: dword() });

export const s = lazyStruct(Point, { x: 3, y: 4 });
