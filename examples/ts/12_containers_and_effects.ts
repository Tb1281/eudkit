/**
 * 12. Containers, camera follow, and engine helpers — eudkit TypeScript DSL.
 *
 * Migration Guide (eudplib -> eudkit):
 *   [EN]
 *   - eudplib: `EUDQueue(cap)()`, `EUDDeque(cap)()`, `EUDStack()(cap)`,
 *     and `ObjPool` live in `eudplib.collections`, while `cammove`,
 *     `noAirCollision`, and `unlimiter` are external euddraft INI plugins.
 *   - eudkit:
 *     - `new Queue("dword", 4)`, `new Deque("dword", 4)`, `new Stack(4)`, and
 *       `new ObjectPool(4)` share one typed, zero-plugin API (`POOL_NULL` on
 *       exhaustion; `lazyQueue` / `lazyStack` for module-level handles).
 *     - `cameraFollow(targetLoc, camLoc, inertia, maxSpeed, switchId)` is a
 *       regular in-code helper replacing euddraft's `cammove` plugin.
 *     - `turbo()` writes `0x6509A0` (`eudTurbo`), and `freeListEmpty()` is a
 *       1-condition CUnit-pool exhaustion check (`0x628438 == 0`).
 *     - Heavyweight init-loop helpers `noAirCollision()` and `unlimiter()`
 *       follow the same zero-plugin pattern (note: `unlimiter()` emits ~8.4 MiB
 *       payload and runs 679k init steps, so pass `eudkit dump-ir --no-exec`
 *       when inspecting it).
 *
 *   [KO]
 *   - eudplib의 `EUDQueue`/`EUDDeque`/`EUDStack`/`ObjPool`은 컬렉션 모듈에
 *     있고, `cammove`/`eudTurbo`/`noAirCollision`/`unlimiter`는 외부
 *     euddraft 플러그인(INI 설정)으로 동작합니다.
 *   - eudkit에서는 모두 일반 DSL 함수/클래스로 통일됩니다:
 *     - `new Queue("dword", 4)`, `new Deque("dword", 4)`, `new Stack(4)`,
 *       `new ObjectPool(4)`(고갈 시 `POOL_NULL` 반환).
 *     - `cameraFollow(...)`: euddraft `cammove` 플러그인 대체.
 *     - `turbo()` (`eudTurbo`), `freeListEmpty()` (CUnit 여유 슬롯 고갈
 *       검사), 그리고 대용량 초기화 패치인 `noAirCollision()` /
 *       `unlimiter()`도 동일하게 모듈 안에서 직접 호출합니다.
 *
 * External package note: in standalone npm projects using `"eudkit"`, import
 * from `"eudkit"` instead of `"../../ts/index.ts"`.
 */

import {
  Deque,
  ObjectPool,
  POOL_NULL,
  Queue,
  Stack,
  Var,
  buildModule,
  cameraFollow,
  condNot,
  freeListEmpty,
  once,
  turbo,
  when,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  const q = new Queue("dword", 4);
  const dq = new Deque("dword", 4);
  const st = new Stack(4);
  const pool = new ObjectPool(4);
  const out = Var(0);

  once(() => {
    q.append(10);
    dq.appendleft(5);
    st.push(77);
    const slot = pool.allocate();
    when([slot.ne(POOL_NULL)], () => {
      pool.set(slot, 123);
      out.set(pool.get(slot) as Var);
      pool.release(slot);
    });
  });

  when([condNot(q.empty())], () => out.set(q.popleft() as Var));
  when([dq.length.ge(1)], () => out.set(dq.pop() as Var));
  when([st.length.ge(1), condNot(freeListEmpty())], () => out.set(st.pop()));

  cameraFollow(0, 1, 5, 48, 0);
  turbo();
});
