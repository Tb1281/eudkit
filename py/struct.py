"""`TriggerStruct` — allocated struct backed by `TriggerPage` `SetDeathsX` action slots."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .lazy_binding import LazyBinding
from .trigger import ACT_COUNT, ACT_STRIDE_EPDS
from .var import Expr, ReadonlyVar, Var, _alloc_var_slot, ensure_page_fit
from .view import EudslError, _describe_value, _is_plain_int, warn_lazy_in_builder


@dataclass(frozen=True)
class FieldSpec:
    mask: int
    shift: int = 0


StructSlotWidth = str | int | FieldSpec | dict[str, Any]

_WIDTH_MASK: dict[str, int] = {
    "byte": 0x000000FF,
    "word": 0x0000FFFF,
    "dword": 0xFFFFFFFF,
}


def dword() -> FieldSpec:
    return FieldSpec(mask=0xFFFFFFFF, shift=0)


def word(shift: int = 0) -> FieldSpec:
    m = ((0x0000FFFF << shift) & 0xFFFFFFFF) or 0x0000FFFF
    return FieldSpec(mask=m, shift=shift)


def byte(shift: int = 0) -> FieldSpec:
    m = ((0x000000FF << shift) & 0xFFFFFFFF) or 0x000000FF
    return FieldSpec(mask=m, shift=shift)


def bit(width: int, shift: int = 0) -> FieldSpec:
    if not (1 <= width <= 32):
        raise EudslError("EUDSL-BOUNDS", f"bit width must be in 1..32, got {width}")
    raw = 0xFFFFFFFF if width == 32 else ((1 << width) - 1)
    m = (raw << shift) & 0xFFFFFFFF
    return FieldSpec(mask=m, shift=shift)


def masked(mask: int, shift: int = 0) -> FieldSpec:
    m = ((mask & 0xFFFFFFFF) << shift) & 0xFFFFFFFF
    return FieldSpec(mask=m, shift=shift)


def _resolve_field_spec(fspec: Any) -> tuple[int, int]:
    if isinstance(fspec, str):
        if fspec not in _WIDTH_MASK:
            raise EudslError("EUDSL-BOUNDS", f"Unknown field width type: '{fspec}'")
        return _WIDTH_MASK[fspec], 0
    if isinstance(fspec, int):
        return fspec & 0xFFFFFFFF, 0
    if isinstance(fspec, FieldSpec):
        return fspec.mask & 0xFFFFFFFF, fspec.shift
    if isinstance(fspec, dict) and "mask" in fspec:
        return int(fspec["mask"]) & 0xFFFFFFFF, int(fspec.get("shift", 0))
    raise EudslError("EUDSL-BOUNDS", f"Invalid field spec: {fspec!r}")


@dataclass(frozen=True)
class TriggerStructField:
    name: str
    actionSlot: int
    epdOffset: int
    mask: int
    shift: int = 0


class TriggerStructInstance:
    def __init__(
        self,
        schema: "TriggerStructSchema",
        field_vars: dict[str, Var] | None = None,
        *,
        # INTERNAL deferred flag (globals P2-02; not public API — the public
        # path is `lazyStruct`): when set, skip field allocation and store
        # schema + field names + validated inits only. The handle resolves
        # to a fully eager binding on first resolving use (see `_resolve`).
        deferred: bool = False,
        inits: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_field_names", list(schema.field_names))
        if deferred:
            object.__setattr__(self, "_stored_inits", dict(inits or {}))
            object.__setattr__(self, "_fields", {})
            # The shared state machine lives in ``_deferred``. Eager
            # instances never set that attribute at all, preserving the
            # ``self.__dict__.get("_deferred", False)`` probes below.
            object.__setattr__(self, "_deferred", LazyBinding("struct"))
            # No page_index/slot_index/field_count/fields attributes are
            # set here: their reads fall through to `__getattr__`, which
            # resolves the binding. `_schema`/`_field_names` above are pure
            # reads that never resolve.
            return
        assert field_vars is not None
        object.__setattr__(self, "_fields", field_vars)
        first_var = next(iter(field_vars.values()))
        object.__setattr__(self, "page_index", first_var.pageIndex)
        object.__setattr__(self, "pageIndex", first_var.pageIndex)
        object.__setattr__(self, "slot_index", first_var.slotIndex)
        object.__setattr__(self, "slotIndex", first_var.slotIndex)
        object.__setattr__(self, "field_count", schema.field_count)
        object.__setattr__(self, "fieldCount", schema.field_count)
        object.__setattr__(self, "fields", list(field_vars.values()))

    # -------------------------------------------------------------------------
    # Deferred resolution
    # -------------------------------------------------------------------------

    def _resolve(self) -> "TriggerStructInstance":
        """The eager instance serving identity reads: this for eager
        handles, the active builder's binding for deferred ones.

        The shared :class:`~py.lazy_binding.LazyBinding` state machine owns
        the builder lookup, cache, first-binding capture, and post-build
        zero/one/many policy; only the atomic eager ``instantiate`` varies
        here."""
        binding = LazyBinding.of(self)
        if binding is None:
            return self
        return binding.resolve(lambda: self._schema.instantiate(self._stored_inits))

    _RESOLVING_ATTRS = frozenset(
        {
            "page_index",
            "pageIndex",
            "slot_index",
            "slotIndex",
            "fields",
        }
    )

    def __getattr__(self, name: str) -> Any:
        if self.__dict__.get("_deferred", False):
            # Pure reads never resolve: field counts come from the schema.
            if name in ("field_count", "fieldCount"):
                return object.__getattribute__(self, "_schema").field_count
            # Only the documented surface resolves: field names plus the
            # index/fields attributes. Any other missing attribute raises
            # exactly as the eager path does, so incidental probes
            # (dunders, hasattr checks) never bind the handle.
            if name in self._field_names or name in self._RESOLVING_ATTRS:
                return getattr(self._resolve(), name)
            schema = object.__getattribute__(self, "_schema")
            raise AttributeError(
                f"Unknown field '{name}' on TriggerStruct '{schema.name}'"
            )
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        schema = object.__getattribute__(self, "_schema")
        raise AttributeError(f"Unknown field '{name}' on TriggerStruct '{schema.name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if self.__dict__.get("_deferred", False):
            if name.startswith("_") or name in self.__dict__:
                super().__setattr__(name, value)
                return
            if name in self._field_names:
                field_var = getattr(self._resolve(), name)
                if value is not field_var:
                    field_var.set(value)
                return
            schema = object.__getattribute__(self, "_schema")
            raise AttributeError(
                f"Unknown field '{name}' on TriggerStruct '{schema.name}'"
            )
        if name.startswith("_") or name in self.__dict__:
            super().__setattr__(name, value)
            return
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            if value is not fields[name]:
                fields[name].set(value)
        else:
            schema = object.__getattribute__(self, "_schema")
            raise AttributeError(f"Unknown field '{name}' on TriggerStruct '{schema.name}'")

    def copyFrom(self, src: "TriggerStructInstance") -> None:
        if self.__dict__.get("_deferred", False):
            self._resolve().copyFrom(src)
            return
        for fname in self._field_names:
            self._fields[fname].set(getattr(src, fname))

    def copy_from(self, src: "TriggerStructInstance") -> None:
        self.copyFrom(src)

    def set(self, src: Any) -> None:
        if self.__dict__.get("_deferred", False):
            self._resolve().set(src)
            return
        if isinstance(src, TriggerStructInstance):
            self.copyFrom(src)
        elif isinstance(src, dict):
            for k, v in src.items():
                if k in self._fields:
                    self._fields[k].set(v)
                else:
                    raise AttributeError(
                        f"Unknown field '{k}' on TriggerStruct '{self._schema.name}'"
                    )
        else:
            raise TypeError(f"Cannot set TriggerStruct from {type(src)}")


class TriggerStructSchema:
    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        if not (1 <= len(spec) <= ACT_COUNT):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"TriggerStruct({name}): field count must be in 1..{ACT_COUNT}, got {len(spec)}",
            )
        self.name = name
        self.field_count = len(spec)
        self.fieldCount = self.field_count
        self.field_names = tuple(spec.keys())
        self.fieldNames = self.field_names

        fields_dict: dict[str, TriggerStructField] = {}
        for idx, (fname, fspec) in enumerate(spec.items()):
            mask, shift = _resolve_field_spec(fspec)
            fields_dict[fname] = TriggerStructField(
                name=fname,
                actionSlot=idx,
                epdOffset=idx * ACT_STRIDE_EPDS,
                mask=mask,
                shift=shift,
            )
        self.fields: Mapping[str, TriggerStructField] = MappingProxyType(fields_dict)

    def instantiate(self, *args: Any, **kwargs: Any) -> TriggerStructInstance:
        inits: dict[str, Any] = {}
        if args:
            if len(args) == 1 and isinstance(args[0], (dict, TriggerStructInstance)):
                if isinstance(args[0], TriggerStructInstance):
                    inits = {k: getattr(args[0], k) for k in self.field_names}
                else:
                    inits = dict(args[0])
            else:
                for k, v in zip(self.field_names, args):
                    inits[k] = v
        if kwargs:
            inits.update(kwargs)

        # Contiguous allocation without splitting an instance across pages.
        # Page slots, not raw variable ids, decide the fit: non-page
        # variables consume ids but no page slots.
        ensure_page_fit(self.field_count)

        field_vars: dict[str, Var] = {}
        for fname in self.field_names:
            fmeta = self.fields[fname]
            init_const = 0
            if fname in inits:
                init_val = inits[fname]
                if _is_plain_int(init_val):
                    init_const = (init_val & fmeta.mask) & 0xFFFFFFFF
                elif not isinstance(init_val, (Var, Expr, ReadonlyVar)):
                    raise EudslError(
                        "EUDSL-TYPE",
                        f"struct field init must be a plain integer, got {_describe_value(init_val)}",
                    )
            vid = _alloc_var_slot(init_const, fmeta.mask, "page")
            var_obj = Var(
                init_const, explicit_id=vid, explicit_mask=fmeta.mask, storage="page"
            )
            field_vars[fname] = var_obj

        inst = TriggerStructInstance(self, field_vars)

        # Apply any non-constant inits
        for fname in self.field_names:
            if fname in inits and not _is_plain_int(inits[fname]):
                field_vars[fname].set(inits[fname])

        return inst

    def __call__(self, *args: Any, **kwargs: Any) -> TriggerStructInstance:
        return self.instantiate(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> TriggerStructInstance:
        return self.instantiate(*args, **kwargs)


def TriggerStruct(
    name_or_spec: str | dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
    **kwargs: Any,
) -> TriggerStructSchema:
    name = "TriggerStruct"
    fields_spec: dict[str, Any] = {}
    if isinstance(name_or_spec, str):
        name = name_or_spec
        if spec is not None:
            fields_spec.update(spec)
    elif isinstance(name_or_spec, dict):
        fields_spec.update(name_or_spec)
        if spec is not None:
            fields_spec.update(spec)
    if kwargs:
        fields_spec.update(kwargs)

    return TriggerStructSchema(name, fields_spec)


def lazyStruct(schema: "TriggerStructSchema", *args: Any, **kwargs: Any) -> TriggerStructInstance:
    """Declare a lazy ``TriggerStruct`` instance (globals P2-02): a handle
    constructible anywhere (no builder needed) that resolves to a fully
    eager binding on first resolving use inside ``build_module``, one
    binding per builder. The schema itself is pure and needs no lazy form.

    Init forms mirror the eager ``instantiate`` forms (single dict,
    kwargs, positional values in field order). Only constant-number inits
    are accepted, validated eagerly at the call (design D4): a non-number
    init (another ``Var``, an ``Expr``, a struct instance) raises
    ``EUDSL-TYPE`` here, because a deferred ``copyFrom`` would be
    scope-shifted by a conditional first use, exactly like a deferred
    ``.set()``. Unknown field names raise ``EUDSL-UNKNOWN-FIELD`` eagerly.
    Number masking per field spec happens at binding creation, inside the
    eager ``instantiate`` call with the stored inits — no separate masking
    logic in the handle. Binding creation is atomic: one full eager
    ``instantiate`` (page fit plus ALL K field vars together), so a
    partially-created binding is never observable. A never-used handle
    emits nothing (D5). This factory is the only documented path to a
    deferred struct handle.
    """
    warn_lazy_in_builder()
    inits: dict[str, Any] = {}
    if args:
        if len(args) == 1 and isinstance(args[0], TriggerStructInstance):
            raise EudslError(
                "EUDSL-TYPE",
                "lazyStruct init must be plain numbers; struct instance init "
                "is not supported (a deferred copyFrom would be scope-shifted)",
            )
        if len(args) == 1 and isinstance(args[0], dict):
            inits = dict(args[0])
        else:
            for k, v in zip(schema.field_names, args):
                inits[k] = v
    if kwargs:
        inits.update(kwargs)
    for k, v in inits.items():
        if k not in schema.field_names:
            raise EudslError(
                "EUDSL-UNKNOWN-FIELD",
                f"Unknown field '{k}' on TriggerStruct '{schema.name}'",
            )
        # Same operand classification as the eager branch: ints and floats
        # pass this eager check and are validated by ``instantiate`` at
        # binding creation, so a float/bool init raises ``EUDSL-TYPE``
        # there (ticket #39) instead of writing a truncated constant.
        if not isinstance(v, (int, float)):
            raise EudslError(
                "EUDSL-TYPE",
                f"lazyStruct init for field '{k}' must be a constant number",
            )
    return TriggerStructInstance(schema, deferred=True, inits=inits)


lazy_struct = lazyStruct

defineStruct = TriggerStruct
defineTriggerStruct = TriggerStruct
