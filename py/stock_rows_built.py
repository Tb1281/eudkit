"""Build the Python stock and eudkit-extension helpers at import time.

`schema/stock-rows.json` is the single source of truth for the 79 stock rows
and the 13 eudkit-extension rows. TypeScript keeps a checked-in generated
module (`ts/stock.generated.ts`); Python has no build step, so this module
constructs the same surface from the spec when `py` is imported. This is the
ADR 0009 asymmetry: no second generated Python artifact is checked in.

Each constructed function carries `__name__`, `__doc__`, and `__signature__`
from its row, so `inspect.signature` still reports the documented ordered
parameters and defaults. Emission validates every parameter in the canonical
`_build_cond`/`_build_act` field order before it builds or queues anything, so
diagnostic labels, codes, and validation order match the TypeScript emitter
and a late failure leaves no partial action behind.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .view import _is_plain_int

SPEC_SECTIONS = ("conditions", "actions", "extensions")
HELPER_SECTIONS = SPEC_SECTIONS

# Canonical condition field order (validation and emission).
COND_FIELD_ORDER = (
    "locid",
    "player",
    "amount",
    "unit",
    "comparison",
    "restype",
    "flags",
    "eudx",
)

# Canonical action field order (validation and emission).
ACT_FIELD_ORDER = (
    "mask",
    "strid",
    "wavid",
    "time",
    "dest",
    "value",
    "unit",
    "modifier",
    "flags",
    "text",
    "wav",
    "uprp",
    "str_index",
    "relocs",
)

# Byte offsets inside a 20-byte HIR condition record.
COND_FIELD_OFFSETS = {
    "locid": 0,
    "player": 4,
    "amount": 8,
    "unit": 12,
    "comparison": 14,
    "condtype": 15,
    "restype": 16,
    "flags": 17,
    "eudx": 18,
}

# Byte offsets inside a 32-byte HIR action record. Mirrors the Rust
# constants `eudkit_lir::DEST_FIELD_OFFSET` (16) and
# `eudkit_lir::VALUE_FIELD_OFFSET` (20); there is no other name for the
# value field (see ADR 0022).
ACT_FIELD_OFFSETS = {
    "mask": 0,
    "strid": 4,
    "wavid": 8,
    "time": 12,
    "dest": 16,
    "value": 20,
    "unit": 24,
    "acttype": 26,
    "modifier": 27,
    "flags": 28,
    "padding": 29,
    "eudx": 30,
}

# Rows whose wire form carries a constant the parameter list cannot name.
IMPLICIT_MODIFIERS = {
    "SetNextPtr": 7,  # SetTo
}

SUPPORTED_ENCODINGS = {
    "u8",
    "u16",
    "u32",
    "u8-truncate",
    "u16-truncate",
    "u32-wrap",
    "mask",
    "epd",
    "epd-from-address",
    "value-or-ptr",
    "ptr",
    "string",
    "string-index",
}

SUPPORTED_VALIDATORS = {"none", "range", "enum", "count"}


def locate_spec() -> Path:
    """Find the stock row spec in the repo tree or an installed package."""
    here = Path(__file__).resolve().parent
    candidates = (
        here / "schema" / "stock-rows.json",
        here.parent / "schema" / "stock-rows.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Stock row spec not found: schema/stock-rows.json")


def load_spec() -> dict[str, Any]:
    return json.loads(locate_spec().read_text(encoding="utf-8"))


def helper_rows(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for section in HELPER_SECTIONS
        for row in spec["sections"][section]
    ]


def _validate_row(row: Mapping[str, Any]) -> None:
    if row["kind"] not in ("condition", "action"):
        raise ValueError(f"stock row spec: {row['id']} is not a trigger row")
    if not isinstance(row.get("chkType"), int):
        raise ValueError(f"stock row spec: {row['id']} has no CHK type")
    if row["special"] != row["handWritten"]:
        raise ValueError(
            f"stock row spec: {row['id']} must mark special and handWritten together"
        )
    order = COND_FIELD_ORDER if row["kind"] == "condition" else ACT_FIELD_ORDER
    seen: set[str] = set()
    saw_default = False
    for param in row["params"]:
        if param["field"] not in order:
            raise ValueError(
                "stock row spec: "
                f"{row['id']}.{param['name']} field {param['field']} has no emit position"
            )
        if param["field"] in seen:
            raise ValueError(
                f"stock row spec: {row['id']} repeats HIR field {param['field']}"
            )
        seen.add(param["field"])
        if param["encoding"] not in SUPPORTED_ENCODINGS:
            raise ValueError(
                "stock row spec: "
                f"{row['id']}.{param['name']} encoding {param['encoding']}"
            )
        validator = param["validator"]
        if validator["kind"] not in SUPPORTED_VALIDATORS:
            raise ValueError(
                "stock row spec: "
                f"{row['id']}.{param['name']} validator {validator['kind']}"
            )
        if validator["kind"] == "range" and "max" not in validator:
            if param["encoding"] != "string-index":
                raise ValueError(
                    "stock row spec: "
                    f"{row['id']}.{param['name']} range validator needs a max"
                )
        if validator["kind"] == "count" and "all" not in validator:
            raise ValueError(
                "stock row spec: "
                f"{row['id']}.{param['name']} count validator needs a sentinel"
            )
        if "default" in param:
            saw_default = True
        elif saw_default:
            raise ValueError(
                f"stock row spec: {row['id']} has a required parameter after a defaulted one"
            )


def validate_spec(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate every non-hand-written row before any helper is built."""
    if spec.get("format") != 1:
        raise ValueError(f"stock row spec: unsupported format {spec.get('format')}")
    rows = helper_rows(spec)
    for row in rows:
        if row["handWritten"]:
            if not row["special"]:
                raise ValueError(
                    f"stock row spec: {row['id']} is hand-written but not special"
                )
            continue
        _validate_row(row)
    ids = {row["id"] for row in rows}
    for row_id in IMPLICIT_MODIFIERS:
        if row_id not in ids:
            raise ValueError(
                f"stock row spec: implicit modifier names no row: {row_id}"
            )
    return rows


