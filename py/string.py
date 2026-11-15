"""Fixed-capacity, STR-backed ``FixedString`` text buffer.

``FixedString(size)`` reserves a marker entry in the module string table and
allocates a page-pinned scalar holding its late-bound byte address. ``size`` is
the maximum content length: content occupies offsets ``0..size-1``, and exactly
one byte at offset ``size`` is reserved for the NUL terminator. Writes that
cannot fit fail at build time with ``EUDSL-BOUNDS``; a runtime ``writeFrom``
position is clamped to ``size`` and every content byte is guarded at runtime.
Writes are dword-only: literal runs pack into constant ``SetDeaths`` dwords,
dynamic bytes patch a scratch pointer and select the byte lane at runtime.

``write`` always starts at byte offset ``0``; ``append`` continues from
``lastpos``; ``writeFrom`` starts at an explicit constant or runtime offset.
Static runs defer their trailing NUL so adjacent writes pack into dwords; the
terminator is emitted at display/print, scope boundaries, and module end.
Dynamic writes still terminate immediately. The terminator never advances
``lastpos`` past the content.

Scope boundaries align partial tails to the next dword with ``0x1A`` padding
(zero-width in SC:R, verified by the committed smoke map). Alignment at a
scope boundary is never a per-append decision: it lets the following dynamic
bytes bake their byte lane into the emitted mask instead of selecting it with
a four-way runtime branch. Padding is used only when the padded tail plus its
NUL still fits in the declared capacity; otherwise the boundary falls back to
the unaligned lane. A runtime ``writeFrom`` position makes the lane unknowable,
so that string also stays unaligned until the next absolute write.

Fixed-width hex placeholders fold directly at their compile-time byte lane,
with mask-limited first/last dword writes when the lane is non-zero, so a
static run needs no mid-string padding, no runtime position shift, and no
action-field patching. Fixed-width decimal placeholders use the same direct
strategy: the static target writes each digit byte into its compile-time
dword with a byte-masked ``SetStringDword`` action, so the fixed-width
decimal path has no scratch trigger, no ``emitVarStore`` field patching, and
no runtime ``pos.shr(2)``. The shared format-run layout walk charges
literals, fixed-width hex, ``spec.width`` per fixed-width decimal
placeholder, and the widest variable-width decimal output; a run that would
overrun ``size`` fails with ``EUDSL-BOUNDS`` before emitting a write.

Format strings use bare ``{}`` / ``{:d}`` for variable-width decimal and
``{:x}`` / ``{:08x}`` for 8-digit lowercase hex. Explicit widths render
fixed-width decimal (for example ``{:04d}``). ``{{`` / ``}}`` escape literal
braces. Format mistakes are loud ``EUDSL-FORMAT`` errors at build time (ADR
0014).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, NamedTuple
from weakref import WeakKeyDictionary
from typing import Callable

from .cp import CP_ADDR
from .lazy_binding import LazyBinding
from .hir import (
    Add,
    AddrReloc,
    AtLeast,
    Deaths,
    DeathsX,
    DisplayTextIndex,
    Exactly,
    Memory,
    MemoryX,
    SetDeaths,
    SetDeathsX,
    SetMemory,
    SetStringDword,
    SetTo,
    Subtract,
    TriggerFieldReloc,
    emitVarStore,
)
from .temporal import when
from .trigger import Trigger, _get_active_module_builder, _register_trigger
from .var import (
    Expr,
    ReadonlyVar,
    Var,
    _alloc_string,
    _alloc_string_var,
    _get_next_string_index,
    _get_scope_serial,
    to_var,
)
from .view import (
    CURRENT_PLAYER,
    EPD_CURRENT_PLAYER,
    EudslError,
    EudslWarning,
    _describe_value as _describe_shared,
    _is_plain_int,
    emit_warning,
    emitWarning,
    on_warning,
    onWarning,
    warn_lazy_in_builder,
)

WarningListener = Callable[[str, str], None]

MAX_SIZE = 65535
DISPLAY_LINE_ADDR = 0x640B58
DWORD_BYTES = 4
TERMINATOR_BYTES = 1
HEX_CONTENT_BYTES = 8

# Zero-width alignment pad byte, verified in StarCraft: Remastered.
STRING_PAD_BYTE = 0x1A

# Sentinel for "use the string's tracked lane" in _append_dynamic_byte, so an
# explicit ``None`` can mean "lane unknowable".
_USE_FIELD = object()


def _describe(value: object) -> str:
    """Render an operand exactly as ``FixedString`` always has.

    Strings use ``json.dumps`` here (the pre-dedupe rendering); every other
    operand delegates to the canonical ``view`` describer. FE0 folded this
    helper into ``view._describe_value``, whose string form is ``repr``, and
    silently changed every format/position error message.
    """
    if isinstance(value, str):
        return json.dumps(value)
    return _describe_shared(value)


def _is_ascii_digit(c: str) -> bool:
    """Return whether ``c`` is an ASCII 0-9 digit.

    This mirrors the character comparisons in ``ts/string.ts``. ``str.isdigit``
    must not be used here: it accepts Unicode digits (for example ``٤``) that
    the TypeScript parser rejects.
    """
    return "0" <= c <= "9"


def encode_utf8(text: str, what: str) -> list[int]:
    """Encode literal content as canonical UTF-8 bytes (RFC 3629).

    ``FixedString(size)`` capacity is a byte capacity (ADR 0003), so string
    content is lowered to its UTF-8 bytes here. Python iterates code points,
    so a valid astral character is one scalar value and emits four bytes.

    An embedded NUL would terminate the ``STRx`` entry, and a lone UTF-16
    surrogate (``U+D800..U+DFFF``) is never valid UTF-8; both are rejected
    with ``EUDSL-TYPE`` before any bytes are emitted.
    """
    out: list[int] = []
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            raise EudslError(
                "EUDSL-TYPE",
                f"{what}: embedded NUL (U+0000) cannot be stored in a FixedString",
            )
        if 0xD800 <= cp <= 0xDFFF:
            raise EudslError(
                "EUDSL-TYPE",
                f"{what}: lone UTF-16 surrogate U+{cp:04X} is not valid UTF-8",
            )
        if cp <= 0x7F:
            out.append(cp)
        elif cp <= 0x7FF:
            out.append(0xC0 | (cp >> 6))
            out.append(0x80 | (cp & 0x3F))
        elif cp <= 0xFFFF:
            out.append(0xE0 | (cp >> 12))
            out.append(0x80 | ((cp >> 6) & 0x3F))
            out.append(0x80 | (cp & 0x3F))
        else:
            # Python caps ``ord`` at U+10FFFF, so no range check is needed.
            out.append(0xF0 | (cp >> 18))
            out.append(0x80 | ((cp >> 12) & 0x3F))
            out.append(0x80 | ((cp >> 6) & 0x3F))
            out.append(0x80 | (cp & 0x3F))
    return out


def _encode_ascii_byte(ch: str, what: str) -> int:
    """Encode one ASCII byte for the format-spec ``fill`` (ADR 0014).

    Fill is repeated as a single byte, so a multi-byte character cannot be a
    fill; literal content uses :func:`encode_utf8` instead.
    """
    cp = ord(ch)
    if cp > 0x7F:
        raise EudslError(
            "EUDSL-ASCII",
            f"{what}: format fill must be a single ASCII byte; got U+{cp:04X} in {ch!r}",
        )
    return cp


def _hex_bytes(value: int) -> list[int]:
    u = value & 0xFFFFFFFF
    out: list[int] = []
    for i in range(7, -1, -1):
        nib = (u >> (4 * i)) & 0xF
        out.append(0x30 + nib if nib < 10 else 0x61 + (nib - 10))
    return out


@dataclass(frozen=True)
class FormatSpec:
    fill: str | None = None
    align: str | None = None
    width: int | None = None
    radix: str = "dec"


class ExpandedArg(NamedTuple):
    kind: str
    bytes: list[int] | None = None
    spec: FormatSpec = FormatSpec()
    value: Var | Expr | None = None


class HexByteGeometry(NamedTuple):
    dword_delta: int
    byte_in_dword: int
    byte_mask: int


def _hex_byte_geometry(lane: int, char_idx: int) -> HexByteGeometry:
    dword_delta = (lane + char_idx) // DWORD_BYTES
    byte_in_dword = (lane + char_idx) % DWORD_BYTES
    byte_mask = (0xFF << (8 * byte_in_dword)) & 0xFFFFFFFF
    return HexByteGeometry(dword_delta, byte_in_dword, byte_mask)


class _HexBit(NamedTuple):
    bit: int
    nib_weight: int
    char_index: int


def _accumulate_hex_nibbles(
    value: Var,
    digit_action: Callable[[_HexBit], dict[str, Any]] | None = None,
) -> list[Var]:
    """Create the eight nibble accumulators and the 32 value-bit triggers
    shared by the direct-dword and dynamic-byte hex paths.

    ``digit_action``, when supplied, is emitted before the nibble update in
    the same bit-test trigger. The direct path uses it to add the ASCII digit
    weight straight to the target string dword.
    """
    nibbles: list[Var] = []
    for _ in range(HEX_CONTENT_BYTES):
        nib = Var(0)
        nib.set(0)
        nibbles.append(nib)

    for b in range(32):
        nib_idx = b // 4
        nib_weight = (1 << (b % 4)) & 0xFFFFFFFF
        char_index = HEX_CONTENT_BYTES - 1 - nib_idx
        acts: list[dict[str, Any]] = []
        if digit_action is not None:
            acts.append(digit_action(_HexBit(b, nib_weight, char_index)))
        acts.append(
            SetDeathsX(nibbles[nib_idx].value, Add, nib_weight, 0, 0xFFFFFFFF)
        )
        Trigger(
            conds=[DeathsX(value.value, AtLeast, 1, 0, (1 << b) & 0xFFFFFFFF)],
            acts=acts,
        )
    return nibbles


def _emit_packed_hex(
    index: int, value: Var, dword_offset: int, lane: int
) -> None:
    """Write eight lowercase hex digits at a compile-time dword/lane.

    The destination is a late-bound ``target_string`` reloc, so lowering
    resolves the string address itself: the fixed path needs no runtime
    ``pos.shr(2)`` and no scratch-trigger ``dest``/``value`` patching. Each
    bit-test trigger adds the ASCII digit weight straight to its target
    byte; the nibble variables exist only to decide the ``>= 10`` lowercase
    adjustment.

    With ``lane != 0`` the eight bytes span three dwords. The first and
    last writes are mask-limited to the bytes the placeholder owns, so
    adjacent content keeps its bytes.
    """
    last_dword_delta = (lane + HEX_CONTENT_BYTES - 1) // DWORD_BYTES
    for delta in range(last_dword_delta + 1):
        mask = 0
        for byte in range(DWORD_BYTES):
            char_idx = delta * DWORD_BYTES + byte - lane
            if 0 <= char_idx < HEX_CONTENT_BYTES:
                mask |= 0xFF << (8 * byte)
        SetStringDword(
            index,
            (dword_offset + delta) * DWORD_BYTES,
            0x30303030,
            mask & 0xFFFFFFFF,
        )

    def _digit_action(bit: _HexBit) -> dict[str, Any]:
        geom = _hex_byte_geometry(lane, bit.char_index)
        digit_weight = (bit.nib_weight << (8 * geom.byte_in_dword)) & 0xFFFFFFFF
        return SetStringDword(
            index,
            (dword_offset + geom.dword_delta) * DWORD_BYTES,
            digit_weight,
            geom.byte_mask,
            Add,
        )

    nibbles = _accumulate_hex_nibbles(value, _digit_action)

    for char_idx in range(HEX_CONTENT_BYTES):
        nib_idx = HEX_CONTENT_BYTES - 1 - char_idx
        geom = _hex_byte_geometry(lane, char_idx)
        adjust = (0x27 << (8 * geom.byte_in_dword)) & 0xFFFFFFFF
        nib = nibbles[nib_idx]

        def _adjust(
            dw_offset=(dword_offset + geom.dword_delta) * DWORD_BYTES,
            adj=adjust,
            mask=geom.byte_mask,
        ):
            SetStringDword(index, dw_offset, adj, mask, Add)

        when(nib.ge(10), _adjust)


def _dec_bytes(
    value: int, width: int | None = None, fill: str | None = None
) -> list[int]:
    value &= 0xFFFFFFFF
    if width is not None and width < 10:
        value %= 10**width
    s = str(value)
    if width is not None and len(s) < width:
        pad = fill if fill is not None else " "
        s = pad * (width - len(s)) + s
    return encode_utf8(s, "FixedString {:d}")


def _has_placeholder(text: str) -> bool:
    i = 0
    while i < len(text):
        c = text[i]
        if c == "{":
            if text[i + 1 : i + 2] == "{":
                i += 2
            else:
                return True
        elif c == "}":
            if text[i + 1 : i + 2] == "}":
                i += 2
            else:
                return True
        else:
            i += 1
    return False


def _parse_spec(spec: str, what: str) -> FormatSpec:
    if spec == "":
        return FormatSpec(fill=None, align=None, width=None, radix="dec")

    fill: str | None = None
    align: str | None = None
    rem = spec

    def is_align(c: str) -> bool:
        return c in (">", "<", "^", "=")

    if len(spec) >= 2 and is_align(spec[1]):
        a = spec[1]
        if a == "<":
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: left alignment '<' is not supported in {spec!r}",
            )
        if a != ">":
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: alignment {a!r} is not supported in {spec!r}",
            )
        _encode_ascii_byte(spec[0], what)
        fill = spec[0]
        align = ">"
        rem = spec[2:]
    elif is_align(spec[0]):
        a = spec[0]
        if a == "<":
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: left alignment '<' is not supported in {spec!r}",
            )
        if a != ">":
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: alignment {a!r} is not supported in {spec!r}",
            )
        fill = " "
        align = ">"
        rem = spec[1:]
    else:
        for c in spec:
            if is_align(c):
                raise EudslError(
                    "EUDSL-FORMAT",
                    f"{what}: malformed format string: misplaced alignment {c!r} in {spec!r}",
                )

    if align is None:
        if rem.startswith("0"):
            fill = "0"
            align = ">"
            rem = rem[1:]
        elif rem and _is_ascii_digit(rem[0]):
            fill = " "
            align = ">"

    if align is not None:
        if not rem or not _is_ascii_digit(rem[0]) or rem[0] == "0":
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: missing positive width after align/fill in {spec!r}",
            )

    width: int | None = None
    if rem and _is_ascii_digit(rem[0]):
        digits = []
        for c in rem:
            if _is_ascii_digit(c):
                digits.append(c)
            else:
                break
        digit_str = "".join(digits)
        rem = rem[len(digit_str) :]
        if digit_str.startswith("0"):
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: invalid width {digit_str!r} in {spec!r}",
            )
        width = int(digit_str)
        if not (1 <= width <= 10):
            raise EudslError(
                "EUDSL-FORMAT",
                f"{what}: malformed format string: width must be between 1 and 10, got {width} in {spec!r}",
            )

    radix: str
    if rem in ("", "d"):
        radix = "dec"
    elif rem == "x":
        radix = "hex"
    else:
        raise EudslError(
            "EUDSL-FORMAT",
            f"{what}: malformed format string: unknown format spec type {rem!r} in {spec!r}",
        )

    if radix == "hex" and width is not None and width != 8:
        raise EudslError(
            "EUDSL-FORMAT",
            f"{what}: malformed format string: hex format spec requires width 8, got {width} in {spec!r}",
        )

    return FormatSpec(fill=fill, align=align, width=width, radix=radix)


def _parse_format(
    fmt: str, what: str
) -> tuple[list[tuple[str, str | FormatSpec]], int]:
    """Parse a ``FixedString`` format string into parts and a placeholder count.

    Returns ``(parts, placeholders)``. Each part is either ``("lit", text)`` or
    ``("arg", FormatSpec)``. The TypeScript ``parseFormat`` returns
    ``{ parts, placeholders }`` with ``{ kind: "lit", text }`` /
    ``{ kind: "arg", spec, radix }`` objects instead. The container difference
    is intentional: Python uses its idiomatic tuple return while TypeScript
    uses an object. ``FormatSpec`` fields and parsing semantics are identical
    in both frontends. The public aliases ``parse_format`` / ``parseFormat``
    return this same Python shape.
    """
    parts: list[tuple[str, str | FormatSpec]] = []
    placeholders = 0
    lit: list[str] = []

    def flush() -> None:
        if lit:
            parts.append(("lit", "".join(lit)))
            lit.clear()

    i = 0
    while i < len(fmt):
        c = fmt[i]
        if c == "{":
            nxt = fmt[i + 1] if i + 1 < len(fmt) else ""
            if nxt == "{":
                lit.append("{")
                i += 2
            elif nxt == "}":
                flush()
                parts.append(("arg", FormatSpec(fill=None, align=None, width=None, radix="dec")))
                placeholders += 1
                i += 2
            elif nxt == ":":
                close = fmt.find("}", i + 2)
                if close < 0:
                    raise EudslError(
                        "EUDSL-FORMAT",
                        f"{what}: malformed format string {fmt!r}: unterminated "
                        "format spec (use '{:x}', '{:d}', or '{{' for a literal brace)",
                    )
                spec = fmt[i + 2 : close]
                parsed_spec = _parse_spec(spec, what)
                flush()
                parts.append(("arg", parsed_spec))
                placeholders += 1
                i = close + 1
            else:
                raise EudslError(
                    "EUDSL-FORMAT",
                    f"{what}: malformed format string {fmt!r}: lone '{{' "
                    "(use '{{' for a literal brace)",
                )
        elif c == "}":
            nxt = fmt[i + 1] if i + 1 < len(fmt) else ""
            if nxt == "}":
                lit.append("}")
                i += 2
            else:
                raise EudslError(
                    "EUDSL-FORMAT",
                    f"{what}: malformed format string {fmt!r}: lone '}}' "
                    "(use '}}' for a literal brace)",
                )
        else:
            lit.append(c)
            i += 1
    flush()

    bare_decimal_count = sum(
        1
        for kind, spec in parts
        if kind == "arg"
        and isinstance(spec, FormatSpec)
        and spec.radix == "dec"
        and spec.width is None
    )
    if bare_decimal_count >= 2:
        emit_warning(
            "EUDSL-WARN-PERF",
            f"{what}: format string interpolates 2 or more bare decimal placeholders; "
            "fixed-width decimal specs (e.g. '{:\\x1a>4d}') reduce static trigger overhead from ~688T to ~51T static per variable",
        )

    return parts, placeholders


parse_format = _parse_format
parseFormat = _parse_format


ContentArg = str | int | Var | Expr | ReadonlyVar


def _is_runtime(value: object) -> bool:
    return isinstance(value, (Var, Expr, ReadonlyVar))


def _materialize(value: Var | Expr | ReadonlyVar) -> Var:
    """Materialize a string operand; the domain is `Var | Expr | ReadonlyVar`.

    The runtime guards (`_is_runtime` at the content/position edges) narrow
    first; this delegates the shared "constant or handle -> Var" step to the
    canonical kernel in `var`.
    """
    return to_var(value)


DECIMAL_POWERS = (
    1_000_000_000,
    100_000_000,
    10_000_000,
    1_000_000,
    100_000,
    10_000,
    1_000,
    100,
    10,
    1,
)
DECIMAL_CONTENT_BYTES = len(DECIMAL_POWERS)


# Shared variable-width decimal kernel, one body per build and capacity.
#
# Inline, a bare `{}` is ~688T of static triggers (10 digit ladders plus the
# conditional byte appends). Declaring the loop once and calling it makes K
# sites cost O(K + M) static, with each extra site paying only the argument
# transfers and the call/result sequence.
#
# The body is emitted against parameter variables for the position and buffer
# base, and it returns the advanced position. The capacity stays a compile-time
# constant, so one body serves every `{}` site on strings of the same size and
# the per-digit `pos < size` guard folds to a direct condition (a runtime
# `limit` would force a var/var comparison: two page-firing transfers per
# digit, which regressed executed cost). Distinct sizes get distinct bodies.
_DECIMAL_KERNELS: "WeakKeyDictionary[Any, dict[int, Any]]" = WeakKeyDictionary()


def _shared_decimal_kernel(string: "FixedString") -> Any:
    """Return this build's shared decimal writer, declaring it on first use."""
    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE", "a variable-width decimal write must be inside buildModule()"
        )
    kernels = _DECIMAL_KERNELS.get(builder)
    if kernels is None:
        kernels = {}
        _DECIMAL_KERNELS[builder] = kernels
    kernel = kernels.get(string.size)
    if kernel is not None:
        return kernel

    kernel = string._declare_decimal_kernel()
    kernels[string.size] = kernel
    return kernel


