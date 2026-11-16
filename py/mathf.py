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
import weakref
from typing import Any, Callable

from .cp import _set_cp_from_var, with_epd
from .hir import Add, AtLeast, Db, DeathsX, SetDeathsX, epd_of_db
from .temporal import when
from .trigger import Trigger, _get_active_module_builder
from .var import (
    Expr,
    ReadonlyVar,
    Var,
    _saturating_sub_into,
    divmod as _unsigned_divmod,
    to_var,
)
from .view import CURRENT_PLAYER, EudslError, _require_plain_int, read_epd

#: Full operand domain for `isqrt` and the signed-math helpers.
MathOperand = int | Var | ReadonlyVar | Expr


def _plain_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    """Validate a width: the shared operand domain (ADR 0019) then range."""
    value = _require_plain_int(value, f"isqrt(): {name}")
    if value < minimum or value > maximum:
        raise EudslError(
            "EUDSL-RANGE",
            f"isqrt(): {name} must be in [{minimum}, {maximum}], got {value}",
        )
    return value


def isqrt(value: MathOperand, value_bits: int = 32, fixed_bits: int = 0) -> Var:
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

    source = _math_operand("isqrt", "value", value)
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


# ---------------------------------------------------------------------------
# Signed division and modulo
# ---------------------------------------------------------------------------

_DWORD = 0xFFFFFFFF


def _signed(value: int) -> int:
    """Interpret a build-time integer as a signed 32-bit dword."""
    value &= _DWORD
    return value - 0x100000000 if value & 0x80000000 else value


def _math_operand(name: str, operand: str, value: Any) -> Var:
    """Materialize one `MathOperand`, naming the function and operand."""
    if isinstance(value, bool):
        raise EudslError(
            "EUDSL-TYPE",
            f"{name}(): {operand} must be a Var, ReadonlyVar, Expr, or "
            f"integer constant, got {value!r}",
        )
    try:
        return to_var(value)
    except EudslError as exc:
        raise EudslError(
            "EUDSL-TYPE",
            f"{name}(): {operand} must be a Var, ReadonlyVar, Expr, or "
            f"integer constant, got {value!r}",
        ) from exc


def _check_math_operand(name: str, operand: str, value: Any) -> None:
    """Type-check a `MathOperand` without materializing it."""
    if isinstance(value, bool) or not isinstance(
        value, (int, Var, ReadonlyVar, Expr)
    ):
        raise EudslError(
            "EUDSL-TYPE",
            f"{name}(): {operand} must be a Var, ReadonlyVar, Expr, or "
            f"integer constant, got {value!r}",
        )


def _abs_with_flag(value: Any) -> tuple[Var, Var]:
    """Return ``(|value|, value < 0)`` as two fresh Vars (flag is 0/1)."""
    source = value if isinstance(value, Var) else to_var(value)
    mag = Var(0)
    neg = Var(0)
    mag.set(source)

    def take() -> None:
        neg.set(1)
        mag.set(0)
        mag.isub(source)

    when(source.ge(0x80000000), take)
    return mag, neg


def _negate_self(value: Var, scratch: Var) -> None:
    """Emit ``value = -value`` through ``scratch`` (wrapping subtraction)."""
    scratch.set(0)
    scratch.isub(value)
    value.set(scratch)


