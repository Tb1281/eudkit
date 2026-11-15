/**
 * 04. Units and Triggers — stock `Trigger`/`Bring`/`CreateUnit`, `CUnit` field
 * access, and `FixedArray` basics.
 *
 * Run these commands from the repository root:
 *
 *   ./target/debug/eudkit synth examples/quickstart/ts/04_units_and_triggers.ts --compact
 *   ./target/debug/eudkit compile examples/quickstart/ts/04_units_and_triggers.ts \
 *       --basemap tests/outputmap/basemap/basemap_silent.scx \
 *       -o /tmp/04_units_and_triggers-ts.scx
 *
 * What to notice:
 *
 * - `new Trigger({ conds: [...], acts: [...] })` is the low-level StarCraft
 *   trigger. `Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere)` and
 *   `CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)` are stock StarEdit
 *   helpers (parameter order matches StarEdit).
 * - `CUnit(EPD)` is a zero-storage typed view over a CUnit slot.
 *   `unit.hp.read()` reads game memory; `unit.hp.set(...)` writes a constant
 *   (runtime `Var` values need the dynamic-write patterns in migration example
 *   08, not `.set(var)`).
 * - `FixedArray("dword", 4)` allocates four eudkit-owned dword slots.
 *   Compile-time `set`/`get` indexes fold to direct actions; a runtime `Var`
 *   index switches to the paged reader.
 * - The shown `Trigger` is `preserved: true` so it keeps running; the
 *   `spawned` flag makes the demonstration spawn exactly one Marine.
 *
 * Installed consumers import `from "eudkit"`; inside this repository the same
 * names come from "../../../ts/index.ts".
 */

import {
  Anywhere,
  AtLeast,
  Bring,
  CUnit,
  CreateUnit,
  EPD,
  FixedArray,
  FixedString,
  P1,
  Trigger,
  UnitType,
  Var,
  buildModule,
  when,
} from "../../../ts/index.ts";

const FIRST_UNIT_EPD = EPD(0x59cca8);

export const module = buildModule(() => {
  const unit = CUnit(FIRST_UNIT_EPD);
  const hp = Var(0);
  const score = Var(0);
  const spawned = Var(0);
  const slots = FixedArray("dword", 4);
  const status = new FixedString(64);

  // CUnit field writes are compile-time constants; reads return a Var.
  unit.hp.set(40 * 256); // SC stores HP in 8.8 fixed point.
  unit.unitType.set(UnitType.TerranMarine);
  unit.posX.set(320);
  hp.set(unit.hp.read());

  // FixedArray basics: owned storage, constant indexes fold at build time.
  slots.set(0, 7);
  slots.set(1, 11);
  score.set(slots.get(0));
  score.iadd(slots.get(1));

  // A stock trigger: Bring sees one Marine, then CreateUnit spawns one.
  new Trigger({
    conds: [
      Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere),
      spawned.eq(0),
    ],
    acts: [
      CreateUnit(1, UnitType.TerranMarine, Anywhere, P1),
      spawned.act_set(1),
    ],
    preserved: true,
  });

  // Direct CUnit comparison, plus a HUD readout of the CUnit/array values.
  when([unit.hp.ge(20 * 256)], () => {
    score.iadd(hp);
  });
  status.write("HP={:\x1a>5d} SCORE={:\x1a>4d}", hp, score);
  status.displayAt(4);
});