class _RunLayout(NamedTuple):
    end: int


def _layout_run(
    expanded: list[ExpandedArg],
    start: int,
    static_known: bool,
) -> _RunLayout:
    """Shared layout walk for one format run.

    ``_emit_args`` uses the emission loop's own item order and
    ``_check_capacity`` consumes ``end`` for the capacity bound, so the
    checked bound cannot drift from what the run writes. Fixed-width decimal
    placeholders are charged by their declared ``spec.width``; variable-width
    decimal charges the widest output it can produce.
    """
    pos = start
    static_pos_known = static_known
    for item in expanded:
        if item.kind == "lit":
            pos += len(item.bytes or [])
            continue
        if item.spec.radix == "hex" and static_pos_known:
            # Fixed-width hex folds at its compile-time lane, so every
            # following literal and placeholder in this run stays statically
            # knowable.
            pos += HEX_CONTENT_BYTES
            continue
        if (
            item.spec.radix == "dec"
            and item.spec.width is not None
            and static_pos_known
        ):
            # Fixed-width decimal writes each column directly at its
            # compile-time lane, so later items keep their static offsets too.
            pos += item.spec.width
            continue
        pos += (
            HEX_CONTENT_BYTES
            if item.spec.radix == "hex"
            else (
                item.spec.width
                if item.spec.width is not None
                else DECIMAL_CONTENT_BYTES
            )
        )
        static_pos_known = False
    return _RunLayout(pos)


