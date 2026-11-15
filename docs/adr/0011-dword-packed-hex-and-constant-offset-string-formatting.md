# Dword-Packed Direct Hex Accumulation and Constant-Offset String Formatting

Status: accepted (2026-11-06; amended 2026-11-06, 2026-11-10, 2026-11-15)

> **Amendment (2026-11-06, ADR 0014):** Bare `{}` now defaults to decimal.
> The 8-digit hex behavior described here applies to explicit `{:x}` /
> `{:08x}` format specs.

## Context

`FixedString(size)` in `eudkit` supports dynamic variable interpolation via `{:x}` (8-digit hex) and `{:d}` / bare `{}` (decimal) format specs ([ADR 0003](./0003-capacity-aware-aligned-string-writes.md); the decimal default and mini-language are [ADR 0014](./0014-decimal-format-default-and-fill-alignment-syntax.md)).

In the pre-Cycle-26 baseline implementation, formatting dynamic values into `FixedString` suffers from severe static and executed trigger inflation. (Cycle 26 later replaced the constant-shift lowering described in item 1 below with a single-trigger masked-action kernel; the $(32-k)$ figures are retained as historical context.)

1. **Iterative Shift Bit Extraction (pre-Cycle-26 history)**: Hex formatting extracts nibbles by calling `value.shr(4 * i)` for each of the 7 non-zero shifts. In `py/var.py` and `ts/var.ts`, each constant shift `shr(k)` then desugared into $(32 - k)$ bit-test triggers. The shifts alone emitted $4 + 8 + 12 + 16 + 20 + 24 + 28 = 112$ condition triggers per 32-bit variable (224 triggers for two variables).
2. **Dynamic Byte De-optimization**: Dynamic characters are written byte-by-byte via `_append_dynamic_byte`. Even when writing fixed-width formats like `{:x}` (always 8 bytes = 2 dwords), `_append_dynamic_byte` treats the write position as a dynamic `Var`, invoking runtime `pos.shr(2)` (30 triggers per byte) and self-modifying action patching (`emitVarStore` targeting action destination and value).
3. **Compound Cost**: A standard two-variable HUD counter like `hud.printAt(1, "wave={:x} hp={:x}", wave, hp)` emits **1,670 static triggers** with an average packing density of only 1.45 actions per trigger, and evaluates 1,843 triggers per loop. That figure includes the `printAt` display tail; the Consequences benchmark measures `hud.write`, which does not.

In `eudplib` (`src/eudplib/string/cpprint.py`), `f_cpstr_addptr` formats a 32-bit hex number in ~42 triggers by initializing two dwords to ASCII `"0000"` (`0x30303030`), testing the 32 bits directly (`DeathsX`) into the ASCII dwords without bitshifts, and adjusting nibbles $\ge 10$ with $+7$ (`'A' - ':'`). That adjustment is eudplib's own behavior and renders uppercase hex. `eudkit` uses the same bit-accumulation shape but a different output contract: it adds `0x27` (`'a' - ':' = 0x61 - 0x3A`) for nibbles $\ge 10$ and renders lowercase hex for `{:x}`. The `+7`/uppercase rule is eudplib-specific and is not `eudkit`'s contract.

## Decision

