"""CUnit capacity predicates and the tier-3 free-slot scan (ADR 0015 §9).

``0x628438`` is the dword holding the head of the CUnit free list.
``head == 0`` is the exact answer to "the engine has no free CUnit slot right
now", and it costs one condition 15 and no pointer reads. ``0x62843C`` is the
documented free-list tail; no helper is built on it, because the head alone
carries the one-condition answer.

Tier 1 (``free_list_empty``) is a predicate: it returns an ordinary condition
dict, not a wrapped ``Trigger``, so it composes into the caller's
``when``/``once``/``until`` gate and consumes one of that gate's 16 condition
slots. It is advisory, not authoritative: the engine's allocator can still
refuse a creation for reasons this dword cannot see.

Tier 3 (``free_list_walk`` / ``free_slot_count``) is the exact count: a
one-shot forward walk of the free list from the same head through each node's
``next`` link (``ptr + 1``). It costs ~37T executed per free node, so the
API contract is "once per module, never per tick" and is enforced at build
time — a second call site in one module is ``EUDSL-BOUNDS``.

TypeScript mirror: ``ts/capacity.ts``.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable

from .cp import CP_ADDR, read_current_player_register
from .hir import AddrReloc, Exactly, Memory, emitVarStore
from .temporal import when, while_loop
from .var import Var
from .view import (
    CUNIT_SLOT_COUNT,
    EPD,
    EudslError,
    _require_plain_int,
    in_cunit_pool,
    read_cunit_pointer_saved,
)

__all__ = [
    "CUNIT_FREE_HEAD",
    "CUNIT_FREE_SCAN_MAX",
    "CUNIT_FREE_TAIL",
    "freeListEmpty",
    "freeListWalk",
    "freeSlotCount",
    "free_list_empty",
    "free_list_walk",
    "free_slot_count",
]

#: Dword holding the head of the CUnit free list.
CUNIT_FREE_HEAD = 0x628438
#: Dword holding the tail of the CUnit free list (documented, no helper).
CUNIT_FREE_TAIL = 0x62843C
#: Hard stop for the tier-3 walk: the pool has 1700 slots, so a cycle can
#: never legitimately outlast it.
CUNIT_FREE_SCAN_MAX = CUNIT_SLOT_COUNT

#: One-shot bookkeeping: builder -> number of tier-3 call sites claimed.
#: A weak key keeps a finished builder (and its module) collectable.
_TIER3_CALL_SITES: "weakref.WeakKeyDictionary[Any, int]" = (
    weakref.WeakKeyDictionary()
)

_ONE_SHOT_MESSAGE = (
    "free_list_walk()/free_slot_count() is a tier-3 one-shot scan; "
    "one call site per module (ADR 0015 §9)"
)


def free_list_empty() -> Any:
    """1C, 0 reads: the CUnit free list has no node right now.

    Advisory, not authoritative: the engine's allocator can still refuse a
    creation for reasons this dword cannot see. It answers exactly one
    question — "is the free list empty at this instant" — and nothing else.
    """
    return Memory(CUNIT_FREE_HEAD, Exactly, 0)


def _claim_tier3_call_site() -> None:
    """Reserve this module's single tier-3 call site, or reject the call.

    The check must run before any emission: a rejected call leaves no HIR.
    Outside a builder the counter is skipped so the ordinary ``EUDSL-SCOPE``
    path (``Var`` construction) reports the real problem.
    """
    from .trigger import _get_active_module_builder

    builder = _get_active_module_builder()
    if builder is None:
        return
    count = _TIER3_CALL_SITES.get(builder, 0) + 1
    _TIER3_CALL_SITES[builder] = count
    if count > 1:
        raise EudslError("EUDSL-BOUNDS", _ONE_SHOT_MESSAGE)


def free_list_walk(
    body: Callable[[], Any],
    *,
    max_nodes: int = CUNIT_FREE_SCAN_MAX,
) -> None:
    """Walk the free list once, calling ``body()`` per free node.

    This is tier 3: the exact count, independent of any tracker, at
    ~37T executed per free node. Use it once per module (map start or on
    author request); a per-frame capacity check is ``free_list_empty()``.

    ``max_nodes`` is a plain int in ``1..1700`` and is also the cycle guard:
    the walk stops on a null pointer, a pointer outside the CUnit pool, or
    ``max_nodes`` iterations — whichever comes first — so a corrupted list
    costs at most ``1700 × 37T`` and never hangs the game. Return
    :data:`py.units.BREAK` from ``body`` to stop after the current node.
    """
    if not callable(body):
        raise EudslError(
            "EUDSL-TYPE",
            f"free_list_walk body must be callable; got {body!r}",
        )
    limit = _require_plain_int(max_nodes, "free_list_walk max_nodes")
    if not 1 <= limit <= CUNIT_FREE_SCAN_MAX:
        raise EudslError(
            "EUDSL-BOUNDS",
            "free_list_walk max_nodes must be in "
            f"1..{CUNIT_FREE_SCAN_MAX}; got {limit}",
        )
    _claim_tier3_call_site()
    _emit_free_list_walk(body, limit)


def _emit_free_list_walk(body: Callable[[], Any], limit: int) -> None:
    """Emit the tier-3 walk; the caller owns validation and the call-site claim."""
    from .units import BREAK

    ptr = Var(0)
    done = Var(0)
    remaining = Var(0)
    saved = read_current_player_register()

    remaining.set(limit)
    ptr.set(read_cunit_pointer_saved(saved, EPD(CUNIT_FREE_HEAD)))

    def iteration() -> None:
        # Read the successor before the body: the free list mutates when a
        # unit is created, so a body that allocates the current node must
        # still reach the next one.
        nxt = read_cunit_pointer_saved(saved, ptr.add(1))
        result = body()
        if result is BREAK:
            done.set(1)
        remaining.isub(1)
        when([remaining.eq(0)], lambda: done.set(1))
        when([done.eq(0)], lambda: ptr.set(nxt))

    while_loop([*in_cunit_pool(ptr), done.eq(0)], iteration)
    emitVarStore(saved.id, AddrReloc(CP_ADDR))


def free_slot_count() -> Var:
    """Exact number of free CUnit nodes right now (tier 3, one-shot).

    Uses the same emitter and call-site budget as :func:`free_list_walk`, so
    calling both in one module is ``EUDSL-BOUNDS``. The call-site claim runs
    **before** the count `Var` is allocated, so a rejected call leaves no HIR.
    """
    _claim_tier3_call_site()
    count = Var(0)
    _emit_free_list_walk(lambda: count.iadd(1), CUNIT_FREE_SCAN_MAX)
    return count


freeListEmpty = free_list_empty
freeListWalk = free_list_walk
freeSlotCount = free_slot_count