def _check_fixed_string_size(size: int) -> None:
    if not _is_plain_int(size) or size <= 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"FixedString(): size must be a positive integer (bytes), got {_describe(size)}",
        )
    if size > MAX_SIZE:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"FixedString(): size {size} exceeds the build-time cap {MAX_SIZE}",
        )


def _shift_byte(value: int, lane: int | None) -> int:
    if lane is None:
        return value & 0xFF
    return ((value & 0xFF) << (8 * lane)) & 0xFFFFFFFF


class _FixedDecimalTarget:
    """Destination strategy for the fixed-width decimal digit loop.

    ``begin`` creates the per-digit accumulator (or writes its initial byte),
    ``add``/``assign`` mutate it, and ``end`` commits a dynamically appended
    byte. The static target owns a compile-time dword/lane and writes masked
    ``SetStringDword`` actions straight into the string buffer; the dynamic
    target keeps a scratch ``Var`` for the runtime-position fallback.
    """

    def lane_for(self, k: int) -> int | None:
        raise NotImplementedError

    def begin(self, k: int, lane: int | None, base: int) -> Var | None:
        raise NotImplementedError

    def add(
        self, handle: Var | None, k: int, lane: int | None, amount: int
    ) -> dict[str, Any]:
        raise NotImplementedError

    def end(self, handle: Var | None, k: int, lane: int | None) -> None:
        raise NotImplementedError


