"""Zero-storage typed layout views (`MemView`, `CondView`, `ActView`, `CUnit`, `CSprite`)
and StarCraft `EPD` address helpers.
"""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence
import warnings

from .conditions import LitNode, OrNode

EPD_BASE = 0x58A364
EPD_CURRENT_PLAYER = 203155
CURRENT_PLAYER = 13


def EPD(addr: int) -> int:
    """Compute the signed StarCraft EPD index for a 4-byte-aligned address:
    `EPD(addr) = (addr - 0x58A364) // 4`.
    """
    return ((addr & 0xFFFFFFFF) - EPD_BASE) // 4


epd = EPD


# ---------------------------------------------------------------------------
# CUnit slot pool
# ---------------------------------------------------------------------------
#
# The engine's CUnit array is 1699 slots of 336 bytes each. Slots are packed
# back-to-back, so consecutive CUnits start 84 dwords apart. In the shifted
# pointer space ``S = A >> 2`` the slot starts therefore span
# ``[0x16732A, 0x18A052]`` and the last slot starts at byte ``0x628148``.
#
#: Byte address of the first CUnit slot.
CUNIT_POOL_BASE = 0x59CCA8
#: Bytes per CUnit slot (``schema/layouts/cunit.json``).
CUNIT_SLOT_SIZE = 336
#: Dwords per CUnit slot: **the stride that matters** for the EPD bound.
CUNIT_SLOT_EPDS = 84
#: Number of CUnit slots in the engine pool.
CUNIT_SLOT_COUNT = 1699
#: EPD of the first CUnit slot (``EPD(0x59CCA8)``).
CUNIT_BASE_EPD = EPD(CUNIT_POOL_BASE)
#: EPD of the last CUnit slot's first dword (``EPD(0x628148)``).
#:
#: Derived, not ``CUNIT_BASE_EPD + CUNIT_SLOT_COUNT - 1``: each slot is 84
#: dwords, so a naive ``+1698`` lands at EPD 20723 and rejects every real unit
#: after the 21st slot.
CUNIT_LAST_EPD = CUNIT_BASE_EPD + CUNIT_SLOT_EPDS * (CUNIT_SLOT_COUNT - 1)
#: Last byte of the CUnit pool, inclusive.
CUNIT_POOL_END = 0x628297

#: Seed restoring the invariant low bits of the shifted pointer space.
_CUNIT_POINTER_SEED = 0x100002
#: One-shot bias from the shifted pointer space to the EPD space.
_CUNIT_POINTER_BIAS = 0xFFE9D727
#: CUnit-slot address bits 4-21, read as shifted-pointer bits 2-19.
_CUNIT_POINTER_MASK = 0x3FFFF0


def _validate_mask_shift(mask: int, shift: int, field_name: str | None) -> None:
    """Reject a custom mask whose set bits fall below the field's byte shift."""
    below = (mask & ((1 << shift) - 1)) if shift > 0 else 0
    if below:
        label = field_name or "field"
        raise EudslError(
            "EUDSL-BOUNDS",
            f"mask {mask:#x} on {label} has set bits below shift {shift}; "
            "the accumulator cannot represent them in field units",
        )


def _field_value_space(bit_mask: int, shift: int, size: int) -> int:
    """Largest value representable in the field's units, derived from the mask.

    A field's value space is the set of bit patterns inside ``bit_mask``,
    interpreted as field units (``raw >> shift``). Because any bit-subset of
    the mask is representable, its largest member is the whole mask shifted
    down — including for *straddling* fields, where that is smaller than the
    nominal width.

    ``size`` is accepted for signature symmetry with ``FieldSpec``; the mask
    alone determines the space.
    """
    return (bit_mask & 0xFFFFFFFF) >> shift


def _fold_ordering(op: str, c: int, max_val: int) -> str | None:
    """Fold an ordering comparison no value in the field's space can satisfy.

    ``c`` is the constant normalised to 32 bits. Returns ``"never"`` (the
    comparison can never hold), ``"always"`` (it holds for every value), or
    ``None`` (emit the comparison). Only constants *outside* the space fold,
    so well-formed HIR stays byte-identical.
    """
    if op == "ge":
        return "never" if c > max_val else None
    if op == "le":
        return "always" if c > max_val else None
    if op == "gt":
        return "never" if c >= max_val else None
    if op == "lt":
        if c == 0:
            return "never"
        return "always" if c > max_val else None
    raise AssertionError(f"not an ordering op: {op}")


FieldWidth = Literal[1, 2, 4]

ComparisonOp = Literal["eq", "ne", "ge", "le", "gt", "lt"]


@dataclass(frozen=True)
class Comparison:
    """One comparison decided in a field's value space.

    ``fold`` is ``"never"``/``"always"`` when no value in the space can
    satisfy the comparison, ``"defer"`` when a middle ``ne`` constant must be
    materialized at the use site, and ``None`` when the adapter should emit
    ``comparison`` against ``value`` (already shifted into the field's lane).
    """

    fold: Literal["never", "always", "defer"] | None = None
    comparison: int | None = None
    value: int = 0


