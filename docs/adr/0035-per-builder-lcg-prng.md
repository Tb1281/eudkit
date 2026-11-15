# Per-builder LCG PRNG with deterministic tick seeding

Status: accepted (2026-11-16)

## Context

`eudplib`'s `random` module keeps one process-global `EUDVariable` seed and
uses `Switch("Switch 1", Random)` + a 32-iteration `SetNextPtr`/`Forward` loop
for `f_randomize()`. Process-global variables bind to the first builder only,
and trigger-rewiring loops violate `eudkit`'s structured HIR invariants.

## Decision

1. **Per-builder LCG state:** Seed state is a private `Var` keyed per module
   builder (`WeakKeyDictionary` / `WeakMap`), matching `py/random.py` /
   `ts/random.ts` without cross-build contamination.
2. **Standard LCG parameters and bit assembly:** `rand()` advances
   `state = state * 1103515245 + 12345 (mod 2**32)` and returns the upper 16 bits
   (`HIWORD`). `dwrand()` advances twice and packs draw 1's HIWORD into bits
   `16..31` and draw 2's HIWORD into bits `0..15` via shifts and addition.
3. **Deterministic tick seeding:** `randomize()` folds `GAME_TICK_ADDR`
   (`0x57F23C`) via `read_epd` into the LCG state rather than rewiring a `Switch`
   trigger loop.
4. **`rand_range(lo, hi)`:** Takes build-time integer bounds (`lo <= hi`) so the
   constant-divisor ladder bounds the result cheaply in `O(1)` range arithmetic.