class _StaticDecimalTarget(_FixedDecimalTarget):
    """Write digit bytes at their compile-time dword and byte lane."""

    def __init__(self, string: FixedString, start_pos: int) -> None:
        self._string = string
        self._start_pos = start_pos

    def lane_for(self, k: int) -> int:
        return (self._start_pos + k) & 3

    def _write(self, k: int, lane: int, value: int, modifier: int) -> dict[str, Any]:
        dword_offset = (self._start_pos + k) >> 2
        mask = (0xFF << (8 * lane)) & 0xFFFFFFFF
        return SetStringDword(
            self._string._index,
            dword_offset * DWORD_BYTES,
            _shift_byte(value, lane),
            mask,
            modifier,
        )

    def begin(self, k: int, lane: int | None, base: int) -> None:
        assert lane is not None
        self._write(k, lane, base, SetTo)
        return None

    def add(
        self, handle: Var | None, k: int, lane: int | None, amount: int
    ) -> dict[str, Any]:
        assert lane is not None
        return self._write(k, lane, amount, Add)

    def end(self, handle: Var | None, k: int, lane: int | None) -> None:
        return None


class _DynamicDecimalTarget(_FixedDecimalTarget):
    """Accumulate a digit in a scratch ``Var`` and append it at runtime."""

    def __init__(self, string: FixedString) -> None:
        self._string = string

    def lane_for(self, k: int) -> None:
        return None

    def begin(self, k: int, lane: int | None, base: int) -> Var:
        digit = Var(0)
        digit.set(_shift_byte(base, lane))
        return digit

    def add(
        self, handle: Var | None, k: int, lane: int | None, amount: int
    ) -> dict[str, Any]:
        assert handle is not None
        return SetDeaths(handle.value, Add, _shift_byte(amount, lane), 0)

    def end(self, handle: Var | None, k: int, lane: int | None) -> None:
        assert handle is not None
        self._string._append_dynamic_byte(handle, True, None)


