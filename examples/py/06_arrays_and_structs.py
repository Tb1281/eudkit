"""06. Arrays, User Structs, and Trigger Slot Memory (eudkit Python).

Migration notes (`eudplib` -> `eudkit`):
- `eudplib`:
  `EUDArray` allocated payload `Db` memory, `EUDVArray(80)()` allocated 80
  72-byte (18-EPD) `vartrigger`s with a 32T `* 18` multiply chain on dynamic
  indexing, `EUDStruct` (`_fields_`) used `copyto` across unmasked dword slots,
  and self-modifying triggers patched raw byte offsets (`trg + 16`, `trg + 344`,
  `trg + 348`) with `Forward()` + `RawTrigger`.
- `eudkit`:
  * `FixedArray.const([...])`: build-time table (0 `TriggerPage`s, folded reads
    and comparisons).
  * `FixedArray("dword", 80)`: `64A` `TriggerPage` storage; dynamic `Var`
    indexing for length >= 64 uses carry-free `PageSlotEPD` (`ceil(log2(N))`
    triggers) and resets `CurrentPlayer = 0` on dynamic `get`.
  * `TriggerStruct`: per-action hardware-masked `TriggerPage` slots with `1T`
    `copyFrom`. Per ADR 0018, sub-dword fields (`byte(8)`, `bit(...)`) operate
    in **raw lane units** (shifted values such as `3 << 8`).
  * `Trigger` + `cond(0).amount` / `act(0).value` + `.fire()`: symbolic slot
    views replace raw `+16` / `+344` / `+348` offsets.
  * Scoping: pure metadata (`FixedArray.const`, `TriggerStruct` schemas) and
    `lazy*` handles (`lazyVar`, `lazyFixedArray`, `lazyStruct`) may live at
    module scope; eager `Var`, `FixedArray`, and `UnitRecord()` allocations
    must live inside `build_module(...)` (calling `lazy*` inside `build_module`
    emits a warning, and calling eager constructors outside throws `EUDSL-SCOPE`).

Note: External packages (`pip install -e ./py`) import `from eudkit import ...`.
"""

from py import (
    DisplayText,
    FixedArray,
    Trigger,
    TriggerStruct,
    Var,
    build_module,
    byte,
    dword,
    lazyVar,
    once,
    when,
)

# Pure build-time metadata & lazy module-scope handle (no builder needed here):
XP_TABLE = FixedArray.const([10, 25, 50, 100])
UnitRecord = TriggerStruct(
    "UnitRecord",
    {
        "hp": dword(),
        "flags": byte(0),
        "tier": byte(8),
    },
)
total_score = lazyVar(0)


def prog() -> None:
    # 1) Read-only XP lookup table (0 TriggerPages; folded at build time)
    xp_tier2 = XP_TABLE.get(2).read()  # Var(50)

    # 2) Mutable 80-dword FixedArray (2 TriggerPages: 64 + 16 slots)
    #    Eager declarations inside build_module keep allocation order deterministic.
    scores = FixedArray("dword", 80)
    scores.set(3, 120)
    const_val = scores.get(3)

    # Dynamic index >= 64 triggers carry-free PageSlotEPD read/write and resets CP=0
    dyn_idx = Var(70)
    scores.set(dyn_idx, 250)
    dyn_val = scores.get(dyn_idx)

    # 3) Multi-field TriggerStruct with copyFrom
    #    ADR 0018: byte(8) uses raw lane units (mask 0x0000FF00), so pass `3 << 8`.
    src = UnitRecord({"hp": 500, "flags": 0x05, "tier": 3 << 8})
    dst = UnitRecord()
    dst.copyFrom(src)
    dst.tier.iadd(1 << 8)  # 0x0300 -> 0x0400 (4 << 8)

    # 4) Self-modifying Trigger patched via cond(0).amount / act(0).value + .fire()
    gate = Var(0)
    bonus = Var(0)
    routine = Trigger(
        conds=[gate.eq(1)],
        acts=[bonus.act_add(50)],
        preserved=True,
    )

    def run_once() -> None:
        routine.cond(0).amount.set(2)
        routine.act(0).value.set(100)
        gate.set(2)
        routine.fire()
        gate.set(0)
        total_score.set(xp_tier2.add(const_val).add(dyn_val).add(dst.hp).add(bonus))

    once(run_once)

    def verify() -> None:
        DisplayText("06_arrays_and_structs: PASS")

    def verify_fail() -> None:
        DisplayText("06_arrays_and_structs: FAIL")

    when(
        [
            XP_TABLE.get(2).eq(50),
            total_score.eq(1020),
            dst.flags.eq(0x05),
            dst.tier.eq(4 << 8),
        ],
        verify,
        verify_fail,
    )


module = build_module(prog)
