"""Fixed-capacity semantic containers: ``Queue``, ``Deque``, ``Stack``, ``UnitGroup``.

``Queue``/``Deque`` are ring buffers built from ``Trigger (16C, 64A)`` slot
triggers. Each slot trigger is both storage and reader: its
``act[0..K-1].dest`` fields are bound statically to a container-owned
``out_box``, so a slot execution copies a complete scalar (``K = 1``) or
``TriggerStruct`` (``K <= 64``) element in one trigger. ``pop``/``popleft``
therefore never patch per-slot destinations; their site cost is independent
of capacity (16 and 128 cost the same).

Writes patch the selected slot trigger's action *value* fields. Constant
elements need one site trigger per ring slot (exactly one fires because the
ring index is unique). Variable elements are written by one pointer trigger
plus one ``VarStore`` per element field: the pointer's ``.dest`` walks the
contiguous slot block at ``TRIGGER_STRIDE_EPDS`` and the store's out-of-band
``TriggerPage`` firing delivers the value, instead of 32 bit-test triggers per
value bit per slot.

Semantics match :mod:`ts/queue.ts`:

- Fixed capacity; no dynamic growth.
- A full queue/deque keeps accepting appends by dropping the element at the
  opposite end (legacy ring behaviour).
- ``pop``/``popleft`` on empty is unchecked; guard with ``empty()``.
- ``clear()`` resets pointers/length, not slot payloads.
- Ring slot reads happen at the start of a loop, so an element appended in a
  loop becomes visible to a pop in the next loop.
"""

from __future__ import annotations

from typing import Any, Callable

from .array import FixedArray, arrayDword
from .backing import ELEM_MASK, pageSlotBitCond
from .cp import (
    cp_empty,
    cp_live,
    emit_cp_read_into,
    emit_cp_sweep,
)
from .hir import (
    Deaths,
    DeathsX,
    Exactly,
    SetDeaths,
    SetDeathsX,
    SetTo,
    TriggerFieldReloc,
    emitVarStore,
)
from .struct import TriggerStructInstance, TriggerStructSchema
from .temporal import when
from .lazy_binding import LazyBinding

from .trigger import (
    TRIGGER_STRIDE_EPDS,
    Trigger,
    _register_trigger,
)
from .var import Var
from .view import (
    CURRENT_PLAYER,
    POOL_NULL,
    EudslError,
    _describe_value,
    _is_plain_int,
    warn_lazy_in_builder,
)

Box = Var | TriggerStructInstance


def _check_capacity(capacity: int, what: str) -> None:
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"{what} capacity must be a positive integer, got {capacity!r}",
        )


def _is_struct_schema(elem: Any) -> bool:
    return isinstance(elem, TriggerStructSchema) or (
        callable(elem) and hasattr(elem, "instantiate")
    )


def _box_fields(box: Box) -> list[Var]:
    if isinstance(box, TriggerStructInstance):
        return list(box.fields)
    return [box]


class _ElementLayout:
    def __init__(self, elem: Any) -> None:
        self.elem = elem
        if _is_struct_schema(elem):
            self.mask = 0xFFFFFFFF
        elif isinstance(elem, str) and elem in ELEM_MASK:
            self.mask = ELEM_MASK[elem]
        else:
            raise EudslError(
                "EUDSL-BOUNDS", f"Unknown container element type: {elem!r}"
            )

    def make(self) -> Box:
        if _is_struct_schema(self.elem):
            return self.elem.instantiate()
        return Var(0, explicit_mask=self.mask, storage="page")


def _make_slot_triggers(
    elem: Any, capacity: int, selector: Var, out_box: Box
) -> list[Trigger]:
    fields = _box_fields(out_box)
    return [
        Trigger(
            conds=[selector.eq(i)],
            acts=[SetDeathsX(f.value, SetTo, 0, 0, f.bitmask) for f in fields],
        )
        for i in range(capacity)
    ]


