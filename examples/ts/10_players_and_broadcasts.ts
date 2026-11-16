/**
 * 10. Players, PVariable, and local broadcasts — eudkit TypeScript DSL.
 *
 * Migration Guide (eudplib -> eudkit):
 *   [EN]
 *   - eudplib: `for p in EUDLoopPlayer():` iterates active players without
 *     changing `CurrentPlayer`, and `IsUserCP()` checks CP against
 *     `f_getuserplayerid()`. `DisplayTextAll` targets `f_getuserplayerid()`.
 *   - eudkit: `forEachPlayer(body)` walks active players `0..7` at runtime
 *     (`activeOnly: true`, checking `playerExists(p) == 1`) or unrolls an
 *     explicit `players: [...]` list, **without modifying `CurrentPlayer`**.
 *   - `userPlayer()` / `isUser(p)` query the local client directly without
 *     binding `CurrentPlayer`.
 *   - `displayTextAll` / `playWavAll` / `minimapPingAll` / `centerViewAll` /
 *     `setMissionObjectivesAll` / `talkingPortraitAll` temporarily switch
 *     `CurrentPlayer` to `userPlayer()` (covering observers as well as active
 *     slots) and restore the ambient `CurrentPlayer` immediately afterward.
 *   - `Player(0)` provides typed access to resources/scores, `PVariable`
 *     allocates 8-slot per-player storage, and `matchNames` emits an exact
 *     UTF-8 nickname matcher (`player: number, name: string`).
 *
 *   [KO]
 *   - eudplib의 `EUDLoopPlayer()`는 `CurrentPlayer`를 바꾸지 않고 활성
 *     플레이어를 순회하며, `IsUserCP()`는 CP를 `f_getuserplayerid()`와
 *     비교합니다.
 *   - eudkit의 `forEachPlayer(body)`도 `CurrentPlayer`를 건드리지 않고
 *     런타임 `0..7` 루프(`activeOnly: true`) 또는 지정한 `players: [...]`
 *     목록을 언롤해 실행합니다.
 *   - `userPlayer()` / `isUser(p)`는 `CurrentPlayer`를 바꾸지 않고 로컬
 *     클라이언트 ID를 직접 읽거나 비교합니다.
 *   - `displayTextAll` 등 `*All` 브로드캐스트 함수들은 `CurrentPlayer`를 잠시
 *     `userPlayer()`(옵저버 포함)로 바꿨다가 즉시 복원합니다.
 *   - `Player(0)`은 자원/점수 필드 타입 뷰를 제공하고, `PVariable`은
 *     8슬롯 플레이어 변수, `matchNames`는 UTF-8 닉네임 매처를 방출합니다.
 *
 * External package note: in standalone npm projects using `"eudkit"`, import
 * from `"eudkit"` instead of `"../../ts/index.ts"`.
 */

import {
  PVariable,
  Player,
  Var,
  buildModule,
  displayTextAll,
  forEachPlayer,
  isUser,
  matchNames,
  once,
  playerExists,
  when,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  const bonus = new PVariable(10);
  const localSeen = Var(0);
  const p1 = new Player(0);

  // Runtime 0..7 active player loop (CurrentPlayer untouched).
  forEachPlayer((player) => {
    const current = bonus.get(player);
    current.iadd(1);
    bonus.set(player, current);
  });

  // Unrolled compile-time subset + explicit playerExists / isUser checks.
  forEachPlayer(
    (p) => bonus.set(p, bonus.get(p)),
    { activeOnly: false, players: [0, 1] },
  );
  when([playerExists(0).eq(1), isUser(0)], () => localSeen.set(1));

  // Exact UTF-8 nickname match (passes compile-time player index 0..7).
  once(() =>
    matchNames(
      ["Hero"],
      (player, _name) => new Player(player).minerals.iadd(50),
      [0, 1],
    ),
  );
  once(() =>
    when([p1.minerals.ge(0)], () =>
      displayTextAll("Active players checked!"),
    ),
  );
});