@dataclass(frozen=True)
class FieldValueSpace:
    """The value space of one masked field and its six comparison decisions.

    A field's value space is the set of bit patterns inside ``bit_mask``,
    interpreted as field units (``raw >> shift``). Because any bit-subset of
    the mask is representable, its largest member is the whole mask shifted
    down — including for *straddling* fields, where that is smaller than the
    nominal width.

    ``size`` is carried for symmetry with :class:`FieldSpec`; the mask alone
    determines the space.

    The three comparison surfaces — ``TriggerFieldRef``, ``MemFieldHandle``
    and ``Var(explicit_mask=...)`` — all ask this object, so the fold table
    and constant normalisation exist once. Writes, lane resolution and the
    concrete HIR record stay with the adapters.
    """

    bit_mask: int
    shift: int
    size: FieldWidth = 4

    @property
    def max_value(self) -> int:
        return _field_value_space(self.bit_mask, self.shift, self.size)

    def lane(self, constant: int) -> int:
        """Normalise a field-unit constant to its in-lane bit pattern."""
        return (constant << self.shift) & 0xFFFFFFFF

    def validate(self, field_name: str | None = None) -> None:
        """Reject a mask whose set bits sit below the field's byte shift."""
        _validate_mask_shift(self.bit_mask, self.shift, field_name)

    def compare(
        self, op: ComparisonOp, constant: int, field_name: str | None = None
    ) -> Comparison:
        """Decide ``op`` against ``constant`` (already normalised to 32 bits)."""
        from .hir import AtLeast, AtMost, Exactly

        max_val = self.max_value
        if op == "eq":
            if constant & ~max_val:
                # A constant the field cannot hold can never equal it.
                return Comparison(fold="never")
            return Comparison(comparison=Exactly, value=self.lane(constant))
        if op == "ne":
            # Validate before folding: an invalid mask must stay a loud error
            # rather than silently becoming an always-true condition.
            self.validate(field_name)
            if constant & ~max_val:
                # Unrepresentable constant ⇒ "always unequal".
                return Comparison(fold="always")
            if constant == 0:
                return Comparison(
                    comparison=AtLeast,
                    value=(1 << self.shift) & 0xFFFFFFFF,
                )
            if constant == max_val:
                return Comparison(
                    comparison=AtMost,
                    value=((max_val - 1) << self.shift) & 0xFFFFFFFF,
                )
            return Comparison(fold="defer")
        if op in ("ge", "le", "gt", "lt"):
            fold = _fold_ordering(op, constant, max_val)
            if fold is not None:
                return Comparison(fold=fold)
            if op == "ge":
                return Comparison(comparison=AtLeast, value=self.lane(constant))
            if op == "le":
                return Comparison(comparison=AtMost, value=self.lane(constant))
            if op == "gt":
                return Comparison(
                    comparison=AtLeast,
                    value=self.lane((constant + 1) & 0xFFFFFFFF),
                )
            return Comparison(
                comparison=AtMost,
                value=self.lane((constant - 1) & 0xFFFFFFFF),
            )
        raise AssertionError(f"not a comparison op: {op}")


def _apply_comparison(
    space: FieldValueSpace,
    op: ComparisonOp,
    rhs: object,
    target: Callable[[], Any],
    *,
    what: str,
    field_name: str | None = None,
    full_mask_uses_deaths: bool = False,
    defer: Callable[[int], Any] | None = None,
) -> Any:
    """Emit (or fold/defer) one comparison through the shared kernel.

    ``target`` is lazy so a folded comparison never resolves a condition
    target — an important no-move guard for CP-backed fields. ``defer`` is
    the adapter's use-site marker factory; only ``ne`` can decide to defer.
    """
    from .hir import Always, Deaths, DeathsX, Never

    if op == "ne":
        # Preserve the historical precedence: an invalid mask is a loud
        # EUDSL-BOUNDS before the operand guard names a bad constant.
        space.validate(field_name)
    constant = _require_plain_int(rhs, what) & 0xFFFFFFFF
    plan = space.compare(op, constant, field_name)
    if plan.fold == "never":
        return Never()
    if plan.fold == "always":
        return Always()
    if plan.fold == "defer":
        if defer is None:
            raise AssertionError(f"{op} deferred without a marker factory")
        return defer(constant)
    assert plan.comparison is not None
    # A whole-dword `Var` keeps the compact `Deaths` row; the field adapters
    # always use `DeathsX`, exactly as before the kernel existed.
    if full_mask_uses_deaths and space.bit_mask == 0xFFFFFFFF:
        return Deaths(target(), plan.comparison, plan.value, 0)
    return DeathsX(target(), plan.comparison, plan.value, 0, space.bit_mask)


def emit_bit_test_read(
    target: Any,
    num_bits: int,
    shift: int,
    dst: Any,
    mask: int | None = None,
    field_name: str | None = None,
    extra_conds: Sequence[Any] | None = None,
) -> None:
    """Append one bit-test trigger per bit of ``target`` into ``dst``.

    Each trigger adds ``1 << bit`` to the accumulator when the corresponding
    bit of the source field is set. The caller owns the accumulator reset, so
    reset ordering (including any queued CP move) stays at the read site.

    With ``mask`` given, emit one trigger per **set bit of ``mask``** at or
    above ``shift`` (``add_val = 1 << (bit_pos - shift)``), so the accumulator
    equals ``raw & mask`` in field units. The default ``None`` path is the
    exact ``num_bits`` window and must stay byte-identical: it keeps the cost
    gate frozen.

    With ``extra_conds`` given, every bit-test trigger also carries those
    conditions, prepended before the bit condition. The ``FixedArray.get``
    reader gates its 32 tests with ``index < length`` so an out-of-range
    index leaves the result at 0, matching the per-element unroll. The
    default ``None`` path is unchanged.
    """
    from .hir import Add, AtLeast, DeathsX, SetDeathsX
    from .trigger import Trigger

    if mask is None:
        bit_positions = list(range(shift, shift + num_bits))
    else:
        _validate_mask_shift(mask, shift, field_name)
        bit_positions = [p for p in range(shift, 32) if (mask >> p) & 1]

    guard = list(extra_conds) if extra_conds is not None else []
    for bit_pos in bit_positions:
        mask_bit = (1 << bit_pos) & 0xFFFFFFFF
        add_val = (1 << (bit_pos - shift)) & 0xFFFFFFFF
        Trigger(
            conds=[*guard, DeathsX(target, AtLeast, mask_bit, 0, mask_bit)],
            acts=[SetDeathsX(dst.value, Add, add_val, 0, 0xFFFFFFFF)],
        )