def _classify_element(slots: list[Trigger], value: int | Var | TriggerStructInstance) -> list[Any]:
    """Validate one element write and expand it to one value per slot field.

    One classification at the seam, before any trigger is emitted (ADR 0019):
    the element domain is a plain integer, a ``Var``, or a struct instance.
    Anything else used to fall through as a ``Var`` and die in
    ``pageSlotBitCond`` after acts were queued (py), or build a silent value
    (ts). The slot has ``count`` field actions and the element must supply
    exactly that many: a scalar expands to one value, so it only fits a
    scalar slot — do not broadcast it into every field.
    """
    count = len(slots[0].acts)
    values: list[Any]
    if isinstance(value, TriggerStructInstance):
        values = list(value.fields)
    elif isinstance(value, Var) or _is_plain_int(value):
        values = [value]
    else:
        raise EudslError(
            "EUDSL-TYPE",
            "container element must be a plain integer, a Var, or a struct; "
            f"got {_describe_value(value)}",
        )
    if len(values) != count:
        raise EudslError(
            "EUDSL-TYPE",
            "container element shape must match the element: "
            f"expected {count} field(s), got {len(values)}",
        )
    return values


def _require_contiguous_slot_block(slots: list[Trigger]) -> None:
    """Check the ring slot triggers form a contiguous trigger-id block.

    The pointer write addresses slot *n* as ``EPD(slot 0) +
    TRIGGER_STRIDE_EPDS * n``. That is only valid while the ring slots are
    adjacent in the emitted trigger array, which holds because
    ``_make_slot_triggers`` creates them back to back with no other trigger
    in between. Trigger ids are the build-time witness; the emitted byte
    offsets are pinned by the slot-block layout test.
    """
    base = slots[0].id
    for i, slot in enumerate(slots):
        if slot.id != base + i:
            raise EudslError(
                "EUDSL-BOUNDS",
                "container ring slots must be a contiguous trigger block "
                f"(slot {i} is trigger {slot.id}, expected {base + i}); "
                "the VarStore slot pointer assumes "
                f"{TRIGGER_STRIDE_EPDS}-EPD trigger adjacency",
            )


def _raw_slot_epd_reset(
    dest_field: Any, slot: Trigger, field_offset: int
) -> dict[str, Any]:
    """Reset act: point ``dest_field`` at the absolute EPD of a slot field.

    Mirrors ``array._raw_epd_reset``; the source is a field of the first ring
    slot trigger instead of a page-backed var slot.
    """
    return {
        "mask": 0xFFFFFFFF,
        "strid": 0,
        "wavid": 0,
        "time": 0,
        "dest": 0,
        "value": 0,
        "unit": 0,
        "acttype": 45,
        "modifier": SetTo,
        "flags": 20,
        "padding": 0,
        "eudx": 0x4353,
        "relocs": [
            {
                "field_offset": 16,
                "target_trigger": dest_field.triggerId,
                "target_offset": dest_field.alignedByteOffset,
                "mode": 1,
            },
            {
                "field_offset": 20,
                "target_trigger": slot.id,
                "target_offset": field_offset,
                "mode": 1,
            },
        ],
    }


def _emit_slot_field_store(
    slots: list[Trigger], selector: Var, k: int, src: Var
) -> None:
    """Write one element field with one pointer trigger plus one VarStore.

    Address: pointer ``.dest`` starts at ``EPD(slot 0.act[k].value)``; one
    carry-free trigger per ``selector`` bit adds ``TRIGGER_STRIDE_EPDS << b``
    so it lands on the selected slot's field. Value: the VarStore points the
    source var's TriggerPage slot at pointer ``.value``, so one out-of-band
    page firing delivers the whole dword; the pointer copies it into the
    selected slot.

    Two distinct offsets are in play and must not be conflated: the reset
    reads the *slot's* field ``k`` (``field0.alignedByteOffset``, which is
    ``0x15c + 32 * k``), while the store writes the *pointer's* action 0
    value field (always ``pointer.act(0).value``). Using the slot offset for
    the store would hit the pointer's later actions (``flag.act_set(0)`` for
    ``k > 0``) and silently drop every field but the first.
    """
    field0 = slots[0].act(k).value
    flag = Var(0)
    pointer = Trigger(
        conds=[flag.eq(1)],
        # Same full-dword mask as the const slot write: the destination is
        # the slot's 32-byte action field, so no neighbouring field is at
        # risk; element width is applied by the slot's own read action.
        acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF), flag.act_set(0)],
        register=False,
    )
    dest_field = pointer.act(0).dest
    Trigger(
        acts=[
            _raw_slot_epd_reset(dest_field, slots[0], field0.alignedByteOffset),
            flag.act_set(1),
        ]
    )
    bits = max(1, (len(slots) - 1).bit_length())
    for b in range(bits):
        Trigger(
            conds=[flag.eq(1), pageSlotBitCond(selector, b)],
            acts=[dest_field.act_add(TRIGGER_STRIDE_EPDS << b)],
        )
    emitVarStore(
        src.id,
        TriggerFieldReloc(pointer.id, pointer.act(0).value.byteOffset),
    )
    _register_trigger(pointer)


