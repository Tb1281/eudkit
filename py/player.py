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

from .array import FixedArray, arrayDword
from .camera import GAME_TICK_ADDR, USER_PLAYER_ADDR
from .cp import CP_ADDR, read_current_player_register
from .hir import (
    AddrReloc,
    Always,
    CenterView,
    Deaths,
    DeathsX,
    DisplayText,
    Exactly,
    Memory,
    MinimapPing,
    Never,
    PlayWAV,
    SetDeaths,
    SetMissionObjectives,
    SetTo,
    TalkingPortrait,
    TriggerFieldReloc,
    emitVarStore,
)
from .string import encode_utf8
from .temporal import when, while_loop
from .trigger import Trigger, _get_active_module_builder, _register_trigger
from .var import Expr, ReadonlyVar, Var, to_var
from .view import (
    EPD,
    EPD_CURRENT_PLAYER,
    EudslError,
    FieldSpec,
    FieldValueSpace,
    MemFieldHandle,
    _describe_value,
    _is_plain_int,
    defineMemView,
    read_epd,
)

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
    "PLAYER_TABLE_ADDR",
    "PLAYER_SLOT_BYTES",
    "PLAYER_SLOT_COUNT",
    "PLAYER_MAX_ID",
    "TURBO_ADDR",
    "turbo",
    "user_player",
    "userPlayer",
    "game_tick",
    "gameTick",
    "is_user",
    "isUser",
    "player_exists",
    "playerExists",
    "for_each_player",
    "forEachPlayer",
    "Player",
    "PlayerField",
    "PVariable",
    "display_text_all",
    "displayTextAll",
    "play_wav_all",
    "playWavAll",
    "minimap_ping_all",
    "minimapPingAll",
    "center_view_all",
    "centerViewAll",
    "set_mission_objectives_all",
    "setMissionObjectivesAll",
    "talking_portrait_all",
    "talkingPortraitAll",
]

#: First byte of player 0's name record.
PLAYER_NAME_TABLE = 0x57EEE8
#: Dwords per player record (36 bytes / 4).
PLAYER_NAME_EPDS = 9
#: The game's record size in bytes.
PLAYER_NAME_BYTES = 36
#: Longest name that still fits its trailing NUL in the record.
PLAYER_NAME_MAX_BYTES = 35

#: Highest id accepted by :class:`Player` (mirrors ``TrgPlayer.range``).
PLAYER_MAX_ID = 11


def _require_player(player: object, what: str) -> int:
    if not _is_plain_int(player):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: player must be a plain integer, got {_describe_value(player)}",
        )
    value = int(player)  # type: ignore[arg-type]
    if value < 0 or value > PLAYER_MAX_ID:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: player must be in 0..{PLAYER_MAX_ID}, got {value}",
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
        value = _require_player(player, "match_names()")
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


# ---------------------------------------------------------------------------
# Player state, existence, and all-player broadcasts
# ---------------------------------------------------------------------------

#: First byte of the eight-slot player existence table (`pexist.py:38`).
PLAYER_TABLE_ADDR = 0x51A280
#: Bytes per existence-table record (two dwords plus the "has left" dword).
PLAYER_SLOT_BYTES = 12
#: Live melee slots addressed by ``player_exists``/``for_each_player``.
PLAYER_SLOT_COUNT = 8
#: ``dwTurbo``; ``turbo()`` writes zero here in the tail position.
TURBO_ADDR = 0x6509A0


def turbo() -> None:
    """Disable the game's speed governor (``eudTurbo.py:9``).

    Emits exactly one action, ``SetMemory(0x6509A0, SetTo, 0)``, so the call
    belongs in the tail trigger where the plugin writes it.
    """
    from .hir import SetMemory

    SetMemory(TURBO_ADDR, SetTo, 0)


def user_player() -> Var:
    """Read the low byte of ``dwUserPlayerId`` (the local human player index)."""
    return read_epd(EPD(USER_PLAYER_ADDR), 8, 0)


