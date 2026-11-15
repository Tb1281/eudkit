"""Smoke map 5: `Stack`, `Queue`, `FixedArray`, `TriggerStruct`, `isqrt`, & live `MemFieldHandle`.

Exercises data structures, integer square root, TriggerStruct fields,
DAT reads, and live game tick / P1 mineral memory reads in StarCraft: Remastered.
"""

from py import (
    EPD,
    FieldSpec,
    FixedArray,
    FixedString,
    GAME_TICK_ADDR,
    MemFieldHandle,
    Queue,
    Stack,
    UnitType,
    Var,
    arrayDword,
    build_module,
    byte,
    defineMemView,
    defineTriggerStruct,
    dword,
    every,
    isqrt,
    once,
    unitsDat,
    when,
    word,
)

P1_MINERALS_ADDR = 0x57F0F0


def prog() -> None:
    u32_field = FieldSpec(0, 4)
    u32_schema = defineMemView("U32", 4, {"val": u32_field})
    tick_field = MemFieldHandle(u32_schema, u32_field, EPD(GAME_TICK_ADDR), "val")
    min_field = MemFieldHandle(u32_schema, u32_field, EPD(P1_MINERALS_ADDR), "val")

    HeroStruct = defineTriggerStruct({"hp": dword(), "team": byte(), "tier": word()})
    hero = HeroStruct()

    stack = Stack(8)
    queue = Queue(arrayDword, 8)
    arr = FixedArray(arrayDword, 6)

    stack_score = Var(0)
    queue_score = Var(0)
    struct_arr_score = Var(0)
    sqrt_sum = Var(0)
    idx = Var(2)

    q_phase = Var(0)
    q1 = Var(0)
    q2 = Var(0)
    q3 = Var(0)

    def init_checks() -> None:
        # Stack LIFO: push 10, 20, 30 -> pop 30, 20, 10 => 30*1 + 20*2 + 10*3 = 100
        stack.push(10)
        stack.push(20)
        stack.push(30)
        s1 = stack.pop()
        s2 = stack.pop()
        s3 = stack.pop()
        stack_score.set(s1.mul(1) + s2.mul(2) + s3.mul(3))

        # TriggerStruct + FixedArray dynamic get/set
        hero.hp.set(500)
        hero.hp.iadd(25)
        hero.team.set(3)
        hero.tier.set(7)
        arr.set(idx, 42)
        got = arr.get(idx)
        struct_arr_score.set(hero.hp + hero.team + hero.tier + got)  # 525 + 3 + 7 + 42 = 577

        # isqrt
        r1 = isqrt(Var(1000000))  # 1000
        r2 = isqrt(Var(65535))  # 255
        sqrt_sum.set(r1 + r2)  # 1255

        # Enqueue 10, 20, 30; Queue refreshes outBox between trigger loops
        queue.push(10)
        queue.push(20)
        queue.push(30)
        q_phase.set(1)

    # Pop 1 item per frame so Queue's top-of-loop reader updates outBox between pops
    # (descending order prevents cascading multiple phases within the same frame;
    # placing these before once(init_checks) ensures frame 1 runs only push/init).
    when([q_phase.eq(4)], lambda: (queue_score.set(q1.mul(1) + q2.mul(2) + q3.mul(3)), q_phase.set(5)))
    when([q_phase.eq(3)], lambda: (q3.set(queue.pop()), q_phase.set(4)))
    when([q_phase.eq(2)], lambda: (q2.set(queue.pop()), q_phase.set(3)))
    when([q_phase.eq(1)], lambda: (q1.set(queue.pop()), q_phase.set(2)))

    once(init_checks)

    marine_hp = unitsDat[UnitType.TerranMarine].maxHp.read()

    line1 = FixedString(64)
    line2 = FixedString(64)
    line3 = FixedString(64)
    verdict = FixedString(64)

    def render_hud() -> None:
        tick = tick_field.read()
        minerals = min_field.read()

        line1.clear()
        line1.write(
            "STACK={:\x1a>3d}/100 QUEUE={:\x1a>3d}/140 STRUCT+ARR={:\x1a>3d}/577",
            stack_score,
            queue_score,
            struct_arr_score,
        )
        line1.displayAt(1)

        line2.clear()
        line2.write("ISQRT={:\x1a>4d}/1255 MARINE_HP_HEX={:x}", sqrt_sum, marine_hp)
        line2.displayAt(2)

        line3.clear()
        line3.write("LIVE TICK={:\x1a>6d} P1_MINERALS={:\x1a>5d}", tick, minerals)
        line3.displayAt(3)

        verdict.clear()
        when(
            [
                stack_score.eq(100),
                queue_score.eq(140),
                struct_arr_score.eq(577),
                sqrt_sum.eq(1255),
            ],
            lambda: verdict.write("STATUS: PASS"),
            lambda: verdict.write("STATUS: FAIL"),
        )
        verdict.displayAt(4)

    every(6, render_hud)


module = build_module(prog)
