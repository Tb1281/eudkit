/**
 * 03. Control Flow and Time — `when`/`otherwise`, `once`, `every`, `after`,
 * and `dispatch`.
 *
 * Run these commands from the repository root:
 *
 *   ./target/debug/eudkit synth examples/quickstart/ts/03_control_flow_and_temporal.ts --compact
 *   ./target/debug/eudkit compile examples/quickstart/ts/03_control_flow_and_temporal.ts \
 *       --basemap tests/outputmap/basemap/basemap_silent.scx \
 *       -o /tmp/03_control_flow_and_temporal-ts.scx
 *
 * Why can't I write `if` or `while`?
 *
 * `phase.eq(0)` is a *runtime condition*: it describes StarCraft memory that
 * only exists once the map is running. JavaScript `if`/`while` execute at
 * build time, before a game exists, so they cannot branch on it; Python raises
 * `EUDSL-COND-TRUTHIFY` and TypeScript's opt-in truthiness checker reports
 * `EUDSL-TS-TRUTHIFY` (ADR 0020). Use the temporal forms:
 *
 * - `when(conds, then, otherwise)` — every loop while `conds` hold; the third
 *   argument is the runtime else arm (`otherwise`).
 * - `once(conds, body)` — the first loop `conds` hold, then self-disable.
 * - `every(n, body)` — every `n` trigger loops.
 * - `after(n, body)` — arm an `n`-loop countdown, then fire once.
 * - `dispatch(value, cases, fallback)` — one-of-N integer choice with
 *   compile-time keys (a runtime `when` chain, never build-time branching).
 *
 * Installed consumers import `from "eudkit"`; inside this repository the same
 * names come from "../../../ts/index.ts".
 */

import {
  FixedString,
  Var,
  after,
  buildModule,
  dispatch,
  every,
  once,
  when,
} from "../../../ts/index.ts";

export const module = buildModule(() => {
  const phase = Var(0);
  const score = Var(0);
  const banner = new FixedString(32);
  const status = new FixedString(64);

  // once(): a boot banner on the first trigger loop only.
  once([phase.eq(0)], () => {
    banner.write("BOOT PHASE=0");
    banner.display();
  });

  // when(..., otherwise): runtime if/else on a game-time Var.
  when(
    [phase.eq(0)],
    () => {
      score.set(10);
    },
    () => {
      score.set(20);
    },
  );

  // every(): advance only while still in phase 0.
  every(4, () => {
    when([phase.eq(0)], () => {
      phase.set(1);
    });
  });

  // after(): a one-shot delayed transition to phase 2.
  after(9, () => {
    phase.set(2);
  });

  // dispatch(): choose a HUD line from the runtime phase value.
  dispatch(
    phase,
    [
      [
        0,
        () => {
          status.write("PHASE=0 SCORE={:\x1a>3d}", score);
        },
      ],
      [
        1,
        () => {
          status.write("PHASE=1 SCORE={:\x1a>3d}", score);
        },
      ],
    ],
    () => {
      status.write("PHASE=2 SCORE={:\x1a>3d}", score);
    },
  );
  status.displayAt(1);
});
