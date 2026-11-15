"""Fixed-capacity ``FixedArray`` container for primitives and ``TriggerStruct`` rows.

Writable arrays are backed by page-aligned ``TriggerPage`` action slots. A
constant index is pure address math (0T) and comparisons compile to one
``MemCond`` condition. Dynamic indexing uses the carry-free ``PageSlotEPD``
weights (``8 << b`` for ``b < 6``, ``602 << (b - 6)`` for ``b >= 6``) and one
trigger per index bit.

``FixedArray.const([...])`` is a build-time table: zero TriggerPages, folded
constant reads, and folded comparisons.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .backing import (
    ELEM_MASK,
    PageSlotEPD,
    pageSlotBitCond,
    pageSlotEpd,
    pageSlotEpdWeight,
)
from .cp import emit_seed_cp_from_slot
from .stock_rows_built import ACT_FIELD_OFFSETS

from .hir import (
    Add,
    Always,
    Never,
    SetDeaths,
    SetDeathsX,
    SetTo,
    TriggerFieldReloc,
    emitVarStore,
)
from .struct import TriggerStructInstance, TriggerStructSchema, dword as struct_dword
from .temporal import when
from .trigger import (
    ACT_COUNT,
    Trigger,
    _register_trigger,
)
from .lazy_binding import LazyBinding
from .var import Var, align_page_boundary
from .view import (
    CURRENT_PLAYER,
    EPD_CURRENT_PLAYER,
    EudslError,
    _describe_value,
    _is_plain_int,
    _require_plain_int,
    emit_bit_test_read,
    warn_lazy_in_builder,
)

#: Minimum ``length`` at which a dynamic ``get`` switches from the O(N)
#: per-element unroll to the PageSlotEPD pointer reader. The measured static
#: crossover for the shipped ``zero`` restore is N=14 (executed N=39); 64 is
#: comfortably past both and keeps the cost-gate fixture's N=4 dynamic read on
#: the unroll, so the frozen goldens do not move. Mirrored in ``ts/array.ts``
#: as ``DYNAMIC_GET_MIN_LENGTH`` (there is no shared constants module); both
#: sides pin the value in a test.
DYNAMIC_GET_MIN_LENGTH = 64


def _raw_epd_reset(dest_field: Any, src_var: Var) -> dict[str, Any]:
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
                "field_offset": ACT_FIELD_OFFSETS["value"],
                "target_var": src_var.id,
                "target_offset": 20,
                "mode": 1,
            },
        ],
    }


class FixedArrayImpl:
    """Fixed-capacity writable array backed by ``TriggerPage`` slots."""

    def __init__(self, elem: Any, length: int, _deferred: bool = False) -> None:
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise EudslError(
                "EUDSL-BOUNDS", f"FixedArray length must be a positive integer, got {length!r}"
            )
        self.length = length
        self.elemType = elem
        self.elem_type = elem
        self.bits = max(1, (length - 1).bit_length())
        self.mask = ELEM_MASK.get(elem, 0xFFFFFFFF) if isinstance(elem, str) else 0xFFFFFFFF
        self.isConst = False
        self.is_const = False
        # INTERNAL deferred flag (globals P2-01; not public API, not
        # exported as a user option — the public path is
        # `lazyFixedArray`): when set, skip page alignment, element
        # allocation, and struct instantiation. The handle resolves its
        # arrays on first use through a per-builder binding (see
        # `_resolve`).
        self._deferred = (
            LazyBinding("FixedArray", entry="build_module") if _deferred else None
        )
        if _deferred:
            # Eager type validation: a bad element type throws at
            # declaration with the eager error code, before any binding
            # exists.
            if not isinstance(elem, TriggerStructSchema):
                if not isinstance(elem, str) or elem not in ELEM_MASK:
                    raise EudslError(
                        "EUDSL-BOUNDS", f"Unknown FixedArray element type: {elem!r}"
                    )
            self.pageCount = length if isinstance(elem, TriggerStructSchema) else (
                (length + ACT_COUNT - 1) // ACT_COUNT
            )
            self.pages = self.pageCount
            self.page_count = self.pageCount
            self._vars: list[Var] | None = None
            self._instances: list[TriggerStructInstance] | None = None
            return

        vars_: list[Var] = []
        instances: list[TriggerStructInstance] = []
        if isinstance(elem, TriggerStructSchema):
            for _ in range(length):
                align_page_boundary()
                instances.append(elem.instantiate())
            for inst in instances:
                vars_.extend(inst.fields)
            self.pageCount = length
        else:
            if not isinstance(elem, str) or elem not in ELEM_MASK:
                raise EudslError("EUDSL-BOUNDS", f"Unknown FixedArray element type: {elem!r}")
            align_page_boundary()
            for _ in range(length):
                vars_.append(Var(0, explicit_mask=self.mask, storage="page"))
            self.pageCount = (length + ACT_COUNT - 1) // ACT_COUNT
        self.pages = self.pageCount
        self.page_count = self.pageCount
        self._vars = vars_
        self._instances = instances

    # ------------------------------------------------------------------
    # Deferred resolution
    # ------------------------------------------------------------------

    def _resolve(self) -> "FixedArrayImpl":
        """The eager instance serving array reads: this for eager handles,
        the active builder's binding for deferred ones.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``FixedArray``
        construction varies here (never per-element, R3)."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(lambda: FixedArray(self.elemType, self.length))

    @property
    def vars(self) -> list[Var]:
        return self._resolve()._vars  # type: ignore[return-value]

    @property
    def instances(self) -> list[TriggerStructInstance]:
        return self._resolve()._instances  # type: ignore[return-value]

    def __len__(self) -> int:
        # Pure metadata (no resolve): `set_all` uses `len(src)` for the
        # copy-length check, so an array source must answer it — eager
        # `set_all` with an array source previously raised TypeError
        # here, which the P2-01 copy-source acceptance item requires
        # to work.
        return self.length

    def _check(self, index: int) -> int:
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < self.length):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"FixedArray index {index!r} out of bounds for length {self.length}",
            )
        return index

    def get(self, index: int | Var) -> Any:
        if isinstance(index, int):
            i = self._check(index)
            if self.instances:
                return self.instances[i]
            return self.vars[i]
        if self.instances:
            raise EudslError(
                "EUDSL-BOUNDS",
                "Dynamic indexing into FixedArray(TriggerStruct) requires a constant page index",
            )
        if self.length >= DYNAMIC_GET_MIN_LENGTH:
            return self._emit_dynamic_read(index)
        out = Var(0)
        for k in range(self.length):
            elem = self.vars[k]
            when(index.eq(k), lambda elem=elem: out.set(elem))
        return out

    def set(self, index: int | Var, value: Any) -> "FixedArrayImpl":
        if isinstance(index, int):
            i = self._check(index)
            if self.instances:
                dst = self.instances[i]
                if isinstance(value, TriggerStructInstance):
                    dst.set(value)
                elif isinstance(value, int) and not isinstance(value, bool):
                    for field in dst.fields:
                        field.set(value)
                else:
                    raise EudslError(
                        "EUDSL-BOUNDS", "Struct array set requires a struct or number"
                    )
                return self
            self.vars[i].set(value)
            return self
        if self.instances:
            raise EudslError(
                "EUDSL-BOUNDS",
                "Dynamic indexing into FixedArray(TriggerStruct) requires a constant page index",
            )
        if not (_is_plain_int(value) or isinstance(value, Var)):
            raise EudslError(
                "EUDSL-TYPE",
                f"array element must be a plain integer or a Var; got {_describe_value(value)}",
            )
        self._emit_dynamic_write(index, value)
        return self

    def clear(self, value: int = 0) -> "FixedArrayImpl":
        if self.instances:
            for inst in self.instances:
                for field in inst.fields:
                    field.set(value)
        else:
            for v in self.vars:
                v.set(value)
        return self

    def fill(self, value: int = 0) -> "FixedArrayImpl":
        return self.clear(value)

    def setAll(self, src: Any) -> "FixedArrayImpl":
        return self.set_all(src)

    def set_all(self, src: Any) -> "FixedArrayImpl":
        src_len = len(src.values) if isinstance(src, ConstFixedArrayImpl) else len(src)
        if src_len != self.length:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"FixedArray copy length mismatch: {self.length} vs {src_len}",
            )
        if self.instances:
            for i in range(self.length):
                self.instances[i].set(src.instances[i])
        else:
            for i in range(self.length):
                v = src.vars[i] if isinstance(src, FixedArrayImpl) else src[i]
                self.vars[i].set(v)
        return self

    def copyFrom(self, src: Any) -> "FixedArrayImpl":
        return self.set_all(src)

    def copy_from(self, src: Any) -> "FixedArrayImpl":
        return self.set_all(src)

    def _emit_dynamic_write(self, index: Var, value: int | Var) -> None:
        elem0 = self.vars[0]
        flag = Var(0)
        pointer = Trigger(
            conds=[flag.eq(1)],
            # `.dest` is the runtime element EPD and `.value` the runtime
            # value; the element's own 32-byte page slot is the destination,
            # so a full-dword write cannot spill into a neighbouring element.
            # The dynamic path stores the raw dword (the element mask only
            # narrows the static `arr.set(i, const)` path), exactly as the
            # pre-change 32-bit-trigger frontend did.
            acts=[SetDeathsX(0, SetTo, 0, 0, 0xFFFFFFFF), flag.act_set(0)],
            register=False,
        )
        dest_field = pointer.act(0).dest

        reset_acts: list[dict[str, Any]] = [_raw_epd_reset(dest_field, elem0), flag.act_set(1)]
        if isinstance(value, int):
            reset_acts.append(pointer.act(0).value.act_set(value))
        else:
            reset_acts.append(pointer.act(0).value.act_set(0))
        Trigger(acts=reset_acts)

        for bit in range(self.bits):
            Trigger(
                conds=[flag.eq(1), pageSlotBitCond(index, bit)],
                acts=[dest_field.act_add(pageSlotEpdWeight(bit))],
            )

        if isinstance(value, Var):
            # One VarStore replaces the 32 value-bit triggers: the setup act
            # points the source var's TriggerPage slot at this pointer's
            # `.value` field, so the page firing writes the whole value in one
            # out-of-band firing. The flag is always 1 at this point (the
            # reset trigger set it and only the pointer clears it), so no
            # extra gate is emitted.
            emitVarStore(
                value.id,
                TriggerFieldReloc(pointer.id, pointer.act(0).value.byteOffset),
            )

        _register_trigger(pointer)

    def _emit_dynamic_read(self, index: Var) -> Var:
        """Read ``self.vars[index]`` through the ``CurrentPlayer`` register.

        The index bits patch CurrentPlayer from ``EPD(elem0.value)`` with the
        same carry-free PageSlotEPD weights the write path uses, so the
        address costs one trigger per index bit instead of one branch per
        element. The dword is then unpacked with 32 bit tests.

        Every bit test carries the ``index < length`` guard: the unroll leaves
        ``out`` at 0 when no branch matches, and the reader must agree. The
        guard adds conditions only (no triggers, no bytes).

        CurrentPlayer is left at the canonical loop-start value ``0`` (ADR
        0004, the ``zero`` policy). A non-zero ambient value is not preserved:
        treat CP as scratch across a dynamic ``get`` (see GLOSSARY.md, CP).
        """
        emit_seed_cp_from_slot(self.vars[0])
        for bit in range(self.bits):
            Trigger(
                conds=[pageSlotBitCond(index, bit)],
                acts=[SetDeaths(EPD_CURRENT_PLAYER, Add, pageSlotEpdWeight(bit), 0)],
            )
        out = Var(0)
        Trigger(conds=[], acts=[out.act_set(0)], preserved=True)
        emit_bit_test_read(
            CURRENT_PLAYER,
            32,
            0,
            out,
            extra_conds=[index.lt(self.length)],
        )
        Trigger(conds=[], acts=[SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)])
        return out

    def eq(self, index: int, rhs: int) -> dict[str, Any]:
        return self.vars[self._check(index)].eq(rhs)

    def ge(self, index: int, rhs: int) -> dict[str, Any]:
        return self.vars[self._check(index)].ge(rhs)

    def le(self, index: int, rhs: int) -> dict[str, Any]:
        return self.vars[self._check(index)].le(rhs)

    def __getitem__(self, index: int) -> Any:
        return self.get(index)

    def __setitem__(self, index: int, value: Any) -> None:
        self.set(index, value)