def read_epd(
    epd: int,
    bits: int,
    shift: int,
    mask: int | None = None,
) -> Any:
    """Read ``bits`` bits from the dword at ``epd`` into a fresh `Var`.

    The accumulator reset is owned by this read site (never by
    :func:`emit_bit_test_read`), so a caller that queued a CP move keeps its
    ordering. This helper itself is for direct reads with no queued CP move.

    ``mask`` follows :func:`emit_bit_test_read`: with a mask, one trigger per
    set bit of the mask at or above ``shift``; without it, the exact ``bits``
    window starting at ``shift``.

    A full 32-bit dword read (``bits == 32``, ``shift == 0``, full mask) is
    the shared-reader shape: it goes through :func:`_shared_dword_reader`
    exactly like ``MemFieldHandle.read()``. Inlining the 32 bit tests here
    would make a direct caller (e.g. ``bgm``) pay the per-site reader again.
    """
    if bits == 32 and shift == 0 and (mask is None or mask == 0xFFFFFFFF):
        return _shared_dword_reader()(epd)

    from .trigger import Trigger
    from .var import Var

    res = Var(0)
    # Reset the accumulator at the read site: a re-entered body must not
    # accumulate onto the previous pass's result.
    Trigger(conds=[], acts=[res.act_set(0)], preserved=True)
    emit_bit_test_read(epd, bits, shift, res, mask=mask)
    return res


def read_epd_pointer(epd: int) -> Any:
    """Read the dword at ``epd`` and return its *EPD* (eudplib ``f_epdread_epd``).

    The dword holds a byte address ``D`` (the game's own pointer, e.g. the
    table pointer at ``0x58D740``). ``EPD(D) = (D - 0x58A364) // 4``, so
    dropping bits 0-1 (addresses are dword aligned) and subtracting
    ``EPD_BASE // 4`` folds the conversion into the bit-test accumulator the
    read already pays for.
    """
    res = read_epd(epd, 30, 2, mask=0xFFFFFFFC)
    res.isub(EPD_BASE // 4)
    return res


def read_cunit_pointer(ptr_epd: Any) -> Any:
    """Read the CUnit byte pointer at ``ptr_epd`` and return its EPD (20T inline).

    ``ptr_epd`` is the EPD of the dword that holds the CUnit byte pointer: one
    of the list-head globals or a node's own ``next``/``prev`` link. The result
    is a `Var` holding the target CUnit's EPD in the absolute eudkit space
    (``EPD(0x59CCA8) = 19025``), ready for `with_epd`, ``DeathsX`` or
    ``CUnit(epd)``.

    An ``int`` target inlines the 18 bit tests — **20T executed**, no call
    sequence. A runtime target binds CP via :func:`py.cp.with_epd` and calls
    the shared CP-relative body; prefer :func:`read_cunit_pointer_saved` inside
    a loop, because a runtime target pays the 33T ambient CP read per call
    (about 60T) instead of hoisting it outside the loop.

    The reader does **not** validate its result. A null pointer reads as
    ``0xFFF9D729`` (seed plus bias); gate the walk with :func:`in_cunit_pool`,
    whose upper bound rejects that sentinel, instead of raising here.
    """
    from .trigger import Trigger
    from .var import Expr, ReadonlyVar, Var

    if isinstance(ptr_epd, bool) or not isinstance(
        ptr_epd, (int, Var, ReadonlyVar, Expr)
    ):
        raise EudslError(
            "EUDSL-TYPE",
            "CUnit pointer EPD must be a plain integer or EPD value; "
            f"got {_describe_value(ptr_epd)}",
        )

    if _is_plain_int(ptr_epd):
        res = Var(0)
        # Seeded reset, not `res.set(0)`: the invariant bits of the shifted
        # pointer space (0, 1 and 20) are folded into the seed so the body is
        # exactly 1 reset + 18 tests + 1 bias = 20T.
        Trigger(
            conds=[],
            acts=[res.act_set(_CUNIT_POINTER_SEED)],
            preserved=True,
        )
        emit_bit_test_read(
            int(ptr_epd) & 0xFFFFFFFF,
            18,
            2,
            res,
            mask=_CUNIT_POINTER_MASK,
        )
        res.iadd(_CUNIT_POINTER_BIAS)
        return res

    from .cp import with_epd

    return with_epd(ptr_epd, lambda _cp: _declare_cunit_reader()())


def read_cunit_pointer_saved(saved_cp: Any, ptr_epd: Any) -> Any:
    """Loop form: caller already holds the ambient CP value in ``saved_cp``.

    Hoist the 33T ambient CP read outside the loop, then call this once per
    node. It never reads the ambient CP: one store, the shared call, one
    restore, plus the caller's own advance — about 28T per node — instead of
    the ~60T :func:`read_cunit_pointer` pays for a runtime target.
    """
    from .cp import _with_epd_saved

    return _with_epd_saved(
        saved_cp, ptr_epd, lambda: _declare_cunit_reader()()
    )


def in_cunit_pool(epd: Any) -> list[Any]:
    """``CUNIT_BASE_EPD <= epd <= CUNIT_LAST_EPD`` as two splicable conditions.

    Returns ``[epd.ge(CUNIT_BASE_EPD), epd.le(CUNIT_LAST_EPD)]`` so a loop gate
    can do ``[*in_cunit_pool(ptr), done.eq(0)]`` without wrapping a trigger.

    A null pointer reads as ``0xFFF9D729``, which is **above**
    ``CUNIT_LAST_EPD`` (161,657): the upper bound, not the lower bound, is what
    rejects end-of-list. This is a predicate, never a raise, so a hijacked list
    stays observable and diagnosable.
    """
    from .var import to_var

    value = to_var(epd)
    return [value.ge(CUNIT_BASE_EPD), value.le(CUNIT_LAST_EPD)]


# Shared full-dword reader subroutines, one body per target address.
#
# The inline reader is 32 bit-test triggers per call site. Declaring it once
# and calling it makes a site pay the call sequence instead, so K sites of an
# M-trigger reader cost O(K + M) triggers and ~K * 2408 bytes instead of
# O(K x M).
#
# The engine reads a condition's `player` field as a literal EPD, so a shared
# body cannot take the address as a runtime variable (the field would name the
# variable's slot, not the dword it holds). Two ways out: patch the 32 fields
# per call (32 acts per call, which regresses executed cost) or emit one body
# per distinct address. We cache per address: sites that read the same field
# share a body exactly, and the body keeps literal `player` fields, so a call
# costs a jump and the return copy — never a per-call rewrite.
_DWORD_READ_KERNELS: "weakref.WeakKeyDictionary[Any, dict[int, Any]]" = (
    weakref.WeakKeyDictionary()
)


class _SharedDwordReader:
    """Callable handle for the shared full-dword readers.

    Calling it with an EPD looks up (and on first use declares) the reader body
    for that address; the call returns a fresh `Var` holding the dword.
    """

    def __init__(self, builder: Any, kernels: dict[int, Any]) -> None:
        self._builder = builder
        self._kernels = kernels

    def __call__(self, epd: int) -> Any:
        epd = _require_plain_int(epd, "field read target") & 0xFFFFFFFF
        handle = self._kernels.get(epd)
        if handle is None:
            handle = _declare_dword_reader(epd)
            self._kernels[epd] = handle
        return handle()


def _shared_dword_reader() -> _SharedDwordReader:
    """Return this build's shared full-dword reader, declaring bodies lazily."""
    from .trigger import _get_active_module_builder

    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE", "field.read() must be called inside buildModule()"
        )
    kernels = _DWORD_READ_KERNELS.get(builder)
    if kernels is None:
        kernels = {}
        _DWORD_READ_KERNELS[builder] = kernels
    return _SharedDwordReader(builder, kernels)


