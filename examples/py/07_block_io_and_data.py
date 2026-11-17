"""Topic 07 (eudkit Python): Block Copy, Fill, and Payload Injection.

Migration notes (eudplib -> eudkit):
- ``f_repmovsd_epd(dst, src, dwords)`` -> ``memcpyEPD(dstEpd, srcEpd, dwords)``:
  eudkit 는 ``CurrentPlayer`` (`0x6509B0`) 를 `srcEpd` 로 옮겨 읽으며 종료 시
  ``CurrentPlayer == srcEpd + count - 1`` 로 남긴다. 또한 ``Db`` 블롭을 직접 소스로
  받아 mode-1 재배치로 복사할 수 있다 (``memcpyEPD(dstEpd, blob, dwords)``).
- ``EUDWhile`` + ``f_dwwrite_epd`` -> ``memsetEPD(dstEpd, value, count)``:
  a constant ``(dstEpd, value, count)`` fill folds into packed ``SetDeaths``
  triggers (84 dwords -> 2 triggers, no loop) — but only when ``dstEpd`` is a
  constant. This example's scratch EPD is a runtime ``Var`` (see below), so
  its fills lower to the check-first ``whileLoop`` instead; the fold itself is
  covered by unit tests (``py/tests/test_memory_costs.py``).
- euddraft ``_add_datadumper`` / ``dataDumper`` 플러그인 -> ``dataDumper(bytes, [addr], ["copy"])``:
  eudkit 은 플러그인 등록/실행 분리가 없으므로 ``dataDumper`` 한 번 호출로
  패딩된 ``Db`` 블롭을 할당하고 ``once`` 게이트 안에 ``"copy"`` 루틴을 방출한다.
  eudplib 의 ``f_dwpatch_epd`` / ``f_unpatchall`` 언패치 스택은 eudkit 에서 제거되었다.

Scratch buffer:
  The 84-dword snapshot target is a 336-byte payload ``Db`` blob, not a CUnit
  slot. ``epd_of_db(blob)`` returns a ``Var`` holding the blob's
  assembly-time EPD, which ``memcpyEPD`` / ``memsetEPD`` accept as a runtime
  destination and ``CUnitView`` accepts as a runtime EPD. The blob is
  compiler-owned payload memory, so the copy can never overwrite a live unit.
  Slot 1699 must never be used as scratch: the pool has 1700 slots
  (``py/view.py: CUNIT_SLOT_COUNT = 1700``), so slot 1699 is the last VALID
  CUnit (BWAPI: unit indices ``0 <= i < 1700``; euddraft wiki: 1700 units
  spanning ``0x59CCA8``–``0x6283E7``).

Note on imports:
  In external packages installed via ``pip install -e ./py``, import from ``eudkit``:
  ``from eudkit import CUnit, Db, DisplayText, EPD, ...``
"""

from py import (
    CUnit,
    CUnitView,
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
    epd_of_db,
    memcpyEPD,
    memsetEPD,
    once,
    when,
)

CUNIT_POOL_ADDR = 0x59CCA8
CUNIT_SIZE = 336
CUNIT_DWORDS = 84

SLOT0_ADDR = CUNIT_POOL_ADDR

SRC_EPD = EPD(SLOT0_ADDR)

BLOB_DST_ADDR = 0x58E100
BLOB_DST_EPD = EPD(BLOB_DST_ADDR)

DUMPER_PTR_ADDR = 0x58D740
DUMPER_DEST_ADDR = 0x58E200


def prog() -> None:
    snapshot_hp = Var(0)
    verified = Var(0)
    runtime_count = Var(4)
    blob = Db(b"EUDKITV3DATA")
    # 336-byte payload scratch: compiler-owned memory, never a live unit.
    # Its assembly-time EPD arrives as a runtime Var (see module docstring).
    scratch = Db(CUNIT_DWORDS * 4)
    scratch_epd = epd_of_db(scratch)

    def run_block_io() -> None:
        # 1. 데모용으로 CUnit slot 0 의 HP(40 HP = 10240 in 8.8 fixed point)를 시드한다.
        src_unit = CUnit(SRC_EPD)
        src_unit.hp.set(10240)

        # 2. CUnit slot 0 (84 dwords)을 페이로드 스크래치 블롭으로 스냅샷 복사한다.
        #    (memcpyEPD 는 CurrentPlayer 를 SRC_EPD + 84 - 1 까지 전진시킨다.)
        memcpyEPD(scratch_epd, SRC_EPD, CUNIT_DWORDS)

        # 3. 스냅샷 사본의 hp 를 읽어 10240 인지 검증한다. 필드 핸들
        #    (``.hp.read()``)은 상수 EPD만 받으므로, 런타임 EPD에서는
        #    ``CUnitView``의 ``read("hp")`` (``with_epd`` 경유)를 쓴다.
        snap_unit = CUnitView(scratch_epd)
        snapshot_hp.set(snap_unit.read("hp"))
        when([snapshot_hp.eq(10240)], lambda: verified.set(1))

        # 4. 런타임 EPD에 대한 상수 카운트 memsetEPD (84 dwords -> check-first
        #    whileLoop; 상수 dst에서만 적용되는 packed SetDeaths 폴드는 아님).
        memsetEPD(scratch_epd, 0, CUNIT_DWORDS)

        # 5. 런타임 카운트 memsetEPD (Var 카운트 -> check-first whileLoop 로 방출).
        memsetEPD(scratch_epd, 0x11223344, runtime_count)

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
