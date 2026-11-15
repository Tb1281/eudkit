# Player state accessors, `PVariable`, and CP-preserving broadcasts

Status: accepted (2026-11-16)

## Context

Map authors frequently toggle extended turbo triggers (`0x6509A0`), query the
local player (`0x512684`), check melee player presence (`0x51A280`), read/write
player resource/supply/score arrays (`scdata/player.py`), index 8-slot
per-player variables (`PVariable`), and broadcast actions to active players.
`eudplib` implements `f_playerexist` with an 8-arm `EUDSwitch` and `SetNextPtr`.

## Decision

1. **`player_exists` arithmetic without `Switch`/`SetNextPtr`:** Constant players
   `0..7` emit a single `Memory(0x51A280 + 12*p + 8, Exactly, ~(0x51A280 + 12*p + 4))`
   comparison. Runtime player operands compute `EPD(0x51A280) + 2 + 3*p` and
   compare against `~(0x51A280 + 4) - 12*p` through `with_epd`. Slots `8..11` are
   rejected with `EUDSL-RANGE` for existence/broadcasts (avoiding out-of-table
   reads) while staying readable on `Player(p)`.
2. **`Player` & `PlayerField`:** `Player(0..11)` exposes the 15 SC:R player
   dword arrays (`minerals`, `gas`, control/supply/psi triples, scores) via
   `PlayerField` (`read()` -> `Var`, build-time `int` `set`/`iadd`/`isub`,
   condition comparisons `eq`/`ne`/`ge`/`le`/`gt`/`lt`).
3. **`PVariable`:** Wraps `FixedArray(arrayDword, 8)` with optional non-zero
   `init` fill.
4. **`for_each_player` and six broadcasts:** Sets `CurrentPlayer` to each
   selected player id while saving ambient `CurrentPlayer` once via
   `read_current_player_register()` and restoring it once with `emitVarStore`.
   Broadcast helpers (`display_text_all`, `play_wav_all`, `minimap_ping_all`,
   `center_view_all`, `set_mission_objectives_all`, `talking_portrait_all`) take
   raw `0..255` integer location ids so `player` remains independent of `location`.