def _declare_dword_reader(epd: int) -> Any:
    """Declare the 32-bit reader body for one target EPD."""
    from .hir import Add, AtLeast, DeathsX, SetDeathsX
    from .temporal import func
    from .trigger import Trigger
    from .var import Var

    def kernel_body() -> Any:
        res = Var(0)
        res.set(0)
        for bit in range(32):
            mask_bit = (1 << bit) & 0xFFFFFFFF
            Trigger(
                conds=[DeathsX(epd, AtLeast, mask_bit, 0, mask_bit)],
                acts=[SetDeathsX(res.value, Add, mask_bit, 0, 0xFFFFFFFF)],
            )
        return res

    return func(kernel_body)


# One CUnit reader body per builder. Unlike the shared dword reader there is no
# per-address cache: the body's condition target is always the literal
# `CURRENT_PLAYER` EPD, and `with_epd`/`_with_epd_saved` bind CP around the call.
_CUNIT_READERS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def _declare_cunit_reader() -> Any:
    """Declare (once per builder) the shared CP-relative CUnit pointer body."""
    from .temporal import func
    from .trigger import _get_active_module_builder
    from .var import Var

    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE",
            "read_cunit_pointer() must be called inside buildModule()",
        )
    handle = _CUNIT_READERS.get(builder)
    if handle is None:

        def kernel_body() -> Any:
            res = Var(0)
            res.set(_CUNIT_POINTER_SEED)
            emit_bit_test_read(
                CURRENT_PLAYER,
                18,
                2,
                res,
                mask=_CUNIT_POINTER_MASK,
            )
            res.iadd(_CUNIT_POINTER_BIAS)
            return res

        handle = func(kernel_body)
        _CUNIT_READERS[builder] = handle
    return handle


class EudslError(ValueError):
    """Loud structured DSL error carrying a canonical error code (e.g. `EUDSL-BOUNDS`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _build_time_truthiness_error(what: str = "condition") -> EudslError:
    """The `EUDSL-COND-TRUTHIFY` error for a runtime value used as a Python bool.

    A condition is a HIR record describing an in-game test, not a build-time
    answer; `if`/`while`/`assert`/`and`/`or`/`not` on one would silently pick
    a branch while `buildModule` runs (ADR 0020).
    """
    return EudslError(
        "EUDSL-COND-TRUTHIFY",
        f"a runtime {what} is not a build-time Python bool; "
        "branch in-game with when()/once()/while_loop()",
    )


def _is_plain_int(value: object) -> bool:
    """`True` for `int` values that are not `bool` (bools are ints in Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _describe_value(value: object) -> str:
    """Short render of an operand for an `EUDSL-TYPE` message."""
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return repr(value)
    return f"a {type(value).__name__}"


def _require_plain_int(value: object, what: str) -> int:
    """Return `value` as an `int`, or raise `EUDSL-TYPE` naming the operand."""
    if not _is_plain_int(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what} must be a plain integer, got {_describe_value(value)}",
        )
    return int(value)


def _require_aligned_dword_address(address: int, subject: str) -> None:
    """Reject an absolute dword address that is not 4-byte aligned.

    A whole-dword read/write names one of ``EPD_BASE + 4 * epd``.  An unaligned
    address silently rounds to the containing dword, and the two conversions in
    the tree round in different directions: ``EPD()`` floors while
    ``eudkit_lir::epd_u32`` truncates (they agree only on aligned input, which
    is why alignment is the seam's job).  Measured with ``0x57F0B2``: the gate
    read the dword at ``0x57F0B0`` and the store wrote the dword at
    ``0x57F0B4`` with no error anywhere.

    This is *not* a guard for byte-lane access.  ``DatFieldHandle``,
    ``SetStringDword`` and mode-1 string relocations deliberately floor an
    unaligned field address and mask the byte lane; that names a byte inside
    the containing dword, not a whole-dword access, so it must not be routed
    through this check (and ``EPD()`` itself stays unguarded).
    """
    folded = address & 0xFFFFFFFF
    if folded & 3:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"{subject} 0x{folded:x} is not 4-byte aligned",
        )


