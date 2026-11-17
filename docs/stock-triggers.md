# Stock trigger catalog

eudkit exposes the StarCraft: Remastered stock trigger vocabulary as helper
functions for every StarEdit row. Each helper mirrors the StarEdit/EUD
parameter order and emits the documented CHK condition or action type.
Parameters are build-time values (plain constants, enums, or
`FixedString`/record objects), except for the two documented **extension target
forms** on `Deaths` and `SetDeaths`. The single source of truth for every
helper row is [`schema/stock-rows.json`](../schema/stock-rows.json); the
tables below are generated from it, and
[`tests/stock-row-goldens.json`](../tests/stock-row-goldens.json) freezes the
emitted HIR and diagnostics for all 92 trigger rows (plus the `UnitProperty`
record, frozen separately).

## Stock versus eudkit extension

**Stock helpers** cover the 22 StarEdit condition rows and the 57 StarEdit
action rows. They are the helpers listed in the two tables below; every row
is marked `stock`. Stock helpers accept constants for every parameter except
the documented extension target forms: `Deaths` and `SetDeaths` also accept
field-reference and pointer targets for `player` plus `amount`/`value`. The
vocabulary constants (`Anywhere`, `All`, player/force values, comparisons,
resource/score/switch/order/alliance enums) use the stock CHK encodings:
`Anywhere = 64`, `All = 0`, and ordinary players are `0..11`. Location IDs
are `0..255`; unit IDs are `0..227`.

**eudkit extension helpers** are additive APIs that reuse the stock CHK
condition 15 (`Deaths`) or action 45 (`SetDeaths`) wire format for masked
memory access, or resolve an existing string index directly. They are not
StarEdit rows and are listed separately below. A map that uses only the 22
and 57 stock helpers still contains only stock condition/action types. For
dynamic values outside the documented `Deaths`/`SetDeaths` target forms,
extensions and the raw builders are the escape hatch.

TypeScript uses camelCase parameter names (`resourceType`, `scoreType`,
`switchId`, `timeModifier`); Python uses snake_case (`resource_type`,
`score_type`, `switch_id`, `time_modifier`). Order and meaning are identical.

## Conditions (22)

Condition `flags` are always `0` in the stock CHK layout. Parameter order is
the call order in both languages.

<!-- BEGIN GENERATED: conditions -->
| ID | Helper | Parameters (in order) | Default flags | Class |
| --: | --- | --- | --: | --- |
| 1 | `CountdownTimer` | `comparison, time` | 0 | stock |
| 2 | `Command` | `player, comparison, number, unit` | 0 | stock |
| 3 | `Bring` | `player, comparison, number, unit, location` | 0 | stock |
| 4 | `Accumulate` | `player, comparison, number, resource_type` | 0 | stock |
| 5 | `Kills` | `player, comparison, number, unit` | 0 | stock |
| 6 | `CommandMost` | `unit` | 0 | stock |
| 7 | `CommandMostAt` | `unit, location` | 0 | stock |
| 8 | `MostKills` | `unit` | 0 | stock |
| 9 | `HighestScore` | `score_type` | 0 | stock |
| 10 | `MostResources` | `resource_type` | 0 | stock |
| 11 | `Switch` | `switch_id, state` | 0 | stock |
| 12 | `ElapsedTime` | `comparison, time` | 0 | stock |
| 14 | `Opponents` | `player, comparison, number` | 0 | stock |
| 15 | `Deaths` | `player, comparison, amount, unit` | 0 | stock |
| 16 | `CommandLeast` | `unit` | 0 | stock |
| 17 | `CommandLeastAt` | `unit, location` | 0 | stock |
| 18 | `LeastKills` | `unit` | 0 | stock |
| 19 | `LowestScore` | `score_type` | 0 | stock |
| 20 | `LeastResources` | `resource_type` | 0 | stock |
| 21 | `Score` | `player, score_type, comparison, number` | 0 | stock |
| 22 | `Always` | `—` | 0 | stock |
| 23 | `Never` | `—` | 0 | stock |
<!-- END GENERATED: conditions -->

## Actions (57)

Default flags: `4` for ordinary actions, `20` for unit/location and
leaderboard actions, and `28` for `CreateUnitWithProperties`. `DisplayText`
and `Transmission` take `always_display` (default `4`).