def game_tick() -> Var:
    """Read the game tick dword."""
    return read_epd(EPD(GAME_TICK_ADDR), 32, 0)


userPlayer = user_player
gameTick = game_tick


def _is_runtime_operand(value: Any) -> bool:
    return isinstance(value, (Var, ReadonlyVar, Expr))


def is_user(player: int | Var) -> Any:
    """Return a condition comparing ``dwUserPlayerId`` with ``player``."""
    current = user_player()
    if _is_plain_int(player):
        return current.eq(int(player))
    if _is_runtime_operand(player):
        return current.eq(to_var(player))
    raise EudslError(
        "EUDSL-TYPE",
        f"is_user(): player must be a plain integer or Var; "
        f"got {_describe_value(player)}",
    )


isUser = is_user


def _require_exists_player(player: object, what: str) -> int:
    if not _is_plain_int(player):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: player must be a plain integer, got {_describe_value(player)}",
        )
    value = int(player)  # type: ignore[arg-type]
    if value < 0 or value >= PLAYER_SLOT_COUNT:
        raise EudslError(
            "EUDSL-RANGE",
            f"{what}: player must be in 0..{PLAYER_SLOT_COUNT - 1}, got {value}",
        )
    return value


def _exists_condition(player: int) -> dict[str, Any]:
    """The constant ``pexist.py`` predicate as one 1-condition gate."""
    base = PLAYER_TABLE_ADDR + PLAYER_SLOT_BYTES * player
    return Memory(base + 8, Exactly, (~(base + 4)) & 0xFFFFFFFF)


def player_exists(player: int | Var) -> Var:
    """Return a fresh ``Var`` that is 1 while ``player`` is still in the game."""
    if _is_plain_int(player):
        value = _require_exists_player(player, "player_exists()")
        out = Var(0)
        out.set(1)
        when(_exists_condition(value), lambda: out.set(0))
        return out

    if not _is_runtime_operand(player):
        raise EudslError(
            "EUDSL-TYPE",
            f"player_exists(): player must be a plain integer or Var; "
            f"got {_describe_value(player)}",
        )

    source = to_var(player)
    out = Var(0)

    def in_range() -> None:
        out.set(1)
        # np = 3 * player (three table dwords), built with value copies.
        np = Var(0)
        np.set(source)
        np.iadd(source)
        np.iadd(source)
        # target = EPD(table) + 2 + np: the "has left" dword of the record.
        target = Var(0)
        target.set((EPD(PLAYER_TABLE_ADDR) + 2) & 0xFFFFFFFF)
        target.iadd(np)
        # expected = ~(table + 4) - 12 * player, the "has left" sentinel.
        expected = Var(0)
        expected.set((~(PLAYER_TABLE_ADDR + 4)) & 0xFFFFFFFF)
        expected.isub(np)
        expected.isub(np)
        expected.isub(np)
        expected.isub(np)
        # The condition's player/amount fields are patched at runtime by the
        # preceding VarStores. The probe is registered after them, so it runs
        # with the freshly written EPD, and it clears `out` only when the
        # record carries the "has left" sentinel.
        probe = Trigger(
            conds=[Deaths(0, Exactly, 0, 0)],
            acts=[out.act_set(0)],
            register=False,
        )
        emitVarStore(
            target.id,
            TriggerFieldReloc(probe.id, probe.cond(0).player.byteOffset),
        )
        emitVarStore(
            expected.id,
            TriggerFieldReloc(probe.id, probe.cond(0).amount.byteOffset),
        )
        _register_trigger(probe)

    when(source.le(PLAYER_SLOT_COUNT - 1), in_range)
    return out


playerExists = player_exists


def _validate_player_subset(players: Iterable[int], what: str) -> list[int]:
    if isinstance(players, (set, frozenset)):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: players must be an ordered iterable; a set is not allowed",
        )
    try:
        raw_players = list(players)
    except TypeError:
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: players must be an iterable of player indices",
        ) from None
    if not raw_players:
        raise EudslError(
            "EUDSL-BOUNDS", f"{what}: players must contain at least one player"
        )
    out: list[int] = []
    seen: set[int] = set()
    for player in raw_players:
        value = _require_player(player, what)
        if value in seen:
            raise EudslError("EUDSL-BOUNDS", f"{what}: duplicate player {value}")
        seen.add(value)
        out.append(value)
    return out