class FixedString:
    """Fixed-capacity text buffer backed by the StarCraft string table."""

    def __init__(self, size: int) -> None:
        _check_fixed_string_size(size)
        self.size = size
        builder = _get_active_module_builder()
        if builder is None or not hasattr(builder, "alloc_string"):
            raise EudslError(
                "EUDSL-SCOPE", "FixedString() must be constructed inside buildModule()"
            )
        tag = _get_next_string_index()
        dummy = " " * (size + TERMINATOR_BYTES) + f"\x7fEUDString#{tag}\x7f"
        self._index = _alloc_string(dummy)
        self._base: Var | None = None
        self._pos_var: Var | None = None
        self._line_save: Var | None = None
        self._pending: list[int] = []
        self._static_pos: int | None = 0
        # Conservative next-content offset when the runtime position is known
        # at build time; None once a runtime writeFrom makes it unknowable.
        self._bound_pos: int | None = 0
        # Byte lane of the next content position when it is provable at build
        # time. None means a runtime write or a conditional tail made the lane
        # unknowable.
        self._aligned_lane: int | None = 0
        self._flushing_boundary = False
        self._static_serial = _get_scope_serial()
        self._needs_terminator = False
        self._emitting = False
        # CP cursor (issue #87): True while the CurrentPlayer cursor holds
        # `base + (pos >> 2)` for the byte writer.
        self._cp_valid = False
        if hasattr(builder, "register_flusher"):
            builder.register_flusher(self._flush_at_scope_boundary)

    @property
    def message_index(self) -> int:
        return self._index

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, *args: ContentArg) -> None:
        self._start_at(0)
        self._emitting = True
        try:
            self._emit_args(args, "write()")
        finally:
            self._emitting = False
        self._finish()

    def append(self, *args: ContentArg) -> None:
        if self._static_pos is None or _get_scope_serial() != self._static_serial:
            self._start_dynamic()
        self._emitting = True
        try:
            self._emit_args(args, "append()")
        finally:
            self._emitting = False
        self._finish()

    def writeFrom(self, pos: int | Var | Expr | ReadonlyVar, *args: ContentArg) -> None:
        if _is_plain_int(pos):
            if pos < 0 or pos > self.size:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"FixedString.writeFrom(): pos must be an integer in 0..{self.size}, got {pos}",
                )
            self._flush_tail()
            self._start_at(pos)
        elif _is_runtime(pos):
            self._start_dynamic(_materialize(pos))
        else:
            raise EudslError(
                "EUDSL-TYPE",
                "FixedString.writeFrom(): pos must be an integer or game-time value, "
                f"got {_describe(pos)}",
            )
        self._emitting = True
        try:
            self._emit_args(args, "writeFrom()")
        finally:
            self._emitting = False
        self._finish()

    write_from = writeFrom

    def print(self, *args: ContentArg) -> None:
        if args:
            self.write(*args)
        self.display()

    def printAt(self, line: int | Var | Expr | ReadonlyVar, *args: ContentArg) -> None:
        if args:
            self.write(*args)
        self.displayAt(line)

    print_at = printAt

    def display(self) -> None:
        self._flush_tail()
        DisplayTextIndex(self._index)

    def displayAt(self, line: int | Var | Expr | ReadonlyVar) -> None:
        self._flush_tail()
        if _is_plain_int(line):
            if not 0 <= line <= 10:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"FixedString.displayAt(): line must be 0-10, got {line}",
                )
        elif not _is_runtime(line):
            raise EudslError(
                "EUDSL-TYPE",
                "FixedString.displayAt(): line must be an integer 0-10 or a game-time "
                f"value, got {_describe(line)}",
            )

        # eudplib semantics:
        # 1. Read current 0x640b58 (bits 8, 4, 2, 1) into saved
        # 2. Add line to 0x640b58 (modulo 11)
        # 3. DisplayText
        # 4. Restore 0x640b58 = saved
        saved = self._ensure_line_save()
        saved.set(0)
        for bit in (8, 4, 2, 1):
            when(
                MemoryX(DISPLAY_LINE_ADDR, AtLeast, 1, bit),
                lambda bit=bit: saved.iadd(bit),
            )

        if _is_plain_int(line):
            if line != 0:
                SetMemory(DISPLAY_LINE_ADDR, Add, line)
                when(
                    Memory(DISPLAY_LINE_ADDR, AtLeast, 11),
                    lambda: SetMemory(DISPLAY_LINE_ADDR, Subtract, 11),
                )
        else:
            emitVarStore(_materialize(line).id, AddrReloc(DISPLAY_LINE_ADDR), Add)
            when(
                Memory(DISPLAY_LINE_ADDR, AtLeast, 11),
                lambda: SetMemory(DISPLAY_LINE_ADDR, Subtract, 11),
            )

        self.display()
        emitVarStore(saved.id, AddrReloc(DISPLAY_LINE_ADDR), SetTo)

    display_at = displayAt

    def clear(self) -> None:
        self._pending.clear()
        self._needs_terminator = False
        clear_bytes = min(self.size + TERMINATOR_BYTES, DWORD_BYTES)
        mask = (0xFFFFFFFF >> (8 * (DWORD_BYTES - clear_bytes))) & 0xFFFFFFFF
        SetStringDword(self._index, 0, 0, mask)
        self._static_pos = 0
        self._bound_pos = 0
        self._aligned_lane = 0
        self._cp_valid = False
        self._static_serial = _get_scope_serial()
        if self._pos_var is not None:
            self._pos_var.set(0)

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def _start_at(self, pos: int) -> None:
        self._pending.clear()
        self._needs_terminator = False
        self._static_pos = pos
        self._bound_pos = pos
        self._aligned_lane = pos & 3
        self._static_serial = _get_scope_serial()
        self._cp_valid = False
        if self._pos_var is not None:
            self._pos_var.set(pos)

    def _start_dynamic(self, pos: Var | None = None) -> None:
        self._flush_tail()
        # `_start_dynamic` always writes `target` itself below, so it must not
        # install the static position twice when the var is created here.
        target = self._ensure_pos(False)
        if pos is not None:
            if pos.id != target.id:
                target.set(pos)
            # A runtime position cannot be proven at build time. Clamp it once,
            # then guard every content byte against `size` while it is emitted.
            # Its lane is unknown too, so keep the four-way lane selection.
            # Clear the static position *before* the clamp's scope flush so
            # that flush cannot mark the string as statically aligned again.
            self._bound_pos = None
            self._aligned_lane = None
            self._static_pos = None
            when(target.gt(self.size), lambda: target.set(self.size))
        elif self._static_pos is not None:
            # `append()` after a scope boundary continues from the content end
            # written so far. The position variable survives across trigger
            # loops, so re-install it at runtime every frame; a one-time initial
            # value would let the writes walk past the terminator on the second
            # frame.
            target.set(self._static_pos)
        self._static_pos = None
        # Every branch above may rewrite the runtime position var.
        self._cp_valid = False
        self._static_serial = _get_scope_serial()

    def _ensure_base(self) -> Var:
        if self._base is None:
            slot = _alloc_string_var(self._index, 0)
            self._base = Var(0, explicit_id=slot, storage="page")
        return self._base

    def _ensure_pos(self, install: bool = True) -> Var:
        if self._pos_var is None:
            if self._static_pos is None:
                raise EudslError(
                    "EUDSL-SCOPE",
                    "FixedString internal error: runtime position requested without "
                    "a known position",
                )
            self._pos_var = Var(self._static_pos)
            if install:
                # The constructor initializer runs once at module load.
                # Re-install the content end at this emission point so a
                # dynamic run that created the position var still starts from
                # `lastpos` on every trigger loop.
                self._pos_var.set(self._static_pos)
        return self._pos_var

    def _ensure_line_save(self) -> Var:
        if self._line_save is None:
            self._line_save = Var(0)
        return self._line_save

    def _finish(self) -> None:
        self._static_serial = _get_scope_serial()
        if self._static_pos is not None:
            self._needs_terminator = True
            self._aligned_lane = (self._static_pos + len(self._pending)) & 3
            if self._pos_var is not None:
                self._pos_var.set(self._static_pos + len(self._pending))
        else:
            self._append_dynamic_byte(Var(0), advance=False, const_byte=0)
        if self._cp_valid:
            # CP cursor (issue #87): leave CurrentPlayer at the payload resting
            # value 0 (ADR 0004 resets curpl once per loop; py/camera.py
            # restores 0).
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
            self._cp_valid = False

    def _flush_at_scope_boundary(self, align: bool = True) -> None:
        if self._emitting:
            self._flush_content()
            return
        if not align:
            self._flush_tail()
            return
        if self._static_pos is None:
            self._flush_dynamic_boundary()
            return

        end = self._static_pos + len(self._pending)
        pad = (DWORD_BYTES - (end % DWORD_BYTES)) % DWORD_BYTES
        fits = end + pad <= self.size

        # Nothing deferred: only rewrite the terminator when the lane needs
        # normalizing, and never touch a tail this boundary cannot fit.
        if not self._pending and not self._needs_terminator:
            if (end & 3) == 0:
                self._aligned_lane = 0
                return
            if not fits:
                self._aligned_lane = None
                return
        elif not fits:
            # Keep the content inside this scope, but give up on alignment.
            self._flush_tail()
            self._aligned_lane = None
            return

        self._pending.extend([STRING_PAD_BYTE] * pad)
        self._pending.append(0)
        self._flush_pending(True)
        self._static_pos = end + pad
        # Padding consumes content offsets, so the capacity bound moves with it.
        if self._bound_pos is not None:
            self._bound_pos = max(self._bound_pos, self._static_pos)
        self._needs_terminator = False
        self._aligned_lane = 0
        if self._pos_var is not None:
            self._pos_var.set(self._static_pos)

    def _flush_dynamic_boundary(self) -> None:
        if self._flushing_boundary:
            return
        lane = self._aligned_lane
        if lane is None or lane == 0:
            return
        pad = (DWORD_BYTES - lane) % DWORD_BYTES
        if self._bound_pos is None or self._bound_pos + pad > self.size:
            self._aligned_lane = None
            return
        self._flushing_boundary = True
        try:
            for i in range(pad):
                self._append_dynamic_byte(
                    Var(STRING_PAD_BYTE), True, (lane + i) & 3
                )
            self._append_dynamic_byte(Var(0), False, 0)
            self._aligned_lane = 0
        finally:
            self._flushing_boundary = False

    def _next_content_lane(self) -> int | None:
        """Lane of the next content byte, including deferred static bytes."""
        if self._static_pos is not None:
            return (self._static_pos + len(self._pending)) & 3
        return self._aligned_lane

    def _flush_content(self) -> None:
        if self._static_pos is not None:
            self._flush_pending(True)
            self._aligned_lane = self._static_pos & 3

    def _flush_tail(self) -> None:
        if self._static_pos is None:
            return
        if not self._pending and not self._needs_terminator:
            self._aligned_lane = self._static_pos & 3
            return
        # Display and module-end tails stay unaligned: no run follows them
        # that could benefit from padding.
        if self._needs_terminator:
            end = self._static_pos + len(self._pending)
            self._pending.append(0)
            self._flush_pending(True)
            self._static_pos = end
            self._needs_terminator = False
        else:
            self._flush_pending(True)
        self._aligned_lane = self._static_pos & 3

    # ------------------------------------------------------------------
    # Argument expansion and rendering
    # ------------------------------------------------------------------

    def _emit_args(self, args: tuple[ContentArg, ...], what: str) -> None:
        expanded: list[ExpandedArg] = []
        first = args[0] if args else None
        if isinstance(first, str) and _has_placeholder(first):
            parts, placeholders = _parse_format(first, f"FixedString.{what}")
            values = args[1:]
            if placeholders != len(values):
                raise EudslError(
                    "EUDSL-FORMAT",
                    f"FixedString.{what}: format string has {placeholders} "
                    f"placeholder(s) but {len(values)} arg(s) were given",
                )
            vi = 0
            for kind, payload in parts:
                if kind == "lit":
                    expanded.append(
                        ExpandedArg(
                            kind="lit",
                            bytes=encode_utf8(payload, f"FixedString.{what}"),  # type: ignore[arg-type]
                            spec=FormatSpec(radix="hex"),
                            value=None,
                        )
                    )
                else:
                    item = self._classify_arg(
                        values[vi], payload, what  # type: ignore[arg-type]
                    )
                    expanded.append(item)
                    vi += 1
        else:
            for arg in args:
                item = self._classify_arg(arg, "hex", what)
                expanded.append(item)

        # One layout walk drives the capacity bound for the same item order
        # the emission loop below writes, so the two cannot drift.
        static_known = self._static_pos is not None
        start: int | None
        if self._static_pos is not None:
            start = self._static_pos + len(self._pending)
        else:
            start = self._bound_pos
        layout = (
            None
            if start is None
            else _layout_run(expanded, start, static_known)
        )
        if start is not None and layout is not None:
            self._check_capacity(layout, start, what)

        for item in expanded:
            if item.kind == "lit":
                self._append_static_bytes(item.bytes or [])
            elif item.spec.radix == "hex" and self._static_pos is not None:
                self._flush_pending(True)
                _emit_packed_hex(
                    self._index,
                    _materialize(item.value),  # type: ignore[arg-type]
                    self._static_pos >> 2,
                    self._static_pos & 3,
                )
                self._static_pos += HEX_CONTENT_BYTES
                if self._bound_pos is not None:
                    self._bound_pos += HEX_CONTENT_BYTES
                if self._pos_var is not None:
                    self._pos_var.set(self._static_pos)
                self._aligned_lane = self._static_pos & 3
            elif item.spec.radix == "dec" and item.spec.width is not None and self._static_pos is not None:
                self._flush_pending(True)
                self._emit_fixed_decimal_value(_materialize(item.value), item.spec)
                self._static_pos += item.spec.width
                if self._bound_pos is not None:
                    self._bound_pos += item.spec.width
                if self._pos_var is not None:
                    self._pos_var.set(self._static_pos)
                self._aligned_lane = self._static_pos & 3
            else:
                self._flush_pending(True)
                self._ensure_pos()
                if item.spec.radix == "hex":
                    self._emit_hex_value(_materialize(item.value))  # type: ignore[arg-type]
                else:
                    self._emit_decimal_value(_materialize(item.value), item.spec)  # type: ignore[arg-type]

    def _check_capacity(
        self,
        layout: _RunLayout,
        start: int,
        what: str,
    ) -> None:
        content_length = layout.end - start
        if layout.end > self.size:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"FixedString.{what}: {content_length} content byte(s) at offset "
                f"{start} would end at {layout.end}, past the {self.size}-byte capacity",
            )

    def _classify_arg(
        self,
        arg: ContentArg,
        spec: FormatSpec | str,
        what: str,
    ) -> ExpandedArg:
        norm_spec = FormatSpec(radix=spec) if isinstance(spec, str) else spec
        if isinstance(arg, str):
            return ExpandedArg(
                kind="lit",
                bytes=encode_utf8(arg, f"FixedString.{what}"),
                spec=norm_spec,
                value=None,
            )
        if _is_plain_int(arg):
            return ExpandedArg(
                kind="lit",
                bytes=(
                    _hex_bytes(arg)
                    if norm_spec.radix == "hex"
                    else _dec_bytes(arg, norm_spec.width, norm_spec.fill)
                ),
                spec=norm_spec,
                value=None,
            )
        if _is_runtime(arg):
            return ExpandedArg(
                kind="game",
                bytes=None,
                spec=norm_spec,
                value=arg,
            )
        raise EudslError(
            "EUDSL-TYPE",
            f"FixedString.{what}: expected a string, integer, or game-time value, "
            f"got {_describe(arg)}",
        )

    def _emit_fixed_decimal_core(
        self,
        value: Var,
        spec: FormatSpec,
        target: _FixedDecimalTarget,
    ) -> None:
        """Shared fixed-width decimal digit loop.

        ``target`` owns the destination: the static target writes each digit
        byte straight into the buffer's compile-time dword with a byte mask,
        while the dynamic target accumulates a scratch ``Var`` and appends it
        through the runtime-position fallback. ``target.lane_for(k)`` returns
        the compile-time lane when it is known, or ``None`` otherwise.
        """
        work = Var(0)
        work.set(value)
        width = spec.width if spec.width is not None else DECIMAL_CONTENT_BYTES
        if width < 10:
            work.imod(10**width)

        fill_byte = ord(spec.fill) if spec.fill is not None else ord(" ")
        suppress_zeros = (spec.fill != "0")
        if suppress_zeros:
            started = Var(0)
            started.set(0)

        ascii_zero = 0x30
        fill_delta = (ascii_zero - fill_byte) & 0xFF

        for k in range(width):
            lane = target.lane_for(k)
            power = 10 ** (width - 1 - k)
            is_units = (k == width - 1)
            weights = (4, 2, 1) if power == 1_000_000_000 else (8, 4, 2, 1)

            if not suppress_zeros or is_units:
                digit = target.begin(k, lane, ascii_zero)
                for w in weights:
                    test_val = w * power
                    Trigger(
                        conds=[Deaths(work.value, AtLeast, test_val, 0)],
                        acts=[
                            SetDeaths(work.value, Subtract, test_val, 0),
                            target.add(digit, k, lane, w),
                        ],
                    )
                target.end(digit, k, lane)
            else:
                digit = target.begin(k, lane, fill_byte)
                # Work still holds this digit's full value here: detecting the
                # first non-zero digit before the weight tests subtract from it
                # removes the per-digit digit-var comparison.
                Trigger(
                    conds=[
                        Deaths(started.value, Exactly, 0, 0),
                        Deaths(work.value, AtLeast, power, 0),
                    ],
                    acts=[SetDeaths(started.value, SetTo, 1, 0)],
                )
                # Digits after the first non-zero one (and that digit itself,
                # now that `started` is set) turn the fill byte into ASCII '0';
                # the weight tests below add the digit's value to it.
                Trigger(
                    conds=[Deaths(started.value, Exactly, 1, 0)],
                    acts=[target.add(digit, k, lane, fill_delta)],
                )
                for w in weights:
                    test_val = w * power
                    Trigger(
                        conds=[Deaths(work.value, AtLeast, test_val, 0)],
                        acts=[
                            SetDeaths(work.value, Subtract, test_val, 0),
                            target.add(digit, k, lane, w),
                        ],
                    )
                target.end(digit, k, lane)

    def _emit_fixed_decimal_value(self, value: Var, spec: FormatSpec) -> None:
        assert self._static_pos is not None
        self._emit_fixed_decimal_core(
            value,
            spec,
            _StaticDecimalTarget(self, self._static_pos),
        )

    def _emit_hex_value(self, value: Var) -> None:
        start_lane = self._next_content_lane()
        nibbles = _accumulate_hex_nibbles(value)

        for char_idx in range(HEX_CONTENT_BYTES):
            nib_idx = HEX_CONTENT_BYTES - 1 - char_idx
            nib = nibbles[nib_idx]

            ascii_var = Var(0)
            ascii_var.set(nib)
            ascii_var.iadd(0x30)

            def _adjust(a=ascii_var):
                a.iadd(0x27)

            when(nib.ge(10), _adjust)

            self._append_dynamic_byte(
                ascii_var,
                True,
                None
                if start_lane is None
                else (start_lane + char_idx) & 3,
            )

    def _emit_decimal_value(
        self, value: Var, spec: FormatSpec | None = None
    ) -> None:
        if spec is not None and spec.width is not None:
            self._emit_fixed_decimal_core(
                value,
                spec,
                _DynamicDecimalTarget(self),
            )
            return
        # Variable-width decimal: one shared kernel per module and capacity
        # (see `_shared_decimal_kernel`). A bare `{}` is ~688T static inline,
        # so sharing it makes K sites cost O(K + M) instead of O(K x M).
        pos = self._ensure_pos()
        base = self._ensure_base()
        # Seed the byte-writer cursor here, at the call site, where the
        # build-time position may still be known (a constant dword index
        # needs no `pos >> 2` bit ladder). The body assumes a live cursor.
        if not self._cp_valid:
            start = self._static_pos
            self._seed_cp(
                pos, base, None if start is None else start >> 2
            )
        kernel = _shared_decimal_kernel(self)
        new_pos = kernel(value, pos, base)
        # The kernel returns the advanced position. Its body keeps the
        # CurrentPlayer cursor in lockstep (`base + (pos >> 2)`, one advance
        # per lane wrap), and it ran against this string's own base, so the
        # caller can keep borrowing the cursor: a following literal or digit
        # run writes through it without re-deriving `pos >> 2`. Only the
        # build-time position becomes unknowable (the width is runtime
        # dependent).
        pos.set(new_pos)
        self._cp_valid = True
        self._static_pos = None
        self._bound_pos = None
        self._aligned_lane = None

    def _emit_decimal_body(self, value: Var) -> None:
        """Emit the variable-width decimal digit loop at the current state."""
        # CP cursor (issue #87): seed the byte-writer cursor once per format
        # run, at an unconditional point, so every conditional digit branch
        # can rely on it without re-deriving `pos >> 2`.
        if not self._cp_valid:
            start = self._static_pos
            self._seed_cp(
                self._ensure_pos(),
                self._ensure_base(),
                None if start is None else start >> 2,
            )
        work = Var(0)
        work.set(value)
        started = Var(0)
        started.set(0)
        for power in DECIMAL_POWERS:
            digit = Var(0)
            digit.set(0)
            weights = (4, 2, 1) if power == 1_000_000_000 else (8, 4, 2, 1)
            for w in weights:
                test_val = w * power
                Trigger(
                    conds=[Deaths(work.value, AtLeast, test_val, 0)],
                    acts=[
                        SetDeaths(work.value, Subtract, test_val, 0),
                        SetDeaths(digit.value, Add, w, 0),
                    ],
                )

            when(started.eq(1), lambda d=digit: self._append_decimal_digit(d))
            if power == 1:
                when(
                    started.eq(0),
                    lambda d=digit, s=started: (
                        s.set(1),
                        self._append_decimal_digit(d),
                    ),
                )
            else:
                when(
                    (started.eq(0), digit.ge(1)),
                    lambda d=digit, s=started: (
                        s.set(1),
                        self._append_decimal_digit(d),
                    ),
                )

    def _declare_decimal_kernel(self) -> Any:
        """Declare the shared variable-width decimal body for this capacity.

        The body runs against parameter variables instead of this string's
        cursor: `pos` (byte position) and `base` (STR buffer base) arrive per
        call, and the body returns the advanced position. The capacity stays
        this declaring string's compile-time `size`, so the digit guards fold
        to direct conditions; call sites of a different size get their own
        body. The caller seeds the CurrentPlayer cursor before jumping in.
        """
        from .temporal import func

        builder = _get_active_module_builder()
        if builder is None:
            raise EudslError(
                "EUDSL-SCOPE", "a variable-width decimal write must be inside buildModule()"
            )
        saved = (
            self._pos_var,
            self._base,
            self._static_pos,
            self._bound_pos,
            self._aligned_lane,
            self._cp_valid,
            self._pending,
        )
        self._pending = []

        def body(value: Any, pos: Any, base: Any) -> Any:
            self._pos_var = pos
            self._base = base
            self._static_pos = None
            self._bound_pos = None
            self._aligned_lane = None
            # The call site seeded the cursor; the body must not re-derive it.
            self._cp_valid = True
            self._emit_decimal_body(value)
            return pos

        try:
            kernel = func(body)
        finally:
            (
                self._pos_var,
                self._base,
                self._static_pos,
                self._bound_pos,
                self._aligned_lane,
                self._cp_valid,
                self._pending,
            ) = saved
        return kernel

    def _append_decimal_digit(self, digit: Var) -> None:
        # CP cursor (issue #87): ASCII '0' is a compile-time constant; the
        # digit value is added as four constant bit adds, so no `ascii_var`
        # and no value shift ladder is emitted.
        self._append_dynamic_byte(digit, True, None, const_byte=0x30, bits=(1, 2, 4, 8))

    def _append_static_bytes(self, data: list[int]) -> None:
        if not data:
            return
        if self._static_pos is not None:
            self._pending.extend(data)
            if self._bound_pos is not None:
                self._bound_pos += len(data)
            return
        if (
            self._aligned_lane == 0
            and self._bound_pos is not None
            and len(data) >= DWORD_BYTES
        ):
            self._append_aligned_dwords(data)
            return
        for byte in data:
            self._append_dynamic_byte(Var(byte), True, self._aligned_lane)

    def _append_aligned_dwords(self, data: list[int]) -> None:
        self._flush_pending(True)
        # The dword writes below move `pos` outside the CP-cursor writer, so
        # any borrowed CurrentPlayer cursor is stale from here on.
        self._cp_valid = False
        pos = self._ensure_pos()
        base = self._ensure_base()
        dword_index = Var(0)
        dword_index.set(pos.shr(2))
        dest_base = Var(0)
        dest_base.set(base)
        dest_base.iadd(dword_index)

        consumed = 0
        chunk = 0
        while len(data) - consumed >= DWORD_BYTES:
            value = 0
            for i in range(DWORD_BYTES):
                value |= data[consumed + i] << (8 * i)
            dest = Var(0)
            dest.set(dest_base)
            if chunk > 0:
                dest.iadd(chunk)
            ptr = Trigger(
                conds=[],
                acts=[SetDeaths(0, SetTo, value & 0xFFFFFFFF, 0)],
                preserved=True,
                register=False,
            )
            emitVarStore(
                dest.id,
                TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
                SetTo,
            )
            _register_trigger(ptr)
            consumed += DWORD_BYTES
            chunk += 1
        pos.iadd(chunk * DWORD_BYTES)
        if self._bound_pos is not None:
            self._bound_pos += chunk * DWORD_BYTES

        rem = len(data) - consumed
        if rem > 0:
            value = 0
            for i in range(rem):
                value |= data[consumed + i] << (8 * i)
            dest = Var(0)
            dest.set(dest_base)
            if chunk > 0:
                dest.iadd(chunk)
            mask = (0xFFFFFFFF >> (8 * (DWORD_BYTES - rem))) & 0xFFFFFFFF
            ptr = Trigger(
                conds=[],
                acts=[SetDeathsX(0, SetTo, value & 0xFFFFFFFF, 0, mask)],
                preserved=True,
                register=False,
            )
            emitVarStore(
                dest.id,
                TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
                SetTo,
            )
            _register_trigger(ptr)
            pos.iadd(rem)
            if self._bound_pos is not None:
                self._bound_pos += rem
            self._aligned_lane = rem & 3
        else:
            self._aligned_lane = 0
        self._static_pos = None

    def _flush_pending(self, write_partial: bool) -> None:
        if not self._pending:
            return
        if self._static_pos is None:
            raise EudslError(
                "EUDSL-SCOPE",
                "FixedString internal error: pending bytes without a static position",
            )
        start = self._static_pos
        pos = start
        consumed = 0
        while consumed < len(self._pending):
            aligned = pos & ~3
            subp = pos & 3
            fit = 4 - subp
            n = min(fit, len(self._pending) - consumed)
            if not write_partial and n < fit:
                break
            dword = 0
            for i in range(n):
                dword |= self._pending[consumed + i] << (8 * (subp + i))
            byte_mask = (0xFFFFFFFF >> (8 * (4 - n))) & 0xFFFFFFFF
            mask = (byte_mask << (8 * subp)) & 0xFFFFFFFF
            SetStringDword(self._index, aligned, dword & 0xFFFFFFFF, mask)
            consumed += n
            pos += n
        if consumed > 0:
            del self._pending[:consumed]
            self._static_pos = pos
            self._cp_valid = False

    def _seed_cp(self, pos: Var, base: Var, dword_index: int | None = None) -> None:
        """Point CurrentPlayer at the dword holding the next content byte.

        The register holds a dword address, so it must be written by a
        var-store (the action *value* field is a literal, not a variable
        reference). When the content start is known at build time the dword
        index is a constant, so no `pos >> 2` bit ladder is emitted.
        """
        cp_target = Var(0)
        cp_target.set(base)
        if dword_index is None:
            cp_target.iadd(pos.shr(2))
        elif dword_index != 0:
            cp_target.iadd(dword_index)
        emitVarStore(cp_target.id, AddrReloc(CP_ADDR), SetTo)
        self._cp_valid = True

    def _append_dynamic_byte(
        self,
        value: Var,
        advance: bool = True,
        lane: object = _USE_FIELD,
        const_byte: int | None = None,
        bits: tuple[int, ...] = (),
    ) -> None:
        self._flush_pending(True)
        pos = self._ensure_pos()
        base = self._ensure_base()
        known_lane = self._next_content_lane() if lane is _USE_FIELD else lane

        if known_lane is not None:
            known = int(known_lane)  # type: ignore[arg-type]

            def write_byte() -> None:
                dword_index = Var(0)
                dword_index.set(pos.shr(2))
                dest = Var(0)
                dest.set(base)
                dest.iadd(dword_index)

                mask = (0xFF << (8 * known)) & 0xFFFFFFFF
                ptr = Trigger(
                    conds=[],
                    acts=[SetDeathsX(0, SetTo, 0, 0, mask)],
                    preserved=True,
                    register=False,
                )
                emitVarStore(
                    dest.id,
                    TriggerFieldReloc(ptr.id, ptr.act(0).dest.byteOffset),
                    SetTo,
                )

                shifted = Var(0)
                shifted.set(value)
                if known > 0:
                    shifted.ishl(8 * known)
                emitVarStore(
                    shifted.id,
                    TriggerFieldReloc(ptr.id, ptr.act(0).value.byteOffset),
                    SetTo,
                )
                _register_trigger(ptr)

            if advance:
                def guarded() -> None:
                    write_byte()
                    pos.iadd(1)
                    self._cp_valid = False
                    if self._bound_pos is not None:
                        self._bound_pos += 1

                when(pos.lt(self.size), guarded)
                self._static_pos = None
                self._aligned_lane = (known + 1) & 3
            else:
                # The terminator is allowed at the clamped position (which can
                # equal `size`), so it bypasses the content-byte guard.
                write_byte()
            return

        # CP-cursor byte-lane writer (issue #87, ADR 0004).
        #
        # The cursor holds `base + (pos >> 2)` (an EPD, so dword-granular).
        # It is advanced in the lane-3 branch when the byte lane wraps, so no
        # per-byte `pos.shr(2)` ladder is emitted. A format run seeds it once
        # (`_seed_cp` in `_emit_decimal_value`); any other unknown-lane write
        # (runtime `writeFrom`, dynamic fixed-width decimal, dynamic static
        # runs) seeds it on demand with a runtime `pos >> 2`. Each lane writes
        # with a constant byte plus constant bit adds (`const_byte` / `bits`),
        # so no per-lane value shift ladder is emitted either: the ASCII digit
        # is `0x30` plus the digit's 1/2/4/8 bits, all compile-time constants.
        if not self._cp_valid:
            start = self._static_pos
            self._seed_cp(
                pos, base, None if start is None else start >> 2
            )

        def write_byte() -> None:
            for k in range(4):
                mask = (0xFF << (8 * k)) & 0xFFFFFFFF
                # `pos & 3 == k` as one masked-equality condition: no lane
                # variable and no `pos.bitAnd` action.
                with when(DeathsX(pos.value, Exactly, k, 0, 0x3)):
                    if const_byte is not None:
                        SetDeathsX(
                            CURRENT_PLAYER,
                            SetTo,
                            (const_byte << (8 * k)) & 0xFFFFFFFF,
                            0,
                            mask,
                        )
                        for bit in bits:
                            with when(DeathsX(value.value, AtLeast, 1, 0, bit)):
                                SetDeathsX(
                                    CURRENT_PLAYER,
                                    Add,
                                    (bit << (8 * k)) & 0xFFFFFFFF,
                                    0,
                                    mask,
                                )
                    else:
                        # Generic byte value: an 8-bit add ladder.
                        SetDeathsX(CURRENT_PLAYER, SetTo, 0, 0, mask)
                        for bit in (1, 2, 4, 8, 16, 32, 64, 128):
                            with when(DeathsX(value.value, AtLeast, 1, 0, bit)):
                                SetDeathsX(
                                    CURRENT_PLAYER,
                                    Add,
                                    (bit << (8 * k)) & 0xFFFFFFFF,
                                    0,
                                    mask,
                                )
                    if k == 3:
                        # Dword-granular cursor: advance on lane wrap.
                        SetDeaths(EPD_CURRENT_PLAYER, Add, 1, 0)

        if advance:
            def guarded() -> None:
                write_byte()
                pos.iadd(1)
                if self._bound_pos is not None:
                    self._bound_pos += 1

            when(pos.lt(self.size), guarded)
            self._static_pos = None
        else:
            # The terminator is allowed at the clamped position (which can
            # equal `size`), so it bypasses the content-byte guard.
            write_byte()
        self._aligned_lane = None


