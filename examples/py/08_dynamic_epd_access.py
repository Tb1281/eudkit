"""08. Dynamic (Runtime Var) EPD Read & Write Patterns — eudkit Python DSL.

Migration Guide (eudplib -> eudkit):
  [EN]
  - eudplib: ``f_dwread_epd(target_epd)`` / ``f_dwwrite_epd(target_epd, val)``
    implicitly hide CurrentPlayer switching and self-modifying trigger patching.
  - eudkit: compile-time EPD vs. runtime ``Var`` EPD are strictly distinct!
    * ``SetMemoryEPD(target_epd, SetTo, v)`` CANNOT dereference ``target_epd``:
      a ``Var`` passed as target gets a ``mode: 1`` relocation pointing to
      ``target_epd``'s own TriggerPage slot (mutating ``target_epd`` itself).
    * ``MemFieldHandle(..., target_epd, ...).read()`` raises ``EUDSL-TARGET``
      outside ``withCP``, and ``withCP(target_epd, ...)`` raises ``EUDSL-TYPE``
      because ``withCP`` requires a compile-time integer EPD base.
    * Idiomatic solutions shown below:
      (1a) Dynamic read: ``memcpyEPD(SCRATCH_EPD, target_epd, 1)`` +
           ``MemFieldHandle(u32_schema, u32_spec, SCRATCH_EPD, "val").read()``
           + ``SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)`` to restore CP.
      (1b) Dynamic read (low-level CP): ``emitVarStore(target_epd.id, AddrReloc(0x6509B0))``
           + ``CPCursor(0).at(0).read()`` + ``SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)``.
      (2)  Dynamic write: public self-modifying ``Trigger`` +
           ``emitVarStore(target_epd.id, TriggerFieldReloc(trig.id, trig.act(0).dest.byteOffset))``
           + ``emitVarStore(write_val.id, TriggerFieldReloc(trig.id, trig.act(0).value.byteOffset))``
           + ``trig.fire()`` (gated by ``write_gate`` so the Trigger never runs unpatched).
      (3)  ``FixedArray`` when indexing eudkit-owned slots dynamically.

  [KO]
  - eudplib의 ``f_dwread_epd`` / ``f_dwwrite_epd`` 는 런타임 Var EPD 역참조를
    내부적으로 자동 처리했지만, eudkit에서는 컴파일 타임 상수 EPD와 런타임
    ``Var`` EPD를 엄격히 구분합니다.
  - ``SetMemoryEPD(target_epd, ...)`` 는 ``target_epd`` 자체 슬롯(mode-1 reloc)을
    수정하며, ``MemFieldHandle(target_epd).read()`` 는 ``withCP`` 밖에서
    ``EUDSL-TARGET``, ``withCP(target_epd)`` 는 ``EUDSL-TYPE`` 을 발생시킵니다.
  - 따라서 eudkit에서는 (1a) ``memcpyEPD`` + 스크래치 ``MemFieldHandle`` (+ CP 복구),
    (1b) ``emitVarStore(..., AddrReloc(0x6509B0))`` + ``CPCursor(0)`` (+ CP 복구),
    (2) ``Trigger`` + ``emitVarStore`` + ``trig.fire()`` 자기수정 트리거 패턴,
    (3) eudkit 소유 슬롯 인덱싱 시 ``FixedArray`` 를 사용합니다.

External package note: when installed via ``pip install -e ./py``, import
symbols from ``eudkit`` instead of ``py`` (``from eudkit import ...``).
"""

from py import (
    AddrReloc,
    CPCursor,
    EPD,
    EPD_CURRENT_PLAYER,
    FieldSpec,
    FixedArray,
    MemFieldHandle,
    SetDeaths,
    SetMemoryEPD,
    SetTo,
    Trigger,
    TriggerFieldReloc,
    Var,
    arrayDword,
    buildModule,
    defineMemView,
    emitVarStore,
    memcpyEPD,
    once,
)

CP_ADDR = 0x6509B0
BASE_ADDR = 0x58A364 + 0x200
SCRATCH_EPD = EPD(0x58A364 + 0x240)


def prog() -> None:
    u32_spec = FieldSpec(0, 4)
    u32_schema = defineMemView("U32", 4, {"val": u32_spec})
    scratch_field = MemFieldHandle(u32_schema, u32_spec, SCRATCH_EPD, "val")

    offset_var = Var(3)
    target_epd = Var(EPD(BASE_ADDR))
    write_val = Var(0x12345678)

    read_val_a = Var(0)
    read_val_b = Var(0)
    after_write = Var(0)

    # Gate so write_trig never executes unpatched during the linear pass.
    write_gate = Var(0)

    slots = FixedArray(arrayDword, 4)
    slot_idx = Var(2)
    slot_read = Var(0)

    def run_once() -> None:
        # Compute runtime target EPD inside once() so it runs only once.
        target_epd.iadd(offset_var)

        write_trig = Trigger(
            conds=[write_gate.eq(1)],
            acts=[SetDeaths(0, SetTo, 0, 0)],
            preserved=True,
        )

        # Seed known dword at EPD(0x58A364 + 0x200) + 3.
        SetMemoryEPD(EPD(BASE_ADDR) + 3, SetTo, 0xCAFEBABE)

        # (1a) Dynamic read via memcpyEPD into a static scratch EPD + MemFieldHandle.
        #      memcpyEPD updates CurrentPlayer during copy, so reset CP to 0 afterward.
        memcpyEPD(SCRATCH_EPD, target_epd, 1)
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
        read_val_a.set(scratch_field.read())

        # (1b) Dynamic read via CPCursor(0) + emitVarStore targeting CurrentPlayer (0x6509B0).
        emitVarStore(target_epd.id, AddrReloc(CP_ADDR))
        read_val_b.set(CPCursor(0).at(0).read())
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)

        # (2) Dynamic write via self-modifying Trigger + emitVarStore + write_trig.fire().
        write_gate.set(1)
        emitVarStore(
            target_epd.id,
            TriggerFieldReloc(write_trig.id, write_trig.act(0).dest.byteOffset),
        )
        emitVarStore(
            write_val.id,
            TriggerFieldReloc(write_trig.id, write_trig.act(0).value.byteOffset),
        )
        write_trig.fire()
        write_gate.set(0)

        # Read back after dynamic write to verify target_epd now holds 0x12345678.
        memcpyEPD(SCRATCH_EPD, target_epd, 1)
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
        after_write.set(scratch_field.read())

        # (3) When indexing eudkit-owned slots, prefer FixedArray over raw EPDs.
        slots.set(slot_idx, write_val)
        slot_read.set(slots.get(slot_idx))

    once(run_once)


module = buildModule(prog)