class EudslWarning(UserWarning):
    """Structured DSL warning carrying a canonical warning code (e.g. `EUDSL-WARN-PERF`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


_warning_listeners: list[Callable[[str, str], None]] = []


def on_warning(fn: Callable[[str, str], None]) -> Callable[[], None]:
    _warning_listeners.append(fn)

    def unsubscribe() -> None:
        if fn in _warning_listeners:
            _warning_listeners.remove(fn)

    return unsubscribe


onWarning = on_warning


def emit_warning(code: str, message: str, *, stacklevel: int = 3) -> None:
    formatted = f"{code}: {message}"
    for listener in list(_warning_listeners):
        listener(code, formatted)
    warnings.warn(EudslWarning(code, message), stacklevel=stacklevel)


emitWarning = emit_warning

#: One canonical EUDSL-WARN-SCOPE message for every lazy* factory, quoted
#: verbatim by ADR 0005, GLOSSARY.md, and the cross-frontend tests.
WARN_SCOPE_CODE = "EUDSL-WARN-SCOPE"
WARN_SCOPE_MESSAGE = (
    "lazy* declared inside buildModule; use eager Var for in-module declarations"
)


def warn_lazy_in_builder() -> None:
    """Emit the canonical ``EUDSL-WARN-SCOPE`` warning when a ``lazy*``
    factory is called inside ``build_module``.

    Warning dispatch lives in this module, next to :class:`EudslWarning` and
    :func:`emit_warning`, so every declaration module can import it without
    a cycle (the string module used to own it, but string is itself a lazy
    factory module). The builder check is a local import for the same
    reason: ``trigger`` imports this module.
    """
    from .trigger import _get_active_module_builder

    if _get_active_module_builder() is not None:
        emit_warning(WARN_SCOPE_CODE, WARN_SCOPE_MESSAGE, stacklevel=4)


@dataclass(frozen=True)
class FieldSpec:
    offset: int
    size: FieldWidth
    mask: int | None = None
    pointer: str | None = None
    composite: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemView:
    name: str
    size: int
    fields: Mapping[str, FieldSpec]


def defineMemView(name: str, size: int, fields: dict[str, FieldSpec]) -> MemView:
    return MemView(name=name, size=size, fields=MappingProxyType(dict(fields)))


CondView = defineMemView(
    "CondView",
    20,
    {
        "mask": FieldSpec(0, 4),
        "player": FieldSpec(4, 4),
        "amount": FieldSpec(8, 4),
        "unit": FieldSpec(12, 2),
        "comparison": FieldSpec(14, 1),
        "condtype": FieldSpec(15, 1),
        "restype": FieldSpec(16, 1),
        "flags": FieldSpec(17, 1),
        "eudx": FieldSpec(18, 2),
    },
)

ActView = defineMemView(
    "ActView",
    32,
    {
        "mask": FieldSpec(0, 4),
        "strid": FieldSpec(4, 4),
        "wavid": FieldSpec(8, 4),
        "time": FieldSpec(12, 4),
        "dest": FieldSpec(16, 4),
        "value": FieldSpec(20, 4),
        "unit": FieldSpec(24, 2),
        "acttype": FieldSpec(26, 1),
        "modifier": FieldSpec(27, 1),
        "flags": FieldSpec(28, 1),
        "pad": FieldSpec(29, 1),
        "eudx": FieldSpec(30, 2),
    },
)


@dataclass(frozen=True)
class TriggerFieldRef:
    """Bound field reference inside a `Trigger (16C, 64A)` record or `TriggerPage` `Var`."""

    triggerId: int
    byteOffset: int
    size: FieldWidth
    initialValue: int = 0
    varId: int | None = None
    customMask: int | None = None

    @classmethod
    def forVar(
        cls,
        var_id: int,
        byte_offset: int,
        size: FieldWidth,
        initial_value: int = 0,
    ) -> "TriggerFieldRef":
        return cls(-1, byte_offset, size, initial_value & 0xFFFFFFFF, var_id)

    @property
    def alignedByteOffset(self) -> int:
        return self.byteOffset - (self.byteOffset & 3)

    @property
    def epdOffset(self) -> int:
        return self.alignedByteOffset // 4

    @property
    def fieldName(self) -> None:
        """No symbolic name: a raw field reference is addressed by offset."""
        return None

    def _cond_target(self) -> "TriggerFieldRef":
        """Condition target for this surface — the reference itself.

        ``MemFieldHandle`` resolves an absolute EPD here; a raw field
        reference already names its slot directly, so the deferred marker can
        treat both surfaces uniformly.

        @internal Seam helper: the ``TriggerFieldRef`` analogue of
        ``MemFieldHandle._cond_target``.
        """
        return self

    @property
    def subp(self) -> int:
        return self.byteOffset & 3

    @property
    def shift(self) -> int:
        return (self.byteOffset & 3) * 8

    @property
    def bitMask(self) -> int:
        if self.customMask is not None:
            return self.customMask & 0xFFFFFFFF
        raw = (
            0xFFFFFFFF
            if self.size == 4
            else (0xFFFF if self.size == 2 else 0xFF)
        )
        return (raw << self.shift) & 0xFFFFFFFF

    def _value_space(self) -> FieldValueSpace:
        """This field's comparison kernel (the one source of comparison truth)."""
        return FieldValueSpace(self.bitMask, self.shift, self.size)

    def __bool__(self) -> bool:
        """Refuse build-time branching: a field read is a runtime value."""
        raise _build_time_truthiness_error("value")

    def __int__(self) -> int:
        return self.initialValue

    def __index__(self) -> int:
        return self.initialValue

    def act_set(self, rhs: int) -> dict[str, Any]:
        from .hir import SetDeathsX, SetTo
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        return SetDeathsX(self, SetTo, val, 0, self.bitMask)

    def act_add(self, rhs: int) -> dict[str, Any]:
        from .hir import Add, SetDeathsX
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        return SetDeathsX(self, Add, val, 0, self.bitMask)

    def act_sub(self, rhs: int) -> dict[str, Any]:
        from .hir import SetDeathsX, Subtract
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        return SetDeathsX(self, Subtract, val, 0, self.bitMask)

    def set(self, rhs: int) -> Any:
        return self.act_set(rhs)

    def iadd(self, rhs: int) -> Any:
        return self.act_add(rhs)

    def isub(self, rhs: int) -> Any:
        return self.act_sub(rhs)

    def eq(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "eq",
            rhs,
            lambda: self,
            what="field comparison",
        )

    def ge(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "ge",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def le(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "le",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def gt(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "gt",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def lt(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "lt",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def ne(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "ne",
            rhs,
            self._cond_target,
            what="field comparison",
            field_name=self.fieldName,
            defer=lambda c: DeferredNe(self, c),
        )

    def read(self) -> Any:
        from .trigger import Trigger
        from .var import Var
        res = Var(0)
        # Reset the accumulator every time this read site executes (reads
        # inside loops would otherwise accumulate into the previous result).
        Trigger(conds=[], acts=[res.act_set(0)], preserved=True)
        emit_bit_test_read(
            self, self.size * 8, self.shift, res, mask=self.bitMask
        )
        return res

    def __eq__(self, other: Any) -> Any:
        if isinstance(other, (int, float, str, bool)):
            return self.eq(other)
        return NotImplemented

    def __ne__(self, other: Any) -> Any:
        if isinstance(other, (int, float, str, bool)):
            return self.ne(other)
        return NotImplemented

    def __ge__(self, other: Any) -> Any:
        return self.ge(other)

    def __le__(self, other: Any) -> Any:
        return self.le(other)

    def __gt__(self, other: Any) -> Any:
        return self.gt(other)

    def __lt__(self, other: Any) -> Any:
        return self.lt(other)

    def __iadd__(self, other: Any) -> Any:
        self.iadd(other)
        return self

    def __isub__(self, other: Any) -> Any:
        self.isub(other)
        return self


# ---------------------------------------------------------------------------
# MemFieldHandle: field handle on a MemView instance
# ---------------------------------------------------------------------------

class MemFieldHandle:
    def __init__(
        self,
        schema: MemView,
        spec: FieldSpec,
        base_epd: Any,
        field_name: str,
    ) -> None:
        self.schema = schema
        self.spec = spec
        self.baseEpd = base_epd
        self.fieldName = field_name

        self.byteOffset = spec.offset
        self.size = spec.size
        subp = spec.offset & 3
        self.subp = subp
        self.shift = subp * 8
        self.alignedByteOffset = spec.offset - subp
        self.dwordOffset = spec.offset // 4

        raw_mask = 0xFFFFFFFF if spec.size == 4 else (0xFFFF if spec.size == 2 else 0xFF)
        self.bitMask = spec.mask if spec.mask is not None else ((raw_mask << self.shift) & 0xFFFFFFFF)

        self.epd: int | None = (
            base_epd + self.dwordOffset if isinstance(base_epd, int) else None
        )

    def _is_cp(self) -> bool:
        return hasattr(self.baseEpd, "moveTo") or hasattr(self.baseEpd, "move_to")

    def _prepare_target(self) -> int:
        if self._is_cp():
            if hasattr(self.baseEpd, "moveTo"):
                self.baseEpd.moveTo(self.dwordOffset)
            else:
                self.baseEpd.move_to(self.dwordOffset)
            return CURRENT_PLAYER  # 13
        if isinstance(self.baseEpd, int):
            return self.baseEpd + self.dwordOffset
        raise EudslError(
            "EUDSL-TARGET",
            f"Dynamic Var EPD access on field '{self.fieldName}' requires with_cp scope",
        )

    def _cond_target(self) -> int:
        """Absolute EPD for a condition: no act, so no scope/alias state.

        ``with_cp(base)`` rejects a dynamic base, so ``base + dwordOffset``
        names the same dword at every evaluation site. ``base == 0`` is the
        internal "follow the live CP" sentinel (``CPCursor(0)`` from ``read()``),
        so keep the hop path for it.

        @internal Seam helper: the condition-only analogue of
        ``_prepare_target``.
        """
        if self._is_cp():
            base = getattr(self.baseEpd, "baseEpd", None)
            if isinstance(base, int) and not isinstance(base, bool) and base != 0:
                return base + self.dwordOffset
        return self._prepare_target()

    def _value_space(self) -> FieldValueSpace:
        """This field's comparison kernel (the one source of comparison truth)."""
        return FieldValueSpace(self.bitMask, self.shift, self.size)

    def act_set(self, rhs: int) -> Any:
        from .hir import SetDeathsX, SetTo
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        target = self._prepare_target()
        return SetDeathsX(target, SetTo, val, 0, self.bitMask)

    def act_add(self, rhs: int) -> Any:
        from .hir import Add, SetDeathsX
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        target = self._prepare_target()
        return SetDeathsX(target, Add, val, 0, self.bitMask)

    def act_sub(self, rhs: int) -> Any:
        from .hir import SetDeathsX, Subtract
        val = ((_require_plain_int(rhs, "field write") & 0xFFFFFFFF) << self.shift) & 0xFFFFFFFF
        target = self._prepare_target()
        return SetDeathsX(target, Subtract, val, 0, self.bitMask)

    def set(self, rhs: int) -> None:
        self.act_set(rhs)

    def iadd(self, rhs: int) -> None:
        self.act_add(rhs)

    def isub(self, rhs: int) -> None:
        self.act_sub(rhs)

    def eq(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "eq",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def ge(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "ge",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def le(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "le",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def gt(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "gt",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def lt(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "lt",
            rhs,
            self._cond_target,
            what="field comparison",
        )

    def ne(self, rhs: int) -> Any:
        return _apply_comparison(
            self._value_space(),
            "ne",
            rhs,
            self._cond_target,
            what="field comparison",
            field_name=self.fieldName,
            defer=lambda c: DeferredNe(self, c),
        )

    def read(self) -> Any:
        target = self._prepare_target()
        if self.size == 4 and self.shift == 0 and self.bitMask == 0xFFFFFFFF:
            # Full dword: one shared kernel serves every site (see
            # `_shared_dword_reader`). Wider/partial fields keep the inline
            # bit-test reader below.
            return _shared_dword_reader()(target)
        from .trigger import Trigger
        from .var import Var
        res = Var(0)
        # `_prepare_target()` may queue a CP move as an immediate act; this
        # unconditional trigger consumes it and resets the accumulator, so
        # repeated reads (loops) start from zero.
        Trigger(conds=[], acts=[res.act_set(0)], preserved=True)
        emit_bit_test_read(
            target,
            self.size * 8,
            self.shift,
            res,
            mask=self.bitMask,
            field_name=self.fieldName,
        )
        return res

    def __bool__(self) -> bool:
        """Refuse build-time branching: a field read is a runtime value."""
        raise _build_time_truthiness_error("value")

    def __eq__(self, other: Any) -> Any:
        return self.eq(other)

    def __ne__(self, other: Any) -> Any:
        return self.ne(other)

    def __ge__(self, other: Any) -> Any:
        return self.ge(other)

    def __le__(self, other: Any) -> Any:
        return self.le(other)

    def __gt__(self, other: Any) -> Any:
        return self.gt(other)

    def __lt__(self, other: Any) -> Any:
        return self.lt(other)

    def __iadd__(self, other: Any) -> Any:
        self.iadd(other)
        return self

    def __isub__(self, other: Any) -> Any:
        self.isub(other)
        return self


class DeferredNe:
    """``field.ne(k)`` for a middle constant, materialized at the use site.

    The eager path built ``diff = k; diff.isub(read())`` at construction, so a
    ``while_loop`` gate re-checked a stale ``diff`` every pass. This marker
    records intent instead: every :meth:`materialize` resets ``diff`` to ``k``
    and then queues exactly the acts the eager path queued into the same
    temporaries. The first call allocates those temporaries and later calls
    reuse them; both emit the reset, so the loop tail rebuilds the comparison
    from live memory and a one-shot site (``when``, a raw ``Trigger``) starts
    every pass clean.

    Any field surface works — ``MemFieldHandle`` and ``TriggerFieldRef`` share
    ``size``/``shift``/``bitMask``, ``lt``/``gt`` and a condition target
    (``_cond_target``) — so both `ne` implementations defer through this one
    marker.

    The read target is resolved through the surface's absolute-EPD
    :meth:`MemFieldHandle._cond_target`, so a CP-backed field needs no hop.
    """

    def __init__(self, surface: Any, ne_const: int) -> None:
        self.surface = surface
        self.ne_const = ne_const
        self._diff: Any = None
        self._res: Any = None
        self._inv: Any = None

    def __bool__(self) -> bool:
        """Refuse build-time branching: `field != k` is a runtime condition."""
        raise _build_time_truthiness_error("condition")

    def gate_plan(self) -> OrNode:
        """The plan whose union is `field != k`: one branch per side of `k`."""
        _validate_mask_shift(
            self.surface.bitMask, self.surface.shift, self.surface.fieldName
        )
        return OrNode((
            LitNode(self.surface.lt(self.ne_const)),
            LitNode(self.surface.gt(self.ne_const)),
        ))

    def complement(self) -> Any:
        """`field != k` negates to the direct `field == k` cond (1C, 0T)."""
        return self.surface.eq(self.ne_const)

    def materialize(self) -> dict[str, Any]:
        from .hir import AtLeast, Deaths
        from .trigger import Trigger
        from .var import Var, _saturating_sub_into

        h = self.surface
        target = h._cond_target()  # absolute EPD; no act, no hop
        # Allocation order must match the eager path: diff, res, inv.
        if self._diff is None:
            self._diff = Var(self.ne_const)
        if self._res is None:
            self._res = Var(0)
        if self._inv is None:
            self._inv = Var(0)

        # Reset the accumulator, then one bit test per in-mask bit.
        Trigger(conds=[], acts=[self._res.act_set(0)], preserved=True)
        emit_bit_test_read(
            target,
            h.size * 8,
            h.shift,
            self._res,
            mask=h.bitMask,
            field_name=h.fieldName,
        )
        # diff = k - res, wrapping, exactly as Var.isub(Var) emits it — but
        # into cached temporaries, so a loop rebuild allocates nothing.
        self._inv.set(0xFFFFFFFF)
        _saturating_sub_into(self._inv, self._res)
        # The declaration init runs once; every evaluation site needs the
        # reset re-emitted, including the first (one-shot) one.
        self._diff.set(self.ne_const)
        self._diff.iadd(1)
        self._diff.iadd(self._inv)
        return Deaths(self._diff.value, AtLeast, 1, 0)


# ---------------------------------------------------------------------------
# Schema Normalization & Resolution
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    return name.replace("_", "").lower()


# Keyed by schema *identity*, mirroring the TS `WeakMap<MemViewSchema, …>`
# (`ts/view.ts`). `MemView` is an unhashable frozen dataclass (its `fields`
# Mapping has no `__hash__`) and is exported public API, so it cannot be a
# dict/weak key without changing its equality semantics. `id()` plus the
# retained schema is the same identity key with no semantic change: retaining
# the schema keeps its `id()` from being recycled onto a different object, and
# the `is` check rejects a stale entry all the same.
_schema_indices: dict[int, tuple[MemView, dict[str, tuple[str, FieldSpec]]]] = {}


def _build_schema_index(schema: MemView) -> dict[str, tuple[str, FieldSpec]]:
    cached = _schema_indices.get(id(schema))
    if cached is not None and cached[0] is schema:
        return cached[1]

    idx: dict[str, tuple[str, FieldSpec]] = {}
    for key, spec in schema.fields.items():
        item = (key, spec)
        idx[key] = item
        idx[_normalize_name(key)] = item

        for alias in spec.aliases:
            idx[alias] = item
            idx[_normalize_name(alias)] = item

    def add_alias(canonical: str, *aliases: str) -> None:
        found = idx.get(canonical) or idx.get(_normalize_name(canonical))
        if found:
            for a in aliases:
                idx[a] = found
                idx[_normalize_name(a)] = found

    add_alias("hp", "hitPoints", "hit_points")
    add_alias("shield", "shields")
    add_alias("order", "orderID", "order_id")
    add_alias("owner", "playerID", "player_id")
    add_alias("posX", "pos_x")
    add_alias("posY", "pos_y")

    _schema_indices[id(schema)] = (schema, idx)
    return idx


def _resolve_field(schema: MemView, prop: str) -> tuple[str, FieldSpec] | None:
    idx = _build_schema_index(schema)
    return idx.get(prop) or idx.get(_normalize_name(prop))


# ---------------------------------------------------------------------------
# MemViewInstance: property access & mutation
# ---------------------------------------------------------------------------

class MemViewInstance:
    def __init__(self, schema: MemView, base_epd: Any) -> None:
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "baseEpd", base_epd)

    def __getattr__(self, name: str) -> MemFieldHandle:
        if name in ("schema", "baseEpd"):
            return object.__getattribute__(self, name)
        resolved = _resolve_field(self.schema, name)
        if resolved is not None:
            canonical_name, spec = resolved
            return MemFieldHandle(self.schema, spec, self.baseEpd, canonical_name)
        raise EudslError(
            "EUDSL-BOUNDS",
            f"Unknown field '{name}' on layout '{self.schema.name}'",
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("schema", "baseEpd"):
            object.__setattr__(self, name, value)
            return
        field = self.__getattr__(name)
        if (
            isinstance(value, MemFieldHandle)
            and value.schema is field.schema
            and value.spec is field.spec
            and (
                value.baseEpd is field.baseEpd
                or (_is_plain_int(value.baseEpd) and value.baseEpd == field.baseEpd)
            )
        ):
            # `u.hp += k` / `u.hp -= k`: __iadd__/__isub__ already queued the
            # act and returned a handle for this same field; the attribute
            # write-back must not treat that handle as a value. Mirrors
            # TriggerStructInstance.__setattr__ (py/struct.py:192-209).
            #
            # The second arm exists because a `Var` base's `__eq__` returns a
            # `DeferredCond` (a runtime condition), so `value.baseEpd ==
            # field.baseEpd` on two `Var` bases truth-tests a condition while
            # building and raised `EUDSL-COND-TRUTHIFY` (cycle-15 review S2).
            # Compare plain integers only; anything else is not "the same
            # base" and falls through to the guarded `field.set`.
            return
        field.set(value)


# ---------------------------------------------------------------------------
# Layout Loaders & Pre-loaded Schemas
# ---------------------------------------------------------------------------

def _locate_schema_file(name: str) -> Path:
    # Packaged copy first: it ships inside the installed `eudkit` package
    # (see pyproject package-data), so an installed import works outside the
    # repo tree. The repo-tree and cwd candidates remain as dev fallbacks;
    # the packaged copy is kept byte-identical to the repo canonical files
    # by test_schema_packaging.py.
    candidates = [
        Path(__file__).resolve().parent / "schema" / "layouts" / f"{name}.json",
        Path(__file__).resolve().parent.parent / "schema" / "layouts" / f"{name}.json",
        Path.cwd() / "schema" / "layouts" / f"{name}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Schema file not found for '{name}'")


def loadLayout(name: str) -> MemView:
    p = _locate_schema_file(name)
    data = json.loads(p.read_text(encoding="utf-8"))
    fields = {}
    for k, v in data["fields"].items():
        fields[k] = FieldSpec(
            offset=v["offset"],
            size=v["size"],
            mask=v.get("mask"),
            pointer=v.get("pointer"),
            composite=v.get("composite"),
            aliases=tuple(v.get("aliases", ())),
        )
    return defineMemView(data.get("struct", name), data["size"], fields)


CUnitSchema = loadLayout("cunit")
CSpriteSchema = loadLayout("csprite")


class _MemViewFactory:
    def __init__(self, schema: MemView) -> None:
        self.schema = schema

    def __call__(self, epd: Any) -> MemViewInstance:
        return MemViewInstance(self.schema, epd)


CUnit = _MemViewFactory(CUnitSchema)
CSprite = _MemViewFactory(CSpriteSchema)


def make_memview(schema: MemView, epd: Any) -> MemViewInstance:
    return MemViewInstance(schema, epd)


def MemCond(
    epd_addr: Any,
    comparison: int | str,
    amount: Any,
    size_or_mask: int = 4,
) -> Any:
    """Single-condition in-place memory comparison against an `EPD` (1C / 1T)."""
    from .hir import AtLeast, AtMost, Deaths, DeathsX, Exactly
    # The two DSL parameters are guarded *here* so the error names the operand
    # the author wrote (`MemCond comparison`), not the record field it would
    # have reached (`condition field comparison`). Only the numeric branch of
    # `comparison` is guarded: a string is a comparison *name*, and an unknown
    # name stays `EUDSL-CMP` (ADR 0019 §6, cycle 16).
    cmp_val: int | str = comparison
    if isinstance(cmp_val, str):
        c = cmp_val.lower()
        if c in ("exactly", "==", "="):
            cmp_val = Exactly
        elif c in ("atleast", ">="):
            cmp_val = AtLeast
        elif c in ("atmost", "<="):
            cmp_val = AtMost
        else:
            raise EudslError("EUDSL-CMP", f"Unknown comparison: {comparison}")
    else:
        cmp_val = _require_plain_int(cmp_val, "MemCond comparison")

    size_or_mask = _require_plain_int(size_or_mask, "MemCond size_or_mask")

    if size_or_mask in (4, 0xFFFFFFFF):
        return Deaths(epd_addr, cmp_val, amount, 0)
    if size_or_mask == 1:
        return DeathsX(epd_addr, cmp_val, amount, 0, 0xFF)
    if size_or_mask == 2:
        return DeathsX(epd_addr, cmp_val, amount, 0, 0xFFFF)
    return DeathsX(epd_addr, cmp_val, amount, 0, size_or_mask & 0xFFFFFFFF)


readEpd = read_epd
readEpdPointer = read_epd_pointer
readCunitPointer = read_cunit_pointer
readCunitPointerSaved = read_cunit_pointer_saved
inCunitPool = in_cunit_pool
