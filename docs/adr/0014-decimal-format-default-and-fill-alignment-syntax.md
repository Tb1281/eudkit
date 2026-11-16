# Decimal Format Default, Format Mini-Language, and Direct Digit Extraction

Status: accepted (2026-11-06; renumbered from ADR 0012; amended 2026-11-07, 2026-11-11, 2026-11-15)

## Context

`FixedString` in `eudkit` supports dynamic variable interpolation. Prior to this ADR, bare `{}` defaulted to 8-digit hex (`{:x}`) due to its fixed 8-byte (2 dword) footprint, which enabled compile-time constant-offset folding ([ADR 0011](./0011-dword-packed-hex-and-constant-offset-string-formatting.md)). Decimal formatting (`{:d}`) was variable-length and implemented via a runtime division ladder (`work.div(power)`), emitting quotient division loops that cost ~884T static per variable.

Defaulting bare `{}` to 8-digit hex violated the principle of least astonishment: in modern languages like Python and Rust, `{}` formats integers in decimal, whereas hex requires an explicit `{:x}` specifier. Common HUD metrics like wave count and hit points rendered as `wave=00000005 hp=00000064` instead of `wave=5 hp=100`.

However, switching bare `{}` to decimal while retaining the division ladder would make the default HUD path prohibitive in trigger cost and lose the compile-time constant-offset guarantees from ADR 0011.

## Decision

1. **Format Mini-Language Subset (`[[fill]align]width[type]`)**:
   - Both Python (`py/string.py`) and TypeScript (`ts/string.ts`) frontends implement a unified format specification parser supporting:
     $$\text{spec} := [[\text{fill}]\text{align}]\text{width}[\text{type}]$$
   - **Type**:
     - `d` (decimal, width 1–10).
     - `x` (lowercase hex, width 8).
     - Bare `{}` defaults to decimal (`{:d}`).
   - **Alignment**:
     - Only right-alignment (`>`) is supported for numeric placeholders. Left (`<`) and center (`^`) alignments are rejected with diagnostic `EUDSL-FORMAT`.
   - **Fill**:
     - Any ASCII byte is a valid fill. `\x1a` (StarCraft: Remastered invisible pad byte `0x1A`), `'0'` (zero-fill), and `' '` (space-fill) are the canonical HUD fills.
     - Examples: `{:\x1a>4d}`, `{:04d}`, `{:4d}`, `{:*>4d}`, `{:x}`.

2. **Direct 4-Test Digit Extraction Engine**:
   - Replace the runtime quotient division ladder with direct binary test extraction:
     - For power $10^9$: 3 tests ($4, 2, 1 \times 10^9$) because $2^{32} - 1 \approx 4.29 \times 10^9 < 5 \times 10^9$.
     - For powers $10^8 \dots 10^0$: 4 binary tests ($8, 4, 2, 1 \times 10^k$).
   - Each binary test is implemented in exactly **1 trigger (1T static and 1T executed)**:
     - Condition: `Deaths(work.value, AtLeast, weight * 10^k, 0)`
     - Action 1: `SetDeaths(work.value, Subtract, weight * 10^k, 0)`
     - Action 2: add the digit weight to the byte accumulator: a scratch `digit` Var on the dynamic path, or the target string dword's masked byte on the compile-time fixed-width path (decision 3).
   - Straight-line, branch-free execution eliminates runtime loop jumps and conditional digit-group skipping overhead.

3. **Fixed-Width Decimal Formatting with Compile-Time Lane Pre-Shifting**:
   - When an explicit width $W$ is specified (e.g. `{:\x1a>4d}`):
     - **Modulo $10^W$ Wrap**: Values wrap modulo $10^W$ (`work.imod(10**W)` for $W < 10$) as the overflow contract.
     - **Truncation**: Only powers $10^{W-1} \dots 10^0$ are evaluated.
     - **Compile-Time Constant Offset**: Every character position $pos = static\_pos + k$ for $k \in [0, W-1]$ is known at build time.
     - **Pre-Shifted Lane Weights**: The byte shift is pre-baked at compile time (`weight << (8 * (pos & 3))`), eliminating runtime `pos.shr(2)` (saving 30 triggers per byte) and runtime `ishl`.
     - **Direct Masked Byte Writes**: Every digit byte is written by `SetStringDword(index, dword_offset, value << (8 * lane), 0xFF << (8 * lane), SetTo | Add)`. The destination is a compile-time `target_string` reloc, so the fixed-width path contains no scratch trigger, no `emitVarStore` field patch, and no runtime `pos.shr(2)`.
     - **Zero Suppression**:
       - Fill `'0'`: no suppression; all digits render ASCII `'0' + d`.
       - Fill `\x1a` or `' '`: leading zero positions receive the fill byte. Each suppressed position starts as a masked `SetTo` of the fill byte; a `started` flag is set from `work >= 10^k` before the weight tests (exactly "this digit is nonzero"), and once set a masked `Add` of `(0x30 - fill) & 0xFF` turns the fill byte into ASCII `'0'` before the digit weights accumulate.
       - Units place ($10^0$) NEVER suppresses: if value is 0, units place renders ASCII `'0'`. Non-suppressed positions start at ASCII `'0'` instead of the fill byte.
     - **Static Offset Advancement**: Advances `static_pos += W`, preserving constant-offset folding for following literals and placeholders.

