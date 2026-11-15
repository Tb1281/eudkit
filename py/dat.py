"""Read/write accessors for the StarCraft Brood War DAT tables.

The canonical layouts live in ``schema/layouts/dat-*.json``. DAT tables are
struct-of-arrays: every field owns an absolute base address, and entry ``i``
lives at ``base + i * stride``. Accessors are therefore constant-index only:
sub-dword fields need a compile-time shift/mask, so a runtime index would
require a branch per byte lane.

``units_dat[UnitType.TerranMarine].maxHp.set(40)`` reuses the same
``MemFieldHandle`` semantics as ``MemView`` (``set``/``iadd``/``isub``/``eq``/
.../``read``). Successful access never allocates storage; a field access is
pure address math, and the emitted action/condition is the same one ``CUnit``
would produce at that absolute EPD.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .stock_rows_built import ACT_FIELD_OFFSETS
from .view import (
    EPD,
    EPD_BASE,
    EudslError,
    FieldSpec,
    MemFieldHandle,
    MemView,
    _describe_value,
    _is_plain_int,
    _locate_schema_file,
    defineMemView,
)


@dataclass(frozen=True)
class DatFieldSpec:
    """One field of a DAT table, as stored in ``schema/layouts/dat-*.json``."""

    base: int
    size: int
    stride: int
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatTableSchema:
    """Parsed DAT table metadata."""

    table: str
    kind: str
    count: int
    fields: Mapping[str, DatFieldSpec]
    names: Mapping[str, int]
    mem_view: MemView


def dat_symbol_identifier(name: str) -> str:
    """Convert a schema display name to a PascalCase identifier."""
    words = [word for word in re.split(r"[^0-9A-Za-z]+", name) if word]
    ident = "".join(word[:1].upper() + word[1:] for word in words)
    if not ident:
        ident = "_"
    if ident[:1].isdigit():
        ident = "_" + ident
    return ident


_RACE_PREFIX = re.compile(r"^(?:Terran|Zerg|Protoss) ")


def dat_symbols_for(names: Mapping[str, int]) -> dict[str, int]:
    """Build the identifier -> entry mapping from a schema's display names.

    Exact names map to a PascalCase identifier. A race-prefixed name also gets
    the prefix-stripped identifier (``Terran Marine`` -> ``Marine``) when that
    alias is unambiguous: it must not collide with a base identifier or with
    another stripped name from a different race.
    """
    symbols: dict[str, int] = {}
    for name, index in names.items():
        ident = dat_symbol_identifier(name)
        existing = symbols.get(ident)
        if existing is not None and existing != index:
            raise ValueError(
                f"DAT symbol collision: '{name}' and another name both map to '{ident}'"
            )
        symbols[ident] = index

    alias_candidates: dict[str, set[str]] = {}
    for name in names:
        stripped = _RACE_PREFIX.sub("", name, count=1)
        if stripped == name:
            continue
        ident = dat_symbol_identifier(stripped)
        if ident in symbols:
            continue
        alias_candidates.setdefault(ident, set()).add(name)
    for ident, candidate_names in alias_candidates.items():
        if len(candidate_names) != 1:
            continue
        name = next(iter(candidate_names))
        symbols[ident] = names[name]
    return symbols


class DatSymbols(Mapping[str, int]):
    """Read-only entry-index namespace supporting ``UnitType.Marine`` and ``["Marine"]``."""

    def __init__(self, values: Mapping[str, int]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> int:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> int:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._values))

    def __repr__(self) -> str:
        return f"DatSymbols({dict(self._values)!r})"


def _normalize_dat_name(name: str) -> str:
    return name.replace("_", "").lower()


def _field_index(schema: DatTableSchema) -> dict[str, tuple[str, DatFieldSpec]]:
    index: dict[str, tuple[str, DatFieldSpec]] = {}
    for name, spec in schema.fields.items():
        item = (name, spec)
        index[name] = item
        index[_normalize_dat_name(name)] = item
        for alias in spec.aliases:
            index[alias] = item
            index[_normalize_dat_name(alias)] = item
    return index


class DatFieldHandle(MemFieldHandle):
    """A field handle bound to one DAT entry.

    The absolute field address is folded into the EPD and byte-lane shift at
    construction time.
    """

    def __init__(
        self,
        schema: DatTableSchema,
        field: str,
        spec: DatFieldSpec,
        index: int,
    ) -> None:
        address = spec.base + index * spec.stride
        epd = EPD(address)
        offset = ((address - EPD_BASE) % 4 + 4) % 4
        super().__init__(
            schema.mem_view,
            FieldSpec(offset=offset, size=spec.size, aliases=spec.aliases),
            epd,
            field,
        )
        self.table = schema
        self.index = index
        self.field = field
        self.address = address

    def set_string(self, fs: Any) -> None:
        """Write ``fs``'s 16-bit wire string id into this 16-bit field.

        ``fs`` is a ``FixedString`` or ``LazyFixedString`` (anything exposing
        an integer ``message_index``). The id is resolved at assembly as
        ``template_count + message_index + 1``, which the frontend cannot
        know. The field's lane mask and bit shift keep the neighbouring
        entry's id intact.
        """
        if self.size != 2:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"set_string() requires a 16-bit field; '{self.field}' is "
                f"{self.size} bytes",
            )
        index = getattr(fs, "message_index", None)
        if not _is_plain_int(index):
            raise EudslError(
                "EUDSL-TYPE",
                "set_string() requires a FixedString with an integer "
                f"message_index, got {_describe_value(fs)}",
            )

        from .hir import SetDeathsX, SetTo
        from .trigger import Trigger

        act = SetDeathsX(self._prepare_target(), SetTo, 0, 0, self.bitMask)
        act.setdefault("relocs", []).append(
            {
                "field_offset": ACT_FIELD_OFFSETS["value"],  # 20
                "target_string": int(index),
                "target_offset": self.shift,  # 0 or 16 — a BIT shift here
                "mode": 6,
            }
        )
        Trigger(conds=[], acts=[act], preserved=True)

    # Python callers get both spellings, mirroring memcpyEPD/memcpy_epd.
    setString = set_string


class DatEntry:
    """One entry of a DAT table.

    Property access resolves a canonical field name, schema alias
    (``verticalOffset``), or snake_case spelling (``max_hp``) to a
    ``DatFieldHandle``. Assigning to a field property calls ``set``.
    """

    def __init__(self, table: "DatTable", index: int) -> None:
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "index", index)

    def field(self, name: str) -> DatFieldHandle:
        resolved = self.table._resolve_field(name)
        if resolved is None:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Unknown field '{name}' on DAT table '{self.table.table}'",
            )
        canonical, spec = resolved
        return DatFieldHandle(self.table._schema, canonical, spec, self.index)

    def __getattr__(self, name: str) -> DatFieldHandle:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self.field(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("table", "index"):
            object.__setattr__(self, name, value)
            return
        self.field(name).set(value)


class DatTable:
    """One DAT table.

    Entry lookup is constant-index only; ``at`` raises ``EUDSL-TARGET`` for a
    non-integer index (for example a ``Var``) and ``EUDSL-BOUNDS`` for an
    out-of-range one.
    """

    def __init__(self, schema: DatTableSchema) -> None:
        self._schema = schema
        self.table = schema.table
        self.kind = "soa"
        self.count = schema.count
        self.fields = schema.fields
        self.names = schema.names
        self.symbols = DatSymbols(dat_symbols_for(schema.names))
        self._index = _field_index(schema)
        self._first_names: dict[int, str] = {}
        for name, index in schema.names.items():
            self._first_names.setdefault(index, name)

    def _resolve_field(self, name: str) -> tuple[str, DatFieldSpec] | None:
        return self._index.get(name) or self._index.get(_normalize_dat_name(name))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise EudslError(
            "EUDSL-BOUNDS",
            f"Unknown property '{name}' on DAT table '{self.table}'",
        )

    def at(self, index: int) -> DatEntry:
        if isinstance(index, bool) or not isinstance(index, int):
            raise EudslError(
                "EUDSL-TARGET",
                f"DAT table '{self.table}' requires a compile-time constant integer index",
            )
        if index < 0 or index >= self.count:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"DAT table '{self.table}' index {index} is outside 0..{self.count - 1}",
            )
        return DatEntry(self, index)

    def __getitem__(self, index: int) -> DatEntry:
        return self.at(index)

    def by_name(self, name: str) -> DatEntry:
        if not isinstance(name, str):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"DAT table '{self.table}' name must be a string",
            )
        index = self.names.get(name)
        if index is None:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Unknown name '{name}' on DAT table '{self.table}'",
            )
        return self.at(index)

    def name_of(self, index: int) -> str | None:
        return self._first_names.get(index)

    # CamelCase spellings mirror the TypeScript DSL.
    byName = by_name
    nameOf = name_of


def _require_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_schema(file_name: str) -> DatTableSchema:
    data = json.loads(_locate_schema_file(file_name).read_text(encoding="utf-8"))
    table = data.get("table")
    if not isinstance(table, str) or not table:
        raise ValueError(f"DAT schema '{file_name}' has no table name")
    if data.get("kind") != "soa":
        raise ValueError(f"DAT schema '{file_name}' must have kind 'soa'")
    count = data.get("count")
    if not _require_int(count) or count <= 0:
        raise ValueError(f"DAT schema '{file_name}' has an invalid count")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        raise ValueError(f"DAT schema '{file_name}' has no fields")
    raw_names = data.get("names")
    if not isinstance(raw_names, dict):
        raise ValueError(f"DAT schema '{file_name}' has no names")

    fields: dict[str, DatFieldSpec] = {}
    for name, raw in raw_fields.items():
        base = raw.get("base") if isinstance(raw, dict) else None
        size = raw.get("size") if isinstance(raw, dict) else None
        stride = raw.get("stride") if isinstance(raw, dict) else None
        if (
            not _require_int(base)
            or base < 0
            or not _require_int(stride)
            or stride < 1
            or not _require_int(size)
            or size not in (1, 2, 4)
        ):
            raise ValueError(
                f"DAT schema '{file_name}' field '{name}' must have base >= 0, "
                "stride >= 1, and size 1/2/4"
            )
        raw_aliases = raw.get("aliases", ())
        aliases = (
            tuple(alias for alias in raw_aliases if isinstance(alias, str))
            if isinstance(raw_aliases, list)
            else ()
        )
        fields[name] = DatFieldSpec(
            base=base, size=size, stride=stride, aliases=aliases
        )

    names: dict[str, int] = {}
    for name, raw_index in raw_names.items():
        if (
            not _require_int(raw_index)
            or raw_index < 0
            or raw_index >= count
        ):
            raise ValueError(
                f"DAT schema '{file_name}' name '{name}' must map to an entry "
                f"in 0..{count - 1}"
            )
        names[name] = raw_index

    return DatTableSchema(
        table=table,
        kind="soa",
        count=count,
        fields=MappingProxyType(fields),
        names=MappingProxyType(names),
        mem_view=defineMemView(table, 4, {}),
    )


def load_dat_table(file_name: str) -> DatTable:
    """Load one ``schema/layouts/<file_name>.json`` DAT table.

    ``file_name`` is the schema basename, e.g. ``dat-trgunit``.
    """
    return DatTable(_load_schema(file_name))


loadDatTable = load_dat_table

#: ``flingy.dat`` / ``dat-flingy.json``.
flingy_dat = load_dat_table("dat-flingy")
#: ``techdata.dat`` / ``dat-tech.json``.
tech_dat = load_dat_table("dat-tech")
#: ``units.dat`` / ``dat-trgunit.json``.
units_dat = load_dat_table("dat-trgunit")
#: ``upgrades.dat`` / ``dat-upgrade.json``.
upgrades_dat = load_dat_table("dat-upgrade")
#: ``weapons.dat`` / ``dat-weapon.json``.
weapons_dat = load_dat_table("dat-weapon")

# CamelCase accessors mirror the TypeScript DSL.
flingyDat = flingy_dat
techDat = tech_dat
unitsDat = units_dat
upgradesDat = upgrades_dat
weaponsDat = weapons_dat

# Entry-index namespaces mirror `FlingyType`/`UnitType`/... in TypeScript.
FlingyType = flingy_dat.symbols
TechType = tech_dat.symbols
UnitType = units_dat.symbols
UpgradeType = upgrades_dat.symbols
WeaponType = weapons_dat.symbols

__all__ = [
    "FlingyType",
    "TechType",
    "UnitType",
    "UpgradeType",
    "WeaponType",
    "DatEntry",
    "DatFieldHandle",
    "DatFieldSpec",
    "DatSymbols",
    "DatTable",
    "DatTableSchema",
    "dat_symbol_identifier",
    "dat_symbols_for",
    "flingyDat",
    "flingy_dat",
    "loadDatTable",
    "load_dat_table",
    "techDat",
    "tech_dat",
    "unitsDat",
    "units_dat",
    "upgradesDat",
    "upgrades_dat",
    "weaponsDat",
    "weapons_dat",
]
