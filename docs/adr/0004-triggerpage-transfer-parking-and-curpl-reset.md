# TriggerPage Transfers, Parking, and curpl Reset

Status: accepted (2026-11-04)

## Context

`TriggerPage` action slots maintain the self-targeted action invariant at rest:
`act[k].dest = EPD(act[k].value)` with `modifier = SetTo` (ADR 0001). A transfer
between two page-backed variables patches the source slot's `dest` to the
destination's `value` field for one firing, then restores it. SC:R snapshots a
trigger's action fields when the trigger starts, so a destination slot that
fires after the source would restore its old snapshot over the transferred
value unless its self-write is redirected for that firing.

Separately, the 70 stage-1 injector triggers leave `curpl` (`0x6509B0`)
pointing at the last trigger they patched. The `eudkit` payload starts with
`DisplayText` and needs `curpl` to name a valid player, otherwise SC:R renders
the map blank. eudplib's finalizer resets it in `inj_finalizer.py`.

## Decision

1. **Setup/restore are a pair.** Transfer setup actions patch the source
   slot's `dest` to the destination `value` address; the continuation trigger
   patches it back to its own `value` address. Both triggers are preserved, so
   the pair runs on every trigger loop.
2. **Park only ordered destinations.** A destination slot needs parking only
   when its page is active and `(dst_page, dst_slot) > (src_page, src_slot)`.
   Sources redirected by another transfer and self-only sources are handled
   separately, so an incoming transfer is never erased by a later self-write.
3. **Park in `wavid`.** Redirect the destination slot's `dest` to its own
   `wavid` field (`+8`) for one firing. `SetDeathsX` ignores `wavid`, so the
   self-write is inert. The continuation trigger restores
   `dest = EPD(value)` before the next firing. Parking is re-applied every
   loop, not made one-shot; `parked_dsts` only deduplicates multiple transfers
   into the same destination within one firing.
4. **Reset `curpl` every loop.** Emit one preserved
   `SetMemory(0x6509B0, SetTo, 0)` trigger after the 70 stage-1 triggers
   (`TrigTriggerBegin`). ADR 0010 extends this to the runner boundary: a
   matching `TrigTriggerEnd` reset runs after the authored classic chain. The
   stage-1 triggers are preserved and recompute `curpl` on every pass, so a
   one-shot reset would only protect the first loop.
5. **Keep the reset out of the stage-1 fixture.** The reset trigger is emitted
   separately so `tests/fixtures/stage1_trigs_py.bin` remains byte-identical to
   Python's 70-trigger list.
6. **Bare `{}` borrows and resets `curpl`.** The variable-width decimal
   byte-lane writer (ADR 0014, issue #87) is pointer-based: it seeds `curpl`
   with the dword holding the next content byte (`base + (pos >> 2)`), keeps
   `curpl = base + (pos >> 2)` after every emitted byte by advancing one dword
   on lane-3 wraps, and emits `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` at
   the end of every `write` / `append` / `writeFrom` run that borrowed it. A
   known-lane run (static text, `{:x}`) never borrows the register and emits no
   reset. The reset uses the same resting value (0 = player 1) as decision 4 and
   the `camera.py` restore.
7. **`with_cp` and `with_epd` have different restore policies.** `with_cp`
   takes a constant int EPD and its `restore()` only re-anchors the cursor
   (a no-op at offset 0); `with_epd` takes a runtime EPD and restores the
   ambient CurrentPlayer value unconditionally
   ([ADR 0029](./0029-runtime-epd-addressing.md) §7). The C7 writer and the
   per-loop reset above keep their cursor-only/resting-0 behavior.

## Consequences

- The parking mechanism assumes SC:R treats `wavid` as unused for
  `SetDeathsX`. If that engine behavior changes, parking must move to another
  unused action field; the self-targeted invariant and the repeated-loop
  snapshot test remain the contract.
- The curpl reset adds one static trigger after stage 1; ADR 0010 adds the
  matching `TrigTriggerEnd` after the authored chain. The begin placement
  (after stage 1, before the preserved template triggers) is part of the
  contract; moving it earlier would let stage 1 clobber it again.
- `coalesced_var_batch_fires_shared_trigger_page_once_and_restores_self_targeted_invariant`
  exercises three snapshot-mode loops and asserts both the transferred values
  and the restored page invariant.
- The bare-`{}` borrow is per run, not additive to the per-loop reset: a write
  nested in a conditional borrows `curpl` only when that branch runs, and the
  decision-4 reset still runs every loop afterwards. Authored code that needs a
  CP value across a bare `{}` write must re-seed after it, as `camera.py` does
  before `CenterView`.
- While `curpl` is borrowed, `Deaths(CURRENT_PLAYER, ...)` addresses the target
  string dword, not a player id. Do not interleave other `CurrentPlayer`-relative
  reads with an in-flight variable-width write.
