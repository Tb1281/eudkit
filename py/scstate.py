"""Tech and upgrade state over the SC/BW per-player byte tables.

An entry lives at byte ``addr & 3`` of the dword at ``EPD(addr)``, where
``addr = BASE + index_offset + STRIDE * player``. The table is split at id 24
(tech) and 46 (upgrade): SC and BW use different bases and player strides, so
:func:`_sc_bw_slot` is the one place that picks the pair.

A constant ``player`` costs one masked ``DeathsX`` (read) or one
``SetDeathsX`` (write). A runtime ``player`` binds the computed EPD with
``with_epd`` and reads/writes through ``CurrentPlayer``. Tech strides are
multiples of four, so the byte lane stays constant; upgrade strides are not,
so its runtime form dispatches over the four lanes.
"""

from __future__ import annotations

from typing import Any

from .cp import with_epd
from .hir import AtLeast, DeathsX, SetDeathsX, SetTo
from .temporal import when
from .var import Expr, ReadonlyVar, Var
from .view import (
    CURRENT_PLAYER,
    EPD,
    EPD_BASE,
    EudslError,
    _describe_value,
    _is_plain_int,
    _require_plain_int,
    read_epd,
)

PlayerOperand = int | Var | ReadonlyVar | Expr

TECH_COUNT: int = 44
TECH_SC_COUNT: int = 24
TECH_STATE_SC_BASE: int = 0x58CF44
TECH_STATE_BW_BASE: int = 0x58F140
TECH_STATE_SC_STRIDE: int = 24
TECH_STATE_BW_STRIDE: int = 20

UPGRADE_COUNT: int = 61
UPGRADE_SC_COUNT: int = 46
UPGRADE_STATE_SC_BASE: int = 0x58D2B0
UPGRADE_STATE_BW_BASE: int = 0x58F32C
UPGRADE_STATE_SC_STRIDE: int = 46
UPGRADE_STATE_BW_STRIDE: int = 15

MAX_PLAYER_ID: int = 11

#: ``(sc_count, sc_base, bw_base, sc_stride, bw_stride)`` for one id domain.
_Split = tuple[int, int, int, int, int]
_TECH_SPLIT: _Split = (
    TECH_SC_COUNT,
    TECH_STATE_SC_BASE,
    TECH_STATE_BW_BASE,
    TECH_STATE_SC_STRIDE,
    TECH_STATE_BW_STRIDE,
)
_UPGRADE_SPLIT: _Split = (
    UPGRADE_SC_COUNT,
    UPGRADE_STATE_SC_BASE,
    UPGRADE_STATE_BW_BASE,
    UPGRADE_STATE_SC_STRIDE,
    UPGRADE_STATE_BW_STRIDE,
)


def _sc_bw_slot(index: int, split: _Split) -> tuple[int, int]:
    """``(base + index_offset, player_stride)`` for one id in a split table."""
    sc_count, sc_base, bw_base, sc_stride, bw_stride = split
    if index < sc_count:
        return sc_base + index, sc_stride
    return bw_base + (index - sc_count), bw_stride


def _require_count(value: object, what: str, maximum: int) -> int:
    """Validate an id against a ``0..maximum`` plain-integer domain."""
    index = _require_plain_int(value, what)
    if index < 0 or index > maximum:
        raise EudslError(
            "EUDSL-RANGE", f"{what} must be in 0..{maximum}, got {index}"
        )
    return index


def _require_player_id(player: object, what: str) -> int:
    """Validate a build-time player id (``0..11``)."""
    return _require_count(player, what, MAX_PLAYER_ID)


def _require_player(player: object, what: str) -> PlayerOperand:
    """Validate a player operand: a plain id or a runtime value handle."""
    if _is_plain_int(player):
        return _require_player_id(player, what)
    if isinstance(player, (Var, ReadonlyVar, Expr)):
        return player
    raise EudslError(
        "EUDSL-TYPE",
        f"{what} must be a plain integer in 0..{MAX_PLAYER_ID}, a Var, "
        f"or an Expr; got {_describe_value(player)}",
    )


