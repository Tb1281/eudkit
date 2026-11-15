"""Scalar `Var` and arithmetic/bitwise `Expr` backed by `64A` `TriggerPage` action slots
enforcing the self-targeted action invariant (`act[k].dest == EPD(act[k].value)` at rest).
"""

from __future__ import annotations

from typing import Any

from .hir import (
    Add,
    AtLeast,
    Deaths,
    DeathsX,
    Exactly,
    SetDeathsX,
    SetTo,
    Subtract,
    TriggerFieldReloc,
    emitVarStore,
)
from .lazy_binding import LazyBinding
from .trigger import (
    ACT_COUNT,
    Trigger,
    _get_active_module_builder,
    _register_trigger,
)
from .view import (
    EudslError,
    FieldValueSpace,
    TriggerFieldRef,
    _apply_comparison,
    _build_time_truthiness_error,
    _describe_value,
    _is_plain_int,
    _require_plain_int,
    warn_lazy_in_builder,
)


def _alloc_var_slot(
    init: int, mask: int = 0xFFFFFFFF, storage: str | None = None
) -> int:
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "alloc_var"):
        return int(builder.alloc_var(init & 0xFFFFFFFF, mask & 0xFFFFFFFF, storage))
    # No fallback: the builder owns the id space, so a Var created outside
    # buildModule has no id to receive. A silent fallback id would collide
    # with the first in-module var (see the aligned-string-padding bug).
    raise EudslError("EUDSL-SCOPE", "Var() must be created inside buildModule()")


def _get_next_var_slot() -> int:
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "get_next_var_id"):
        return int(builder.get_next_var_id())
    raise EudslError("EUDSL-SCOPE", "Var ids are only available inside buildModule()")


def _alloc_string(text: str) -> int:
    """Reserve a module string table entry and return its 0-based index."""
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "alloc_string"):
        return int(builder.alloc_string(text))
    raise EudslError(
        "EUDSL-SCOPE", "FixedString buffers must be allocated inside buildModule()"
    )


def _get_next_string_index() -> int:
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "get_next_string_index"):
        return int(builder.get_next_string_index())
    return 0


def _get_scope_serial() -> int:
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "get_scope_serial"):
        return int(builder.get_scope_serial())
    return 0


def _alloc_string_var(index: int, offset: int = 0, mask: int = 0xFFFFFFFF) -> int:
    """Allocate a page-pinned scalar initialized to a module string address."""
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "alloc_string_var"):
        return int(builder.alloc_string_var(index, offset, mask))
    raise EudslError(
        "EUDSL-SCOPE", "FixedString buffers must be allocated inside buildModule()"
    )


def _get_next_page_slot() -> int:
    """Next free ``TriggerPage`` action slot.

    Scalars demoted to dword/bit cells do not occupy a page slot, so page
    alignment must count page-pinned variables rather than raw variable ids.
    """
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "get_next_page_slot"):
        return int(builder.get_next_page_slot())
    raise EudslError("EUDSL-SCOPE", "Page slots are only available inside buildModule()")


def align_page_boundary() -> int:
    """Pad with page-pinned variables until the next page slot is page-aligned.

    Alignment must count page slots, not raw variable ids: non-page
    variables consume ids but no page slots, so padding on the raw counter
    would place page-aligned containers at the wrong pinned rank. Returns
    the number of pads added (zero when already aligned).
    """
    rem = _get_next_page_slot() % ACT_COUNT
    if rem == 0:
        return 0
    pads = ACT_COUNT - rem
    for _ in range(pads):
        _alloc_var_slot(0, 0xFFFFFFFF, "page")
    return pads


def ensure_page_fit(field_count: int) -> int:
    """Pad to the next page boundary only when ``field_count`` fields would
    not fit in the current page. When the fields fit, nothing is padded, so
    packing is preserved. Returns the number of pads added.
    """
    rem = _get_next_page_slot() % ACT_COUNT
    if rem != 0 and rem + field_count > ACT_COUNT:
        return align_page_boundary()
    return 0


def _emit_var_transfer(dst_var: int, src_var: int, modifier: int) -> None:
    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "queue_var_transfer"):
        builder.queue_var_transfer(dst_var, src_var, modifier & 0xFF)


class Expr:
    """Lazy arithmetic or bitwise expression over `Var` and constant operands."""

    def __init__(self, op: str, lhs: Any, rhs: Any = None) -> None:
        self.op = op
        self.lhs = lhs
        self.rhs = rhs

    def add(self, rhs: Any) -> "Expr":
        return Expr("add", self, rhs)

    def sub(self, rhs: Any) -> "Expr":
        return Expr("sub", self, rhs)

    def mul(self, rhs: Any) -> "Expr":
        return Expr("mul", self, rhs)

    def div(self, rhs: Any) -> "Expr":
        return Expr("div", self, rhs)

    def mod(self, rhs: Any) -> "Expr":
        return Expr("mod", self, rhs)

    def bitAnd(self, rhs: Any) -> "Expr":
        return Expr("bitand", self, rhs)

    def bit_and(self, rhs: Any) -> "Expr":
        return self.bitAnd(rhs)

    def band(self, rhs: Any) -> "Expr":
        return self.bitAnd(rhs)

    def bitOr(self, rhs: Any) -> "Expr":
        return Expr("bitor", self, rhs)

    def bit_or(self, rhs: Any) -> "Expr":
        return self.bitOr(rhs)

    def bor(self, rhs: Any) -> "Expr":
        return self.bitOr(rhs)

    def bitXor(self, rhs: Any) -> "Expr":
        return Expr("bitxor", self, rhs)

    def bit_xor(self, rhs: Any) -> "Expr":
        return self.bitXor(rhs)

    def xor(self, rhs: Any) -> "Expr":
        return self.bitXor(rhs)

    def bxor(self, rhs: Any) -> "Expr":
        return self.bitXor(rhs)

    def bitNot(self) -> "Expr":
        return Expr("bitnot", self)

    def bit_not(self) -> "Expr":
        return self.bitNot()

    def invert(self) -> "Expr":
        return self.bitNot()

    def bnot(self) -> "Expr":
        return self.bitNot()

    def shl(self, rhs: Any) -> "Expr":
        return Expr("shl", self, rhs)

    def lshift(self, rhs: Any) -> "Expr":
        return self.shl(rhs)

    def shr(self, rhs: Any) -> "Expr":
        return Expr("shr", self, rhs)

    def rshift(self, rhs: Any) -> "Expr":
        return self.shr(rhs)

    def neg(self) -> "Expr":
        return Expr("neg", self)

    # Python arithmetic/bitwise operator overloads
    def __add__(self, rhs: Any) -> "Expr":
        return self.add(rhs)

    def __radd__(self, lhs: Any) -> "Expr":
        return Expr("add", lhs, self)

    def __sub__(self, rhs: Any) -> "Expr":
        return self.sub(rhs)

    def __rsub__(self, lhs: Any) -> "Expr":
        return Expr("sub", lhs, self)

    def __mul__(self, rhs: Any) -> "Expr":
        return self.mul(rhs)

    def __rmul__(self, lhs: Any) -> "Expr":
        return Expr("mul", lhs, self)

    def __floordiv__(self, rhs: Any) -> "Expr":
        return self.div(rhs)

    def __rfloordiv__(self, lhs: Any) -> "Expr":
        return Expr("div", lhs, self)

    def __mod__(self, rhs: Any) -> "Expr":
        return self.mod(rhs)

    def __rmod__(self, lhs: Any) -> "Expr":
        return Expr("mod", lhs, self)

    def __and__(self, rhs: Any) -> "Expr":
        return self.bitAnd(rhs)

    def __rand__(self, lhs: Any) -> "Expr":
        return Expr("bitand", lhs, self)

    def __or__(self, rhs: Any) -> "Expr":
        return self.bitOr(rhs)

    def __ror__(self, lhs: Any) -> "Expr":
        return Expr("bitor", lhs, self)

    def __xor__(self, rhs: Any) -> "Expr":
        return self.bitXor(rhs)

    def __rxor__(self, lhs: Any) -> "Expr":
        return Expr("bitxor", lhs, self)

    def __invert__(self) -> "Expr":
        return self.bitNot()

    def __neg__(self) -> "Expr":
        return self.neg()

    def __lshift__(self, rhs: Any) -> "Expr":
        return self.shl(rhs)

    def __rlshift__(self, lhs: Any) -> "Expr":
        return Expr("shl", lhs, self)

    def __rshift__(self, rhs: Any) -> "Expr":
        return self.shr(rhs)

    def __rrshift__(self, lhs: Any) -> "Expr":
        return Expr("shr", lhs, self)

    # Comparisons
    def eq(self, rhs: Any) -> Any:
        return to_var(self).eq(rhs)

    def ne(self, rhs: Any) -> Any:
        return to_var(self).ne(rhs)

    def lt(self, rhs: Any) -> Any:
        return to_var(self).lt(rhs)

    def le(self, rhs: Any) -> Any:
        return to_var(self).le(rhs)

    def gt(self, rhs: Any) -> Any:
        return to_var(self).gt(rhs)

    def ge(self, rhs: Any) -> Any:
        return to_var(self).ge(rhs)

    def __bool__(self) -> bool:
        """Refuse build-time branching: an expression is a runtime value."""
        raise _build_time_truthiness_error("value")

    def __eq__(self, rhs: Any) -> Any:  # type: ignore[override]
        if isinstance(rhs, (bool, float, str)):
            return self.eq(rhs)
        if not (
            _is_plain_int(rhs) or isinstance(rhs, (Var, Expr, ReadonlyVar))
        ):
            return NotImplemented
        return self.eq(rhs)

    def __ne__(self, rhs: Any) -> Any:  # type: ignore[override]
        if isinstance(rhs, (bool, float, str)):
            return self.ne(rhs)
        if not (
            _is_plain_int(rhs) or isinstance(rhs, (Var, Expr, ReadonlyVar))
        ):
            return NotImplemented
        return self.ne(rhs)

    def __lt__(self, rhs: Any) -> Any:
        return self.lt(rhs)

    def __le__(self, rhs: Any) -> Any:
        return self.le(rhs)

    def __gt__(self, rhs: Any) -> Any:
        return self.gt(rhs)

    def __ge__(self, rhs: Any) -> Any:
        return self.ge(rhs)

    def __hash__(self) -> int:
        return id(self)