def _trunc_quotient(a: int, b: int) -> int:
    """C/JS truncating quotient of two non-zero signed integers."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _fold_signed(a: int, b: int, mode: str, part: str) -> Var:
    """Fold one signed division result at build time (``b != 0``)."""
    if mode == "floor":
        value = a // b if part == "quot" else a % b
    elif mode == "euclid":
        rem = a % abs(b)
        value = (a - rem) // b if part == "quot" else rem
    else:
        quotient = _trunc_quotient(a, b)
        value = quotient if part == "quot" else a - quotient * b
    out = Var(0)
    out.set(value & _DWORD)
    return out


def _signed_divmod(
    a: Any, b: Any, mode: str, part: str, name: str
) -> Var:
    """Shared sign-magnitude fixup over the unsigned restoring ladder."""
    a_const = isinstance(a, int) and not isinstance(a, bool)
    b_const = isinstance(b, int) and not isinstance(b, bool)
    # A constant zero divisor must fail before any emission.
    if b_const and _signed(b) == 0:
        raise EudslError("EUDSL-DIV-ZERO", f"{name}(): division by zero")
    if a_const and b_const:
        return _fold_signed(_signed(a), _signed(b), mode, part)

    ua, na = _abs_with_flag(_math_operand(name, "a", a))
    if b_const:
        sb = _signed(b)
        nb = 1 if sb < 0 else 0
        ub = abs(sb) & _DWORD
        b_value: int | Var = sb & _DWORD
        nonzero: list[Any] = []
        zero_divisor: list[Any] | None = None
    else:
        b_var = _math_operand(name, "b", b)
        ub, nb = _abs_with_flag(b_var)
        b_value = b_var
        nonzero = [ub.ge(1)]
        zero_divisor = [ub.eq(0), na.eq(1)]
    q, r = _unsigned_divmod(ua, ub)

    scratch = Var(0)
    sign_diff = Var(0)
    sign_diff.set(na)
    sign_diff.iadd(nb)

    if mode == "towards_zero":
        when(sign_diff.eq(1), lambda: _negate_self(q, scratch))
        when(na.eq(1), lambda: _negate_self(r, scratch))
        return q if part == "quot" else r

    if mode == "floor":
        # Truncated remainder takes the dividend's sign.
        when(na.eq(1), lambda: _negate_self(r, scratch))
        when(sign_diff.eq(1), lambda: _negate_self(q, scratch))

        def floor_fix() -> None:
            q.isub(1)
            r.iadd(b_value)

        when([*nonzero, sign_diff.eq(1), r.ne(0)], floor_fix)
        return q if part == "quot" else r

    # Euclidean: the remainder is always non-negative. `q` starts truncated;
    # when the truncated remainder was negative it gains |b|, so `q` moves by
    # `-sign(b)` (the ticket's `q -= nb` only holds for a positive divisor).
    when(sign_diff.eq(1), lambda: _negate_self(q, scratch))

    def euclid_fix() -> None:
        if isinstance(b_value, int):
            q.iadd(2 * nb - 1)
        else:
            q.iadd(nb)
            q.iadd(nb)
            q.isub(1)

    when([*nonzero, na.eq(1), r.ne(0)], euclid_fix)
    if zero_divisor is not None:
        when(zero_divisor, lambda: _negate_self(r, scratch))
    return q if part == "quot" else r


def div_towards_zero(a: MathOperand, b: MathOperand) -> Var:
    """Emit C/JS truncating signed division (``-7 / 2 == -3``)."""
    return _signed_divmod(a, b, "towards_zero", "quot", "div_towards_zero")


def mod_towards_zero(a: MathOperand, b: MathOperand) -> Var:
    """Emit C/JS truncating signed remainder (sign follows the dividend)."""
    return _signed_divmod(a, b, "towards_zero", "rem", "mod_towards_zero")


def div_floor(a: MathOperand, b: MathOperand) -> Var:
    """Emit Python ``//`` signed division (the floor quotient)."""
    return _signed_divmod(a, b, "floor", "quot", "div_floor")


def mod_floor(a: MathOperand, b: MathOperand) -> Var:
    """Emit Python ``%`` signed remainder (sign follows the divisor)."""
    return _signed_divmod(a, b, "floor", "rem", "mod_floor")


def div_euclid(a: MathOperand, b: MathOperand) -> Var:
    """Emit Euclidean division (remainder always in ``[0, |b|)``)."""
    return _signed_divmod(a, b, "euclid", "quot", "div_euclid")


def mod_euclid(a: MathOperand, b: MathOperand) -> Var:
    """Emit the non-negative Euclidean remainder."""
    return _signed_divmod(a, b, "euclid", "rem", "mod_euclid")


