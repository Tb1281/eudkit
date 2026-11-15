"""Block memory primitives: ``memcpyEPD`` / ``memsetEPD``.

EPD-primary C-like semantics: copy ``count`` dwords forward (overlapping
ranges are undefined, no memmove), or fill ``count`` dwords with a 32-bit
value. Constant counts fold at build time: ``count == 0`` is a no-op that
emits nothing, a constant ``(dstEPD, value, count)`` fill with
``ceil(count/64) <= MAX_FOLD_CHUNKS`` emits packed ``SetDeaths`` triggers
instead of the runtime loop, and a constant ``count == 1`` copy emits the loop
body once with no loop scaffolding. Constant counts above ``0xFFFFFFFF`` are
rejected with ``EUDSL-RANGE``.

Runtime loops use a check-first ``while_loop`` on a page-backed remaining
counter. ``memcpyEPD`` binds CurrentPlayer (``0x6509B0``) to the source EPD and
reads through a CP cursor; both primitives land each dword through a scratch
pointer whose destination/value fields are patched from runtime vars.

``memcpyEPD``'s source also accepts a module data blob (a ``Db`` / ``DbRef``):
the blob's runtime EPD is only known at assembly time, so the cursor variable
is seeded through a mode-1 blob relocation instead of a build-time constant.
"""

from __future__ import annotations

from .cp import CP_ADDR, CPCursor, _set_cp_from_var
from .hir import (
    Add,
    AddrReloc,
    DbRef,
    SetDeaths,
    SetDeathsX,
    SetTo,
    Subtract,
    TriggerFieldReloc,
    _emit_blob_epd_into,
    emitVarStore,
)
from .temporal import when, while_loop
from .trigger import Trigger, _register_trigger
from .var import Expr, Var, to_var
from .view import (
    EPD_CURRENT_PLAYER,
    EudslError,
    _describe_value as _describe,
    _is_plain_int,
)

# Fill folds emit ceil(count/64) packed SetDeaths triggers. The cap is a static
# *byte* budget, not a crossover with the runtime loop: at 512 chunks the fold
# costs 512T static / 1,232,896 B (~1.18 MiB, the ADR 0015 advisory ceiling) but
# 512T executed instead of ~196,610T. ADR 0001 ranks executed cost first, static
# second, bytes last, so do not lower this back to the old 7T static crossover.
MAX_FOLD_CHUNKS = 512


def _materialize(value: Var | Expr) -> Var:
    """Materialize a block operand; the caller narrowed the domain first.

    `_epd_value` / `_count_value` / `memsetEPD` keep the block operand domain
    (``Var | Expr``; a `ReadonlyVar`, float, etc. is `EUDSL-TYPE` there with
    the surface-specific message). This only performs the shared
    "constant or handle -> Var" step, delegated to the canonical kernel.
    """
    return to_var(value)


def _epd_value(
    value: object, what: str, name: str, *, allow_blob: bool = False
) -> int | Var | DbRef:
    if allow_blob and isinstance(value, DbRef):
        return value
    if _is_plain_int(value):
        if value < 0:
            raise EudslError(
                "EUDSL-TYPE",
                f"{what}: {name} must be a non-negative integer EPD or a "
                f"game-time value, got {_describe(value)}",
            )
        return value & 0xFFFFFFFF
    if isinstance(value, (Var, Expr)):
        return _materialize(value)
    raise EudslError(
        "EUDSL-TYPE",
        f"{what}: {name} must be a non-negative integer EPD or a game-time "
        f"value, got {_describe(value)}",
    )


def _count_value(value: object, what: str) -> tuple[bool, int | Var]:
    """Return ``(is_zero, value)``; a const zero is a build-time no-op."""
    if _is_plain_int(value):
        if value < 0:
            raise EudslError(
                "EUDSL-RANGE",
                f"{what}: count must be a non-negative integer (dwords), got {value}",
            )
        if value > 0xFFFFFFFF:
            raise EudslError(
                "EUDSL-RANGE",
                f"{what}: count must be at most 0xFFFFFFFF dwords, got {value}",
            )
        return value == 0, value & 0xFFFFFFFF
    if isinstance(value, (Var, Expr)):
        return False, _materialize(value)
    raise EudslError(
        "EUDSL-TYPE",
        f"{what}: count must be a non-negative integer or a game-time value, "
        f"got {_describe(value)}",
    )


