"""DAT accessors: schema loading, address math, symbols, and errors.

Expected addresses are written here independently of the schema so a schema
edit that moves a table fails loudly instead of silently updating a golden.
"""

from __future__ import annotations

import unittest

from py import (
    FlingyType,
    TechType,
    UnitType,
    UpgradeType,
    Var,
    WeaponType,
    build_module,
    flingy_dat,
    tech_dat,
    units_dat,
    upgrades_dat,
    weapons_dat,
)
from py.dat import (
    DatEntry,
    DatFieldHandle,
    dat_symbol_identifier,
    dat_symbols_for,
)
from py.view import EudslError, EPD


class DatTablesTests(unittest.TestCase):
    def test_tables_load_their_schema_metadata_and_entry_counts(self) -> None:
        self.assertEqual(units_dat.table, "TrgUnit")
        self.assertEqual(units_dat.kind, "soa")
        self.assertEqual(units_dat.count, 228)
        self.assertEqual(weapons_dat.table, "Weapon")
        self.assertEqual(weapons_dat.count, 130)
        self.assertEqual(flingy_dat.table, "Flingy")
        self.assertEqual(flingy_dat.count, 209)
        self.assertEqual(tech_dat.table, "Tech")
        self.assertEqual(tech_dat.count, 44)
        self.assertEqual(upgrades_dat.table, "Upgrade")
        self.assertEqual(upgrades_dat.count, 61)

    def test_units_field_addresses_fold_into_epd_plus_byte_lane(self) -> None:
        marine = units_dat[UnitType.TerranMarine]

        max_hp = marine.maxHp
        self.assertEqual(max_hp.address, 6_693_712)
        self.assertEqual(max_hp.epd, 221_179)
        self.assertEqual(max_hp.subp, 0)
        self.assertEqual(max_hp.size, 4)
        self.assertEqual(max_hp.bitMask, 0xFFFFFFFF)
        set_hp = max_hp.act_set(40)
        self.assertEqual(set_hp["acttype"], 45)
        self.assertEqual(set_hp["modifier"], 7)  # SetTo
        self.assertEqual(set_hp["dest"], 221_179)
        self.assertEqual(set_hp["value"], 40)
        self.assertEqual(set_hp["mask"], 0xFFFFFFFF)
        self.assertIsNone(max_hp.set(40))

        armor = units_dat.at(1).armor
        self.assertEqual(armor.address, 6_684_361)
        self.assertEqual(armor.epd, 218_841)
        self.assertEqual(armor.subp, 1)
        self.assertEqual(armor.size, 1)
        self.assertEqual(armor.bitMask, 0xFF00)
        set_armor = armor.act_set(1)
        self.assertEqual(set_armor["dest"], 218_841)
        self.assertEqual(set_armor["value"], 0x100)
        self.assertEqual(set_armor["mask"], 0xFF00)

        shield = units_dat.at(1).maxShield
        self.assertEqual(shield.address, 6_688_258)
        self.assertEqual(shield.epd, 219_815)
        self.assertEqual(shield.subp, 2)
        self.assertEqual(shield.shift, 16)
        self.assertEqual(shield.bitMask, 0xFFFF0000)
        set_shield = shield.act_set(0x1234)
        self.assertEqual(set_shield["dest"], 219_815)
        self.assertEqual(set_shield["value"], 0x12340000)
        self.assertEqual(set_shield["mask"], 0xFFFF0000)

    def test_other_tables_use_their_own_stride(self) -> None:
        damage = weapons_dat[WeaponType.GaussRifle].damage
        self.assertEqual(damage.address, 6_647_472)
        self.assertEqual(damage.epd, 209_619)
        self.assertEqual(damage.act_set(6)["value"], 6)

        speed = flingy_dat[FlingyType.Scourge].topSpeed
        self.assertEqual(speed.address, 7_118_584)
        self.assertEqual(speed.epd, 327_397)
        self.assertEqual(speed.act_set(0x1234)["value"], 0x1234)

        stim = tech_dat[TechType.StimPacks]
        self.assertEqual(stim.mineralCost.address, 6_644_296)
        self.assertEqual(stim.mineralCost.epd, 208_825)
        self.assertEqual(stim.mineralCost.act_set(100)["value"], 100)
        self.assertEqual(tech_dat.at(1).mineralCost.subp, 2)
        self.assertEqual(tech_dat.at(1).mineralCost.epd, 208_825)

        armor_upgrade = upgrades_dat[UpgradeType.TerranInfantryArmor]
        self.assertEqual(armor_upgrade.mineralCostBase.address, 6_641_472)
        self.assertEqual(armor_upgrade.mineralCostBase.epd, 208_119)
        self.assertEqual(upgrades_dat.at(1).mineralCostBase.address, 6_641_474)
        self.assertEqual(upgrades_dat.at(1).mineralCostBase.subp, 2)

    def test_aliases_snake_case_and_canonical_names_share_one_handle(self) -> None:
        scourge = flingy_dat[FlingyType.Scourge]
        self.assertEqual(scourge.turnSpeed.address, scourge.turnRadius.address)
        self.assertEqual(scourge.turn_speed.address, scourge.turn_radius.address)
        self.assertEqual(
            weapons_dat[WeaponType.GaussRifle].verticalOffset.address,
            weapons_dat[WeaponType.GaussRifle].graphicYOffset.address,
        )
        self.assertEqual(
            weapons_dat[WeaponType.GaussRifle].graphicXOffset.address,
            weapons_dat[WeaponType.GaussRifle].forwardOffset.address,
        )

        marine = units_dat[UnitType.TerranMarine]
        self.assertEqual(marine.max_hp.address, marine.maxHp.address)
        self.assertEqual(marine.field("max_hp").address, marine.maxHp.address)
        self.assertEqual(marine.field("MAX_HP").address, marine.maxHp.address)
        self.assertIsInstance(marine.maxHp, DatFieldHandle)
        self.assertIsInstance(marine, DatEntry)

    def test_assignment_routes_to_set_without_shadowing(self) -> None:
        entry = units_dat[UnitType.TerranMarine]
        entry.maxHp = 123
        # Assignment routes through DatFieldHandle.set; the property still
        # resolves to a handle rather than becoming an instance attribute.
        self.assertIsInstance(entry.maxHp, DatFieldHandle)
        self.assertEqual(entry.maxHp.address, 6_693_712)

    def test_lookup_by_schema_name_and_reverse_name_lookup(self) -> None:
        self.assertEqual(units_dat.by_name("Terran Marine").index, 0)
        self.assertEqual(units_dat.byName("Terran Marine").maxHp.address, 6_693_712)
        self.assertEqual(units_dat.name_of(0), "Terran Marine")
        self.assertEqual(units_dat.nameOf(6), "Siege Tank Turret (Tank Mode)")
        self.assertIsNone(units_dat.name_of(999))
        self.assertEqual(units_dat[UnitType.TerranMarine].table.table, "TrgUnit")

    def test_entry_symbols_are_identifier_safe_and_unambiguous(self) -> None:
        self.assertEqual(UnitType.TerranMarine, 0)
        self.assertEqual(UnitType.Marine, 0)
        self.assertEqual(WeaponType.GaussRifle, 0)
        self.assertEqual(FlingyType.Scourge, 0)
        self.assertEqual(TechType.StimPacks, 0)
        self.assertEqual(UpgradeType.TerranInfantryArmor, 0)

        self.assertEqual(UnitType.DarkTemplar, 74)
        self.assertEqual(UnitType.ProtossDarkTemplar, 61)
        self.assertNotIn("Marker", UnitType)
        self.assertNotIn("Beacon", UnitType)
        self.assertEqual(UnitType.LurkerEgg, 97)

        self.assertEqual(dict(units_dat.symbols), dict(UnitType))
        self.assertEqual(dict(weapons_dat.symbols), dict(WeaponType))
        self.assertEqual(dict(flingy_dat.symbols), dict(FlingyType))
        self.assertEqual(dict(tech_dat.symbols), dict(TechType))
        self.assertEqual(dict(upgrades_dat.symbols), dict(UpgradeType))

    def test_identifier_and_symbol_helpers(self) -> None:
        self.assertEqual(dat_symbol_identifier("Terran Marine"), "TerranMarine")
        self.assertEqual(dat_symbol_identifier("Tank Turret type   1"), "TankTurretType1")
        self.assertEqual(dat_symbol_identifier("1 bit"), "_1Bit")
        symbols = dat_symbols_for({"Terran Marine": 0, "Marine": 1})
        # The exact `Marine` name wins over the stripped alias.
        self.assertEqual(symbols["Marine"], 1)
        self.assertEqual(symbols["TerranMarine"], 0)

    def test_out_of_range_dynamic_and_unknown_lookups_fail(self) -> None:
        for bad_index in (228, -1):
            with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
                units_dat.at(bad_index)
            with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
                units_dat[bad_index]
        for bad_index in (1.5, True, "0", float("nan")):
            with self.assertRaisesRegex(EudslError, "EUDSL-TARGET"):
                units_dat.at(bad_index)
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            units_dat.by_name("Not A Unit")
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            units_dat[0].field("notAField")
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            getattr(units_dat[0], "notAField")
        with self.assertRaisesRegex(EudslError, "EUDSL-BOUNDS"):
            getattr(units_dat, "bogusProperty")

        def prog() -> None:
            dynamic = Var(0)
            with self.assertRaisesRegex(EudslError, "EUDSL-TARGET"):
                units_dat.at(dynamic)

        build_module(prog)

    def test_epd_matches_view_helper(self) -> None:
        self.assertEqual(units_dat[0].maxHp.epd, EPD(6_693_712))


if __name__ == "__main__":
    unittest.main()
