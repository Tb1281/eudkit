"""Topic 07 (eudplib baseline): Block Copy, Fill, and Payload Injection.

eudplib / euddraft 패턴 요약:
- 블록 복사(EPD): ``f_repmovsd_epd(dst_epd, src_epd, dwords)`` 로 ``CurrentPlayer``
  를 이동시키며 4바이트(dword) 단위로 복사한다.
- 블록 채우기: eudplib 에는 별도의 ``memsetEPD`` 내장 함수가 없어 ``EUDWhile`` 루프와
  ``f_dwwrite_epd`` 로 직접 각 dword 를 덮어써야 한다.
- 바이트 블록/복사: ``Db(b"...")`` 로 페이로드 오브젝트를 만들고, ``f_memcpy(dst, src,
  bytes)`` 는 인자가 4바이트 정렬된 상수 ``int`` 일 때만 ``f_repmovsd_epd`` 로 접히며
  ``Db`` 가 들어오면 바이트 단위 루프(``_memcpy``)로 떨어진다.
- 페이로드 주입(dataDumper): euddraft ``[dataDumper]`` 플러그인이 빌드 시
  ``_add_datadumper(data, out_offsets, flags)`` 로 등록하고, ``onPluginStart`` 에서
  ``_f_datadumper()`` 를 실행한다.
  * ``"copy"`` 플래그: 대상 주소에 저장된 포인터를 ``f_epdread_epd`` 로 읽어
    ``f_repmovsd_epd`` 로 복사.
  * ``"unpatchable"`` 플래그: ``f_dwpatch_epd`` 로 패치 스택(``dwstack`` /
    ``patchstack``)에 이전 값을 기록해, 종료 시 ``f_unpatchall()`` 로 복원 가능.
"""

from eudplib import *
from eudplib.eudlib.utilf.datadumper import _add_datadumper, _f_datadumper
from eudplib.eudlib.utilf.mempatch import f_dwpatch_epd, f_unpatchall

CUNIT_POOL_ADDR = 0x59CCA8
CUNIT_SIZE = 336
CUNIT_DWORDS = 84

SLOT0_ADDR = CUNIT_POOL_ADDR
SCRATCH_SLOT_ADDR = CUNIT_POOL_ADDR + 1699 * CUNIT_SIZE

SRC_EPD = EPD(SLOT0_ADDR)
SCRATCH_EPD = EPD(SCRATCH_SLOT_ADDR)

BLOB_DST_ADDR = 0x58E100
BLOB_DST_EPD = EPD(BLOB_DST_ADDR)

DUMPER_PTR_ADDR = 0x58D740
DUMPER_DEST_ADDR = 0x58E200

# euddraft 플러그인 방식: 빌드 시 페이로드와 대상 주소를 미리 큐에 등록한다.
_add_datadumper(b"HELLO_EUD!", [DUMPER_PTR_ADDR], {"copy"})


def epd_memset_loop(dst_epd, value, count):
    """eudplib 에는 memsetEPD 가 없으므로 EUDWhile + f_dwwrite_epd 루프로 채운다."""
    idx = EUDVariable(0)
    if EUDWhile()(idx < count):
        f_dwwrite_epd(dst_epd + idx, value)
        idx += 1
    EUDEndWhile()


def run():
    verified = EUDVariable(0)
    runtime_count = EUDVariable(4)

    # 1. 데모용으로 CUnit slot 0 의 HP(40 HP = 10240 in 8.8 fixed point)를 시드한다.
    src_unit = CUnit(SRC_EPD)
    src_unit.hp = 10240

    # 2. CUnit slot 0 (84 dwords)을 스크래치 슬롯 1699 로 스냅샷 복사한다.
    #    인자 순서: (dst_epd, src_epd, dword_count)
    f_repmovsd_epd(SCRATCH_EPD, SRC_EPD, CUNIT_DWORDS)

    # 3. 스냅샷된 CUnit 사본의 hp 필드를 검증한다.
    snap_unit = CUnit(SCRATCH_EPD)
    snapshot_hp = snap_unit.hp
    if EUDIf()(snapshot_hp == 10240):
        verified << 1
    EUDEndIf()

    # 4. 스크래치 영역 84 dwords 를 0 으로 초기화한 뒤, 런타임 횟수(4 dwords)만 0x11223344 로 채운다.
    epd_memset_loop(SCRATCH_EPD, 0, CUNIT_DWORDS)
    epd_memset_loop(SCRATCH_EPD, 0x11223344, runtime_count)

    # 5. Db 페이로드 생성 및 복사:
    #    - f_repmovsd_epd 는 EPD 단위로 3 dwords(12바이트)를 직접 복사한다.
    #    - f_memcpy 는 바이트 주소/바이트 길이를 받으며, Db 소스는 바이트 루프로 폴백된다.
    blob = Db(b"EUDKITV3DATA")
    f_repmovsd_epd(BLOB_DST_EPD, EPD(blob), 3)
    f_memcpy(BLOB_DST_ADDR + 12, blob, 12)

    # 6. dataDumper("copy") 가 읽을 포인터를 시드한 뒤, 등록된 dataDumper 를 실행한다.
    DoActions(SetMemory(DUMPER_PTR_ADDR, SetTo, DUMPER_DEST_ADDR))
    _f_datadumper()

    # (참고) "unpatchable" 모드나 직접 메모리 패치를 쓴 경우:
    #   f_dwpatch_epd(EPD(DUMPER_PTR_ADDR), Db(b"PATCHED"))
    # 를 거쳐 패치 스택에 이전 값을 쌓고, 종료 시 f_unpatchall() 로 일괄 복구한다.
    if EUDIf()(verified == 1):
        DoActions(DisplayText("Block IO & DataDumper OK"))
    EUDEndIf()


def onPluginStart():
    run()


def afterTriggerExec():
    # unpatchable 패치를 썼을 때 언로드 직전 f_unpatchall() 로 원상복구하는 패턴.
    _ = (f_dwpatch_epd, f_unpatchall)
    EUDOnStart(lambda: None)