# ---------------------------------------------------------------------------
# Integer powers
# ---------------------------------------------------------------------------


def pow(base: MathOperand, exp: int, *, bits: int = 32) -> Var:
    """Emit ``(base ** exp) & ((1 << bits) - 1)`` for ``0 <= exp <= 31``.

    ``exp`` is build-time so the exponentiation-by-squaring chain is fully
    unrolled: ``(bit_length - 1) + (popcount - 1)`` ``Var`` multiplications.
    A runtime base is copied into a fresh result, so the operand is never
    mutated. ``exp == 1`` is one value copy and no multiplication.
    """
    exp = _require_plain_int(exp, "pow(): exp")
    if exp < 0 or exp > 31:
        raise EudslError("EUDSL-RANGE", f"pow(): exp must be in [0, 31], got {exp}")
    bits = _require_plain_int(bits, "pow(): bits")
    if bits < 1 or bits > 32:
        raise EudslError("EUDSL-RANGE", f"pow(): bits must be in [1, 32], got {bits}")
    mask = (1 << bits) - 1

    if isinstance(base, int) and not isinstance(base, bool):
        out = Var(0)
        out.set((int(base) ** exp) & mask)
        return out

    _check_math_operand("pow", "base", base)
    if exp == 0:
        out = Var(0)
        out.set(1 & mask)
        return out

    source = to_var(base)
    out = Var(0)
    out.set(source)
    for bit in range(exp.bit_length() - 2, -1, -1):
        out.imul(out)
        if (exp >> bit) & 1:
            out.imul(source)
    if bits < 32:
        out.iand(mask)
    return out


# ---------------------------------------------------------------------------
# Table-driven trigonometry
# ---------------------------------------------------------------------------

TRIG_TABLE_Q: int = 16
TRIG_TABLE_SCALE: int = 1 << TRIG_TABLE_Q


def _q16(value: float) -> int:
    """Round a fraction-of-a-turn value to unsigned Q16."""
    return int(_py_math.floor(value * TRIG_TABLE_SCALE + 0.5))


#: ``round(atan(t / 256) / (2*pi) * 65536)`` for ``t = 0..256`` (Q16 turns).
_ATAN256_Q16: tuple[int, ...] = tuple(
    _q16(_py_math.atan(t / 256.0) / (2.0 * _py_math.pi)) for t in range(257)
)
#: ``floor(cos(pi/180 * i) * 65536 + 0.5)`` for ``i = 0..90``.
_COS_DEG_Q16: tuple[int, ...] = tuple(
    _q16(_py_math.cos(_py_math.pi / 180.0 * i)) for i in range(91)
)
#: ``floor(sin(pi/180 * i) * 65536 + 0.5)`` for ``i = 0..90``.
_SIN_DEG_Q16: tuple[int, ...] = tuple(
    _q16(_py_math.sin(_py_math.pi / 180.0 * i)) for i in range(91)
)
#: ``floor(cos(2*pi * i / 256) * 65536 + 0.5)`` for ``i = 0..64``.
_COS_256_Q16: tuple[int, ...] = tuple(
    _q16(_py_math.cos(2.0 * _py_math.pi * i / 256.0)) for i in range(65)
)
#: ``floor(sin(2*pi * i / 256) * 65536 + 0.5)`` for ``i = 0..64``.
_SIN_256_Q16: tuple[int, ...] = tuple(
    _q16(_py_math.sin(2.0 * _py_math.pi * i / 256.0)) for i in range(65)
)


def _payload(values: tuple[int, ...]) -> bytes:
    """Little-endian dword payload for a constant table."""
    return b"".join((value & _DWORD).to_bytes(4, "little") for value in values)


