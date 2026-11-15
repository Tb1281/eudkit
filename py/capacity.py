"""CUnit capacity predicates: the one-condition free-list truth (ADR 0015 §9).

``0x628438`` is the dword holding the head of the CUnit free list.
``head == 0`` is the exact answer to "the engine has no free CUnit slot right
now", and it costs one condition 15 and no pointer reads. ``0x62843C`` is the
documented free-list tail; no helper is built on it, because the head alone
carries the one-condition answer.

The result is an ordinary condition dict, not a wrapped ``Trigger``: it composes
into the caller's ``when``/``once``/``until`` gate and consumes one of that
gate's 16 condition slots. This is tier 1 of the capacity vocabulary in ADR
0015 §9. It is advisory, not authoritative: the engine's allocator can still
refuse a creation for reasons this dword cannot see.

TypeScript mirror: ``ts/capacity.ts``.
"""

from __future__ import annotations

from typing import Any

from .hir import Exactly, Memory

__all__ = [
    "CUNIT_FREE_HEAD",
    "CUNIT_FREE_TAIL",
    "freeListEmpty",
    "free_list_empty",
]

#: Dword holding the head of the CUnit free list.
CUNIT_FREE_HEAD = 0x628438
#: Dword holding the tail of the CUnit free list (documented, no helper).
CUNIT_FREE_TAIL = 0x62843C


def free_list_empty() -> Any:
    """1C, 0 reads: the CUnit free list has no node right now.

    Advisory, not authoritative: the engine's allocator can still refuse a
    creation for reasons this dword cannot see. It answers exactly one
    question — "is the free list empty at this instant" — and nothing else.
    """
    return Memory(CUNIT_FREE_HEAD, Exactly, 0)


freeListEmpty = free_list_empty
