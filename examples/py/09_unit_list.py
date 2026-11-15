"""09. Unit-list iteration (`U` / `T` / `S` / `D`) — eudkit Python DSL.

Migration Guide (eudplib -> eudkit):
  [EN]
  - eudplib: ``for ptr, epd in EUDLoopUnit():`` is a runtime linked-list walk
    with a 32-bit shared pointer reader; ``EUDLoopUnit`` only covers the live
    ``U`` list, and dying units need a separate loop.
  - eudkit: ``for_each_unit_list("U", body)`` emits the body once and walks
    the global CUnit list at run time. The body receives ``(unit_epd, loop)``;
    ``unit_epd`` is a runtime ``Var`` in absolute EPD space, so
    ``with_epd(unit_epd, ...)`` reads ``CUnit`` fields.
  - Return the ``BREAK`` singleton (or call ``loop.break_()``) to stop the
    walk. ``True``/``1`` continue: ``BREAK`` is a named sentinel, not a bool.
  - ``D`` (dying) is walked backward from the tail through ``prev``; the free
    list ``F`` is rejected because it enumerates unallocated slots.

  [KO]
  - eudplib의 ``EUDLoopUnit()``은 런타임 연결 리스트 순회이며 live ``U``
    목록만 다룹니다. eudkit의 ``for_each_unit_list("U", body)``는 본문을 한 번
    방출하고 런타임에 전역 CUnit 목록을 순회합니다.
  - 본문은 ``(unit_epd, loop)``를 받고 ``unit_epd``는 절대 EPD 공간의 런타임
    ``Var``이므로 ``with_epd(unit_epd, ...)``로 ``CUnit`` 필드를 읽습니다.
  - 중단하려면 ``BREAK`` 싱글턴을 반환하거나 ``loop.break_()``를 호출합니다.
    ``True``/``1``은 계속 순회합니다(불리언이 아닌 이름 있는 센티널).
  - ``D``(dying)는 tail에서 ``prev``로 역방향 순회하며, free list ``F``는
    미할당 슬롯을 열거하므로 거부됩니다.

External package note: when installed via ``pip install -e ./py``, import
symbols from ``eudkit`` instead of ``py`` (``from eudkit import ...``).
"""

from py import (
    UNIT_LIST_U,
    Var,
    build_module,
    for_each_unit_list,
    when,
    with_epd,
)


def prog() -> None:
    visited = Var(0)

    def visit(unit_epd: Var, loop) -> None:
        visited.iadd(1)
        # hp is CUnit +8 bytes = dword offset 2. Stop after the first dead
        # unit; the break is gated behind the runtime hp check.
        with_epd(
            unit_epd,
            lambda cp: when([cp.at(2).read().eq(0)], loop.break_),
        )

    for_each_unit_list(UNIT_LIST_U, visit)


module = build_module(prog)