class _TrigTables:
    """One build's lazily materialized trig payloads and runtime base EPDs."""

    def __init__(self) -> None:
        self._epds: dict[str, Var] = {}

    def _table_epd(self, name: str, values: tuple[int, ...]) -> Var:
        epd = self._epds.get(name)
        if epd is None:
            epd = epd_of_db(Db(_payload(values)))
            self._epds[name] = epd
        return epd

    @property
    def atan_epd(self) -> Var:
        return self._table_epd("atan", _ATAN256_Q16)

    @property
    def cos_epd(self) -> Var:
        return self._table_epd("cos", _COS_DEG_Q16)

    @property
    def sin_epd(self) -> Var:
        return self._table_epd("sin", _SIN_DEG_Q16)

    @property
    def cos256_epd(self) -> Var:
        return self._table_epd("cos256", _COS_256_Q16)

    @property
    def sin256_epd(self) -> Var:
        return self._table_epd("sin256", _SIN_256_Q16)


_TABLE_CACHE: "weakref.WeakKeyDictionary[Any, _TrigTables]" = weakref.WeakKeyDictionary()


def _tables() -> _TrigTables:
    """Return (and lazily build) the active builder's shared trig tables."""
    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE", "mathf trig tables must be built inside buildModule()"
        )
    tables = _TABLE_CACHE.get(builder)
    if tables is None:
        tables = _TrigTables()
        _TABLE_CACHE[builder] = tables
    return tables


def _read_table(base_epd: Var, index: Var) -> Var:
    """Read ``base_epd[index]`` through a temporary CurrentPlayer binding."""
    cursor = Var(0)
    cursor.set(base_epd)
    cursor.iadd(index)
    return with_epd(cursor, lambda cp: read_epd(CURRENT_PLAYER, 32, 0))


def _read_table_pair(
    first_epd: Var, second_epd: Var, index: Var
) -> tuple[Var, Var]:
    """Read two table rows inside one ambient CurrentPlayer save/restore."""
    first_cursor = Var(0)
    first_cursor.set(first_epd)
    first_cursor.iadd(index)
    second_cursor = Var(0)
    second_cursor.set(second_epd)
    second_cursor.iadd(index)

    def body(_cp: Any) -> tuple[Var, Var]:
        first = read_epd(CURRENT_PLAYER, 32, 0)
        _set_cp_from_var(second_cursor)
        second = read_epd(CURRENT_PLAYER, 32, 0)
        return first, second

    return with_epd(first_cursor, body)


def _atan256_core(y: Any, x: Any, name: str) -> Var:
    """Shared octant reduction returning a 1/256-turn angle (0..255)."""
    yv = _math_operand(name, "y", y)
    xv = _math_operand(name, "x", x)
    ax, nx = _abs_with_flag(xv)
    ay, ny = _abs_with_flag(yv)

    swap = Var(0)
    when(ay.gt(ax), lambda: swap.set(1))

    maximum = Var(0)
    minimum = Var(0)
    maximum.set(ax)
    minimum.set(ay)

    def choose_swap() -> None:
        maximum.set(ay)
        minimum.set(ax)

    when(swap.eq(1), choose_swap)

    scaled = Var(0)
    scaled.set(minimum)
    scaled.ishl(8)
    index, _ = _unsigned_divmod(scaled, maximum)
    raw = _read_table(_tables().atan_epd, index)
    octant = Var(0)
    octant.set(raw)
    octant.ishr(8)

    quadrant = Var(0)
    quadrant.set(nx)
    quadrant.iadd(nx)
    quadrant.iadd(ny)
    half = Var(0)
    half.set(quadrant)
    half.ishr(1)
    odd = Var(0)
    odd.set(quadrant)
    odd.iand(1)
    flip = Var(0)
    flip.set(half)
    flip.ixor(odd)

    signed_octant = Var(0)
    signed_octant.set(octant)
    when(flip.eq(1), lambda: _negate_self(signed_octant, Var(0)))

    # Non-swap: off = 128*half, angle = off + signed_octant.
    # Swap:     off = 64 + 128*odd, angle = off - signed_octant.
    normal = Var(0)
    normal.set(half)
    normal.ishl(7)
    normal.iadd(signed_octant)
    swapped = Var(0)
    swapped.set(odd)
    swapped.ishl(7)
    swapped.iadd(64)
    swapped.isub(signed_octant)

    angle = Var(0)
    angle.set(normal)
    when(swap.eq(1), lambda: angle.set(swapped))
    angle.iand(0xFF)
    return angle


