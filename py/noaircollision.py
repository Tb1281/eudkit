"""The air-collision scrubber: a core helper port of euddraft's
``plugins/noAirCollision.py`` (issue #119).

TypeScript mirror: ``ts/noaircollision.ts``.

What the source does
--------------------
``beforeTriggerExec`` emits 115 triggers: ``t[0]`` .. ``t[113]`` hold 64
``SetMemory`` actions each, ``t[114]`` holds 15. Every action is
``SetMemory(dummy, SetTo, 0)`` — all zero the same placeholder dword, because
the destination is rewritten immediately after the plugin starts:

``onPluginStart`` reads the dword at ``0x6D5CD8`` (the game's collision-table
pointer, eudplib's ``f_epdread_epd(EPD(0x6D5CD8))``), which yields the table's
base EPD ``P``, then patches the 7,311 action destinations to the consecutive
EPDs ``P, P + 1, ... P + 7310``. From then on every trigger loop zeroes 7,311
consecutive dwords of the collision table, one ``SetMemory`` action per dword.

The patch walk is exactly the source's:

* ``s[i] = EPD(t[i]) + 86`` — action 0's ``dest`` field sits ``86`` EPDs into
  the trigger record (``328`` byte action base ``+ 16`` bytes to ``dest``,
  divided by 4);
* ``k`` steps by ``8`` — actions are ``32`` bytes = ``8`` EPDs apart;
* ``EUDBreakIf([i == 114, k >= 15 * 8])`` — the last trigger stops after 15
  actions instead of 64.

So the pass patched ``114 * 64 + 15 = 7,311`` action slots, and the last
trigger's inner loop stops at ``k == 120`` (``15 * 8``).

How the port is expressed
-------------------------
The 115 zeroing triggers and their 64/15 action split are emitted one to one.
Each action starts as ``SetMemory(CP_ADDR, SetTo, 0)``: ``0x6509B0`` is the
``curpl`` scratch dword (ADR 0004 decision 4), so an unpatched action only
rewrites a register whose resting value is 0 — the same "harmless placeholder"
role the source's ``dummy`` plays. The patch block is registered before the
115 triggers, so it runs first and every zeroing action is patched before its
first execution.

eudkit's HIR has no "store a variable into a run-time *computed* address"
primitive: ``emitVarStore`` destinations are static (a fixed address or a
statically-named trigger field), and a ``Var`` in a ``SetMemoryEPD``
destination resolves to the *storage address* of that variable rather than its
value (ADR 0001 decision 1 keeps ``Var.value`` free of absolute addresses).
The port therefore keeps the source's *shape* — one runtime loop that patches
one field per iteration — and writes through one scratch trigger whose ``dest``
and ``value`` fields the loop advances in place, exactly like
:mod:`py.unlimiter`'s ``EUDLoopN`` port. After each write the scratch's
``dest`` field grows by ``8`` EPDs (``k.AddNumber(8)``) and its ``value`` field
by ``1`` (``reph_epd.AddNumber(1)``).

One addressing difference is forced by eudkit's static-destination rule: the
source indexes a run-time array ``s`` of 115 per-trigger base EPDs. The port
does not need an array because consecutive trigger records are a fixed
``602`` EPDs apart (``TRIGGER_STRIDE_EPDS``, the same invariant ADR 0001
decision 2 relies on for cross-page ``FixedArray`` indexing), so the walk
skips from the last action of one trigger to the first action of the next by
``602 - 64 * 8 = 90`` EPDs. The walk starts from ``EPD(t[0]) + 86`` and each
step is the source's ``+8``, so the destination sequence is identical to
``EPD(t[i]) + 86 + k``.

The ``+90`` skip presumes the 115 zeroing triggers are consecutive trigger
records. Call the helper at module top level (or in a body made only of
triggers): unconditional preserved blocks lower in 64-action chunks, so each
full zeroing trigger fills exactly one record and the last one fills one
15-action record. Bare actions interleaved in the same block as the call are
the one unsupported placement, because they can be packed into the first
trigger's action run and shift every later record.

Cost
----
Per trigger loop the helper executes the source's 7,311 ``SetMemory`` actions
(115 trigger evaluations): the whole point of the plugin, and the price that
keeps every zeroing action at ``1T`` instead of a memory-copy loop. Arming the
helper costs one pass of the patch loop (7,311 iterations of write + two field
bumps + the loop check) on the first trigger loop only. Static cost of a module
that arms only this helper measures 163 triggers / 33 conditions / 7,442
actions (392,504 trigger bytes + 8 blob bytes) before assembly: the 115 zeroing
triggers (``115 * 2,408 = 276,920`` bytes) plus the patch-block loops.
"""