def to_operand(op: Any) -> int | "Var":
    """Canonical operand materializer: a constant or handle becomes `int | Var`.

    Plain integers stay constants (masked to 32 bits); every runtime handle
    becomes the `Var` that carries it. This is the single "operand -> value"
    kernel shared by `var`, `string`, and `blockio`; each caller narrows the
    accepted *domain* at its own edge before delegating (see :func:`to_var`).
    """
    if _is_plain_int(op):
        return int(op) & 0xFFFFFFFF
    return to_var(op)


def to_var(op: Any) -> "Var":
    """Canonical operand materializer: a constant or handle becomes a `Var`.

    Accepted operands are a plain integer, `Var`, `ReadonlyVar`, or `Expr` —
    the full operand domain. Callers with a narrower domain (`blockio` takes
    ``Var | Expr``; `string` takes ``Var | Expr | ReadonlyVar``) keep their
    domain check at the edge and then delegate here, so the materialization
    step has exactly one implementation per frontend.
    """
    if isinstance(op, ReadonlyVar):
        return op._var
    if isinstance(op, Var):
        return op
    if _is_plain_int(op):
        return Var(int(op) & 0xFFFFFFFF)
    if isinstance(op, Expr):
        tmp = Var(0)
        _eval_expr_into(tmp, op)
        return tmp
    raise EudslError(
        "EUDSL-TYPE",
        f"Unsupported Var operand: {_describe_value(op)}",
    )


#: Multiplier bits per early-exit chunk in :func:`mul_shift_add`.
#:
#: The kernel gates each following chunk on the multiplier's remaining high
#: bits, so a sparse multiplier skips whole chunks while a dense one pays one
#: extra gate. 16 halves sparse work (``0x5``: E 137/32/2,356 ->
#: 77/17/1,351) while bounding the dense regression at +2T/+1C/+2A. Tune here
#: if a profile ever favours a different balance; the kernel does not hard-code
#: two copies.
MUL_SHIFT_ADD_CHUNK = 16


def mul_shift_add(a: "Var", b: "Var", bits: int = 32) -> "Var":
    """``out = a * b`` by doubling ``a`` and adding it on each set bit of ``b``.

    Shared by :class:`Var`'s Var×Var ``mul`` path and the camera helper (which
    imports it as ``_mul_shift_add`` and keeps its call sites unchanged):
    ``bits`` condition triggers plus one conditional copy per bit, instead of
    the multiplicative bit-pair kernel's ``bits * bits`` triggers. The gates
    are emitted in chunks of :data:`MUL_SHIFT_ADD_CHUNK`; after each chunk but
    the last, the remaining gates are nested under one condition on
    ``b & ~((1 << end) - 1)``, so a sparse multiplier stops early.

    The accumulator is cleared on every call: the kernel is emitted inside a
    per-loop body, so a build-time zero init would let pass 2 accumulate on
    pass 1. Callers must copy the result back (``dst.set(out)``) rather than
    pass ``dst`` in, because ``b`` is read by every gate and ``a`` through
    ``cur``: an in-place accumulator would corrupt a later bit test.

    @internal Seam helper shared with `camera`; not part of the public DSL.
    """
    from .temporal import when  # local import: temporal imports Var at load

    out = Var(0)
    out.set(0)
    cur = Var(0)
    cur.set(a)

    def emit_chunk(start: int) -> None:
        end = min(start + MUL_SHIFT_ADD_CHUNK, bits)
        for bit in range(start, end):
            b_bit = 1 << bit

            def take(cur: Var = cur) -> None:
                out.iadd(cur)

            when(DeathsX(b.value, AtLeast, 1, 0, b_bit), take)
            if bit + 1 < bits:
                cur.iadd(cur)
        if end < bits:
            high_mask = (~((1 << end) - 1)) & 0xFFFFFFFF

            def rest(start: int = end) -> None:
                emit_chunk(start)

            when(DeathsX(b.value, AtLeast, 1, 0, high_mask), rest)

    emit_chunk(0)
    return out


def _saturating_sub_into(dst: "Var", rhs: Any) -> None:
    """Emit StarCraft's saturating ``Subtract``.

    Public arithmetic subtraction wraps mod 2^32 (issue #42), so the paths
    that genuinely depend on underflow clamping — comparison helpers,
    complement kernels, and division's ``den >= rem`` test — opt in here.

    @internal Seam helper shared with the deferred-comparison materializers.
    """
    if _is_plain_int(rhs):
        v = (int(rhs) & dst.bitmask) & 0xFFFFFFFF
        SetDeathsX(dst.value, Subtract, v, 0, dst.bitmask)
        return
    src = rhs if isinstance(rhs, Var) else to_var(rhs)
    _emit_var_transfer(dst.id, src.id, Subtract)


def _const_shl_acts(target: "Var", n: int) -> list[dict[str, Any]]:
    """In-place ``target <<= n`` actions, eudplib ``vbase.py`` parity.

    ``1 <= n <= 31``. The shift is expressed as masked SetTo/Add pairs over
    alternating bit lanes, so it always fits one Trigger (``<= 63`` actions)
    instead of the old per-source-bit trigger ladder.
    """
    mask = (1 << (n + 1)) - 1
    acts: list[dict[str, Any]] = []
    for t in range(31 - n, -1, -1):
        acts.append(
            SetDeathsX(
                target.value,
                SetTo,
                0,
                0,
                ((mask >> 1) << (t + 1)) & 0xFFFFFFFF,
            )
        )
        acts.append(
            SetDeathsX(
                target.value,
                Add,
                ((mask >> 1) << t) & 0xFFFFFFFF,
                0,
                (mask << t) & 0xFFFFFFFF,
            )
        )
    acts.append(SetDeathsX(target.value, SetTo, 0, 0, (mask >> 1) & 0xFFFFFFFF))
    return acts


def _const_shr_acts(target: "Var", n: int) -> list[dict[str, Any]]:
    """In-place ``target >>= n`` actions, eudplib ``vbase.py`` parity.

    ``1 <= n <= 31``. At most 32 masked actions, so it is always one Trigger.
    """
    mask = (1 << (n + 1)) - 1
    acts: list[dict[str, Any]] = [
        SetDeathsX(target.value, SetTo, 0, 0, (mask >> 1) & 0xFFFFFFFF)
    ]
    for t in range(32 - n):
        acts.append(
            SetDeathsX(
                target.value,
                Subtract,
                ((mask >> 1) << t) & 0xFFFFFFFF,
                0,
                (mask << t) & 0xFFFFFFFF,
            )
        )
    return acts


def _complement_into(dst: "Var") -> None:
    """Emit ``dst = ~dst`` inside ``dst.bitmask`` with two alternating-lane adds.

    A full-mask destination gets exactly the historical two acts. A masked
    destination narrows each lane mask so bits outside its value space are
    left untouched (ADR 0018).
    """
    mask = dst.bitmask & 0xFFFFFFFF
    even = 0x55555555 & mask
    odd = 0xAAAAAAAA & mask
    if even != 0:
        SetDeathsX(dst.value, Add, 0x55555555, 0, even)
    if odd != 0:
        SetDeathsX(dst.value, Add, 0xAAAAAAAA, 0, odd)


def _dynamic_shift(
    target: "Var",
    amount: "Var",
    const_acts: Any,
) -> None:
    """Emit the five-stage barrel shl/shr body plus its ``>= 32`` zero guard.

    ``const_acts`` is ``_const_shl_acts`` or ``_const_shr_acts``; each stage
    runs the matching one-trigger constant kernel under a single amount-bit
    gate. The caller owns copying the source into ``target`` and copying a
    temporary result back into the destination.
    """
    from .temporal import when  # local import: temporal imports Var at load

    for k in range(5):
        when(
            DeathsX(amount.value, AtLeast, 1, 0, 1 << k),
            lambda sa=(1 << k): Trigger(conds=[], acts=const_acts(target, sa)),
        )
    Trigger(
        conds=[Deaths(amount.value, AtLeast, 32, 0)],
        acts=[SetDeathsX(target.value, SetTo, 0, 0, 0xFFFFFFFF)],
    )


