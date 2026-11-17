/**
 * 02. Variables and Strings — runtime `Var` arithmetic plus a HUD line.
 *
 * Run these commands from the repository root:
 *
 *   ./target/debug/eudkit synth examples/quickstart/ts/02_variables_and_strings.ts --compact
 *   ./target/debug/eudkit compile examples/quickstart/ts/02_variables_and_strings.ts \
 *       --basemap tests/outputmap/basemap/basemap_silent.scx \
 *       -o /tmp/02_variables_and_strings-ts.scx
 *
 * What to notice:
 *
 * - `Var(0)` is mutable game memory, not a JavaScript number. `counter.iadd(1)`
 *   queues a trigger action that runs once per trigger loop in StarCraft.
 * - `counter.mul(2)` is an expression; wrapping it in `Var(...)` materializes
 *   the result once so it can be formatted and reused.
 * - `FixedString.write` renders game-time values into SC:R string memory.
 *   The `\x1a` in a format spec is SC:R's zero-width alignment pad, so
 *   `COUNT=   3` appears without extra visible spaces.
 * - `status.displayAt(2)` shifts the display cursor forward by 2 lines
 *   (mod 11) from its current position and then restores it; it does not
 *   write to absolute chat line 2. `display()`
 *   appends to the next line instead.
 *
 * Installed consumers import `from "eudkit"`; inside this repository the same
 * names come from "../../../ts/index.ts".
 */

import { FixedString, Var, buildModule } from "../../../ts/index.ts";

export const module = buildModule(() => {
  const counter = Var(0);
  const status = new FixedString(64);

  // Runtime arithmetic: each call emits trigger actions, not build-time math.
  counter.iadd(1);
  const doubled = Var(counter.mul(2));
  counter.iadd(doubled);

  status.write("COUNT={:\x1a>4d} DOUBLE={:\x1a>4d}", counter, doubled);
  status.displayAt(2);
});
