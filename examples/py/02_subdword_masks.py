"""Topic 02 (eudkit Python): Sub-Dword (Byte / Word / Bitmask) Reads, Writes, Conditions.

External packages installed via `pip install -e ./py` import from `eudkit`
(`from eudkit import ...`); inside this repo, examples import from `py`.

마이그레이션 비교 노트 (eudplib -> eudkit):
- eudplib:
  * `MemoryXEPD` / `SetMemoryXEPD` / `f_maskwrite_epd` / `f_maskread_epd` 는
    mask 레인에 맞춰 **사전 시프트(pre-shifted)** 된 값만 받거나 반환합니다.
  * `f_bread_epd` / `f_bwrite_epd` / `f_wread_epd` / `f_wwrite_epd` 는 `(epd, subp)` 와
    **시프트되지 않은(unshifted)** 바이트/워드 값을 사용합니다.
- eudkit:
  * `defineMemView` + `FieldSpec(offset, size, mask?)` (ADR 0017):
    - `FieldSpec` 기반 `MemFieldHandle` 은 바이트 레인 시프트(`(offset & 3) * 8`)를
      자동으로 처리하여 **unshifted field units** 로 `.set(c)` / `.eq(c)` / `.ne(c)` /
      `.read()` 를 수행합니다. (예: `offset=6, size=2` 에 `.set(0x1234)` -> `0x1234 << 16`)
    - `value_space` 밖의 상수(예: byte/word 필드에 `-1` == `0xFFFFFFFF`)는 자동으로
      `Never()` / `Always()` 로 폴딩됩니다 (ADR 0017 §3). "all ones" 는 반드시
      `0xFF` (byte) / `0xFFFF` (word) 로 명시해야 합니다.
  * Raw escape (`SetMemoryXEPD` / `MemoryXEPD`):
    - 레인 시프트된 원시 마스크/상수를 직접 제어할 때 사용합니다.
  * `TriggerStruct` masked fields (`bit` / `byte` / `word`, ADR 0018):
    - `TriggerStruct` 필드는 `FieldSpec.shift` 를 버린 `Var(explicit_mask=mask)` 이므로
      **raw lane units** 로 동작합니다. (예: `word(16)` 은 `0x12340000`, all-ones 는
      `0xFFFF0000` — `-1` 은 동일하게 범위 밖으로 폴딩됩니다.)
"""

from py import (
    AtLeast,
    EPD,
    Exactly,
    FieldSpec,
    MemFieldHandle,
    MemoryXEPD,
    SetMemoryXEPD,
    SetTo,
    TriggerStruct,
    Var,
    bit,
    buildModule,
    byte,
    defineMemView,
    when,
    word,
)

FLAGS_ADDR = 0x58A364 + 0x200
STATE_ADDR = 0x58A364 + 0x204
BASE_EPD = EPD(FLAGS_ADDR)
FLAGS_EPD = EPD(FLAGS_ADDR)
STATE_EPD = EPD(STATE_ADDR)

STEALTH_MASK = 0x00000020

stealthSpec = FieldSpec(0, 1, mask=STEALTH_MASK)
lowByteSpec = FieldSpec(4, 1)
statusByteSpec = FieldSpec(5, 1)
upperWordSpec = FieldSpec(6, 2)

StateView = defineMemView(
    "StateView",
    8,
    {
        "stealth": stealthSpec,
        "lowByte": lowByteSpec,
        "statusByte": statusByteSpec,
        "upperWord": upperWordSpec,
    },
)

PackedState = TriggerStruct(
    "PackedState",
    {
        "stealth": bit(1, 5),
        "lowByte": byte(),
        "upperWord": word(16),
    },
)


def prog() -> None:
    # 1. defineMemView + MemFieldHandle (ADR 0017: unshifted field units).
    stealth = MemFieldHandle(StateView, stealthSpec, BASE_EPD, "stealth")
    lowByte = MemFieldHandle(StateView, lowByteSpec, BASE_EPD, "lowByte")
    statusByte = MemFieldHandle(StateView, statusByteSpec, BASE_EPD, "statusByte")
    upperWord = MemFieldHandle(StateView, upperWordSpec, BASE_EPD, "upperWord")

    # Byte 0 bit 5 (shift=0 -> field unit 0x20), byte +0 = 0x2A, word +2 = 0x1234 (unshifted!).
    stealth.set(STEALTH_MASK)
    lowByte.set(0x2A)
    upperWord.set(0x1234)

    fieldHits = Var(0)

    def on_field_match() -> None:
        statusByte.set(1)
        fieldHits.iadd(1)

    when(
        [stealth.eq(STEALTH_MASK), lowByte.ne(0xFF), upperWord.eq(0x1234)],
        on_field_match,
    )

    # Read sub-dword fields into Vars (each returns unshifted field units).
    stealthVal = stealth.read()
    lowVal = lowByte.read()
    upperVal = upperWord.read()

    readSum = Var(0)
    readSum.set(lowVal)
    readSum.iadd(upperVal)

    readHits = Var(0)
    when(
        [stealthVal.eq(STEALTH_MASK), lowVal.eq(0x2A), upperVal.eq(0x1234), readSum.eq(0x125E)],
        lambda: readHits.iadd(1),
    )

    # 2. Raw SetMemoryXEPD / MemoryXEPD for explicit pre-shifted masks.
    SetMemoryXEPD(STATE_EPD, SetTo, 0x5678 << 16, 0xFFFF0000)
    rawHits = Var(0)
    when(
        [
            MemoryXEPD(FLAGS_EPD, AtLeast, 1, STEALTH_MASK),
            MemoryXEPD(STATE_EPD, Exactly, 0x5678 << 16, 0xFFFF0000),
        ],
        lambda: rawHits.iadd(1),
    )

    # 3. TriggerStruct masked fields (ADR 0018: raw lane units, shift dropped).
    ps = PackedState.instantiate(
        {
            "stealth": STEALTH_MASK,
            "lowByte": 0x2A,
            "upperWord": 0x1234 << 16,
        }
    )
    structHits = Var(0)
    when(
        [ps.stealth.eq(STEALTH_MASK), ps.lowByte.ne(0xFF), ps.upperWord.eq(0x1234 << 16)],
        lambda: structHits.iadd(1),
    )

    # 4. Negative `-1` rule (ADR 0017 §3 / ADR 0018 §2):
    # `-1` normalises to 0xFFFFFFFF, which is outside any sub-dword space, so
    # `.eq(-1)` folds to Never() and `.ne(-1)` folds to Always().
    # Always write explicit width all-ones: 0xFF (byte), 0xFFFF (field word),
    # or 0xFFFF0000 (raw-lane word(16)).
    negFoldHits = Var(0)
    when([lowByte.eq(-1)], lambda: negFoldHits.iadd(1))  # Never -> +0
    when([upperWord.ne(-1)], lambda: negFoldHits.iadd(1))  # Always -> +1
    when([ps.lowByte.eq(-1)], lambda: negFoldHits.iadd(1))  # Never -> +0
    when([ps.upperWord.ne(-1)], lambda: negFoldHits.iadd(1))  # Always -> +1
    when(
        [
            lowByte.ne(0xFF),
            upperWord.ne(0xFFFF),
            ps.lowByte.ne(0xFF),
            ps.upperWord.ne(0xFFFF0000),
        ],
        lambda: negFoldHits.iadd(1),
    )  # +1


module = buildModule(prog)
