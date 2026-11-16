# Release pipelines and deliverables

This document is the English single source of truth for how eudkit is
packaged and released. The workflows live in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

## The five release deliverables

Each tagged release publishes five deliverables that share one unified version
(`0.1.0`). The CI `packaging` job builds and smoke-tests all five on every pull
request and push; the release workflow repeats them with optional registry
publishing.

| # | Deliverable | Built from | Artifact |
| --- | --- | --- | --- |
| 1 | Standalone CLI bundles | `crates/eudkit-cli`, `crates/eudkit-lsp`, `ts/`, `py/` | `eudkit-<ver>-<target>.tar.gz` (`.zip` on Windows) plus `.sha256` |
| 2 | VS Code extension | `editors/vscode/` | `eudkit-vscode.vsix` |
| 3 | Zed extension | `editors/zed/` | `eudkit-zed-<ver>.tar.gz` plus `.sha256` |
| 4 | Python package | `py/` | `eudkit-<ver>.whl` and `eudkit-<ver>.tar.gz` |
| 5 | npm package | `ts/` | `eudkit-<ver>.tgz` |

### 1. Standalone CLI bundles

`build-binaries` runs on Linux x86_64, Linux aarch64, macOS aarch64, and
Windows x86_64. It builds `eudkit` and `eudkit-lsp`, builds the TypeScript
`dist/`, then calls [`scripts/package_bundle.py`](../scripts/package_bundle.py)
with `--verify`. Each archive contains:

```text
eudkit-<ver>-<target>/
  bin/eudkit(.exe)
  bin/eudkit-lsp(.exe)
  share/eudkit/ts/      dist/, *.ts, package.json, schema/layouts/*.json
  share/eudkit/py/      *.py, README.md, schema/, pyproject.toml
  LICENSE
  README.md
```

