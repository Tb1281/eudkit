# Soundlooper: a segmented loop player as a core helper

Status: accepted (2026-11-11; amended 2026-11-14)

> **Amendment (2026-11-14):** decision 7's `when` chain is a **deliberate
> exception** to ADR 0025's `dispatch` preference, not an oversight. Dispatch
> is cold here (evaluated only on a due pass), and the tree trades static
> triggers for due-pass work. RT3 measured the crossover with
> `scripts/bench_soundlooper_dispatch.py` and
> `ts/scripts/bench_soundlooper_dispatch.ts` at N=16/64/256: the tree cuts the
> due-pass condition path from `N + 1` to `log2(N) + 2` (17 → 6, 65 → 8,
> 257 → 10) and the evaluated trigger path from `N + 2` to `2*log2(N) + 3`
> (18 → 11, 66 → 15, 258 → 19), but adds `2*log2(N)` executed actions
> (3 → 11/15/19) and `2N - 2` static triggers (about 4.8 KB per saved
> worst-case condition, ~4.8 MB at N=1,000). The measured due-pass win is
> clear at N >= 16, with N=10..15 a gray band. Production keeps the `when`
> chain until a map-size budget justifies adoption; the `N + 1` cost sentence
> in §7 stands. If `dispatch` is adopted later, amend §7 and that sentence in
> the same change.

## Context