def for_each_player(
    body: Callable[[Var], Any],
    *,
    active_only: bool = True,
    players: Iterable[int] | None = None,
) -> None:
    """Run ``body(player_var)`` once per selected player with CP bound to it.

    With ``players=None`` the selection is the runtime loop ``0..7``; when a
    subset is given the loop is unrolled at build time. The ambient
    ``CurrentPlayer`` register is saved once and restored unconditionally.
    """
    if not callable(body):
        raise EudslError("EUDSL-TYPE", "for_each_player(): body must be callable")

    selected = (
        None
        if players is None
        else _validate_player_subset(players, "for_each_player()")
    )

    saved = read_current_player_register()

    if selected is None:
        current = Var(0)

        def iteration() -> None:
            def active() -> None:
                emitVarStore(current.id, AddrReloc(CP_ADDR), SetTo)
                body(current)

            if active_only:
                when(player_exists(current).eq(1), active)
            else:
                active()
            current.iadd(1)

        while_loop(current.le(PLAYER_SLOT_COUNT - 1), iteration)
    else:
        for player in selected:
            player_var = Var(player)

            def run(index: int = player, var: Var = player_var) -> None:
                SetDeaths(EPD_CURRENT_PLAYER, SetTo, index, 0)
                body(var)

            if active_only:
                when(player_exists(player).eq(1), run)
            else:
                run()

    emitVarStore(saved.id, AddrReloc(CP_ADDR))


forEachPlayer = for_each_player


_PLAYER_FIELD_VIEW = defineMemView(
    "PlayerField",
    4,
    {"value": FieldSpec(offset=0, size=4)},
)
_PLAYER_FIELD_SPEC = FieldSpec(offset=0, size=4)
_PLAYER_FIELD_VALUE_SPACE = FieldValueSpace(0xFFFFFFFF, 0)

#: The ``scdata/player.py`` dword arrays, one element per player.
_PLAYER_FIELD_ADDRS: tuple[tuple[str, int], ...] = (
    ("minerals", 0x57F0F0),
    ("gas", 0x57F120),
    ("cumulative_gas", 0x57F150),
    ("cumulative_minerals", 0x57F180),
    ("zerg_control_available", 0x582144),
    ("zerg_control_used", 0x582174),
    ("zerg_control_max", 0x5821A4),
    ("terran_supply_available", 0x5821D4),
    ("terran_supply_used", 0x582204),
    ("terran_supply_max", 0x582234),
    ("protoss_psi_available", 0x582264),
    ("protoss_psi_used", 0x582294),
    ("protoss_psi_max", 0x5822C4),
    ("unit_score", 0x581E44),
    ("building_score", 0x582024),
    ("kill_score", 0x581F04),
    ("razing_score", 0x582054),
    ("custom_score", 0x5822F4),
)

_PLAYER_FIELD_ALIASES: tuple[tuple[str, str], ...] = (
    ("cumulativeMinerals", "cumulative_minerals"),
    ("cumulativeGas", "cumulative_gas"),
    ("zergControlAvailable", "zerg_control_available"),
    ("zergControlUsed", "zerg_control_used"),
    ("zergControlMax", "zerg_control_max"),
    ("terranSupplyAvailable", "terran_supply_available"),
    ("terranSupplyUsed", "terran_supply_used"),
    ("terranSupplyMax", "terran_supply_max"),
    ("protossPsiAvailable", "protoss_psi_available"),
    ("protossPsiUsed", "protoss_psi_used"),
    ("protossPsiMax", "protoss_psi_max"),
    ("unitScore", "unit_score"),
    ("buildingScore", "building_score"),
    ("killScore", "kill_score"),
    ("razingScore", "razing_score"),
    ("customScore", "custom_score"),
    ("mineral", "minerals"),
    ("cumulativeMineral", "cumulative_minerals"),
)