def _emit_slot_writes(
    slots: list[Trigger], selector: Var, values: list[Any]
) -> None:
    """Write one element into the ring slot selected by ``selector``.

    Constants keep one site trigger per slot (exactly one fires because the
    write index is unique). Variable fields are written by one pointer plus
    one ``VarStore`` per field, not by 32 bit-test triggers per slot.
    """
    count = len(slots[0].acts)
    _require_contiguous_slot_block(slots)
    for i, slot in enumerate(slots):
        const_acts = []
        for k in range(count):
            src = values[k]
            if isinstance(src, int) and not isinstance(src, bool):
                field = slot.act(k).value
                const_acts.append(
                    SetDeathsX(field, SetTo, (src & field.bitMask) & 0xFFFFFFFF, 0, field.bitMask)
                )
        if const_acts:
            Trigger(conds=[selector.eq(i)], acts=const_acts)
    for k in range(count):
        src = values[k]
        if not (isinstance(src, int) and not isinstance(src, bool)):
            _emit_slot_field_store(slots, selector, k, src)


def _advance_up(v: Var, capacity: int) -> None:
    """Advance a ring index by ``+1 mod capacity``.

    The increment runs unconditionally and the wrap subtracts ``capacity``
    *after* it. Two independent conditional triggers would both fire when
    ``v == capacity - 1`` (the first resets to 0, then the second sees 0 and
    increments again), silently skipping ring slots.
    """
    Trigger(conds=[], acts=[v.act_add(1)])
    Trigger(conds=[v.ge(capacity)], acts=[v.act_sub(capacity)])


def _advance_down(v: Var, capacity: int) -> None:
    """Advance a ring index by ``-1 mod capacity``.

    Adding ``capacity - 1`` and wrapping on the post-add value avoids relying
    on unsigned underflow: StarCraft's masked ``Subtract`` saturates at zero,
    so ``0 - 1`` would stay ``0`` instead of wrapping.
    """
    Trigger(conds=[], acts=[v.act_add(capacity - 1)])
    Trigger(conds=[v.ge(capacity)], acts=[v.act_sub(capacity)])


