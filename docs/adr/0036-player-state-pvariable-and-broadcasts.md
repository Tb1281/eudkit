# Player state accessors, `PVariable`, and CP-preserving broadcasts

Status: accepted (2026-11-16; amended 2026-11-16)

> **Amendment (2026-11-16):** decision 1's runtime `player_exists` probe now
> relocates the condition's `player`/`amount` fields with
> `TriggerFieldReloc` rather than writing through a raw trigger pointer,
> `user_player()` reads only the low 8 bits of `dwUserPlayerId`
> (`read_epd(EPD(USER_PLAYER_ADDR), 8, 0)`) so the adjacent dword cannot leak
> into the result, `for_each_player` iterates without saving/modifying
> `CurrentPlayer` (matching `eudplib`'s `EUDLoopPlayer`), and the six `*_all`
> broadcast helpers run once with `CurrentPlayer` bound to `user_player()`
> (including observers) and restore ambient `CurrentPlayer` afterwards.

## Context

Map authors frequently toggle extended turbo triggers (`0x6509A0`), query the
local player (`0x512684`), check melee player presence (`0x51A280`), read/write
player resource/supply/score arrays (`scdata/player.py`), index 8-slot
per-player variables (`PVariable`), and broadcast actions to active players.
`eudplib` implements `f_playerexist` with an 8-arm `EUDSwitch` and `SetNextPtr`.

## Decision

1. **`player_exists` arithmetic without `Switch`/`SetNextPtr`:** Constant players
   `0..7` emit a single `Memory(0x51A280 + 12*p + 8, Exactly, ~(0x51A280 + 12*p + 4))`
   comparison. Runtime player operands default to absent, and inside a
   `player <= 7` guard compute `EPD(0x51A280) + 2 + 3*p` and
   `~(0x51A280 + 4) - 12*p`, write both into a condition-probe trigger's
   `player`/`amount` fields with `emitVarStore`, and let that probe clear the
   result when the record carries the "has left" sentinel. This avoids both
   `CurrentPlayer` binding and the 32-bit `read_epd` sweep. Slots `8..11` are
   rejected with `EUDSL-RANGE` for existence/broadcasts (avoiding out-of-table
   reads) while staying readable on `Player(p)`.
2. **`Player` & `PlayerField`:** `Player(0..11)` exposes the 18 SC:R player
   dword arrays (`minerals`, `gas`, control/supply/psi triples, scores) via
   `PlayerField` (`read()` -> `Var`, build-time `int` `set`/`iadd`/`isub`,
   condition comparisons `eq`/`ne`/`ge`/`le`/`gt`/`lt`).
3. **`PVariable`:** Wraps `FixedArray(arrayDword, 8)` with optional non-zero
   `init` fill.
4. **`for_each_player` and six local-user broadcasts:** `for_each_player`
   iterates the selected player slots (`0..7` runtime loop or build-time
   unrolled `players` subset, gated by `player_exists` when `active_only` is
   true) without touching `CurrentPlayer`. The six broadcast helpers
   (`display_text_all`, `play_wav_all`, `minimap_ping_all`,
   `center_view_all`, `set_mission_objectives_all`, `talking_portrait_all`)
   save ambient `CurrentPlayer` via `read_current_player_register()`, set
   `CurrentPlayer` to `user_player()` (`0x512684`, including observers), emit
   the single stock action (taking raw `0..255` integer location ids so
   `player` remains independent of `location`), and restore `CurrentPlayer`
   with `emitVarStore`.
