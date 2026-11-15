"""Player-name table helpers and the layout-A nickname matcher (ADR 0032).

StarCraft keeps twelve 36-byte player-name records starting at ``0x57EEE8``
(nine dwords per player).  The table sits below ``EPD_BASE``, so ``EPD()``
already returns a wrapped 32-bit index that the engine reads and writes like
any other Deaths-table target.

``match_names`` is a build-time unroll, not a runtime loop: for every player it
emits one ordered ``when`` chain over the candidate names.  Each arm compares
the player's name field dword by dword with ``DeathsX`` and calls
``on_match(player, name)`` for the first candidate that matches.  Nicknames are
encoded by ``encode_utf8`` (the same byte source ``FixedString`` uses), and the
final dword's mask always includes the NUL byte, so a prefix such as ``"Armo"``
can never match a record containing ``"Armoha"``.

Layout C (a runtime byte reader over a NUL-separated name DB) is deliberately
not shipped: it needs ``SetNextPtr``-style trigger rewiring, which eudkit
rejects.  See ADR 0032 §5 for the deferral and the cost model.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from .hir import DeathsX, Exactly
from .string import encode_utf8
from .temporal import when
from .trigger import _get_active_module_builder
from .view import EPD, EudslError, _describe_value, _is_plain_int

__all__ = [
    "PLAYER_NAME_TABLE",
    "PLAYER_NAME_EPDS",
    "PLAYER_NAME_BYTES",
    "PLAYER_NAME_MAX_BYTES",
    "player_name_epd",
    "playerNameEpd",
    "player_name_addr",
    "playerNameAddr",
    "match_names",
    "matchNames",
]

#: First byte of player 0's name record.
PLAYER_NAME_TABLE = 0x57EEE8
#: Dwords per player record (36 bytes / 4).
PLAYER_NAME_EPDS = 9
#: The game's record size in bytes.
PLAYER_NAME_BYTES = 36
#: Longest name that still fits its trailing NUL in the record.
PLAYER_NAME_MAX_BYTES = 35

_PLAYER_MAX = 11


def _require_player(player: object, what: str) -> int:
    if not _is_plain_int(player):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: player must be a plain integer, got {_describe_value(player)}",
        )
    value = int(player)  # type: ignore[arg-type]
    if value < 0 or value > _PLAYER_MAX:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: player must be in 0..{_PLAYER_MAX}, got {value}",
        )
    return value


def player_name_epd(player: int) -> int:
    """EPD of ``player``'s 36-byte name record.

    Returns the unsigned 32-bit value the engine reads as an EPD:
    ``(EPD(0x57EEE8) + 9 * player) & 0xFFFFFFFF``.
    """
    value = _require_player(player, "player_name_epd()")
    return (EPD(PLAYER_NAME_TABLE) + PLAYER_NAME_EPDS * value) & 0xFFFFFFFF


playerNameEpd = player_name_epd


def player_name_addr(player: int) -> int:
    """Byte address of ``player``'s name record: ``0x57EEE8 + 36 * player``."""
    value = _require_player(player, "player_name_addr()")
    return (PLAYER_NAME_TABLE + PLAYER_NAME_BYTES * value) & 0xFFFFFFFF


playerNameAddr = player_name_addr


def _word(data: Sequence[int], index: int) -> int:
    """Little-endian dword ``data[4*index:4*index+4]``, zero-padded."""
    value = 0
    for lane in range(4):
        pos = 4 * index + lane
        if pos < len(data):
            value |= (data[pos] & 0xFF) << (8 * lane)
    return value & 0xFFFFFFFF


