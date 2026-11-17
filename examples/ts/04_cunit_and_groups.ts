/**
 * eudkit TypeScript example: CUnit / CSprite Views & UnitGroup Iteration.
 *
 * Shared Intent:
 *   Wrap a CUnit EPD, inspect hp / unitType / posX / sprite, track up to 6
 *   unit EPDs in a UnitGroup with auto-prune on dead units, and count living
 *   visits in the group loop.
 *
 * Migration Notes (eudplib -> eudkit):
 *   [EN]
 *   1. CUnit(epd) / CSprite(epd) are zero-storage MemView proxies backed by
 *      `schema/layouts/{cunit,csprite}.json`. Direct property reads
 *      (`unit.hp.read()`), comparisons (`unit.unitType.eq(...)`), and writes
 *      (`unit.hp.set(...)`) emit MemoryX/SetDeathsX directly. Write RHS is
 *      const-only (plain integer); dynamic Var writes throw `EUDSL-TYPE`.
 *   2. `new UnitGroup(6, { compact: true, autoPrune: true })` replaces
 *      eudplib's `group.cploop` + `for dead in unit.dying:` boilerplate.
 *      `autoPrune` emits a per-frame sweep checking `+0x0C` (sprite);
 *      `group.forEach((unitEpd) => ...)` emits the callback body once using a
 *      CP-sweep cursor that automatically saves and restores `CurrentPlayer`
 *      (`0x6509B0`) and passes a single reused scratch `Var` (`unitEpd`).
 *      Copy `unitEpd` if needed; never retain/alias it across iterations.
 *
 *   [KR]
 *   1. `CUnit(epd)` / `CSprite(epd)`는 별도 저장 공간을 갖지 않는 MemView
 *      프록시입니다. `unit.hp.read()`, `unit.unitType.eq(...)`,
 *      `unit.hp.set(...)` 형태로 필드에 직접 접근하며, 쓰기 우변(RHS)은
 *      컴파일 타임 정수 상수만 허용됩니다(동적 `Var` 쓰기 시 `EUDSL-TYPE`).
 *   2. `new UnitGroup(6, { compact: true, autoPrune: true })`는 eudplib의
 *      `cploop` + `unit.dying` 패턴을 대체합니다. `autoPrune`이 매 프레임
 *      스프라이트(`+0x0C`)가 0인 사망 유닛을 자동 제거하고, `forEach`는
 *      CP-sweep으로 `CurrentPlayer`(`0x6509B0`)를 자동 저장/복원하며
 *      재사용되는 단일 스크래치 `Var`(`unitEpd`)를 콜백에 전달합니다.
 *
 * Note:
 *   Inside this repo we import `from "../../ts/index.ts"`; external packages
 *   using the published npm `"eudkit"` package import `from "eudkit"`.
 */

import {
  CSprite,
  CUnit,
  EPD,
  UnitGroup,
  UnitType,
  Var,
  buildModule,
  when,
  type VarImpl,
} from "../../ts/index.ts";

const FIRST_UNIT_EPD = EPD(0x59cca8);
const SECOND_UNIT_EPD = EPD(0x59cca8 + 336);
const FIRST_SPRITE_EPD = EPD(0x629d98);

export const module = buildModule(() => {
  const unit = CUnit(FIRST_UNIT_EPD);
  const deadUnit = CUnit(SECOND_UNIT_EPD);
  const sprite = CSprite(FIRST_SPRITE_EPD);

  // Compact UnitGroup emits autoPrune & forEach as single CP-sweep loops
  // (saves/restores CurrentPlayer 0x6509B0 automatically).
  const group = new UnitGroup(6, { compact: true, autoPrune: true });

  const phase = Var(0);
  const inspectedHp = Var(0);
  const inspectedPosX = Var(0);
  const inspectedSprite = Var(0);
  const spritePosX = Var(0);
  const marineOk = Var(0);
  const livingVisits = Var(0);
  const lastVisitedEpd = Var(0);

  when([phase.eq(0)], () => {
    // Const-only write RHS on CUnit / CSprite properties
    unit.hp.set(40 * 256);
    unit.unitType.set(UnitType.TerranMarine);
    unit.posX.set(320);
    unit.sprite.set(0x629d98);
    sprite.posX.set(320);

    // Dead unit (sprite == 0): autoPrune clears this slot on the next tick.
    // Note: the eudplib baseline marks death differently (order == 0); the
    // two libraries detect a dead unit by different fields.
    deadUnit.sprite.set(0);

    group.add(FIRST_UNIT_EPD);
    group.add(SECOND_UNIT_EPD);
  });

  // Direct CUnit & CSprite property reads
  inspectedHp.set(unit.hp.read());
  inspectedPosX.set(unit.posX.read());
  inspectedSprite.set(unit.sprite.read());
  spritePosX.set(sprite.posX.read());

  // Direct CUnit & CSprite property comparisons
  when(
    [
      unit.hp.ge(20 * 256),
      unit.unitType.eq(UnitType.TerranMarine),
      unit.posX.ge(100),
      unit.sprite.ne(0),
      sprite.posX.eq(320),
    ],
    () => {
      marineOk.set(1);
    },
  );

  // Group iteration: `unitEpd` is one reused scratch Var per live slot
  livingVisits.set(0);
  group.forEach((unitEpd: VarImpl) => {
    livingVisits.iadd(1);
    lastVisitedEpd.set(unitEpd);
  });

  phase.iadd(1);
});
