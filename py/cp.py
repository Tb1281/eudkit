"""Scoped CurrentPlayer (`0x6509B0`) pointer views and absolute offset hops.
`with_cp(epd, ...)` and `with with_cp(epd) as cp:`

A constant EPD base is the only supported form: the CP register holds an
**EPD**, not a byte address, so a dynamic base would silently write a byte
address into it. Hops are absolute anchors (`SetTo baseEpd + target`), so a
hop inside a replayed runtime loop body re-anchors the cursor on every pass
instead of drifting.
"""

from __future__ import annotations

from typing import Any, Callable

from .hir import (
    Add,
    AddrReloc,
    AtLeast,
    Db,
    DbRef,
    Deaths,
    Exactly,
    SetDeaths,
    SetTo,
    emitVarStore,
    epd_of_db,
)
from .stock_rows_built import ACT_FIELD_OFFSETS
from .temporal import when, while_loop
from .trigger import ACT_COUNT, ACT_STRIDE_EPDS, TRIGGER_STRIDE_EPDS, Trigger
from .var import Expr, ReadonlyVar, Var, to_var
from .view import (
    CURRENT_PLAYER,
    EPD_CURRENT_PLAYER,
    EudslError,
    FieldSpec,
    MemFieldHandle,
    MemView,
    MemViewInstance,
    defineMemView,
    emit_bit_test_read,
)

#: Byte address of the runtime `CurrentPlayer` register.
CP_ADDR = 0x6509B0

#: `CP_ADDR` viewed as a dword field.
CP_REGISTER_VIEW = defineMemView(
    "CurrentPlayerRegister",
    4,
    {"value": FieldSpec(0, 4)},
)
CP_REGISTER_FIELD = FieldSpec(0, 4)

#: `CurrentPlayer` page-slot stride in EPDs (one 32-byte action slot).
SLOT_STRIDE_EPDS = ACT_STRIDE_EPDS
#: Extra EPDs needed to hop from the last slot of a page to the next page.
PAGE_HOP_EPDS = TRIGGER_STRIDE_EPDS - ACT_COUNT * ACT_STRIDE_EPDS
#: Slots per `TriggerPage`.
PAGE_SLOTS = ACT_COUNT


def read_current_player_register() -> Var:
    """Materialise the dword at ``CP_ADDR`` (the actual CurrentPlayer value)."""
    return MemFieldHandle(
        CP_REGISTER_VIEW,
        CP_REGISTER_FIELD,
        EPD_CURRENT_PLAYER,
        "value",
    ).read()


def emit_seed_cp_from_slot(slot0: Var) -> None:
    """Point CurrentPlayer at the runtime value field of ``slot0``.

    The mode-1 reloc materialises ``EPD(slot0.value)`` into the action's
    value field. The action then writes that EPD into the CurrentPlayer
    register itself, so ``Deaths(CurrentPlayer, ...)`` reads the slot value.
    """
    act = SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
    act["relocs"] = [
        {
            "field_offset": ACT_FIELD_OFFSETS["value"],
            "target_var": slot0.id,
            "target_offset": 20,
            "mode": 1,
        }
    ]
    Trigger(conds=[], acts=[act], preserved=True)


def cp_live() -> Any:
    """CurrentPlayer slot holds a non-zero member EPD."""
    return Deaths(CURRENT_PLAYER, AtLeast, 1, 0)


def cp_empty() -> Any:
    """CurrentPlayer slot is the zero sentinel."""
    return Deaths(CURRENT_PLAYER, Exactly, 0, 0)


def emit_cp_read_into(dst: Var) -> None:
    """Read the dword at CurrentPlayer into ``dst`` with 32 bit tests."""
    dst.set(0)
    emit_bit_test_read(CURRENT_PLAYER, 32, 0, dst)


def emit_cp_sweep(slot0: Var, capacity: int, iteration: Callable[[], Any]) -> None:
    """Drive ``iteration`` once per storage slot with a CP cursor.

    The actual CurrentPlayer register is saved once per site execution
    and restored after the loop, so nesting is path-complete even when
    the iteration body clobbers CurrentPlayer via ``with_cp``. The slot
    pointer is carried in ``offset`` (EPDs relative to ``slot0``'s
    value field) and re-seeded into CurrentPlayer before every slot;
    a body clobber therefore cannot corrupt the cursor.
    """
    saved = read_current_player_register()
    remaining = Var(0)
    page_remaining = Var(0)
    offset = Var(0)

    remaining.set(capacity)
    page_remaining.set(min(PAGE_SLOTS, capacity))
    offset.set(0)

    def body() -> None:
        emit_seed_cp_from_slot(slot0)
        emitVarStore(offset.id, AddrReloc(CP_ADDR), Add)
        iteration()
        offset.iadd(SLOT_STRIDE_EPDS)
        remaining.isub(1)
        page_remaining.isub(1)
        when(
            [page_remaining.eq(0)],
            lambda: (
                offset.iadd(PAGE_HOP_EPDS),
                page_remaining.set(PAGE_SLOTS),
            ),
        )

    while_loop([remaining.ge(1)], body)
    emitVarStore(saved.id, AddrReloc(CP_ADDR))