def _conds_for(player: int, data: Sequence[int]) -> list[dict[str, Any]]:
    """One trigger's conditions comparing ``player``'s record to ``data``."""
    base = player_name_epd(player)
    conds: list[dict[str, Any]] = []
    whole = len(data) // 4
    for word_index in range(whole):
        conds.append(
            DeathsX(base + word_index, Exactly, _word(data, word_index), 0, 0xFFFFFFFF)
        )
    word_index = whole
    lane = len(data) % 4
    # The mask spans the bytes before the NUL plus the NUL itself.  It is the
    # correctness property: without it, "Armo" would match "Armoha".
    mask = (1 << (8 * (lane + 1))) - 1
    conds.append(
        DeathsX(
            base + word_index,
            Exactly,
            _word(data, word_index) & mask,
            0,
            mask,
        )
    )
    return conds


def _validate_names(names: Any) -> list[str]:
    if not isinstance(names, (list, tuple)):
        raise EudslError(
            "EUDSL-TYPE", "match_names(): names must be a list/tuple of strings"
        )
    if not names:
        raise EudslError(
            "EUDSL-BOUNDS", "match_names(): names must contain at least one name"
        )
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            raise EudslError(
                "EUDSL-TYPE",
                f"match_names(): every name must be a string, got {_describe_value(name)}",
            )
        if name in seen:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"match_names(): duplicate name {name!r}",
            )
        seen.add(name)
    return list(names)


def _validate_players(players: Any) -> list[int]:
    if isinstance(players, (set, frozenset)):
        raise EudslError(
            "EUDSL-TYPE",
            "match_names(): players must be an ordered iterable; a set is not allowed",
        )
    try:
        raw_players = list(players)
    except TypeError:
        raise EudslError(
            "EUDSL-TYPE",
            "match_names(): players must be an iterable of player indices",
        ) from None
    if not raw_players:
        raise EudslError(
            "EUDSL-BOUNDS", "match_names(): players must contain at least one player"
        )
    out: list[int] = []
    seen: set[int] = set()
    for player in raw_players:
        if not _is_plain_int(player):
            raise EudslError(
                "EUDSL-TYPE",
                "match_names(): every player must be a plain integer, got "
                f"{_describe_value(player)}",
            )
        value = int(player)
        if value < 0 or value > _PLAYER_MAX:
            raise EudslError(
                "EUDSL-RANGE",
                f"match_names(): player must be in 0..{_PLAYER_MAX}, got {value}",
            )
        if value in seen:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"match_names(): duplicate player {value}",
            )
        seen.add(value)
        out.append(value)
    return out


def match_names(
    names: Sequence[str],
    on_match: Callable[[int, str], Any],
    players: Iterable[int] = range(8),
) -> None:
    """Emit an unrolled nickname matcher; call ``on_match(player, name)`` on a hit.

    ``players`` defaults to ``range(8)`` (the eight playable slots).  Matching
    is exact and case-sensitive; the first matching name in caller order wins
    for each player.  Every ``(player, name)`` arm is exactly one trigger with
    at most nine conditions.
    """
    builder = _get_active_module_builder()
    if builder is None:
        raise EudslError(
            "EUDSL-SCOPE", "match_names() must be called inside buildModule()"
        )
    validated_names = _validate_names(names)
    encoded = [encode_utf8(name, "nickname") for name in validated_names]
    for name, data in zip(validated_names, encoded):
        if len(data) > PLAYER_NAME_MAX_BYTES:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"match_names(): name {name!r} encodes to {len(data)} bytes; "
                f"the record holds at most {PLAYER_NAME_MAX_BYTES} plus its NUL",
            )
    validated_players = _validate_players(players)
    if not callable(on_match):
        raise EudslError("EUDSL-TYPE", "match_names(): on_match must be callable")

    # Every validation above has run before the first emission, so a rejected
    # call leaves no partial chain behind.
    for player in validated_players:

        def arm(index: int, player: int = player) -> None:
            if index == len(validated_names):
                return
            name = validated_names[index]
            conds = _conds_for(player, encoded[index])
            if index + 1 == len(validated_names):
                when(conds, lambda: on_match(player, name))
            else:
                when(
                    conds,
                    lambda: on_match(player, name),
                    lambda: arm(index + 1, player),
                )

        arm(0)


matchNames = match_names