4. **Dynamic Variable-Width Decimal Fallback (`{}` / `{:d}`)**:
   - Variable-width decimal evaluates powers $10^9 \dots 10^0$ using the direct extraction engine.
   - Uses a dynamic `started` flag to suppress leading zeros.
   - Emits active digits and the units place through the **CP-cursor byte-lane writer**: `CurrentPlayer` (`0x6509B0`) is seeded once per format run with `base + (pos >> 2)`, each byte site selects its lane with one masked equality (`pos & 3`), writes the compile-time `0x30` base plus the digit's constant `1/2/4/8` bit adds, and advances the cursor on lane 3. A seed is also emitted on demand by any unknown-lane write outside a format run (runtime `writeFrom`, dynamic fixed-width decimal, dynamic static runs), with a runtime `pos >> 2`. Every run that borrowed the cursor restores `curpl` to 0 (ADR 0004).

5. **Frontend Parity**:
   - Both `py/string.py` and `ts/string.ts` produce byte-identical HIR triggers, identical condition orders, and identical action masks.

## Consequences

- **Astonishment Eliminated**: Bare `{}` renders human-readable decimal integers (`wave=5 hp=100`), matching Rust and Python standard library conventions.
- **Measured Cost Contract**:
  - Fixed-width decimal `{:\x1a>4d}` compiles to **51T static / 53T executed per 4-digit placeholder in isolation** (44 conditions, 140 actions static; 44 conditions, 215 actions executed). The two-variable sample below measures 100T static / 105T executed.
  - The earlier **~25T** estimate counted only the 16 binary digit tests plus zero suppression and byte writes. It omitted the modulo-$10^W$ wrap ladder: `work.imod(10**W)` for $W=4$ is a generic 19-trigger binary long-division remainder (18 down to 0), because 10000 is not a power of two. The wrap contract still requires that ladder, so the old target remains retired. The measured number above is the honest remaining cost after removing per-digit byte-write pointer patching.
  - Bare decimal `{}` compiles to **688T static / 105T executed per variable at 2 digits** (492 conditions, 841 actions static; 90 conditions, 238 actions executed). The earlier **~50–60T** estimate counted only the 39 direct extraction tests (3 for $10^9$, 4 each for $10^8 \dots 10^0$), not variable-width byte emission.
  - Static cost is value-independent: the same **688T** covers 1, 2 and 10 digits; only the executed cost moves with the value (**94T** at 1 digit, **105T** at 2, **193T** at 10). The CP-cursor writer seeds `curpl` once per run, selects the byte lane with one masked equality, writes `0x30` plus four constant bit adds per lane, and advances the cursor on lane 3. Of the 688T, about **101T** is digit extraction, **3T** is the cursor seed + reset, **104T** is the four-lane dispatch and constant lane write, and **480T** is the per-lane bit-add ladder. That ladder is not removable with the current action model: an action value field is a literal, so a runtime byte must be decomposed into bit tests. The ablation floor with the byte writer deleted entirely is **104T static / 79T executed**, so a "~110T static / ~48T exec" projection is unattainable; it prices only the extraction side and leaves no room for emitting the bytes.
  - The known-lane path is unchanged: `{:x}` and static runs with a provable lane keep byte writes at their compile-time dword and lane, with no cursor borrow. The cost gate fixture uses `{:x}` and does not move. Unknown-lane writes outside a format run (runtime `writeFrom`, dynamic fixed-width decimal) seed the same cursor on demand with a runtime `pos >> 2` (about 30T once per run, not per byte).
  - Two-variable sample HUD (`"wave={:\x1a>4d} hp={:\x1a>4d}"`) compiles to **100T pre-assembly dump-ir static triggers / 105T executed** (88 conditions, 220 actions static; 87 conditions, 435 actions executed). It preserves constant-offset folding: HIR contains zero varstores and no dynamic `when` statements.
  - The fixed-width decimal byte writes are **zero-patching**: no runtime action fields are patched. Each digit byte is a masked `SetStringDword` action at its compile-time dword offset and lane, resolved through a `target_string` reloc. There is no scratch trigger, no `emitVarStore` call, and no runtime `pos.shr(2)`.
  - The runtime-position fallback is still dynamic by necessity: `writeFrom` with a Var offset (and variable-width decimal) cannot know its byte lane at build time, so `_append_dynamic_byte` keeps a four-way lane dispatch, now keyed by one masked equality on `pos & 3`. It writes through the same CP cursor instead of patching a scratch trigger's dest/value fields: one cursor seed (runtime `pos >> 2` when the start is unknown) plus constant bit adds per lane, and no `emitVarStore` per byte. That fallback is not part of the compile-time fixed-width path.
  - The `cost_gate.ts` / `cost_gate.py` fixtures write only the static text `"cost-gate"` and contain no dynamic decimal placeholder, so this re-baseline does not change their golden reports.
- **Breaking Change & Migration**:
  - Code relying on bare `{}` emitting 8-digit hex must be migrated to explicit `{:x}` or `{:08x}`.

## Amendment (2026-11-15)

Fill and alignment stay **ASCII-only** because a fill byte is repeated as a
single byte. A multi-byte character has no single-byte repeat semantics, so
`{:한>4d}` is rejected with `EUDSL-ASCII` rather than padded with its UTF-8
bytes or truncated to one byte. This restriction governs only the format spec:
literal string content is UTF-8 (ADR 0003), so `s.write("한")` is legal.