def atan2_256(y: MathOperand, x: MathOperand) -> Var:
    """Emit ``atan2(y, x)`` in 1/256-turn units (0..255)."""
    return _atan256_core(y, x, "atan2_256")


def atan2(y: MathOperand, x: MathOperand) -> Var:
    """Emit ``atan2(y, x)`` in degrees (0..359), from the shared turn table."""
    angle = _atan256_core(y, x, "atan2")
    degrees = Var(0)
    degrees.set(angle)
    degrees.imul(360)
    degrees.ishr(8)
    return degrees


def _lengthdir(
    length: Any,
    angle: Any,
    period: int,
    quadrant_size: int,
    *,
    turn256: bool = False,
    name: str,
) -> tuple[Var, Var]:
    """Shared degree/256-turn length-and-direction kernel."""
    angle_var = _math_operand(name, "angle", angle)
    _, normalised = _unsigned_divmod(angle_var, period)
    quadrant, remainder = _unsigned_divmod(normalised, quadrant_size)

    tables = _tables()
    if turn256:
        cos_epd, sin_epd = tables.cos256_epd, tables.sin256_epd
    else:
        cos_epd, sin_epd = tables.cos_epd, tables.sin_epd
    cosine, sine = _read_table_pair(cos_epd, sin_epd, remainder)

    odd = Var(0)
    odd.set(quadrant)
    odd.iand(1)
    cos_mag = Var(0)
    cos_mag.set(cosine)
    sin_mag = Var(0)
    sin_mag.set(sine)

    def swap_magnitudes() -> None:
        cos_mag.set(sine)
        sin_mag.set(cosine)

    when(odd.eq(1), swap_magnitudes)

    length_const = isinstance(length, int) and not isinstance(length, bool)
    scratch = Var(0)
    if length_const:
        signed_length = _signed(length)
        length_mag = abs(signed_length)
        length_neg = 1 if signed_length < 0 else 0
        dx = Var(0)
        dx.set(cos_mag)
        dx.imul(length_mag)
        dy = Var(0)
        dy.set(sin_mag)
        dy.imul(length_mag)
    else:
        length_var = _math_operand(name, "length", length)
        length_mag_var, length_neg_var = _abs_with_flag(length_var)
        length_neg = length_neg_var
        dx = Var(0)
        dx.set(cos_mag)
        dx.imul(length_mag_var)
        dy = Var(0)
        dy.set(sin_mag)
        dy.imul(length_mag_var)

    dx.ishr(16)
    dy.ishr(16)

    if isinstance(length_neg, int):
        if length_neg:
            _negate_self(dx, scratch)
            _negate_self(dy, scratch)
    else:

        def apply_length_sign() -> None:
            _negate_self(dx, scratch)
            _negate_self(dy, scratch)

        when(length_neg.eq(1), apply_length_sign)

    half = Var(0)
    half.set(quadrant)
    half.ishr(1)
    combined = Var(0)
    combined.set(half)
    combined.iadd(odd)
    when(combined.eq(1), lambda: _negate_self(dx, scratch))
    when(half.eq(1), lambda: _negate_self(dy, scratch))
    return dx, dy


def lengthdir(length: MathOperand, angle: MathOperand) -> tuple[Var, Var]:
    """Return ``(dx, dy)`` for ``length`` at ``angle`` degrees.

    ``length`` is read as a signed 32-bit value and must satisfy
    ``|length| <= 65535`` so the Q16 product stays below ``2**32``. ``angle``
    is an unsigned degree value normalised modulo 360.
    """
    return _lengthdir(length, angle, 360, 90, name="lengthdir")