def _signature(row: Mapping[str, Any]) -> inspect.Signature:
    parameters = [
        inspect.Parameter(
            param["name"],
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=param.get("default", inspect.Parameter.empty),
        )
        for param in row["params"]
    ]
    return inspect.Signature(parameters)


def _field_offset(row: Mapping[str, Any], field: str) -> int:
    from .view import EudslError

    offsets = (
        COND_FIELD_OFFSETS if row["kind"] == "condition" else ACT_FIELD_OFFSETS
    )
    offset = offsets.get(field)
    if offset is None:
        raise EudslError(
            "EUDSL-TYPE",
            f"{row['id']}: HIR field {field} has no record offset",
        )
    return offset


def _validate_value(validator: Mapping[str, Any], value: Any) -> Any:
    from .hir import _stock_enum, _stock_int

    kind = validator["kind"]
    if kind == "none":
        return value
    if kind == "range":
        return _stock_int(validator["label"], value, validator["min"], validator["max"])
    if kind == "enum":
        return _stock_enum(validator["label"], value, tuple(validator["values"]))
    if kind == "count":
        return (
            validator["all"]
            if _is_plain_int(value) and value == validator["all"]
            else _stock_int(
                validator["label"], value, validator["min"], validator["max"]
            )
        )
    raise AssertionError(f"unsupported stock validator {kind}")


def _resolve_value(
    row: Mapping[str, Any],
    param: Mapping[str, Any],
    raw: Any,
    relocs: list[dict[str, int]],
) -> Any:
    from .hir import (
        DbRef,
        _resolve_epd_dest,
        _resolve_value_or_ptr,
        _unwrap_field_ref,
    )
    from .trigger import Trigger
    from .view import (
        EPD,
        EudslError,
        _describe_value,
        _require_aligned_dword_address,
        _require_plain_int,
    )

    if param["encoding"] == "string-index":
        validator = param["validator"]
        label = (
            validator["label"]
            if validator["kind"] == "range"
            else "string index"
        )
        if not _is_plain_int(raw) or raw < 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"{row['id']}: {label} must be a non-negative integer, got {raw!r}",
            )
        return raw

    value = _validate_value(param["validator"], raw)
    encoding = param["encoding"]
    if encoding == "epd":
        return _resolve_epd_dest(value, _field_offset(row, param["field"]), relocs)
    if encoding == "epd-from-address":
        # Guard *before* `EPD()` folds the address: `EPD` masks any operand
        # into a plausible lane, so the guard downstream never saw the float
        # (cycle 17, ADR 0019 §1). Subject stays `memory address` so the test
        # can prove the guard sits at this site. A `Db`/`DbRef` is already a
        # relocation target (HIR #99), so it bypasses the address fold — its
        # byte offset is already a non-negative multiple of 4 and
        # `lir2payload` starts every payload item at `align4(previous end)`,
        # so the blob payload base is dword-aligned by layout (cycle 26,
        # #120); no address guard is needed there (#111).
        if not isinstance(value, DbRef):
            value = _require_plain_int(value, "memory address")
            _require_aligned_dword_address(value, "memory address")
            value = EPD(value)
        return _resolve_epd_dest(
            value, _field_offset(row, param["field"]), relocs
        )
    if encoding == "value-or-ptr":
        return _resolve_value_or_ptr(
            value, _field_offset(row, param["field"]), relocs
        )
    if encoding == "ptr":
        # Only a `Trigger` (its `.next`) or a handle that unwraps to a
        # `TriggerFieldRef` is legal. Never read `.next` before the type
        # check: `value.next` on a float/str/None raised a raw
        # `AttributeError` and named `undefined` on TS (cycle 17).
        if isinstance(value, Trigger):
            ref = value.next
        else:
            ref = _unwrap_field_ref(value)
            if ref is None:
                raise EudslError(
                    "EUDSL-TYPE",
                    f"src must be a plain integer, got {_describe_value(value)}",
                )
        return _resolve_epd_dest(ref, ACT_FIELD_OFFSETS["dest"], relocs)
    # `mask` / `u32-wrap` are *not* truncated here: `_build_cond` / `_build_act`
    # validate-then-mask every integer field, and this resolver masking first
    # is what let `SetDeathsX(1002, Add, 1, 0, 1.5)` write `mask: 1` (ADR 0019
    # §6, cycle 16). One masking site, after the guard.
    return value