class Queue:
    """FIFO queue backed by statically-bound ring slot triggers."""

    def __init__(self, elem: Any, capacity: int, _deferred: bool = False) -> None:
        _check_capacity(capacity, "Queue()")
        self.capacity = capacity
        self._deferred = (
            LazyBinding("Queue", entry="build_module") if _deferred else None
        )
        self._elem = elem
        if _deferred:
            # Eager element-type validation without any allocation; the
            # eager constructor validates the same input after its vars.
            _ElementLayout(elem)
            return
        length = Var(0)
        head = Var(0)
        tail = Var(0)
        self._length = length
        self._head = head
        self._tail = tail
        self._layout = _ElementLayout(elem)
        out_box = self._layout.make()
        self._out_box = out_box
        self._slots = _make_slot_triggers(elem, capacity, head, out_box)

    # ------------------------------------------------------------------
    # Deferred resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> "Queue":
        """The eager instance serving container reads: this for eager
        handles, the active builder's binding for deferred ones.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``Queue`` construction
        varies here (R3)."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(lambda: Queue(self._elem, self.capacity))

    @property
    def length(self) -> Var:
        return self._resolve()._length

    @property
    def head(self) -> Var:
        return self._resolve()._head

    @property
    def tail(self) -> Var:
        return self._resolve()._tail

    @property
    def outBox(self) -> Box:
        return self._resolve()._out_box

    @property
    def out_box(self) -> Box:
        return self.outBox

    @property
    def slots(self) -> list[Trigger]:
        return self._resolve()._slots

    def empty(self) -> Any:
        return self.length.eq(0)

    def full(self) -> Any:
        return self.length.ge(self.capacity)

    def append(self, value: int | Var | TriggerStructInstance) -> "Queue":
        values = _classify_element(self.slots, value)
        when([self.length.ge(self.capacity)], lambda: _advance_up(self.head, self.capacity))
        _emit_slot_writes(self.slots, self.tail, values)
        _advance_up(self.tail, self.capacity)
        when([self.length.le(self.capacity - 1)], lambda: self.length.iadd(1))
        return self

    def push(self, value: int | Var | TriggerStructInstance) -> "Queue":
        return self.append(value)

    def popleft(self) -> Box:
        ret = self.outBox
        _advance_up(self.head, self.capacity)
        self.length.isub(1)
        return ret

    def pop(self) -> Box:
        return self.popleft()

    def clear(self) -> "Queue":
        self.head.set(0)
        self.tail.set(0)
        self.length.set(0)
        return self


class Deque:
    """Double-ended queue backed by two statically-bound ring read views."""

    def __init__(self, elem: Any, capacity: int, _deferred: bool = False) -> None:
        _check_capacity(capacity, "Deque()")
        self.capacity = capacity
        self._deferred = (
            LazyBinding("Deque", entry="build_module") if _deferred else None
        )
        self._elem = elem
        if _deferred:
            # Eager element-type validation without any allocation.
            _ElementLayout(elem)
            return
        length = Var(0)
        left = Var(0)
        right = Var(0)
        self._length = length
        self._left = left
        self._right = right
        self._layout = _ElementLayout(elem)
        out_left = self._layout.make()
        out_right = self._layout.make()
        self._out_left = out_left
        self._out_right = out_right
        self._left_slots = _make_slot_triggers(elem, capacity, left, out_left)
        self._right_slots = [
            Trigger(
                conds=[right.eq((i + 1) % capacity)],
                acts=[
                    SetDeathsX(f.value, SetTo, 0, 0, f.bitmask)
                    for f in _box_fields(out_right)
                ],
            )
            for i in range(capacity)
        ]

    # ------------------------------------------------------------------
    # Deferred resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> "Deque":
        """The eager instance serving container reads.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``Deque`` construction
        varies here (R3)."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(lambda: Deque(self._elem, self.capacity))

    @property
    def length(self) -> Var:
        return self._resolve()._length

    @property
    def left(self) -> Var:
        return self._resolve()._left

    @property
    def right(self) -> Var:
        return self._resolve()._right

    @property
    def outLeft(self) -> Box:
        return self._resolve()._out_left

    @property
    def out_left(self) -> Box:
        return self.outLeft

    @property
    def outRight(self) -> Box:
        return self._resolve()._out_right

    @property
    def out_right(self) -> Box:
        return self.outRight

    @property
    def leftSlots(self) -> list[Trigger]:
        return self._resolve()._left_slots

    @property
    def left_slots(self) -> list[Trigger]:
        return self.leftSlots

    @property
    def rightSlots(self) -> list[Trigger]:
        return self._resolve()._right_slots

    @property
    def right_slots(self) -> list[Trigger]:
        return self.rightSlots

    def empty(self) -> Any:
        return self.length.eq(0)

    def full(self) -> Any:
        return self.length.ge(self.capacity)

    def append(self, value: int | Var | TriggerStructInstance) -> "Deque":
        values = _classify_element(self.leftSlots, value)
        when([self.length.ge(self.capacity)], lambda: _advance_up(self.left, self.capacity))
        _emit_slot_writes(self.leftSlots, self.right, values)
        _emit_slot_writes(self.rightSlots, self.right, values)
        _advance_up(self.right, self.capacity)
        when([self.length.le(self.capacity - 1)], lambda: self.length.iadd(1))
        return self

    def appendleft(self, value: int | Var | TriggerStructInstance) -> "Deque":
        values = _classify_element(self.leftSlots, value)
        _advance_down(self.left, self.capacity)
        self._emit_left_write(values)
        when(
            [self.length.ge(self.capacity)],
            lambda: _advance_down(self.right, self.capacity),
        )
        when([self.length.le(self.capacity - 1)], lambda: self.length.iadd(1))
        return self

    def _emit_left_write(self, values: list[Any]) -> None:
        _emit_slot_writes(self.leftSlots, self.left, values)
        _emit_slot_writes(self.rightSlots, self.left, values)

    def pop(self) -> Box:
        ret = self.outRight
        _advance_down(self.right, self.capacity)
        self.length.isub(1)
        return ret

    def popleft(self) -> Box:
        ret = self.outLeft
        _advance_up(self.left, self.capacity)
        self.length.isub(1)
        return ret