def _seed_epd_cursor(dst: Var, node: int | Var | DbRef) -> None:
    """Seed a source-cursor variable with an EPD.

    A plain integer or a run-time value is a normal variable store. A `Db`
    payload EPD is only known at assembly time, so the mode-1 relocation writes
    it into the variable's own slot; nothing else in the loop differs.
    """
    if isinstance(node, DbRef):
        _emit_blob_epd_into(dst, node)
        return
    dst.set(node)


def _seed_cp_from_source(src_node: int | Var | DbRef, tmp: Var) -> None:
    """Seed `CurrentPlayer` to ``srcEPD - 1`` for a forward copy loop.

    The body advances CP before every read, so the first read lands on the
    source EPD and a copy of ``count`` dwords ends at ``srcEPD + count - 1``
    (the observable end value). A constant source folds to one direct
    ``SetDeaths``; a runtime source is materialised into ``tmp`` and stored
    through the CP register.
    """
    if isinstance(src_node, DbRef):
        raise AssertionError("blob sources seed CurrentPlayer from a cursor var")
    if isinstance(src_node, Var):
        tmp.set(src_node)
        tmp.isub(1)
        _set_cp_from_var(tmp)
    else:
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, (src_node - 1) & 0xFFFFFFFF, 0)


def _memcpy_constant_one(dst_node: int | Var, src_node: int | Var | DbRef) -> None:
    """Emit the constant-count-1 copy body once with no loop scaffolding."""
    src_cur = Var(0)
    dst_cur = Var(0)
    tmp = Var(0)
    _seed_epd_cursor(src_cur, src_node)
    dst_cur.set(dst_node)

    def emit_copy() -> None:
        _set_cp_from_var(src_cur)
        tmp.set(CPCursor(0).at(0).read())
        emitVarStore(
            dst_cur.id,
            TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
            SetTo,
        )
        emitVarStore(
            tmp.id,
            TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
            SetTo,
        )
        _register_trigger(ptr)

    ptr = Trigger(
        conds=[],
        acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF)],
        preserved=True,
        register=False,
    )

    emit_copy()


def memcpyEPD(dstEPD: object, srcEPD: object, count: object) -> None:
    """Copy ``count`` dwords from EPD ``srcEPD`` to EPD ``dstEPD``.

    ``srcEPD`` may also be a `Db`/`DbRef`, in which case the copy reads the
    blob's payload (e.g. ``memcpyEPD(addr, db, (len(data) + 3) // 4)``).
    """
    what = "memcpyEPD()"
    dst_node = _epd_value(dstEPD, what, "dstEPD")
    src_node = _epd_value(srcEPD, what, "srcEPD", allow_blob=True)
    is_zero, count_node = _count_value(count, what)
    if is_zero:
        return

    # A constant count of 1 runs the check-first loop body exactly once, so it
    # needs no counter. Keep that path on the cursor-variable lowering.
    if _is_plain_int(count) and count == 1:
        _memcpy_constant_one(dst_node, src_node)
        return

    # The counter is set from the source expression once. A constant count
    # still pays one setup store, keeping the loop scaffolding's trigger count
    # identical to the old cursor-variable lowering.
    cnt = Var(0)
    tmp = Var(0)
    cnt.set(count_node)

    # A blob's EPD is only known at assembly time: seed one temp cursor, then
    # (gated) put it into CurrentPlayer. Constant/runtime sources need no
    # cursor var at all.
    src_cursor: Var | None = None
    if isinstance(src_node, DbRef):
        src_cursor = Var(0)
        _seed_epd_cursor(src_cursor, src_node)

    ptr = Trigger(
        conds=[],
        acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF)],
        preserved=True,
        register=False,
    )

    def seed() -> None:
        if isinstance(dst_node, Var):
            emitVarStore(
                dst_node.id,
                TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
                SetTo,
            )
        else:
            SetDeaths(ptr.act(0).dest, SetTo, dst_node, 0)
        if src_cursor is not None:
            emitVarStore(src_cursor.id, AddrReloc(CP_ADDR), SetTo)
            SetDeaths(EPD_CURRENT_PLAYER, Subtract, 1, 0)
        else:
            _seed_cp_from_source(src_node, tmp)

    # A runtime zero count must leave CP and the scratch destination untouched.
    when(cnt.ge(1), seed)

    def body() -> None:
        SetDeaths(EPD_CURRENT_PLAYER, Add, 1, 0)
        tmp.set(CPCursor(0).at(0).read())
        emitVarStore(
            tmp.id,
            TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
            SetTo,
        )
        _register_trigger(ptr)
        SetDeaths(ptr.act(0).dest, Add, 1, 0)
        cnt.isub(1)

    while_loop(cnt.ge(1), body)


