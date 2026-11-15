/**
 * 01. Direct Fixed-Address & EPD Reads, Writes, Conditions (eudkit TypeScript).
 *
 * eudplib -> eudkit 마이그레이션 / Migration notes:
 * - eudplib:
 *     * Memory(addr, AtLeast, 100) / SetMemory(addr, Add, 50)
 *     * f_dwread_epd(EPD(addr)) 는 내부에서 CurrentPlayer(0x6509B0) 를 사용하므로
 *       DisplayText 전후로 f_getcurpl() / f_setcurpl() 보존이 필요.
 * - eudkit:
 *     * MemCond(EPD(addr), ">=", 100) / Memory(addr, AtLeast, 100) / MemFieldHandle.ge(...)
 *     * MemView / MemFieldHandle (zero-storage, CP 비파괴 bit-test read)
 *     * once([...], body) 로 1회성 보너스 가드 + SetMemory(addr, Add, 50) 직접 쓰기
 *     * MemFieldHandle(...).read() 는 CurrentPlayer(0x6509B0) 를 클로버하지 않으므로
 *       FixedString / DisplayText 직전에 CP 를 수동 복원할 필요가 없음.
 *
 * Note: External npm `"eudkit"` packages import from `"eudkit"`
 * (`import { ... } from "eudkit"`). Inside this repo, examples import from `"../../ts/index.ts"`.
 */

import {
  Add,
  AtLeast,
  DisplayText,
  EPD,
  FixedString,
  GAME_TICK_ADDR,
  MemCond,
  MemFieldHandle,
  Memory,
  SetMemory,
  Var,
  buildModule,
  defineMemView,
  once,
  when,
} from "../../ts/index.ts";

const P1_MINERALS_ADDR = 0x57f0f0;

export const module = buildModule(() => {
  const u32Field = { offset: 0, size: 4 as const };
  const u32Schema = defineMemView("U32", 4, { val: u32Field });
  const mineralsField = new MemFieldHandle(u32Schema, u32Field, EPD(P1_MINERALS_ADDR), "val");
  const tickField = new MemFieldHandle(u32Schema, u32Field, EPD(GAME_TICK_ADDR), "val");

  const bonusApplied = Var(0);
  const liveMinerals = Var(0);
  const liveTick = Var(0);
  const status = new FixedString(64);

  // 1C engine-direct gate + once(): add +50 bonus minerals once when minerals >= 100 and flag == 0
  once(
    [MemCond(EPD(P1_MINERALS_ADDR), ">=", 100), bonusApplied.eq(0)],
    () => {
      SetMemory(P1_MINERALS_ADDR, Add, 50);
      bonusApplied.set(1);
      DisplayText("BONUS +50 MINERALS APPLIED");
    },
  );

  // CP-safe MemFieldHandle.read(): uses engine-direct DeathsX bit tests (no 0x6509B0 clobber)
  liveMinerals.set(mineralsField.read());
  liveTick.set(tickField.read());

  status.clear();
  // Both Memory(addr, AtLeast, 100) and MemFieldHandle.ge(150) compile to 1C engine-direct tests
  when(
    [Memory(P1_MINERALS_ADDR, AtLeast, 100), mineralsField.ge(150)],
    () => {
      status.write(
        "READY MIN={:\x1a>5d} TICK={:\x1a>6d} BONUS={:\x1a>1d}",
        liveMinerals,
        liveTick,
        bonusApplied,
      );
    },
    () => {
      status.write(
        "WAIT  MIN={:\x1a>5d} TICK={:\x1a>6d} BONUS={:\x1a>1d}",
        liveMinerals,
        liveTick,
        bonusApplied,
      );
    },
  );
  status.display();
});