class Stack:
    """LIFO stack backed by ``FixedArray`` + a game-time ``pos`` index."""

    def __init__(self, capacity: int, _deferred: bool = False) -> None:
        _check_capacity(capacity, "Stack()")
        self.capacity = capacity
        self._deferred = (
            LazyBinding("Stack", entry="build_module") if _deferred else None
        )
        if _deferred:
            return
        self._storage = FixedArray(arrayDword, capacity)
        self._pos = Var(0)

    # ------------------------------------------------------------------
    # Deferred resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> "Stack":
        """The eager instance serving container reads.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``Stack`` construction
        varies here (R3)."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(lambda: Stack(self.capacity))

    @property
    def storage(self) -> FixedArray:
        return self._resolve()._storage

    @property
    def pos(self) -> Var:
        return self._resolve()._pos

    @property
    def length(self) -> Var:
        return self.pos

    def empty(self) -> Any:
        return self.pos.eq(0)

    def full(self) -> Any:
        return self.pos.ge(self.capacity)

    def push(self, value: int | Var) -> "Stack":
        self.storage.set(self.pos, value)
        self.pos.iadd(1)
        return self

    def pop(self) -> Var:
        self.pos.isub(1)
        out = self.storage.get(self.pos)
        ret = Var(0)
        ret.set(out)
        return ret

    def clear(self) -> "Stack":
        self.pos.set(0)
        return self


class UnitGroup:
    """Fixed-capacity set of unit EPDs backed by page slots.

    ``add`` fills the first empty slot, ``remove`` clears matching slots, and
    ``for_each`` visits non-empty slots. With ``autoPrune`` (default ``True``)
    a per-frame sweep clears dead members using the vanilla sprite field or
    the unlimiter order field.

    With ``compact=True`` the same storage is driven by a runtime
    ``CurrentPlayer`` cursor loop: ``for_each`` emits its callback body once,
    and ``autoPrune``/``add``/``remove``/``clear`` each emit a single runtime
    sweep instead of unrolling one trigger per slot. Compact mode is a
    *static-size*
    optimization: executed cost is higher because every live EPD is
    materialised with a 32-bit reader and every site saves/restores the
    actual CurrentPlayer register. The default (``compact=False``) unroll is
    unchanged and remains the executed-cost choice.

    Callback contract: the unroll passes the actual slot ``Var``, so a body
    that writes it mutates storage. Compact ``for_each`` instead passes one
    reused, read-only snapshot ``Var``; writes affect only that scratch copy,
    and the callback must not retain or alias it across iterations.

    ``deadCheck`` runs once at build time per group in compact mode (the
    unroll runs it once per slot) and receives ``slot_value + check_offset``.
    The returned *dead* condition is reused for every live slot; HIR the
    predicate emits in compact mode is placed inside the per-slot live gate.
    """

    def __init__(
        self,
        capacity: int,
        *,
        unlimiter: bool = False,
        autoPrune: bool = True,
        auto_prune: bool | None = None,
        deadCheck: Callable[[Var], Any] | None = None,
        dead_check: Callable[[Var], Any] | None = None,
        compact: bool = False,
        _deferred: bool = False,
    ) -> None:
        _check_capacity(capacity, "UnitGroup()")
        self.capacity = capacity
        self.autoPrune = autoPrune if auto_prune is None else auto_prune
        self.unlimiter = unlimiter
        self.deadCheck = deadCheck if dead_check is None else dead_check
        self.compact = compact
        self._deferred = (
            LazyBinding("UnitGroup", entry="build_module") if _deferred else None
        )
        if _deferred:
            return
        self._length = Var(0)
        storage = FixedArray(arrayDword, capacity)
        self._slots = storage.vars
        if self.autoPrune:
            self._emit_auto_prune()

    # ------------------------------------------------------------------
    # Deferred resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> "UnitGroup":
        """The eager instance serving container reads.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``UnitGroup``
        construction varies here (R3, including auto-prune triggers)."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(
            lambda: UnitGroup(
                self.capacity,
                unlimiter=self.unlimiter,
                autoPrune=self.autoPrune,
                deadCheck=self.deadCheck,
                compact=self.compact,
            )
        )

    @property
    def length(self) -> Var:
        return self._resolve()._length

    @property
    def slots(self) -> list[Var]:
        return self._resolve()._slots

    def _emit_auto_prune(self) -> None:
        if self.compact:
            self._emit_auto_prune_compact()
            return
        check_offset = 0x13 if self.unlimiter else 3
        mask = 0xFF00 if self.unlimiter else 0xFFFFFFFF
        for slot in self.slots:
            when([slot.ne(0)], lambda slot=slot: self._emit_prune_slot(slot, check_offset, mask))

    def _emit_auto_prune_compact(self) -> None:
        """One shared sweep: read the live EPD, test its death field, clear."""
        check_offset = 0x13 if self.unlimiter else 3
        mask = 0xFF00 if self.unlimiter else 0xFFFFFFFF
        value = Var(0)
        check = Var(0)
        custom = self.deadCheck

        def iteration() -> None:
            def live() -> None:
                emit_cp_read_into(value)
                check.set(value)
                if check_offset != 0:
                    check.iadd(check_offset)
                acts = [
                    SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
                    self.length.act_sub(1),
                ]
                if custom is None:
                    probe = Trigger(
                        conds=[DeathsX(0, Exactly, 0, 0, mask)],
                        acts=acts,
                        register=False,
                    )
                    emitVarStore(
                        check.id,
                        TriggerFieldReloc(
                            probe.id, probe.cond(0).player.byteOffset
                        ),
                    )
                    _register_trigger(probe)
                else:
                    # The predicate runs once at build time for the whole
                    # group, inside the per-slot live gate, so HIR it emits
                    # runs only for live members. The returned condition is
                    # reused for every live slot.
                    Trigger(conds=[custom(check)], acts=acts)

            when([cp_live()], live)

        emit_cp_sweep(self.slots[0], self.capacity, iteration)

    def _emit_prune_slot(self, slot: Var, check_offset: int, mask: int) -> None:
        check = Var(0)
        check.set(slot)
        if check_offset != 0:
            check.iadd(check_offset)

        # The engine reads ``cond.player`` as an EPD, so a dead-membership
        # test needs the *value* of ``check`` written into the condition's
        # player field at runtime. ``VarStore`` performs that copy through
        # ``check``'s TriggerPage slot. The probe trigger is registered after
        # the store so it executes with the freshly patched field.
        cond = (
            self.deadCheck(check)
            if self.deadCheck is not None
            else DeathsX(0, Exactly, 0, 0, mask)
        )
        probe = Trigger(
            conds=[cond],
            acts=[slot.act_set(0), self.length.act_sub(1)],
            register=False,
        )
        if self.deadCheck is None:
            emitVarStore(
                check.id,
                TriggerFieldReloc(probe.id, probe.cond(0).player.byteOffset),
            )
        _register_trigger(probe)

    def empty(self) -> Any:
        return self.length.eq(0)

    def full(self) -> Any:
        return self.length.ge(self.capacity)

    def add(self, epd: int | Var) -> "UnitGroup":
        if self._deferred:
            self._resolve().add(epd)
            return self
        if isinstance(epd, int) and not isinstance(epd, bool):
            if epd < 0:
                raise EudslError(
                    "EUDSL-BOUNDS", f"UnitGroup.add() EPD must be non-negative, got {epd}"
                )
        if self.compact:
            self._add_compact(epd)
            return self
        done = Var(0)
        for slot in self.slots:
            when(
                [slot.eq(0), done.eq(0)],
                lambda slot=slot, done=done: (
                    slot.set(epd),
                    done.set(1),
                    self.length.iadd(1),
                ),
            )
        return self

    def _add_compact(self, epd: int | Var) -> None:
        done = Var(0)
        if isinstance(epd, int) and not isinstance(epd, bool):
            def iteration() -> None:
                when(
                    [cp_empty(), done.eq(0)],
                    lambda: (
                        SetDeaths(CURRENT_PLAYER, SetTo, epd, 0),
                        done.set(1),
                        self.length.iadd(1),
                    ),
                )
        else:
            writer = Trigger(
                conds=[cp_empty(), done.eq(0)],
                acts=[
                    SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
                    done.act_set(1),
                    self.length.act_add(1),
                ],
                register=False,
            )
            emitVarStore(
                epd.id,
                TriggerFieldReloc(writer.id, writer.act(0).value.byteOffset),
            )

            def iteration() -> None:
                _register_trigger(writer)

        emit_cp_sweep(self.slots[0], self.capacity, iteration)

    def remove(self, epd: int | Var) -> "UnitGroup":
        if self._deferred:
            self._resolve().remove(epd)
            return self
        if self.compact:
            self._remove_compact(epd)
            return self
        for slot in self.slots:
            when(
                [slot.eq(epd)],
                lambda slot=slot: (slot.set(0), self.length.isub(1)),
            )
        return self

    def _remove_compact(self, epd: int | Var) -> None:
        if isinstance(epd, int) and not isinstance(epd, bool):
            def iteration() -> None:
                when(
                    [Deaths(CURRENT_PLAYER, Exactly, epd, 0)],
                    lambda: (
                        SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
                        self.length.isub(1),
                    ),
                )
        else:
            probe = Trigger(
                conds=[Deaths(CURRENT_PLAYER, Exactly, 0, 0)],
                acts=[
                    SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
                    self.length.act_sub(1),
                ],
                register=False,
            )
            emitVarStore(
                epd.id,
                TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
            )

            def iteration() -> None:
                _register_trigger(probe)

        emit_cp_sweep(self.slots[0], self.capacity, iteration)

    def for_each(self, body: Callable[[Var], Any]) -> "UnitGroup":
        if self._deferred:
            self._resolve().for_each(body)
            return self
        if self.compact:
            self._for_each_compact(body)
            return self
        for slot in self.slots:
            when([slot.ne(0)], lambda slot=slot: body(slot))
        return self

    def _for_each_compact(self, body: Callable[[Var], Any]) -> None:
        epd = Var(0)

        def iteration() -> None:
            def live() -> None:
                emit_cp_read_into(epd)
                body(epd)

            when([cp_live()], live)

        emit_cp_sweep(self.slots[0], self.capacity, iteration)

    def forEach(self, body: Callable[[Var], Any]) -> "UnitGroup":
        return self.for_each(body)

    def clear(self) -> "UnitGroup":
        if self._deferred:
            self._resolve().clear()
            return self
        if self.compact:
            emit_cp_sweep(
                self.slots[0],
                self.capacity,
                lambda: SetDeaths(CURRENT_PLAYER, SetTo, 0, 0),
            )
            self.length.set(0)
            return self
        for slot in self.slots:
            slot.set(0)
        self.length.set(0)
        return self


