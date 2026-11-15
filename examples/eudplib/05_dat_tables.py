"""Example 05: StarCraft .dat Tables (units.dat / weapons.dat / techdata.dat / upgrades.dat)
eudplib baseline.

eudplib pattern:
- `TrgUnit("Terran Marine")`, `Weapon("Gauss Rifle")`, `Tech("Stim Packs")`, and
  `Upgrade("Terran Infantry Armor")` are `EPDOffsetMap` subclasses accepting either
  compile-time string/integer IDs or runtime `EUDVariable` IDs (via stride caching).
- Field assignments (`=`, `+=`) read/write directly to StarCraft memory; reading a
  field evaluates to an `EUDVariable` (or condition when compared with `>=`, `==`, etc.).
- For dynamic unit IDs on 4-byte-stride arrays such as `units.dat` `maxHp` (`0x662350`),
  you can either use `TrgUnit(unit_id).maxHp` (stride-cached) or index directly with
  `f_dwread_epd(EPD(0x662350) + unit_id)`.
"""

from eudplib import *

# Global variables storing read-back DAT values
marine_hp = EUDVariable(0)
armor_base_cost = EUDVariable(0)
dynamic_unit_hp = EUDVariable(0)


def onPluginStart():
    # Patch Marine maxHp (8.8 fixed point: 60 * 256 = 15360) and Gauss Rifle damage
    TrgUnit("Terran Marine").maxHp = 60 * 256
    Weapon("Gauss Rifle").damage = 8

    # Increment Stim Packs mineral cost by 50
    Tech("Stim Packs").mineralCost += 50

    # Conditional check against patched weapons.dat field, then read back Marine maxHp
    if EUDIf()(Weapon("Gauss Rifle").damage >= 8):
        marine_hp << TrgUnit("Terran Marine").maxHp
    EUDEndIf()

    # Read upgrades.dat field (mineralCostBase for Terran Infantry Armor)
    armor_base_cost << Upgrade("Terran Infantry Armor").mineralCostBase

    # Dynamic unit ID access:
    # 0x662350 is units.dat maxHp base (4-byte stride), so EPD(0x662350) + unit_id
    # directly addresses unit_id's dword entry at runtime.
    unit_id = EUDVariable(0)  # e.g., runtime-selected Terran Marine (0)
    dynamic_unit_hp << f_dwread_epd(EPD(0x662350) + unit_id)
