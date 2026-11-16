# eudkit LSP for VS Code

> **한국어 안내:** [editors/vscode/README.ko.md](./README.ko.md) (English document is the SSOT).

Completion, hover documentation, and capacity diagnostics for
[eudkit](https://github.com/Tb1281/eudkit) TypeScript and Python DSL
files. This extension is a thin client for the `eudkit-lsp` language server in
the eudkit repository; it contains no DSL logic of its own.

## Install

1. Build the language server from an eudkit checkout:

   ```sh
   cargo build -p eudkit-lsp --release
   ```

   This produces `target/release/eudkit-lsp` (`eudkit-lsp.exe` on Windows). The
   debug binary from `cargo build -p eudkit-lsp` works too.

2. Install the extension from the VS Code Marketplace or Open VSX:

   ```sh
   code --install-extension Tb1281.eudkit-lsp
   ```

   The same extension ID (`Tb1281.eudkit-lsp`) is published to Open VSX for
   compatible editors. To install a GitHub release build instead, download the
   `eudkit-vscode.vsix` asset from the release page and run:

   ```sh
   code --install-extension eudkit-vscode.vsix
   ```

   To build it yourself from the checkout:

   ```sh
   cd editors/vscode
   npm install
   npx --yes @vscode/vsce@4.1.1 package --out eudkit-vscode.vsix
   code --install-extension eudkit-vscode.vsix
   ```

   or run it directly from the checkout:

   ```sh
   code --extensionDevelopmentPath="$PWD/editors/vscode" .
   ```

## Locating the server

The extension resolves the `eudkit-lsp` binary in this order:

1. The `eudkit.lsp.path` setting, if non-empty. In an untrusted workspace only
   the user-level value is honored; workspace-provided values are ignored.
2. `target/release/eudkit-lsp` or `target/debug/eudkit-lsp` inside a workspace
   folder (`.exe` on Windows). This workspace lookup requires a trusted
   workspace.
3. `eudkit-lsp` on `PATH`.

Set the path explicitly when the server lives outside the workspace:

```json
{
  "eudkit.lsp.path": "/absolute/path/to/eudkit/target/release/eudkit-lsp"
}
```

Changing the setting restarts the client in place. If the server cannot be
started, the extension logs the command it tried once to the **eudkit LSP**
output channel and opens that channel instead of popping a modal error, so
ordinary TypeScript/Python workspaces without an eudkit install are not
interrupted.

The extension declares limited support for untrusted workspaces: you can use a
globally configured server or one on `PATH`, but workspace binary discovery and
workspace setting overrides are disabled until the workspace is trusted.

## Language features

The extension is a thin client: completion, hover documentation, and
`EUDSL-BOUNDS` capacity diagnostics are implemented by `eudkit-lsp`. Files are
matched by language, so the client attaches to TypeScript and Python documents
with the `file` scheme. The full capability list, diagnostics, and documented
limitations live in
[docs/lsp.md](../../docs/lsp.md#supported-features).

## Development

```sh
npm install
npm test
npx --yes @vscode/vsce@4.1.1 package --out eudkit-vscode.vsix
```

`npm test` runs the stubbed-`vscode` unit tests with `node --test`.
`node_modules` is intentionally not ignored so that `vsce` bundles
`vscode-languageclient` into the `.vsix`.

## License

MIT. See [LICENSE](./LICENSE).