def lazyQueue(elem: Any, capacity: int) -> Queue:
    """Declare a lazy ``Queue`` (globals P3): a handle constructible
    anywhere (no builder needed) that resolves to a fully eager binding
    on first resolving use inside ``build_module``, one binding per
    builder.

    Call shape mirrors ``Queue(elem, capacity)``. Capacity and element
    type are validated eagerly at the declaration; no var, out box, or
    slot trigger is allocated until first resolving use. Binding creation
    is atomic: one full eager ``Queue`` construction in eager order. A
    never-used handle emits nothing (D5). This factory is the only
    documented path to deferred mode; the internal constructor flag stays
    undocumented.
    """
    warn_lazy_in_builder()
    return Queue(elem, capacity, _deferred=True)


def lazyDeque(elem: Any, capacity: int) -> Deque:
    """Declare a lazy ``Deque`` (globals P3). See :func:`lazyQueue`: same
    declaration/validation and per-builder binding rules, with call shape
    ``Deque(elem, capacity)``."""
    warn_lazy_in_builder()
    return Deque(elem, capacity, _deferred=True)


def lazyStack(capacity: int) -> Stack:
    """Declare a lazy ``Stack`` (globals P3). See :func:`lazyQueue`: same
    declaration/validation and per-builder binding rules, with call shape
    ``Stack(capacity)``."""
    warn_lazy_in_builder()
    return Stack(capacity, _deferred=True)


