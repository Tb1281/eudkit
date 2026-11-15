"""Unit-list iteration: walk the global CUnit lists `U` / `T` / `S` / `D`.

The body receives ``(unit_epd, loop)``. ``unit_epd`` is a runtime `Var` holding
the current unit's EPD in eudkit's absolute space, so ``with_epd(unit_epd, ...)``
reads ``CUnit`` fields. Return :data:`BREAK` (or call ``loop.break_()``) to stop
the walk after the current node.

``D`` (dying) is walked **backward** from the tail dword through ``prev``; a
forward walk from the same dword is empty on Remastered and silently misses
every death. The free list ``F`` is not a unit list and is rejected.
"""

from __future__ import annotations

from typing import Any, Callable

from .cp import CP_ADDR, read_current_player_register
from .hir import AddrReloc, emitVarStore
from .temporal import when, while_loop
from .var import Var
from .view import EudslError, in_cunit_pool, read_cunit_pointer_saved

UNIT_LIST_U = "U"
UNIT_LIST_T = "T"
UNIT_LIST_S = "S"
UNIT_LIST_D = "D"

#: Per list: EPD of the dword holding the first node byte pointer and the
#: dword offset from a node's own EPD to its link. ``D`` starts at the tail and
#: walks ``prev`` at ``+0``; the forward lists walk ``next`` at ``+4``.
_UNIT_LISTS = {
    UNIT_LIST_U: (161843, 1),  # 0x628430, next at +4
    UNIT_LIST_T: (161841, 1),  # 0x628428, next at +4
    UNIT_LIST_S: (161844, 1),  # 0x628434, next at +4
    UNIT_LIST_D: (161842, 0),  # 0x62842C, the D *tail*; walk prev from here
}

_FREE_LIST_MESSAGE = (
    "the free list is not a unit list: walking it enumerates unallocated "
    "slots; use free_list_empty() / free_list_walk()"
)


class Break:
    """Named sentinel: return BREAK from a unit-list body to stop the walk."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "BREAK"


BREAK = Break()


class UnitLoop:
    """Build-time handle for one in-flight unit-list walk.

    ``break_()`` emits a single ``done.set(1)`` at the current build point, so
    the author gates it at runtime (``when([...], loop.break_)``). Calling it
    twice emits two acts and still stops the walk once.
    """

    __slots__ = ("list_name", "epd", "_done", "_active")

    def __init__(self, list_name: str, epd: Var, _done: Var | None = None) -> None:
        _validate_list_name(list_name)
        if not isinstance(epd, Var):
            raise EudslError(
                "EUDSL-TYPE",
                f"UnitLoop epd must be a Var; got {epd!r}",
            )
        self.list_name = list_name
        self.epd = epd
        self._done = _done
        self._active = False

    def break_(self) -> None:
        """Emit ``done.set(1)``; valid only while the walk's body is running."""
        if not self._active or self._done is None:
            raise EudslError(
                "EUDSL-TYPE",
                "UnitLoop.break_() may only be called inside a "
                "for_each_unit_list() body",
            )
        self._done.set(1)

    breakLoop = break_

    def _begin(self) -> None:
        self._active = True

    def _end(self) -> None:
        self._active = False

    def _emit_break(self) -> None:
        assert self._done is not None
        self._done.set(1)


def _validate_list_name(list_name: Any) -> None:
    if not isinstance(list_name, str):
        raise EudslError(
            "EUDSL-TYPE",
            "for_each_unit_list list_name must be a plain str in "
            f'{{"U", "T", "S", "D"}}; got {list_name!r}',
        )
    if list_name == "F":
        raise EudslError("EUDSL-BOUNDS", _FREE_LIST_MESSAGE)
    if list_name not in _UNIT_LISTS:
        raise EudslError(
            "EUDSL-TYPE",
            "for_each_unit_list list_name must be one of "
            f'"U", "T", "S", "D"; got {list_name!r}',
        )


def _run_body(body: Callable[[Var, UnitLoop], Any], loop: UnitLoop) -> None:
    loop._begin()
    try:
        result = body(loop.epd, loop)
    finally:
        loop._end()
    if result is BREAK:
        loop._emit_break()


def for_each_unit_list(
    list_name: str,
    body: Callable[[Var, UnitLoop], Any],
    reentrant: bool = False,
) -> None:
    """Walk one global CUnit list, emitting ``body(unit_epd, loop)`` once.

    ``reentrant=False`` reads the successor link **after** the body;
    ``reentrant=True`` reads it **before**, so a body that frees the current
    node still reaches the next one. The ambient CurrentPlayer value is read
    once per site and restored unconditionally after the loop.
    """
    _validate_list_name(list_name)
    if not isinstance(reentrant, bool):
        raise EudslError(
            "EUDSL-TYPE",
            f"for_each_unit_list reentrant must be a plain bool; got {reentrant!r}",
        )
    if not callable(body):
        raise EudslError(
            "EUDSL-TYPE",
            f"for_each_unit_list body must be callable; got {body!r}",
        )

    ptr = Var(0)
    done = Var(0)
    saved = read_current_player_register()
    head, link_off = _UNIT_LISTS[list_name]

    ptr.set(read_cunit_pointer_saved(saved, head))

    def iteration() -> None:
        loop = UnitLoop(list_name, ptr, _done=done)
        link = ptr.add(link_off) if link_off else ptr
        if reentrant:
            nxt = read_cunit_pointer_saved(saved, link)
        _run_body(body, loop)
        if not reentrant:
            nxt = read_cunit_pointer_saved(saved, link)
        when([done.eq(0)], lambda: ptr.set(nxt))

    while_loop([*in_cunit_pool(ptr), done.eq(0)], iteration)
    emitVarStore(saved.id, AddrReloc(CP_ADDR))


forEachUnitList = for_each_unit_list


__all__ = [
    "BREAK",
    "Break",
    "UNIT_LIST_D",
    "UNIT_LIST_S",
    "UNIT_LIST_T",
    "UNIT_LIST_U",
    "UnitLoop",
    "for_each_unit_list",
    "forEachUnitList",
]