<!-- BEGIN GENERATED: actions -->
| ID | Helper | Parameters (in order) | Default flags | Class |
| --: | --- | --- | --: | --- |
| 1 | `Victory` | `—` | 4 | stock |
| 2 | `Defeat` | `—` | 4 | stock |
| 3 | `PreserveTrigger` | `—` | 4 | stock |
| 4 | `Wait` | `time` | 4 | stock |
| 5 | `PauseGame` | `—` | 4 | stock |
| 6 | `UnpauseGame` | `—` | 4 | stock |
| 7 | `Transmission` | `unit, location, sound, time_modifier, time, text, always_display` | 4 | stock |
| 8 | `PlayWAV` | `wav` | 4 | stock |
| 9 | `DisplayText` | `text, always_display` | 4 | stock |
| 10 | `CenterView` | `location` | 4 | stock |
| 11 | `CreateUnitWithProperties` | `count, unit, location, player, properties` | 28 | stock |
| 12 | `SetMissionObjectives` | `text` | 4 | stock |
| 13 | `SetSwitch` | `switch_id, state` | 4 | stock |
| 14 | `SetCountdownTimer` | `modifier, time` | 4 | stock |
| 15 | `RunAIScript` | `script` | 4 | stock |
| 16 | `RunAIScriptAt` | `script, location` | 4 | stock |
| 17 | `LeaderBoardControl` | `unit, label` | 20 | stock |
| 18 | `LeaderBoardControlAt` | `unit, location, label` | 20 | stock |
| 19 | `LeaderBoardResources` | `resource_type, label` | 4 | stock |
| 20 | `LeaderBoardKills` | `unit, label` | 20 | stock |
| 21 | `LeaderBoardScore` | `score_type, label` | 4 | stock |
| 22 | `KillUnit` | `unit, player` | 20 | stock |
| 23 | `KillUnitAt` | `count, unit, location, player` | 20 | stock |
| 24 | `RemoveUnit` | `unit, player` | 20 | stock |
| 25 | `RemoveUnitAt` | `count, unit, location, player` | 20 | stock |
| 26 | `SetResources` | `player, modifier, amount, resource_type` | 4 | stock |
| 27 | `SetScore` | `player, modifier, amount, score_type` | 4 | stock |
| 28 | `MinimapPing` | `location` | 4 | stock |
| 29 | `TalkingPortrait` | `unit, time` | 20 | stock |
| 30 | `MuteUnitSpeech` | `—` | 4 | stock |
| 31 | `UnMuteUnitSpeech` | `—` | 4 | stock |
| 32 | `LeaderBoardComputerPlayers` | `state` | 4 | stock |
| 33 | `LeaderBoardGoalControl` | `goal, unit, label` | 20 | stock |
| 34 | `LeaderBoardGoalControlAt` | `goal, unit, location, label` | 20 | stock |
| 35 | `LeaderBoardGoalResources` | `goal, resource_type, label` | 4 | stock |
| 36 | `LeaderBoardGoalKills` | `goal, unit, label` | 20 | stock |
| 37 | `LeaderBoardGoalScore` | `goal, score_type, label` | 4 | stock |
| 38 | `MoveLocation` | `location, on_unit, owner, search_location` | 20 | stock |
| 39 | `MoveUnit` | `count, unit, owner, start_location, dest_location` | 20 | stock |
| 40 | `LeaderBoardGreed` | `goal` | 4 | stock |
| 41 | `SetNextScenario` | `scenario_name` | 4 | stock |
| 42 | `SetDoodadState` | `state, unit, owner, location` | 20 | stock |
| 43 | `SetInvincibility` | `state, unit, owner, location` | 20 | stock |
| 44 | `CreateUnit` | `number, unit, location, player` | 20 | stock |
| 45 | `SetDeaths` | `player, modifier, value, unit` | 20 | stock |
| 46 | `Order` | `unit, owner, start_location, order_type, dest_location` | 20 | stock |
| 47 | `Comment` | `text` | 4 | stock |
| 48 | `GiveUnits` | `count, unit, owner, location, new_owner` | 20 | stock |
| 49 | `ModifyUnitHitPoints` | `count, unit, owner, location, percent` | 20 | stock |
| 50 | `ModifyUnitEnergy` | `count, unit, owner, location, percent` | 20 | stock |
| 51 | `ModifyUnitShields` | `count, unit, owner, location, percent` | 20 | stock |
| 52 | `ModifyUnitResourceAmount` | `count, owner, location, new_value` | 4 | stock |
| 53 | `ModifyUnitHangarCount` | `add, count, unit, owner, location` | 20 | stock |
| 54 | `PauseTimer` | `—` | 4 | stock |
| 55 | `UnpauseTimer` | `—` | 4 | stock |
| 56 | `Draw` | `—` | 4 | stock |
| 57 | `SetAllianceStatus` | `player, status` | 4 | stock |
<!-- END GENERATED: actions -->