def lazyUnitGroup(capacity: int, **opts: Any) -> UnitGroup:
    """Declare a lazy ``UnitGroup`` (globals P3). See :func:`lazyQueue`:
    same declaration/validation and per-builder binding rules, with call
    shape ``UnitGroup(capacity, **opts)``. ``autoPrune``, ``unlimiter``,
    and ``compact`` are pure reads that never resolve; ``slots``/``length``
    and every method do."""
    warn_lazy_in_builder()
    return UnitGroup(capacity, _deferred=True, **opts)


lazy_queue = lazyQueue
lazy_deque = lazyDeque
lazy_stack = lazyStack
lazy_unit_group = lazyUnitGroup


EUDQueue = Queue
EUDDeque = Deque
EUDStack = Stack
EUDUnitGroup = UnitGroup


class ObjectPool:
    """Fixed-capacity free-list pool over a ``FixedArray``.

    The free list is a LIFO ``Stack`` of indices, so a fresh pool hands out
    ``0, 1, 2, ...`` deterministically. ``POOL_NULL`` (``0xFFFFFFFF``) is
    returned when the pool is exhausted and is distinguishable from every
    legal index because capacity is capped at ``0xFFFFFFFE``.

    ``release`` performs no bounds check (documented): releasing an index
    twice or outside ``0..capacity-1`` corrupts the free list.
    """

    def __init__(self, capacity: int, schema_or_width: Any = arrayDword) -> None:
        if not _is_plain_int(capacity):
            raise EudslError(
                "EUDSL-TYPE",
                f"ObjectPool capacity must be a plain integer, got "
                f"{_describe_value(capacity)}",
            )
        if capacity <= 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"ObjectPool capacity must be positive, got {capacity}",
            )
        if capacity > 0xFFFFFFFE:
            raise EudslError(
                "EUDSL-RANGE",
                f"ObjectPool capacity must be at most 0xFFFFFFFE, got {capacity}",
            )
        self.capacity = capacity
        self._storage = FixedArray(schema_or_width, capacity)
        self._free = Stack(capacity)
        self._fill_free()

    def _fill_free(self) -> None:
        """Seed the stack body statically so a pop yields ``0, 1, 2, ...``.

        ``Stack.push`` would emit a dynamic-write trigger per slot; writing the
        constant body and setting ``pos`` is the same state with one packed
        initialization.
        """
        for index in range(self.capacity):
            self._free.storage.set(index, self.capacity - 1 - index)
        self._free.pos.set(self.capacity)

    @property
    def storage(self) -> Any:
        """The user-data array; ``get``/``set`` delegate to it."""
        return self._storage

    @property
    def free_list(self) -> Stack:
        """The LIFO index stack backing allocation."""
        return self._free

    freeList = free_list

    def allocate(self) -> Var:
        """Pop one free index, or return ``POOL_NULL`` when full."""
        out = Var(0)
        # Reset per call, not just at module init: a reused allocation site
        # inside a loop must return POOL_NULL after the pool becomes full.
        out.set(POOL_NULL)
        when(self._free.pos.ge(1), lambda: out.set(self._free.pop()))
        return out

    def release(self, index: int | Var) -> None:
        """Push ``index`` back onto the free list."""
        if not (_is_plain_int(index) or isinstance(index, Var)):
            raise EudslError(
                "EUDSL-TYPE",
                f"ObjectPool.release index must be an integer or a Var; got "
                f"{_describe_value(index)}",
            )
        self._free.push(index)

    def full(self) -> Any:
        """A condition that holds when there are no free slots."""
        return self._free.pos.eq(0)

    def empty(self) -> Any:
        """A condition that holds when every slot is free."""
        return self._free.pos.eq(self.capacity)

    def clear(self) -> None:
        """Reset the free list to its fresh LIFO order; storage is untouched."""
        self._free.clear()
        self._fill_free()

    def get(self, index: int | Var) -> Any:
        """Read a user-data slot through the backing array."""
        return self._storage.get(index)

    def set(self, index: int | Var, value: Any) -> None:
        """Write a user-data slot through the backing array."""
        self._storage.set(index, value)

    allocateObject = allocate
    releaseObject = release