1. **Direct 32-Bit Accumulation Into the Target String**:
   - Replace iterative `value.shr(4 * i)` shifts with direct bit tests that accumulate each ASCII digit in place in the target string's compile-time dword lanes. There are no scratch dwords for ASCII digit accumulation; the destination bytes are the accumulator. Eight nibble variables remain as scratch state for the >=10 lowercase adjustment pass.
   - Before the bit tests, initialize every covered target dword to ASCII `"0000"` (`0x30303030`) with a mask-limited `SetStringDword`. A lane-aligned 8-byte placeholder covers two dwords; an unaligned start covers three, with the first and last writes masked to the bytes the placeholder owns (Decision 2).
   - For each bit $b \in [0, 31]$:
     - Test bit $b$ using a single condition: `DeathsX(value.value, AtLeast, 1, 0, 1 << b)`.
     - In the same trigger, add the bit's ASCII weight directly to the target dword at the placeholder's compile-time dword offset, masked to its byte lane:
       $$\text{action.amount} = 2^{(b \bmod 4) + 8 \cdot \text{byteLane}}$$
       The trigger also updates the nibble accumulator used by the adjustment pass.
   - For each of the 8 nibbles $j \in [0, 7]$:
     - Check if `nibble[j] >= 10`.
     - If true, add `0x27` (`'a' - ':' = 0x61 - 0x3A`) to that byte's target dword lane. This is `eudkit`'s lowercase hex contract; no ADR 0003 decision specifies hex case.
   - Pre-Cycle-26 history: hex bit-extraction and ASCII conversion dropped from ~112T to **32 bit-test triggers + 8 adjustment triggers = 40T per 32-bit variable** under the then-current constant-shift lowering. The masked initializers pack into the surrounding literal/terminator triggers, so they do not add per-variable triggers. (Cycle 26's one-trigger constant-shift kernel is a later, separate change; this 40T figure remains the historical consequence of this ADR.)

2. **Compile-Time Constant-Offset and Dword Folding**:
   - When a format string contains fixed-width hex placeholders (`{:x}`, always 8 bytes = 2 dwords) and literal runs, all byte offsets and dword boundaries from the string base are deterministic at compile time.
   - For provably fixed-width format runs:
     - Compute `dword_offset = static_pos >> 2` and `byte_lane = static_pos & 3` at compile time.
     - Eliminate runtime `pos.shr(2)` (saving 30 triggers per byte).
     - Eliminate dynamic action-pointer patching (`emitVarStore`) for these compile-time fixed-width writes.
     - Write the 8 hex characters as direct 4-byte dword writes at the compile-time offsets.
     - An unaligned start whose byte lane is known at compile time uses the same folding at that lane: the first and last of the three covered dwords are written with byte-lane masks, so adjacent content is preserved and no compile-time unaligned fallback is needed. A runtime `writeFrom` position is not compile-time known and retains the dynamic lane path from ADR 0003 decision 3.

3. **Decimal Boundaries**:
   - Variable-width decimal (`{:d}` / bare `{}`) retains leading zero suppression and dynamic lane tracking. Fixed-width decimal and the `[[fill]align]width[type]` mini-language are specified by [ADR 0014](./0014-decimal-format-default-and-fill-alignment-syntax.md) and are implemented in the same frontends. This ADR keeps only the fixed-width hex folding path described above.

4. **Frontend Parity**:
   - `ts/string.ts` and `py/string.py` implement identical direct bit-accumulation and constant-offset dword folding.
   - Emitted HIR triggers, condition orders, and action masks remain byte-identical between TypeScript and Python.

5. **Shared Format-Run Layout and Capacity Accounting**:
   - One layout walk covers a `write` / `append` / `writeFrom` format run in item order. It charges literal bytes, 8 bytes per fixed-width hex placeholder, `spec.width` per fixed-width decimal placeholder, and the widest output per variable-width decimal placeholder.
   - Fixed-width placeholders fold at their compile-time byte lane. Format runs do not insert mid-string `0x1A` padding (the final #73 resolution); an unaligned start uses the mask-limited first/last dword writes from Decision 2 instead of a dynamic fallback.
   - Capacity accounting charges the whole expanded run and fails with `EUDSL-BOUNDS` before emitting a write if it would end past offset `size`. The NUL remains at offset `size`; no path writes content at offset `>= size`.

## Consequences

- The original static target was under 100T for the `write` benchmark. The merged named benchmark (`string_format_benchmark_two_variable_hex_regression_adr_0011`) measures `hud.write("wave={:x} hp={:x}", wave, hp)` at **83T static** (80 conditions, 171 actions), inside the target. The Context's 1,670T figure is for `hud.printAt(1, ...)` and includes the display tail; it is not the same program as this `write` benchmark, so the two numbers must not be presented as a like-for-like percentage reduction. For the hex work itself, each 32-bit variable is 40T (32 bit tests + 8 adjustments); the benchmark's 80T of hex work plus 3T of literal/terminator writes is 83T.
- The original executed target was ~90T. The same `hud.write` benchmark measures **83T executed** (80 conditions, 35 actions) per loop. The Context's 1,843T figure is the `printAt` program including its display tail, not this benchmark.
- Default map assembly appends ADR 0013's 35-trigger once-per-tick gate: **118T static** (83 + 35). `--no-tick-gate` / `--single-player` or the equivalent API option assembles at **83T static**.
- The cost-gate fixture measures **320T static / 426T executed** pre-assembly (`crates/eudkit-cli/tests/fixtures/cost_gate.expected.json`) and **355T static gated / 320T ungated** assembled (`crates/eudkit-cli/tests/fixtures/cost_gate.assembled.expected.json`). Ticket #76 added the two-variable hex HUD to the fixture and updated both goldens; ticket #10 re-blessed them again when the fixture's constant `memsetEPD(3000, 0xAABBCCDD, 2)` started folding to one packed `SetDeaths` trigger instead of a 7T runtime loop (−6T static / −14T executed). Ticket #11 then added the memory-primitive probes (`.eq` 1T, `.ne` 38T, `.read()` into a variable 36T, `MemCond` 1T, variable-count `memsetEPD` 8T / `memcpyEPD` 44T) and re-blessed both goldens to the totals quoted here. Ticket #16 replaced the `.ne` marker's 38T fallback with the 4T deferred-flag form at the five one-shot gate sites (`when` / `once` / `until` / `onRising` / `onFalling`) and re-blessed both goldens again (−33T static / −35T executed); `while_loop` now expands the marker to the same 4T gate, emitting the prelude at entry **and** as the last body statements, with **one flag per site shared by both**; raw `Trigger` uses the 5T gate (#66) when a builder is active. **Ticket #97** made full-dword `read()` call one shared 32-bit-test reader per module, so the fixture's inline readers collapse to a single body and each site pays a 5T call sequence: static **355T -> 307T** (−48T, −115,584 B), conditions 282 -> 218, and static actions 903 -> 957 as each site's field-patch trigger replaces its inline per-bit conditions. Executed cost is re-blessed with the same trade: **442T -> 490T** executed triggers and 2,412 -> 3,268 actions, because every executed read now patches the body's 32 `player` fields (one trigger, 32 acts) before entering the shared body; executed conditions stay at 332. Ticket #96 unified transfer and `VarStore` batching in one block-wide scheduler and re-blessed both goldens again (−2T static / −8T executed; the fixture's HIR is byte-identical, the shift is entirely in lowering's act packing around the store sites), taking the combined gate to **305T static / 482T executed**. **The #97 rework** then stops paying the shared-call `TriggerPage` traffic for bodies that share nothing: opt splices a func body with a single call site back in, substituting the call's arguments for the callee's parameters (and writing a result parameter back in place when the call's `dst` is the same variable), while `fuse_call_results` lands every call result directly in its single consumer. The fixture's three readers have call counts 1, 2 and 1, so the two single-site bodies inline and the two-site body still shares. That re-blessed both goldens to **320T static / 426T executed** (253 static conditions, 856 static actions, 770,560 trigger bytes / 770,724 total; executed 335 conditions / 1,683 actions) and 355T gated / 320T ungated assembled: executed cost is now *below* the all-inlined 442T / 2,412A baseline while static stays far below the all-inlined 305T-per-reader shape, because only genuinely shared bodies pay one `TriggerPage` argument/result move per extra site. `py/tests/test_memory_costs.py` and `ts/tests/test_memory_costs.ts` pin the same primitives one probe per module so a single-primitive drift is attributable rather than hidden in the gate total.
- Aligned boundary padding rules from ADR 0003 continue to govern scope exits. Format runs do not add mid-string padding; their layout is still charged as content (Decision 5), and ADR 0003's 2026-11-06 amendment records the split.
- Format-run content is included in the compile-time capacity bound: `FixedString(10)` with `write("a{:x}b", v)` and `FixedString(25)` with `write("wave={:x} hp={:x}", w, h)` are exact fits with the NUL at offset `size`; `FixedString(9)` and `FixedString(24)` fail with `EUDSL-BOUNDS`. A fixed-width decimal placeholder is charged by its declared width (ADR 0014), so `FixedString(4)` with `write("{:4d}", v)` fits and `FixedString(3)` fails.

## Relationship to other decisions

- **ADR 0003 (capacity-aware aligned writes).** Decisions 2 and 3 still govern scope-boundary alignment and runtime-unknown lanes. Its 2026-11-06 amendment records Decision 5's format-run layout as capacity-checked content without mid-string padding.
- **ADR 0010 and ADR 0013 (runner boundary and tick gate).** ADR 0010's classic-first ordering is unchanged. ADR 0013 keeps the 35T once-per-tick gate as the default and allows single-chain builds to omit it; `eudkit.toml` is not supported (ADR 0013 decision 6).
- **ADR 0014 (decimal formatting).** ADR 0014 changes the bare `{}` default to decimal and adds the format mini-language and fixed-width decimal path (#74, #80, #81, #82); it is part of the integrated line. ADR 0012 remains the explicit-entry contract, not a decimal ADR.

## Amendment (2026-11-10)

Ticket #16 historically left two sites on the 38T fallback for the deferred
`field.ne(middle)` marker: raw `Trigger` and `while_loop`; both are addressed
below. The reason recorded for `while_loop` was
that its pre-triggers "are not re-emitted per pass", but that premise no longer
holds: the prelude (`reset` · `when(lt)` · `when(gt)`) is now emitted twice,
once before the while node (entry) and once as the **last body statements**
(tail). The tail's `SetNextPtr` loops back to a gate whose flag was just rebuilt
from live memory, so one flag per site, shared by both emission points, is
enough. Non-gate conditions (variable-RHS `DeferredCond`, direct
`MemFieldCond`, folded extremes) still re-materialize at the tail, so their
recompute semantics are unchanged.

On the pinned fixture `while_loop([CUnit(1000).hp.ne(500), guard.ge(1)])` the
site moves from **74T / 66C / 166A / 178,204 B static, 78T executed** to
**10T / 6C / 12A / 24,092 B static, 11T executed** — −64 static triggers and
−67 executed triggers per site, identical on both frontends. The 36-trigger
fallback re-emission at two points collapses to one reset trigger plus two
`when`s per emission point.

Raw `Trigger` was left on the 38T fallback in #16 as a compatibility default,
with "its cond list is the user's whole explicit contract" as the stated
reason. That reason does not hold: `materialize()` already injects 35–36
triggers and 3 variables into the same list, so the fallback is the larger
contract change. #66 adopts the same 4T gate used by the temporal forms: the
pinned raw-`Trigger` site moves from **38T / 33C / 114A / 91,512 B** to
**5T / 3C / 5A / 12,048 B** (field-ref form 91,516 → 12,052 B) with identical
iteration counts at seeds 499/500/501 and 1/2/3/5 passes. A detached
`Trigger` (no active module builder) keeps `materialize()`, so the gate is
only emitted where `when()` would work. The cost-gate fixture has no raw
`Trigger` `ne` site, so no golden is re-blessed here.

## Amendment (2026-11-15)

Literal segments in a format run — both standalone `write`/`append` content and
the literal bytes between placeholders — are **UTF-8 bytes** (ADR 0003), and
`save_tbl` / `TemplateStrings::parse` carry them byte-for-byte. A multi-byte
literal advances the byte offset by its encoded length; it does not change the
constant-offset or dword-folding arithmetic.

The packed-hex and fixed-width decimal paths still emit **ASCII bytes only**
(`0`–`9`, lowercase `a`–`f`, and the format-spec fill), so the trigger and cost
figures in this ADR are unchanged. The format-spec fill restriction is
ASCII-only because a fill is repeated as one byte (ADR 0014); literal content is
not subject to it.
