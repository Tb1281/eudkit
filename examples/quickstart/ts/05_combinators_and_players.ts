/**
 * 05. Combinators and Players — `condAll`/`condAny`/`condNot`, `Player`,
 * `PVariable`, `forEachPlayer`, and `displayTextAll`.
 *
 * Run these commands from the repository root:
 *
 *   ./target/debug/eudkit synth examples/quickstart/ts/05_combinators_and_players.ts --compact
 *   ./target/debug/eudkit compile examples/quickstart/ts/05_combinators_and_players.ts \
 *       --basemap tests/outputmap/basemap/basemap_silent.scx \
 *       -o /tmp/05_combinators_and_players-ts.scx
 *
 * What to notice:
 *
 * - `condAll`, `condAny`, and `condNot` compose arbitrary StarCraft conditions
 *   (AND/OR/NOT) and lower into flag preludes automatically, including >16-
 *   condition lists via auto-chunking.
 * - `Player(0)` exposes typed per-player fields (`minerals`, `gas`,
 *   `killScore`, etc.) without raw address math, and `PVariable(init)`
 *   allocates an 8-slot per-player variable array.
 * - `forEachPlayer(body)` walks active players (`playerExists(p) == 1` for
 *   players `0..7` at runtime, or an unrolled compile-time `{ players: [...] }`
 *   subset) **without modifying `CurrentPlayer`** (`0x6509B0`).
 * - `displayTextAll` binds `CurrentPlayer` temporarily to `userPlayer()`
 *   (local human id, including observers) and restores the previous
 *   `CurrentPlayer` right after.
 *
 * Installed consumers import `from "eudkit"`; inside this repository the same
 * names come from `../../../ts/index.ts`.
 */

import {
  FixedString,
  PVariable,
  Player,
  Var,
  buildModule,
  condAll,
  condAny,
  condNot,
  displayTextAll,
  forEachPlayer,
  once,
  when,
} from "../../../ts/index.ts";

export const module = buildModule(() => {
  const ticks = Var(0);
  const bonus = new PVariable(10);
  const p1 = new Player(0);
  const buf = new FixedString(64);

  // Boolean combinators lower OR/NOT through trigger flags while AND flattens.
  const ready = condAll(
    condAny(ticks.eq(0), ticks.ge(5)),
    condNot(p1.gas.ge(9999)),
  );

  when([ready], () => {
    once(() => displayTextAll("Combinators & Players ready!"));

    // Runtime player loop (0..7, activeOnly: true): CurrentPlayer stays untouched.
    forEachPlayer((player) => {
      const current = bonus.get(player);
      current.iadd(1);
      bonus.set(player, current);
    });

    buf.write("P1 bonus={:d}", bonus.get(0));
    buf.displayAt(1);
  });

  ticks.iadd(1);
});
