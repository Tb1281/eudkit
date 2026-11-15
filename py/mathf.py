"""Runtime integer math helpers.

`isqrt` is a fixed-point integer square root: a digit-by-digit (restoring)
square root of ``value`` scaled by ``2**fixed_bits``. Each of the
``ceil(value_bits / 2) + fixed_bits`` steps shifts two bits into the
remainder, doubles the running root, and subtracts ``2 * root + 1`` when it
fits. The runtime kernel carries ``u = 2 * root`` beside ``root`` so the
trial ``u + 1`` comes from one doubling instead of a rebuild from ``root``
each step. The cost is therefore fixed by the declared bit widths instead of
the runtime value, unlike an unrolled binary-search tree over every possible
result.
"""

from __future__ import annotations

import math as _py_math
from typing import Any

from .hir import Add, AtLeast, DeathsX, SetDeathsX
from .temporal import when
from .trigger import Trigger
from .var import Expr, ReadonlyVar, Var, _saturating_sub_into, to_var
from .view import EudslError, _require_plain_int

#: Full operand domain for `isqrt`'s `value`: the same set `to_var` accepts.
IsqrtOperand = int | Var | ReadonlyVar | Expr


def _plain_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    """Validate a width: the shared operand domain (ADR 0019) then range."""
    value = _require_plain_int(value, f"isqrt(): {name}")
    if value < minimum or value > maximum:
        raise EudslError(
            "EUDSL-RANGE",
            f"isqrt(): {name} must be in [{minimum}, {maximum}], got {value}",
        )
    return value


def _as_var(value: Any) -> Var:
    """Materialize the full operand domain through the canonical `to_var`.

    `bool` is checked explicitly because `to_var` would reject it with the
    generic "Unsupported Var operand" text, and `isqrt` owns its diagnostic.
    """
    if isinstance(value, bool):
        raise EudslError(
            "EUDSL-TYPE",
            "isqrt() value must be a Var, ReadonlyVar, Expr, or integer "
            f"constant, got {value!r}",
        )
    try:
        return to_var(value)
    except EudslError as exc:
        raise EudslError(
            "EUDSL-TYPE",
            "isqrt() value must be a Var, ReadonlyVar, Expr, or integer "
            f"constant, got {value!r}",
        ) from exc


def isqrt(value: IsqrtOperand, value_bits: int = 32, fixed_bits: int = 0) -> Var:
    """Emit ``floor(sqrt(value) * 2**fixed_bits)`` for a runtime value.

    ``value`` must satisfy ``0 <= value < 2**value_bits`` at game time; the
    caller owns that bound because a dword has no sign and no in-band range
    marker. ``fixed_bits`` adds fractional result bits (``8`` gives a
    ``Q8`` square root). The result must fit in 32 bits:
    ``ceil(value_bits / 2) + fixed_bits <= 32``.

    A constant ``value`` folds to a single constant ``Var`` at build time but
    still returns a `Var`, so the result composes with runtime arithmetic.
    ``value`` is the full operand domain — ``int | Var | ReadonlyVar | Expr``
    (ADR 0019): a `ReadonlyVar` forwards its underlying var without a copy and
    an `Expr` is materialized through the canonical `to_var`, exactly as a
    hand-written temp would be. Static cost is one trigger per compared input
    bit plus five per step (``2 * value_bits + 5 * steps``); executed cost is
    proportional to ``steps`` because untaken subtract branches are skipped.

    The remainder and root accumulators are cleared on every call: the kernel
    is emitted inline, so it can sit in a per-loop body where a build-time
    zero init only holds for the first pass.
    """
    value_bits = _plain_int("value_bits", value_bits, 1, 32)
    fixed_bits = _plain_int("fixed_bits", fixed_bits, 0, 16)
    steps = (value_bits + 1) // 2 + fixed_bits
    if steps > 32:
        raise EudslError(
            "EUDSL-RANGE",
            "isqrt(): ceil(value_bits / 2) + fixed_bits must be at most 32, "
            f"got {steps}",
        )

    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value >= (1 << value_bits):
            raise EudslError(
                "EUDSL-RANGE",
                f"isqrt(): constant value {value} is outside [0, 2**{value_bits})",
            )
        root = _py_math.isqrt(value << (2 * fixed_bits))
        if root >= (1 << 32):
            raise EudslError(
                "EUDSL-RANGE", f"isqrt({value}) = {root} does not fit in 32 bits"
            )
        out = Var(0)
        out.set(root)
        return out

    source = _as_var(value)
    rem = Var(0)
    root = Var(0)
    u = Var(0)
    diff = Var(0)
    rem.set(0)
    root.set(0)
    u.set(0)
    for step in range(steps - 1, -1, -1):
        rem.iadd(rem)
        rem.iadd(rem)
        # Two bits of `value` shift in as the integer part; below that the
        # shift-in bits are the fractional zeros of ``value << 2*fixed_bits``.
        hi_bit = 2 * step + 1 - 2 * fixed_bits
        lo_bit = 2 * step - 2 * fixed_bits
        if 0 <= hi_bit < value_bits:
            Trigger(
                conds=[DeathsX(source.value, AtLeast, 1, 0, (1 << hi_bit) & 0xFFFFFFFF)],
                acts=[SetDeathsX(rem.value, Add, 2, 0, 0xFFFFFFFF)],
            )
        if 0 <= lo_bit < value_bits:
            Trigger(
                conds=[DeathsX(source.value, AtLeast, 1, 0, (1 << lo_bit) & 0xFFFFFFFF)],
                acts=[SetDeathsX(rem.value, Add, 1, 0, 0xFFFFFFFF)],
            )
        # Carry `u = 2*root` beside `root` so each step only doubles both
        # accumulators instead of rebuilding `trial = 2*root + 1` from `root`.
        root.iadd(root)
        u.iadd(u)
        diff.set(u)
        diff.iadd(1)  # trial = u + 1
        _saturating_sub_into(diff, rem)  # 0 exactly when rem >= trial

        def take(rem: Var = rem, root: Var = root, u: Var = u) -> None:
            # The gate means ``rem >= u + 1``, so wrapping subtraction cannot
            # underflow here (unlike the saturating comparison above).
            rem.isub(u)
            rem.isub(1)
            root.iadd(1)
            u.iadd(2)

        when(diff.eq(0), take)
    return root


__all__ = ["isqrt"]