class LazyFixedString:
    """A lazy ``FixedString`` handle (globals P4): constructible anywhere
    (no builder needed) that resolves to a fully eager ``FixedString``
    binding on first resolving use inside ``build_module``, one binding per
    builder.

    ADR 0005 D8 selects a wrapper plus explicit method delegation for this
    phase, not a subclass and not a deferred flag on ``FixedString``.
    ``size`` is a pure read; ``message_index`` and every method resolve.
    Binding creation is one full eager ``FixedString`` construction (string
    index and flusher registration in eager order), so flusher order follows
    binding order (D9). A never-used handle emits nothing (D5).
    """

    def __init__(self, size: int) -> None:
        _check_fixed_string_size(size)
        self.size = size
        # Per-builder bindings live in the shared deferred-binding state
        # machine (weak-keyed by builder; its count stays monotonic so
        # post-build ambiguity stays loud after a builder is collected).
        self._deferred = LazyBinding("FixedString", entry="build_module")

    def _resolve(self) -> FixedString:
        """The eager instance serving reads: the active builder's binding
        for this handle.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the eager ``FixedString`` construction
        varies here (R3, string index plus flusher registration in eager
        order)."""
        return self._deferred.resolve(lambda: FixedString(self.size))

    @property
    def message_index(self) -> int:
        return self._resolve().message_index

    def write(self, *args: ContentArg) -> None:
        self._resolve().write(*args)

    def append(self, *args: ContentArg) -> None:
        self._resolve().append(*args)

    def writeFrom(self, pos: int | Var | Expr | ReadonlyVar, *args: ContentArg) -> None:
        self._resolve().writeFrom(pos, *args)

    write_from = writeFrom

    def print(self, *args: ContentArg) -> None:
        self._resolve().print(*args)

    def printAt(self, line: int | Var | Expr | ReadonlyVar, *args: ContentArg) -> None:
        self._resolve().printAt(line, *args)

    print_at = printAt

    def display(self) -> None:
        self._resolve().display()

    def displayAt(self, line: int | Var | Expr | ReadonlyVar) -> None:
        self._resolve().displayAt(line)

    display_at = displayAt

    def clear(self) -> None:
        self._resolve().clear()


def lazyFixedString(size: int) -> LazyFixedString:
    """Declare a lazy ``FixedString`` (globals P4). Call shape mirrors
    ``FixedString(size)``. Bounds are validated eagerly with the same
    ``EUDSL-BOUNDS`` messages; no string-table entry, index, var, or flusher
    is allocated until first resolving use. See :class:`LazyFixedString`.
    This factory is the documented entry point; the wrapper class stays
    internal."""
    warn_lazy_in_builder()
    return LazyFixedString(size)


lazy_fixed_string = lazyFixedString
