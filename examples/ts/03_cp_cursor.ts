/**
 * 03_cp_cursor (eudkit TypeScript): CurrentPlayer (0x6509B0) Scoped Cursor & CP Reset Safety.
 *
 * Migration guide (eudplib -> eudkit):
 *   - eudplib:
 *       `f_setcurpl(EPD(0x59CCA8))`, relative `Add(+cpo) / Add(-cpo)` via
 *       `f_dwread_cp(2)` / `f_wread_cp(0x64 // 4, 0)` / `f_bwrite_cp(0x4D // 4, 1, v)`,
 *       then `f_setcurpl(0)` / `f_setcurpl2cpcache()` before `DoActions(DisplayText(...))`.
 *   - eudkit:
 *       `withCP(EPD(0x59CCA8), (cp) => { ... })`.
 *       * Constant `number` (integer) EPD base ONLY — passing a runtime `Var` base
 *         throws `EUDSL-TYPE` at build time so a byte address never corrupts `0x6509B0`.
 *       * `cp.moveTo(dwordOffset)` and `cp.at(dwordOffset)` / `cp.view(CUnit)` emit
 *         absolute `SetDeaths(EPD_CURRENT_PLAYER, SetTo, baseEpd + offset, 0)` anchors
 *         (never relative `Add` deltas), preventing cursor drift inside runtime loops.
 *       * CRITICAL RULE: `CPCursor.restore()` at `withCP` exit re-anchors
 *         `CurrentPlayer` to `baseEpd` (NOT `0`!). Always emit
 *         `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` before `DisplayText` or
 *         `FixedString.displayAt`, or use `CUnit(EPD(0x59CCA8))` directly without
 *         touching `0x6509B0`.
 *
 * 마이그레이션 요약 (한국어):
 *   - `withCP`는 상수 EPD만 허용하며(`Var` 전달 시 `EUDSL-TYPE`), 오프셋 이동(`moveTo`,
 *     `cp.at`, `cp.view(CUnit)`)은 상대 덧셈이 아닌 절대 `SetTo (baseEpd + offset)`을
 *     생성해 루프 내부에서도 커서가 어긋나지 않는다.
 *   - `withCP` 블록 종료 시 `CPCursor.restore()`는 CP를 `0`이 아니라 `baseEpd`로
 *     복귀시킨다. 따라서 `DisplayText` / `FixedString.displayAt` 호출 전에는 반드시
 *     `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)`으로 P1(0) 복구가 필요하다.
 *   - 단일 상수 EPD 접근이라면 `0x6509B0`을 건드리지 않는 `CUnit(EPD(0x59CCA8))`
 *     직접 뷰가 더 안전하고 간결하다.
 *
 * Note: External npm `"eudkit"` packages import `from "eudkit"`.
 * Inside this repository we import from `"../../ts/index.ts"`.
 */

import {
  type CPCursor,
  CUnit,
  DisplayText,
  EPD,
  EPD_CURRENT_PLAYER,
  FixedString,
  SetDeaths,
  SetTo,
  buildModule,
  when,
  withCP,
} from "../../ts/index.ts";

const CUNIT0_EPD = EPD(0x59cca8);

export const module = buildModule(() => {
  // Seed initial values on CUnit slot 0 (0x59CCA8) via direct MemView before reading.
  const seed = CUnit(CUNIT0_EPD);
  seed.hp.set(100 * 256);
  seed.unitType.set(0);

  // 1. Scoped CP cursor access (constant int EPD only; Var base throws EUDSL-TYPE)
  const [hpFromAt, unitType] = withCP(CUNIT0_EPD, (cp: CPCursor) => {
    // cp.at(dwordOffset): dword +2 == +0x08 (hp)
    cp.at(2).set(100 * 256);
    const hp = cp.at(2).read();

    // cp.view(CUnit): typed MemView bound to the CP cursor
    const u = cp.view(CUnit);

    // Explicit absolute moveTo hops (SetTo baseEpd + offset, no relative drift)
    cp.moveTo(0x64 >> 2); // dword 25 (+0x64: unitType)
    cp.moveTo(0x64 >> 2); // repeating the same offset is a build-time no-op
    const uType = u.unitType.read();

    cp.moveTo(0x4d >> 2); // dword 19 (+0x4D: order)
    u.order.set(10);

    // Accessing u.hp (+0x08 -> dword 2) re-anchors CP to baseEpd + 2 (!= 0),
    // so scope exit triggers CPCursor.restore() -> SetTo baseEpd (NOT 0).
    u.hp.iadd(256);
    return [hp, uType] as const;
  });

  // 2. CRITICAL CP reset safety:
  //    CPCursor.restore() leaves CurrentPlayer at CUNIT0_EPD, NOT 0!
  //    Reset CP to P1 (0) before DisplayText / FixedString.displayAt.
  SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
  DisplayText("CUnit0 inspected via CP cursor");

  const hud = new FixedString(48);
  hud.write("HP={:\x1a>5d} TYPE={:\x1a>3d}", hpFromAt, unitType);
  hud.displayAt(1);

  // 3. Zero-CP direct CUnit(EPD(0x59CCA8)) alternative (never touches 0x6509B0)
  const direct = CUnit(CUNIT0_EPD);
  const directHp = direct.hp.read();
  const directType = direct.unitType.read();
  direct.order.set(0);
  when([direct.hp.ge(256)], () => direct.hp.isub(256));
  void directHp;
  void directType;
});