def _collect_values(
    row: Mapping[str, Any],
    args: tuple[Any, ...],
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    from .view import EudslError

    order = COND_FIELD_ORDER if row["kind"] == "condition" else ACT_FIELD_ORDER

    def field_of(param: Mapping[str, Any]) -> str:
        # A `ptr` parameter writes the action destination field even though
        # the spec stores it under `relocs`; resolve it where `dest` is
        # validated so relocations keep their frozen push order.
        return "dest" if param["encoding"] == "ptr" else param["field"]

    by_field: dict[str, tuple[Mapping[str, Any], Any]] = {}
    for index, param in enumerate(row["params"]):
        field = field_of(param)
        if field in by_field:
            raise EudslError(
                "EUDSL-TYPE",
                f"{row['id']}: duplicate HIR field {field}",
            )
        by_field[field] = (param, args[index])
    for field in by_field:
        if field not in order:
            raise EudslError(
                "EUDSL-TYPE",
                f"{row['id']}: HIR field {field} is not an emit field",
            )

    values: dict[str, Any] = {}
    relocs: list[dict[str, int]] = []
    for field in order:
        entry = by_field.get(field)
        if entry is not None:
            param, arg = entry
            values[field] = _resolve_value(row, param, arg, relocs)
    return values, relocs


def _row_eudx(row: Mapping[str, Any]) -> int:
    from .hir import EUDX_MAGIC

    return EUDX_MAGIC if any(p["encoding"] == "mask" for p in row["params"]) else 0


def _emit_condition(row: Mapping[str, Any], args: tuple[Any, ...]) -> dict[str, Any]:
    from .hir import _build_cond

    values, relocs = _collect_values(row, args)
    return _build_cond(
        locid=values.get("locid", 0),
        player=values.get("player", 0),
        amount=values.get("amount", 0),
        unit=values.get("unit", 0),
        comparison=values.get("comparison", 0),
        condtype=row["chkType"],
        restype=values.get("restype", 0),
        flags=values.get("flags", row["defaultFlags"]),
        eudx=values.get("eudx", _row_eudx(row)),
        relocs=relocs,
    )


def _emit_action(row: Mapping[str, Any], args: tuple[Any, ...]) -> dict[str, Any]:
    from .hir import _build_act

    values, relocs = _collect_values(row, args)
    return _build_act(
        mask=values.get("mask", 0),
        strid=values.get("strid", 0),
        wavid=values.get("wavid", 0),
        time=values.get("time", 0),
        dest=values.get("dest", 0),
        value=values.get("value", 0),
        unit=values.get("unit", 0),
        acttype=row["chkType"],
        modifier=values.get("modifier", IMPLICIT_MODIFIERS.get(row["id"], 0)),
        flags=values.get("flags", row["defaultFlags"]),
        eudx=values.get("eudx", _row_eudx(row)),
        text=values.get("text"),
        wav=values.get("wav"),
        str_index=values.get("str_index"),
        relocs=relocs,
    )


def _make_helper(row: Mapping[str, Any]) -> Callable[..., dict[str, Any]]:
    signature = _signature(row)
    emit = _emit_condition if row["kind"] == "condition" else _emit_action

    def helper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        ordered = tuple(
            bound.arguments[param["name"]] for param in row["params"]
        )
        return emit(row, ordered)

    helper.__name__ = row["id"]
    helper.__qualname__ = row["id"]
    helper.__doc__ = row["doc"]
    helper.__signature__ = signature
    return helper


def build_helpers(
    spec: Mapping[str, Any] | None = None,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Validate the spec, then construct every non-special helper."""
    rows = validate_spec(load_spec() if spec is None else spec)
    return {
        row["id"]: _make_helper(row)
        for row in rows
        if not row["handWritten"]
    }
