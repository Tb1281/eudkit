/**
 * DAT accessors: typed table entry lookup, address math, symbol names, and
 * field-handle semantics.
 *
 * Expected addresses are written here independently of the schema so a schema
 * edit that moves a table fails loudly instead of silently updating a golden.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  CPCursor,
  CURRENT_PLAYER,
  FixedString,
  FlingyType,
  SetTo,
  TechType,
  UnitType,
  UpgradeType,
  Var,
  WeaponType,
  buildModule,
  flingyDat,
  lazyFixedString,
  techDat,
  unitsDat,
  upgradesDat,
  weaponsDat,
  type DatFieldHandle,
} from "../index.ts";

function findMode6Reloc(mod: ReturnType<typeof buildModule>) {
  for (const trig of mod.triggers) {
    for (const act of trig.acts) {
      for (const reloc of act.relocs ?? []) {
        if ((reloc.mode as number) === 6) {
          return { trig, act, reloc };
        }
      }
    }
  }
  throw new Error("module has no mode-6 string-id relocation");
}

test("DAT tables load their schema metadata and entry counts", () => {
  assert.equal(unitsDat.table, "TrgUnit");
  assert.equal(unitsDat.kind, "soa");
  assert.equal(unitsDat.count, 228);
  assert.equal(weaponsDat.table, "Weapon");
  assert.equal(weaponsDat.count, 130);
  assert.equal(flingyDat.table, "Flingy");
  assert.equal(flingyDat.count, 209);
  assert.equal(techDat.table, "Tech");
  assert.equal(techDat.count, 44);
  assert.equal(upgradesDat.table, "Upgrade");
  assert.equal(upgradesDat.count, 61);
});

test("units.dat field addresses fold into EPD plus byte lane", () => {
  const marine = unitsDat[UnitType.TerranMarine];

  const maxHp = marine.maxHp;
  assert.equal(maxHp.address, 6_693_712);
  assert.equal(maxHp.epd, 221_179);
  assert.equal(maxHp.subp, 0);
  assert.equal(maxHp.size, 4);
  assert.equal(maxHp.bitMask, 0xffffffff);
  const setHp = maxHp.act_set(40);
  assert.equal(setHp.acttype, 45);
  assert.equal(setHp.modifier, SetTo);
  assert.equal(setHp.dest, 221_179);
  assert.equal(setHp.value, 40);
  assert.equal(setHp.mask, 0xffffffff);
  assert.equal(maxHp.set(40), undefined);

  // Byte-wide armor: entry 1 shares entry 0's EPD at byte lane 1.
  const armor = unitsDat.at(1).armor;
  assert.equal(armor.address, 6_684_361);
  assert.equal(armor.epd, 218_841);
  assert.equal(armor.subp, 1);
  assert.equal(armor.size, 1);
  assert.equal(armor.bitMask, 0xff00);
  const setArmor = armor.act_set(1);
  assert.equal(setArmor.dest, 218_841);
  assert.equal(setArmor.value, 0x100);
  assert.equal(setArmor.mask, 0xff00);

  // Word-wide shield: odd entry lands in the high half of the dword.
  const shield = unitsDat.at(1).maxShield;
  assert.equal(shield.address, 6_688_258);
  assert.equal(shield.epd, 219_815);
  assert.equal(shield.subp, 2);
  assert.equal(shield.shift, 16);
  assert.equal(shield.bitMask, 0xffff0000);
  const setShield = shield.act_set(0x1234);
  assert.equal(setShield.dest, 219_815);
  assert.equal(setShield.value, 0x12340000);
  assert.equal(setShield.mask, 0xffff0000);
});

test("weapon, flingy, tech, and upgrade addresses use their own stride", () => {
  const damage = weaponsDat[WeaponType.GaussRifle].damage;
  assert.equal(damage.address, 6_647_472);
  assert.equal(damage.epd, 209_619);
  assert.equal(damage.subp, 0);
  assert.equal(damage.size, 2);
  assert.equal(damage.act_set(6).value, 6);

  const speed = flingyDat[FlingyType.Scourge].topSpeed;
  assert.equal(speed.address, 7_118_584);
  assert.equal(speed.epd, 327_397);
  assert.equal(speed.act_set(0x1234).value, 0x1234);

  const stim = techDat[TechType.StimPacks];
  assert.equal(stim.mineralCost.address, 6_644_296);
  assert.equal(stim.mineralCost.epd, 208_825);
  assert.equal(stim.mineralCost.act_set(100).value, 100);
  // Entry 1 is a halfword later: same EPD, high lane.
  assert.equal(techDat.at(1).mineralCost.subp, 2);
  assert.equal(techDat.at(1).mineralCost.epd, 208_825);

  const armorUpgrade = upgradesDat[UpgradeType.TerranInfantryArmor];
  assert.equal(armorUpgrade.mineralCostBase.address, 6_641_472);
  assert.equal(armorUpgrade.mineralCostBase.epd, 208_119);
  assert.equal(upgradesDat.at(1).mineralCostBase.address, 6_641_474);
  assert.equal(upgradesDat.at(1).mineralCostBase.subp, 2);
});

test("schema aliases, snake_case, and canonical names resolve to one handle", () => {
  assert.equal(
    flingyDat[FlingyType.Scourge].turnSpeed.address,
    flingyDat[FlingyType.Scourge].turnRadius.address,
  );
  assert.equal(
    flingyDat[FlingyType.Scourge].turn_speed.address,
    flingyDat[FlingyType.Scourge].turn_radius.address,
  );
  assert.equal(
    weaponsDat[WeaponType.GaussRifle].verticalOffset.address,
    weaponsDat[WeaponType.GaussRifle].graphicYOffset.address,
  );
  assert.equal(
    weaponsDat[WeaponType.GaussRifle].graphicXOffset.address,
    weaponsDat[WeaponType.GaussRifle].forwardOffset.address,
  );

  const marine = unitsDat[UnitType.TerranMarine];
  assert.equal(marine.max_hp.address, marine.maxHp.address);
  assert.equal(marine.field("max_hp").address, marine.maxHp.address);
  assert.equal(marine.field("MAX_HP").address, marine.maxHp.address);

  // Compile-time surface: canonical and snake handles are DatFieldHandles.
  const canonical: DatFieldHandle = marine.maxHp;
  const snake: DatFieldHandle = marine.max_hp;
  assert.equal(canonical.address, snake.address);
});

test("entry lookup by schema name and reverse name lookup", () => {
  assert.equal(unitsDat.byName("Terran Marine").index, 0);
  assert.equal(unitsDat.byName("Terran Marine").maxHp.address, 6_693_712);
  assert.equal(unitsDat.nameOf(0), "Terran Marine");
  // Duplicate display names keep the first schema spelling.
  assert.equal(unitsDat.nameOf(6), "Siege Tank Turret (Tank Mode)");
  assert.equal(unitsDat.nameOf(999), undefined);
  assert.equal(unitsDat[UnitType.TerranMarine].table.table, "TrgUnit");
});

test("entry symbols are identifier-safe and unambiguous", () => {
  assert.equal(UnitType.TerranMarine, 0);
  assert.equal(UnitType.Marine, 0);
  assert.equal(WeaponType.GaussRifle, 0);
  assert.equal(FlingyType.Scourge, 0);
  assert.equal(TechType.StimPacks, 0);
  assert.equal(UpgradeType.TerranInfantryArmor, 0);

  // Exact names win; race-prefixed aliases only appear when unambiguous.
  assert.equal(UnitType.DarkTemplar, 74);
  assert.equal(UnitType.ProtossDarkTemplar, 61);
  assert.equal(Object.hasOwn(UnitType, "Marker"), false);
  assert.equal(Object.hasOwn(UnitType, "Beacon"), false);
  // `Lurker Egg` is an exact name, so it survives even though the
  // `Zerg Lurker Egg` stripped alias is ambiguous.
  assert.equal(UnitType.LurkerEgg, 97);

  // Runtime `symbols` agrees with the checked-in generated constants.
  assert.deepEqual({ ...unitsDat.symbols }, { ...UnitType });
  assert.deepEqual({ ...weaponsDat.symbols }, { ...WeaponType });
  assert.deepEqual({ ...flingyDat.symbols }, { ...FlingyType });
  assert.deepEqual({ ...techDat.symbols }, { ...TechType });
  assert.deepEqual({ ...upgradesDat.symbols }, { ...UpgradeType });
});

test("out-of-range, dynamic, and unknown DAT lookups fail with DSL errors", () => {
  assert.throws(() => unitsDat.at(228), /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat.at(-1), /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat[228], /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat.at(1.5), /EUDSL-TARGET/);
  assert.throws(() => unitsDat.at(Number.NaN), /EUDSL-TARGET/);
  assert.throws(() => unitsDat.at("0" as never), /EUDSL-TARGET/);
  assert.throws(() => unitsDat.byName("Not A Unit"), /EUDSL-BOUNDS/);
  // Plain-object prototype keys are not valid display names.
  assert.throws(() => unitsDat.byName("toString"), /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat.byName("constructor"), /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat.byName("__proto__"), /EUDSL-BOUNDS/);
  assert.throws(() => unitsDat[0].field("notAField"), /EUDSL-BOUNDS/);
  assert.throws(() => (unitsDat[0] as any).notAField, /EUDSL-BOUNDS/);
  assert.throws(() => (unitsDat as any).bogusProperty, /EUDSL-BOUNDS/);

  buildModule(() => {
    const dynamic = Var(0);
    assert.throws(() => unitsDat.at(dynamic as never), /EUDSL-TARGET/);
  });
});

test("nameString setString emits a lane-masked wire-id relocation", () => {
  let index = -1;
  const mod = buildModule(() => {
    const fs = new FixedString(8);
    index = fs.messageIndex;
    unitsDat[UnitType.Marine].nameString.setString(fs);
  });
  const { trig, act, reloc } = findMode6Reloc(mod);

  assert.deepEqual(trig.conds, []);
  assert.equal(trig.acts.length, 1);
  assert.equal(trig.preserved, true);
  assert.equal(act.dest, 219_071);
  assert.equal(act.value, 0);
  assert.equal(act.modifier, SetTo);
  assert.equal(act.acttype, 45);
  assert.equal(act.mask, 0x0000ffff);
  assert.deepEqual(reloc, {
    field_offset: 20,
    target_string: index,
    target_offset: 0,
    mode: 6,
  });
});

test("nameString odd entry targets the high lane and keeps its neighbour", () => {
  let index = -1;
  let address = -1;
  let epd = -1;
  const mod = buildModule(() => {
    const fs = new FixedString(8);
    index = fs.messageIndex;
    const handle = unitsDat[UnitType.ZergZergling].nameString;
    address = handle.address;
    epd = handle.epd as number;
    handle.setString(fs);
  });
  const { act, reloc } = findMode6Reloc(mod);

  assert.equal(address, 0x6602aa);
  assert.equal(epd, 219_089);
  assert.equal(act.dest, 219_089);
  // Only the high lane is in the mask, so the even neighbour is untouched.
  assert.equal(act.mask, 0xffff0000);
  assert.equal(act.mask & 0x0000ffff, 0);
  assert.equal(reloc.target_offset, 16);
  assert.equal(reloc.target_string, index);
});

test("nameString setString accepts a lazy FixedString", () => {
  const lazy = lazyFixedString(8);
  let index = -1;
  const mod = buildModule(() => {
    unitsDat[UnitType.Marine].nameString.setString(lazy);
    index = lazy.messageIndex;
  });
  const { reloc } = findMode6Reloc(mod);

  assert.equal(reloc.target_string, index);
  assert.equal(reloc.target_offset, 0);
});

test("nameString setString rejects non-string operands", () => {
  for (const bad of [new Uint8Array([0]), 5, true, null]) {
    assert.throws(
      () => unitsDat[UnitType.Marine].nameString.setString(bad as never),
      /EUDSL-TYPE/,
    );
  }
});

test("setString requires a 16-bit field", () => {
  buildModule(() => {
    const fs = new FixedString(8);
    assert.throws(
      () => unitsDat[UnitType.Marine].maxHp.setString(fs),
      /EUDSL-BOUNDS/,
    );
  });
});

test("set_string is a parity alias for setString", () => {
  const handle = unitsDat[UnitType.Marine].nameString;
  assert.equal(handle.set_string, handle.setString);

  const snake = buildModule(() => {
    const fs = new FixedString(8);
    unitsDat[UnitType.Marine].nameString.set_string(fs);
  }).toJson();
  const camel = buildModule(() => {
    const fs = new FixedString(8);
    unitsDat[UnitType.Marine].nameString.setString(fs);
  }).toJson();
  assert.equal(snake, camel);
});

test("setString follows a cursor-bound handle target", () => {
  const mod = buildModule(() => {
    const fs = new FixedString(8);
    const handle = unitsDat[UnitType.Marine].nameString;
    // Exercise the protected `prepareTarget()` seam: a CP-bound base must
    // resolve to the live CurrentPlayer dword, not the build-time EPD.
    Object.defineProperty(handle, "baseEpd", { value: new CPCursor(1000) });
    handle.setString(fs);
  });
  const { act, reloc } = findMode6Reloc(mod);

  assert.equal(act.dest, CURRENT_PLAYER);
  assert.equal(reloc.mode, 6);
  assert.equal(act.mask, 0x0000ffff);
});

test("rank is still a plain byte write", () => {
  const mod = buildModule(() => {
    unitsDat[UnitType.Marine].rank.set(2);
  });
  const acts = mod.triggers.flatMap((trig) => trig.acts);
  assert.ok(
    acts.some(
      (act) =>
        act.dest === 222_875 && act.value === 2 && act.mask === 0x000000ff,
    ),
    JSON.stringify(acts),
  );
});