def lengthdir_256(length: MathOperand, angle: MathOperand) -> tuple[Var, Var]:
    """Like :func:`lengthdir` with a 256-unit turn and Q16 quarter table."""
    return _lengthdir(length, angle, 256, 64, turn256=True, name="lengthdir_256")


# ---------------------------------------------------------------------------
# Binary classification search
# ---------------------------------------------------------------------------


def _binary_search(
    lo: int,
    hi: int,
    predicate: Callable[[Var], Any],
    *,
    last: bool,
    name: str,
) -> Var:
    """Shared ``ceil(log2(span))`` halving search over a runtime predicate."""
    lo = _require_plain_int(lo, f"{name}(): lo")
    hi = _require_plain_int(hi, f"{name}(): hi")
    if lo < 0 or hi < 0 or lo > hi or hi >= _DWORD:
        raise EudslError(
            "EUDSL-RANGE",
            f"{name}(): lo/hi must satisfy 0 <= lo <= hi < 0xFFFFFFFF, "
            f"got lo={lo}, hi={hi}",
        )
    if not callable(predicate):
        raise EudslError("EUDSL-TYPE", f"{name}(): predicate must be callable")

    span = hi - lo
    steps = (span + 1).bit_length()
    left = Var(0)
    right = Var(0)
    left.set(0)
    right.set(span)

    for _ in range(steps):
        mid = Var(0)
        width = Var(0)
        mid.set(left)
        width.set(right)
        width.isub(left)
        width.ishr(1)
        mid.iadd(width)
        if last:
            probe = Var(0)
            probe.set(hi)
            probe.isub(mid)
            condition = predicate(probe)
        else:
            probe = Var(0)
            probe.set(lo)
            probe.iadd(mid)
            condition = predicate(probe)

        def accept(mid: Var = mid) -> None:
            right.set(mid)

        def reject(mid: Var = mid) -> None:
            left.set(mid)
            left.iadd(1)

        when(condition, accept, reject)

    out = Var(0)
    if last:
        out.set(hi)
        out.isub(left)
    else:
        out.set(lo)
        out.iadd(left)
    return out


def binary_min(lo: int, hi: int, predicate: Callable[[Var], Any]) -> Var:
    """First ``x`` in ``[lo, hi]`` with ``predicate(x)``, else ``hi + 1``.

    The range is assumed to be ``False … False True … True``. The predicate is
    evaluated at its emission site, so it must be condition-only (an action
    would run unconditionally).
    """
    return _binary_search(lo, hi, predicate, last=False, name="binary_min")


def binary_max(lo: int, hi: int, predicate: Callable[[Var], Any]) -> Var:
    """Last ``x`` in ``[lo, hi]`` with ``predicate(x)``, else ``lo - 1``.

    The range is assumed to be ``True … True False … False``. The predicate is
    evaluated at its emission site, so it must be condition-only (an action
    would run unconditionally).
    """
    return _binary_search(lo, hi, predicate, last=True, name="binary_max")


# camelCase aliases (same objects)
divTowardsZero = div_towards_zero
divFloor = div_floor
divEuclid = div_euclid
modTowardsZero = mod_towards_zero
modFloor = mod_floor
modEuclid = mod_euclid
pow_int = pow
powInt = pow
atan2Fixed8 = atan2_256
lengthdirFixed8 = lengthdir_256
binaryMin = binary_min
binaryMax = binary_max


__all__ = [
    "isqrt", "MathOperand", "TRIG_TABLE_Q", "TRIG_TABLE_SCALE",
    "div_towards_zero", "divTowardsZero", "div_floor", "divFloor",
    "div_euclid", "divEuclid", "mod_towards_zero", "modTowardsZero",
    "mod_floor", "modFloor", "mod_euclid", "modEuclid",
    "pow", "pow_int", "powInt",
    "atan2", "atan2_256", "atan2Fixed8",
    "lengthdir", "lengthdir_256", "lengthdirFixed8",
    "binary_min", "binaryMin", "binary_max", "binaryMax",
]
