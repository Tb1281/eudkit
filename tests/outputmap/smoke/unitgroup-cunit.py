"""Smoke map 3: interactive `UnitGroup(compact=True, autoPrune=True)` & CUnit tracking.

Spawns 4 P1 Terran Marines (at t=1..4s), captures their runtime EPDs from
`firstPlayerUnit[P1]` (`0x6283F8`), registers them into a compact `UnitGroup`,
counts visits in `forEach`, and displays live `group.length` + `visits` on HUD.
Attacking/killing Marines in-game auto-prunes them (`4 -> 3 -> 2 -> 1 -> 0`).
"""

from py import (
    Anywhere,
    AtLeast,
    CreateUnit,
    EPD,
    EPD_BASE,
    ElapsedTime,
    FieldSpec,
    FixedString,
    MemFieldHandle,
    P1,
    UnitGroup,
    UnitType,
    Var,
    build_module,
    defineMemView,
    every,
    once,
    when,
)

FIRST_P1_UNIT_ADDR = 0x6283F8


def prog() -> None:
    u32_field = FieldSpec(0, 4)
    u32_schema = defineMemView("U32", 4, {"val": u32_field})
    p1_head_field = MemFieldHandle(u32_schema, u32_field, EPD(FIRST_P1_UNIT_ADDR), "val")

    group = UnitGroup(6, compact=True, autoPrune=True)
    last_head = Var(0)
    last_epd = Var(0)
    spawned = Var(0)
    live_visits = Var(0)

    def spawn_one() -> None:
        CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)
        spawned.iadd(1)

    for sec in (1, 2, 3, 4):
        once(ElapsedTime(AtLeast, sec), spawn_one)

    def poll_new_unit() -> None:
        head = p1_head_field.read()

        def register() -> None:
            last_head.set(head)
            last_epd.set((head - EPD_BASE).shr(2))
            group.add(last_epd)

        when([head.ne(0), head.ne(last_head)], register)

        live_visits.set(0)
        group.for_each(lambda _u: live_visits.iadd(1))

    every(6, poll_new_unit)

    line1 = FixedString(64)
    line2 = FixedString(64)
    line3 = FixedString(64)
    verdict = FixedString(64)

    def render_hud() -> None:
        line1.clear()
        line1.write("UNITGROUP COMPACT LEN={:\x1a>1d} VISITS={:\x1a>1d}", group.length, live_visits)
        line1.displayAt(1)

        line2.clear()
        line2.write("SPAWNED={:\x1a>1d}/4 LAST_EPD={:\x1a>5d}", spawned, last_epd)
        line2.displayAt(2)

        line3.clear()
        line3.write("Kill own Marine (A-click) -> autoPrune drops LEN & VISITS")
        line3.displayAt(3)

        verdict.clear()
        when(
            [group.length.eq(live_visits)],
            lambda: verdict.write("STATUS: PASS (LEN == VISITS)"),
            lambda: verdict.write("STATUS: MISMATCH"),
        )
        verdict.displayAt(4)

    every(6, render_hud)


module = build_module(prog)