from __future__ import annotations

from typing import Any

from .cp import CP_ADDR
from .hir import (
    Add,
    SetDeathsX,
    SetMemory,
    SetMemoryEPD,
    SetTo,
    TriggerFieldReloc,
    emitVarStore,
)
from .stock_rows_built import ACT_FIELD_OFFSETS
from .temporal import once, when, while_loop
from .trigger import (
    ACT_BASE_BYTES,
    ACT_STRIDE_EPDS,
    TRIGGER_STRIDE_EPDS,
    Trigger,
    _register_trigger,
)
from .var import Var
from .view import EPD, TriggerFieldRef, read_epd_pointer

#: Dword holding the collision table's byte address; eudplib's
#: ``f_epdread_epd(EPD(0x6D5CD8))`` turns it into the table's base EPD.
REPH_EPD_ADDR = 0x6D5CD8

#: Forward triggers the source allocates (``t = [Forward() for _ in range(115)]``).
TRIGGER_COUNT = 115
#: Actions of every full trigger (``RawTrigger(actions=[... for _ in range(64)])``).
FULL_TRIGGER_ACTIONS = 64
#: Actions of the last trigger (``15``).
LAST_TRIGGER_ACTIONS = 15
#: Patched action slots: ``114 * 64 + 15``.
TOTAL_ACTIONS = (TRIGGER_COUNT - 1) * FULL_TRIGGER_ACTIONS + LAST_TRIGGER_ACTIONS

#: EPD distance from a trigger's record start to action 0's ``dest`` field:
#: ``(328 byte action base + 16 byte dest field) / 4 = 86``.
FIRST_ACTION_DEST_EPD_OFFSET = (ACT_BASE_BYTES + ACT_FIELD_OFFSETS["dest"]) // 4
#: EPD distance between consecutive actions: ``k.AddNumber(8)``.
ACTION_EPD_STRIDE = ACT_STRIDE_EPDS
#: Inner-loop bound of a full trigger: ``k <= 63 * 8``.
FULL_TRIGGER_EPD_LIMIT = (FULL_TRIGGER_ACTIONS - 1) * ACTION_EPD_STRIDE
#: Inner-loop bound of the last trigger: ``EUDBreakIf([i == 114, k >= 15 * 8])``.
LAST_TRIGGER_EPD_LIMIT = LAST_TRIGGER_ACTIONS * ACTION_EPD_STRIDE
#: EPDs between consecutive trigger records (ADR 0001 decision 2).
TRIGGER_EPD_STRIDE = TRIGGER_STRIDE_EPDS
#: Skip from the last action of a full trigger to the next trigger's action 0.
CROSS_TRIGGER_EPD_STEP = TRIGGER_EPD_STRIDE - FULL_TRIGGER_ACTIONS * ACTION_EPD_STRIDE


def _seed_field_epd(dst: TriggerFieldRef, src: TriggerFieldRef) -> None:
    """Write ``EPD(src)`` into the 4-byte field ``dst`` (mode-1 relocation).

    Both halves are link-time fixups: the action's ``dest`` field is pointed at
    ``dst`` and its ``value`` field is filled with ``EPD(src)``. This is the
    field-to-field form of the relocation ``blockio._seed_epd_cursor`` uses to
    seed a cursor variable with a blob's EPD.
    """
    act = SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF)
    act["relocs"] = [
        {
            "field_offset": ACT_FIELD_OFFSETS["dest"],
            "target_trigger": dst.triggerId,
            "target_offset": dst.byteOffset,
            "mode": 1,
        },
        {
            "field_offset": ACT_FIELD_OFFSETS["value"],
            "target_trigger": src.triggerId,
            "target_offset": src.byteOffset,
            "mode": 1,
        },
    ]
    Trigger(conds=[], acts=[act], preserved=True)


