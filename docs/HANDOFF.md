# Handoff: `eudplib` / `euddraft` Feature Absorption (`feat/absorb-eudplib-euddraft`)

> Self-contained continuation guide for a fresh session after `git clone` or
> `git pull` on any machine.

## Current State

- **Branch**: `feat/absorb-eudplib-euddraft` (branched from `master` at `2ba939c`)
- **Latest Code Commit**: `432ee13` (`chore(absorb): Wave 2 coordinator barrel, ADR 0037-0039, and glossary handoff`)
- **Stage**: **Cycle 1 Wave 1 + Wave 2 Implementation & Merge COMPLETE** → Ready for **Stage 5–6 (Two-Axis Review, Recursive Refinement Loop, Merge to `master`)**.

### Completed Commits on `feat/absorb-eudplib-euddraft`

1. **Wave 1 (`mathf`, `random`, `player`) + Coordinator Handoff**:
   - `230ce38` — `feat(random)`: per-builder LCG PRNG (`py/random.py`, `ts/random.ts`, `crates/eudkit-cli/tests/random_runtime.rs`), tick seeding via `GAME_TICK_ADDR` (`0x57F23C`), `rand_range`, `dwrand` ([ADR 0035](./adr/0035-per-builder-lcg-prng.md)).
   - `f32a0a5` — `feat(mathf)`: signed div/mod (`towards_zero`, `floor`, `euclid`), compile-time `pow`, shared Q16 trig payload tables (`atan2`, `atan2_256`, `lengthdir`, `lengthdir_256`), `binary_min`/`binary_max` (`steps = (hi - lo + 1).bit_length()`) ([ADR 0034](./adr/0034-signed-math-trig-and-binary-search.md)).
   - `54e15f4` — `feat(player)`: `turbo()`, `user_player()`, `game_tick()`, `is_user()`, arithmetic `player_exists(0..7)`, `Player(0..11)` / `PlayerField` dword arrays, 8-slot `PVariable`, CP-preserving `for_each_player` + 6 `*_all` broadcasts ([ADR 0036](./adr/0036-player-state-pvariable-and-broadcasts.md)).
   - `9acbef6` — Wave 1 barrel (`py/__init__.py`, `ts/index.ts`), `GLOSSARY.md`, ADRs `0034`–`0036`.
2. **Wave 2 (`location`, `scstate`, `view`, `queue`) + Coordinator Handoff**:
   - `241e69e` — `feat(location)`: `Location` 5-dword handle at `0x58DC4C`, `getloc_tl` returning `(left, top)`, `memsetEPD` dynamic path, build-time `draw_circle`/`draw_line`/`draw_polygon`/`draw_star` ([ADR 0037](./adr/0037-location-handles-and-geometry.md)).
   - `1f59623` — `feat(scstate)`: SC/BW split byte tables (`0x58CF44`/24 vs `0x58F140`/20 for tech; `0x58D2B0`/46 vs `0x58F32C`/15 for upgrade), masked `DeathsX`/`SetDeathsX`, 4-lane `with_epd` dispatch ([ADR 0038](./adr/0038-tech-and-upgrade-state-tables.md)).
   - `bcf692c` — `feat(view,queue)`: `CUnitView` facade, `split_dword`/`split_word`, aligned `read_bit`/`read_bit_var`/`write_bit`/`write_mask`, `StatusFlags`/`PathingFlags`/`GroupFlags`/`AvailabilityFlags`, LIFO `ObjectPool` (`POOL_NULL = 0xFFFFFFFF`) ([ADR 0039](./adr/0039-cunit-view-bit-helpers-and-object-pool.md)).
   - `432ee13` — Wave 2 barrel (`py/__init__.py` = 596 exports, `ts/index.ts`), `GLOSSARY.md`, ADRs `0037`–`0039`.

### Verified Baseline (at `432ee13`)

- `python3 -c 'import py; assert len(py.__all__) == len(set(py.__all__))'` → `596` exports, OK
- `cd ts && npm run typecheck` → `tsc --noEmit` + `truthiness: clean (168 files)`
- `cd ts && npm test` → `1488 pass, 0 fail`
- `make test-py` → `Ran 1574 tests`, `OK`
- `cargo test -p eudkit` → `157 passed, 0 failed` (`cunit_reader`, `location_runtime`, `mathf_runtime`, `player_runtime`, `random_runtime`, `scstate_runtime`, `view_pool_runtime`, etc.)

*(Note: `make test-examples` and full `cargo test --workspace` were not yet re-run at `432ee13`.)*

---

## Architecture Constraints & Exclusions (Must Preserve)

1. **Zero `schema/` edits** (`schema/layouts/*.json`, `schema/stock-rows.json` untouched).
2. **Zero new `EUDSL-*` diagnostic codes** (use only the 16 canonical codes in `docs/diagnostics.md`).
3. **No public hack primitives** in DSL modules (`EUDByteReader`, `EUDLightBool`, `Forward`, `SetNextPtr`, `Switch`, `VProc`). Lowering/Opt (`crates/eudkit-lower`, `crates/eudkit-opt`) still uses `SetNextPtr` / `TriggerPage` internally; high-level DSL modules must not.
4. **No unverified 1.16-only hacks**: `MSQC`, `QGC`, `chatEvent`, `freeze/` remain out of scope unless SC:R memory safety is separately proven.
5. **Runtime/Static Cost Superiority & Py/TS Parity**:
   - Python (`snake_case` + `camelCase` alias) and TypeScript (`camelCase` + `snake_case` alias) must emit byte-identical HIR and identical executed trigger counts.
   - Guard against TDZ circular imports in TS (`ts/view.ts` intentionally avoids importing `ts/blockio.ts` directly).
