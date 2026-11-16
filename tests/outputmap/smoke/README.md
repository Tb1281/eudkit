# Smoke maps

Hand-test scenarios. Compile a source, open the `.scx` in StarCraft:
Remastered, and compare the HUD lines with the tables below. Unless a section
says otherwise, run the map in single-player.

## Map reference

Silent basemap = `tests/outputmap/basemap/basemap_silent.scx`; plain basemap =
`tests/outputmap/basemap/basemap.scx`.

| Map | Source | Basemap | Committed artifact |
| --- | --- | --- | --- |
| Stock trigger catalog | `stock-catalog.py` / `stock-catalog.ts` | silent | `stock-catalog-py.scx` and `stock-catalog-ts.scx`, 26,186 bytes |
| DAT tables | `dat-tables.ts` | silent | `dat-tables.scx` |
| Classic-chain runner order | `classic-runner-order.ts` | plain | `classic-runner-order.scx`, 26,086 bytes |
| Aligned `FixedString` padding | `aligned-string-padding.ts` | silent | `aligned-string-padding.scx`, 51,448 bytes |
| Decimal HUD sample | `sample.py` / `sample.ts` | silent | source-only |
| Runtime `else` and `dispatch` | `else-dispatch.py` / `else-dispatch.ts` | silent | `else-dispatch.scx`, 70,945 bytes |
| Re-entrant `divmod` | `divmod-loop.py` / `divmod-loop.ts` | silent | `divmod-loop.scx`, 87,030 bytes |
| `UnitGroup` auto-prune | `unitgroup-cunit.py` / `unitgroup-cunit.ts` | silent | `unitgroup-cunit.scx`, 51,967 bytes |
| Camera follow | `camera-follow.py` / `camera-follow.ts` | silent | `camera-follow.scx`, 130,180 bytes |
| Containers and math | `hud-containers-math.py` / `hud-containers-math.ts` | silent | `hud-containers-math.scx`, 102,743 bytes |
| `displayAt` ownership | `displayat_owning_verify.ts` | silent | source-only |

## Stock trigger catalog

`stock-catalog.py` and `stock-catalog.ts` expose one root-namespace `module`
entry (`module = build_module(prog)` and `export const module = buildModule(...)`).
Only that entry is read; see
[ADR 0012](../../../docs/adr/0012-explicit-entry-module-export.md).

| Order | Expected text | Meaning |
| --- | --- | --- |
| 1 | `STOCK SETUP` | setup marker; first action of the setup trigger |
| 2 | `STOCK BRING` | `MoveLocation`/`Bring` chain saw the Marine |
| 3 | `STOCK CATALOG OK` | Marine portrait transmission |

The setup trigger creates a Marine at `Anywhere` (the engine map-center
sentinel) and centers the camera there. `STOCK SETUP` is deliberately the first
action: `MoveLocation`/`CenterView` defer the rest of the trigger's actions to
the next cycle, so a marker placed after them would display after `STOCK BRING`
even though the trigger runs first. A leaderboard labelled `STOCK` appears; it
may not render in single-player even when the action executes. When the
transmission finishes, `KillUnit` removes the Marine.

Both frontends emit byte-identical HIR and `.scx`; the CLI test
`stock_catalog_smoke_map_is_identical_across_frontends` enforces the comparison.

## DAT tables

`dat-tables.ts` reads live DAT values through the typed accessors and displays
them as zero-padded hex:

| Line | Expected text | Source |
| ---- | ------------- | ------ |
| 1 | `DAT HP=00002800` | `unitsDat[UnitType.TerranMarine].maxHp` (raw 8.8 fixed point: 40 * 256) |
| 2 | `DAT DMG=00000006` | `weaponsDat[WeaponType.GaussRifle].damage` (6) |

DAT accessors return raw memory values, not scaled game units: StarCraft stores
`maxHp` in 8.8 fixed point, so a Marine with 40 HP reads as `0x00002800`.

