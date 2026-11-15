# Smoke maps

These maps are hand-test scenarios. Open the compiled `.scx` in
StarCraft: Remastered and compare the displayed lines with the table below.

## Stock trigger catalog

`stock-catalog-ts.scx` and `stock-catalog-py.scx` (issue #68) are built from
the explicit root-source entries in `stock-catalog.ts`
(`export const module = buildModule(...)`) and `stock-catalog.py`
(`module = build_module(prog)`). Only these root-namespace `module` entries
are read; see [ADR 0012](../../../docs/adr/0012-explicit-entry-module-export.md).
The sources exercise the representative stock set:

- `Bring` plus `CreateUnit`;
- `MoveLocation` plus `CenterView`;
- `KillUnit`;
- `Transmission` with text and a WAV name;
- `LeaderBoardControl` with the string label `STOCK`.

Expected in-game sequence:

1. After one game second, `STOCK SETUP` appears, then a Marine is created at
   `Anywhere` (the engine map-center sentinel) and the camera centers there.
   The marker is deliberately the setup trigger's first action: the
   subsequent `MoveLocation`/`CenterView` sequence defers the rest of the
   trigger's actions to the next cycle, so a marker placed after them would
   be displayed *after* `STOCK BRING` even though the trigger runs first.
2. Location 63 is moved onto the Marine and `Bring` sees it.
3. `STOCK BRING` appears, confirming the `MoveLocation`/`Bring` chain fired;
   the leaderboard labelled `STOCK` appears.
4. A Marine portrait transmission reads `STOCK CATALOG OK` and plays the
   stock Terran advisor sound.
5. When the transmission finishes, `KillUnit` removes the Marine. The
   leaderboard remains, showing zero controlled units.

The two marker texts make failures local: if `STOCK SETUP` appears but the
camera does not move, only `CenterView` failed in that environment. If
`STOCK BRING` never appears, the `MoveLocation`/`Bring` chain did not see the
Marine. The leaderboard may not render in single-player even when the action
executes.

Regenerate either language with:

```sh
eudkit compile tests/outputmap/smoke/stock-catalog.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/stock-catalog-ts.scx
eudkit compile tests/outputmap/smoke/stock-catalog.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/stock-catalog-py.scx
```

`eudkit synth` emits byte-identical HIR for both sources. The two compiled
maps are also byte-identical (26,186 bytes, SHA-256
`7860db3f74fbe226a34cd8432c7412e05324a85bfd42a242ee1f79bcba41524c`).
The CLI test `stock_catalog_smoke_map_is_identical_across_frontends` keeps
that comparison enforced.

**In-game result:** verified on 2026-11-06 in StarCraft: Remastered
single-player. The Marine appeared at the map center and the camera moved to
it. The observed on-screen marker order was `STOCK SETUP` → `STOCK BRING` →
`STOCK CATALOG OK`; the Marine was killed when the transmission finished. The
`STOCK` leaderboard action executed but did not visibly render in
single-player, consistent with the note above.

The earlier `BRING` → `SETUP` observation was this action-order effect, not a
trigger-chain inversion. Two artifacts keep it from recurring:

- The CLI test `stock_catalog_smoke_map_is_identical_across_frontends`
  asserts that the setup trigger's first action is `DisplayText("STOCK
  SETUP")`, followed by `CreateUnit`, `MoveLocation`, and `CenterView`.
- `tests/outputmap/diagnostics/setup-display-probe.ts` is the standalone
  diagnostic that isolated the effect. Its in-game result on 2026-11-06 was
  `B TWO` above `A SETUP`, then `C THREE`, `D CREATE ONLY`, `E FIVE`. `A`
  followed `MoveLocation`/`CenterView`, while `D` shows that `CreateUnit` alone
  does not defer the next `DisplayText`.

Rebuild the probe with:

```sh
eudkit compile tests/outputmap/diagnostics/setup-display-probe.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/diagnostics/setup-display-probe.scx
```

## DAT tables

`dat-tables.scx` (issue #51) reads live DAT values through the typed accessors
and displays them as zero-padded hex:

| Line | Expected text | Source |
| ---- | ------------- | ------ |
| 1 | `DAT HP=00002800` | `unitsDat[UnitType.TerranMarine].maxHp` (raw 8.8 fixed point: 40 * 256) |
| 2 | `DAT DMG=00000006` | `weaponsDat[WeaponType.GaussRifle].damage` (6) |

In-game verified on 2026-11-05: both lines appear exactly as above and no
other chat line is written. DAT accessors return raw memory values, not
scaled game units: StarCraft stores `maxHp` in 8.8 fixed point, so a Marine
with 40 HP reads as `0x00002800`.

Regenerate with:

```sh
eudkit compile tests/outputmap/smoke/dat-tables.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/dat-tables.scx
```

`basemap_silent.scx` is `basemap.scx` with every authored classic trigger
disabled (the disabled bit, `8`, set in each trigger's flag dword). The plain
`basemap.scx` carries an enabled authored `Hello World!` trigger, and #40
preserves authored trigger flags, so a map built on it also displays that
line. Disabling the authored triggers isolates the smoke payload. Rebuild the
silent basemap with:

```sh
cargo run -p eudkit-map --example silent_basemap -- \
  tests/outputmap/basemap/basemap.scx \
  tests/outputmap/basemap/basemap_silent.scx
```

The committed artifacts are `basemap_silent.scx` (22,640 bytes, SHA-256
`ce2509f0…`) and `dat-tables.scx` (SHA-256 `199486d8…`).

## Classic-chain runner order

`classic-runner-order.scx` is the ADR 0010 ordering smoke. It is built on the
plain `basemap.scx`, so the authored `Hello World!` classic trigger is
enabled. The map displays:

| Order | Line | Source |
| ----- | ---- | ------ |
| 1 | `Hello World!` | authored classic trigger in `basemap.scx` |
| 2 | `RUNNER PAYLOAD` | compiled payload, once |

Expected: `Hello World!` appears first, then `RUNNER PAYLOAD`; the payload
line must still render. Both lines use plain `DisplayText` (no `printAt`), so
the earlier line appears above the later one. Run it single-player — the
contract is per player chain, and the tick gate makes the payload run once per
tick.

Regenerate with:

```sh
eudkit compile tests/outputmap/smoke/classic-runner-order.ts \
  --basemap tests/outputmap/basemap/basemap.scx \
  -o tests/outputmap/smoke/classic-runner-order.scx
```

In-game verified on 2026-11-05: both lines render exactly once, with
`Hello World!` above `RUNNER PAYLOAD` (classic-first). The committed map is
26,086 bytes with SHA-256 `85520677…`.

## Aligned FixedString padding

`aligned-string-padding.scx` is a hand-test scenario for the capacity-aware
aligned `(d)` follow-up.

The map displays two lines:

| Line | Expected text |
| ---- | ------------- |
| 1 | `ABCDEFGH` |
| 2 | `ABCDEFGH` |

Line 1 is a plain control string. Line 2 is assembled across two scope
boundaries, so capacity-aware alignment writes:

```text
AB 1A 1A CD 1A 1A EF GH
```

`0x1A` is confirmed zero-width in SC:R: both lines render `ABCDEFGH` with the
same spacing, cursor position, and line length. The aligned lane in
`ts/string.ts` and `py/string.py` uses `STRING_PAD_BYTE = 0x1A`.

In-game verified on 2026-11-05: both lines render `ABCDEFGH` and no other
chat line is written.

## Decimal HUD sample

`sample.ts` and `sample.py` are the ADR 0014 decimal-format sample sources
(issue #81). The sample is source-only: compile it locally rather than
committing an `.scx` that can drift from the current lowering.

Both sources write:

```text
wave=\x1a\x1a\x1a5 hp=\x1a100
```

`0x1A` is confirmed zero-width in SC:R (see the aligned-padding section
above), so the displayed line reads `wave=5 hp=100`.

`eudkit synth` emits byte-identical HIR for both sources. The CLI test
`string_format_benchmark_two_variable_decimal_regression_adr_0014` uses these
files as its benchmark fixture and pins the re-baselined measured cost from
issue #98: 100T static / 105T executed pre-assembly (51T / 53T for one
4-digit placeholder in isolation). Issue #98 removed the per-digit
scratch-trigger `emitVarStore` pointer patching, so each digit byte is a
masked `SetStringDword` action at a compile-time dword and lane with no
runtime `pos.shr(2)`.

Compile either source locally with:

```sh
eudkit compile tests/outputmap/smoke/sample.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/sample-ts.scx
eudkit compile tests/outputmap/smoke/sample.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/sample-py.scx
```

Both compiled maps are byte-identical (35,419 bytes at this baseline).

## Regenerating

The committed `.scx` is produced by the real implementation:

```sh
eudkit compile tests/outputmap/smoke/aligned-string-padding.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/aligned-string-padding.scx
```

The silent basemap is the same fixture used by the DAT smoke map; it disables
the authored `Hello World!` trigger that plain `basemap.scx` would otherwise
preserve. The committed map is 51,448 bytes with SHA-256 `afe0714d…`.

`--plan` reports the same padding layout without touching the basemap.

## Runtime `else` & `dispatch` (`else-dispatch.scx`)

`else-dispatch.scx` (`else-dispatch.py` / `else-dispatch.ts`) exercises runtime
`when(..., else_body)` (#51) and multi-way integer `dispatch()` (4-case
else-chain `N <= 9` and 12-case balanced BST `N >= 10`), plus a live step
counter cycling `0..15` every 12 frames. Both frontends emit byte-identical HIR
and `.scx` (70,945 bytes, SHA-256 `c472d6c9…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `DISPATCH CHAIN=1099/1099 BST=1070/1070` | 4-case chain + 12-case BST checksums |
| 2 | `WHEN ELSE CHECK=12/12` | Both `then` (+10) and `else` (+2) arms fired |
| 3 | `LIVE STEP= 0..15 ODD=0/1 BST_OUT=...` | Cycles every 12 frames |
| 4 | `STATUS: PASS` | All self-checks hold |

## Re-entrant `divmod` & `DeferredNe` gate (`divmod-loop.scx`)

`divmod-loop.scx` (`divmod-loop.py` / `divmod-loop.ts`) continuously re-runs the
#122 `whileLoop` `divmod(1000000 + k, 7)` reproducer and constant `divmod(frame,
10)` every 6 frames, alongside a `Var.mul` check and raw `Trigger` `DeferredNe`
5T flag gate (#66). Both frontends emit byte-identical HIR and `.scx` (87,030
bytes, SHA-256 `82a6b225…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `DIVMOD QSUM=714285/714285 RSUM=15/15 FAIL=0` | `fail` must stay `0` across frames |
| 2 | `FRAME=    N /10 -> Q=... R=...` | Live frame counter modulo 10 |
| 3 | `MUL=699678/699678 NE_GATE=2/2` | `1234 * 567` and raw `Trigger` `ne(500)` |
| 4 | `STATUS: PASS` | All self-checks hold continuously |

## Interactive `UnitGroup` auto-prune (`unitgroup-cunit.scx`)

`unitgroup-cunit.scx` (`unitgroup-cunit.py` / `unitgroup-cunit.ts`) spawns 4
Terran Marines for P1 at map center (`t = 1..4s`), captures each runtime EPD
from `firstPlayerUnit[P1]` (`0x6283F8`), registers them into `UnitGroup(6,
compact=True, autoPrune=True)`, and iterates them with `for_each`. Both
frontends emit byte-identical HIR and `.scx` (51,967 bytes, SHA-256
`9800758a…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `UNITGROUP COMPACT LEN=4 VISITS=4` | Grows `0 -> 4` over first 4s |
| 2 | `SPAWNED=4/4 LAST_EPD=.....` | Live CUnit EPD registered |
| 3 | `Kill own Marine (A-click) -> autoPrune drops LEN & VISITS` | Attack-kill own Marines in-game to watch `LEN` & `VISITS` drop `4 -> 3 -> ... -> 0` |
| 4 | `STATUS: PASS (LEN == VISITS)` | Length and `for_each` visits match |

## Camera follow & live coordinates (`camera-follow.scx`)

`camera-follow.scx` (`camera-follow.py` / `camera-follow.ts`) spawns a P1 Terran
Marine at map center after 1s, centers the view on it, sets Switch 0, and runs
`camera_follow(63, 62, inertia=5, on_unit=TerranMarine, owner=P1)` (Cycle 6
`mulConst` -> `Var.mul`). Both frontends emit byte-identical HIR and `.scx`
(130,180 bytes, SHA-256 `5971f25b…`).

Expected HUD lines (`displayAt(1..3)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `CAMERA X=... Y=... SCREENW= 640` | Live camera pixel X/Y (`0x62848C` / `0x6284A8`) |
| 2 | `MODE=SP ...` or `MODE=MP (1+) CAMMOVE ACTIVE - move Marine!` | Smoothing activates in LAN/Multiplayer (`0x57F0B4 >= 1`); walk Marine to verify smooth easing |
| 3 | `LAST_TICK=...` | Hex tick tracked by `CameraFollowState` |

## Containers, `TriggerStruct`, `isqrt`, & live memory (`hud-containers-math.scx`)

`hud-containers-math.scx` (`hud-containers-math.py` / `hud-containers-math.ts`)
verifies `Stack` (LIFO), `Queue` (FIFO across trigger frames), `FixedArray`,
`TriggerStruct` (`dword`/`byte`/`word`), `isqrt`, `unitsDat`, and live
`MemFieldHandle` reads of `GAME_TICK_ADDR` (`0x57F23C`) & P1 minerals (`0x57F0F0`). Both
frontends emit byte-identical HIR and `.scx` (102,743 bytes, SHA-256
`37f223e0…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `STACK=100/100 QUEUE=140/140 STRUCT+ARR=577/577` | Settled within 5 ticks (`Queue` pops 1/frame) |
| 2 | `ISQRT=1255/1255 MARINE_HP_HEX=00002800` | `isqrt(1e6)+isqrt(65535)` and Marine `maxHp` |
| 3 | `LIVE TICK=...... P1_MINERALS=...` | Updates live as game ticks & minerals change |
| 4 | `STATUS: PASS` | All self-checks hold |

Regenerate any new smoke map with:

```sh
eudkit compile tests/outputmap/smoke/<name>.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/<name>.scx
```
