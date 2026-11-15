# eudkit

> **한국어 안내:** [README.ko.md](README.ko.md) (English document is the SSOT).

eudkit is a compiler toolchain and unified TypeScript/Python DSL for authoring
StarCraft: Remastered EUD maps around the native `Trigger (16C, 64A)`
execution and storage model. You write build-time macros in TypeScript or
Python; eudkit lowers them to trigger records, overlays them onto a basemap, and
writes a playable `.scx`.

> **Upstream attribution:** eudkit builds upon and improves ideas from
> [armoha/eudplib](https://github.com/armoha/eudplib) and
> [armoha/euddraft](https://github.com/armoha/euddraft).

## The 4-layer pipeline

```text
  hello.ts / hello.py
          |
          |  frontend capture (Node or Python)
          v
  +-----------------+
  | 1. HIR          |  eudkit-hir    typed trigger tree, HIR 3.2 JSON
  +-----------------+
  | 2. Optimizer    |  eudkit-opt    temporal desugar, batching, coalescing
  +-----------------+
  | 3. Lowering     |  eudkit-lower  storage plan, HIR -> LIR, relocations
  +-----------------+
  | 4. LIR + map    |  eudkit-lir    wire records
  |                 |  eudkit-map    CHK fixups, MPQ assembly
  +-----------------+
          |
          v
      hello.scx
```

The four compiler layers are the ones named in
[ADR 0001](docs/adr/0001-trigger-16c-64a-architecture.md): HIR, optimizer,
lowering, and LIR plus map assembly. The Python and TypeScript frontends sit
above them and hand HIR JSON to the CLI.

Compilation is an overlay: `eudkit compile` requires
`--basemap <basemap.scx>` and writes the generated data over a copy of that map.
The repository ships a silent basemap for the quickstart and the test suite at
`tests/outputmap/basemap/basemap_silent.scx`.

## Prerequisites

| Tool | Minimum | Needed for |
| --- | --- | --- |
| Rust | 1.85.0 | Building the CLI and workspace crates (edition 2024; `rust-version = "1.85.0"` in `Cargo.toml`) |
| Node.js | 22.6 | Compiling `.ts` sources; the CLI invokes `node --experimental-strip-types`, which landed in Node 22.6.0 |
| Python | 3.11 | Compiling `.py` sources (`requires-python = ">=3.11"` in `py/pyproject.toml`) |

StarCraft: Remastered is needed only to play the compiled map, not to build or
compile.

## Install

Use the language package that matches your frontend:

```sh
pip install eudkit      # Python DSL
npm install eudkit      # TypeScript DSL
```

The `eudkit` CLI and the editor language server are not part of those packages.
Download a prebuilt standalone bundle (`eudkit-<ver>-<target>.tar.gz`, or
`.zip` on Windows) from
[GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest) and
unpack it; `bin/eudkit` resolves the bundled DSL runtime from the adjacent
`share/eudkit/` directory. Build from source below if no bundle matches your
platform.

## Build the CLI

From the repository root:

```sh
cargo build --release --workspace
./target/release/eudkit --version
```

The release binary is `target/release/eudkit`. For a debug build, run
`cargo build --workspace` and use `target/debug/eudkit`; the Python and
TypeScript test suites expect that debug binary.

The first build fetches the `stormlib` git dependency declared in `Cargo.toml`.

## Install and set up a project

Release builds also work outside this checkout. A standalone `eudkit` resolves
its DSL runtime in this order:

1. `EUDKIT_ROOT`, if set.
2. The nearest ancestor of the current directory containing both `ts/index.ts`
   and `py/__init__.py`.
3. The nearest ancestor of the source file containing those same trees
   (`compile` and `synth` only; `doctor` inspects the current directory and
   environment).
4. `<prefix>/share/eudkit/` adjacent to the executable. This is the external
   standalone distribution layout contract; `cargo install` and the `Makefile`
   do not populate `share/eudkit/` automatically.
5. The installed `eudkit` npm or Python package.
6. Debug builds only: the build-time checkout, unless
   `EUDKIT_NO_MANIFEST_FALLBACK=1` is set.

Check which runtime and frontend toolchain the binary will use before
compiling:

```sh
eudkit doctor
eudkit doctor --json    # machine-readable output
```

Scaffold a starter project in a new directory:

```sh
eudkit init hello-py --py
eudkit init hello-ts --ts
```

Use a separate directory per frontend: `eudkit init` refuses to overwrite an
existing starter. `eudkit init [<dir>]` writes into `<dir>` (default: the
current directory); select the frontend with `--ts` or `--py`.

## 60-second Hello World

Run these commands from the repository root. Both snippets use the in-repo
import form, so they work against a checkout. Installed consumers import the
package as `eudkit`; see [py/README.md](py/README.md) and
[ts/README.md](ts/README.md).

### Python

Create `hello.py`:

```python
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

Compile it:

```sh
./target/release/eudkit compile hello.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o hello.scx
```

### TypeScript

Create `hello.ts`:

```ts
import { DisplayText, buildModule } from "./ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

Compile it with the same command shape:

```sh
./target/release/eudkit compile hello.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o hello.scx
```

Open `hello.scx` in StarCraft: Remastered to see the generated trigger. To
inspect the HIR without writing a map, run `./target/release/eudkit synth
hello.py` (or `hello.ts`); `dump-ir` prints a readable form with cost data.
Add `--explain-folds` to append the optimizer's condition/gate fold log.

For the full first-run path, including basemap choice and in-game checks, see
[docs/quickstart.md](docs/quickstart.md) and
[examples/quickstart/README.md](examples/quickstart/README.md).

## Repository map

| Path | Contents |
| --- | --- |
| `crates/eudkit-hir/` | HIR 3.2 types, validation, and JSON |
| `crates/eudkit-opt/` | Temporal desugaring, batching, coalescing, and storage analysis |
| `crates/eudkit-lower/` | HIR-to-LIR lowering and relocations |
| `crates/eudkit-lir/` | LIR wire schema |
| `crates/eudkit-map/` | CHK fixups, MPQ assembly, and `.scx` output |
| `crates/eudkit-cli/` | The `eudkit` binary (`compile`, `dump-ir`, `synth`) |
| `crates/eudkit-lsp/` | JSON-RPC language server |
| `py/` | Python DSL (in-repo import name `py`; installed package `eudkit`) |
| `ts/` | TypeScript DSL (raw sources in-repo; `npm run build` emits `dist/`) |
| `schema/` | Canonical stock-row spec and layout JSON; packaged copies live in `py/schema/` and `ts/schema/layouts/` |
| `examples/` | Beginner quickstart (`examples/quickstart/`) and `eudplib` / `euddraft` migration examples |
| `tests/` | Stock goldens, output-map fixtures, and smoke maps |
| `scripts/` | Compile benchmark and generated-surface helpers |
| `docs/` | Quickstart, guides, and architecture decision records (ADRs) |

## Documentation index

| Document | What it covers |
| --- | --- |
| [README.ko.md](README.ko.md) | Korean overview and setup guide (English README is the SSOT) |
| [docs/quickstart.md](docs/quickstart.md) | End-to-end first-run walkthrough |
| [docs/quickstart.ko.md](docs/quickstart.ko.md) | Korean quickstart and beginner guide (English document is the SSOT) |
| [examples/quickstart/README.md](examples/quickstart/README.md) | Runnable Hello World examples |
| [examples/quickstart/README.ko.md](examples/quickstart/README.ko.md) | Korean quickstart examples guide (English document is the SSOT) |
| [examples/README.md](examples/README.md) | Migration examples and Py/TS parity |
| [examples/README.ko.md](examples/README.ko.md) | Korean migration examples and Py/TS parity guide |
| [docs/diagnostics.md](docs/diagnostics.md) | `EUDSL-*` diagnostic codes and fixes |
| [docs/lsp.md](docs/lsp.md) | Building and wiring `eudkit-lsp` |
| [docs/release.md](docs/release.md) | Release deliverables, registry secrets, and bundle packaging |
| [GLOSSARY.md](GLOSSARY.md) | Domain vocabulary and cost units |
| [docs/adr/README.md](docs/adr/README.md) | Architecture decision record index |
| [docs/stock-triggers.md](docs/stock-triggers.md) | Stock and extension trigger catalog |
| [docs/benchmarking.md](docs/benchmarking.md) | Compile-stage benchmark harness |
| [tests/outputmap/smoke/README.md](tests/outputmap/smoke/README.md) | Manual in-game smoke-map checks |
| [py/README.md](py/README.md) | Python DSL entry, catalog, and DAT tables ([한국어](py/README.ko.md)) |
| [ts/README.md](ts/README.md) | TypeScript DSL entry, catalog, and DAT tables ([한국어](ts/README.ko.md)) |
| [editors/vscode/README.md](editors/vscode/README.md) | VS Code extension for `eudkit-lsp` ([한국어](editors/vscode/README.ko.md)) |
| [editors/zed/README.md](editors/zed/README.md) | Zed extension for `eudkit-lsp` ([한국어](editors/zed/README.ko.md)) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Build/test matrix, schema sync, and runtime contract |

## Build and test

The root `Makefile` wraps the contributor workflow; run these targets from the
repository root:

| Target | What it runs |
| --- | --- |
| `make build` | `cargo build --workspace` |
| `make release` | `cargo build --release --workspace` |
| `make test-rust` | Workspace tests plus the focused `map_bridge` suite |
| `make test-ts` | TypeScript typecheck and tests (installs `ts/` dependencies first) |
| `make test-py` | Python `unittest` discovery from the repository root |
| `make test-quickstart` | The four quickstart Py/TS parity pairs |
| `make test-examples` | The migration-example Py/TS parity checks |
| `make typecheck` | `tsc --noEmit` plus the truthiness check |
| `make test` | `test-rust`, then `test-ts`, then `test-py` |
| `make clean` | `cargo clean` plus `ts/dist` cleanup |

The equivalent direct commands are:

```sh
cargo build --workspace
cargo test --workspace
cargo test -p eudkit-lower --test map_bridge
(cd ts && npm install && npm run typecheck && npm test)
PYTHONPATH=. python3 -m unittest discover -s py/tests -p 'test_*.py' -t .
```

The Python suite shells out to `target/debug/eudkit` and
`target/debug/eudkit-compile`, so build the workspace first. On a fresh
checkout, install the TypeScript dev dependencies from `ts/` before running the
type checker or tests.

Use `python3 -m unittest`, not `pytest`, and use `npm`, not `bun`. The full
contributor workflow, including schema-copy rules and the stale-target trap, is
in [CONTRIBUTING.md](CONTRIBUTING.md).

## Versions

| Component | Version | Source |
| --- | --- | --- |
| `eudkit` CLI, Rust workspace, and LSP editor extensions | 0.1.0 | `Cargo.toml` (`[workspace.package] version`), `editors/` |
| Python `eudkit` package | 0.1.0 | `py/pyproject.toml` |
| npm `eudkit` package | 0.1.0 | `ts/package.json` |

All backend crates, editor extensions, and Python/npm packages share the same
unified version (`0.1.0`). Release packaging, registry publishing, and the
standalone bundle layout are documented in [docs/release.md](docs/release.md).

## Status and limitations

- Every `compile` needs a basemap; there is no basemap-from-scratch command.
- The CLI resolves its DSL runtime through `EUDKIT_ROOT`, checkout ancestors of
  the current or source directory, an adjacent `share/eudkit/`, an installed
  `eudkit` package, or (debug builds only) the build-time checkout; run
  `eudkit doctor` when it cannot find one. See
  [CONTRIBUTING.md#runtime-resolution](CONTRIBUTING.md#runtime-resolution).
- In-game behavior is checked manually with the smoke maps, not by the test
  suites.

## Acknowledgments & License

eudkit is an independent compiler and DSL toolchain improved from the concepts
and map-making ecosystems of [armoha/eudplib](https://github.com/armoha/eudplib)
and [armoha/euddraft](https://github.com/armoha/euddraft).

MIT. See [LICENSE](LICENSE).