`basemap_silent.scx` is `basemap.scx` with every authored classic trigger
disabled (the disabled bit, `8`, set in each trigger's flag dword). The plain
`basemap.scx` carries an enabled authored `Hello World!` trigger, and the
compiler preserves authored trigger flags, so a map built on it also displays
that line. Disabling the authored triggers isolates the smoke payload. Rebuild
the silent basemap with:

```sh
cargo run -p eudkit-map --example silent_basemap -- \
  tests/outputmap/basemap/basemap.scx \
  tests/outputmap/basemap/basemap_silent.scx
```

The committed artifacts are `basemap_silent.scx` (22,640 bytes, SHA-256
`ce2509f0…`) and `dat-tables.scx` (SHA-256 `199486d8…`).

## Classic-chain runner order

`classic-runner-order.ts` is the ADR 0010 ordering smoke. It is built on the
plain `basemap.scx`, so the authored `Hello World!` classic trigger is enabled.

| Order | Line | Source |
| ----- | ---- | ------ |
| 1 | `Hello World!` | authored classic trigger in `basemap.scx` |
| 2 | `RUNNER PAYLOAD` | compiled payload, once |

`Hello World!` appears first, then `RUNNER PAYLOAD`; the payload line must still
render. Both lines use plain `DisplayText` (no `printAt`), so the earlier line
appears above the later one. Run it single-player: the contract is per-player
chain, and the tick gate makes the payload run once per tick. The committed map
is 26,086 bytes with SHA-256 `85520677…`.

## Aligned FixedString padding

`aligned-string-padding.ts` exercises capacity-aware aligned `(d)` output.

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
`ts/string.ts` and `py/string.py` uses `STRING_PAD_BYTE = 0x1A`. The committed
map is 51,448 bytes with SHA-256 `afe0714d…`.

## Decimal HUD sample

`sample.ts` and `sample.py` are the ADR 0014 decimal-format sample sources. The
sample is source-only: compile it locally rather than committing an `.scx` that
can drift from the current lowering.

Both sources write:

```text
wave=\x1a\x1a\x1a5 hp=\x1a100
```

`0x1A` is confirmed zero-width in SC:R (see the aligned-padding section above),
so the displayed line reads `wave=5 hp=100`.

`eudkit synth` emits byte-identical HIR for both sources. The CLI test
`string_format_benchmark_two_variable_decimal_regression_adr_0014` uses these
files as its benchmark fixture and pins the pre-assembly cost: 100T static /
105T executed (51T / 53T for one 4-digit placeholder in isolation). Each digit
byte is a masked `SetStringDword` action at a compile-time dword and lane, with
no runtime `pos.shr(2)` or scratch-trigger pointer patching.

Compile either source locally with:

```sh
eudkit compile tests/outputmap/smoke/sample.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/sample-ts.scx
eudkit compile tests/outputmap/smoke/sample.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/sample-py.scx
```

Both compiled maps are byte-identical (32,608 bytes, 135T static with the
default once-per-tick gate at this baseline).

## Runtime `else` & `dispatch` (`else-dispatch.scx`)

`else-dispatch.py` / `else-dispatch.ts` exercise runtime `when(..., else_body)`
and multi-way integer `dispatch()` (4-case else-chain `N <= 9` and 12-case
balanced BST `N >= 10`), plus a live step counter cycling `0..15` every 12
frames. Both frontends emit byte-identical HIR and `.scx` (70,945 bytes,
SHA-256 `c472d6c9…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `DISPATCH CHAIN=1099/1099 BST=1070/1070` | 4-case chain + 12-case BST checksums |
| 2 | `WHEN ELSE CHECK=12/12` | Both `then` (+10) and `else` (+2) arms fired |
| 3 | `LIVE STEP= 0..15 ODD=0/1 BST_OUT=...` | Cycles every 12 frames |
| 4 | `STATUS: PASS` | All self-checks hold |

## Re-entrant `divmod` & `DeferredNe` gate (`divmod-loop.scx`)

`divmod-loop.py` / `divmod-loop.ts` continuously re-run the `whileLoop`
`divmod(1000000 + k, 7)` reproducer and constant `divmod(frame, 10)` every 6
frames, alongside a `Var.mul` check and a raw `Trigger` `DeferredNe` 5T flag
gate. Both frontends emit byte-identical HIR and `.scx` (87,030 bytes, SHA-256
`82a6b225…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `DIVMOD QSUM=714285/714285 RSUM=15/15 FAIL=0` | `fail` must stay `0` across frames |
| 2 | `FRAME=    N /10 -> Q=... R=...` | Live frame counter modulo 10 |
| 3 | `MUL=699678/699678 NE_GATE=2/2` | `1234 * 567` and raw `Trigger` `ne(500)` |
| 4 | `STATUS: PASS` | All self-checks hold continuously |

## Interactive `UnitGroup` auto-prune (`unitgroup-cunit.scx`)

`unitgroup-cunit.py` / `unitgroup-cunit.ts` spawn 4 Terran Marines for P1 at map
center (`t = 1..4s`), capture each runtime EPD from `firstPlayerUnit[P1]`
(`0x6283F8`), register them into `UnitGroup(6, compact=True, autoPrune=True)`,
and iterate them with `for_each`. Both frontends emit byte-identical HIR and
`.scx` (51,967 bytes, SHA-256 `9800758a…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `UNITGROUP COMPACT LEN=4 VISITS=4` | Grows `0 -> 4` over the first 4s |
| 2 | `SPAWNED=4/4 LAST_EPD=.....` | Live CUnit EPD registered |
| 3 | `Kill own Marine (A-click) -> autoPrune drops LEN & VISITS` | Attack-kill own Marines in-game to watch `LEN` and `VISITS` drop `4 -> 3 -> ... -> 0` |
| 4 | `STATUS: PASS (LEN == VISITS)` | Length and `for_each` visits match |

## Camera follow & live coordinates (`camera-follow.scx`)

`camera-follow.py` / `camera-follow.ts` spawn a P1 Terran Marine at map center
after 1s, center the view on it, set Switch 0, and run
`camera_follow(63, 62, inertia=5, on_unit=TerranMarine, owner=P1)`. Both
frontends emit byte-identical HIR and `.scx` (130,180 bytes, SHA-256
`5971f25b…`).

Expected HUD lines (`displayAt(1..3)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `CAMERA X=... Y=... SCREENW= 640` | Live camera pixel X/Y (`0x62848C` / `0x6284A8`) |
| 2 | `MODE=SP ...` or `MODE=MP (1+) CAMMOVE ACTIVE - move Marine!` | Smoothing activates in LAN/multiplayer (`0x57F0B4 >= 1`); walk the Marine to verify smooth easing |
| 3 | `LAST_TICK=...` | Hex tick tracked by `CameraFollowState` |

## Containers, `TriggerStruct`, `isqrt`, & live memory (`hud-containers-math.scx`)

`hud-containers-math.py` / `hud-containers-math.ts` verify `Stack` (LIFO),
`Queue` (FIFO across trigger frames), `FixedArray`, `TriggerStruct`
(`dword`/`byte`/`word`), `isqrt`, `unitsDat`, and live `MemFieldHandle` reads of
`GAME_TICK_ADDR` (`0x57F23C`) and P1 minerals (`0x57F0F0`). Both frontends emit
byte-identical HIR and `.scx` (102,743 bytes, SHA-256 `37f223e0…`).

Expected HUD lines (`displayAt(1..4)`):

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `STACK=100/100 QUEUE=140/140 STRUCT+ARR=577/577` | Settled within 5 ticks (`Queue` pops 1/frame) |
| 2 | `ISQRT=1255/1255 MARINE_HP_HEX=00002800` | `isqrt(1e6)+isqrt(65535)` and Marine `maxHp` |
| 3 | `LIVE TICK=...... P1_MINERALS=...` | Updates live as game ticks and minerals change |
| 4 | `STATUS: PASS` | All self-checks hold |

## `displayAt` ownership (`displayat_owning_verify.ts`)

`displayat_owning_verify.ts` verifies which string owns a `displayAt` line.

| Line | Expected text | Meaning |
| ---- | ------------- | ------- |
| 1 | `AAA` | `FixedString.displayAt(1)` owns chat line 1 |
| 2 | `BBB` | `FixedString.display()` appends to the next line |

Compile it to `/tmp`; the map is source-only and not committed:

```sh
eudkit compile tests/outputmap/smoke/displayat_owning_verify.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/displayat-owning.scx
```

## Regenerating

Compile a smoke map with the real implementation:

```sh
eudkit compile tests/outputmap/smoke/<name>.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o tests/outputmap/smoke/<name>.scx
```

Use `basemap.scx` for `classic-runner-order`, and compile the source-only
`sample.*` and `displayat_owning_verify.ts` maps to `/tmp` with the commands in
their sections above. `--plan` reports the aligned-padding layout without
touching the basemap.
