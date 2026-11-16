"""10. Players, PVariable, and local broadcasts — eudkit Python DSL.

Migration Guide (eudplib -> eudkit):
  [EN]
  - eudplib: ``for p in EUDLoopPlayer():`` iterates active players without
    changing ``CurrentPlayer``, and ``IsUserCP()`` checks CP against
    ``f_getuserplayerid()``. ``DisplayTextAll`` targets ``f_getuserplayerid()``.
  - eudkit: ``for_each_player(body)`` walks active players ``0..7`` at runtime
    (``active_only=True``, checking ``player_exists(p) == 1``) or unrolls an
    explicit ``players=[...]`` list, **without modifying ``CurrentPlayer``**.
  - ``user_player()`` / ``is_user(p)`` query the local client directly without
    binding ``CurrentPlayer``.
  - ``display_text_all`` / ``play_wav_all`` / ``minimap_ping_all`` /
    ``center_view_all`` / ``set_mission_objectives_all`` /
    ``talking_portrait_all`` temporarily switch ``CurrentPlayer`` to
    ``user_player()`` (covering observers as well as active slots) and restore
    the ambient ``CurrentPlayer`` immediately afterward.
  - ``Player(0)`` provides typed access to resources/scores, ``PVariable``
    allocates 8-slot per-player storage, and ``match_names`` emits an exact
    UTF-8 nickname matcher (``player: int, name: str``).

  [KO]
  - eudplib의 ``EUDLoopPlayer()``는 ``CurrentPlayer``를 바꾸지 않고 활성
    플레이어를 순회하며, ``IsUserCP()``는 CP를 ``f_getuserplayerid()``와
    비교합니다.
  - eudkit의 ``for_each_player(body)``도 ``CurrentPlayer``를 건드리지 않고
    런타임 ``0..7`` 루프(``active_only=True``) 또는 지정한 ``players=[...]``
    목록을 언롤해 실행합니다.
  - ``user_player()`` / ``is_user(p)``는 ``CurrentPlayer``를 바꾸지 않고
    로컬 클라이언트 ID를 직접 읽거나 비교합니다.
  - ``display_text_all`` 등 ``*_all`` 브로드캐스트 함수들은 ``CurrentPlayer``를
    잠시 ``user_player()``(옵저버 포함)로 바꿨다가 즉시 복원합니다.
  - ``Player(0)``은 자원/점수 필드 타입 뷰를 제공하고, ``PVariable``은
    8슬롯 플레이어 변수, ``match_names``는 UTF-8 닉네임 매처를 방출합니다.

External package note: when installed via ``pip install -e ./py``, import
symbols from ``eudkit`` instead of ``py`` (``from eudkit import ...``).
"""

from py import (
    PVariable,
    Player,
    Var,
    build_module,
    display_text_all,
    for_each_player,
    is_user,
    match_names,
    once,
    player_exists,
    when,
)


def prog() -> None:
    bonus = PVariable(10)
    local_seen = Var(0)
    p1 = Player(0)

    def add_bonus(player: Var) -> None:
        current = bonus.get(player)
        current.iadd(1)
        bonus.set(player, current)

    # Runtime 0..7 active player loop (CurrentPlayer untouched).
    for_each_player(add_bonus)

    # Unrolled compile-time subset + explicit playerExists / isUser checks.
    for_each_player(
        lambda p: bonus.set(p, bonus.get(p)),
        active_only=False,
        players=[0, 1],
    )
    when([player_exists(0).eq(1), is_user(0)], lambda: local_seen.set(1))

    # Exact UTF-8 nickname match (passes compile-time player int 0..7).
    once(
        lambda: match_names(
            ["Hero"],
            lambda player, _name: Player(player).minerals.iadd(50),
            players=[0, 1],
        )
    )
    once(lambda: when([p1.minerals.ge(0)], lambda: display_text_all("Active players checked!")))


module = build_module(prog)
