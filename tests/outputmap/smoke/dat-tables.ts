/**
 * In-game verification for issue #51: DAT table accessors.
 *
 * Displays two lines:
 *
 *   1. `DAT HP=00002800`  (Marine `maxHp`, raw 8.8 fixed point: 40 * 256)
 *   2. `DAT DMG=00000006` (Gauss Rifle `damage`, 6 decimal)
 *
 * The values are read from the live `units.dat` and `weapons.dat` fields
 * through `unitsDat`/`weaponsDat`, so the map proves symbol lookup,
 * constant-index address math, byte-lane folding, and `.read`.
 */
import {
  FixedString,
  UnitType,
  WeaponType,
  buildModule,
  every,
  unitsDat,
  weaponsDat,
} from "../../../ts/index.ts";

export const module = buildModule(() => {
  const marineHp = unitsDat[UnitType.TerranMarine].maxHp.read();
  const gaussDamage = weaponsDat[WeaponType.GaussRifle].damage.read();

  const hpLine = new FixedString(32);
  every(6, () => {
    hpLine.clear();
    hpLine.write("DAT HP={:x}", marineHp);
    hpLine.displayAt(1);
  });

  const damageLine = new FixedString(32);
  every(6, () => {
    damageLine.clear();
    damageLine.write("DAT DMG={:x}", gaussDamage);
    damageLine.displayAt(2);
  });
});
