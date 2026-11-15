/**
 * Smoke map 4: `cameraFollow` (Cycle 6 `mulConst` -> `Var.mul`) + live camera HUD.
 *
 * Mirrors `camera-follow.py` 1:1.
 */

import {
  Anywhere,
  AtLeast,
  CAMERA_X_ADDR,
  CAMERA_Y_ADDR,
  CenterView,
  CreateUnit,
  EPD,
  ElapsedTime,
  FixedString,
  MULTIPLAYER_MODE_ADDR,
  MemFieldHandle,
  MoveLocation,
  P1,
  SetSwitch,
  SwitchActionSet,
  UnitType,
  buildModule,
  cameraFollow,
  defineMemView,
  every,
  once,
  when,
} from "../../../ts/index.ts";

const TARGET_LOC = 63;
const CAMERA_LOC = 62;

export const module = buildModule(() => {
  const u32Field = { offset: 0, size: 4 as const };
  const u32Schema = defineMemView("U32", 4, { val: u32Field });
  const camXField = new MemFieldHandle(u32Schema, u32Field, EPD(CAMERA_X_ADDR), "val");
  const camYField = new MemFieldHandle(u32Schema, u32Field, EPD(CAMERA_Y_ADDR), "val");
  const mpField = new MemFieldHandle(u32Schema, u32Field, EPD(MULTIPLAYER_MODE_ADDR), "val");

  once(ElapsedTime(AtLeast, 1), () => {
    CreateUnit(1, UnitType.TerranMarine, Anywhere, P1);
    MoveLocation(TARGET_LOC, UnitType.TerranMarine, P1, Anywhere);
    CenterView(TARGET_LOC);
    SetSwitch(0, SwitchActionSet);
  });

  MoveLocation(TARGET_LOC, UnitType.TerranMarine, P1, Anywhere);
  const state = cameraFollow(TARGET_LOC, CAMERA_LOC, 5, 48, 0, {
    onUnit: UnitType.TerranMarine,
    owner: P1,
  });

  const line1 = new FixedString(64);
  const line2 = new FixedString(64);
  const line3 = new FixedString(64);

  every(6, () => {
    const camX = camXField.read();
    const camY = camYField.read();
    const mpMode = mpField.read();

    line1.clear();
    line1.write("CAMERA X={:\x1a>4d} Y={:\x1a>4d} SCREENW={:\x1a>4d}", camX, camY, state.screenX);
    line1.displayAt(1);

    line2.clear();
    when(
      [mpMode.ge(1)],
      () => line2.write("MODE=MP ({:\x1a>1d}) CAMMOVE ACTIVE - move Marine!", mpMode),
      () => line2.write("MODE=SP (0x57F0B4=0) cammove idle; use LAN/MP to ease"),
    );
    line2.displayAt(2);

    line3.clear();
    line3.write("LAST_TICK={:x}", state.lastTick);
    line3.displayAt(3);
  });
});