def _zero_act() -> dict[str, Any]:
    """One placeholder ``SetMemory`` action; the patch loop rewrites ``dest``."""
    return SetMemory(CP_ADDR, SetTo, 0)


def noAirCollision() -> None:
    """Zero the collision table on every trigger loop (the plugin's whole job).

    Call it once inside :func:`build_module`; the 115 zeroing triggers occupy
    the trigger order at the call site, and the first-pass patch block is
    emitted just before them, as ``onPluginStart`` runs before
    ``beforeTriggerExec`` in the source.
    """
    zeros = [
        Trigger(conds=[], acts=[_zero_act() for _ in range(actions)], register=False)
        for actions in (FULL_TRIGGER_ACTIONS,) * (TRIGGER_COUNT - 1)
        + (LAST_TRIGGER_ACTIONS,)
    ]

    def patch() -> None:
        # reph_epd = f_epdread_epd(EPD(0x6D5CD8)): the table base EPD.
        table = read_epd_pointer(EPD(REPH_EPD_ADDR))
        # One action that writes a dword to a run-time EPD; the loop chooses
        # which field by walking this action's own dest/value fields.
        writer = Trigger(
            conds=[],
            acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF)],
            preserved=True,
            register=False,
        )
        # s[0] = EPD(t[0]) + 86, and the first value written is reph_epd.
        _seed_field_epd(writer.act(0).dest, zeros[0].act(0).dest)
        emitVarStore(
            table.id,
            TriggerFieldReloc(writer.id, writer.act(0).value.byteOffset),
            SetTo,
        )

        i = Var(0)
        k = Var(0)
        limit = Var(0)

        def inner() -> None:
            # SetMemoryEPD(s[i] + k, SetTo, reph_epd)
            _register_trigger(writer)
            # reph_epd.AddNumber(1), k.AddNumber(8)
            SetMemoryEPD(writer.act(0).dest, Add, ACTION_EPD_STRIDE)
            SetMemoryEPD(writer.act(0).value, Add, 1)
            k.iadd(ACTION_EPD_STRIDE)

        def outer() -> None:
            # k <= 63 * 8, but the last trigger breaks at k >= 15 * 8.
            limit.set(FULL_TRIGGER_EPD_LIMIT)
            when(i.eq(TRIGGER_COUNT - 1), lambda: limit.set(LAST_TRIGGER_EPD_LIMIT))
            k.set(0)
            while_loop(k.le(limit), inner)
            i.iadd(1)
            # Next trigger's action 0: 602 EPDs on, 64 * 8 already walked.
            SetMemoryEPD(writer.act(0).dest, Add, CROSS_TRIGGER_EPD_STEP)

        while_loop(i.le(TRIGGER_COUNT - 1), outer)

    once(patch)
    for zero in zeros:
        _register_trigger(zero)


#: The canonical camelCase name (ADR 0002 §3); `no_air_collision` is the
#: Python snake_case alias, mirroring `ts/noaircollision.ts`.
no_air_collision = noAirCollision

__all__ = [
    "ACTION_EPD_STRIDE",
    "CROSS_TRIGGER_EPD_STEP",
    "FIRST_ACTION_DEST_EPD_OFFSET",
    "FULL_TRIGGER_ACTIONS",
    "FULL_TRIGGER_EPD_LIMIT",
    "LAST_TRIGGER_ACTIONS",
    "LAST_TRIGGER_EPD_LIMIT",
    "REPH_EPD_ADDR",
    "TOTAL_ACTIONS",
    "TRIGGER_COUNT",
    "TRIGGER_EPD_STRIDE",
    "noAirCollision",
    "no_air_collision",
]
