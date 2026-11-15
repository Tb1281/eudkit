"""03_cp_cursor (eudplib baseline): CurrentPlayer (0x6509B0) 커서 접근 및 CP 복구 안전성.

eudplib에서 CurrentPlayer(0x6509B0, CP)를 이용한 CUnit 슬롯 0(0x59CCA8) 다중 필드
접근과 DisplayText 호출 전 CP 복구 패턴입니다.

핵심 동작:
- f_setcurpl(EPD(0x59CCA8)): 0x6509B0과 내부 cpcache를 모두 해당 EPD로 설정한다.
- f_dwread_cp(cpo) / f_wread_cp(cpo, subp) / f_bwrite_cp(cpo, subp, b):
  실행 전후에 SetMemory(0x6509B0, Add, cpo) / Add(-cpo)로 오프셋을 이동했다가 복귀한다.
- CP 복구 필수: DisplayText 등 플레이어 타깃 액션은 현재 0x6509B0 값을 대상으로
  삼으므로, CP를 P1(0) 또는 원래 플레이어로 되돌리지 않으면 잘못된 플레이어 번호로
  출력된다.
- 주의: f_setcurpl(EPD(...))는 cpcache도 덮어쓰므로, P1(0)으로 돌아가려면 f_setcurpl(0)을
  호출해야 한다. cpcache가 이미 플레이어 ID를 보관 중일 때(예: raw SetMemory로
  0x6509B0만 바꾼 뒤)에는 f_setcurpl2cpcache()로 cpcache 값을 복원한다.
- 무-CP 대안: 상수/변수 EPD를 직접 다루는 CUnit(EPD(0x59CCA8))은 0x6509B0을 건드리지
  않고 필드를 읽고 쓴다.
"""

from eudplib import *

CUNIT0_EPD = EPD(0x59CCA8)


def run():
    # 1. CP 커서로 CUnit 슬롯 0 다중 필드 접근
    #    - +0x08 (dword 2): hp (4바이트)
    #    - +0x64 (dword 25, subp 0): unitType (2바이트)
    #    - +0x4D (dword 19, subp 1): order (1바이트)
    f_setcurpl(CUNIT0_EPD)
    hp = f_dwread_cp(2)
    unit_type = f_wread_cp(0x64 // 4, 0)
    f_bwrite_cp(0x4D // 4, 1, 10)  # order = 10

    # 2. DisplayText 호출 전 반드시 CP를 P1(0)으로 재설정
    #    f_setcurpl(CUNIT0_EPD)가 cpcache도 CUNIT0_EPD로 갱신했으므로,
    #    여기서 f_setcurpl2cpcache()만 부르면 CUNIT0_EPD가 복원된다.
    #    따라서 명시적으로 f_setcurpl(0)을 호출해 0x6509B0과 cpcache를 0으로 맞춘다.
    f_setcurpl(0)
    DoActions(DisplayText("CUnit0 inspected via CP cursor"))

    # (참고) cpcache가 이미 P1(0)인 상태에서 raw SetMemory(0x6509B0, SetTo, ...)만
    # 임시로 썼다면, f_setcurpl2cpcache()로 cpcache의 플레이어 값을 즉시 복원할 수 있다.
    DoActions(SetMemory(0x6509B0, SetTo, CUNIT0_EPD))
    _hp_peek = f_dwread_cp(2)
    f_setcurpl2cpcache()
    DoActions(DisplayText("CP restored via f_setcurpl2cpcache()"))

    # 3. 무-CP(Zero-CP) 대안: CUnit(EPD(0x59CCA8)) 직접 접근
    #    0x6509B0을 전혀 변경하지 않고 각 필드 주소에 직접 SetMemoryX / MemoryX를 생성한다.
    unit0 = CUnit(CUNIT0_EPD)
    direct_hp = unit0.hp
    direct_type = unit0.unitType
    unit0.order = 0
    if EUDIf()(direct_hp >= 256):
        unit0.hp -= 256
    EUDEndIf()
    _ = (hp, unit_type, _hp_peek, direct_type)


def onPluginStart():
    run()
