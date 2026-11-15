"""01. Direct Fixed-Address & EPD Reads, Writes, Conditions (eudkit Python).

eudplib -> eudkit 마이그레이션 / Migration notes:
- eudplib:
    * Memory(addr, AtLeast, 100) / SetMemory(addr, Add, 50)
    * f_dwread_epd(EPD(addr)) 는 내부에서 CurrentPlayer(0x6509B0) 를 사용하므로
      DisplayText 전후로 f_getcurpl() / f_setcurpl() 보존이 필요.
- eudkit:
    * MemCond(EPD(addr), ">=", 100) / Memory(addr, AtLeast, 100) / MemFieldHandle >=
    * MemView / MemFieldHandle (zero-storage, CP 비파괴 bit-test read)
    * once([...], body) 로 1회성 보너스 가드 + SetMemory(addr, Add, 50) 직접 쓰기
    * MemFieldHandle(...).read() 는 CurrentPlayer(0x6509B0) 를 클로버하지 않으므로
      FixedString / DisplayText 직전에 CP 를 수동 복원할 필요가 없음.

Note: External packages (`pip install -e ./py`) import from `eudkit`
(`from eudkit import ...`). Inside this repo, examples import from `py`.
"""

from py import (
    Add,
    AtLeast,
    DisplayText,
    EPD,
    FieldSpec,
    FixedString,
    GAME_TICK_ADDR,
    MemCond,
    MemFieldHandle,
    Memory,
    SetMemory,
    Var,
    build_module,
    defineMemView,
    once,
    when,
)

P1_MINERALS_ADDR = 0x57F0F0


def prog() -> None:
    u32_field = FieldSpec(0, 4)
    u32_schema = defineMemView("U32", 4, {"val": u32_field})
    minerals_field = MemFieldHandle(u32_schema, u32_field, EPD(P1_MINERALS_ADDR), "val")
    tick_field = MemFieldHandle(u32_schema, u32_field, EPD(GAME_TICK_ADDR), "val")

    bonus_applied = Var(0)
    live_minerals = Var(0)
    live_tick = Var(0)
    status = FixedString(64)

    # 1C engine-direct gate + once(): add +50 bonus minerals once when minerals >= 100 and flag == 0
    once(
        [MemCond(EPD(P1_MINERALS_ADDR), ">=", 100), bonus_applied.eq(0)],
        lambda: (
            SetMemory(P1_MINERALS_ADDR, Add, 50),
            bonus_applied.set(1),
            DisplayText("BONUS +50 MINERALS APPLIED"),
        ),
    )

    # CP-safe MemFieldHandle.read(): uses engine-direct DeathsX bit tests (no 0x6509B0 clobber)
    live_minerals.set(minerals_field.read())
    live_tick.set(tick_field.read())

    status.clear()
    # Both Memory(addr, AtLeast, 100) and MemFieldHandle >= 150 compile to 1C engine-direct tests
    when(
        [Memory(P1_MINERALS_ADDR, AtLeast, 100), minerals_field >= 150],
        lambda: status.write(
            "READY MIN={:\x1a>5d} TICK={:\x1a>6d} BONUS={:\x1a>1d}",
            live_minerals,
            live_tick,
            bonus_applied,
        ),
        lambda: status.write(
            "WAIT  MIN={:\x1a>5d} TICK={:\x1a>6d} BONUS={:\x1a>1d}",
            live_minerals,
            live_tick,
            bonus_applied,
        ),
    )
    status.display()


module = build_module(prog)
