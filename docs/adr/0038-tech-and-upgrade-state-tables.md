# Tech researched and upgrade level state tables

Status: accepted (2026-11-16)

## Context

SC:R stores per-player tech research flags and upgrade levels in separate SC and
BW byte arrays with distinct base addresses and strides. `eudplib` indexes them
with `_get_stride_cache`, `VProc`, and `SetNextPtr` `Forward` chains.

## Decision

1. **SC/BW split byte tables:** Tech state (`0..43`, split at `24`) maps to
   `0x58CF44` (stride `24`) and `0x58F140` (stride `20`). Upgrade state
   (`0..60`, split at `46`) maps to `0x58D2B0` (stride `46`) and `0x58F32C`
   (stride `15`). Player domains `0..11` are validated locally so `scstate` does
   not depend on `player`.
2. **Constant fast paths:** Constant `(id, player)` pairs emit a single masked
   `DeathsX` condition (`tech_researched`, 1C / 0T), normalized 8-bit `read_epd`
   (`tech_researched_var` 10T, `upgrade_level` 9T), or single masked
   `SetDeathsX` write (`set_tech_researched`, `set_upgrade_level`, 1T/1A).
3. **Runtime `player` paths without `SetNextPtr`/`VProc`:** Tech strides (`24`,
   `20`) are divisible by `4`, keeping a constant byte-lane mask under
   `with_epd`. Upgrade strides (`46`, `15`) vary modulo `4`, so runtime
   `player` dispatches the 4 byte lanes inside one `with_epd` binding.
   `tech_researched` remains constant-only because conditions cannot name a
   runtime EPD (`tech_researched_var` handles runtime queries).