## Extension target forms

`Deaths` and `SetDeaths` remain stock rows, but two of their parameters also
accept **eudkit extension** targets: `TriggerFieldRef` values and pointers
are relocated to the referenced trigger slot at assembly time instead of
being encoded as stock constants. No other stock helper accepts dynamic
targets.

<!-- BEGIN GENERATED: extension-target-forms -->
| Helper | Dynamic parameters | Class |
| --- | --- | --- |
| `Deaths` | `player, amount` | eudkit extension |
| `SetDeaths` | `player, value` | eudkit extension |
<!-- END GENERATED: extension-target-forms -->

## String parameter slots

A string parameter resolves through exactly one HIR field into one CHK action
slot. `Transmission` carries both a sound and a text; `PlayWAV` uses only the
sound slot, so action 8 has the single spelling `wav`.

<!-- BEGIN GENERATED: string-parameter-slots -->
| Helper | Parameter | HIR field | CHK slot |
| --- | --- | --- | --- |
| `Transmission` | `sound` | `wav` | `wavid` |
| `Transmission` | `text` | `text` | `strid` |
| `PlayWAV` | `wav` | `wav` | `wavid` |
| `DisplayText` | `text` | `text` | `strid` |
| `SetMissionObjectives` | `text` | `text` | `strid` |
| `LeaderBoardControl` | `label` | `text` | `strid` |
| `LeaderBoardControlAt` | `label` | `text` | `strid` |
| `LeaderBoardResources` | `label` | `text` | `strid` |
| `LeaderBoardKills` | `label` | `text` | `strid` |
| `LeaderBoardScore` | `label` | `text` | `strid` |
| `LeaderBoardGoalControl` | `label` | `text` | `strid` |
| `LeaderBoardGoalControlAt` | `label` | `text` | `strid` |
| `LeaderBoardGoalResources` | `label` | `text` | `strid` |
| `LeaderBoardGoalKills` | `label` | `text` | `strid` |
| `LeaderBoardGoalScore` | `label` | `text` | `strid` |
| `SetNextScenario` | `scenario_name` | `text` | `strid` |
| `Comment` | `text` | `text` | `strid` |
<!-- END GENERATED: string-parameter-slots -->

## Extension helpers

These helpers are **eudkit extensions**: they emit stock condition 15 or
action 45 with a mask/memory form, emit action 9 with a pre-resolved string
index, or patch an existing string dword. They are not StarEdit rows.

<!-- BEGIN GENERATED: extension-helpers -->
| Helper | Wire form | Class |
| --- | --- | --- |
| `DeathsX` | condition 15 + `eudx` mask | eudkit extension |
| `Memory` | condition 15 at `EPD(addr)` | eudkit extension |
| `MemoryEPD` | condition 15 at an EPD | eudkit extension |
| `MemoryX` | condition 15 + mask at `EPD(addr)` | eudkit extension |
| `MemoryXEPD` | condition 15 + mask at an EPD | eudkit extension |
| `SetDeathsX` | action 45 + `eudx` mask | eudkit extension |
| `SetMemory` | action 45 with a fixed destination EPD | eudkit extension |
| `SetMemoryEPD` | action 45 with an EPD destination | eudkit extension |
| `SetMemoryX` | action 45 + mask with a fixed address | eudkit extension |
| `SetMemoryXEPD` | action 45 + mask with an EPD destination | eudkit extension |
| `SetNextPtr` | action 45 writing a trigger `next` pointer | eudkit extension |
| `DisplayTextIndex` | action 9 with a pre-resolved `strid` | eudkit extension |
| `SetStringDword` | action 45 into a string-table dword | eudkit extension |
<!-- END GENERATED: extension-helpers -->

`UnitProperty` is a builder for the 20-byte UPRP record consumed by the stock
action 11 (`CreateUnitWithProperties`); it is not itself a trigger action.
Slot allocation happens at map assembly: authored slots are preserved,
identical records are deduped, new records take the lowest free slot, and
all 64 slots in use is a build error.

## CHK field aliases

The action record is 32 bytes and the condition record is 20 bytes. The
stock helpers use these field names:

