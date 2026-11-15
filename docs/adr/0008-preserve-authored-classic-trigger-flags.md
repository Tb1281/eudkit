# Preserve authored basemap classic-trigger flags

Status: accepted (2026-11-05)

## Context

The stage-1 injector rebuilds `TRIG` as the stage-1 setup block,
the CurrentPlayer reset, and the basemap's own classic triggers. Like
eudplib's stage 1, it XORs the disabled bit (mask `0x8`, flag dword at
byte 2368) on every basemap trigger. eudplib's stage 3 `_flip_prop` XORs
the bit back at runtime and dispatches compiled inline code; `eudkit` has no
stage 3. Without it, authored enabled triggers stay permanently disabled
in the compiled map.

## Decision

When the payload carries no stage-3 dispatch wiring
(`dispatch_wired_func_ids` is empty), `initialize_payload` preserves
every **authored classic trigger**'s flag dword byte-for-byte. Only the
**synthetic injector trigger** appended by `preprocess_inline_code` (the
trailing blank terminator) keeps the stage-1 XOR. Authored enabled stays
enabled and authored disabled stays disabled from the first trigger
loop.

When the payload does carry wiring, the global XOR stays. There the
payload's flip is what restores the chain at runtime, including
synthesized dispatchers whose enabled flag is the XOR's input;
preserving flags on that path would invert the bug and stop inline code
from dispatching.

## Considered options

- **One-shot runtime restore (eudplib stage-3 shape).** Rejected: it
  needs a guard and extra triggers, only corrects loop 1, and from loop 2
  the chain order is unchanged. `eudkit` also lacks eudplib's runner-pair
  rewiring, so the runtime flip would imitate the symptom without the
  ordering fix.
- **Blanket enable.** Rejected: it changes triggers the author disabled,
  which is not the authored state.

## Consequences

- Every compiled map built on a basemap with authored classic triggers
  changes at those flag dwords. This is intended.
- Blank basemaps are unchanged: the 71 bootstrap records plus the toggled
  blank terminator (`0x0C`) stay byte-identical.
- No LIR or cost change. The tick gate's target and behavior are
  unchanged; authored triggers keep their native per-chain cadence.
- **First-loop ordering divergence.** An authored classic trigger can
  execute (and `Wait`) before the payload on the first pass, whereas
  eudplib temporarily suppresses it. Accepted within #40's restore-only
  scope; the runner-boundary work owns payload/classic ordering and the
  `TrigTriggerBegin`/`TrigTriggerEnd` pair.
- **#41's `Preserve` path** keeps authored flags untouched and still does
  not dispatch inline code. That is its contract, not a new regression.
- **Follow-up (ADR 0010).** The first-loop ordering question is resolved:
  classic-first every loop, bracketed by `TrigTriggerBegin` /
  `TrigTriggerEnd`. The divergence described above is now the contract.
