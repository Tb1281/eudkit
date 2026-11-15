"""`buildModule` compiler entry point, stock conditions, stock actions,
and HIR 3.0 serializer for the Python DSL (`py/`).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .trigger import (
    ACT_COUNT,
    Trigger,
    _get_active_module_builder,
    _set_active_module_builder,
)
from .view import (
    EPD,
    EudslError,
    TriggerFieldRef,
    _build_time_truthiness_error,
    _describe_value,
    _is_plain_int,
    _require_aligned_dword_address,
    _require_plain_int,
)

HIR_VERSION = "3.2"
EUDX_MAGIC = 0x4353

# Player constants
P1 = 0
P2 = 1
P3 = 2
P4 = 3
P5 = 4
P6 = 5
P7 = 6
P8 = 7
P9 = 8
P10 = 9
P11 = 10
P12 = 11

Player1 = 0
Player2 = 1
Player3 = 2
Player4 = 3
Player5 = 4
Player6 = 5
Player7 = 7 - 1
Player8 = 7
Player9 = 8
Player10 = 9
Player11 = 10
Player12 = 11

CurrentPlayer = 13
AllPlayers = 17
Foes = 14
Allies = 15
NeutralPlayers = 16
Force1 = 18
Force2 = 19
Force3 = 20
Force4 = 21
NonAlliedVictoryPlayers = 26

# Comparison constants
AtLeast = 0
AtMost = 1
Exactly = 10

# Modifier constants
SetTo = 7
Add = 8
Subtract = 9

# VarStore relocation descriptors ------------------------------------------


@dataclass(frozen=True)
class AddrReloc:
    """A VarStore destination named by an absolute, 4-byte-aligned address."""

    addr: int


@dataclass(frozen=True)
class TriggerFieldReloc:
    """A VarStore destination named by a dword field inside a trigger record."""

    trigger_id: int
    offset: int = 0


VarStoreTarget = AddrReloc | TriggerFieldReloc

# Stock trigger vocabulary -------------------------------------------------

Anywhere = 64
All = 0

Ore = 0
Gas = 1
OreAndGas = 2

ScoreTotal = 0
ScoreUnits = 1
ScoreBuildings = 2
ScoreUnitsAndBuildings = 3
ScoreKills = 4
ScoreRazings = 5
ScoreKillsAndRazings = 6
ScoreCustom = 7

SwitchSet = 2
SwitchCleared = 3

SwitchActionSet = 4
SwitchActionClear = 5
SwitchActionToggle = 6
SwitchActionRandom = 11

PropEnable = 4
PropDisable = 5
PropToggle = 6

OrderMove = 0
OrderPatrol = 1
OrderAttack = 2

Enemy = 0
Ally = 1
AlliedVictory = 2

_PLAYER_VALUES = (
    P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, P12,
    CurrentPlayer, Foes, Allies, NeutralPlayers, AllPlayers,
    Force1, Force2, Force3, Force4, NonAlliedVictoryPlayers,
)


def _stock_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if not _is_plain_int(value) or not (minimum <= value <= maximum):
        raise EudslError(
            "EUDSL-BOUNDS",
            f"{name} must be an integer in {minimum}..{maximum}, got {value!r}",
        )
    return value


def _stock_enum(name: str, value: int, allowed: tuple[int, ...]) -> int:
    if not _is_plain_int(value) or value not in allowed:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"{name} must be one of {', '.join(map(str, allowed))}, got {value!r}",
        )
    return value


def _stock_player(player: int) -> int:
    return _stock_enum("player", player, _PLAYER_VALUES)


def _stock_location(location: int) -> int:
    return _stock_int("location", location, 0, 255)


def _stock_unit(unit: int) -> int:
    return _stock_int("unit", unit, 0, 227)


PlayerOrFieldTarget = Any
ValueOrPtrTarget = Any


class DbRef:
    """A pointer into a module data blob (`Db`), usable as a memory target or value.

    ``blobId`` is the 0-based module blob index and ``byteOffset`` the byte
    offset inside the blob. ``db + 16`` / ``db.at(16)`` shift the reference.
    ``SetMemory(head, SetTo, db)`` emits an action whose value field relocates
    to the blob's payload address (mode 4); ``SetMemory(db + 0x10, ...)``
    relocates the destination field to the blob's EPD (mode 1).
    """

    blobId: int
    byteOffset: int

    def __init__(self, blob_id: int, byte_offset: int = 0) -> None:
        if not _is_plain_int(blob_id) or blob_id < 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Db blob id must be a non-negative integer, got {blob_id!r}",
            )
        if (
            not _is_plain_int(byte_offset)
            or byte_offset < 0
            or byte_offset % 4 != 0
        ):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Db offset must be a non-negative multiple of 4, got {byte_offset!r}",
            )
        self.blobId = blob_id
        self.byteOffset = byte_offset

    def at(self, byte_offset: int) -> "DbRef":
        """A reference to the 4-byte dword ``byte_offset`` bytes into the blob."""
        if not _is_plain_int(byte_offset):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Db offset must be a non-negative multiple of 4, got {byte_offset!r}",
            )
        return DbRef(self.blobId, self.byteOffset + byte_offset)

    def __add__(self, byte_offset: int) -> "DbRef":
        return self.at(byte_offset)

    def __radd__(self, byte_offset: int) -> "DbRef":
        return self.at(byte_offset)

    def __repr__(self) -> str:
        return f"DbRef(blob={self.blobId}, offset={self.byteOffset})"


class Db(DbRef):
    """A relocatable raw data blob (`Db(bytes | size)`), HIR #99.

    ``Db(16)`` reserves 16 zero-filled bytes; ``Db(b"...")`` stores the bytes
    verbatim. The blob is appended to the map payload (after the trigger pages
    and the demoted Cell blob) and can be pointed at from any action or
    condition field. Must be created inside :func:`buildModule`.
    """

    size: int

    def __init__(
        self,
        payload: int | bytes | bytearray,
        relocs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        builder = _get_active_module_builder()
        if builder is None or not hasattr(builder, "alloc_blob"):
            raise EudslError(
                "EUDSL-SCOPE", "Db() must be called inside buildModule()"
            )
        blob_id, size = builder.alloc_blob(payload, relocs)
        super().__init__(blob_id, 0)
        self.size = size


db = Db


def _emit_blob_epd_into(dst: Var, ref: DbRef) -> None:
    """Write ``ref``'s assembly-time blob EPD into the existing ``dst`` var.

    One preserved zero-condition trigger whose mode-1 relocation writes the
    payload EPD into ``dst``'s value field. Shared by :func:`epd_of_db` (fresh
    Var) and block-memory seeders (caller-owned destination cursor).
    """
    from .stock_rows_built import ACT_FIELD_OFFSETS

    act = dst.act_set(0)
    act.setdefault("relocs", []).append(
        {
            "field_offset": ACT_FIELD_OFFSETS["value"],
            "target_blob": ref.blobId,
            "target_offset": ref.byteOffset,
            "mode": 1,
        }
    )
    Trigger(conds=[], acts=[act], preserved=True)


def epd_of_db(ref: Db | DbRef) -> Var:
    """Return a Var holding the EPD of ``ref``'s payload dword.

    One preserved zero-condition trigger: a mode-1 blob relocation writes
    the EPD into the fresh Var's value field. ``ref`` must already be a
    Db/DbRef (4-byte aligned); ``Db`` means offset 0.
    """
    from .var import Var

    if not isinstance(ref, DbRef):
        raise EudslError(
            "EUDSL-TYPE",
            f"epd_of_db(): expected Db or DbRef, got {_describe_value(ref)}",
        )
    out = Var(0)
    _emit_blob_epd_into(out, ref)
    return out


epdOfDb = epd_of_db


def _normalize_blob_relocs(
    relocs: Sequence[Mapping[str, Any]],
) -> list[dict[str, int]]:
    """Validate author-supplied blob-internal relocations into HIR records.

    Each entry names a 4-byte ``offset`` inside the blob and exactly one
    ``target`` (a :class:`DbRef`/``Db``, a :class:`Trigger`, or explicit
    ``target_blob``/``target_trigger``), plus ``target_offset`` and ``mode``.
    """
    out: list[dict[str, int]] = []
    for reloc in relocs:
        if not isinstance(reloc, Mapping):
            raise EudslError(
                "EUDSL-TYPE", f"Db reloc must be a mapping, got {reloc!r}"
            )
        offset = reloc.get("offset")
        if not _is_plain_int(offset) or offset < 0 or offset % 4 != 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Db reloc offset must be a non-negative multiple of 4, got {offset!r}",
            )
        target_offset = reloc.get("target_offset", 0)
        if not _is_plain_int(target_offset) or target_offset < 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Db reloc target_offset must be a non-negative integer, got {target_offset!r}",
            )
        mode = reloc.get("mode", 4)
        if not _is_plain_int(mode):
            raise EudslError(
                "EUDSL-BOUNDS", f"Db reloc mode must be an integer, got {mode!r}"
            )
        entry: dict[str, int] = {
            "offset": offset,
            "target_offset": target_offset,
            "mode": mode,
        }
        target = reloc.get("target")
        target_blob = reloc.get("target_blob")
        target_trigger = reloc.get("target_trigger")
        if target is not None:
            if isinstance(target, DbRef):
                target_blob = target.blobId
                target_offset += target.byteOffset
                entry["target_offset"] = target_offset
            elif isinstance(target, Trigger):
                target_trigger = target.id
            else:
                raise EudslError(
                    "EUDSL-TYPE",
                    f"Db reloc target must be a Db or Trigger, got {_describe_value(target)}",
                )
        if (target_blob is None) == (target_trigger is None):
            raise EudslError(
                "EUDSL-BOUNDS",
                "Db reloc needs exactly one of target_blob/target_trigger",
            )
        if target_blob is not None:
            if not _is_plain_int(target_blob) or target_blob < 0:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"Db reloc target_blob must be a non-negative integer, got {target_blob!r}",
                )
            entry["target_blob"] = target_blob
        else:
            if not _is_plain_int(target_trigger) or target_trigger < 0:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"Db reloc target_trigger must be a non-negative integer, got {target_trigger!r}",
                )
            entry["target_trigger"] = target_trigger
        out.append(entry)
    return out


def _unwrap_field_ref(v: Any) -> TriggerFieldRef | None:
    if isinstance(v, TriggerFieldRef):
        return v
    val_attr = getattr(v, "value", None)
    if isinstance(val_attr, TriggerFieldRef):
        return val_attr
    return None


def _resolve_epd_dest(
    target: PlayerOrFieldTarget,
    field_offset: int,
    relocs: list[dict[str, int]],
) -> int:
    ref = _unwrap_field_ref(target)
    if ref is not None:
        if ref.varId is not None:
            relocs.append(
                {
                    "field_offset": field_offset,
                    "target_var": ref.varId,
                    "target_offset": ref.alignedByteOffset,
                    "mode": 1,
                }
            )
        else:
            relocs.append(
                {
                    "field_offset": field_offset,
                    "target_trigger": ref.triggerId,
                    "target_offset": ref.alignedByteOffset,
                    "mode": 1,
                }
            )
        return 0
    if isinstance(target, DbRef):
        relocs.append(
            {
                "field_offset": field_offset,
                "target_blob": target.blobId,
                "target_offset": target.byteOffset,
                "mode": 1,
            }
        )
        return 0
    # A relocation form is consumed above; what is left is a plain integer
    # address or nothing (ADR 0019 §1, cycle 16). `int()` here silently
    # truncated floats, numeric strings, and bools.
    return _require_plain_int(target, "memory destination") & 0xFFFFFFFF


def _resolve_value_or_ptr(
    val: ValueOrPtrTarget,
    field_offset: int,
    relocs: list[dict[str, int]],
) -> int:
    ref = _unwrap_field_ref(val)
    if ref is not None:
        if ref.varId is not None:
            relocs.append(
                {
                    "field_offset": field_offset,
                    "target_var": ref.varId,
                    "target_offset": ref.byteOffset,
                    "mode": 4,
                }
            )
        else:
            relocs.append(
                {
                    "field_offset": field_offset,
                    "target_trigger": ref.triggerId,
                    "target_offset": ref.byteOffset,
                    "mode": 4,
                }
            )
        return 0
    if isinstance(val, Trigger):
        relocs.append(
            {
                "field_offset": field_offset,
                "target_trigger": val.id,
                "target_offset": 0,
                "mode": 4,
            }
        )
        return 0
    if isinstance(val, DbRef):
        relocs.append(
            {
                "field_offset": field_offset,
                "target_blob": val.blobId,
                "target_offset": val.byteOffset,
                "mode": 4,
            }
        )
        return 0
    return _require_plain_int(val, "memory value") & 0xFFFFFFFF


class HirCond(dict):
    """A HIR condition record — a runtime condition, never a Python bool.

    It subclasses `dict` because every cond consumer (raw `Trigger`,
    `_normalize_conds`, `json`) already passes plain cond dicts around, so the
    public dict API must stay unchanged (`cond["condtype"]`, `dict(cond)`,
    `json.dumps(cond)`). Only truthiness is refused: `if`/`while`/`assert` on
    one would pick a branch while `buildModule` runs, which is never what the
    author meant (ADR 0020).
    """

    def __bool__(self) -> bool:
        raise _build_time_truthiness_error("condition")


# The integer field tuple for each HIR record — one explicit `(name, mask)` per
# field, so "which fields are in the operand domain" is a list a reader can
# count, not an inline expression list that drifts. `text`/`wav`/`uprp`/
# `str_index`/`relocs` are not integer operands and stay out (ADR 0019 §6).
COND_INT_FIELDS: tuple[tuple[str, int], ...] = (
    ("locid", 0xFFFFFFFF),
    ("player", 0xFFFFFFFF),
    ("amount", 0xFFFFFFFF),
    ("unit", 0xFFFF),
    ("comparison", 0xFF),
    ("condtype", 0xFF),
    ("restype", 0xFF),
    ("flags", 0xFF),
    ("eudx", 0xFFFF),
)

ACT_INT_FIELDS: tuple[tuple[str, int], ...] = (
    ("mask", 0xFFFFFFFF),
    ("strid", 0xFFFFFFFF),
    ("wavid", 0xFFFFFFFF),
    ("time", 0xFFFFFFFF),
    ("dest", 0xFFFFFFFF),
    ("value", 0xFFFFFFFF),
    ("unit", 0xFFFF),
    ("acttype", 0xFF),
    ("modifier", 0xFF),
    ("flags", 0xFF),
    ("padding", 0xFF),
    ("eudx", 0xFFFF),
)


def _masked_int_fields(
    record: str,
    fields: tuple[tuple[str, int], ...],
    values: Mapping[str, Any],
) -> dict[str, int]:
    """Validate every integer field, then mask it.

    The record builders are the single chokepoint every death-table and
    raw-memory constructor converges on (the hand-written rows and the
    generated stock rows alike), so this is the one place a mask can be
    applied only to a validated integer. Validate first: `1.5 & 0xFF` is a raw
    `TypeError`, and `>>> 0` would silently write `1`.
    """
    masked: dict[str, int] = {}
    for name, mask in fields:
        value = values[name]
        if not _is_plain_int(value):
            raise EudslError(
                "EUDSL-TYPE",
                f"{record} field {name} must be a plain integer; "
                f"got {_describe_value(value)}",
            )
        masked[name] = value & mask
    return masked


def _build_cond(
    *,
    locid: int = 0,
    player: int = 0,
    amount: int = 0,
    unit: int = 0,
    comparison: int = 0,
    condtype: int,
    restype: int = 0,
    flags: int = 0,
    eudx: int = 0,
    relocs: list[dict[str, int]] | None = None,
) -> "HirCond":
    cond: dict[str, Any] = _masked_int_fields(
        "condition",
        COND_INT_FIELDS,
        {
            "locid": locid,
            "player": player,
            "amount": amount,
            "unit": unit,
            "comparison": comparison,
            "condtype": condtype,
            "restype": restype,
            "flags": flags,
            "eudx": eudx,
        },
    )
    if relocs:
        cond["relocs"] = relocs
    return HirCond(cond)


def _build_act(
    *,
    mask: int = 0,
    strid: int = 0,
    wavid: int = 0,
    time: int = 0,
    dest: int = 0,
    value: int = 0,
    unit: int = 0,
    acttype: int,
    modifier: int = 0,
    flags: int = 0,
    padding: int = 0,
    eudx: int = 0,
    text: str | None = None,
    wav: str | None = None,
    uprp: list[int] | None = None,
    str_index: int | None = None,
    relocs: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    act: dict[str, Any] = _masked_int_fields(
        "action",
        ACT_INT_FIELDS,
        {
            "mask": mask,
            "strid": strid,
            "wavid": wavid,
            "time": time,
            "dest": dest,
            "value": value,
            "unit": unit,
            "acttype": acttype,
            "modifier": modifier,
            "flags": flags,
            "padding": padding,
            "eudx": eudx,
        },
    )
    if text is not None:
        act["text"] = text
    if wav is not None:
        act["wav"] = wav
    if uprp is not None:
        act["uprp"] = uprp
    if str_index is not None:
        act["str_index"] = int(str_index)
    if relocs:
        act["relocs"] = relocs

    builder = _get_active_module_builder()
    if builder is not None and hasattr(builder, "queue_act"):
        builder.queue_act(act)
    return act


# ---------------------------------------------------------------------------
# Special rows (the only hand-written helper bodies)
#
# Every other stock and eudkit-extension helper is built from
# `schema/stock-rows.json` at the bottom of this module.
# ---------------------------------------------------------------------------


def Deaths(
    player: PlayerOrFieldTarget,
    comparison: int,
    amount: ValueOrPtrTarget,
    unit: int = 0,
) -> dict[str, Any]:
    relocs: list[dict[str, int]] = []
    p = _resolve_epd_dest(player, 4, relocs)
    a = _resolve_value_or_ptr(amount, 8, relocs)
    return _build_cond(
        player=p,
        amount=a,
        unit=unit,
        comparison=comparison,
        condtype=15,
        relocs=relocs,
    )


# ---------------------------------------------------------------------------
# Special rows, continued
# ---------------------------------------------------------------------------


def SetDeaths(
    player: PlayerOrFieldTarget,
    modifier: int,
    value: ValueOrPtrTarget,
    unit: int = 0,
) -> dict[str, Any]:
    relocs: list[dict[str, int]] = []
    dest = _resolve_epd_dest(player, 16, relocs)
    val = _resolve_value_or_ptr(value, 20, relocs)
    return _build_act(
        dest=dest,
        value=val,
        unit=unit,
        acttype=45,
        modifier=modifier,
        flags=20,
        relocs=relocs,
    )


def SetStringDword(
    index: int,
    byte_offset: int,
    value: int,
    mask: int = 0xFFFFFFFF,
    modifier: int = SetTo,
) -> dict[str, Any]:
    """Write one 32-bit word of a module string buffer through its StrAddr."""
    if not _is_plain_int(index) or index < 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            f"SetStringDword: string index must be a non-negative integer, got {index!r}",
        )
    if not _is_plain_int(byte_offset) or byte_offset < 0 or (byte_offset & 3) != 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            "SetStringDword: byte offset must be a non-negative multiple of 4, "
            f"got {byte_offset!r}",
        )
    # Validate then mask at the one site (ADR 0019 §6, cycle 17): masking
    # first below turned `value=1.5` into `value: 1`, `mask=1.5` into
    # `mask: 1, eudx: 17235` (a different act classification), and
    # `modifier=True` into `modifier: 1`.
    value = _require_plain_int(value, "SetStringDword value")
    mask = _require_plain_int(mask, "SetStringDword mask")
    modifier = _require_plain_int(modifier, "SetStringDword modifier")
    m = mask & 0xFFFFFFFF
    return _build_act(
        mask=0 if m == 0xFFFFFFFF else m,
        value=value & 0xFFFFFFFF,
        acttype=45,
        modifier=modifier & 0xFF,
        flags=20,
        eudx=0 if m == 0xFFFFFFFF else EUDX_MAGIC,
        relocs=[
            {
                "field_offset": 16,
                "target_string": int(index),
                "target_offset": int(byte_offset),
                "mode": 1,
            }
        ],
    )


def _optional_unit_property_int(
    name: str, value: int | None, minimum: int, maximum: int
) -> int | None:
    return None if value is None else _stock_int(name, value, minimum, maximum)


def _optional_unit_property_bool(name: str, value: bool | None) -> bool | None:
    if value is None:
        return None
    if value is not True and value is not False:
        raise EudslError(
            "EUDSL-TYPE",
            f"{name} must be True, False, or None, got {value!r}",
        )
    return value


def UnitProperty(
    hit_points: int | None = None,
    shields: int | None = None,
    energy: int | None = None,
    resource: int | None = None,
    hangar: int | None = None,
    cloaked: bool | None = None,
    burrowed: bool | None = None,
    in_transit: bool | None = None,
    hallucinated: bool | None = None,
    invincible: bool | None = None,
) -> list[int]:
    """Build the normalized 20-byte UPRP record used by action 11.

    ``hit_points``, ``shields``, and ``energy`` are percentages (0..100).
    ``resource`` is 0..65535, ``hangar`` is 0..255, and the five special
    flags accept ``True``, ``False``, or ``None``. Normalization follows
    eudplib's ``property_key``: 100% HP/shields and a 0 hangar mean
    "unset", and an explicit ``False`` flag is equivalent to unset.
    """
    hp = _optional_unit_property_int("hit points", hit_points, 0, 100)
    sh = _optional_unit_property_int("shields", shields, 0, 100)
    en = _optional_unit_property_int("energy", energy, 0, 100)
    res = _optional_unit_property_int("resource", resource, 0, 65535)
    han = _optional_unit_property_int("hangar", hangar, 0, 255)
    cloak = _optional_unit_property_bool("cloaked", cloaked)
    burrow = _optional_unit_property_bool("burrowed", burrowed)
    transit = _optional_unit_property_bool("in transit", in_transit)
    halluc = _optional_unit_property_bool("hallucinated", hallucinated)
    invinc = _optional_unit_property_bool("invincible", invincible)

    prpvalid = 0
    if hp is not None:
        prpvalid |= 1 << 1
    if sh is not None:
        prpvalid |= 1 << 2
    if en is not None:
        prpvalid |= 1 << 3
    if res is not None:
        prpvalid |= 1 << 4
    if han is not None:
        prpvalid |= 1 << 5

    sprpvalid = 0
    if cloak is not None:
        sprpvalid |= 1 << 0
    if burrow is not None:
        sprpvalid |= 1 << 1
    if transit is not None:
        sprpvalid |= 1 << 2
    if halluc is not None:
        sprpvalid |= 1 << 3
    if invinc is not None:
        sprpvalid |= 1 << 4

    sprpflag = 0
    if cloak:
        sprpflag |= 1 << 0
    if burrow:
        sprpflag |= 1 << 1
    if transit:
        sprpflag |= 1 << 2
    if halluc:
        sprpflag |= 1 << 3
    if invinc:
        sprpflag |= 1 << 4

    # eudplib ``property_key`` normalization.
    if hp == 100:
        prpvalid &= ~(1 << 1)
        hp = 0
    if sh == 100:
        prpvalid &= ~(1 << 2)
        sh = 0
    if han == 0:
        prpvalid &= ~(1 << 5)
    if prpvalid & (1 << 1) == 0:
        hp = 0
    if prpvalid & (1 << 2) == 0:
        sh = 0
    if prpvalid & (1 << 3) == 0:
        en = 0
    if prpvalid & (1 << 4) == 0:
        res = 0
    if prpvalid & (1 << 5) == 0:
        han = 0
    sprpvalid &= sprpflag
    sprpflag &= sprpvalid

    res = res or 0
    han = han or 0
    return [
        sprpvalid & 0xFF,
        (sprpvalid >> 8) & 0xFF,
        prpvalid & 0xFF,
        (prpvalid >> 8) & 0xFF,
        0,  # player: always NULL
        hp or 0,
        sh or 0,
        en or 0,
        res & 0xFF,
        (res >> 8) & 0xFF,
        (res >> 16) & 0xFF,
        (res >> 24) & 0xFF,
        han & 0xFF,
        (han >> 8) & 0xFF,
        sprpflag & 0xFF,
        (sprpflag >> 8) & 0xFF,
        0,
        0,
        0,
        0,
    ]


def CreateUnitWithProperties(
    count: int,
    unit: int,
    location: int,
    player: int,
    properties: list[int],
) -> dict[str, Any]:
    """Create ``count`` units with a :func:`UnitProperty` record (type 11).

    The action's 1-based property index is assigned during map assembly,
    after UPRP slot allocation.
    """
    if not isinstance(properties, list) or len(properties) != 20:
        raise EudslError(
            "EUDSL-TYPE",
            "properties must be a 20-byte UnitProperty record",
        )
    for byte in properties:
        if not _is_plain_int(byte) or not 0 <= byte <= 255:
            raise EudslError(
                "EUDSL-TYPE",
                f"properties bytes must be integers in 0..255, got {byte!r}",
            )
    return _build_act(
        mask=_stock_location(location),
        dest=_stock_player(player),
        value=0,
        unit=_stock_unit(unit),
        acttype=11,
        modifier=_stock_int("count", count, 1, 255),
        flags=28,
        uprp=properties,
    )


# ---------------------------------------------------------------------------
# buildModule & HIR 3.0 Serialization
# ---------------------------------------------------------------------------


def _trigger_to_hir_node(t: Trigger) -> dict[str, Any]:
    node: dict[str, Any] = {
        "kind": "trigger",
        "id": t.id,
        "conds": list(t.conds),
        "acts": list(t.acts),
        "preserved": t.preserved,
        "flags": t.flags,
    }
    if t.prevPtr is not None:
        node["prev"] = t.prevPtr
    if t.nextPtr is not None:
        node["next"] = t.nextPtr
    return node


class CompiledModule:
    """Compiled HIR 3.0 module produced by `buildModule`."""

    def __init__(
        self,
        triggers: Sequence[Trigger],
        vars_decl: Sequence[Mapping[str, Any]] = (),
        stmts: Sequence[Mapping[str, Any]] | None = None,
        strings: Sequence[str] = (),
        files: Sequence[Mapping[str, str]] = (),
        funcs: Sequence[Mapping[str, Any]] = (),
        blobs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.triggers: tuple[Trigger, ...] = tuple(triggers)
        self.vars: tuple[Mapping[str, Any], ...] = tuple(vars_decl)
        self.files: tuple[Mapping[str, str], ...] = tuple(files)
        self.funcs: tuple[Mapping[str, Any], ...] = tuple(funcs)
        self.blobs: tuple[Mapping[str, Any], ...] = tuple(blobs)
        final_stmts: list[dict[str, Any]] = (
            [dict(s) for s in stmts]
            if stmts is not None
            else [_trigger_to_hir_node(t) for t in self.triggers]
        )
        mod_obj: dict[str, Any] = {}
        if self.vars:
            mod_obj["vars"] = [dict(v) for v in self.vars]
        mod_obj["stmts"] = final_stmts
        if strings:
            mod_obj["strings"] = list(strings)
        if self.files:
            mod_obj["files"] = [
                {"name": f["name"], "path": f["path"]} for f in self.files
            ]
        if self.funcs:
            mod_obj["funcs"] = [dict(f) for f in self.funcs]
        if self.blobs:
            mod_obj["blobs"] = [dict(b) for b in self.blobs]
        self._doc: dict[str, Any] = {
            "version": HIR_VERSION,
            "module": mod_obj,
        }

    def toHir(self) -> dict[str, Any]:
        return self._doc

    def to_hir(self) -> dict[str, Any]:
        return self._doc

    def toDict(self) -> dict[str, Any]:
        return self._doc

    def to_dict(self) -> dict[str, Any]:
        return self._doc

    def toJson(self) -> str:
        return json.dumps(self._doc, separators=(",", ":"), ensure_ascii=False)

    def to_json(self) -> str:
        return self.toJson()


def _normalize_var_store_target(
    target: VarStoreTarget | Mapping[str, Any],
) -> Mapping[str, Any]:
    """Lower a relocation descriptor to the legacy HIR target object.

    Plain mappings keep the un-migrated call sites on the legacy path and are
    returned untouched; a mapping is never reinterpreted as a descriptor.
    """
    if isinstance(target, AddrReloc):
        return {"addr": target.addr}
    if isinstance(target, TriggerFieldReloc):
        return {"trigger_id": target.trigger_id, "offset": target.offset}
    if isinstance(target, Mapping):
        return target
    raise EudslError(
        "EUDSL-TYPE",
        "VarStore target must be AddrReloc, TriggerFieldReloc, or a mapping, "
        f"got {_describe_value(target)}",
    )


class _ModuleBuilderState:
    """Builder seam implementation (candidate 8, shape C).

    This class OWNS every piece of builder state -- trigger/var/page
    counters, the scope serial, the scope stack, and the flusher list --
    and is installed as the active builder for the duration of
    ``buildModule``. ``trigger.py`` holds just the active-builder slot;
    see its contract comment for the ordering guarantees.

    - Flushers run before every scope change (``push_scope`` AND
      ``pop_scope``) and once more at module end; ``scope_serial`` bumps
      at every scope change. Scope-boundary flushes call each flusher
      with ``align=True``; the module-end flush calls it with
      ``align=False``.
    - ``_flushers`` is a list: registering the same function twice runs
      it twice per flush. The TypeScript mirror uses a set (runs once);
      the seam tests pin that divergence as-is instead of unifying it.
    - ``pop_scope`` guards the root scope (returns ``[]`` and keeps the
      root); the TypeScript mirror pops unconditionally. Balanced
      temporal scopes never pop the root, so the divergence is
      unreachable in practice (pinned as-is by the seam tests).
    """

    def __init__(self) -> None:
        self.next_id = 0
        self.next_var_id = 0
        self.next_page_slot = 0
        self.next_func_id = 0
        self.scope_serial = 0
        self.vars: list[dict[str, Any]] = []
        self.strings: list[str] = []
        self.files: list[dict[str, str]] = []
        self.funcs: list[dict[str, Any]] = []
        self.blobs: list[dict[str, Any]] = []
        self.scope_stack: list[list[tuple[str, Any]]] = [[]]
        self._flushers: list[Callable[[bool], None]] = []

    @property
    def entries(self) -> list[tuple[str, Any]]:
        return self.scope_stack[-1]

    def register_flusher(self, fn: Callable[[bool], None]) -> None:
        self._flushers.append(fn)

    def flush_all(self, align: bool) -> None:
        for flush in list(self._flushers):
            flush(align)

    def push_scope(self) -> None:
        # Align before a branch so its skipped path and its taken path share a
        # lane; the module-end flush stays unaligned because no run follows it.
        self.flush_all(True)
        self.scope_serial += 1
        self.scope_stack.append([])

    def pop_scope(self) -> list[tuple[str, Any]]:
        self.flush_all(True)
        self.scope_serial += 1
        return self.scope_stack.pop() if len(self.scope_stack) > 1 else []

    def queue_stmt(self, stmt: Mapping[str, Any]) -> None:
        self.entries.append(("stmt", stmt))

    def flush_entries(self, entries: Sequence[tuple[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for kind, obj in entries:
            if kind == "act":
                out.append({"kind": "act", **dict(obj)})
            elif kind == "trigger":
                out.append(_trigger_to_hir_node(obj))
            elif kind in ("vartransfer", "varstore"):
                out.append(dict(obj))
            elif kind == "stmt":
                out.append(dict(obj))
        return out

    def alloc_trigger_id(self) -> int:
        tid = self.next_id
        self.next_id += 1
        return tid

    def alloc_var(
        self, init: int, mask: int = 0xFFFFFFFF, storage: str | None = None
    ) -> int:
        vid = self.next_var_id
        self.next_var_id += 1
        decl: dict[str, Any] = {
            "id": vid,
            "init": int(init) & 0xFFFFFFFF,
        }
        if (mask & 0xFFFFFFFF) != 0xFFFFFFFF:
            decl["mask"] = mask & 0xFFFFFFFF
        if storage is not None:
            decl["storage"] = storage
        if storage == "page":
            self.next_page_slot += 1
        self.vars.append(decl)
        return vid

    def alloc_string(self, text: str) -> int:
        index = len(self.strings)
        self.strings.append(text)
        return index

    def embed_file(self, name: str, path: str) -> None:
        if not isinstance(name, str) or not name:
            raise EudslError(
                "EUDSL-BOUNDS",
                "embedFile: embedded file name must be a non-empty string",
            )
        if not isinstance(path, str) or not path:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"embedFile: embedded file {name!r} must have a non-empty path",
            )
        for existing in self.files:
            if existing["name"] != name:
                continue
            if existing["path"] == path:
                return
            raise EudslError(
                "EUDSL-BOUNDS", f"duplicate embedded file name {name!r}"
            )
        self.files.append({"name": name, "path": path})

    def alloc_blob(
        self,
        payload: int | bytes | bytearray,
        relocs: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[int, int]:
        """Declare a raw data blob; returns ``(blob_id, byte_size)``.

        ``payload`` is either a non-negative byte size (zero-filled) or a
        ``bytes``/``bytearray`` payload (base64-encoded into the HIR).
        """
        if _is_plain_int(payload):
            if payload < 0:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"Db size must be a non-negative integer, got {payload!r}",
                )
            size = payload
            data = ""
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            raw = bytes(payload)
            size = len(raw)
            data = base64.b64encode(raw).decode("ascii")
        else:
            raise EudslError(
                "EUDSL-TYPE",
                f"Db payload must be a byte size or bytes, got {_describe_value(payload)}",
            )
        decl: dict[str, Any] = {"size": size}
        if data:
            decl["data"] = data
        normalized = _normalize_blob_relocs(relocs)
        if normalized:
            decl["relocs"] = normalized
        blob_id = len(self.blobs)
        self.blobs.append(decl)
        return blob_id, size

    def get_next_string_index(self) -> int:
        return len(self.strings)

    def alloc_string_var(
        self, index: int, offset: int = 0, mask: int = 0xFFFFFFFF
    ) -> int:
        if not _is_plain_int(index) or index < 0:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"alloc_string_var: string index must be a non-negative integer, got {index!r}",
            )
        vid = self.next_var_id
        self.next_var_id += 1
        decl: dict[str, Any] = {
            "id": vid,
            "init": 0,
            "storage": "page",
            "init_string": {"index": int(index), "offset": int(offset) & 0xFFFFFFFF},
        }
        if (mask & 0xFFFFFFFF) != 0xFFFFFFFF:
            decl["mask"] = mask & 0xFFFFFFFF
        self.next_page_slot += 1
        self.vars.append(decl)
        return vid

    def get_scope_serial(self) -> int:
        return self.scope_serial

    def get_next_var_id(self) -> int:
        return self.next_var_id

    def get_next_page_slot(self) -> int:
        return self.next_page_slot

    def queue_act(self, act: Mapping[str, Any]) -> None:
        self.entries.append(("act", act))

    def queue_var_transfer(self, dst_var: int, src_var: int, modifier: int) -> None:
        self.entries.append(
            (
                "vartransfer",
                {
                    "kind": "vartransfer",
                    "dst_var": dst_var,
                    "src_var": src_var,
                    "modifier": modifier & 0xFF,
                },
            )
        )

    def queue_var_store(
        self,
        src_var: int,
        target: VarStoreTarget | Mapping[str, Any],
        modifier: int,
    ) -> None:
        target = _normalize_var_store_target(target)
        node: dict[str, Any] = {
            "kind": "varstore",
            "src_var": src_var,
            "modifier": modifier & 0xFF,
        }
        if "addr" in target:
            addr = _require_plain_int(target["addr"], "VarStore address") & 0xFFFFFFFF
            # `Db`/`DbRef` never reaches here (it is a `dst_trigger` field
            # relocation), and a `trigger_id` target is a field inside a
            # trigger, not an absolute address — so this is the only place a
            # VarStore names a dword by byte address (#111).
            _require_aligned_dword_address(addr, "VarStore address")
            node["dst_addr"] = addr
        else:
            dst_trigger = _require_plain_int(
                target.get("trigger_id"), "VarStore trigger_id"
            )
            dst_offset = _require_plain_int(target.get("offset", 0), "VarStore offset")
            if dst_offset % 4:
                raise EudslError(
                    "EUDSL-BOUNDS",
                    f"VarStore offset {dst_offset} is not a multiple of 4",
                )
            node["dst_trigger"] = dst_trigger
            node["dst_offset"] = dst_offset
        self.entries.append(("varstore", node))

    def alloc_func_id(self) -> int:
        fid = self.next_func_id
        self.next_func_id += 1
        return fid

    def queue_func(self, func: Mapping[str, Any]) -> None:
        self.funcs.append(dict(func))

    def queue_call(
        self, func_id: int, args: Sequence[Mapping[str, Any]], dst_var: int | None
    ) -> None:
        node: dict[str, Any] = {
            "kind": "call",
            "func": int(func_id),
            "args": [dict(a) for a in args],
        }
        if dst_var is not None:
            node["dst"] = int(dst_var)
        self.entries.append(("stmt", node))

    def capture_entries(self, thunk: Callable[[], None]) -> list[dict[str, Any]]:
        """Run ``thunk`` in an isolated root scope and return its statements.

        Subroutine bodies are ordinary statement lists -- they may open
        ``when``/``while`` scopes and register flushers -- so the capture
        swaps in a fresh scope stack and flusher list, runs the body, then
        restores both. The collected entries go through the same act
        bin-packing as a module root, but are returned instead of installed.
        """
        saved_scope = self.scope_stack
        saved_flushers = self._flushers
        self.scope_stack = [[]]
        self._flushers = []
        try:
            thunk()
        finally:
            entries = self.scope_stack[0]
            self.scope_stack = saved_scope
            self._flushers = saved_flushers
        _, stmts = _finalize_root_entries(self, entries)
        return stmts

    def consume_queued_acts(self, acts: Sequence[Mapping[str, Any]]) -> None:
        if not acts:
            return
        to_consume_ids = {id(a) for a in acts}
        entries = self.entries
        for idx in range(len(entries) - 1, -1, -1):
            if not to_consume_ids:
                break
            kind, obj = entries[idx]
            if kind == "act" and id(obj) in to_consume_ids:
                to_consume_ids.remove(id(obj))
                entries.pop(idx)

    def register_trigger(self, trig: Trigger) -> None:
        self.entries.append(("trigger", trig))


def emitVarStore(
    src_var: int,
    target: VarStoreTarget | Mapping[str, Any],
    modifier: int = SetTo,
) -> None:
    """Store a variable's value into an arbitrary 4-byte memory location.

    ``target`` is an :class:`AddrReloc` (a fixed byte address, e.g. the
    ``CurrentPlayer`` slot) or a :class:`TriggerFieldReloc` (a field inside a
    trigger, e.g. a condition's ``player`` field). The legacy mapping forms
    ``{"addr": ...}`` / ``{"trigger_id": ..., "offset": ...}`` remain accepted
    for un-migrated call sites. This is the runtime primitive behind
    dynamic-EPD indirection; scalar variables are copied via their
    ``TriggerPage`` slot.

    Both numeric forms are validated eagerly at the call: ``addr``,
    ``trigger_id`` and ``offset`` must be plain integers, an ``addr`` must be
    4-byte aligned, and a trigger-field ``offset`` (default ``0``) must be a
    multiple of 4 because the write is a whole dword.
    """
    builder = _get_active_module_builder()
    if builder is None or not hasattr(builder, "queue_var_store"):
        raise EudslError(
            "EUDSL-SCOPE", "emitVarStore() must be called inside buildModule()"
        )
    builder.queue_var_store(src_var, target, modifier & 0xFF)


def embedFile(name: str, path: str) -> None:
    """Queue a build-time file to be added to the output MPQ under ``name``.

    ``name`` is the archived file name (e.g. ``"bgm"``); ``path`` is authored
    as written, and the build resolves relative paths against the directory of
    the compiling source. The same ``(name, path)`` pair may be requested more
    than once (it is queued once); a different path for an existing ``name``
    is an error. This is the general MPQ embed surface shared by helpers such
    as :func:`eudkit.bgm.playBgm` and the dataDumper.
    """
    builder = _get_active_module_builder()
    if builder is None or not hasattr(builder, "embed_file"):
        raise EudslError(
            "EUDSL-SCOPE", "embedFile() must be called inside buildModule()"
        )
    builder.embed_file(name, path)


embed_file = embedFile


def _finalize_root_entries(
    state: _ModuleBuilderState,
    root_entries: Sequence[tuple[str, Any]],
) -> tuple[list[Trigger], list[dict[str, Any]]]:
    """Flatten a root entry list into triggers + HIR statements.

    Adjacent top-level actions are bin-packed into `Trigger (16C, 64A)`
    nodes with fresh ids. Shared by `buildModule` (the module root) and
    subroutine body capture, so both surfaces produce the same shape.
    """
    final_triggers: list[Trigger] = []
    final_stmts: list[dict[str, Any]] = []
    pending_acts: list[Mapping[str, Any]] = []

    def flush_pending() -> None:
        if not pending_acts:
            return
        for i in range(0, len(pending_acts), ACT_COUNT):
            chunk = pending_acts[i : i + ACT_COUNT]
            # Finalization may run while a builder is active (subroutine body
            # capture); suppress it so the packed trigger is not registered
            # into the current scope and does not consume queued acts from a
            # list being iterated. The id still comes from the module counter.
            prev = _set_active_module_builder(None)
            try:
                trig = Trigger(
                    conds=[],
                    acts=chunk,
                    preserved=True,
                    id=state.alloc_trigger_id(),
                    register=False,
                )
            finally:
                _set_active_module_builder(prev)
            final_triggers.append(trig)
            final_stmts.append(_trigger_to_hir_node(trig))
        pending_acts.clear()

    for kind, obj in root_entries:
        if kind == "act":
            pending_acts.append(obj)
        elif kind == "trigger":
            if pending_acts:
                flush_pending()
            final_triggers.append(obj)
            final_stmts.append(_trigger_to_hir_node(obj))
        elif kind in ("vartransfer", "varstore"):
            if pending_acts:
                flush_pending()
            final_stmts.append(dict(obj))
        else:
            if pending_acts:
                flush_pending()
            final_stmts.append(dict(obj))
    if pending_acts:
        flush_pending()
    return final_triggers, final_stmts


def buildModule(fn: Callable[[], Any]) -> CompiledModule:
    """Compile a top-level EUD module body into a canonical HIR 3.0 `CompiledModule`.
    Adjacent unconditional top-level actions are automatically bin-packed up to
    64 actions per `Trigger (16C, 64A)` before opening a new `Trigger`.
    """
    # Single entry point (user rule, 2026-11-04): one build_module per
    # map, like Rust main. A nested call throws instead of saving and
    # restoring a previous builder; there is no inner-builder case anymore.
    if _get_active_module_builder() is not None:
        raise EudslError(
            "EUDSL-SCOPE",
            "build_module() must not be called inside build_module() "
            "(single entry point, one per map)",
        )
    state = _ModuleBuilderState()
    prev = _set_active_module_builder(state)
    try:
        fn()
        state.flush_all(False)
    finally:
        _set_active_module_builder(prev)

    final_triggers, final_stmts = _finalize_root_entries(state, state.scope_stack[0])

    compiled = CompiledModule(
        final_triggers,
        state.vars,
        final_stmts,
        state.strings,
        state.files,
        state.funcs,
        state.blobs,

    )
    return compiled


build_module = buildModule


# ---------------------------------------------------------------------------
# Stock and eudkit-extension helpers, built at import from the stock row spec
# ---------------------------------------------------------------------------

from .stock_rows_built import build_helpers as _build_stock_helpers

STOCK_HELPERS = _build_stock_helpers()
globals().update(STOCK_HELPERS)