def _set_cp_from_var(src: Var) -> None:
    """Store ``src``'s value into the runtime CurrentPlayer register.

    This is the one CP-seed shape shared by ``with_epd`` and ``blockio``'s
    private seeders. ``memcpyEPD`` still leaves CP clobbered at its end value;
    only ``with_epd`` pairs this store with an unconditional ambient restore.
    """
    emitVarStore(src.id, AddrReloc(CP_ADDR), SetTo)


def _require_int_base(base_epd: Any) -> None:
    """Reject a dynamic CP base before any seed act is queued.

    The CP register must hold an EPD, so a `Var` base would seed it with a
    byte-address pointer (reloc mode 4) and every read through the cursor
    would be wrong. A dynamic base is already unusable, so it is rejected
    loudly instead of silently emitting a relative hop.
    """
    if not isinstance(base_epd, int) or isinstance(base_epd, bool):
        raise EudslError(
            "EUDSL-TYPE",
            f"with_cp base must be an int EPD; got {base_epd!r}",
        )


class CPCursor:
    def __init__(self, base_epd: Any) -> None:
        _require_int_base(base_epd)
        self.baseEpd = base_epd
        self.currentOffset: int = 0

    @property
    def current_offset(self) -> int:
        return self.currentOffset

    def moveTo(self, target_dword_offset: int) -> None:
        """Anchor CurrentPlayer at ``baseEpd + target_dword_offset``.

        The act is absolute (`SetTo`), not a delta from build-time state:
        inside a replayed runtime loop body it re-anchors the cursor on
        every pass, so the register cannot drift. Repeating the current
        offset emits nothing.
        """
        from .hir import SetDeaths, SetTo

        if target_dword_offset != self.currentOffset:
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.baseEpd + target_dword_offset, 0)
        self.currentOffset = target_dword_offset

    move_to = moveTo

    def at(self, dword_offset: int) -> MemFieldHandle:
        spec = FieldSpec(offset=dword_offset * 4, size=4)
        schema = defineMemView("CP", (dword_offset + 1) * 4, {"val": spec})
        return MemFieldHandle(schema, spec, self, f"at({dword_offset})")

    def view(self, schema_or_ctor: Any) -> Any:
        if hasattr(schema_or_ctor, "schema"):
            return MemViewInstance(schema_or_ctor.schema, self)
        if isinstance(schema_or_ctor, MemView):
            return MemViewInstance(schema_or_ctor, self)
        if callable(schema_or_ctor):
            return schema_or_ctor(self)
        raise ValueError(f"Invalid schema or constructor passed to cp.view(): {schema_or_ctor}")

    def restore(self) -> None:
        """Re-anchor CurrentPlayer at ``baseEpd`` and reset the build-time offset."""
        from .hir import SetDeaths, SetTo

        if self.currentOffset != 0:
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.baseEpd, 0)
        self.currentOffset = 0


class CPContextManager:
    def __init__(self, epd: Any) -> None:
        _require_int_base(epd)
        self.epd = epd
        self.cursor: CPCursor | None = None

    def __enter__(self) -> CPCursor:
        from .hir import SetDeaths, SetTo

        _require_int_base(self.epd)
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.epd, 0)
        self.cursor = CPCursor(self.epd)
        return self.cursor

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.cursor is not None:
            self.cursor.restore()


def with_cp(epd: Any, body: Callable[[CPCursor], Any] | None = None) -> Any:
    """Bind CurrentPlayer (`0x6509B0`) to `epd` for `body(cp)` or in `with with_cp(epd) as cp:`.

    `epd` must be a constant int EPD. Hops are absolute (`SetTo base + target`),
    so a hop inside a replayed loop body re-anchors instead of drifting;
    `restore` re-anchors to the base. A dynamic base raises `EUDSL-TYPE` before
    any act is queued, because the CP register holds an EPD, not a byte address.
    """
    cm = CPContextManager(epd)
    if body is not None:
        with cm as cp:
            return body(cp)
    return cm


withCP = with_cp


#: Public documentation name for the accepted `with_epd` operand domain.
EpdOperand = int | Var | ReadonlyVar | Expr | Db | DbRef


def _require_epd_operand(epd: Any) -> None:
    """Reject a non-EPD-bearing operand before any act is queued."""
    if isinstance(epd, bool) or not isinstance(
        epd, (int, DbRef, Var, ReadonlyVar, Expr)
    ):
        raise EudslError(
            "EUDSL-TYPE",
            "with_epd epd must be an EPD-bearing value (int, Var, "
            f"ReadonlyVar, Expr, Db, or DbRef); got {epd!r}",
        )


