"""The unlimiter: a core helper port of euddraft's ``plugins/unlimiter.py``.

The plugin replaces four grow-only game lists (bullets, sprites, images and
orders) with lists backed by module data blobs, so the game can keep
allocating entries past its vanilla caps. ``connect_dlist(el_start, el_end,
item_size, item_n, is_sprite)`` builds one such list on the trigger loop the
first pass runs, and the helper's last act rewrites one dword every loop.

This module is a regular core helper: no plugin system, no settings manifest
and no lifecycle hook. One :func:`unlimiter` call inside ``build_module`` arms
the four lists for the compiled module; :func:`connect_dlist` is the
underlying primitive for the rare caller that wants one list only.

List layout
-----------
Each element is a node of a doubly-linked list in StarCraft's node order:
``prev`` at ``+0`` and ``next`` at ``+4`` (the same order ``CUnit`` /
``CSprite`` use). For ``n`` elements at stride ``size`` starting at element 0:

* ``element[k].prev = element[k - 1]`` for ``k >= 1`` and ``element[0].prev = 0``;
* ``element[k].next = element[k + 1]`` for ``k < n - 1`` and
  ``element[n - 1].next = 0``;
* ``el_start`` points at element 0, ``el_end`` at ``element[n - 1]``.

The four lists the plugin connects are pinned as module constants:

===============  ==========  =========  ========  =========
list             head        tail       stride    elements
===============  ==========  =========  ========  =========
bullets          0x64EED8    0x64EEDC   112       8192
sprites          0x63FE30    0x63FE34   36        65536
images           0x57EB68    0x57EB70   64        65536
orders           0x64B2E0    0x64B2E4   20        65536
===============  ==========  =========  ========  =========

Sprite elements also get ``+0x10 = 10000``; the source patches that act's
value by ``+1`` per element, so the field runs ``10000, 10001, ...`` in list
order. The four blobs total 8,782,288 bytes of zero-filled payload
(8.38 MiB): the helper is a payload-size cost as much as a trigger cost.

How the loop is ported
----------------------
``EUDLoopN()(itemN)`` is a *runtime* loop: the source emits one body and
advances its own ``SetMemory`` actions by patching the action's ``dest``
(``+16``, an EPD delta of ``itemSize / 4``) and ``value`` (``+20``, a byte
address delta of ``itemSize``) after each write. The port keeps that shape with
``while_loop`` and eudkit's action-slot references: :meth:`Trigger.act`
exposes ``act(k).dest`` / ``.value`` as relocatable fields, and
``SetMemoryEPD`` (the field-reference form of the Deaths write) emits the same
additive patches.

The source puts the writes and their own patches in one ``DoActions`` trigger,
so a single evaluation writes element *k* and then advances the actions for
element *k + 1*. eudkit action slots are only addressable once the trigger
exists, so the port emits the same iterations as back-to-back triggers: write
trigger, optional sprite trigger, one shared patch block, then the loop check.
The write/patch order per element is unchanged; the split costs one extra
trigger evaluation per element (accounted for in the cost model below).

One deviation is forced by eudkit's HIR:

* **One padding element on each side of the list.** The source seeds element
  0's ``prev`` with ``dataDb - itemSize`` (one element *before* the blob) and
  the last element's ``next`` with one element *after* it. eudkit blob
  relocations are non-negative offsets, so the blob reserves a padding element
  on both sides (``itemSize * (itemN + 2)`` bytes) and the list starts at
  ``db + itemSize``: both seeds address in-blob padding, and the two dangling
  pointers are still overwritten with 0 by the final patch, exactly as in the
  source. No read ever observes a padding element.

Arming must happen inside a single loop pass: the patches that advance the
write actions only take effect on the *next* evaluation of the write trigger,
and the tail zeroes the two dangling pointers right after the loop. Both hold
for the source's ``EUDLoopN`` too — the loop is intra-pass, not spread across
frames.

Because the pass is long, ``eudkit dump-ir`` (which simulates one loop by
default) trips the simulator's 100,000-evaluation guard on a module that arms
the helper; use ``dump-ir --no-exec`` for the static report or
``compile --check-cost``. An unlimiter-only module is 22T / 4C / 64A with
8,782,304 blob bytes (8,782,288 payload plus the four-entry blob table).

The per-loop ``SetMemory(0x64DEBC, SetTo, 40)`` is emitted as a top-level act
by :func:`unlimiter`; call it after the module's other statements so the write
stays last, matching ``afterTriggerExec``.

Cost
----
Static cost is tiny (four loop counters, the gate/check pairs, three or four
triggers per element shape and the tails), while arming the helper costs one
trigger loop pass: 679,936 evaluations (204,800 loop checks + 204,800 write
triggers + 204,800 patch blocks + 65,536 sprite write triggers). That is one
extra trigger evaluation per element over the source's combined write+patch
trigger; the payload bytes above are the part that shows up in the map file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .hir import Add, Db, SetMemory, SetMemoryEPD, SetTo
from .temporal import once, while_loop
from .trigger import Trigger
from .var import Var
from .view import EudslError, _require_plain_int

#: Bullet list head/tail dwords.
BULLET_LIST_ADDR = 0x64EED8
BULLET_LIST_END_ADDR = 0x64EEDC
BULLET_ITEM_SIZE = 112
BULLET_ITEM_COUNT = 8192

#: Sprite list head/tail dwords.
SPRITE_LIST_ADDR = 0x63FE30
SPRITE_LIST_END_ADDR = 0x63FE34
SPRITE_ITEM_SIZE = 36
SPRITE_ITEM_COUNT = 65536
#: Sprite element field the plugin seeds with :data:`SPRITE_FIELD_SEED`.
SPRITE_FIELD_OFFSET = 0x10
SPRITE_FIELD_SEED = 10000

#: Image list head/tail dwords.
IMAGE_LIST_ADDR = 0x57EB68
IMAGE_LIST_END_ADDR = 0x57EB70
IMAGE_ITEM_SIZE = 64
IMAGE_ITEM_COUNT = 65536

#: Order list head/tail dwords.
ORDER_LIST_ADDR = 0x64B2E0
ORDER_LIST_END_ADDR = 0x64B2E4
ORDER_ITEM_SIZE = 20
ORDER_ITEM_COUNT = 65536

#: Dword the plugin rewrites on every trigger loop in ``afterTriggerExec``.
PER_TICK_ADDR = 0x64DEBC
#: Value written to :data:`PER_TICK_ADDR` every loop.
PER_TICK_VALUE = 40

#: Fields of one linked-list node: ``prev`` at ``+0``, ``next`` at ``+4``.
PREV_FIELD_OFFSET = 0
NEXT_FIELD_OFFSET = 4
#: Smallest node stride that holds both link fields.
MIN_ITEM_SIZE = NEXT_FIELD_OFFSET + 4


@dataclass(frozen=True)
class UnlimitedLists:
    """The four data blobs backing the connected game lists."""

    bullets: Db
    sprites: Db
    images: Db
    orders: Db


def _require_list_addr(name: str, value: Any) -> int:
    value = _require_plain_int(value, f"connect_dlist(): {name}")
    if value <= 0 or value > 0xFFFFFFFC or value % 4 != 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"connect_dlist(): {name} must be a positive 4-byte-aligned "
            f"address, got {value}",
        )
    return value


def _require_item_size(value: Any, is_sprite: bool) -> int:
    value = _require_plain_int(value, "connect_dlist(): item_size")
    minimum = MIN_ITEM_SIZE
    if is_sprite:
        minimum = max(minimum, SPRITE_FIELD_OFFSET + 4)
    if value < minimum or value % 4 != 0 or value > 0xFFFF:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"connect_dlist(): item_size must be a multiple of 4 in "
            f"[{minimum}, {0xFFFF}], got {value}",
        )
    return value


def _require_item_count(value: Any) -> int:
    value = _require_plain_int(value, "connect_dlist(): item_n")
    if value < 1 or value > 0xFFFFFFFF:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"connect_dlist(): item_n must be in [1, {0xFFFFFFFF}], got {value}",
        )
    return value


def _require_sprite_flag(value: Any) -> bool:
    value = _require_plain_int(value, "connect_dlist(): is_sprite")
    if value not in (0, 1):
        raise EudslError(
            "EUDSL-BOUNDS",
            f"connect_dlist(): is_sprite must be 0 or 1, got {value}",
        )
    return value == 1


def connect_dlist(
    el_start: int,
    el_end: int,
    item_size: int,
    item_n: int,
    is_sprite: int = 0,
) -> Db:
    """Build one unlimiter list in a data blob and patch its head/tail.

    ``el_start``/``el_end`` are the game's list head and tail dwords,
    ``item_size`` the node stride in bytes, ``item_n`` the element count and
    ``is_sprite`` selects the sprite variant (an extra ``+0x10 = 10000`` field
    per element, incremented per element). The body runs once, on the first
    trigger loop (eudplib's ``onPluginStart``), and returns the blob holding
    the list.
    """
    is_sprite_flag = _require_sprite_flag(is_sprite)
    el_start = _require_list_addr("el_start", el_start)
    el_end = _require_list_addr("el_end", el_end)
    item_size = _require_item_size(item_size, is_sprite_flag)
    item_n = _require_item_count(item_n)

    stride = item_size // 4
    # One padding element before and after the list; see the module docstring.
    data = Db(item_size * (item_n + 2))
    base = data.at(item_size)
    last = data.at(item_size * item_n)

    def build() -> None:
        counter = Var(item_n)

        def body() -> None:
            writes = Trigger(
                conds=[],
                acts=[
                    SetMemory(base + PREV_FIELD_OFFSET, SetTo, data),
                    SetMemory(
                        base + NEXT_FIELD_OFFSET, SetTo, data.at(2 * item_size)
                    ),
                ],
            )
            # The source emits the sprite write before the patch block, and a
            # trigger's slots are only addressable after it exists, so the
            # sprite write trigger is created here too. All patches then share
            # the next trigger of this iteration (see the module docstring).
            sprite = None
            if is_sprite_flag:
                sprite = Trigger(
                    conds=[],
                    acts=[
                        SetMemory(
                            base + SPRITE_FIELD_OFFSET,
                            SetTo,
                            SPRITE_FIELD_SEED,
                        )
                    ],
                )

            SetMemoryEPD(writes.act(0).dest, Add, stride)
            SetMemoryEPD(writes.act(0).value, Add, item_size)
            SetMemoryEPD(writes.act(1).dest, Add, stride)
            SetMemoryEPD(writes.act(1).value, Add, item_size)
            if sprite is not None:
                SetMemoryEPD(sprite.act(0).dest, Add, stride)
                SetMemoryEPD(sprite.act(0).value, Add, 1)

            counter.isub(1)

        while_loop(counter.ge(1), body)

        # `onPluginStart` tail: point the head/tail dwords at the blob and
        # terminate the list with the two zero pointers.
        SetMemory(el_start, SetTo, base)
        SetMemory(base + PREV_FIELD_OFFSET, SetTo, 0)
        SetMemory(el_end, SetTo, last)
        SetMemory(last + NEXT_FIELD_OFFSET, SetTo, 0)

    once(build)
    return data


def unlimiter() -> UnlimitedLists:
    """Connect the four unlimiter lists and rewrite ``0x64DEBC`` every loop.

    Equivalent to the plugin's ``onPluginStart`` plus ``afterTriggerExec``.
    Call it once at module top level, after the module's other statements, so
    the per-loop write stays last. Returns the four backing blobs.
    """
    lists = UnlimitedLists(
        bullets=connect_dlist(
            BULLET_LIST_ADDR, BULLET_LIST_END_ADDR, BULLET_ITEM_SIZE, BULLET_ITEM_COUNT
        ),
        sprites=connect_dlist(
            SPRITE_LIST_ADDR,
            SPRITE_LIST_END_ADDR,
            SPRITE_ITEM_SIZE,
            SPRITE_ITEM_COUNT,
            1,
        ),
        images=connect_dlist(
            IMAGE_LIST_ADDR, IMAGE_LIST_END_ADDR, IMAGE_ITEM_SIZE, IMAGE_ITEM_COUNT
        ),
        orders=connect_dlist(
            ORDER_LIST_ADDR, ORDER_LIST_END_ADDR, ORDER_ITEM_SIZE, ORDER_ITEM_COUNT
        ),
    )
    SetMemory(PER_TICK_ADDR, SetTo, PER_TICK_VALUE)
    return lists


#: The canonical camelCase name (ADR 0002 §3); `connect_dlist` is the Python
#: snake_case alias, mirroring `ts/unlimiter.ts`.
connectDList = connect_dlist

__all__ = [
    "BULLET_ITEM_COUNT",
    "BULLET_ITEM_SIZE",
    "BULLET_LIST_ADDR",
    "BULLET_LIST_END_ADDR",
    "IMAGE_ITEM_COUNT",
    "IMAGE_ITEM_SIZE",
    "IMAGE_LIST_ADDR",
    "IMAGE_LIST_END_ADDR",
    "MIN_ITEM_SIZE",
    "NEXT_FIELD_OFFSET",
    "ORDER_ITEM_COUNT",
    "ORDER_ITEM_SIZE",
    "ORDER_LIST_ADDR",
    "ORDER_LIST_END_ADDR",
    "PER_TICK_ADDR",
    "PER_TICK_VALUE",
    "PREV_FIELD_OFFSET",
    "SPRITE_FIELD_OFFSET",
    "SPRITE_FIELD_SEED",
    "SPRITE_ITEM_COUNT",
    "SPRITE_ITEM_SIZE",
    "SPRITE_LIST_ADDR",
    "SPRITE_LIST_END_ADDR",
    "UnlimitedLists",
    "connectDList",
    "connect_dlist",
    "unlimiter",
]
