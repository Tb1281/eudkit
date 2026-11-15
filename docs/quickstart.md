# Quickstart: from zero to a playable `.scx`

> **한국어 안내:** [docs/quickstart.ko.md](./quickstart.ko.md) (English document is the SSOT).

This guide takes you from a fresh checkout to a map you can open in
StarCraft: Remastered. It uses the four progressive examples in
[`examples/quickstart/`](../examples/quickstart/README.md).

If a term is unfamiliar, the canonical definitions live in
[`GLOSSARY.md`](../GLOSSARY.md). The short version: eudkit compiles a Python or
TypeScript program into a StarCraft map payload. Your source describes triggers,
game memory, and HUD text; `eudkit compile` combines that payload with a base map
(`--basemap`) to produce a `.scx` file.

## 0. What you need

| Tool | Why | Notes |
|---|---|---|
| Rust 1.85+ | builds the `eudkit` CLI | the workspace uses edition 2024 |
| Python 3.11+ | Python DSL, tests | the in-repo package is named `py` |
| Node.js | TypeScript DSL | the frontend invokes Node's type stripping |
| StarCraft: Remastered | in-game verification | optional for synth/compile-only work |

A **basemap** is a normal `.scx` map whose terrain, players, and settings you
reuse. This repository ships one for tests and examples:

```text
tests/outputmap/basemap/basemap_silent.scx
```

`basemap_silent.scx` is `basemap.scx` with its authored classic triggers
disabled, so the only HUD lines you see come from your compiled program. Use
`basemap.scx` instead when you deliberately want the authored `Hello World!`
trigger to run first.

## 1. Build the CLI

From the repository root:

```sh
cargo build --workspace
./target/debug/eudkit --version
```

The workspace also builds `target/debug/eudkit-compile`, the native simulator
wrapper used by `py/tests/helpers.py`. For a faster release binary:

```sh
cargo build --release --workspace
./target/release/eudkit --version
```

The commands below use `./target/debug/eudkit`; substitute
`./target/release/eudkit` if you built release.

> The CLI must be able to find the DSL runtime. It resolves one in this order:
> `EUDKIT_ROOT`; ancestors of the current directory; ancestors of the source
> file (`compile`/`synth` only); an adjacent `<prefix>/share/eudkit/`; the
> installed `eudkit` npm/Python package; then, in debug builds only, the
> build-time checkout unless `EUDKIT_NO_MANIFEST_FALLBACK=1` is set. A
> set-but-invalid `EUDKIT_ROOT` is an error and never falls back to another
> runtime.
>
> `<prefix>/share/eudkit/` is the external standalone distribution layout
> contract. `cargo install` and the `Makefile` do not populate it automatically.

### Check the runtime with `doctor`

`eudkit doctor` reports the DSL runtime root and frontend toolchain the binary
will use. It inspects the current directory and environment; `compile` and
`synth` additionally check ancestors of the source file. Use `--json` for
scripts, bug reports, or CI logs:

```sh
./target/debug/eudkit doctor
./target/debug/eudkit doctor --json
```

If the reported root is wrong, set `EUDKIT_ROOT=/path/to/eudkit`.

### Scaffold a project with `init`

`eudkit init [<dir>] [--ts|--py]` creates a starter project in `<dir>` (default:
the current directory). Pick the frontend explicitly while learning, using a
separate directory per frontend because `init` refuses to overwrite an existing
starter:

```sh
./target/debug/eudkit init hello-py --py
./target/debug/eudkit init hello-ts --ts
```

Edit the generated module, then run `synth` or `compile` as below.

## 2. Start from the smallest program

The Python source is
[`examples/quickstart/py/01_hello_world.py`](../examples/quickstart/py/01_hello_world.py):

```python
from py import DisplayText, build_module, once


def prog() -> None:
    once([], lambda: DisplayText("Hello from eudkit!"))


module = build_module(prog)
```

The TypeScript source is
[`examples/quickstart/ts/01_hello_world.ts`](../examples/quickstart/ts/01_hello_world.ts):

```ts
import { DisplayText, buildModule, once } from "../../../ts/index.ts";

export const module = buildModule(() => {
  once([], () => {
    DisplayText("Hello from eudkit!");
  });
});
```

`once([], ...)` displays the line on the first trigger loop only. Without it,
`DisplayText` appends the same chat line on every loop.

Every source passed to `eudkit` must expose exactly one compiled module named
`module`:

- Python: `module = build_module(prog)` at module scope.
- TypeScript: `export const module = buildModule(...)`.

`build_module` / `buildModule` runs the body **once at compile time** and records
the HIR (the intermediate representation). It is not a runtime capture hook.
See [ADR 0012](adr/0012-explicit-entry-module-export.md).

### Import spelling: checkout vs installed package