6. **Raw `int` location id boundary**: Broadcast helpers (`py/player.py`) and `CUnitView.set_location` (`py/view.py`) take raw `0..255` integer location ids so `player`, `location`, and `view` stay decoupled.

---

## Exact Remaining Tasks (Next Session Checklist)

### 1. Stage 5 — Two-Axis Code Review on `git diff master..feat/absorb-eudplib-euddraft`
Inspect both axes thoroughly (in parallel if orchestrating):
- **Axis 1 (Engineering Standards, Deduplication, Exports & Diagnostics)**:
  - **Known Bug to Fix**: `py/mathf.py` replaced `__all__ = ["isqrt"]` without keeping `"isqrt"` in `__all__` (`py/mathf.py` & `py/tests/test_mathf_ext.py` currently omit `"isqrt"`). Restore `"isqrt"` in `py/mathf.py.__all__` and update `py/tests/test_mathf_ext.py`.
  - Check for duplicated player-field tables, duplicated SC/BW slot helpers, or duplicated validation/trigger-packing logic across `mathf`, `player`, `location`, `scstate`, `view`.
  - Verify `ts/index.ts` and `py/__init__.py` export parity (`barrel_parity` if present) and snake/camel alias identity in Python.
- **Axis 2 (SC:R Addresses, Runtime Trigger Cost vs `eudplib`, Exclusions)**:
  - Cross-check all memory bases/strides (`0x51A280`, `0x512684`, `0x57F23C`, `0x6509A0`, `0x57F0F0..0x5822F4`, `0x58DC4C`, `0x58CF44`/`0x58F140`, `0x58D2B0`/`0x58F32C`, `cunit.json`/`csprite.json` offsets).
  - Evaluate whether `for_each_player(active_only=True)` runtime loop (`~825` executed triggers due to 8x nested `with_epd` inside `player_exists(Var)`) can reuse the single ambient CP save or cheaper existence check cleanly without sacrificing static cost, OR document the static-vs-executed trade-off cleanly.
  - Verify zero `SetNextPtr`/`Forward`/`Switch`/`VProc` leakage, zero new `EUDSL-*` codes, zero `schema/` diffs.

### 2. Stage 6 — Recursive Refinement Loop & Cost-Benefit Evaluation
- Fix `[P0]`/`[P1]` review findings and deepen clean abstractions (`isqrt` `__all__` fix + any deduplication / runtime cost improvements).
- Check whether **Cycle 2** candidates from `eudplib` / `euddraft` have positive net benefit (otherwise skip cleanly):
  - Candidate: additional `CUnitView` / `CSprite` ergonomic helpers or shared `(mask, shift)` field accessors, or `examples/` showcase/update if helpful;
  - Deliberately **skip**: `MSQC`, `QGC`, `chatEvent`, `freeze/`, `ConstFixedArray` runtime indexing redesign (separate compiler pass), per-player CUnit linked-list relinking in `give_to`.
- Re-run Two-Axis Confirmatory Review until `NET_BENEFIT` is `low`/`none` and zero `[P0]`/`[P1]` remain.

### 3. Stage 7 — Full Final Verification, Merge & Push
Run all repository checks cleanly after the final edit:
```bash
cargo build --workspace
(cd ts && npm run typecheck && npm test)
make test-py
cargo test --workspace
make test-examples
```
Then clean up `docs/HANDOFF.md` if appropriate (or move notes to PR/ADR), fast-forward / merge `feat/absorb-eudplib-euddraft` into `master`, push `origin/master`, and delete the feature branch.

---

## Environment Setup on a New Machine

1. **Dependencies** (`CONTRIBUTING.md`):
   - Rust (`cargo`), Python 3.12+ (`python3 -m unittest` from repo root with `PYTHONPATH=.` — **never** `pytest`), Node.js (`npm`, **not** `bun`).
   - First-time TS setup: `cd ts && npm ci`
2. **Or using `js_binary` orchestration** (optional):
   - Use an isolated `JS_BINARY_DATA_DIR=/tmp/jsb-eudkit-...`, default model (`env -u JS_BINARY_MODEL`), and respect the user's limit gate (`JS_BINARY_TEST_SCRIPT=~/limit-gate.sh` if applicable).
   - Clean up any temporary `git worktree` slots and stop the isolated daemon before finishing.

---

## Suggested Skills

For the next agent resuming this task, invoke:
- `code-review` — Run the Stage 5 Two-Axis review on `git diff master..feat/absorb-eudplib-euddraft`.
- `codebase-design` — Evaluate deepening/deduplication opportunities cleanly before Cycle 1 refinement or Cycle 2 gating.
- `tdd` — Keep Py, TS, and Rust simulator (`common::run_case`) parity tests green during refinement edits.