def memsetEPD(dstEPD: object, value: object, count: object) -> None:
    """Fill ``count`` dwords at EPD ``dstEPD`` with the 32-bit ``value``."""
    what = "memsetEPD()"
    dst_node = _epd_value(dstEPD, what, "dstEPD")
    is_zero, count_node = _count_value(count, what)
    if is_zero:
        return

    value_var: Var | None = None
    value_const = 0
    if _is_plain_int(value):
        value_const = value & 0xFFFFFFFF
    elif isinstance(value, (Var, Expr)):
        value_var = _materialize(value)
    else:
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: value must be an integer or a game-time value, got "
            f"{_describe(value)}",
        )

    if _is_plain_int(dstEPD) and _is_plain_int(value) and _is_plain_int(count):
        # The whole fill is known at build time: emit one packed SetDeaths
        # trigger per 64 dwords. The cap is a static/byte budget, not a
        # crossover with the loop -- ADR 0001 puts executed cost first, so the
        # fold is worth up to 512T static / 1.18 MiB (the ADR 0015 advisory
        # ceiling) to turn ~196,610T executed into 512T. Do not lower it back.
        chunks = (count_node + 63) // 64
        if chunks <= MAX_FOLD_CHUNKS:
            for base in range(0, count_node, 64):
                span = min(64, count_node - base)
                Trigger(
                    conds=[],
                    acts=[
                        SetDeaths(
                            (dst_node + base + i) & 0xFFFFFFFF, SetTo, value_const, 0
                        )
                        for i in range(span)
                    ],
                )
            return

    # Always emit a runtime store. A constant count cannot use `Var(count_node)`:
    # that is a one-time module init, so a non-folded fill inside a loop or a
    # re-entered `while_loop` would run only on its first pass.
    cnt = Var(0)
    cnt.set(count_node)

    ptr = Trigger(
        conds=[],
        acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF)],
        preserved=True,
        register=False,
    )

    def seed() -> None:
        if isinstance(dst_node, Var):
            emitVarStore(
                dst_node.id,
                TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
                SetTo,
            )
        else:
            SetDeaths(ptr.act(0).dest, SetTo, dst_node, 0)

    # A runtime zero count must leave the scratch destination untouched for
    # every destination form, including a constant EPD.
    when(cnt.ge(1), seed)

    def body() -> None:
        if value_var is not None:
            emitVarStore(
                value_var.id,
                TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
                SetTo,
            )
        else:
            SetDeaths(ptr.act(0).value, SetTo, value_const, 0)
        _register_trigger(ptr)
        SetDeaths(ptr.act(0).dest, Add, 1, 0)
        cnt.isub(1)

    while_loop(cnt.ge(1), body)


memcpy_epd = memcpyEPD
memset_epd = memsetEPD

__all__ = [
    "MAX_FOLD_CHUNKS",
    "memcpyEPD",
    "memcpy_epd",
    "memsetEPD",
    "memset_epd",
]