def _divmod_ladder(num: "Var", den: "Var") -> tuple["Var", "Var"]:
    """Emit one 32-step restoring ladder and return fresh ``(quot, rem)``.

    The ladder is the single source of both results: :meth:`Var.div`,
    :meth:`Var.mod` and :func:`divmod` all route through here and copy only
    the results they expose. ``den == 0`` keeps the historical contract:
    every gated body is skipped, so the pair is ``(0, num)``.
    """
    from .temporal import when  # local import: temporal imports Var at load

    rem = Var(0)
    quot = Var(0)
    diff = Var(0)
    rem.set(0)  # per-invocation reset; Var(0) init runs only once
    quot.set(0)
    for b in range(31, -1, -1):
        # Ticket #55: double `rem` in place. `rem.iadd(rem)` lowers to a
        # self-transfer (`dst_var == src_var`, modifier Add), a first-class
        # path already exercised by the `v.set(v.add(v))` idiom, so no new
        # lowering shape is introduced. Materialising `rem.shl(1)` into a
        # fresh temporary costs ≈30 static triggers / 31 static conditions
        # per ladder iteration (961 T / 992 C per site), plus a copy-back;
        # both are pure overhead.
        # NOTE: executed *actions* rise (the add form updates the slot
        # directly) while executed triggers/conditions fall sharply; the
        # gain is map size and trigger-queue pressure, not raw work.
        rem.iadd(rem)  # rem <<= 1 (self-transfer)
        # #102: one hoisted `diff` replaces the per-bit `diff` + `sub_amt`
        # pair. `diff` is 0 exactly when `rem >= den`; the gated body then
        # takes `den` out of `rem` and sets the quotient bit in one place.
        # `den >= 1` preserves divide-by-zero == 0, the same contract as the
        # old `Deaths(den, AtLeast, 1)` gate.
        #
        # Cycle 4: `diff.set(den)` sits above the numerator-bit trigger. The
        # trigger writes `rem` and reads `num`; the copy writes `diff` and
        # reads `den`, so the two transfers are independent and transfer
        # coalescing folds them into one page firing per bit.
        diff.set(den)
        Trigger(
            conds=[DeathsX(num.value, AtLeast, 1, 0, (1 << b) & 0xFFFFFFFF)],
            acts=[SetDeathsX(rem.value, Add, 1, 0, 0xFFFFFFFF)],
        )
        _saturating_sub_into(diff, rem)  # 0 exactly when rem >= den

        def take(
            rem: Var = rem, quot: Var = quot, den: Var = den, b: int = b
        ) -> None:
            _saturating_sub_into(rem, den)
            quot.iadd((1 << b) & 0xFFFFFFFF)

        when([diff.eq(0), den.ge(1)], take)
    return quot, rem


def _const_divmod(num: "Var", c: int) -> tuple["Var", "Var"]:
    """Constant-divisor restoring ladder returning fresh ``(quot, rem)``.

    The single-result constant paths keep their specialised shapes in
    :func:`_eval_expr_into`; this helper is the one-ladder form for
    :func:`divmod`. It mirrors the general constant-divisor ladder, which
    already subtracts the scaled divisor from ``rem`` and sets the quotient
    bit in the same trigger.
    """
    if c == 0:
        raise EudslError("EUDSL-DIV-ZERO", "Division by zero")
    quot = Var(0)
    rem = Var(0)
    if c == 1:
        quot.set(num)
        return quot, rem
    if (c & (c - 1)) == 0:
        k = c.bit_length() - 1
        _eval_expr_into(quot, num.shr(k))
        _eval_expr_into(rem, num.bitAnd(c - 1))
        return quot, rem
    rem.set(num)
    quot.set(0)
    max_b = 0
    while max_b < 31 and c * (1 << (max_b + 1)) <= 0xFFFFFFFF:
        max_b += 1
    for b in range(max_b, -1, -1):
        scaled = (c * (1 << b)) & 0xFFFFFFFF
        bit = (1 << b) & 0xFFFFFFFF
        Trigger(
            conds=[Deaths(rem.value, AtLeast, scaled, 0)],
            acts=[
                SetDeathsX(rem.value, Subtract, scaled, 0, 0xFFFFFFFF),
                SetDeathsX(quot.value, Add, bit, 0, 0xFFFFFFFF),
            ],
        )
    return quot, rem


def _eval_divmod(lhs: Any, rhs: Any) -> tuple["Var", "Var"]:
    """Evaluate ``divmod(lhs, rhs)`` into a fresh ``(quot, rem)`` pair."""
    num = to_var(lhs)
    operand = to_operand(rhs)
    if _is_plain_int(operand):
        quot, rem = _const_divmod(num, int(operand) & 0xFFFFFFFF)
    else:
        assert isinstance(operand, Var)
        quot, rem = _divmod_ladder(num, operand)
    # Return fresh result vars, so `q, r = divmod(q, r)` never aliases a
    # ladder temporary even though the ladder already reads both operands.
    result_quot = Var(0)
    result_rem = Var(0)
    result_quot.set(quot)
    result_rem.set(rem)
    return result_quot, result_rem


def divmod(a: Any, b: Any) -> tuple["Var", "Var"]:
    """Return ``(a // b, a % b)`` as a pair of fresh Vars from one ladder.

    ``b`` may be a runtime ``Var`` (one 32-step restoring ladder) or a plain
    integer constant (the constant-divisor ladder). Divisor zero yields
    ``(0, a)`` at runtime, matching :meth:`Var.div` / :meth:`Var.mod`; a
    build-time constant zero raises ``EUDSL-DIV-ZERO`` like those paths.

    The builtin ``divmod(a, b)`` reaches the same helper through
    :meth:`Var.__divmod__`.
    """
    return _eval_divmod(a, b)


