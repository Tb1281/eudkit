"""06. Arrays, User Structs, and Trigger Slot Memory (eudplib baseline).

Migration context (`eudplib` -> `eudkit`):
- `eudplib`:
  * `EUDArray([...])` is a payload `Db` buffer of 32-bit dwords.
  * `EUDVArray(80)()` allocates 80 isolated 72-byte (18-EPD) `vartrigger`
    slots; dynamic indexing runs a 32-step binary multiply by 18 EPDs.
  * `EUDStruct` (`_fields_`) wraps an internal `EUDVArray` (1 dword / 18 EPDs
    per field, no per-slot hardware bitmasks); `src.copyto(dst)` copies field
    by field.
  * Self-modifying triggers use `Forward()` + `RawTrigger(...)` + manual byte
    offsets such as `SetMemory(trg + 16, ...)` (cond 0 amount),
    `SetMemory(trg + 344, ...)` (act 0 dest), and `SetMemory(trg + 348, ...)`
    (act 0 value), spliced manually via `nextptr` / `NextTrigger()`.
- `eudkit`:
  * `FixedArray.const([...])` folds at build time (0 `TriggerPage`s).
  * `FixedArray("dword", 80)` packs 64 dwords per `TriggerPage` and uses
    carry-free `PageSlotEPD` bit weights for dynamic indexing (`N >= 64`
    resets `CurrentPlayer = 0` on `get`).
  * `TriggerStruct` packs masked slots into `TriggerPage`s (`copyFrom` in 1T).
  * `Trigger.cond(i)` / `Trigger.act(k)` + `routine.fire()` replace raw
    `+16` / `+344` / `+348` trigger pointer arithmetic.
"""

from eudplib import *  # noqa: F403

# 1) Read-only lookup table (payload Db of 32-bit dwords)
XP_TABLE = EUDArray([10, 25, 50, 100])


# 3) Multi-field user struct (each field occupies one 18-EPD vartrigger slot)
class UnitRecord(EUDStruct):
    _fields_ = ["hp", "flags", "tier"]


def run() -> None:
    # 1) Read-only lookup table read (constant index)
    xp_tier2 = EUDVariable(0)
    xp_tier2 << XP_TABLE[2]  # 50

    # 2) Mutable 80-dword EUDVArray with constant & EUDVariable indexing
    scores = EUDVArray(80)()
    scores[3] = 120
    const_val = EUDVariable(0)
    const_val << scores[3]

    dyn_idx = EUDVariable(70)
    scores[dyn_idx] = 250
    dyn_val = EUDVariable(0)
    dyn_val << scores[dyn_idx]

    # 3) Multi-field EUDStruct with copyto
    # Note: eudplib EUDStruct fields are full 32-bit dwords (no hardware mask),
    # whereas eudkit TriggerStruct packs byte(0)/byte(8) in raw lane units.
    src = UnitRecord()
    src.hp = 500
    src.flags = 0x05
    src.tier = 3

    dst = UnitRecord()
    src.copyto(dst)
    dst.tier += 1  # 4

    # 4) Self-modifying RawTrigger via Forward() + SetMemory(trg + offset, ...)
    # Trigger layout (2408B):
    #   +16  : Condition 0 amount  (8 + 8)
    #   +344 : Action 0 dest       (328 + 16)
    #   +348 : Action 0 value      (328 + 20)
    gate = EUDVariable(0)
    bonus = EUDVariable(0)
    trg = Forward()
    ret = Forward()

    PushTriggerScope()
    trg << RawTrigger(
        nextptr=ret,
        conditions=[gate.Exactly(1)],
        actions=[SetDeaths(0, Add, 50, 0)],
        preserved=True,
    )
    PopTriggerScope()

    DoActions(
        SetMemory(trg + 16, SetTo, 2),
        SetMemory(trg + 344, SetTo, EPD(bonus.getValueAddr())),
        SetMemory(trg + 348, SetTo, 100),
        gate.SetNumber(2),
    )
    RawTrigger(nextptr=trg)
    ret << NextTrigger()
    DoActions(gate.SetNumber(0))

    total_score = EUDVariable(0)
    total_score << xp_tier2 + const_val + dyn_val + dst.hp + bonus  # 1020

    if EUDIf()([
        total_score.Exactly(1020),
        dst.flags.Exactly(0x05),
        dst.tier.Exactly(4),
    ]):
        DoActions(DisplayText("06_arrays_and_structs: PASS"))
    EUDEndIf()


def onPluginStart() -> None:  # noqa: N802
    run()
