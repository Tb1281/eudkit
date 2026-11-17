# Optional tick gate for single-chain maps

Status: accepted (2026-11-06; amends
[ADR 0010](./0010-classic-chain-runner-boundary.md) for single-chain maps)

## Context

ADR 0010 decision 1 defines classic-first ordering and says the compiled
payload is gated to execute once per game tick. That contract assumes the
normal multiplayer shape: stage 1 rewires all eight player chains to the
compiled payload, and every active player's chain evaluates it on each
trigger loop. The 35-trigger gate compares the game-tick counter
(`0x57F23C`) with the previous value and skips duplicate executions inside
the same tick, so N active chains still run the program once.

Some maps are deliberately built for exactly one active chain
(single-player missions, or one player/force owning the only runtime
chain). With one chain the gate has no duplicates to remove; the payload
already runs once per trigger loop. Keeping the gate costs 35 static
triggers and 33 conditions that never suppress anything.

Although `eudkit compile --single-player` / `--no-tick-gate` were added for that
case, they did not initially supersede ADR 0010 decision 1, the lower-level
`compile_hir_to_map` entry point hardcoded the gate on, and early notes
mentioned an `eudkit.toml` option that `eudkit` does not implement. This ADR settles
those three points.

## Decision

1. **The default stays gated (ADR 0010 decision 1 unchanged).** Unless the
   caller explicitly opts out, map assembly appends the 35-trigger
   once-per-tick gate. The payload runs at most once per game tick across
   all active player chains; classic triggers, the runner pair, and
   classic-first ordering are unchanged.

2. **Single-chain builds may omit the gate.** `eudkit compile
   --single-player` and `--no-tick-gate` (synonyms) and the equivalent map
   API option omit the gate. This amends ADR 0010 decision 1 only for
   builds that set the option; every other build keeps the once-per-tick
   contract.

3. **Exactly the gate prelude is omitted.** The omitted items are the
   branch, restore, clear, and 32 bit-copy triggers: 35 triggers, 33
   conditions, and 35 actions. Stage 1 then links directly to the compiled
   program's entry (payload offset + 0) instead of the gate entry. The
   runner boundary (`TrigTriggerBegin` / `TrigTriggerEnd`), authored classic
   triggers, the synthetic blank, and stage 1 itself are untouched.

4. **Cadence without the gate.** On a single-chain map the payload runs
   once per trigger loop on that chain — native per-chain cadence — rather
   than being deduplicated to once per game tick. The gate only removes
   duplicate executions across multiple chains in the same loop; no
   measurement is cited for whether it otherwise slows the single chain, so
   no such claim is made here.

5. **The option reaches every entry point.** The CLI single-source build,
   frozen-plan builds, `eudkit-lower::compile_hir_to_map_with_options`
   (with `compile_hir_to_map` defaulting to gated), the `eudkit-compile map`
   bridge, and `eudkit-map::assemble_map` /
   `assembled_static_cost_with_options` all carry the same option. No
   opted-out path hardcodes the gate on, and the reported static cost is
   computed from the exact document that was written.

6. **No `eudkit.toml`.** `eudkit` has no configuration-file loader, and this
   change does not add one. The supported configuration surface is the CLI
   flag and the Rust API option. Plans do not persist the flag: a plan
   build must repeat it, and the frozen `expected_static_cost` rejects a
   gated/ungated mismatch before writing a map. Only the CLI/API behavior
   above is supported.

## Considered options

- **Always append the gate.** Rejected: 35 static triggers with no
  deduplication work on a single-chain map.
- **Infer single-chain automatically.** Rejected: the compiler cannot know
  how many player chains the engine will keep active at runtime. The author
  asserts the map shape by choosing the flag.
- **Implement `eudkit.toml` for one option.** Rejected: a new config
  format, discovery rules, precedence rules, and dependency are not
  justified for a single build flag. The CLI/API surface is explicit and
  testable.
- **Keep a separate positive `--enable-tick-gate` flag.** Rejected: gated is
  already the default; only the opt-out needs spelling.

## Consequences

- Single-chain builds save 35 trigger records (33 conditions, 35 actions)
  and their payload bytes; the reported static cost drops by exactly the
  gate cost.
- Default multiplayer builds are byte-for-byte unchanged.
- Using the opt-out on a map with more than one active chain makes the
  payload run once per chain per trigger loop — the duplicate execution the
  gate exists to prevent. This is a build-time author assertion, not an
  inferred property.
- Plans remain reproducible: the flag is not stored, and the frozen cost
  gate fails loudly if it is omitted or added on a rebuild.
- `eudkit-compile map` accepts the same opt-out flags for lower-level
  workflows.
