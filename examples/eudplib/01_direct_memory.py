"""01. Direct Fixed-Address & EPD Reads, Writes, Conditions (eudplib baseline).

eudplib 기준 패턴 (Baseline):
- 고정 주소 조건/쓰기:
    `Memory(0x57F0F0, AtLeast, 100)`, `SetMemory(0x57F0F0, Add, 50)` 으로
    엔진 메모리(P1 미네랄)에 1C/1A 로 직접 접근.
- 동적 읽기 (`f_dwread_epd`):
    `f_dwread_epd(EPD(0x57F0F0))`, `f_dwread_epd(EPD(0x57F23C))` 는 내부적으로
    `CurrentPlayer(0x6509B0)` 를 이동해 비트를 스캔한 뒤 `cpcache` 로 복원한다.
    따라서 이전에 CP 를 변경했거나 `DisplayText` 대상을 P1 로 보장하려면
    `f_getcurpl()` / `f_setcurpl()` 로 CP 를 직접 보존/복구해야 한다.
- 버퍼 저장 및 출력 (`Db`, `f_dwwrite_epd`, `DisplayText`):
    `f_dwwrite_epd(EPD(snapshot_buf), val)` 로 `Db(...)` 버퍼에 읽은 값을 저장하고,
    `DisplayText(...)` 로 상태를 출력한다. (참고: `DisplayText` 는 맵 STR
    인덱스나 문자열 리터럴을 받으므로 raw `Db` 포인터는 직접 전달할 수 없고,
    동적 출력 시 `StringBuffer` 나 `f_dbstr_print` + STR 슬롯 연결이 필요하다.)
"""

from eudplib import *

P1_MINERALS_ADDR = 0x57F0F0
GAME_TICK_ADDR = 0x57F23C

# 1회성 보너스 지급 여부 플래그 & 라이브 스냅샷 저장용 Db 버퍼 (minerals, tick 각 4B)
bonus_applied = EUDVariable(0)
snapshot_buf = Db(8)


def run() -> None:
    # 1. 고정 주소 1C 조건 + EUDVariable 플래그: 미네랄 >= 100 이고 플래그 == 0 이면 1회 +50 지급
    if EUDIf()([Memory(P1_MINERALS_ADDR, AtLeast, 100), bonus_applied.Exactly(0)]):
        DoActions(
            SetMemory(P1_MINERALS_ADDR, Add, 50),
            bonus_applied.SetNumber(1),
            DisplayText("BONUS +50 MINERALS APPLIED"),
        )
    EUDEndIf()

    # 2. CurrentPlayer(0x6509B0) 보존 후 f_dwread_epd 로 미네랄 및 게임 틱 읽기
    saved_cp = f_getcurpl()
    live_minerals = f_dwread_epd(EPD(P1_MINERALS_ADDR))
    live_tick = f_dwread_epd(EPD(GAME_TICK_ADDR))

    # 3. 읽은 값을 Db 버퍼에 저장 (f_dwwrite_epd)
    f_dwwrite_epd(EPD(snapshot_buf), live_minerals)
    f_dwwrite_epd(EPD(snapshot_buf) + 1, live_tick)

    # 4. DisplayText 전 CurrentPlayer 복원 (미네랄 상태 출력)
    f_setcurpl(saved_cp)
    if EUDIf()(Memory(P1_MINERALS_ADDR, AtLeast, 100)):
        DoActions(DisplayText("STATUS: MINERALS READY"))
    if EUDElse()():
        DoActions(DisplayText("STATUS: WAITING FOR MINERALS"))
    EUDEndIf()


def onPluginStart() -> None:
    # 맵 시작 시 1회 초기화가 필요하면 여기에서 수행 (예: 스냅샷 버퍼 0 초기화)
    f_dwwrite_epd(EPD(snapshot_buf), 0)
    f_dwwrite_epd(EPD(snapshot_buf) + 1, 0)


def beforeTriggerExec() -> None:
    run()
