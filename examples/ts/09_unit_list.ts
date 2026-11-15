/**
 * 09. Unit-list iteration (`U` / `T` / `S` / `D`) — eudkit TypeScript DSL.
 *
 * Migration Guide (eudplib -> eudkit):
 *   [EN]
 *   - eudplib: `for ptr, epd in EUDLoopUnit():` is a runtime linked-list walk
 *     with a 32-bit shared pointer reader; `EUDLoopUnit` only covers the live
 *     `U` list, and dying units need a separate loop.
 *   - eudkit: `forEachUnitList("U", body)` emits the body once and walks the
 *     global CUnit list at run time. The body receives `(unitEpd, loop)`;
 *     `unitEpd` is a runtime `Var` in absolute EPD space, so `withEpd(unitEpd,
 *     ...)` reads `CUnit` fields.
 *   - Return the `BREAK` singleton (or call `loop.break_()`) to stop the walk.
 *     `true`/`1` continue: `BREAK` is a named sentinel, not a boolean.
 *   - `D` (dying) is walked backward from the tail through `prev`; the free
 *     list `F` is rejected because it enumerates unallocated slots.
 *
 *   [KO]
 *   - eudplib의 `EUDLoopUnit()`은 런타임 연결 리스트 순회이며 live `U` 목록만
 *     다룹니다. eudkit의 `forEachUnitList("U", body)`는 본문을 한 번 방출하고
 *     런타임에 전역 CUnit 목록을 순회합니다.
 *   - 본문은 `(unitEpd, loop)`를 받고 `unitEpd`는 절대 EPD 공간의 런타임
 *     `Var`이므로 `withEpd(unitEpd, ...)`로 `CUnit` 필드를 읽습니다.
 *   - 중단하려면 `BREAK` 싱글턴을 반환하거나 `loop.break_()`를 호출합니다.
 *     `true`/`1`은 계속 순회합니다(불리언이 아닌 이름 있는 센티널).
 *   - `D`(dying)는 tail에서 `prev`로 역방향 순회하며, free list `F`는 미할당
 *     슬롯을 열거하므로 거부됩니다.
 *
 * External package note: in standalone npm projects using `"eudkit"`, import
 * from `"eudkit"` instead of `"../../ts/index.ts"`.
 */

import {
  UNIT_LIST_U,
  Var,
  buildModule,
  forEachUnitList,
  when,
  withEpd,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  const visited = Var(0);

  forEachUnitList(UNIT_LIST_U, (unitEpd, loop) => {
    visited.iadd(1);
    // hp is CUnit +8 bytes = dword offset 2. Stop after the first dead unit;
    // the break is gated behind the runtime hp check.
    withEpd(unitEpd, (cp) => {
      when([cp.at(2).read().eq(0)], () => loop.break_());
    });
  });
});
