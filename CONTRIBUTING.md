# Contributing to eudkit

eudkit is a Rust workspace plus two DSL frontends, `py/` and `ts/`, that share
one canonical vocabulary. Start with the [README](README.md), then read
[GLOSSARY.md](GLOSSARY.md) for the domain vocabulary and the ADRs that touch
your area ([docs/adr/README.md](docs/adr/README.md)).

## Build and test

The root `Makefile` wraps the commands below. Run these targets from the
repository root:

| Target | What it runs |
| --- | --- |
| `make build` | `cargo build --workspace` |
| `make release` | `cargo build --release --workspace` |
| `make test-rust` | `cargo test --workspace`, then `cargo test -p eudkit-lower --test map_bridge` |
| `make test-ts` | `cargo build` (via `build`), `typecheck`, then `npm test` in `ts/` |
| `make test-py` | `PYTHONPATH=. $(PYTHON) -m unittest discover -s py/tests -p 'test_*.py' -t .` |
| `make test-quickstart` | `PYTHONPATH=. $(PYTHON) -m unittest py/tests/test_quickstart_examples.py` |
| `make test-examples` | `PYTHONPATH=. $(PYTHON) -m unittest py/tests/test_examples.py` |
| `make typecheck` | `npm install && npm run typecheck` in `ts/` |
| `make test` | `test-rust`, then `test-ts`, then `test-py` |
| `make clean` | `cargo clean` plus `ts/dist` cleanup |

The direct commands are:

```sh
cargo build --workspace
cargo test --workspace
cargo test -p eudkit-lower --test map_bridge
(cd ts && npm install && npm run typecheck && npm test)
PYTHONPATH=. python3 -m unittest discover -s py/tests -p 'test_*.py' -t .
```

- `cargo build --workspace` builds both the `eudkit` CLI and the
  `eudkit-compile` simulator. Both the Python and TypeScript test suites shell
  out to `target/debug/eudkit` and `target/debug/eudkit-compile`, so build
  first.
- Use `python3 -m unittest`, not `pytest`. The repository's `py/` package
  collides with pytest's own `py` dependency, and running from inside `py/`
  makes `py/struct.py` shadow the standard-library `struct` module.
- Use `npm`, not `bun`, for the TypeScript workspace.
- `cargo test -p eudkit-lower --test map_bridge` is the focused
  map-bridge suite.

For the Py/TS migration examples and their byte-identical HIR/`.scx` parity
check:

```sh
PYTHONPATH=. python3 -m unittest py/tests/test_examples.py
```

That check also needs the debug binaries from `cargo build --workspace`.

## Releases

Release packaging covers the CLI bundles, VS Code extension, Zed extension,
Python wheel/sdist, and npm tarball. See [docs/release.md](docs/release.md) for
the deliverable list, version conventions, required GitHub secrets and
variables, and the local `scripts/package_bundle.py --verify` smoke flow.

## Schema is the single source of truth

Root `schema/` is canonical. The packaged copies are committed and must stay
byte-identical; packaging tests fail on drift.

- **Layouts:** `schema/layouts/*.json` is copied byte-for-byte to
  `py/schema/layouts/` and `ts/schema/layouts/`. Copy both when you edit a
  layout.
- **Stock rows:** `schema/stock-rows.json` feeds
  `py/schema/stock-rows.json`, `py/stock_rows_generated.py`, and the generated
  tables in [docs/stock-triggers.md](docs/stock-triggers.md). After editing the
  spec, run:

  ```sh
  python3 scripts/generate_stock_surfaces.py
  ```

- **TypeScript generated sources:** from `ts/`, run `npm run generate:stock`
  after editing `schema/stock-rows.json`, and `npm run generate:dat` after
  editing a `schema/layouts/dat-*.json` file.
