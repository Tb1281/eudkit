/**
 * Example 05: StarCraft .dat Tables (`unitsDat` / `weaponsDat` / `techDat` / `upgradesDat`).
 *
 * Migration notes (eudplib -> eudkit):
 * - eudplib: `TrgUnit("Terran Marine").maxHp = 60 * 256`, `Weapon("Gauss Rifle").damage = 8`,
 *   `Tech("Stim Packs").mineralCost += 50`, and dynamic `f_dwread_epd(EPD(0x662350) + unit_id)`.
 * - eudkit: Typed SoA DAT tables (`unitsDat`, `weaponsDat`, `techDat`, `upgradesDat`,
 *   `flingyDat`) indexed by identifier-safe symbol namespaces (`UnitType`, `WeaponType`,
 *   `TechType`, `UpgradeType`, `FlingyType`). For example,
 *   `TrgUnit("Terran Marine")` / `Weapon("Gauss Rifle")` -> `weaponsDat[WeaponType.GaussRifle]`.
 * - ADR 0009 (상수 인덱스 전용 / Constant index only):
 *   DAT 테이블은 SoA 구조이므로 서브-dword 필드의 시프트/마스크를 컴파일 타임에 계산해야
 *   합니다. 따라서 `unitsDat[var]`처럼 런타임 `Var` 인덱스를 넘기면 `EUDSL-TARGET` 에러가
 *   발생합니다. 런타임 unit ID가 필요하면 알려진 유닛 집합에는 `dispatch(unitId, [...])`를
 *   사용하거나, `CurrentPlayer` (`0x6509B0`) 포인터 연산 + 바이트 레인 마스크를 명시적으로
 *   사용한 뒤 CP를 `0`으로 복원하세요.
 *
 * Note: External npm `"eudkit"` packages import `from "eudkit"`.
 */

import {
  TechType,
  UnitType,
  UpgradeType,
  Var,
  WeaponType,
  buildModule,
  once,
  techDat,
  unitsDat,
  upgradesDat,
  weaponsDat,
  when,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  const marineHp = Var(0);
  const armorBaseCost = Var(0);

  once(() => {
    // Patch Marine maxHp (8.8 fixed point: 60 * 256 = 15360) and Gauss Rifle damage
    unitsDat[UnitType.TerranMarine].maxHp.set(60 * 256);
    weaponsDat[WeaponType.GaussRifle].damage.set(8);

    // Increment Stim Packs mineral cost by 50
    techDat[TechType.StimPacks].mineralCost.iadd(50);

    // Check weaponsDat[WeaponType.GaussRifle].damage >= 8 and read back Marine maxHp
    when([weaponsDat[WeaponType.GaussRifle].damage.ge(8)], () => {
      const readHp = unitsDat[UnitType.TerranMarine].maxHp.read();
      marineHp.set(readHp);
    });

    // Read upgradesDat field into a Var
    const readUpgradeCost = upgradesDat[UpgradeType.TerranInfantryArmor].mineralCostBase.read();
    armorBaseCost.set(readUpgradeCost);

    // Dynamic unit IDs (ADR 0009):
    // Passing a runtime `Var` to `unitsDat[unitId]` raises `EUDSL-TARGET`.
    // Handle dynamic IDs via `dispatch(unitId, [...])` for known unit types or
    // explicit `CurrentPlayer` (`0x6509B0`) EPD arithmetic + byte-lane masks.
  });
});
