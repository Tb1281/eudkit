# Compile-stage benchmarking

`scripts/bench_compile.py` measures where a large map build spends its wall
clock and prints a stable per-stage table. It is deliberately **manual**: the
source and basemap are command-line arguments, so neither the large fixture
nor its ~40 MB raw CHK has to live in the repository, a normal test, or CI.

## Quick start

From the repository root:

```sh
python3 scripts/bench_compile.py \
    --source /path/to/map.ts \
    --basemap /path/to/basemap.scx
```

The default path builds `target/release/eudkit`, runs one warmup and three
measured compiles, then prints the stage table:

```text
stage                     median ms     min ms     max ms    share
------------------------------------------------------------------
frontend                      492.2      479.0      499.8    24.3%
hir.parse                     166.4      163.5      168.1     8.2%
...
mpq.add-chk                   801.7      783.2      805.5    39.6%
mpq.compact                    38.4       38.4       41.2     1.9%
------------------------------------------------------------------
sum(stages)                  1992.4                          98.4%
total (wall clock)           2025.6     2024.9     2053.5   100.0%

output: 1,011,328 bytes; identical sha256 ac5c6dbf…
static: 16878T; identical across runs
```

`share` is the stage median divided by the wall-clock median. `sum(stages)`
is below 100% because process start-up, environment setup, and the gap
between the last stage and process exit are not instrumented.

## Options

| Option | Meaning |
| --- | --- |
| `--source PATH` | Map source (`.ts` or `.py`). Required. |
| `--basemap PATH` | Template map. Required. |
| `--runs N` | Measured runs (default 3). |
| `--warmup N` | Unmeasured warmup runs (default 1). |
| `--no-build` | Reuse `target/release/eudkit` instead of building it. |
| `--eudkit PATH` | Use a specific binary; skips the build. |
| `--out PATH` | Write the map here instead of a temporary directory. |
| `--keep` | Keep the temporary output directory and print its path. |
| `--json` | Print the same data as JSON. |

The harness sets `EUDKIT_ROOT` to the repository root unless it is already
set, and always sets `EUDKIT_STAGE_TIMINGS=1` for its child processes.

Repeated runs must produce the same output bytes, sha256, and static cost.
The harness exits non-zero if they differ, so a non-deterministic tail is
reported as a benchmark failure rather than being averaged away.

## Stage timing contract

Stage timings are opt-in. With `EUDKIT_STAGE_TIMINGS` unset (or empty, `0`,
or `false`), no clocks are read and nothing is printed. With it enabled,
the CLI and map crates write one JSON line per completed stage to stderr:

```text
__EUDKIT_STAGE__ {"name":"map.savechk","ms":50.399}
```

stdout stays the normal CLI output. Timings are wall-clock, in-process, and
single-threaded, so stage values add up to the compile phase. They are
diagnostic numbers, not a stable API; the harness is the only consumer of
the stage names.

## Stage reference

Stages run in this order for a map build:

| Stage | Work |
| --- | --- |
| `frontend` | Frontend subprocess evaluates the DSL and emits HIR JSON. |
| `hir.parse` | CLI parses HIR JSON into the HIR document. |
| `opt.optimize` | HIR optimization pass. |
| `opt.storage` | Storage/cell analysis of the optimized module. |
| `lower.basemap-info` | Open the basemap, read/tokenize `scenario.chk`, and extract template strings and MapInfo — the one template string-table parse per compile (map assembly reuses it). |
| `lower.str-offsets` | Run the control-flow pre-pass once, walk its triggers for the final ordered string list, and build the STR offsets from the shared basemap parse. |
| `lower.final` | Final lowering to LIR, from the same pre-pass (no second flatten, no probe lowering). |
| `lir.serialize` | Serialize LIR to JSON for the map tail. |
| `map.template` | Take the tokenized template CHK from the lowering context (a ~0.003 ms clone; no MPQ re-read), or open/read/tokenize it when assembly has no context. |
| `map.lir` | Parse the LIR JSON and validate the program entry. |
| `map.gate` | Append the optional once-per-tick gate. |
| `map.uprp` | Allocate UPRP/UPUS slots and patch property indices. |
| `map.payload` | LIR → payload plus relocation fixups. |
| `map.cost` | Static cost of the exact document that produced the payload. |
| `map.strings` | Decode module strings and build the TBL/STR offset table, reusing the template parse from `lower.basemap-info`. |
| `map.inline` | `_preprocess_inline_code` and the authored-trigger flag decision. |
| `map.fixups` | `update_map_data` + `_fix_map_data`. |
| `map.init` | `initialize_payload`: PRT/ORT fixups, STR assembly, MRGN obfuscation, TRIG rebuild. |
| `map.savechk` | CHK optimization and `savechk` (including fake `ISOM` draws). |
| `mpq.copy` | Clone the basemap into the output at the requested sector size (raw copy when none is set). |
| `mpq.add-chk` | Add the rebuilt `scenario.chk` (and the clone-path placeholder) to the archive. |
| `mpq.update` | Add queued auxiliary files. |
| `mpq.compact` | Compact the output archive. |

The `lower.str-offsets` stage runs the HIR control-flow pre-pass once; the
final string list is walked from that pre-pass and the final lowering emits
items from the same triggers, so there is no second lowering pass. Map
assembly (`map.strings`) reuses the template table parsed in
`lower.basemap-info` rather than parsing it again.

## Reference baseline

Measured on 2026-11-08 with a release build, using a large external fixture
(`test.ts` and `test-base.scx`):

- Wall clock: **2,026 ms median** (2,025–2,054 ms across 3 runs).
- Output: **1,011,328 bytes**, sha256
  `ac5c6dbf7ca9979bc9300f8e88eefb0b3ff5ba66b7ac6c99e002c2d940c8a415`,
  **16,878T static**.
- Largest stages: `mpq.add-chk` 802 ms (39.6%), `frontend` 492 ms (24.3%),
  `hir.parse` 166 ms (8.2%), `map.init` 108 ms (5.3%), `map.lir` 75 ms
  (3.7%).

The map is 16,878 triggers, so its raw CHK is roughly 40 MB even though the
source is a few kilobytes and the compressed `.scx` is about 1 MB.
`mpq.add-chk` is dominated by compressing that CHK. Numbers vary with CPU,
filesystem, and output path; compare runs from the same machine and avoid
writing the output to a slow network or emulated drive when measuring.

The earlier 16,883T result predates the fixed-width decimal arithmetic
change and must not be reintroduced; the current deterministic result is
16,878T.

## CI rule

The large fixture is **not** part of normal CI. No test may depend on
external local fixture paths, and the fixture must not be checked in. The benchmark
harness is the only supported way to regenerate the stage table; run it
manually when changing the compile pipeline and record the new baseline in
this file if the numbers move materially.