def _slotted(index: int, player: int, split: _Split) -> tuple[int, int]:
    """``(epd, shift)`` for a constant ``(id, player)`` pair."""
    anchor, stride = _sc_bw_slot(index, split)
    addr = anchor + stride * player
    return EPD(addr), 8 * (addr & 3)


def _runtime_addr(index: int, player: PlayerOperand, split: _Split) -> Var:
    """Materialize ``(base + index_offset) + stride * player`` as a value."""
    anchor, stride = _sc_bw_slot(index, split)
    addr = Var(0)
    addr.set(player)  # value copy, never a pointer store
    addr.imul(stride)
    addr.iadd(anchor)
    return addr


def _addr_epd(addr: Var) -> Var:
    """Fold a byte address into its dword EPD."""
    epd = Var(0)
    epd.set(addr.shr(2))
    epd.isub(EPD_BASE // 4)
    return epd


def _masked_bit(epd: int, shift: int) -> Any:
    """One condition testing the byte lane ``shift`` of ``epd``."""
    return DeathsX(epd, AtLeast, 1, 0, 0xFF << shift)


def _masked_read(epd: int | Var, shift: int) -> Var:
    """Read one byte lane into a fresh `Var` (``epd`` may be runtime)."""
    if _is_plain_int(epd):
        return read_epd(int(epd), 8, shift)
    return with_epd(
        epd, lambda _cp: read_epd(CURRENT_PLAYER, 8, shift)
    )


def _masked_write(epd: int | Var, value: int, shift: int) -> None:
    """Write ``value`` into one byte lane, leaving the other lanes untouched."""
    mask = 0xFF << shift
    payload = (value << shift) & 0xFFFFFFFF
    if _is_plain_int(epd):
        SetDeathsX(int(epd), SetTo, payload, 0, mask)
        return
    with_epd(
        epd,
        lambda _cp: SetDeathsX(CURRENT_PLAYER, SetTo, payload, 0, mask),
    )


def _lane_read(addr: Var) -> Var:
    """Read the byte at ``addr`` through a four-arm lane dispatch."""
    epd = _addr_epd(addr)
    lane = Var(0)
    lane.set(addr.bitAnd(3))
    out = Var(0)

    def body(_cp: Any) -> None:
        for k in range(4):
            when(
                lane.eq(k),
                lambda k=k: out.set(
                    _masked_read(CURRENT_PLAYER, 8 * k)
                ),
            )

    with_epd(epd, body)
    return out


def _lane_write(addr: Var, level: int) -> None:
    """Write ``level`` to the byte at ``addr`` through four lane arms."""
    epd = _addr_epd(addr)
    lane = Var(0)
    lane.set(addr.bitAnd(3))

    def body(_cp: Any) -> None:
        for k in range(4):
            when(
                lane.eq(k),
                lambda k=k: _masked_write(
                    CURRENT_PLAYER, level, 8 * k
                ),
            )

    with_epd(epd, body)


def tech_state_slot(tech: int, player: int) -> tuple[int, int]:
    """``(epd, shift)`` of one tech entry for a build-time player id."""
    t = _require_count(tech, "tech_state_slot() tech", TECH_COUNT - 1)
    p = _require_player_id(player, "tech_state_slot() player")
    return _slotted(t, p, _TECH_SPLIT)


techStateSlot = tech_state_slot


def upgrade_state_slot(upgrade: int, player: int) -> tuple[int, int]:
    """``(epd, shift)`` of one upgrade entry for a build-time player id."""
    u = _require_count(upgrade, "upgrade_state_slot() upgrade", UPGRADE_COUNT - 1)
    p = _require_player_id(player, "upgrade_state_slot() player")
    return _slotted(u, p, _UPGRADE_SPLIT)


upgradeStateSlot = upgrade_state_slot


def tech_researched(tech: int, player: int) -> Any:
    """One condition that holds when ``player`` has researched ``tech``.

    Constant-player only: a condition cannot name a runtime EPD. Use
    :func:`tech_researched_var` for a runtime player.
    """
    t = _require_count(tech, "tech_researched() tech", TECH_COUNT - 1)
    p = _require_player(player, "tech_researched() player")
    if not _is_plain_int(p):
        raise EudslError(
            "EUDSL-TYPE",
            "tech_researched() player must be a build-time plain integer; "
            "use tech_researched_var() for a runtime player",
        )
    epd, shift = _slotted(t, p, _TECH_SPLIT)
    return _masked_bit(epd, shift)


techResearched = tech_researched


def tech_researched_var(tech: int, player: PlayerOperand) -> Var:
    """Researched state as a ``0``/``1`` value (runtime player allowed)."""
    t = _require_count(tech, "tech_researched_var() tech", TECH_COUNT - 1)
    p = _require_player(player, "tech_researched_var() player")
    if _is_plain_int(p):
        epd, shift = _slotted(t, p, _TECH_SPLIT)
        res = _masked_read(epd, shift)
    else:
        anchor, _stride = _sc_bw_slot(t, _TECH_SPLIT)
        res = _masked_read(_addr_epd(_runtime_addr(t, p, _TECH_SPLIT)), 8 * (anchor & 3))
    out = Var(0)
    when(res.ne(0), lambda: out.set(1))
    return out


techResearchedVar = tech_researched_var


def upgrade_level(upgrade: int, player: PlayerOperand) -> Var:
    """Current level of ``upgrade`` for ``player`` as a byte value."""
    u = _require_count(upgrade, "upgrade_level() upgrade", UPGRADE_COUNT - 1)
    p = _require_player(player, "upgrade_level() player")
    if _is_plain_int(p):
        epd, shift = _slotted(u, p, _UPGRADE_SPLIT)
        return _masked_read(epd, shift)
    return _lane_read(_runtime_addr(u, p, _UPGRADE_SPLIT))


upgradeLevel = upgrade_level


def set_tech_researched(tech: int, player: PlayerOperand, on: bool) -> None:
    """Write ``on`` to one tech entry, leaving the other byte lanes intact."""
    t = _require_count(tech, "set_tech_researched() tech", TECH_COUNT - 1)
    p = _require_player(player, "set_tech_researched() player")
    if not isinstance(on, bool):
        raise EudslError(
            "EUDSL-TYPE",
            f"set_tech_researched() on must be a bool, got {_describe_value(on)}",
        )
    value = 1 if on else 0
    if _is_plain_int(p):
        epd, shift = _slotted(t, p, _TECH_SPLIT)
        _masked_write(epd, value, shift)
        return
    anchor, _stride = _sc_bw_slot(t, _TECH_SPLIT)
    _masked_write(
        _addr_epd(_runtime_addr(t, p, _TECH_SPLIT)), value, 8 * (anchor & 3)
    )


setTechResearched = set_tech_researched


def set_upgrade_level(
    upgrade: int,
    player: PlayerOperand,
    level: int,
) -> None:
    """Write ``level`` (a byte) to one upgrade entry."""
    u = _require_count(upgrade, "set_upgrade_level() upgrade", UPGRADE_COUNT - 1)
    p = _require_player(player, "set_upgrade_level() player")
    value = _require_count(level, "set_upgrade_level() level", 0xFF)
    if _is_plain_int(p):
        epd, shift = _slotted(u, p, _UPGRADE_SPLIT)
        _masked_write(epd, value, shift)
        return
    _lane_write(_runtime_addr(u, p, _UPGRADE_SPLIT), value)


setUpgradeLevel = set_upgrade_level

__all__ = [
    "PlayerOperand",
    "TECH_COUNT", "TECH_SC_COUNT", "TECH_STATE_SC_BASE", "TECH_STATE_BW_BASE",
    "TECH_STATE_SC_STRIDE", "TECH_STATE_BW_STRIDE",
    "UPGRADE_COUNT", "UPGRADE_SC_COUNT", "UPGRADE_STATE_SC_BASE",
    "UPGRADE_STATE_BW_BASE", "UPGRADE_STATE_SC_STRIDE",
    "UPGRADE_STATE_BW_STRIDE", "MAX_PLAYER_ID",
    "tech_state_slot", "techStateSlot", "upgrade_state_slot", "upgradeStateSlot",
    "tech_researched", "techResearched", "tech_researched_var",
    "techResearchedVar", "upgrade_level", "upgradeLevel",
    "set_tech_researched", "setTechResearched",
    "set_upgrade_level", "setUpgradeLevel",
]