def _require_epd_offset(offset: Any) -> None:
    """Validate the relative pre-hop as a signed 32-bit plain int."""
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < -(2**31)
        or offset > 2**31 - 1
    ):
        raise EudslError(
            "EUDSL-RANGE",
            f"with_epd offset must be a signed 32-bit integer; got {offset!r}",
        )


def _materialize_epd_base(epd: Any) -> Var:
    """Turn an EPD-bearing operand into the private base `Var`.

    A `Db`/`DbRef` routes through the assembly-time blob constructor; an int
    becomes a private constant `Var`; a runtime operand goes through the
    canonical `to_var` materializer.
    """
    if isinstance(epd, DbRef):
        return epd_of_db(epd)
    if isinstance(epd, int):
        return Var(epd & 0xFFFFFFFF)
    return to_var(epd)


def _bind_epd(base: Var, offset: int) -> tuple["EpdCursor", Var]:
    """Bind a private copy of ``base`` to CP and return its ambient save."""
    cursor_base = Var(0)
    cursor_base.set(base)
    if offset:
        cursor_base.iadd(offset)
    saved = read_current_player_register()
    _set_cp_from_var(cursor_base)
    return EpdCursor(cursor_base, 0), saved


class EpdCursor:
    """CP cursor over a **runtime** EPD base, with relative dword hops.

    Unlike :class:`CPCursor`, the base lives in a `Var`, so hops must be
    relative (``Add``) rather than absolute (``SetTo base + target``). Each
    call owns its own base copy; the public constructor is :func:`with_epd`,
    which also saves and restores the ambient CP.
    """

    def __init__(self, base: Var, offset: int = 0) -> None:
        if not isinstance(base, Var):
            raise EudslError(
                "EUDSL-TYPE",
                f"EpdCursor base must be a Var; got {base!r}",
            )
        _require_epd_offset(offset)
        self.base = base
        self.currentOffset = offset

    @property
    def current_offset(self) -> int:
        return self.currentOffset

    def moveTo(self, target_dword_offset: int) -> None:
        """Hop CP by the relative delta to ``target_dword_offset``.

        Repeating the current offset emits nothing. ``restore`` only resets
        this build-time offset; the ambient CP restore belongs to
        :func:`with_epd`.
        """
        if target_dword_offset != self.currentOffset:
            SetDeaths(
                EPD_CURRENT_PLAYER,
                Add,
                target_dword_offset - self.currentOffset,
                0,
            )
            self.currentOffset = target_dword_offset

    move_to = moveTo

    def at(self, dword_offset: int) -> MemFieldHandle:
        """Hop to ``dword_offset`` and return the live-CP dword handle.

        The returned handle is anchored at CP's current position
        (``CPCursor(0).at(0)``), so its ``_prepare_target`` hop is a no-op and
        the relative hop is never emitted twice.
        """
        self.moveTo(dword_offset)
        return CPCursor(0).at(0)

    def restore(self) -> None:
        """Reset the build-time offset; emit nothing (see ``with_epd``)."""
        self.currentOffset = 0


class EpdContextManager:
    """Context-manager form of :func:`with_epd`."""

    def __init__(self, epd: Any, offset: int = 0) -> None:
        _require_epd_operand(epd)
        _require_epd_offset(offset)
        self.epd = epd
        self.offset = offset
        self.cursor: EpdCursor | None = None
        self.saved: Var | None = None

    def __enter__(self) -> EpdCursor:
        _require_epd_operand(self.epd)
        _require_epd_offset(self.offset)
        base = _materialize_epd_base(self.epd)
        self.cursor, self.saved = _bind_epd(base, self.offset)
        return self.cursor

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.saved is not None:
            _set_cp_from_var(self.saved)


def with_epd(
    epd: EpdOperand, body: Callable[[EpdCursor], Any] | None = None, offset: int = 0
) -> Any:
    """Bind CurrentPlayer to a runtime EPD for ``body(cp)`` or a ``with`` block.

    ``epd`` may be a plain int, a `Var`/`ReadonlyVar`/`Expr`, or a `Db`/`DbRef`
    (routed through :func:`epd_of_db`). The runtime base is copied into a
    private scratch `Var`, so a caller's `Var` is never mutated; ``offset``
    pre-hops that copy by a constant dword count.

    The ambient CurrentPlayer value is saved with the shared 32-bit reader and
    restored **unconditionally** (including the exception path and when the
    body clobbers CP). Nesting is LIFO: each call owns its own save and base
    copy, so an inner call restores the outer binding and the outer call
    restores the ambient value. This is strictly stronger than
    :meth:`CPCursor.restore`, which no-ops at offset 0.
    """
    cm = EpdContextManager(epd, offset)
    if body is not None:
        with cm as cp:
            return body(cp)
    return cm


withEpd = with_epd