The examples are written for this checkout because the CLI captures the
repository's `py/` and `ts/` trees directly.

| Mode | Python | TypeScript |
|---|---|---|
| In this repository | `from py import ...` | `from "../../../ts/index.ts"` |
| Installed library | `from eudkit import ...` | `from "eudkit"` |

To use the installed Python spelling:

```sh
pip install -e ./py
```

To use the installed TypeScript spelling, build the package first, then install
or link `ts/`:

```sh
cd ts
npm install
npm run build
npm link          # or: npm install /path/to/eudkit/ts
cd ..
```

Then a consumer project can write `from "eudkit"`. The import name changes; the
module body does not. The CLI resolves a checkout or the installed package as
described above; when both are available it prefers a checkout, so keep
`EUDKIT_ROOT` pointing at this repository when you want to capture its DSL
sources.

## 3. `synth`: source → HIR

`synth` runs the frontend and prints HIR JSON. Use `--compact` for a
single-line, diff-friendly form:

```sh
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py --compact
```

Expected compact output:

```json
{"version":"3.2","module":{"stmts":[{"kind":"once","conds":[],"body":[{"kind":"act","mask":0,"strid":0,"wavid":0,"time":0,"dest":0,"value":0,"unit":0,"acttype":9,"modifier":0,"flags":4,"padding":0,"eudx":0,"text":"Hello from eudkit!"}]}]}}
```

Write it to a file when you want to inspect or reuse it:

```sh
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py \
  -o /tmp/quickstart-hello.hir.json
```

Both frontends share one HIR pipeline, so the TypeScript example produces the
same bytes:

```sh
./target/debug/eudkit synth examples/quickstart/ts/01_hello_world.ts --compact
```

## 4. `dump-ir`: inspect cost and storage

`dump-ir` reports static and simulated cost, storage planning, and trigger
packing. It accepts a source file or an `.hir.json` file:

```sh
# Directly from source; simulate two trigger loops
./target/debug/eudkit dump-ir examples/quickstart/py/01_hello_world.py \
  --json --loops 2

# Or from the HIR you saved above, with no execution
./target/debug/eudkit dump-ir /tmp/quickstart-hello.hir.json \
  --json --no-exec
```

Useful fields:

| Field | Meaning |
|---|---|
| `static_cost` | triggers, conditions, actions, and bytes in the payload |
| `executed_cost` | what two simulated trigger loops actually visited |
| `storage` | `Var` cells, `TriggerPage` slots/pages, dword/bit cells |
| `packing` | how conditions/actions were packed into triggers |

Add `--explain-folds` to append the optimizer's fold log: condition fusion,
constant-gate opening, dead-gate removal, sibling `when` merges, and
16-condition chunking.

```sh
./target/debug/eudkit dump-ir /tmp/quickstart-hello.hir.json --explain-folds
```

The text report ends with a `Folds:` block. With `--json` the same events
appear under the `folds` key (`kind`, `site`, `before`, `after`). The key is
omitted unless the flag is present, so default JSON is unchanged.

The simulator executes a model of the generated triggers; it is not StarCraft.
It is the fastest way to catch a branch that never fires or a `Var` that never
changes before opening the game.

## 5. `compile`: HIR → playable `.scx`

Compile the Python example onto the silent basemap:

```sh
./target/debug/eudkit compile examples/quickstart/py/01_hello_world.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-py.scx
```

Compile the TypeScript twin:

```sh
./target/debug/eudkit compile examples/quickstart/ts/01_hello_world.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-ts.scx
```

The two files are byte-identical. The final line of output reports the static
trigger cost, for example:

```text
compiled examples/quickstart/py/01_hello_world.py -> /tmp/01_hello_world-py.scx (36T static)
```

`T` is triggers. To inspect static cost without writing a map, use
`eudkit dump-ir <source> --json --no-exec` (see §4). `--check-cost` is a
deprecated no-op accepted for compatibility; `--plan-out <plan.json>` writes a
reusable build plan whose basemap fingerprint is verified later.

Without `-o`, the CLI chooses an output name next to the source. Prefer an
explicit `-o` while learning so you never overwrite a source-adjacent artifact
by accident.

## 6. Open it in StarCraft

1. Copy the compiled `.scx` into a map folder StarCraft: Remastered reads. The
   exact directory is platform/installation dependent; any folder visible under
   the game's **Maps** browser works.
2. Start StarCraft: Remastered, choose **Single Player → Custom Game**, and pick
   the map.
3. Start the game. The hello-world map shows:

   ```text
   Hello from eudkit!
   ```

   as a chat/HUD line from the local player (P1 in a solo game).

