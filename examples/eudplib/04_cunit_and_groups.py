"""eudplib baseline: CUnit / CSprite Views & UnitGroup Iteration.

Shared Intent:
    Wrap a CUnit EPD, inspect hp / unitType / posX / sprite, track up to 6
    unit EPDs in a UnitGroup with auto-prune on dead units, and count living
    visits in the group loop.

eudplib Pattern Notes:
    - ``CUnit(EPD(0x59CCA8))`` wraps CUnit slot 0 as an ``EPDOffsetMap``.
    - ``unit.hp`` / ``unit.posX`` read via ``f_dwread_epd`` / ``f_wread_epd``;
      ``unit.sprite`` reads the CSprite pointer and returns a ``CSprite``.
    - ``unit.eqattr("unitType", "Terran Marine")`` emits ``MemoryXEPD`` for
      in-place condition checks without materialising a variable.
    - ``UnitGroup(6)`` uses a VArray + CP-trick jump table. Every
      ``for unit in group.cploop:`` MUST contain a ``for dead in unit.dying:``
      block (eudplib asserts ``pos.IsSet()`` and auto-removes dead units at
      the end of the ``dying`` block).
"""

from eudplib import *

FIRST_UNIT_EPD = EPD(0x59CCA8)
SECOND_UNIT_EPD = EPD(0x59CCA8 + 336)
FIRST_SPRITE_EPD = EPD(0x629D98)

group = UnitGroup(6)
inspected_hp = EUDVariable(0)
inspected_pos_x = EUDVariable(0)
sprite_pos_x = EUDVariable(0)
marine_ok = EUDVariable(0)
living_visits = EUDVariable(0)


def onPluginStart():
    unit = CUnit(FIRST_UNIT_EPD)
    dead_unit = CUnit(SECOND_UNIT_EPD)
    sprite = CSprite(FIRST_SPRITE_EPD)

    # Seed CUnit[0] (alive) and CUnit[1] (dead: order == 0, sprite == 0x629D98 -> removed)
    unit.hp = 40 * 256
    unit.unitType = "Terran Marine"
    unit.posX = 320
    unit.order = 1  # non-zero order so unit.dying treats CUnit[0] as alive
    unit.sprite = sprite
    sprite.posX = 320

    # CUnit[1] is marked dying (order == 0, sprite != 0) so unit.dying prunes it.
    # Note: the eudkit mirrors mark death differently (sprite == 0); the two
    # libraries detect a dead unit by different fields.
    dead_unit.order = 0
    dead_unit.sprite = sprite

    group.add(FIRST_UNIT_EPD)
    group.add(SECOND_UNIT_EPD)


def run():
    unit = CUnit(FIRST_UNIT_EPD)
    sprite = CSprite(FIRST_SPRITE_EPD)

    # Read CUnit & CSprite properties into EUDVariables
    inspected_hp << unit.hp
    inspected_pos_x << unit.posX
    unit_sprite = unit.sprite
    sprite_pos_x << sprite.posX

    if EUDIf()([
        unit.hp >= 20 * 256,
        unit.eqattr("unitType", "Terran Marine"),
        unit.posX >= 100,
        unit_sprite.posX == 320,
    ]):
        marine_ok << 1
    EUDEndIf()

    # UnitGroup CP-loop: handle dying units first, then living units
    living_visits << 0
    for u in group.cploop:
        for dead in u.dying:
            # Auto-removed at the end of the dying block
            dead.move_cp(0x4C // 4)
        # Alive body: CP is restored to the unit slot; move_cp navigates fields
        u.move_cp(0x08 // 4)
        living_visits += 1


def beforeTriggerExec():
    run()