class ConstFixedArrayValue:
    """Build-time value from ``FixedArray.const``."""

    def __init__(self, value: int) -> None:
        self.value = _require_plain_int(value, "const array value") & 0xFFFFFFFF

    def _cmp(self, rhs: int, kind: str) -> dict[str, Any]:
        other = _require_plain_int(rhs, "const array comparison") & 0xFFFFFFFF
        if kind == "eq":
            ok = self.value == other
        elif kind == "ne":
            ok = self.value != other
        elif kind == "ge":
            ok = self.value >= other
        elif kind == "le":
            ok = self.value <= other
        elif kind == "gt":
            ok = self.value > other
        else:
            ok = self.value < other
        return Always() if ok else Never()

    def eq(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "eq")

    def ne(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "ne")

    def ge(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "ge")

    def le(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "le")

    def gt(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "gt")

    def lt(self, rhs: int) -> dict[str, Any]:
        return self._cmp(rhs, "lt")

    def read(self) -> Var:
        return Var(self.value)

    def __int__(self) -> int:
        return self.value

    def __index__(self) -> int:
        return self.value


class ConstFixedArrayImpl:
    """Read-only payload-backed constant table with zero TriggerPages."""

    def __init__(self, values: Sequence[int]) -> None:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise EudslError("EUDSL-BOUNDS", "FixedArray.const requires a literal sequence")
        self.values = [_require_plain_int(v, "const array value") & 0xFFFFFFFF for v in values]
        self.length = len(self.values)
        self.isConst = True
        self.is_const = True
        self.pageCount = 0
        self.page_count = 0
        self.pages = 0
        self.vars: list[Var] = []

    def get(self, index: int) -> ConstFixedArrayValue:
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < self.length):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"FixedArray.const index {index!r} out of bounds for length {self.length}",
            )
        return ConstFixedArrayValue(self.values[index])

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        raise EudslError("EUDSL-BOUNDS", "FixedArray.const is read-only")

    def __getitem__(self, index: int) -> ConstFixedArrayValue:
        return self.get(index)


