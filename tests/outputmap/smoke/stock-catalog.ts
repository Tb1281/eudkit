/**
 * Stock trigger catalog smoke map (issue #68).
 *
 * Uses the explicit `module` source entry. The payload:
 *
 * 1. waits one game second, displays `STOCK SETUP`, then `CreateUnit`s a
 *    Marine at `Anywhere` (the engine's map-center sentinel),
 *    `MoveLocation`s location 63 onto it, and `CenterView`s location 63.
 *    The marker is deliberately first: the `MoveLocation`/`CenterView`
 *    sequence defers the rest of the trigger's actions to the next cycle,
 *    which would put the setup marker *after* the later `STOCK BRING` marker
 *    in the chat log;
 * 2. when `Bring` sees that Marine in location 63, sets the `STOCK`
 *    leaderboard, sends a `Transmission` (text + sound), and `KillUnit`s it.
 *
 * Expected in-game result:
 * - the camera centers on a newly created Marine;
 * - a Marine portrait transmission reads `STOCK CATALOG OK`;
 * - a leaderboard labelled `STOCK` appears;
 * - the Marine is killed when the transmission finishes.
 */
import {
  Add,
  Anywhere,
  AtLeast,
  Bring,
  CenterView,
  CreateUnit,
  DisplayText,
  ElapsedTime,
  KillUnit,
  LeaderBoardControl,
  MoveLocation,
  P1,
  Transmission,
  UnitType,
  buildModule,
  once,
} from "../../../ts/index.ts";

const STOCK_POINT = 63;
const SOUND = "sound\\terran\\advisor\\tadup00.wav";

export const module = buildModule(() => {
  once(ElapsedTime(AtLeast, 1), () => {
    DisplayText("STOCK SETUP");
    CreateUnit(1, UnitType.TerranMarine, Anywhere, P1);
    MoveLocation(STOCK_POINT, UnitType.TerranMarine, P1, Anywhere);
    CenterView(STOCK_POINT);
  });

  once(Bring(P1, AtLeast, 1, UnitType.TerranMarine, STOCK_POINT), () => {
    DisplayText("STOCK BRING");
    LeaderBoardControl(UnitType.TerranMarine, "STOCK");
    Transmission(UnitType.TerranMarine, STOCK_POINT, SOUND, Add, 5000, "STOCK CATALOG OK");
    KillUnit(UnitType.TerranMarine, P1);
  });
});