| Helper field | CHK field |
| --- | --- |
| `mask` | action `locid1` (location, or a masked action's bit mask) |
| `dest` | action `player1` (player, or an EPD destination) |
| `value` | action `player2` (value, goal, count, pointer) |
| `modifier` | action `amount` |
| `time` | action `time` |
| `unit` | action `unit` |
| `strid` / `wavid` | action string and sound offsets |
| condition `locid` / `player` / `amount` / `unit` / `comparison` / `restype` | matching 20-byte condition fields |

A few stock rows use fields that are easy to misread:

- `Bring` maps `location` to `locid`, `player` to `player`, `number` to
  `amount`, and `comparison` to `comparison`.
- `Switch` maps `switch_id` to `restype` and `state` to `comparison`.
- `Deaths` and `SetDeaths` keep the stock constant layout, but a field
  reference or pointer passed for `player`/`amount` (`value`) is an extension
  target that relocates that CHK field. The `Memory*`/`SetMemory*` helpers are
  the address-taking wrappers.
- `Transmission` interns `text` into `strid` and `sound` (HIR `wav`) into
  `wavid`; `PlayWAV` uses HIR `wav` for `wavid` and has no text slot.
- `DisplayText` and `DisplayTextIndex` show the text to CurrentPlayer
  (`0x6509B0`), not to all players. The compiled payload runs with
  CurrentPlayer reset to 0 (Player 1); a message for the local human must set
  CurrentPlayer to the user player id (`0x512684`) or be emitted from a
  classic trigger owned by that player.
- A `DisplayText` placed after `MoveLocation`/`CenterView` in the same
  trigger is *reported* (unverified) to sometimes appear a cycle late; no
  primary source in StarEdit, BWAPI, GPTP, or eudplib confirms or refutes
  this, and nothing in this repo states it. Treat it as an unconfirmed
  ordering heuristic, not established engine behaviour: if chat order must
  match trigger order, put user-visible markers before those location/camera
  actions and verify in game.

## Adding a stock row

The helper bodies are not hand-maintained. Adding a row means:

1. Add the row to `schema/stock-rows.json` (condition, action, or extension)
   with its `chkType`, `defaultFlags`, ordered `params`, and `doc`. Mark it
   `special` only if it needs behaviour the data emitter cannot express; at
   most five rows may be special, and the enforcement tests fail on a sixth.
2. Regenerate the derived surfaces:

   ```sh
   python3 scripts/generate_stock_surfaces.py   # docs tables + Python export manifest
   cd ts && npm run generate:stock              # ts/stock.generated.ts
   ```

   The LSP vocabulary regenerates in `crates/eudkit-lsp/build.rs` on the next
   Rust build, and Python builds its helpers from the spec at import time.
3. Run the coverage and freshness tests (`py/tests/test_stock_rows.py`,
   `py/tests/test_stock_surfaces.py`, `py/tests/test_stock_contract.py`,
   `ts/tests/test_stock_rows.ts`, `ts/tests/test_stock_codegen.ts`, and
   `cargo test -p eudkit-lsp --test stock_spec`). They fail if any surface
   drifts from the spec.

`tests/stock-row-goldens.json` is the deliberately frozen contract for the
existing 92 rows and is never auto-rewritten. A new row gets its golden
captured once from the now-derived helper; changing an existing row's emitted
HIR requires an explicit golden update and an HIR version decision.

## Verification

- `ts/tests/test_stock_rows.ts` and `py/tests/test_stock_rows.py` replay all
  92 frozen goldens byte-identically (a row carries no HIR version field, so
  the goldens stay valid across the 3.1 -> 3.2 bump), check the helper
  signatures and defaults, the five special rows, the `Deaths`/`SetDeaths`
  target forms and relocations, and the generated docs tables.
- `ts/tests/test_stock_codegen.ts` proves `ts/stock.generated.ts` is fresh,
  every non-special row is generated, only the five special rows stay
  hand-written, and a spec edit alone renders a new helper.
- `py/tests/test_stock_contract.py` proves only the five special rows keep
  hand-written Python bodies, the retired test-only catalog stays gone, and a
  spec edit alone builds the new helper plus its docs and export manifest.
- `py/tests/test_stock_surfaces.py` checks the generated Python export
  manifest and docs regions are in sync with the spec.
- `crates/eudkit-lsp/tests/stock_spec.rs` checks the generated LSP
  vocabulary, including that a spec row becomes an editor symbol.
- The in-game smoke map lives in
  [`tests/outputmap/smoke`](../tests/outputmap/smoke/README.md) and uses the
  explicit `module` export.
