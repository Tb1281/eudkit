"""Topic 02 (eudplib baseline): Sub-Dword (Byte / Word / Bitmask) Reads, Writes, Conditions.

마이그레이션 비교 노트 (eudplib -> eudkit):
- eudplib:
  * Raw mask primitives (`MemoryXEPD`, `SetMemoryXEPD`, `f_maskwrite_epd`, `f_maskread_epd`)
    require **pre-shifted** values aligned to the mask lane (e.g. `0x5678 << 16` for `0xFFFF0000`).
  * Sub-dword helpers (`f_bread_epd`, `f_bwrite_epd`, `f_wread_epd`, `f_wwrite_epd`) take an
    explicit `(epd, subp)` pair with **unshifted** byte/word values, and mask-read helpers
    like `f_maskread_epd(epd, mask)` return **pre-shifted** bits.
  * Note parameter orders differ across families:
    - `SetMemoryXEPD(epd, modtype, value, mask)`
    - `MemoryXEPD(dest, cmptype, value, mask)`
    - `f_maskwrite_epd(epd, value, mask)` (pre-shifted `value`)
    - `f_maskread_epd(epd, mask)` (returns pre-shifted `value & mask`)
    - `f_bwrite_epd(epd, subp, b)` / `f_bread_epd(epd, subp)`
    - `f_wwrite_epd(epd, subp, w)` / `f_wread_epd(epd, subp)`
- eudkit:
  * Replaces ad-hoc `(epd, subp)` + manual bit-shift arithmetic with typed `defineMemView`
    (`FieldSpec(offset, size, mask?)`) in **unshifted field units** (ADR 0017), while keeping
    `SetMemoryXEPD` / `MemoryXEPD` for explicit raw-lane bitmasks and `TriggerStruct` masked
    fields (`bit`, `byte`, `word`) in **raw lane units** (ADR 0018).
"""

from eudplib import *  # noqa: F403

FLAGS_ADDR = 0x58A364 + 0x200
STATE_ADDR = 0x58A364 + 0x204
FLAGS_EPD = EPD(FLAGS_ADDR)
STATE_EPD = EPD(STATE_ADDR)

STEALTH_MASK = 0x00000020


def run():
    # 1. Raw bitmask write via SetMemoryXEPD & f_maskwrite_epd (pre-shifted values).
    # Set bit 5 ('stealth' = 0x00000020) in flags dword at 0x58A364 + 0x200.
    DoActions(SetMemoryXEPD(FLAGS_EPD, SetTo, STEALTH_MASK, STEALTH_MASK))
    f_maskwrite_epd(FLAGS_EPD, STEALTH_MASK, STEALTH_MASK)

    # 2. Sub-dword writes via f_bwrite_epd / f_wwrite_epd (unshifted values + subp lane).
    # Packed state dword at 0x58A364 + 0x204:
    #   byte +0 (subp=0): low byte = 0x2A
    #   word +2 (subp=2): upper word = 0x1234 (written as 0x1234 << 16 with mask 0xFFFF0000)
    f_bwrite_epd(STATE_EPD, 0, 0x2A)
    f_wwrite_epd(STATE_EPD, 2, 0x1234)

    # 3. Masked conditions via MemoryXEPD (pre-shifted value & mask).
    # Test bit 0x00000020 is set AND masked low byte != 0xFF.
    if EUDIf()(
        [
            MemoryXEPD(FLAGS_EPD, AtLeast, 1, STEALTH_MASK),
            EUDNot(MemoryXEPD(STATE_EPD, Exactly, 0xFF, 0x000000FF)),
        ]
    ):
        # Record pass flag in byte +1 (subp=1) of state dword.
        f_bwrite_epd(STATE_EPD, 1, 1)
    EUDEndIf()

    # 4. Read masked bits & sub-dword lanes into EUDVariables.
    # f_maskread_epd returns pre-shifted bits (0x20 or 0);
    # f_bread_epd / f_wread_epd return unshifted byte (0..255) / word (0..65535).
    stealth_bits = f_maskread_epd(FLAGS_EPD, STEALTH_MASK)
    low_byte = f_bread_epd(STATE_EPD, 0)
    upper_word = f_wread_epd(STATE_EPD, 2)

    summary = EUDVariable()
    if EUDIf()([stealth_bits == STEALTH_MASK, low_byte != 0xFF]):
        summary << low_byte + upper_word
    EUDEndIf()

    # 5. Raw upper-word overwrite via SetMemoryXEPD (must pre-shift << 16).
    DoActions(SetMemoryXEPD(STATE_EPD, SetTo, 0x5678 << 16, 0xFFFF0000))
    if EUDIf()(MemoryXEPD(STATE_EPD, Exactly, 0x5678 << 16, 0xFFFF0000)):
        f_bwrite_epd(STATE_EPD, 1, 2)
    EUDEndIf()


def onPluginStart():
    run()