def _require_field_value(value: object, what: str) -> int:
    if not _is_plain_int(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: value must be a plain integer, got {_describe_value(value)}",
        )
    return int(value)  # type: ignore[arg-type]


class PlayerField:
    """One dword of one player's ``scdata/player.py`` state array."""

    def __init__(self, player: int, addr: int) -> None:
        self.player = _require_player(player, "PlayerField()")
        self.base_addr = addr & 0xFFFFFFFF
        self.addr = (addr + 4 * self.player) & 0xFFFFFFFF
        self.epd = (EPD(addr) + self.player) & 0xFFFFFFFF

    def _handle(self) -> MemFieldHandle:
        return MemFieldHandle(
            _PLAYER_FIELD_VIEW,
            _PLAYER_FIELD_SPEC,
            self.epd,
            "value",
        )

    def read(self) -> Var:
        return read_epd(self.epd, 32, 0)

    def set(self, value: int) -> None:
        SetDeaths(
            self.epd,
            SetTo,
            _require_field_value(value, "PlayerField.set"),
            0,
        )

    def iadd(self, value: int) -> None:
        from .hir import Add

        SetDeaths(
            self.epd,
            Add,
            _require_field_value(value, "PlayerField.iadd"),
            0,
        )

    def isub(self, value: int) -> None:
        from .hir import Subtract

        SetDeaths(
            self.epd,
            Subtract,
            _require_field_value(value, "PlayerField.isub"),
            0,
        )

    def _compare(self, op: str, rhs: int) -> Any:
        value = _require_field_value(rhs, f"PlayerField.{op}") & 0xFFFFFFFF
        plan = _PLAYER_FIELD_VALUE_SPACE.compare(op, value)
        if plan.fold == "never":
            return Never()
        if plan.fold == "always":
            return Always()
        if plan.fold == "defer":
            return self._handle().ne(value)
        assert plan.comparison is not None
        return Deaths(self.epd, plan.comparison, plan.value, 0)

    def eq(self, rhs: int) -> Any:
        return self._compare("eq", rhs)

    def ne(self, rhs: int) -> Any:
        return self._compare("ne", rhs)

    def ge(self, rhs: int) -> Any:
        return self._compare("ge", rhs)

    def le(self, rhs: int) -> Any:
        return self._compare("le", rhs)

    def gt(self, rhs: int) -> Any:
        return self._compare("gt", rhs)

    def lt(self, rhs: int) -> Any:
        return self._compare("lt", rhs)


class Player:
    """Player state arrays and existence for one player id (``0..11``)."""

    def __init__(self, player: int) -> None:
        self.index = _require_player(player, "Player()")
        for name, addr in _PLAYER_FIELD_ADDRS:
            setattr(self, name, PlayerField(self.index, addr))
        for alias, target in _PLAYER_FIELD_ALIASES:
            setattr(self, alias, getattr(self, target))

    def exists(self) -> Var:
        return player_exists(self.index)


class PVariable:
    """An eight-slot per-player variable backed by ``FixedArray(dword, 8)``."""

    def __init__(self, init: int = 0) -> None:
        if not _is_plain_int(init):
            raise EudslError(
                "EUDSL-TYPE",
                f"PVariable(): init must be a plain integer, "
                f"got {_describe_value(init)}",
            )
        self._storage = FixedArray(arrayDword, PLAYER_SLOT_COUNT)
        if int(init) != 0:  # type: ignore[arg-type]
            self._storage.fill(int(init))  # type: ignore[arg-type]

    @property
    def storage(self) -> FixedArray:
        return self._storage

    def _index(self, player: int | Var) -> int | Var:
        if _is_plain_int(player):
            value = int(player)  # type: ignore[arg-type]
            if value < 0 or value >= PLAYER_SLOT_COUNT:
                raise EudslError(
                    "EUDSL-RANGE",
                    f"PVariable index must be in 0..{PLAYER_SLOT_COUNT - 1}, "
                    f"got {value}",
                )
            return value
        if _is_runtime_operand(player):
            return to_var(player)
        raise EudslError(
            "EUDSL-TYPE",
            f"PVariable index must be a plain integer or Var; "
            f"got {_describe_value(player)}",
        )

    def get(self, player: int | Var) -> Any:
        return self._storage.get(self._index(player))

    def set(self, player: int | Var, value: int | Var) -> None:
        index = self._index(player)
        if _is_plain_int(value):
            self._storage.set(index, int(value))  # type: ignore[arg-type]
            return
        if isinstance(value, Var):
            self._storage.set(index, value)
            return
        raise EudslError(
            "EUDSL-TYPE",
            f"PVariable.set value must be a plain integer or Var; "
            f"got {_describe_value(value)}",
        )

    def __getitem__(self, player: int | Var) -> Any:
        return self.get(player)

    def __setitem__(self, player: int | Var, value: int | Var) -> None:
        self.set(player, value)


