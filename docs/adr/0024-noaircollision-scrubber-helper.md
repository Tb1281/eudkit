# noAirCollision: an unrolled memory scrubber as a core helper

Status: accepted (2026-11-12)

## Context

euddraft's `plugins/noAirCollision.py` is ported the same way as the earlier
plugin ports (cammove, bgmplayer, unlimiter, dataDumper, soundlooper): a plain
build-time constructor on both frontends
(`py/noaircollision.py`, `ts/noaircollision.ts`), no plugin system, no
lifecycle hook, byte-for-byte parity between the two frontends.

The source has two halves:

1. `beforeTriggerExec` emits 115 forward triggers `t[0] .. t[114]` holding
   64 `SetMemory(dummy, SetTo, 0)` actions each, except `t[114]`, which holds
   15 — 7,311 actions in total. Every action writes the same placeholder
   dword; the destination is meaningless until patched.
2. `onPluginStart` reads the dword at `0x6D5CD8` (the game's collision-table
   pointer) with eudplib's `f_epdread_epd`, giving the table's base EPD `P`,
   then walks the 115 triggers and rewrites each action's `dest` field to
   `P + n` for `n = 0 .. 7310`.

After the first trigger loop the module zeroes the whole collision table every
loop, one `SetMemory` action per dword, which keeps each write at `1T` — the
entire point of the plugin.

## Decision

1. **Emit the source's trigger shape one to one.** `noAirCollision()`
   registers 115 condition-free, preserved zeroing triggers with the exact
   `64 x 114 + 15` split (`TOTAL_ACTIONS = 7,311`). Each action starts as
   `SetMemory(CP_ADDR, SetTo, 0)`. `CP_ADDR` (`0x6509B0`, the `curpl`
   scratch dword) is the port's "harmless placeholder": ADR 0004 leaves it at
   0 at every trigger-loop boundary, so an unpatched action is a no-op on a
   register whose resting value is already 0.

2. **Emit the patch block before the zeroing triggers.** The source's
   `onPluginStart` runs before `beforeTriggerExec`, so the port registers a
   `once` block ahead of the 115 triggers. The first loop patches every
   destination and then zeroes the table; later loops only run the zeroing
   triggers.

3. **Address the slots without a runtime array.** The source keeps
   `s[i] = EPD(t[i]) + 86` in a eudplib variable array. eudkit variables hold
   no absolute addresses (ADR 0001 decision 1), and consecutive trigger
   records are a fixed `602` EPDs apart (ADR 0001 decision 2), so the walk
   needs no array: it starts at `EPD(t[0]) + 86` and skips
   `602 - 64 * 8 = 90` EPDs from the last action of one trigger to the first
   action of the next. The visited destination sequence is exactly
   `EPD(t[i]) + 86 + k`, matching the source's per-trigger base plus `+8`
   stride.

4. **Patch through a scratch write trigger.** eudkit's HIR has no general
   runtime store to a computed address: `emitVarStore` destinations are static,
   and a `Var` used as a `SetMemoryEPD` destination resolves to the variable's
   *storage address*, not its value. ADR 0029 §7 supersedes this only for the
   CP-relative constructor (`with_epd`, a scalar runtime base with constant
   relative hops); the scrubber here still needs arbitrary per-iteration
   destinations and keeps the static path. The port therefore keeps the
   source's shape — one write per iteration — by writing through one scratch
   trigger whose own `dest`/`value` fields the loop advances in place, the
   same idiom as the unlimiter port. One mode-1 relocation points the seed
   action's `dest` field at the scratch's `dest` field and fills its `value`
   field with `EPD(t[0].act(0).dest)`; a variable store then writes the table
   EPD `P` into the scratch's value field. Per iteration the loop emits the
   scratch trigger, then `dest += 8`, `value += 1`, `k += 8`.

5. **Keep the source's loop bounds.** A full trigger walks `k <= 63 * 8 =
   504`; the outer loop lowers the bound to `15 * 8 = 120` under a
   `when(i == 114)` before the inner loop starts, which is the source's
   `EUDBreakIf([i == 114, k >= 15 * 8])`.

6. **Pin the addressing constants.** `FIRST_ACTION_DEST_EPD_OFFSET` is derived
   from the shared action record offsets (`(328 + 16) / 4 = 86`),
   `ACTION_EPD_STRIDE` from the action stride (`8`), and
   `CROSS_TRIGGER_EPD_STEP` from `TRIGGER_STRIDE_EPDS - 64 * 8` (`90`), so a
   change to the trigger record layout breaks the constants instead of
   silently mis-addressing the table. Both frontends export the same names.

7. **The walk assumes the recorded layout.** The `+90` skip is only correct
   while the 115 zeroing triggers are consecutive trigger records. That holds
   because they are registered consecutively and an unconditional preserved
   block is emitted in 64-action chunks, filling exactly one record per full
   trigger; the helper is documented as a module-top-level (or trigger-only
   body) call. Bare actions interleaved in the same block as the call are the
   one unsupported placement, because they can be packed into the first
   trigger's action run.

## Consequences

- Both frontends emit byte-identical HIR for the same program; the Rust
  parity test lowers it, runs the first loop in the simulator with a seeded
  `0x6D5CD8`, and proves all 7,311 table dwords are zeroed while the dword
  before the table and the two after it are untouched.
- Per-loop cost is the source's: 7,311 `SetMemory` actions across 115 trigger
  evaluations. That is the deliberate price of `1T` per dword.
- Arming costs one pass of the patch loop on the first trigger loop: 7,311
  iterations of scratch write + two field bumps + the loop check, plus the
  32-test `f_epdread_epd` read and the seed.
- Static cost is 115 `2,408`-byte triggers (`276,920` bytes) plus the small
  patch block. A module that arms only this helper measures 163 triggers /
  33 conditions / 7,442 actions and 392,504 trigger bytes (+8 blob bytes)
  before assembly. The helper does not add payload blobs of its own.
- Existing maps, dumps and cost goldens are unchanged: nothing is emitted
  unless a caller invokes `noAirCollision()`.

## Rejected alternatives

- **Unroll the patched destinations as `Var` stores.** Rejected: the HIR has
  no dynamic-destination store, and unrolling 7,311 static destinations would
  emit megabytes of shadow variables/acts, far beyond the source's own cost.
- **Emit a memory-copy loop (`memcpyEPD`).** Rejected: one `SetMemory` per
  dword is what keeps each write at `1T`; a copy loop adds per-iteration
  control cost and diverges from the source's trigger shape.
- **Keep a runtime array of 115 base EPDs.** Rejected: eudkit variables hold
  no absolute addresses, and the fixed 602-EPD trigger stride removes the
  need for the array entirely.
- **State the plugin's setup/patch as a build-time patch.** Rejected: the
  table address is only known at run time (the game writes `0x6D5CD8`), so
  the walk must remain a runtime loop.
