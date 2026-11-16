# Beginner Quickstart Examples

> **한국어 안내:** [examples/quickstart/README.ko.md](./README.ko.md) (English document is the SSOT).

Five small, progressive, **runnable** programs. Every `py/` and `ts/` pair is
checked to synthesize with zero warnings, produce byte-identical compact HIR,
keep the simulator's self-target invariant, and compile to byte-identical
`.scx` bytes.

If you have never built an eudkit map, start with the step-by-step walkthrough in
[`docs/quickstart.md`](../../docs/quickstart.md), then come back here.

## The five examples

| # | Python | TypeScript | Concepts |
|---|---|---|---|
| 01 | [`py/01_hello_world.py`](./py/01_hello_world.py) | [`ts/01_hello_world.ts`](./ts/01_hello_world.ts) | `DisplayText`, `build_module` / `buildModule`, the required `module` entry |
| 02 | [`py/02_variables_and_strings.py`](./py/02_variables_and_strings.py) | [`ts/02_variables_and_strings.ts`](./ts/02_variables_and_strings.ts) | `Var`, runtime arithmetic, `FixedString` formatting, `display_at` / `displayAt` |
| 03 | [`py/03_control_flow_and_temporal.py`](./py/03_control_flow_and_temporal.py) | [`ts/03_control_flow_and_temporal.ts`](./ts/03_control_flow_and_temporal.ts) | `when`/`otherwise`, `once`, `every`, `after`, `dispatch`, why `if`/`while` cannot branch on runtime conditions |
| 04 | [`py/04_units_and_triggers.py`](./py/04_units_and_triggers.py) | [`ts/04_units_and_triggers.ts`](./ts/04_units_and_triggers.ts) | stock `Trigger`/`Bring`/`CreateUnit`, `CUnit` field read/set, `FixedArray` basics |
| 05 | [`py/05_combinators_and_players.py`](./py/05_combinators_and_players.py) | [`ts/05_combinators_and_players.ts`](./ts/05_combinators_and_players.ts) | `cond_all`/`cond_any`/`cond_not`, `Player`, `PVariable`, `for_each_player` / `forEachPlayer`, `display_text_all` / `displayTextAll` |

Each file starts with the exact `synth` and `compile` commands for that file.

## Prerequisites

- Rust 1.85+ (the workspace uses edition 2024)
- Python 3.11+ for the Python DSL
- Node.js 22.6+ for the TypeScript DSL (the frontend uses
  `--experimental-strip-types`, added in Node 22.6.0)
- StarCraft: Remastered only if you want to open the compiled `.scx` in-game

Build the CLI once from the repository root:

```sh
cargo build --workspace
./target/debug/eudkit --version
```

`cargo build --workspace` also builds `target/debug/eudkit-compile`, which the
Python test helper uses to simulate HIR. For a release binary, run
`cargo build --release --workspace` and use `target/release/eudkit`.

## Run one example

From the repository root:

```sh
# Python -> compact HIR JSON
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py --compact

# TypeScript -> the same compact HIR JSON (byte-for-byte)
./target/debug/eudkit synth examples/quickstart/ts/01_hello_world.ts --compact

# Compile either one to a playable map
./target/debug/eudkit compile examples/quickstart/py/01_hello_world.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-py.scx
```

Swap the `01_hello_world` slug for any of the other four.

## Run the quickstart checks

From the repository root:

```sh
PYTHONPATH=. python3 -m unittest py/tests/test_quickstart_examples.py
```

That suite checks:

1. The Python and TypeScript slug inventories match exactly.
2. Both frontends synthesize with **empty stderr** (zero warnings).
3. Their `--compact` HIR output is byte-identical.
4. `run_sim` reports `self_targeted_invariant_holds: true`.
5. Both frontends compile to byte-identical `.scx` files on
   `tests/outputmap/basemap/basemap_silent.scx`.

## In-repo imports vs installed packages

The examples import from this checkout (`from py import ...`,
`from "../../../ts/index.ts"`). Installed consumers use Python
`from eudkit import ...` after `pip install -e ./py`, and TypeScript
`from "eudkit"` after `cd ts && npm install && npm run build` and an
install/link of `ts/`. The CLI captures sources from the checkout's `py/` and
`ts/` trees; if the binary lives outside the checkout, point it at the
repository with `EUDKIT_ROOT=/path/to/eudkit`. See
[docs/quickstart.md](../../docs/quickstart.md#import-spelling-checkout-vs-installed-package)
for the full spelling table.

Do not run the examples with `python3 examples/quickstart/py/01_hello_world.py`
or bare `node`: they are **sources for `eudkit`**, not standalone programs.
The third-party PyPI package named `py` can also shadow the repository's `py/`
package when the repository root is not first on `sys.path`.

## Next steps

- [`docs/quickstart.md`](../../docs/quickstart.md) — zero-to-playable map
  walkthrough, including `synth` → `dump-ir` → `compile` and in-game testing.
- [`examples/README.md`](../README.md) — migration examples for memory access,
  CP cursors, `CUnit` groups/unit lists, DAT tables, arrays/structs, block I/O,
  dynamic EPD access, player loops & local broadcasts, math/RNG/locations/tech,
  and containers/camera/engine helpers.
- [`GLOSSARY.md`](../../GLOSSARY.md) — canonical vocabulary and ADR decisions.
- [`tests/outputmap/smoke/README.md`](../../tests/outputmap/smoke/README.md) —
  the in-game verification protocol and expected-line tables used by committed
  smoke maps.