def _require_text(text: object, what: str) -> str:
    if not isinstance(text, str):
        raise EudslError(
            "EUDSL-TYPE",
            f"{what}: text must be a string, got {_describe_value(text)}",
        )
    return text


def _require_stock_range(value: object, name: str, minimum: int, maximum: int) -> int:
    """Validate a raw stock-argument range before any loop is emitted."""
    if not _is_plain_int(value):
        raise EudslError(
            "EUDSL-TYPE",
            f"{name} must be a plain integer in {minimum}..{maximum}, "
            f"got {_describe_value(value)}",
        )
    number = int(value)  # type: ignore[arg-type]
    if number < minimum or number > maximum:
        raise EudslError(
            "EUDSL-RANGE",
            f"{name} must be an integer in {minimum}..{maximum}, got {number}",
        )
    return number


def display_text_all(text: str) -> None:
    """Show ``text`` to every live player (slots 0..7 only; observers excluded)."""
    message = _require_text(text, "display_text_all()")
    for_each_player(lambda _p: DisplayText(message, always_display=4))


displayTextAll = display_text_all


def play_wav_all(wav: str) -> None:
    """Play ``wav`` for every live player (slots 0..7 only; observers excluded)."""
    sound = _require_text(wav, "play_wav_all()")
    for_each_player(lambda _p: PlayWAV(sound))


playWavAll = play_wav_all


def minimap_ping_all(location: int) -> None:
    """Ping a raw location id (``0..255``) on every live player's minimap.

    Only live slots ``0..7`` are addressed; observers (``8..11``) are excluded.
    """
    loc = _require_stock_range(location, "minimap_ping_all() location", 0, 255)
    for_each_player(lambda _p: MinimapPing(loc))


minimapPingAll = minimap_ping_all


def center_view_all(location: int) -> None:
    """Center every live player's view on a raw location id (``0..255``).

    Only live slots ``0..7`` are addressed; observers (``8..11``) are excluded.
    """
    loc = _require_stock_range(location, "center_view_all() location", 0, 255)
    for_each_player(lambda _p: CenterView(loc))


centerViewAll = center_view_all


def set_mission_objectives_all(text: str) -> None:
    """Replace the mission objectives text for every live player.

    Only live slots ``0..7`` are addressed; observers (``8..11``) are excluded.
    """
    message = _require_text(text, "set_mission_objectives_all()")
    for_each_player(lambda _p: SetMissionObjectives(message))


setMissionObjectivesAll = set_mission_objectives_all


def talking_portrait_all(unit: int, time: int) -> None:
    """Show ``unit``'s portrait for ``time`` ms to every live player.

    Only live slots ``0..7`` are addressed; observers (``8..11``) are excluded.
    """
    portrait = _require_stock_range(unit, "talking_portrait_all() unit", 0, 227)
    delay = _require_stock_range(
        time, "talking_portrait_all() time", 0, 0xFFFFFFFF
    )
    for_each_player(lambda _p: TalkingPortrait(portrait, delay))


talkingPortraitAll = talking_portrait_all
