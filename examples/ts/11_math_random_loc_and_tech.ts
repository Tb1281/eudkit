/**
 * 11. Math, random, locations, and tech/upgrades — eudkit TypeScript DSL.
 *
 * Migration Guide (eudplib -> eudkit):
 *   [EN]
 *   - eudplib: math (`f_div_floor`, `f_sqrt`, `f_pow`, `f_atan2`, `f_lengthdir`,
 *     `EUDBinaryMin`), LCG RNG (`f_srand`, `f_randomize`, `f_dwrand`), locations
 *     (`f_setloc`, `f_getlocTL`), and `TrgPlayer.tech`/`upgrade` are spread
 *     across `eudlib` and `scdata`.
 *   - eudkit: unified core exports:
 *     - `divFloor` (plus `divTowardsZero`/`divEuclid`), `isqrt`, `pow`,
 *       `atan2`/`lengthdir` (backed by lazy shared trig tables per builder),
 *       and `binaryMin`/`binaryMax`.
 *     - `srand`, `randomize`, `rand`, `dwrand`, `randRange` with builder-local
 *       LCG state (no global seed collisions across modules).
 *     - `Location(0)` OOP wrapper (`setRect`, `move`, `center`, `topLeft`),
 *       `getlocTl`, and compile-time shape drawers (`drawCircle`, `drawLine`,
 *       `drawPolygon`, `drawStar`).
 *     - `techResearched`, `setTechResearched`, `upgradeLevel`,
 *       `setUpgradeLevel` over SC/BW split byte tables (`0..43` / `0..60`).
 *
 *   [KO]
 *   - eudplib에서는 수학(`f_sqrt`, `f_atan2`, `EUDBinaryMin`), 난수(`f_srand`,
 *     `f_randomize`), 로케이션(`f_setloc`, `f_getlocTL`), 테크/업그레이드가
 *     여러 서브패키지에 나뉘어 있습니다.
 *   - eudkit은 루트 모듈에서 직접 제공합니다:
 *     - `divFloor`/`isqrt`/`pow`/`atan2`/`lengthdir`/`binaryMin`: 빌더별
 *       공유 삼각함수 테이블을 자동 방출합니다.
 *     - `srand`/`randomize`/`randRange`: 빌더 격리형 LCG 상태를 사용합니다.
 *     - `Location(0)` 래퍼와 `getlocTl`, 빌드 타임 도형 생성기(`drawCircle` 등).
 *     - `techResearched`/`setTechResearched`/`upgradeLevel`/
 *       `setUpgradeLevel`로 SC/BW 분할 바이트 테이블을 직접 읽고 씁니다.
 *
 * External package note: in standalone npm projects using `"eudkit"`, import
 * from `"eudkit"` instead of `"../../ts/index.ts"`.
 */

import {
  Location,
  UnitType,
  Var,
  atan2,
  binaryMin,
  buildModule,
  divFloor,
  drawCircle,
  getlocTl,
  isqrt,
  lengthdir,
  once,
  pow,
  randRange,
  randomize,
  setTechResearched,
  setUpgradeLevel,
  srand,
  techResearched,
  upgradeLevel,
  when,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  const vx = Var(40);
  const vy = Var(30);
  const base = Var(3);
  const negSeven = Var(0xffffffff - 6); // two's-complement -7

  const q = divFloor(negSeven, 3);
  const root = isqrt(vx.mul(vx).add(vy.mul(vy)), 16);
  const p = pow(base, 3);
  const angle = atan2(vy, vx);
  const [dx, dy] = lengthdir(100, angle);
  const found = binaryMin(0, 15, (x) => x.ge(10));

  srand(42);
  randomize();
  const roll = randRange(1, 6);

  const loc = new Location(0);
  loc.setRect(64, 64, 192, 192);
  const [lx, ly] = getlocTl(0);

  // Emit 4 Marines on a build-time circle using Location 1 once.
  once(() =>
    drawCircle(1, UnitType.TerranMarine, 0, 256, 256, { radius: 64, dots: 4 }),
  );

  const out = Var(0);
  when([found.ge(10)], () =>
    out.set(q.add(root).add(p).add(dx).add(dy).add(roll).add(lx).add(ly)),
  );

  // Tech / upgrade state (0 = Stim Packs, 0 = upgrade slot 0).
  once(() => {
    setUpgradeLevel(0, 0, 2);
    setTechResearched(0, 0, true);
  });
  const upLvl = upgradeLevel(0, 0);
  when([techResearched(0, 0), upLvl.ge(2)], () => out.iadd(1));
});