class FixedArray(FixedArrayImpl):
    """Unified fixed-capacity array factory."""

    @classmethod
    def const(cls, values: Sequence[int]) -> ConstFixedArrayImpl:
        return ConstFixedArrayImpl(values)


def lazyFixedArray(elem: Any, length: int) -> FixedArray:
    """Declare a lazy ``FixedArray`` (globals P2-01): a handle
    constructible anywhere (no builder needed) that resolves to a fully
    eager binding on first resolving use inside ``build_module``, one
    binding per builder.

    Binding creation is atomic: one full eager ``FixedArray``
    construction — page alignment plus ALL element vars, or ALL struct
    instances, in one uninterrupted run. There is no per-element
    laziness and no init form: elements always start at zero, exactly
    like the eager constructor. A never-used handle emits nothing (no
    decl, no page slot, no statement, D5). This factory is the only
    documented path to deferred mode; the internal constructor flag
    stays undocumented.
    """
    warn_lazy_in_builder()
    return FixedArray(elem, length, _deferred=True)


lazy_fixed_array = lazyFixedArray


arrayByte = "byte"
arrayWord = "word"
arrayDword = "dword"
arrayVar = "var"


__all__ = [
    "FixedArray",
    "FixedArrayImpl",
    "lazyFixedArray",
    "lazy_fixed_array",
    "ConstFixedArrayImpl",
    "ConstFixedArrayValue",
    "PageSlotEPD",
    "pageSlotEpd",
    "pageSlotEpdWeight",
    "pageSlotBitCond",
    "arrayByte",
    "arrayWord",
    "arrayDword",
    "arrayVar",
]