def _eval_expr_into(dst: "Var", expr: Expr) -> None:
    op = expr.op
    if op == "bitnot":
        lhs = to_operand(expr.lhs)
        if _is_plain_int(lhs):
            dst.set((~int(lhs)) & 0xFFFFFFFF)
            return
        assert isinstance(lhs, Var)
        if dst.id != lhs.id:
            dst.set(lhs)
        _complement_into(dst)
        return

    if op == "neg":
        lhs = to_operand(expr.lhs)
        if _is_plain_int(lhs):
            dst.set((-int(lhs)) & 0xFFFFFFFF)
            return
        assert isinstance(lhs, Var)
        if dst.bitmask != 0xFFFFFFFF:
            # The complement and +1 run on a full-mask temporary so the
            # destination's raw lane keeps every bit outside its value space.
            target = Var(0)
            target.set(lhs)
            target.iand(lhs.bitmask)
            _complement_into(target)
            SetDeathsX(target.value, Add, 1, 0, 0xFFFFFFFF)
            dst.set(target)
            return
        if dst.id != lhs.id:
            dst.set(lhs)
        _complement_into(dst)
        SetDeathsX(dst.value, Add, 1, 0, 0xFFFFFFFF)
        return

    lhs = to_operand(expr.lhs)
    rhs = to_operand(expr.rhs)

    if _is_plain_int(lhs) and _is_plain_int(rhs):
        lval = int(lhs) & 0xFFFFFFFF
        rval = int(rhs) & 0xFFFFFFFF
        if op == "add":
            dst.set((lval + rval) & 0xFFFFFFFF)
            return
        if op == "sub":
            dst.set((lval - rval) & 0xFFFFFFFF)
            return
        if op == "mul":
            dst.set((lval * rval) & 0xFFFFFFFF)
            return
        if op == "div":
            if rval == 0:
                raise EudslError("EUDSL-DIV-ZERO", "Division by zero")
            dst.set((lval // rval) & 0xFFFFFFFF)
            return
        if op == "mod":
            if rval == 0:
                raise EudslError("EUDSL-DIV-ZERO", "Modulo by zero")
            dst.set((lval % rval) & 0xFFFFFFFF)
            return
        if op == "bitand":
            dst.set((lval & rval) & 0xFFFFFFFF)
            return
        if op == "bitor":
            dst.set((lval | rval) & 0xFFFFFFFF)
            return
        if op == "bitxor":
            dst.set((lval ^ rval) & 0xFFFFFFFF)
            return
        if op == "shl":
            dst.set(0 if rval >= 32 else ((lval << rval) & 0xFFFFFFFF))
            return
        if op == "shr":
            dst.set(0 if rval >= 32 else (lval >> rval))
            return

    if op == "add":
        if _is_plain_int(lhs) and isinstance(rhs, Var):
            if dst.id == rhs.id:
                dst.iadd(int(lhs))
            else:
                dst.set(int(lhs))
                dst.iadd(rhs)
            return
        if isinstance(lhs, Var) and _is_plain_int(rhs):
            if dst.id == lhs.id:
                dst.iadd(int(rhs))
            else:
                dst.set(int(rhs))
                dst.iadd(lhs)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        if dst.id == lhs.id:
            dst.iadd(rhs)
        elif dst.id == rhs.id:
            dst.iadd(lhs)
        else:
            dst.set(0)
            dst.iadd(lhs)
            dst.iadd(rhs)
        return

    if op == "sub":
        if isinstance(lhs, Var) and _is_plain_int(rhs):
            if dst.id == lhs.id:
                dst.isub(int(rhs))
            else:
                dst.set(lhs)
                dst.isub(int(rhs))
            return
        if _is_plain_int(lhs) and isinstance(rhs, Var):
            if dst.id != rhs.id:
                dst.set(int(lhs))
                dst.isub(rhs)
            else:
                tmp = Var(int(lhs))
                tmp.isub(rhs)
                dst.set(tmp)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        if dst.id == lhs.id:
            dst.isub(rhs)
        elif dst.id != rhs.id:
            dst.set(lhs)
            dst.isub(rhs)
        else:
            tmp = Var(0)
            tmp.set(lhs)
            tmp.isub(rhs)
            dst.set(tmp)
        return

    if op == "bitand":
        if _is_plain_int(lhs) or _is_plain_int(rhs):
            v = rhs if _is_plain_int(lhs) else lhs
            c = int(lhs if _is_plain_int(lhs) else rhs) & 0xFFFFFFFF
            assert isinstance(v, Var)
            if dst.id != v.id:
                dst.set(v)
            SetDeathsX(dst.value, SetTo, 0, 0, (~c) & dst.bitmask)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        if lhs.id == rhs.id:
            if dst.id != lhs.id:
                dst.set(lhs)
            return
        if dst.bitmask != 0xFFFFFFFF:
            target = Var(0)
            target.set(lhs)
            target.iand(lhs.bitmask)
        else:
            target = dst if dst.id != rhs.id else Var(0)
            if target.id != lhs.id:
                target.set(lhs)
        # The trigger's action is a normal ``target &= rhs`` kernel whose mask
        # field is patched at run time by the preceding setup acts: the
        # build-time value is only mask 1 (a non-BitCell placeholder for
        # eudkit-opt). Those acts widen the mask to all bits, then store
        # ``rhs`` into it with Subtract so the effective mask becomes ``~rhs``.
        # Registering after the setup acts keeps them ahead of the trigger.
        t = Trigger(
            conds=[],
            acts=[SetDeathsX(target.value, SetTo, 0, 0, 1)],
            register=False,
        )
        SetDeathsX(t.act(0).mask, SetTo, 0xFFFFFFFF, 0, 0xFFFFFFFF)
        emitVarStore(
            rhs.id,
            TriggerFieldReloc(t.id, t.act(0).mask.byteOffset),
            Subtract,
        )
        _register_trigger(t)
        if target.id != dst.id:
            dst.set(target)
        return

    if op == "bitor":
        if _is_plain_int(lhs) or _is_plain_int(rhs):
            v = rhs if _is_plain_int(lhs) else lhs
            c = int(lhs if _is_plain_int(lhs) else rhs) & 0xFFFFFFFF
            assert isinstance(v, Var)
            if dst.id != v.id:
                dst.set(v)
            m = c & dst.bitmask
            SetDeathsX(dst.value, SetTo, m, 0, m)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        if lhs.id == rhs.id:
            if dst.id != lhs.id:
                dst.set(lhs)
            return
        if dst.bitmask != 0xFFFFFFFF:
            target = Var(0)
            target.set(lhs)
            target.iand(lhs.bitmask)
        else:
            target = dst if dst.id != rhs.id else Var(0)
            if target.id != lhs.id:
                target.set(lhs)
        # ``target |= rhs``: the build-time placeholder under mask 1 is
        # widened at run time by the preceding setup acts, which store
        # ``rhs`` into the mask field and so select exactly its set bits.
        t = Trigger(
            conds=[],
            acts=[SetDeathsX(target.value, SetTo, 0xFFFFFFFF, 0, 1)],
            register=False,
        )
        emitVarStore(
            rhs.id,
            TriggerFieldReloc(t.id, t.act(0).mask.byteOffset),
            SetTo,
        )
        _register_trigger(t)
        if target.id != dst.id:
            dst.set(target)
        return

    if op == "bitxor":
        if _is_plain_int(lhs) or _is_plain_int(rhs):
            v = rhs if _is_plain_int(lhs) else lhs
            c = int(lhs if _is_plain_int(lhs) else rhs) & 0xFFFFFFFF
            assert isinstance(v, Var)
            if dst.id != v.id:
                dst.set(v)
            if c != 0:
                even_mask = 0x55555555 & dst.bitmask
                odd_mask = 0xAAAAAAAA & dst.bitmask
                even = c & even_mask
                odd = c & odd_mask
                if even != 0:
                    SetDeathsX(dst.value, Add, even, 0, even_mask)
                if odd != 0:
                    SetDeathsX(dst.value, Add, odd, 0, odd_mask)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        if lhs.id == rhs.id:
            dst.set(0)
            return
        if dst.bitmask != 0xFFFFFFFF:
            target = Var(0)
            target.set(lhs)
            target.iand(lhs.bitmask)
        else:
            target = dst if dst.id != rhs.id else Var(0)
            if target.id != lhs.id:
                target.set(lhs)
        # Both action values are patched from ``rhs`` after construction:
        # adding ``rhs`` under the even/odd masks toggles exactly its set bits.
        t = Trigger(
            conds=[],
            acts=[
                SetDeathsX(target.value, Add, 0, 0, 0x55555555),
                SetDeathsX(target.value, Add, 0, 0, 0xAAAAAAAA),
            ],
            register=False,
        )
        emitVarStore(
            rhs.id,
            TriggerFieldReloc(t.id, t.act(0).value.byteOffset),
            SetTo,
        )
        emitVarStore(
            rhs.id,
            TriggerFieldReloc(t.id, t.act(1).value.byteOffset),
            SetTo,
        )
        _register_trigger(t)
        if target.id != dst.id:
            dst.set(target)
        return

    if op == "shl":
        if isinstance(lhs, Var) and _is_plain_int(rhs):
            k = int(rhs) & 0xFFFFFFFF
            if k == 0:
                if dst.id != lhs.id:
                    dst.set(lhs)
                return
            if k >= 32:
                dst.set(0)
                return
            if dst.bitmask != 0xFFFFFFFF:
                target = Var(0)
                target.set(lhs)
                target.iand(lhs.bitmask)
                Trigger(conds=[], acts=_const_shl_acts(target, k))
                dst.set(target)
                return
            if dst.id != lhs.id:
                dst.set(lhs)
            Trigger(conds=[], acts=_const_shl_acts(dst, k))
            return
        v1 = to_var(lhs)
        assert isinstance(rhs, Var)
        v2 = rhs
        if dst.bitmask != 0xFFFFFFFF:
            target = Var(0)
            target.set(v1)
            target.iand(v1.bitmask)
        else:
            target = dst if dst.id != v2.id else Var(0)
            if target.id != v1.id:
                target.set(v1)
        _dynamic_shift(target, v2, _const_shl_acts)
        if target.id != dst.id:
            dst.set(target)
        return

    if op == "shr":
        if isinstance(lhs, Var) and _is_plain_int(rhs):
            k = int(rhs) & 0xFFFFFFFF
            if k == 0:
                if dst.id != lhs.id:
                    dst.set(lhs)
                return
            if k >= 32:
                dst.set(0)
                return
            if dst.bitmask != 0xFFFFFFFF:
                target = Var(0)
                target.set(lhs)
                target.iand(lhs.bitmask)
                Trigger(conds=[], acts=_const_shr_acts(target, k))
                dst.set(target)
                return
            if dst.id != lhs.id:
                dst.set(lhs)
            Trigger(conds=[], acts=_const_shr_acts(dst, k))
            return
        v1 = to_var(lhs)
        assert isinstance(rhs, Var)
        v2 = rhs
        if dst.bitmask != 0xFFFFFFFF:
            target = Var(0)
            target.set(v1)
            target.iand(v1.bitmask)
        else:
            target = dst if dst.id != v2.id else Var(0)
            if target.id != v1.id:
                target.set(v1)
        _dynamic_shift(target, v2, _const_shr_acts)
        if target.id != dst.id:
            dst.set(target)
        return

    if op == "mul":
        if _is_plain_int(lhs) or _is_plain_int(rhs):
            v = rhs if _is_plain_int(lhs) else lhs
            c = int(lhs if _is_plain_int(lhs) else rhs) & 0xFFFFFFFF
            assert isinstance(v, Var)
            if c == 0:
                dst.set(0)
                return
            if c == 1:
                if dst.id != v.id:
                    dst.set(v)
                return
            if c == 0xFFFFFFFF:
                _eval_expr_into(dst, v.neg())
                return
            if (c & (c - 1)) == 0:
                _eval_expr_into(dst, v.shl(c.bit_length() - 1))
                return
            # Cycle 4: general k-term shift-add chain behind a measured cost
            # gate. `bits` is ascending; emit the highest term first, then
            # descend. `2k + 5` is the measured upper bound on the chain's
            # executed triggers (k = 2..14, low/high bit extremes).
            bits = [b for b in range(32) if (c >> b) & 1]
            window = 33 - ((c & -c).bit_length() - 1)  # 1 + (32 - ctz(c))
            if 2 * len(bits) + 5 <= window:
                acc = dst if dst.id != v.id else Var(0)
                _eval_expr_into(acc, v.shl(bits[-1]))
                for b in reversed(bits[:-1]):
                    acc.iadd(v.shl(b) if b else v)
                if acc.id != dst.id:
                    dst.set(acc)
                return
            target = dst if dst.id != v.id else Var(0)
            target.set(0)
            for b in range(32):
                bit = (1 << b) & 0xFFFFFFFF
                weight = (c * bit) & 0xFFFFFFFF
                if weight != 0:
                    Trigger(
                        conds=[DeathsX(v.value, AtLeast, 1, 0, bit)],
                        acts=[SetDeathsX(target.value, Add, weight, 0, 0xFFFFFFFF)],
                    )
            if target.id != dst.id:
                dst.set(target)
            return
        assert isinstance(lhs, Var) and isinstance(rhs, Var)
        dst.set(mul_shift_add(lhs, rhs, 32))
        return

    if op in ("div", "mod"):
        if isinstance(lhs, Var) and _is_plain_int(rhs):
            c = int(rhs) & 0xFFFFFFFF
            if c == 0:
                raise EudslError("EUDSL-DIV-ZERO", "Division by zero")
            if c == 1:
                if op == "div":
                    if dst.id != lhs.id:
                        dst.set(lhs)
                else:
                    dst.set(0)
                return
            if (c & (c - 1)) == 0:
                k = c.bit_length() - 1
                if op == "div":
                    _eval_expr_into(dst, lhs.shr(k))
                else:
                    _eval_expr_into(dst, lhs.bitAnd(c - 1))
                return
            rem = Var(0)
            rem.set(lhs)
            if op == "div":
                dst.set(0)
            max_b = 0
            while max_b < 31 and c * (1 << (max_b + 1)) <= 0xFFFFFFFF:
                max_b += 1
            for b in range(max_b, -1, -1):
                scaled = (c * (1 << b)) & 0xFFFFFFFF
                bit = (1 << b) & 0xFFFFFFFF
                if op == "div":
                    Trigger(
                        conds=[Deaths(rem.value, AtLeast, scaled, 0)],
                        acts=[
                            SetDeathsX(rem.value, Subtract, scaled, 0, 0xFFFFFFFF),
                            SetDeathsX(dst.value, Add, bit, 0, 0xFFFFFFFF),
                        ],
                    )
                else:
                    Trigger(
                        conds=[Deaths(rem.value, AtLeast, scaled, 0)],
                        acts=[SetDeathsX(rem.value, Subtract, scaled, 0, 0xFFFFFFFF)],
                    )
            if op == "mod":
                dst.set(rem)
            return
        num = to_var(lhs)
        assert isinstance(rhs, Var)
        quot, rem = _divmod_ladder(num, rhs)
        # `quot`/`rem` are fresh, so aliasing (dst == num or dst == den) is
        # safe: the ladder reads the operands and the result lands once, here.
        if op == "div":
            dst.set(quot)
        else:
            dst.set(rem)
        return


class DeferredCond:
    """A comparison recorded as intent, materialized at its use site.

    Every :meth:`materialize` re-emits the whole act sequence into the same
    temporaries: the accumulator is reset to its init first, then the
    comparison acts accumulate into it. The first call allocates the
    temporaries and later calls reuse them — both emit the reset, so a
    ``while_loop`` check slot stays valid when the loop tail rebuilds it and a
    one-shot site (``when``, a raw ``Trigger``) still starts each pass clean.
    """

    def __init__(
        self,
        target: "Var",
        op: str,
        rhs: Any,
        ne_const: int | None = None,
    ) -> None:
        self.target = target          # Var (left operand)
        self.op = op                  # "eq" | "ne" | "ge" | "le" | "gt" | "lt"
        self.rhs = rhs                # Var | ReadonlyVar | Expr (or const for ne_const)
        self.ne_const = ne_const      # masked middle-value constant for Var.ne(c)
        self._tmp: "Var | None" = None
        self._rhs_tmp: "Var | None" = None

    def __bool__(self) -> bool:
        """Refuse build-time branching: a deferred comparison is not a bool."""
        raise _build_time_truthiness_error("condition")

    def complement(self) -> Any:
        """Return the opposite comparison, keeping the same deferred shape."""
        if self.op == "ne" and self.ne_const is not None:
            # `ne` with a middle constant is the deferred `neq`; its opposite
            # is a direct equality against that constant (1C, 0T).
            return self.target.eq(self.ne_const)
        if self.op == "eq":
            return self.target.ne(self.rhs)
        if self.op == "ne":
            return self.target.eq(self.rhs)
        if self.op == "ge":
            return self.target.lt(self.rhs)
        if self.op == "lt":
            return self.target.ge(self.rhs)
        if self.op == "gt":
            return self.target.le(self.rhs)
        if self.op == "le":
            return self.target.gt(self.rhs)
        raise AssertionError(f"unknown comparison op {self.op!r}")

    # Dict-style reads keep the pre-seam API working (test_ticket96 reads
    # ``cond["comparison"]``); this materializes exactly as ``when`` would.
    def __getitem__(self, key: str) -> Any:
        return self.materialize()[key]

    def _operand(self) -> int | "Var":
        if isinstance(self.rhs, Expr):
            if self._rhs_tmp is None:
                self._rhs_tmp = Var(0)
            _eval_expr_into(self._rhs_tmp, self.rhs)  # re-evaluated every pass
            return self._rhs_tmp
        return to_operand(self.rhs)

    def _temp(self, init: int) -> "Var":
        """Temp whose accumulation is reset at every materialization."""
        if self._tmp is None:
            self._tmp = Var(init)
        # The declaration init runs once; every evaluation site needs the
        # reset re-emitted, including the first (one-shot) one.
        self._tmp.set(init)
        return self._tmp

    def _fresh_tmp(self, init: int) -> "Var":
        """Temp whose first act each pass is an explicit ``set()``."""
        if self._tmp is None:
            self._tmp = Var(init)
        return self._tmp

    def materialize(self) -> dict[str, Any]:
        op = self._operand()
        target = self.target
        if self.ne_const is not None:
            init = (-self.ne_const) & 0xFFFFFFFF
            if target.bitmask == 0xFFFFFFFF:
                diff = self._temp(init)          # unchanged full-mask path
                diff.iadd(target)
            else:
                diff = self._fresh_tmp(0)
                _eval_expr_into(diff, Expr("bitand", target, target.bitmask))
                diff.iadd(init)
            return Deaths(diff.value, AtLeast, 1, 0)
        if self.op in ("eq", "ne"):
            diff = self._temp(0xFFFFFFFF)
            _saturating_sub_into(diff, op)
            diff.iadd(1)
            diff.iadd(target)
            if self.op == "eq":
                return Deaths(diff.value, Exactly, 0, 0)
            return Deaths(diff.value, AtLeast, 1, 0)
        if self.op in ("lt", "ge"):
            diff = self._fresh_tmp(0)
            diff.set(op)
            _saturating_sub_into(diff, target)
            if self.op == "lt":
                return Deaths(diff.value, AtLeast, 1, 0)
            return Deaths(diff.value, Exactly, 0, 0)
        assert self.op in ("le", "gt")
        diff = self._fresh_tmp(0)
        diff.set(target)
        _saturating_sub_into(diff, op)
        if self.op == "le":
            return Deaths(diff.value, Exactly, 0, 0)
        return Deaths(diff.value, AtLeast, 1, 0)


class Var:
    """Mutable 32-bit scalar variable backed by a `64A` `TriggerPage` action slot.

    ``pageIndex``/``slotIndex`` are ``TriggerPage`` coordinates ONLY for
    page-pinned variables (constructed with ``storage="page"``, either by
    the constructor allocator or by an explicit-id page site such as a
    struct field or the String base variable). For such variables they
    report the pinned rank assigned at allocation, which under pinned-first
    lowering is also the final rank. For every other variable they keep
    the legacy id-derived values (``id // 64``, ``id % 64``) and must NOT
    be read as page coordinates: non-page variables consume ids but no
    page slots, and at lowering they become dword/bit cells rather than
    page slots.
    """

    def __init__(
        self,
        init: Any = 0,
        explicit_id: int | None = None,
        explicit_mask: int | None = None,
        storage: str | None = None,
        # INTERNAL deferred flag (globals P1-01 prefactor; not public API,
        # not exported as a user option — the public path is `lazyVar`):
        # when set, skip allocation, field-ref creation, and any init
        # statement. The handle resolves its identity on first use through
        # a per-builder binding (see `_resolve`).
        deferred: bool = False,
    ) -> None:
        is_const = _is_plain_int(init)
        const_val = (int(init) & 0xFFFFFFFF) if is_const else 0
        mask_val = (
            (explicit_mask & 0xFFFFFFFF) if explicit_mask is not None else 0xFFFFFFFF
        )
        self.bitmask: int = mask_val
        self.init: int = const_val
        self.initialValue: int = const_val
        self.initial_value: int = const_val
        self._deferred = LazyBinding("Var") if deferred else None
        if deferred:
            if explicit_id is not None:
                raise EudslError(
                    "EUDSL-TYPE", "deferred Var cannot carry an explicit id"
                )
            if not is_const:
                # D4 (const-only init): a deferred `.set()` would be
                # scope-shifted by a conditional first use, so non-const
                # init fails fast here instead of silently dropping the
                # initialization.
                raise EudslError(
                    "EUDSL-TYPE", "deferred Var init must be a constant number"
                )
            self._deferred_storage = storage
            return
        var_id = (
            explicit_id
            if explicit_id is not None
            else _alloc_var_slot(const_val, mask_val, storage)
        )
        self._id: int = var_id
        if storage == "page":
            # The page allocation that produced this variable (the
            # constructor's own _alloc_var_slot, or the call site's
            # _alloc_var_slot for an explicit-id page variable) has already
            # incremented the page counter, so the rank just allocated is
            # the current counter minus one. Read it only: this must never
            # allocate a second page slot. Outside buildModule the page
            # counter falls back to the raw counter, so the rank equals the
            # id and behaviour is unchanged there.
            rank = _get_next_page_slot() - 1
            self._pageIndex: int = rank // ACT_COUNT
            self._slotIndex: int = rank % ACT_COUNT
        else:
            self._pageIndex: int = var_id // ACT_COUNT
            self._slotIndex: int = var_id % ACT_COUNT

        self._mask = TriggerFieldRef.forVar(var_id, 0, 4, mask_val)
        self._dest = TriggerFieldRef.forVar(var_id, 16, 4, 0)
        self._value = TriggerFieldRef.forVar(var_id, 20, 4, const_val)
        self._modifier = TriggerFieldRef.forVar(var_id, 27, 1, SetTo)

        if not is_const:
            self.set(init)

    # -------------------------------------------------------------------------
    # Deferred resolution
    # -------------------------------------------------------------------------

    def _resolve(self) -> "Var":
        """The eager instance serving identity reads: this for eager
        handles, the active builder's binding for deferred ones.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the eager construction varies here."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(
            lambda: Var(
                self.init,
                explicit_mask=self.bitmask,
                storage=self._deferred_storage,
            )
        )

    @property
    def id(self) -> int:
        return self._resolve()._id

    @property
    def pageIndex(self) -> int:
        return self._resolve()._pageIndex

    @property
    def page_index(self) -> int:
        return self._resolve()._pageIndex

    @property
    def slotIndex(self) -> int:
        return self._resolve()._slotIndex

    @property
    def slot_index(self) -> int:
        return self._resolve()._slotIndex

    @property
    def mask(self) -> Any:
        return self._resolve()._mask

    @property
    def dest(self) -> Any:
        return self._resolve()._dest

    @property
    def value(self) -> Any:
        return self._resolve()._value

    @property
    def modifier(self) -> Any:
        return self._resolve()._modifier

    def copy(self) -> "Var":
        return Var(self)

    # -------------------------------------------------------------------------
    # Declarative Action Constructors
    # -------------------------------------------------------------------------

    def act_set(self, rhs: int) -> dict[str, Any]:
        v = (_require_plain_int(rhs, "Var.set operand") & self.bitmask) & 0xFFFFFFFF
        return SetDeathsX(self.value, SetTo, v, 0, self.bitmask)

    def act_add(self, rhs: int) -> dict[str, Any]:
        v = (_require_plain_int(rhs, "Var.iadd operand") & self.bitmask) & 0xFFFFFFFF
        return SetDeathsX(self.value, Add, v, 0, self.bitmask)

    def act_sub(self, rhs: int) -> dict[str, Any]:
        return self.act_add((-_require_plain_int(rhs, "Var.isub operand")) & 0xFFFFFFFF)

    # -------------------------------------------------------------------------
    # In-place Mutations
    # -------------------------------------------------------------------------

    def set(self, rhs: Any) -> None:
        if _is_plain_int(rhs):
            self.act_set(int(rhs))
            return
        if isinstance(rhs, ReadonlyVar):
            _emit_var_transfer(self.id, rhs.id, SetTo)
            return
        if isinstance(rhs, Var):
            _emit_var_transfer(self.id, rhs.id, SetTo)
            return
        if isinstance(rhs, Expr):
            _eval_expr_into(self, rhs)
            return
        raise EudslError(
            "EUDSL-TYPE",
            f"Var.set operand must be an integer constant, a Var, or an Expr; "
            f"got {_describe_value(rhs)}",
        )

    def iadd(self, rhs: Any) -> None:
        if _is_plain_int(rhs):
            self.act_add(int(rhs))
            return
        if isinstance(rhs, ReadonlyVar):
            _emit_var_transfer(self.id, rhs.id, Add)
            return
        if isinstance(rhs, Var):
            _emit_var_transfer(self.id, rhs.id, Add)
            return
        if isinstance(rhs, Expr):
            if rhs.op == "add":
                self.iadd(rhs.lhs)
                self.iadd(rhs.rhs)
                return
            tmp = to_var(rhs)
            _emit_var_transfer(self.id, tmp.id, Add)
            return
        raise EudslError(
            "EUDSL-TYPE",
            f"Var.iadd operand must be an integer constant, a Var, or an Expr; "
            f"got {_describe_value(rhs)}",
        )

    def isub(self, rhs: Any) -> None:
        if _is_plain_int(rhs):
            # eudplib parity: ``a - n`` is wrapping addition of the
            # two's complement, so negative constants work too.
            self.act_sub(int(rhs))
            return
        if isinstance(rhs, ReadonlyVar):
            rhs = rhs._var
        if isinstance(rhs, Var):
            # eudplib's Var form is ``(self + 1) + (0xFFFFFFFF - other)``.
            # Materialize the complement before mutating ``self`` so
            # ``v -= v`` and ``dst.isub(rhs)`` with ``dst is rhs`` read
            # the original value.
            inv = Var(0)
            inv.set(0xFFFFFFFF)
            _saturating_sub_into(inv, rhs)
            self.iadd(1)
            self.iadd(inv)
            return
        if isinstance(rhs, Expr):
            self.isub(to_var(rhs))
            return
        raise EudslError(
            "EUDSL-TYPE",
            f"Var.isub operand must be an integer constant, a Var, or an Expr; "
            f"got {_describe_value(rhs)}",
        )

    def imul(self, rhs: Any) -> None:
        _eval_expr_into(self, self.mul(rhs))

    def idiv(self, rhs: Any) -> None:
        _eval_expr_into(self, self.div(rhs))

    def imod(self, rhs: Any) -> None:
        _eval_expr_into(self, self.mod(rhs))

    def iand(self, rhs: Any) -> None:
        if _is_plain_int(rhs):
            SetDeathsX(self.value, SetTo, 0, 0, (~int(rhs)) & 0xFFFFFFFF)
            return
        _eval_expr_into(self, self.bitAnd(rhs))

    def ior(self, rhs: Any) -> None:
        if _is_plain_int(rhs):
            c = int(rhs) & 0xFFFFFFFF
            SetDeathsX(self.value, SetTo, c, 0, c)
            return
        _eval_expr_into(self, self.bitOr(rhs))

    def ixor(self, rhs: Any) -> None:
        _eval_expr_into(self, self.bitXor(rhs))

    def ishl(self, rhs: Any) -> None:
        _eval_expr_into(self, self.shl(rhs))

    def ishr(self, rhs: Any) -> None:
        _eval_expr_into(self, self.shr(rhs))

    # Python in-place operator overloads (`+=`, `-=`, etc.)
    def __iadd__(self, rhs: Any) -> "Var":
        self.iadd(rhs)
        return self

    def __isub__(self, rhs: Any) -> "Var":
        self.isub(rhs)
        return self

    def __imul__(self, rhs: Any) -> "Var":
        self.imul(rhs)
        return self

    def __ifloordiv__(self, rhs: Any) -> "Var":
        self.idiv(rhs)
        return self

    def __imod__(self, rhs: Any) -> "Var":
        self.imod(rhs)
        return self

    def __iand__(self, rhs: Any) -> "Var":
        self.iand(rhs)
        return self

    def __ior__(self, rhs: Any) -> "Var":
        self.ior(rhs)
        return self

    def __ixor__(self, rhs: Any) -> "Var":
        self.ixor(rhs)
        return self

    def __ilshift__(self, rhs: Any) -> "Var":
        self.ishl(rhs)
        return self

    def __irshift__(self, rhs: Any) -> "Var":
        self.ishr(rhs)
        return self

    # -------------------------------------------------------------------------
    # Pure Expressions
    # -------------------------------------------------------------------------

    def add(self, rhs: Any) -> Expr:
        return Expr("add", self, rhs)

    def sub(self, rhs: Any) -> Expr:
        return Expr("sub", self, rhs)

    def mul(self, rhs: Any) -> Expr:
        return Expr("mul", self, rhs)

    def div(self, rhs: Any) -> Expr:
        return Expr("div", self, rhs)

    def mod(self, rhs: Any) -> Expr:
        return Expr("mod", self, rhs)

    def divmod(self, rhs: Any) -> tuple["Var", "Var"]:
        """Return ``(self // rhs, self % rhs)`` from one restoring ladder."""
        return _eval_divmod(self, rhs)

    def bitAnd(self, rhs: Any) -> Expr:
        return Expr("bitand", self, rhs)

    def bit_and(self, rhs: Any) -> Expr:
        return self.bitAnd(rhs)

    def band(self, rhs: Any) -> Expr:
        return self.bitAnd(rhs)

    def bitOr(self, rhs: Any) -> Expr:
        return Expr("bitor", self, rhs)

    def bit_or(self, rhs: Any) -> Expr:
        return self.bitOr(rhs)

    def bor(self, rhs: Any) -> Expr:
        return self.bitOr(rhs)

    def bitXor(self, rhs: Any) -> Expr:
        return Expr("bitxor", self, rhs)

    def bit_xor(self, rhs: Any) -> Expr:
        return self.bitXor(rhs)

    def xor(self, rhs: Any) -> Expr:
        return self.bitXor(rhs)

    def bxor(self, rhs: Any) -> Expr:
        return self.bitXor(rhs)

    def bitNot(self) -> Expr:
        return Expr("bitnot", self)

    def bit_not(self) -> Expr:
        return self.bitNot()

    def invert(self) -> Expr:
        return self.bitNot()

    def bnot(self) -> Expr:
        return self.bitNot()

    def shl(self, rhs: Any) -> Expr:
        return Expr("shl", self, rhs)

    def lshift(self, rhs: Any) -> Expr:
        return self.shl(rhs)

    def shr(self, rhs: Any) -> Expr:
        return Expr("shr", self, rhs)

    def rshift(self, rhs: Any) -> Expr:
        return self.shr(rhs)

    def neg(self) -> Expr:
        return Expr("neg", self)

    # Python binary/unary arithmetic & bitwise operator overloads
    def __add__(self, rhs: Any) -> Expr:
        return self.add(rhs)

    def __radd__(self, lhs: Any) -> Expr:
        return Expr("add", lhs, self)

    def __sub__(self, rhs: Any) -> Expr:
        return self.sub(rhs)

    def __rsub__(self, lhs: Any) -> Expr:
        return Expr("sub", lhs, self)

    def __mul__(self, rhs: Any) -> Expr:
        return self.mul(rhs)

    def __rmul__(self, lhs: Any) -> Expr:
        return Expr("mul", lhs, self)

    def __floordiv__(self, rhs: Any) -> Expr:
        return self.div(rhs)

    def __rfloordiv__(self, lhs: Any) -> Expr:
        return Expr("div", lhs, self)

    def __mod__(self, rhs: Any) -> Expr:
        return self.mod(rhs)

    def __rmod__(self, lhs: Any) -> Expr:
        return Expr("mod", lhs, self)

    def __divmod__(self, rhs: Any) -> tuple["Var", "Var"]:
        return self.divmod(rhs)

    def __rdivmod__(self, lhs: Any) -> tuple["Var", "Var"]:
        return _eval_divmod(lhs, self)

    def __and__(self, rhs: Any) -> Expr:
        return self.bitAnd(rhs)

    def __rand__(self, lhs: Any) -> Expr:
        return Expr("bitand", lhs, self)

    def __or__(self, rhs: Any) -> Expr:
        return self.bitOr(rhs)

    def __ror__(self, lhs: Any) -> Expr:
        return Expr("bitor", lhs, self)

    def __xor__(self, rhs: Any) -> Expr:
        return self.bitXor(rhs)

    def __rxor__(self, lhs: Any) -> Expr:
        return Expr("bitxor", lhs, self)

    def __invert__(self) -> Expr:
        return self.bitNot()

    def __neg__(self) -> Expr:
        return self.neg()

    def __lshift__(self, rhs: Any) -> Expr:
        return self.shl(rhs)

    def __rlshift__(self, lhs: Any) -> Expr:
        return Expr("shl", lhs, self)

    def __rshift__(self, rhs: Any) -> Expr:
        return self.shr(rhs)

    def __rrshift__(self, lhs: Any) -> Expr:
        return Expr("shr", lhs, self)

    # -------------------------------------------------------------------------
    # Comparisons (1C direct Deaths for constants; temp diff for variable RHS)
    # -------------------------------------------------------------------------

    def _value_space(self) -> FieldValueSpace:
        """This ``Var``'s comparison kernel: its mask, no byte shift."""
        return FieldValueSpace(self.bitmask, 0, 4)

    def eq(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.eq operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "eq", rhs)
        return _apply_comparison(
            self._value_space(),
            "eq",
            rhs,
            lambda: self.value,
            what="Var.eq operand",
            full_mask_uses_deaths=True,
        )

    def ne(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.ne operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "ne", rhs)
        return _apply_comparison(
            self._value_space(),
            "ne",
            rhs,
            lambda: self.value,
            what="Var.ne operand",
            full_mask_uses_deaths=True,
            defer=lambda c: DeferredCond(self, "ne", rhs, ne_const=c),
        )

    def ge(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.ge operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "ge", rhs)
        return _apply_comparison(
            self._value_space(),
            "ge",
            rhs,
            lambda: self.value,
            what="Var.ge operand",
            full_mask_uses_deaths=True,
        )

    def le(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.le operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "le", rhs)
        return _apply_comparison(
            self._value_space(),
            "le",
            rhs,
            lambda: self.value,
            what="Var.le operand",
            full_mask_uses_deaths=True,
        )

    def gt(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.gt operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "gt", rhs)
        return _apply_comparison(
            self._value_space(),
            "gt",
            rhs,
            lambda: self.value,
            what="Var.gt operand",
            full_mask_uses_deaths=True,
        )

    def lt(self, rhs: Any) -> Any:
        if not _is_plain_int(rhs):
            if not isinstance(rhs, (Var, Expr, ReadonlyVar)):
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Var.lt operand must be an integer constant, a Var, or an "
                    f"Expr; got {_describe_value(rhs)}",
                )
            return DeferredCond(self, "lt", rhs)
        return _apply_comparison(
            self._value_space(),
            "lt",
            rhs,
            lambda: self.value,
            what="Var.lt operand",
            full_mask_uses_deaths=True,
        )

    def at_least(self, rhs: Any) -> Any:
        return self.ge(rhs)

    atLeast = at_least

    def at_most(self, rhs: Any) -> Any:
        return self.le(rhs)

    atMost = at_most

    def exactly(self, rhs: Any) -> Any:
        return self.eq(rhs)

    # Python comparison operator overloads (`==`, `!=`, `<`, `<=`, `>`, `>=`)
    def __bool__(self) -> bool:
        """Refuse build-time branching: a Var is a runtime value, not a bool."""
        raise _build_time_truthiness_error("value")

    def __eq__(self, rhs: Any) -> Any:  # type: ignore[override]
        if isinstance(rhs, (bool, float, str)):
            return self.eq(rhs)
        if not (
            _is_plain_int(rhs) or isinstance(rhs, (Var, Expr, ReadonlyVar))
        ):
            return NotImplemented
        return self.eq(rhs)

    def __ne__(self, rhs: Any) -> Any:  # type: ignore[override]
        if isinstance(rhs, (bool, float, str)):
            return self.ne(rhs)
        if not (
            _is_plain_int(rhs) or isinstance(rhs, (Var, Expr, ReadonlyVar))
        ):
            return NotImplemented
        return self.ne(rhs)

    def __lt__(self, rhs: Any) -> Any:
        return self.lt(rhs)

    def __le__(self, rhs: Any) -> Any:
        return self.le(rhs)

    def __gt__(self, rhs: Any) -> Any:
        return self.gt(rhs)

    def __ge__(self, rhs: Any) -> Any:
        return self.ge(rhs)

    def __hash__(self) -> int:
        return hash(("Var", self.id))


def lazyVar(
    init: Any = 0,
    mask: int | None = None,
    storage: str | None = None,
) -> "Var":
    """Declare a lazy ``Var`` (globals P1-02): a handle constructible
    anywhere (no builder needed) that resolves to a fully eager binding
    on first resolving use inside ``build_module``, one binding per
    builder.

    Only constant-number init is accepted: a non-const init (another
    ``Var``, an ``Expr``) raises ``EUDSL-TYPE`` eagerly here, because a
    deferred ``.set()`` would be scope-shifted by a conditional first use
    (design D4). There is no explicit-id form: ids are builder-assigned
    in first-use order. A never-used handle emits nothing (no decl, no
    id, no statement, D5). This factory is the only documented path to
    deferred mode; the internal constructor flag stays undocumented.
    """
    warn_lazy_in_builder()
    return Var(init, explicit_mask=mask, storage=storage, deferred=True)


lazy_var = lazyVar


class ReadonlyVar:
    """Read-only runtime view of a `Var` for the cross-file owner convention (#96/#97).

    Exposes scalar reads (`.id`, `.init`, `.bitmask`, etc.), condition methods
    (.eq, .ne, .ge, .le, .gt, .lt, .at_least, .at_most, .exactly), pure
    expressions, and string/position arguments. It deliberately omits the raw
    `value`/`dest`/`mask`/`modifier` field refs and `copy()`: those carry
    mutation methods or return the mutable owner handle. Every mutation entry
    point raises `EUDSL-READONLY` with an actionable message.

    This is a runtime guard, not a security boundary: the owner's raw `Var` and
    the private `_var` attribute remain mutable by design.
    """

    def __init__(self, var: "Var | ReadonlyVar") -> None:
        if isinstance(var, ReadonlyVar):
            self._var: Var = var._var
        elif isinstance(var, Var):
            self._var = var
        else:
            raise TypeError(f"ReadonlyVar expects Var, got {type(var).__name__}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_var" and "_var" not in self.__dict__:
            super().__setattr__(name, value)
            return
        raise AttributeError(
            f"ReadonlyVar attributes are read-only: cannot set {name!r}. "
            "Export a mutator function from the owning module instead."
        )

    @property
    def id(self) -> int:
        return self._var.id

    @property
    def pageIndex(self) -> int:
        return self._var.pageIndex

    @property
    def page_index(self) -> int:
        return self._var.page_index

    @property
    def slotIndex(self) -> int:
        return self._var.slotIndex

    @property
    def slot_index(self) -> int:
        return self._var.slot_index

    @property
    def init(self) -> int:
        return self._var.init

    @property
    def initial_value(self) -> int:
        return self._var.initial_value

    @property
    def initialValue(self) -> int:
        return self._var.initialValue

    @property
    def bitmask(self) -> int:
        return self._var.bitmask

    # -------------------------------------------------------------------------
    # Comparisons and Conditions
    # -------------------------------------------------------------------------

    def eq(self, rhs: Any) -> Any:
        return self._var.eq(rhs)

    def ne(self, rhs: Any) -> Any:
        return self._var.ne(rhs)

    def ge(self, rhs: Any) -> Any:
        return self._var.ge(rhs)

    def le(self, rhs: Any) -> Any:
        return self._var.le(rhs)

    def gt(self, rhs: Any) -> Any:
        return self._var.gt(rhs)

    def lt(self, rhs: Any) -> Any:
        return self._var.lt(rhs)

    def at_least(self, rhs: Any) -> Any:
        return self._var.ge(rhs)

    atLeast = at_least

    def at_most(self, rhs: Any) -> Any:
        return self._var.le(rhs)

    atMost = at_most

    def exactly(self, rhs: Any) -> Any:
        return self._var.eq(rhs)

    def __bool__(self) -> bool:
        """Refuse build-time branching: a read-only view is not a Python bool."""
        raise _build_time_truthiness_error("value")

    def __eq__(self, rhs: Any) -> Any:  # type: ignore[override]
        return self._var.__eq__(rhs)

    def __ne__(self, rhs: Any) -> Any:  # type: ignore[override]
        return self._var.__ne__(rhs)

    def __lt__(self, rhs: Any) -> Any:
        return self._var.lt(rhs)

    def __le__(self, rhs: Any) -> Any:
        return self._var.le(rhs)

    def __gt__(self, rhs: Any) -> Any:
        return self._var.gt(rhs)

    def __ge__(self, rhs: Any) -> Any:
        return self._var.ge(rhs)

    # -------------------------------------------------------------------------
    # Pure Expressions
    # -------------------------------------------------------------------------

    # Build expressions with the read-only view itself as the operand. Using
    # `self._var` here would expose the owner's mutable handle through
    # `Expr.lhs`/`Expr.rhs`, reopening the mutation path this wrapper closes.

    def add(self, rhs: Any) -> Expr:
        return Expr("add", self, rhs)

    def sub(self, rhs: Any) -> Expr:
        return Expr("sub", self, rhs)

    def mul(self, rhs: Any) -> Expr:
        return Expr("mul", self, rhs)

    def div(self, rhs: Any) -> Expr:
        return Expr("div", self, rhs)

    def mod(self, rhs: Any) -> Expr:
        return Expr("mod", self, rhs)

    def divmod(self, rhs: Any) -> tuple["Var", "Var"]:
        """Return ``(self // rhs, self % rhs)`` from one restoring ladder."""
        return _eval_divmod(self, rhs)

    def bitAnd(self, rhs: Any) -> Expr:
        return Expr("bitand", self, rhs)

    bit_and = bitAnd
    and_ = bitAnd
    band = bitAnd

    def bitOr(self, rhs: Any) -> Expr:
        return Expr("bitor", self, rhs)

    bit_or = bitOr
    or_ = bitOr
    bor = bitOr

    def bitXor(self, rhs: Any) -> Expr:
        return Expr("bitxor", self, rhs)

    bit_xor = bitXor
    xor = bitXor
    bxor = bitXor

    def bitNot(self) -> Expr:
        return Expr("bitnot", self)

    bit_not = bitNot
    invert = bitNot
    bnot = bitNot

    def shl(self, rhs: Any) -> Expr:
        return Expr("shl", self, rhs)

    lshift = shl

    def shr(self, rhs: Any) -> Expr:
        return Expr("shr", self, rhs)

    rshift = shr

    def neg(self) -> Expr:
        return Expr("neg", self)

    def __add__(self, rhs: Any) -> Expr:
        return Expr("add", self, rhs)

    def __radd__(self, lhs: Any) -> Expr:
        return Expr("add", lhs, self)

    def __sub__(self, rhs: Any) -> Expr:
        return Expr("sub", self, rhs)

    def __rsub__(self, lhs: Any) -> Expr:
        return Expr("sub", lhs, self)

    def __mul__(self, rhs: Any) -> Expr:
        return Expr("mul", self, rhs)

    def __rmul__(self, lhs: Any) -> Expr:
        return Expr("mul", lhs, self)

    def __floordiv__(self, rhs: Any) -> Expr:
        return Expr("div", self, rhs)

    def __rfloordiv__(self, lhs: Any) -> Expr:
        return Expr("div", lhs, self)

    def __mod__(self, rhs: Any) -> Expr:
        return Expr("mod", self, rhs)

    def __rmod__(self, lhs: Any) -> Expr:
        return Expr("mod", lhs, self)

    def __divmod__(self, rhs: Any) -> tuple["Var", "Var"]:
        return self.divmod(rhs)

    def __rdivmod__(self, lhs: Any) -> tuple["Var", "Var"]:
        return _eval_divmod(lhs, self)

    def __and__(self, rhs: Any) -> Expr:
        return Expr("bitand", self, rhs)

    def __rand__(self, lhs: Any) -> Expr:
        return Expr("bitand", lhs, self)

    def __or__(self, rhs: Any) -> Expr:
        return Expr("bitor", self, rhs)

    def __ror__(self, lhs: Any) -> Expr:
        return Expr("bitor", lhs, self)

    def __xor__(self, rhs: Any) -> Expr:
        return Expr("bitxor", self, rhs)

    def __rxor__(self, lhs: Any) -> Expr:
        return Expr("bitxor", lhs, self)

    def __lshift__(self, rhs: Any) -> Expr:
        return Expr("shl", self, rhs)

    def __rlshift__(self, lhs: Any) -> Expr:
        return Expr("shl", lhs, self)

    def __rshift__(self, rhs: Any) -> Expr:
        return Expr("shr", self, rhs)

    def __rrshift__(self, lhs: Any) -> Expr:
        return Expr("shr", lhs, self)

    def __invert__(self) -> Expr:
        return Expr("bitnot", self)

    def __neg__(self) -> Expr:
        return Expr("neg", self)

    def __hash__(self) -> int:
        return hash(("ReadonlyVar", self._var))

    # -------------------------------------------------------------------------
    # Mutations explicitly disallowed
    # -------------------------------------------------------------------------

    def _raise_readonly(self, operation: str) -> None:
        raise EudslError(
            "EUDSL-READONLY",
            f"ReadonlyVar is read-only: {operation} cannot mutate it. "
            "Keep the mutable Var private to the owning module and export a "
            "mutator function (for example advance_wave()) instead.",
        )

    def set(self, rhs: Any) -> None:
        self._raise_readonly("set()")

    def iadd(self, rhs: Any) -> None:
        self._raise_readonly("iadd()")

    def isub(self, rhs: Any) -> None:
        self._raise_readonly("isub()")

    def imul(self, rhs: Any) -> None:
        self._raise_readonly("imul()")

    def idiv(self, rhs: Any) -> None:
        self._raise_readonly("idiv()")

    def imod(self, rhs: Any) -> None:
        self._raise_readonly("imod()")

    def iand(self, rhs: Any) -> None:
        self._raise_readonly("iand()")

    def ior(self, rhs: Any) -> None:
        self._raise_readonly("ior()")

    def ixor(self, rhs: Any) -> None:
        self._raise_readonly("ixor()")

    def ishl(self, rhs: Any) -> None:
        self._raise_readonly("ishl()")

    def ishr(self, rhs: Any) -> None:
        self._raise_readonly("ishr()")

    def act_set(self, rhs: Any) -> None:
        self._raise_readonly("act_set()")

    def act_add(self, rhs: Any) -> None:
        self._raise_readonly("act_add()")

    def act_sub(self, rhs: Any) -> None:
        self._raise_readonly("act_sub()")

    def __iadd__(self, rhs: Any) -> Any:
        self._raise_readonly("+=")

    def __isub__(self, rhs: Any) -> Any:
        self._raise_readonly("-=")

    def __imul__(self, rhs: Any) -> Any:
        self._raise_readonly("*=")

    def __itruediv__(self, rhs: Any) -> Any:
        self._raise_readonly("/=")

    def __ifloordiv__(self, rhs: Any) -> Any:
        self._raise_readonly("//=")

    def __imod__(self, rhs: Any) -> Any:
        self._raise_readonly("%=")

    def __iand__(self, rhs: Any) -> Any:
        self._raise_readonly("&=")

    def __ior__(self, rhs: Any) -> Any:
        self._raise_readonly("|=")

    def __ixor__(self, rhs: Any) -> Any:
        self._raise_readonly("^=")

    def __ilshift__(self, rhs: Any) -> Any:
        self._raise_readonly("<<=")

    def __irshift__(self, rhs: Any) -> Any:
        self._raise_readonly(">>=")


def as_readonly(v: Any) -> ReadonlyVar:
    """Wrap a `Var` or imported lazy handle in a runtime read-only view (#96/#97).

    The view preserves scalar reads, conditions, pure expressions, and format /
    position arguments while every mutation entry point raises `EUDSL-READONLY`.
    The owner keeps the mutable `Var` (or exports a mutator function); the view
    exposes no `value`/`dest`/`mask`/`modifier` field refs and no `copy()`.
    """
    if isinstance(v, ReadonlyVar):
        return v
    if isinstance(v, Var):
        return ReadonlyVar(v)
    raise TypeError(f"as_readonly expects Var or ReadonlyVar, got {type(v).__name__}")


asReadonly = as_readonly

