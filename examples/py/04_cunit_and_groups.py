"""eudkit Python example: CUnit / CSprite Views & UnitGroup Iteration.

Shared Intent:
    Wrap a CUnit EPD, inspect hp / unitType / posX / sprite, track up to 6
    unit EPDs in a UnitGroup with auto-prune on dead units, and count living
    visits in the group loop.

Migration Notes (eudplib -> eudkit):
    [EN]
    1. CUnit(epd) / CSprite(epd) are zero-storage MemView proxies backed by
       ``schema/layouts/{cunit,csprite}.json``. Direct property reads
       (``unit.hp.read()``), comparisons (``unit.unitType.eq(...)``), and
       writes (``unit.hp.set(...)``) emit MemoryX/SetDeathsX directly.
       Write RHS is const-only (plain int); dynamic Var writes raise EUDSL-TYPE.
    2. ``UnitGroup(6, compact=True, autoPrune=True)`` replaces eudplib's
       ``group.cploop`` + ``for dead in unit.dying:`` boilerplate.
       ``autoPrune`` emits a per-frame sweep checking ``+0x0C`` (sprite);
       ``group.forEach(fn)`` emits the callback body once using a CP-sweep
       cursor that automatically saves and restores ``CurrentPlayer``
       (``0x6509B0``) and passes a single reused scratch ``Var`` (``unit_epd``).
       Copy ``unit_epd`` if needed; never retain/alias it across iterations.

    [KR]
    1. ``CUnit(epd)`` / ``CSprite(epd)``는 별도 저장 공간을 갖지 않는 MemView
       프록시입니다. ``unit.hp.read()``, ``unit.unitType.eq(...)``,
       ``unit.hp.set(...)`` 형태로 필드에 직접 접근하며, 쓰기 우변(RHS)은
       컴파일 타임 정수 상수만 허용됩니다(동적 ``Var`` 쓰기 시 ``EUDSL-TYPE``).
    2. ``UnitGroup(6, compact=True, autoPrune=True)``는 eudplib의
       ``cploop`` + ``unit.dying`` 패턴을 대체합니다. ``autoPrune``이 매 프레임
       스프라이트(``+0x0C``)가 0인 사망 유닛을 자동 제거하고, ``forEach``는
       CP-sweep으로 ``CurrentPlayer``(``0x6509B0``)를 자동 저장/복원하며
       재사용되는 단일 스크래치 ``Var``(``unit_epd``)를 콜백에 전달합니다.

Note:
    Inside this repo we import ``from py import ...``; external packages
    installed via ``pip install -e ./py`` import ``from eudkit import ...``.
"""

from py import (
    CSprite,
    CUnit,
    EPD,
    UnitGroup,
    UnitType,
    Var,
    build_module,
    when,
)

FIRST_UNIT_EPD = EPD(0x59CCA8)
SECOND_UNIT_EPD = EPD(0x59CCA8 + 336)
FIRST_SPRITE_EPD = EPD(0x629D98)


def prog() -> None:
    unit = CUnit(FIRST_UNIT_EPD)
    dead_unit = CUnit(SECOND_UNIT_EPD)
    sprite = CSprite(FIRST_SPRITE_EPD)

    # Compact UnitGroup emits autoPrune & forEach as single CP-sweep loops
    # (saves/restores CurrentPlayer 0x6509B0 automatically).
    group = UnitGroup(6, compact=True, autoPrune=True)

    phase = Var(0)
    inspected_hp = Var(0)
    inspected_pos_x = Var(0)
    inspected_sprite = Var(0)
    sprite_pos_x = Var(0)
    marine_ok = Var(0)
    living_visits = Var(0)
    last_visited_epd = Var(0)

    def seed_units() -> None:
        # Const-only write RHS on CUnit / CSprite properties
        unit.hp.set(40 * 256)
        unit.unitType.set(UnitType.TerranMarine)
        unit.posX.set(320)
        unit.sprite.set(0x629D98)
        sprite.posX.set(320)

        # Dead unit (sprite == 0): autoPrune clears this slot on the next tick
        dead_unit.sprite.set(0)

        group.add(FIRST_UNIT_EPD)
        group.add(SECOND_UNIT_EPD)

    when([phase.eq(0)], seed_units)

    # Direct CUnit & CSprite property reads
    inspected_hp.set(unit.hp.read())
    inspected_pos_x.set(unit.posX.read())
    inspected_sprite.set(unit.sprite.read())
    sprite_pos_x.set(sprite.posX.read())

    # Direct CUnit & CSprite property comparisons
    when(
        [
            unit.hp.ge(20 * 256),
            unit.unitType.eq(UnitType.TerranMarine),
            unit.posX.ge(100),
            unit.sprite.ne(0),
            sprite.posX.eq(320),
        ],
        lambda: marine_ok.set(1),
    )

    # Group iteration: `unit_epd` is one reused scratch Var per live slot
    living_visits.set(0)

    def visit(unit_epd: Var) -> None:
        living_visits.iadd(1)
        last_visited_epd.set(unit_epd)

    group.forEach(visit)

    phase.iadd(1)


module = build_module(prog)
