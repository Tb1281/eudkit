"""12. Containers, camera follow, and engine helpers — eudkit Python DSL.

Migration Guide (eudplib -> eudkit):
  [EN]
  - eudplib: ``EUDQueue(cap)()``, ``EUDDeque(cap)()``, ``EUDStack()(cap)``,
    and ``ObjPool`` live in ``eudplib.collections``, while ``cammove``,
    ``noAirCollision``, and ``unlimiter`` are external euddraft INI plugins.
  - eudkit:
    - ``Queue("dword", 4)``, ``Deque("dword", 4)``, ``Stack(4)``, and
      ``ObjectPool(4)`` share one typed, zero-plugin API (``POOL_NULL``
      on exhaustion; ``lazy_queue`` / ``lazy_stack`` for module-level handles).
    - ``camera_follow(target_loc, cam_loc, inertia=5, maxspeed=48, switch_id=0)``
      is a regular in-code helper replacing euddraft's ``cammove`` plugin.
    - ``turbo()`` writes ``0x6509A0`` (`eudTurbo`), and ``free_list_empty()``
      is a 1-condition CUnit-pool exhaustion check (`0x628438 == 0`).
    - Heavyweight init-loop helpers ``no_air_collision()`` and
      ``unlimiter()`` are available as ordinary DSL calls in the same
      zero-plugin style, but are not demonstrated here (note: arming
      ``unlimiter()`` allocates ~8.4 MiB and runs 679k init steps, so pass
      ``eudkit dump-ir --no-exec`` when inspecting it).

  [KO]
  - eudplib의 ``EUDQueue``/``EUDDeque``/``EUDStack``/``ObjPool``은 컬렉션
    모듈에 있고, ``cammove``/``eudTurbo``/``noAirCollision``/``unlimiter``는
    외부 euddraft 플러그인(INI 설정)으로 동작합니다.
  - eudkit에서는 모두 일반 DSL 함수/클래스로 통일됩니다:
    - ``Queue("dword", 4)``, ``Deque("dword", 4)``, ``Stack(4)``,
      ``ObjectPool(4)``(고갈 시 ``POOL_NULL`` 반환).
    - ``camera_follow(...)``: euddraft ``cammove`` 플러그인 대체.
    - ``turbo()`` (``eudTurbo``), ``free_list_empty()`` (CUnit 여유 슬롯
      고갈 검사), 그리고 대용량 초기화 패치인 ``no_air_collision()`` /
      ``unlimiter()``도 동일하게 모듈 안에서 직접 호출할 수 있습니다(여기서는 시연하지 않음).

External package note: when installed via ``pip install -e ./py``, import
symbols from ``eudkit`` instead of ``py`` (``from eudkit import ...``).
"""

from py import (
    Deque,
    ObjectPool,
    POOL_NULL,
    Queue,
    Stack,
    Var,
    build_module,
    camera_follow,
    cond_not,
    free_list_empty,
    once,
    turbo,
    when,
)


def prog() -> None:
    q = Queue("dword", 4)
    dq = Deque("dword", 4)
    st = Stack(4)
    pool = ObjectPool(4)
    out = Var(0)

    def init_once() -> None:
        q.append(10)
        dq.appendleft(5)
        st.push(77)
        slot = pool.allocate()
        when(
            [slot.ne(POOL_NULL)],
            lambda: (pool.set(slot, 123), out.set(pool.get(slot)), pool.release(slot)),
        )

    once(init_once)

    when([cond_not(q.empty())], lambda: out.set(q.popleft()))
    when([dq.length.ge(1)], lambda: out.set(dq.pop()))
    when([st.length.ge(1), cond_not(free_list_empty())], lambda: out.set(st.pop()))

    camera_follow(0, 1, inertia=5, maxspeed=48, switch_id=0)
    turbo()


module = build_module(prog)