`share/eudkit/` is the adjacent runtime layout the CLI already searches next to
the executable (see
[CONTRIBUTING.md#runtime-resolution](../CONTRIBUTING.md#runtime-resolution)).
Node is required to compile TypeScript sources and Python 3.11+ to compile
Python sources; the bundle itself ships both DSL runtimes. Windows targets use
`.zip`; every other target uses `.tar.gz`. Both formats carry a sibling
`.sha256` checksum.

`--verify` checks the required binaries, both schema trees
(`share/eudkit/ts/schema/layouts/*.json`,
`share/eudkit/py/schema/stock-rows.json`, and
`share/eudkit/py/schema/layouts/*.json`), rejects `node_modules/`, `tests/`,
`__pycache__/`, `build/`, and `*.egg-info`, and confirms the checksum.
`--version` and `--target` reject path separators and `..`, so a malicious
matrix value cannot write outside `--out-dir`. `.tar.gz` members normalize
`uid`, `gid`, and `mtime` to `0` so the same inputs produce the same metadata.

The workflow extracts each bundle and runs `eudkit doctor`, `eudkit init`,
`eudkit compile`, and an `eudkit-lsp` JSON-RPC handshake (`initialize` ->
`shutdown` -> `exit`, asserting `serverInfo`) before uploading it.

### 2. VS Code extension

`build-vscode` compiles `crates/eudkit-lsp` for `wasm32-wasip1`, copies
`eudkit-lsp.wasm` into `editors/vscode/`, runs the extension tests (including
the bundled Wasm stdio handshake), and packages `editors/vscode/` with
`@vscode/vsce@4.1.1`. It always uploads `eudkit-vscode.vsix` as a release
artifact. On a publishing run it publishes to the VS Code Marketplace
(`VSCE_PAT`) and Open VSX (`OVSX_PAT`) when the corresponding secret is
configured; a missing secret logs a notice and skips that registry instead of
failing the release.

### 3. Zed extension

`build-zed` compiles `editors/zed/` with `--locked` for `wasm32-wasip2` and
builds `eudkit-zed-<ver>.tar.gz` with a top-level `eudkit-zed-<ver>/` folder:

```text
eudkit-zed-<ver>/
  extension.toml
  extension.wasm
  Cargo.toml
  Cargo.lock
  src/
  LICENSE
  README.md
```

`LICENSE` and `README.md` come from `editors/zed/`, not the repository root, so
the archive is a self-contained extension payload. CI builds and packages the
same archive. It is the reproducible release record; Zed itself installs the
extension from the registry source tree (or via **Install Dev Extension** on a
host with `rustc` and the `wasm32-wasip2` target), and at runtime the extension
automatically downloads the matching `eudkit-<ver>-<target>` release bundle from
GitHub Releases when `eudkit-lsp` is not configured or on `PATH`.

### 4. Python package

`build-python` runs `python -m build --no-isolation py`, checks the metadata
with `twine check`, and installs the wheel into a clean virtualenv to verify
`import eudkit`. CI also builds both the sdist and wheel and asserts that both
exist. The release job publishes them to PyPI with `--skip-existing` (or the
trusted-publishing action's `skip-existing: true`), so re-running a partially
failed release is safe.

### 5. npm package

`build-node` runs `npm ci`, `npm run build`, `npm run typecheck`, and
`npm pack`, then runs [`scripts/smoke_npm.mjs`](../scripts/smoke_npm.mjs) to
install the tarball in a clean temporary project and verify the `eudkit`
import. It publishes with `--provenance --access public`.

## Triggers and scope

CI runs on pushes to `master`/`main` and on every pull request. Its
`packaging` job builds and smoke-tests all five deliverables but never
publishes anything.

The release workflow runs on:

- **Tag pushes** matching `v*`. These build everything, publish to registries
  when credentials are configured, and create the GitHub release.
- **Manual `workflow_dispatch`**. With `publish_registries=true`, it publishes
  to the enabled registries. Without it, it builds and uploads workflow
  artifacts only. A manual run never creates a GitHub release; the
  `publish-release` job only runs for tag pushes.

`concurrency: { group: release-${{ github.ref }}, cancel-in-progress: false }`
serializes runs for the same ref instead of cancelling a release midway.

The `verify` job runs `scripts/check_versions.py` (and, on a tag,
`--tag "$GITHUB_REF_NAME"`) plus the bundle unit tests. Every build and publish
job `needs: verify`, so a version mismatch blocks the release before any
artifact is built.

## Version conventions

All eight manifests share one unified version (`0.1.0`):

| Manifest | Value |
| --- | --- |
| [`Cargo.toml`](../Cargo.toml) `[workspace.package] version` | `0.1.0` (source of truth) |
| [`crates/eudkit-cli/Cargo.toml`](../crates/eudkit-cli/Cargo.toml) | `version.workspace = true` |
| [`crates/eudkit-lsp/Cargo.toml`](../crates/eudkit-lsp/Cargo.toml) | `version.workspace = true` |
| [`editors/vscode/package.json`](../editors/vscode/package.json) | `0.1.0` |
| [`editors/zed/Cargo.toml`](../editors/zed/Cargo.toml) | `0.1.0` |
| [`editors/zed/extension.toml`](../editors/zed/extension.toml) | `0.1.0` |
| [`py/pyproject.toml`](../py/pyproject.toml) | `0.1.0` |
| [`ts/package.json`](../ts/package.json) | `0.1.0` |

Tags use the form `v<MAJOR.MINOR.PATCH>`, for example `v0.1.0`. A prerelease
tag such as `v0.2.0-rc.1` is accepted only when every manifest
also says `0.2.0-rc.1`. Validate the whole checklist before tagging:

```sh
python3 scripts/check_versions.py --tag v0.1.0
```

All release bundles, editor extensions, and Python/npm artifacts carry this
unified version.

## Required GitHub configuration

Secrets are never exposed as job-level environment variables. Each publish
step receives only the secret it needs, and skips with a notice when the secret
is absent. `publish-release` always needs the default `GITHUB_TOKEN` with
`contents: write` (declared per job); the workflow never grants write access at
the top level.

| Name | Kind | Used by | Purpose |
| --- | --- | --- | --- |
| `NPM_TOKEN` | secret | `build-node` publish step | npm automation token (`npm publish --provenance`) |
| `PYPI_API_TOKEN` | secret | `build-python` token publish step | PyPI API token alternative to Trusted Publishing |
| `PUBLISH_PYPI` | repository variable | `build-python` | Set to `true` to enable PyPI Trusted Publishing instead of a token |
| `VSCE_PAT` | secret | `build-vscode` Marketplace step | VS Code Marketplace personal access token |
| `OVSX_PAT` | secret | `build-vscode` Open VSX step | Open VSX access token |

PyPI Trusted Publishing is preferred: configure the PyPI project to trust this
repository and the `release.yml` workflow, then leave `PYPI_API_TOKEN` unset
and set `PUBLISH_PYPI=true`. The `build-python` job requests `id-token: write`
for the OIDC exchange. With `PUBLISH_PYPI` unset and an API token present, the
token path is used instead. npm provenance likewise uses the job's
`id-token: write` permission.

## Zed registry submission

Zed's extension registry is a pull request against
[`zed-industries/extensions`](https://github.com/zed-industries/extensions),
not a generated upload endpoint. The release workflow's
`eudkit-zed-<ver>.tar.gz` is a release asset, but the registry consumes the
source tree directly.

For each release:

1. Fork and clone `zed-industries/extensions` (`pnpm install` first if needed).
2. Add this repository as a submodule at `extensions/eudkit`:

   ```sh
   git submodule add https://github.com/Tb1281/eudkit.git extensions/eudkit
   ```

3. Add an `[eudkit]` entry to `extensions.toml`. The `version` must match
   `editors/zed/extension.toml`, and `path` points at the extension package
   inside the submodule:

   ```toml
   [eudkit]
   submodule = "extensions/eudkit"
   path = "editors/zed"
   version = "0.1.0"
   ```

4. Sort the index and run the repository's checks:

   ```sh
   pnpm sort-extensions
   ```

5. Commit the submodule, `extensions.toml`, and `.gitmodules`, then open the
   pull request against `zed-industries/extensions`.

The `eudkit-zed-<ver>.tar.gz` archive remains the reproducible release record
and a local installation input; it is not itself submitted to the registry.

## Local bundle testing

Build the inputs first, then assemble and verify a bundle:

```sh
cargo build --release -p eudkit -p eudkit-lsp
(cd ts && npm run build)

python3 scripts/package_bundle.py \
  --version 0.1.0 \
  --target x86_64-unknown-linux-gnu \
  --bin-dir target/release \
  --repo-root . \
  --out-dir dist \
  --verify
```

`--verify` checks the archive layout (`bin/eudkit*`,
`share/eudkit/ts/index.ts`, both schema trees, `share/eudkit/py/__init__.py`),
rejects `node_modules/`, `tests/`, `__pycache__/`, `build/`, and `*.egg-info`,
and confirms the `.sha256` checksum. Unpack and exercise both binaries:

```sh
mkdir -p /tmp/eudkit-bundle
tar -xzf dist/eudkit-0.1.0-x86_64-unknown-linux-gnu.tar.gz -C /tmp/eudkit-bundle
BUNDLE=$(find /tmp/eudkit-bundle -mindepth 1 -maxdepth 1 -type d | head -n 1)
cd "$BUNDLE"
./bin/eudkit doctor
./bin/eudkit init /tmp/eudkit-project --ts
./bin/eudkit compile /tmp/eudkit-project/main.ts \
  --basemap /tmp/eudkit-project/basemap.scx \
  --out /tmp/eudkit-project/main.scx
```

Send `initialize`, `shutdown`, and `exit` to `./bin/eudkit-lsp` over stdio (the
workflows run this check) and expect a JSON response containing `serverInfo`
and exit status `0`. The CLI finds the bundled DSL runtime through the adjacent
`share/eudkit/` layout, so no checkout or installed package is needed.

## Related documentation

- [CONTRIBUTING.md](../CONTRIBUTING.md) for the contributor build and test flow
- [docs/lsp.md](lsp.md) for running `eudkit-lsp` and the editor integrations
- [README.md](../README.md) for the repository overview and version table
