/**
 * Smoke map 3: interactive `UnitGroup(compact=true, autoPrune=true)` & CUnit tracking.
 *
 * Mirrors `unitgroup-cunit.py` 1:1.
 */

import {
  Anywhere,
  AtLeast,
  CreateUnit,
  EPD,
  EPD_BASE,
  ElapsedTime,
  FixedString,
  MemFieldHandle,
  P1,
  UnitGroup,
  UnitType,
  Var,
  buildModule,
  defineMemView,
  every,
  once,
  when,
} from "../../../ts/index.ts";

const FIRST_P1_UNIT_ADDR = 0x6283f8;

export const module = buildModule(() => {
  const u32Field = { offset: 0, size: 4 as const };
  const u32Schema = defineMemView("U32", 4, { val: u32Field });
  const p1HeadField = new MemFieldHandle(u32Schema, u32Field, EPD(FIRST_P1_UNIT_ADDR), "val");

  const group = new UnitGroup(6, { compact: true, autoPrune: true });
  const lastHead = Var(0);
  const lastEpd = Var(0);
  const spawned = Var(0);
  const liveVisits = Var(0);

  for (const sec of [1, 2, 3, 4]) {
    once(ElapsedTime(AtLeast, sec), () => {
      CreateUnit(1, UnitType.TerranMarine, Anywhere, P1);
      spawned.iadd(1);
    });
  }

  every(6, () => {
    const head = p1HeadField.read();
    when([head.ne(0), head.ne(lastHead)], () => {
      lastHead.set(head);
      lastEpd.set(head.sub(EPD_BASE).shr(2));
      group.add(lastEpd);
    });

    liveVisits.set(0);
    group.for_each(() => {
      liveVisits.iadd(1);
    });
  });

  const line1 = new FixedString(64);
  const line2 = new FixedString(64);
  const line3 = new FixedString(64);
  const verdict = new FixedString(64);

  every(6, () => {
    line1.clear();
    line1.write("UNITGROUP COMPACT LEN={:\x1a>1d} VISITS={:\x1a>1d}", group.length, liveVisits);
    line1.displayAt(1);

    line2.clear();
    line2.write("SPAWNED={:\x1a>1d}/4 LAST_EPD={:\x1a>5d}", spawned, lastEpd);
    line2.displayAt(2);

    line3.clear();
    line3.write("Kill own Marine (A-click) -> autoPrune drops LEN & VISITS");
    line3.displayAt(3);

    verdict.clear();
    when(
      [group.length.eq(liveVisits)],
      () => verdict.write("STATUS: PASS (LEN == VISITS)"),
      () => verdict.write("STATUS: MISMATCH"),
    );
    verdict.displayAt(4);
  });
});
