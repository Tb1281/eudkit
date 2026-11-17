"""Topic 07 (eudkit Python): Block Copy, Fill, and Payload Injection.

Migration notes (eudplib -> eudkit):
- ``f_repmovsd_epd(dst, src, dwords)`` -> ``memcpyEPD(dstEpd, srcEpd, dwords)``:
  eudkit 는 ``CurrentPlayer`` (`0x6509B0`) 를 `srcEpd` 로 옮겨 읽으며 종료 시
  ``CurrentPlayer == srcEpd + count - 1`` 로 남긴다. 또한 ``Db`` 블롭을 직접 소스로
  받아 mode-1 재배치로 복사할 수 있다 (``memcpyEPD(dstEpd, blob, dwords)``).
- ``EUDWhile`` + ``f_dwwrite_epd`` -> ``memsetEPD(dstEpd, value, count)``:
  상수 ``(dstEpd, value, count)`` 채우기는 ``ceil(count / 64) <= 512`` 청크 이하에서
  packed ``SetDeaths`` 트리거로 폴드되며(84 dwords -> 2 triggers, 루프 없음),
  런타임 변수 카운트(``Var``)는 check-first ``whileLoop`` 로 낮춰진다.
- euddraft ``_add_datadumper`` / ``dataDumper`` 플러그인 -> ``dataDumper(bytes, [addr], ["copy"])``:
  eudkit 은 플러그인 등록/실행 분리가 없으므로 ``dataDumper`` 한 번 호출로
  패딩된 ``Db`` 블롭을 할당하고 ``once`` 게이트 안에 ``"copy"`` 루틴을 방출한다.
  eudplib 의 ``f_dwpatch_epd`` / ``f_unpatchall`` 언패치 스택은 eudkit 에서 제거되었다.

Note on imports:
  In external packages installed via ``pip install -e ./py``, import from ``eudkit``:
  ``from eudkit import CUnit, Db, DisplayText, EPD, ...``

WARNING — scratch slot 1699 is NOT established safe scratch. This example
assumes the repo's 1699-slot CUnit model (``py/view.py: CUNIT_SLOT_COUNT =
1699``, slots 0..1698, pool end ``0x628297``), so slot 1699 looks like free
space past the pool. External sources contradict that model: BWAPI accepts
unit indices ``0 <= unitIndex < 1700``
(https://bwapi.github.io/class_b_w_a_p_i_1_1_game.html), and the euddraft wiki
states 1700 units (0th–1699th) span ``0x59CCA8``–``0x6283E7``
(https://github-wiki-see.page/m/phu54321/euddraft/wiki/05.-Selecting-a-pawn).
Under those sources slot 1699 (``0x628298``–``0x6283E7``) is the last VALID
CUnit, and the 84-dword copy below overwrites it when occupied. Do not copy
this scratch choice into a real map until the 1699-vs-1700 pool size is
settled at the code level; the address is kept here only so the shipped
example still matches its eudplib baseline.
"""

from py import (
    CUnit,
    Db,
    DisplayText,
    EPD,
    EPD_CURRENT_PLAYER,
    SetDeaths,
    SetMemory,
    SetTo,
    Var,
    build_module,
    dataDumper,
    memcpyEPD,
    memsetEPD,
    once,
    when,
)

CUNIT_POOL_ADDR = 0x59CCA8
CUNIT_SIZE = 336
CUNIT_DWORDS = 84

SLOT0_ADDR = CUNIT_POOL_ADDR
# WARNING: slot 1699 is the last VALID CUnit under BWAPI (1700 slots) — see
# the module docstring. This address is kept only to match the eudplib
# baseline; do not treat it as safe scratch in a real map.
SCRATCH_SLOT_ADDR = CUNIT_POOL_ADDR + 1699 * CUNIT_SIZE

SRC_EPD = EPD(SLOT0_ADDR)
SCRATCH_EPD = EPD(SCRATCH_SLOT_ADDR)

BLOB_DST_ADDR = 0x58E100
BLOB_DST_EPD = EPD(BLOB_DST_ADDR)

DUMPER_PTR_ADDR = 0x58D740
DUMPER_DEST_ADDR = 0x58E200


def prog() -> None:
    snapshot_hp = Var(0)
    verified = Var(0)
    runtime_count = Var(4)
    blob = Db(b"EUDKITV3DATA")

    def run_block_io() -> None:
        # 1. 데모용으로 CUnit slot 0 의 HP(40 HP = 10240 in 8.8 fixed point)를 시드한다.
        src_unit = CUnit(SRC_EPD)
        src_unit.hp.set(10240)

        # 2. CUnit slot 0 (84 dwords)을 스크래치 슬롯 1699 로 스냅샷 복사한다.
        #    (memcpyEPD 는 CurrentPlayer 를 SRC_EPD + 84 - 1 까지 전진시킨다.)
        memcpyEPD(SCRATCH_EPD, SRC_EPD, CUNIT_DWORDS)

        # 3. 스냅샷 사본의 hp 를 읽어 10240 인지 검증한다.
        snap_unit = CUnit(SCRATCH_EPD)
        snapshot_hp.set(snap_unit.hp.read())
        when([snapshot_hp.eq(10240)], lambda: verified.set(1))

        # 4. 상수 카운트 memsetEPD (84 dwords -> 2 packed SetDeaths 트리거로 폴드, 루프 없음).
        memsetEPD(SCRATCH_EPD, 0, CUNIT_DWORDS)

        # 5. 런타임 카운트 memsetEPD (Var 카운트 -> check-first whileLoop 로 방출).
        memsetEPD(SCRATCH_EPD, 0x11223344, runtime_count)

        # 6. Db 블롭을 직접 memcpyEPD 소스로 전달해 3 dwords(12바이트) 복사한다.
        memcpyEPD(BLOB_DST_EPD, blob, 3)

        # 7. dataDumper("copy") 가 역참조할 대상 포인터를 시드한다.
        SetMemory(DUMPER_PTR_ADDR, SetTo, DUMPER_DEST_ADDR)

    once(run_block_io)

    # 8. dataDumper 로 페이로드를 주입하고 포인터가 가리키는 주소로 "copy" 한다.
    #    (내부적으로 once 게이트를 생성하며, 마지막에 dword 경계로 0-패딩된다.)
    dataDumper(b"HELLO_EUD!", [DUMPER_PTR_ADDR], ["copy"])

    # 9. memcpyEPD / dataDumper("copy") 가 변경한 CurrentPlayer(0x6509B0) 를 P1(0) 으로 복구한다.
    def finish() -> None:
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
        when([verified.eq(1)], lambda: DisplayText("Block IO & DataDumper OK"))

    once(finish)


module = build_module(prog)