The repository's established in-game verification protocol is
[`tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md). Use it
when you add more examples:

- It lists the exact HUD lines each committed smoke map should display.
- It explains line-order effects such as `DisplayText` versus `displayAt`.
- It shows how committed `.scx` artifacts are regenerated with the same
  `--basemap tests/outputmap/basemap/basemap_silent.scx` command.

A useful first debugging rule: if `synth` succeeds but the map shows nothing,
check that the map is launched in **single-player** and that you are looking at
the line `displayAt` targets. `DisplayText` appends; `displayAt(n)` writes chat
line `n` (0–10). Both target `CurrentPlayer` (`0x6509B0`), which the payload
runner leaves at 0 (P1) for a normal solo start.

## 7. Learn the language in four steps

Run each example with `synth` and `compile` as above, then read the comments:

| Step | Example | What it teaches |
|---|---|---|
| 01 | [Hello World](../examples/quickstart/py/01_hello_world.py) | the required `module` entry and `DisplayText` |
| 02 | [Variables and Strings](../examples/quickstart/py/02_variables_and_strings.py) | game-time `Var` arithmetic and `FixedString`/`display_at` |
| 03 | [Control Flow and Time](../examples/quickstart/py/03_control_flow_and_temporal.py) | `when`/`otherwise`, `once`, `every`, `after`, `dispatch` |
| 04 | [Units and Triggers](../examples/quickstart/py/04_units_and_triggers.py) | stock triggers, `CUnit` fields, `FixedArray` |

### The one rule that surprises Python/TypeScript developers

A runtime condition is **not** a language boolean:

```python
phase = Var(0)
# phase.eq(0) describes game memory; it is a HIR condition, not True/False.
when([phase.eq(0)], do_something)   # correct: emits a runtime branch
# if phase.eq(0): ...               # WRONG: EUDSL-COND-TRUTHIFY
```

`if`/`while` run at build time, before a game exists. Use `when`/`once`/
`while_loop`, `every`, `after`, and `dispatch` for runtime decisions. See
[ADR 0020](adr/0020-build-time-vs-runtime-conditions.md) and the runtime
truthiness table in [`py/README.md`](../py/README.md) and
[`ts/README.md`](../ts/README.md).

## 8. Run the automated checks

From the repository root:

```sh
# The four quickstart pairs: slug match, zero warnings, HIR parity,
# simulator invariant, and .scx parity.
PYTHONPATH=. python3 -m unittest py/tests/test_quickstart_examples.py

# The older migration examples must remain green too.
PYTHONPATH=. python3 -m unittest py/tests/test_examples.py
```

The quickstart suite requires both `target/debug/eudkit` and
`target/debug/eudkit-compile`; `cargo build --workspace` produces both.

The root `Makefile` wraps both suites for convenience:

```sh
make test-quickstart
make test-examples
```

Use `python3 -m unittest`, not `pytest`, in this repository. The local package
named `py` collides with pytest's own `py` dependency from the repository root.

## 9. Where to go next

Once the four quickstart examples make sense:

1. Read [`examples/README.md`](../examples/README.md) and work through the
   migration examples in order. They cover direct memory access, sub-dword
   masks, CP cursors, `CUnit` groups, DAT tables, arrays/structs, block I/O, and
   dynamic EPD access.
2. Keep [`GLOSSARY.md`](../GLOSSARY.md) open for canonical terms.
3. Read the decision records in [`docs/adr/README.md`](adr/README.md) when a
   design choice looks surprising.
4. Use [`py/README.md`](../py/README.md) and [`ts/README.md`](../ts/README.md)
   for the full API surface and stock trigger catalog.
5. Use the smoke maps and their expected-line tables in
   [`tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md) as a
   model for your own in-game tests.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'eudkit'` | In-checkout sources use `from py import ...`. Use `eudkit` only after `pip install -e ./py`. |
| `ERR_MODULE_NOT_FOUND: Cannot find package 'eudkit'` | In-checkout sources import `"../../../ts/index.ts"`. Installed consumers need `cd ts && npm run build` and an install/link. |
| `ImportError: cannot import name ... from 'py'` when running a file directly | Run sources through `./target/debug/eudkit`, not `python3 file.py`; the PyPI package `py` may shadow the repository package. |
| `EUDSL-COND-TRUTHIFY` | You used a runtime condition in `if`/`while`/`assert`. Use `when`, `once`, `while_loop`, `every`, `after`, or `dispatch`. |
| `missing compiled binary: .../target/debug/eudkit` in tests | Run `cargo build --workspace` first. |
| Frontend warnings on stderr during `synth` | The source uses a discouraged idiom. Fix the source instead of adding `--no-warnings`; the quickstart tests require empty stderr. |
| `compile: cannot infer frontend` / cannot find the DSL | Run from the checkout or set `EUDKIT_ROOT` to the checkout root. |
| Map compiles but shows the authored `Hello World!` line | You used `basemap.scx`; use `basemap_silent.scx` or account for the authored trigger. |
