/**
 * 08. Dynamic (Runtime Var) EPD Read & Write Patterns — eudkit TypeScript DSL.
 *
 * Migration Guide (eudplib -> eudkit):
 *   [EN]
 *   - eudplib: `f_dwread_epd(targetEpd)` / `f_dwwrite_epd(targetEpd, val)`
 *     implicitly hide CurrentPlayer switching and self-modifying trigger patching.
 *   - eudkit: compile-time EPD vs. runtime `Var` EPD are strictly distinct!
 *     * `SetMemoryEPD(targetEpd, SetTo, v)` CANNOT dereference `targetEpd`:
 *       a `Var` passed as target gets a `mode: 1` relocation pointing to
 *       `targetEpd`'s own TriggerPage slot (mutating `targetEpd` itself).
 *     * `new MemFieldHandle(..., targetEpd, ...).read()` throws `EUDSL-TARGET`
 *       outside `withCP`, and `withCP(targetEpd, ...)` throws `EUDSL-TYPE`
 *       because `withCP` requires a compile-time integer EPD base.
 *     * Idiomatic solutions shown below:
 *       (1a) Dynamic read: `memcpyEPD(SCRATCH_EPD, targetEpd, 1)` +
 *            `new MemFieldHandle(u32Schema, u32Spec, SCRATCH_EPD, "val").read()`
 *            + `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` to restore CP.
 *       (1b) Dynamic read (low-level CP): `emitVarStore(targetEpd.id, AddrReloc(0x6509B0))`
 *            + `new CPCursor(0).at(0).read()` + `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)`.
 *       (2)  Dynamic write: public self-modifying `Trigger` +
 *            `emitVarStore(targetEpd.id, TriggerFieldReloc(trig.id, trig.act(0).dest.byteOffset))`
 *            + `emitVarStore(writeVal.id, TriggerFieldReloc(trig.id, trig.act(0).value.byteOffset))`
 *            + `trig.fire()` (gated by `writeGate` so the Trigger never runs unpatched).
 *       (3)  `FixedArray` when indexing eudkit-owned slots dynamically.
 *
 *   [KO]
 *   - eudplib의 `f_dwread_epd` / `f_dwwrite_epd` 는 런타임 Var EPD 역참조를
 *     내부적으로 자동 처리했지만, eudkit에서는 컴파일 타임 상수 EPD와 런타임
 *     `Var` EPD를 엄격히 구분합니다.
 *   - `SetMemoryEPD(targetEpd, ...)` 는 `targetEpd` 자체 슬롯(mode-1 reloc)을
 *     수정하며, `MemFieldHandle(targetEpd).read()` 는 `withCP` 밖에서
 *     `EUDSL-TARGET`, `withCP(targetEpd)` 는 `EUDSL-TYPE` 을 발생시킵니다.
 *   - 따라서 eudkit에서는 (1a) `memcpyEPD` + 스크래치 `MemFieldHandle` (+ CP 복구),
 *     (1b) `emitVarStore(..., AddrReloc(0x6509B0))` + `CPCursor(0)` (+ CP 복구),
 *     (2) `Trigger` + `emitVarStore` + `trig.fire()` 자기수정 트리거 패턴,
 *     (3) eudkit 소유 슬롯 인덱싱 시 `FixedArray` 를 사용합니다.
 *
 * External package note: in standalone npm projects using `"eudkit"`, import
 * from `"eudkit"` instead of `"../../ts/index.ts"`.
 */

import {
  AddrReloc,
  CPCursor,
  EPD,
  EPD_CURRENT_PLAYER,
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
} from "../../ts/index.ts";

const CP_ADDR = 0x6509b0;
const BASE_ADDR = 0x58a364 + 0x200;
const SCRATCH_EPD = EPD(0x58a364 + 0x240);

export const module = buildModule(() => {
  const u32Spec = { offset: 0, size: 4 as const };
  const u32Schema = defineMemView("U32", 4, { val: u32Spec });
  const scratchField = new MemFieldHandle(u32Schema, u32Spec, SCRATCH_EPD, "val");

  const offsetVar = Var(3);
  const targetEpd = Var(EPD(BASE_ADDR));
  const writeVal = Var(0x12345678);

  const readValA = Var(0);
  const readValB = Var(0);
  const afterWrite = Var(0);

  // Gate so writeTrig never executes unpatched during the linear pass.
  const writeGate = Var(0);

  const slots = FixedArray(arrayDword, 4);
  const slotIdx = Var(2);
  const slotRead = Var(0);

  once(() => {
    // Compute runtime target EPD inside once() so it runs only once.
    targetEpd.iadd(offsetVar);

    const writeTrig = Trigger({
      conds: [writeGate.eq(1)],
      acts: [SetDeaths(0, SetTo, 0, 0)],
      preserved: true,
    });

    // Seed known dword at EPD(0x58A364 + 0x200) + 3.
    SetMemoryEPD(EPD(BASE_ADDR) + 3, SetTo, 0xcafebabe);

    // (1a) Dynamic read via memcpyEPD into a static scratch EPD + MemFieldHandle.
    //      memcpyEPD updates CurrentPlayer during copy, so reset CP to 0 afterward.
    memcpyEPD(SCRATCH_EPD, targetEpd, 1);
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
    readValA.set(scratchField.read());

    // (1b) Dynamic read via CPCursor(0) + emitVarStore targeting CurrentPlayer (0x6509B0).
    emitVarStore(targetEpd.id, AddrReloc(CP_ADDR));
    readValB.set(new CPCursor(0).at(0).read());
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);

    // (2) Dynamic write via self-modifying Trigger + emitVarStore + writeTrig.fire().
    writeGate.set(1);
    emitVarStore(
      targetEpd.id,
      TriggerFieldReloc(writeTrig.id, writeTrig.act(0).dest.byteOffset),
    );
    emitVarStore(
      writeVal.id,
      TriggerFieldReloc(writeTrig.id, writeTrig.act(0).value.byteOffset),
    );
    writeTrig.fire();
    writeGate.set(0);

    // Read back after dynamic write to verify targetEpd now holds 0x12345678.
    memcpyEPD(SCRATCH_EPD, targetEpd, 1);
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
    afterWrite.set(scratchField.read());

    // (3) When indexing eudkit-owned slots, prefer FixedArray over raw EPDs.
    slots.set(slotIdx, writeVal);
    slotRead.set(slots.get(slotIdx));
  });
});