- **Guards:** `py/tests/test_schema_packaging.py`,
  `ts/tests/test_schema_packaging.ts`, `py/tests/test_stock_surfaces.py`,
  `ts/tests/test_stock_codegen.ts`, and `ts/tests/test_dat_codegen.ts` fail when
  a packaged copy or generated source drifts.

The TypeScript package ships `ts/schema/layouts/`; it reads the canonical
`schema/stock-rows.json` from the checkout at build and test time rather than
shipping a second spec copy.

## Runtime resolution

The compiler resolves its DSL runtime in this order:

1. `EUDKIT_ROOT`, if set to a non-empty value. A set-but-invalid (non-empty)
   value is an error and never falls back to another runtime; an empty or
   whitespace-only value counts as unset.
2. The nearest ancestor of the current directory containing both
   `ts/index.ts` and `py/__init__.py`.
3. The nearest ancestor of the source file containing those same trees
   (`compile` and `synth` only; `doctor` inspects the current directory and
   environment).
4. `<prefix>/share/eudkit/` adjacent to the executable, with the checkout
   layout inside it (`share/eudkit/ts/index.ts`, `share/eudkit/py/...`). This is
   the external standalone distribution layout contract; `cargo install` and
   the `Makefile` do not populate `share/eudkit/` automatically.
5. The installed `eudkit` npm or Python package, resolved relative to the
   source file.
6. Debug builds only: the build-time checkout, unless
   `EUDKIT_NO_MANIFEST_FALLBACK=1` disables that fallback.

For a checkout it imports `<root>/ts/index.ts` through
`node --experimental-strip-types`, or runs Python with `<root>` on `sys.path`
and imports `py`. An installed-package runtime imports `eudkit` instead. Both
the `ts/index.ts`/`py/` checkout trees and the installed package are accepted
runtime roots, so a standalone binary can compile without a checkout.

Run `eudkit doctor` to print the resolved root and frontend toolchain;
`eudkit doctor --json` emits the same report for scripts. `doctor` inspects the
current directory and environment, while `compile`/`synth` additionally check the source file's ancestors. If resolution
picks the wrong tree, set `EUDKIT_ROOT=/path/to/eudkit`.

`eudkit init [<dir>] [--ts|--py]` scaffolds a new project against the resolved
runtime. It writes into `<dir>` (default: the current directory), selects the
frontend with `--ts` or `--py`, and emits checkout imports when a checkout is
resolved or installed-package imports (`eudkit`) otherwise.

Consequences:

- Installing the `eudkit` Python or npm package is a supported runtime source;
  a checkout is only required when you want to edit or capture the in-repo
  `py/` and `ts/` trees.
- A release binary run outside the checkout no longer needs the original
  build-time path. Debug builds can still fall back to the build-time checkout;
  set `EUDKIT_NO_MANIFEST_FALLBACK=1` to exercise the release behavior.

## Stale cargo artifacts

Test binaries bake `env!("CARGO_MANIFEST_DIR")` into their paths. After moving
or removing a checkout, tests can fail with `No such file or directory` or
`Map(Storm(FileNotFound))`. Run `cargo clean` and rebuild; this is stale artifact
state, not a source regression.

## Formatting

The workspace is not rustfmt-clean today: `cargo fmt --all -- --check` already
fails on files your change did not touch. Do not run a workspace-wide
`cargo fmt --all`; it rewrites unrelated code and buries the change. Keep
formatting local to the lines you edit.

## Commit messages

Use Conventional Commits with an imperative summary:

```text
type(scope?): imperative summary
```

- Keep the subject in the imperative mood (`fix(cli): reject a missing
  basemap`), lower-case after the colon, and without a trailing period.
- The scope is optional: use `docs: ...` when a change spans the repository.
- Wrap every commit message body line at 72 columns. The body carries the
  reason and any migration note; the subject stays a single line.
- Keep commit messages public-repo clean: no internal ticket, cycle, wave,
  worktree, or handoff tags. Describe the change itself.
- Do not rewrite or force-push published `master`; add a follow-up commit
  instead.
