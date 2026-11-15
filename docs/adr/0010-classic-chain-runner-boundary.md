# Classic-chain runner boundary and trigger ordering

Status: accepted (2026-11-05; amended by
[ADR 0013](./0013-optional-tick-gate-for-single-chain-maps.md) for
single-chain maps)

## Context

Issue #40 restored authored basemap classic triggers by preserving their flag
dwords (ADR 0008). That decision deliberately left one question open: the
order between the authored classic chain and the compiled payload. In the
current chain, stage 1 links the payload after the synthetic blank terminator,
so every trigger loop runs

```text
stage 1 -> curpl reset -> authored classic triggers -> blank -> payload
```

On the first loop this means an authored classic trigger can execute (and
`Wait`) before the payload, whereas eudplib suppresses the authored chain
until its stage-3 finalizer has run. The deferred `TrigTriggerBegin` /
`TrigTriggerEnd` runner pair was meant to own that boundary.

The compiled payload is one shared sequence of 2408-byte triggers. The
authored classic chain is per player: the engine allocates a separate trigger
node for each `(trigger, player)` pair, and the first authored node is not at a
compile-time address. Returning from the shared payload to a per-player
classic chain would therefore need a runtime pointer to that player's first
authored node. The only stable per-player pointers are the list-head fields
(`firsttrigger` / `lasttrigger`); reaching the first authored node from them
requires walking the runtime list or assuming an engine-specific node layout.
eudplib does this with a VProc-based stage-3 finalizer that rewrites each
player's list to a crash-preventer/runner pair and makes the authored chain
reachable only on demand. `eudkit` has no VProc runtime, and making the authored
chain on-demand would break the native per-chain cadence.

## Decision

1. **Ordering is classic-first, every trigger loop.** Authored classic
   triggers retain native per-chain cadence and run before the once-per-tick
   payload. The first-loop behavior is part of the contract, not a
   divergence: an authored trigger that displays text or waits runs before
   the payload on loop 1 and on every later loop. The payload gate still
   makes the compiled program execute once per game tick.

2. **The runner pair brackets the authored chain.** `TrigTriggerBegin` is a
   synthetic classic trigger placed immediately after the stage-1 setup and
   before the first authored classic trigger. `TrigTriggerEnd` is a synthetic
   classic trigger placed immediately after the last authored classic trigger
   and before the synthetic blank terminator. Both are `AllPlayers`, enabled,
   preserved, and are not subject to authored-flag preservation.

3. **The pair owns the `curpl` boundary contract.** Both triggers execute
   `SetMemory(0x6509B0, SetTo, 0)`. `begin` guarantees the authored chain
   starts with a valid player even after stage 1's pointer arithmetic; `end`
   guarantees the payload starts with a valid player even if an authored
   trigger changed `curpl`. The existing post-stage-1 reset becomes the
   `begin` trigger; the new `end` trigger is the only added record.

4. **The chain link is unchanged.** Stage 1 still writes
   `payload_offset + gate_entry_bytes` into `lasttrigger->next`, where
   `lasttrigger` is the synthetic blank after the runner end. The runner pair
   does not move the payload link or change the gate.

## Considered options

- **Payload-first every loop.** Rejected for this slice. The shared payload
  cannot statically return to a per-player authored chain. A correct return
  needs a runtime chain-address capture plus a self-modifying next-pointer
  write (VProc-like), or engine-specific node-address assumptions. Both are
  larger than the runner boundary and would need their own design note and
  in-game proof.
- **Payload-first on loop 1 only, classic-first afterwards.** Rejected for
  the same return problem. Skipping the authored chain on loop 1 without a
  runtime list walk would require toggling authored flags at runtime, which is
  eudplib's stage-3 finalizer again.
- **eudplib on-demand classic execution (`RunTrigTrigger`).** Rejected. It
  changes the authored-trigger contract from "authored triggers keep running" to "authored
  triggers run only when the map calls them", and needs the stage-3 runner
  rewiring that `eudkit` does not have.
- **Behavior-neutral pair with no `curpl` contract.** Rejected. Two no-op
  records would not establish a boundary invariant and would only add cost.

## Consequences

- The compiled `TRIG` section gains one synthetic classic trigger (`end`);
  the existing reset is renamed/treated as `begin`. Payload static cost is
  unchanged because classic triggers are not part of the LIR static cost.
- Authored classic triggers keep their bytes and flags; the synthetic blank
  terminator remains last and keeps the stage-1 toggle.
- The payload is robust against authored triggers that leave `curpl`
  pointing anywhere else: `end` restores player 0 before the gate.
- Because the payload starts with `curpl = 0`, a payload `DisplayText`
  targets Player 1. Text for the local human must set `curpl` to the user
  player id (`0x512684`) or come from a classic trigger owned by that player.
- A basemap with authored triggers compiled on the plain fixture now runs the
  authored `Hello World!` display before the payload's display. The smoke
  check for this contract uses that order.
- If payload-first ordering is required later, `TrigTriggerEnd` is the seam.
  That work needs a runtime per-player chain capture and return, and a new
  ADR that supersedes this one.