Issue #116 asks for euddraft's `lib/soundlooper.py` to be ported the same way
as the periodic BGM helper (issue #25): a plain build-time constructor on both
frontends, no plugin system, no trigger-exec hook, byte-for-byte parity between
`py/` and `ts/`.

Soundlooper splits a song into `000`-`999` numbered `.ogg` segments, embeds
them in the MPQ, and plays an intro once, a bar on a fixed period, and a bridge
on the last segment, returning to a configured `goto` sound. Relative to the
BGM helper it adds three things that need decisions:

1. a **build-time Ogg length reader** — segment timing comes from the files
   themselves (the source's `_TinyTag`), not from a caller-supplied number;
2. a **shared progress array** — one dword per loop, read and written by
   runtime code with dynamic indices;
3. **per-bar dispatch** — the source uses `EUDMethod`/`EUDSwitch` jump tables
   to play one of up to 1000 archive names.

The merged #99 `Db`/`DbRef` seam stores a payload blob in the trigger
payload. It cannot serve this helper: `PlayWAV` resolves an *archived name*
from the MPQ, so the segments must be added to the archive. The applicable
surface is ADR 0021's `embedFile`, which is exactly the source's
`MPQAddFile`.

## Decision

1. **The Ogg parser is a shared build-time module.** `py/ogg.py` and
   `ts/ogg.ts` expose `ogg_duration` / `oggDuration` and
   `round_duration` / `roundDuration`. Only the source's `_TinyTag` subset is
   ported: walk pages, read the Vorbis identification header's sample rate and
   the last granule position. Fixtures under `tests/fixtures/ogg` and
   `tests/fixtures/sounds/theme` pin the parser on both frontends.

2. **`SoundLoops` owns the registry and the progress array.** Registration
   happens under `buildModule` / `build_module` scope (the array is a
   `FixedArray("dword", n)`, and `add_loop` calls `embedFile`). The array is
   allocated lazily on first use and registration freezes afterwards — the
   source's `ep_assert(SoundLooper._bars is None)`. A second loop with the same
   title is `EUDSL-BOUNDS`, not a silent overwrite.

3. **Archive ids are deterministic.** The source samples five random
   characters from `string.ascii_letters + string.digits`; two builds of the
   same map therefore differ. The port derives the five-character id from the
   SHA-256 digest of `(title, registration ordinal)`, so builds are
   reproducible and the py/ts ids match (`loop_identifier("theme", 0) ==
   "idwNy"` on both). Duplicate titles are rejected, so the ordinal (not the
   title alone) is only a tiebreaker for reproducibility, never for collision
   avoidance. `manual_add_loop` takes the title itself as the prefix, as the
   source does.

4. **Manual ids are five single-byte ASCII characters.** The source checks
   only that the title is five *cp949 bytes*; the TS frontend has no cp949
   codec, so the shared contract is five single-byte characters (`EUDSL-RANGE`
   otherwise, space allowed). `add_loop` titles are directory names and remain
   unrestricted.

5. **Per-segment scheduling.** On a due pass the body advances the bar, plays
   the segment, then rewrites the probe's threshold from the raw countdown
   clock (`0x51CE8C`). The emitted expression is
   `raw + err + ceil(-bar*1000) - 1`, plus `ceil((bar - intro)*1000)` on bar 1
   and `ceil((bar - bridge)*1000)` on the last bar. `err` comes from
   `calculate_speed_error` (`(493 - 7x) // 41`): `x` defaults to `-41`, becomes
   `0x5124F0` when that dword is 42, and is overridden by the speed pair at
   `0x51CE84`/`0x51CE88`. Both frontends emit the identical HIR expression
   (`x.mul(7).neg().add(493).div(41)`), and the threshold is patched into the
   probe condition amount with `emitVarStore`, the BGM helper's mechanism.

6. **The bar-digit register keeps the source's exact packing.** Decimal digits
   live at `hundreds | tens << 8 | ones << 16` and advance with the source's
   two carries (ones -> tens -> hundreds). In this source version the
   `StringBuffer` write and the `PlayWAV(_sb.StringIndex)` path are dead (the
   line is commented out), so the port does not allocate the eight-byte
   buffer; playback uses constant archive names per case.

7. **`when` chains replace `EUDSwitch`.** eudplib compiles `EUDMethod`
   dispatch to a jump table; eudkit does not model jump tables. The port
   emits one `when` per loop and per bar, evaluated only on a due pass. For a
   loop with `N` segments the play dispatch costs `N + 1` conditions, the same
   order as the source's case table.

8. **CurrentPlayer is saved and restored.** `PlayWAV` must run with
   CurrentPlayer set to the local human, but ADR 0004 requires the cursor to
   be parked per trigger loop, so the body reads `0x6509B0`, writes the local
   player id, plays, and restores the saved value. The local player id itself
   is read once in a `once` block (the counterpart of the source's
   `EUDOnStart`), keeping the 32-test read off the per-segment path.

9. **Reset to the loop's `goto`, not to bar 1.** The source's reset action is
   `self._set_goto[0] << self.current_bar.SetNumber(1)`: it always writes 1,
   while the per-loop `goto` value is written to `_set_goto[0] + 20`. The port
   deliberately resets `current_bar` to `loop.goto` (and the packed digits and
   the progress slot to match). This is the documented intent — "if the last
   bar was played, go to start" — and without it the `goto` argument would be
   dead. The divergence is recorded here because it changes runtime behaviour
   relative to a literal transcription of the source.

10. **Frontend-consistent validation.** Unknown loop titles, constant bars
    above 999, non-constant indices where the address is needed at build time
    (`sendbar`, `setloopbar`, `sendcurrentbar`, `cond`), non-finite or
    negative segment lengths, and out-of-range `goto`/`count` fail with the
    same `EUDSL-*` codes on both frontends. The source's `UnboundLocalError`
    in `play(title=None, bar=None)` becomes "read the current loop's slot",
    guarded so a paused player is a no-op.

## Consequences

- Both frontends produce the same archive names, the same threshold
  expression and the same bar digits for the same input; the py and TS suites
  cover segment scanning, the length parser, digits, reset, pause/resume and
  CurrentPlayer save/restore.
- Builds are reproducible: no random ids in HIR, cost tables or the MPQ.
- Idle cost is one probe trigger (two conditions: clock read and
  `current_loop != -1`) plus the `when` gate; the clock is not read until a
  pass is due. Segment bytes dominate output size.
- `Db` / `DbRef` (#99) stays unused here by design; `embedFile` (ADR 0021) is
  the only segment surface.
- Existing maps, dumps and cost goldens are unchanged: nothing is emitted
  unless a caller constructs a `SoundLoops` and calls `player()`.

## Rejected alternatives

- **Ship segments through `Db` / `DbRef` (#99).** Rejected: the blob lives in
  the trigger payload and `PlayWAV` looks up an MPQ archive name, so the
  segments would be silent.
- **Keep the source's random five-character ids.** Rejected: two builds of the
  same map would differ, which breaks reproducible dumps and diffing.
- **Transcribe the source's `goto` write literally.** Rejected: it always
  resets to bar 1 and silently ignores the registered `goto`; the port follows
  the documented intent and records the difference (Decision 9).
- **Allow cp949 manual ids.** Rejected: no cp949 codec in the TS frontend, and
  a five-byte ASCII id is the common subset.
- **Model `EUDMethod` / `EUDSwitch` jump tables.** Rejected: dispatch happens
  only on a due pass, and per-loop/per-bar `when` chains are the idiom the
  rest of the helper layer already uses.
- **Allocate the `StringBuffer` path.** Rejected: dead in this source version;
  the port would pay eight bytes and a string write for output nobody reads.
- **Read the local player id every pass.** Rejected: it is a 32-test memory
  read that never changes; a `once` block matches `EUDOnStart` and keeps the
  due pass cheaper.
