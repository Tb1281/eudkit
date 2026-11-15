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
   npx @vscode/vsce package --out eudkit-vscode.vsix
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

## Features

- **Completion** for the DSL vocabulary (stock and extension helpers, core
  identifiers, Python spellings) and for receiver members after `.`
  (`Trigger`, `ActView`, `CondView`, `CUnit`, `CSprite`, `FixedString`,
  `FixedArray`).
- **Hover** documentation (Markdown) for vocabulary identifiers and members.
- **Capacity diagnostics** with source `eudkit-lsp` and code `EUDSL-BOUNDS`:
  `FixedString` capacities must be in `1..=65535`, `FixedArray` / `Queue` /
  `Deque` / `Stack` / `UnitGroup` capacities must be positive, and a `Trigger`
  may hold at most 16 `conds` and 64 `acts`. String literals and comments are
  never flagged.

Files are matched by language: the client attaches to TypeScript and Python
documents with the `file` scheme.

## Limitations

- **No type inference.** Receivers are guessed from their identifier
  (`u`/`unit` → `CUnit`, `s`/`sprite` → `CSprite`, `trig`/`trigger`/`t` →
  `Trigger`, `str`/`msg` → `FixedString`, `arr`/`array` → `FixedArray`), so a
  `FixedString` named `s` offers `CSprite` members.
- **Capacity checks only.** `EUDSL-TYPE`, `EUDSL-SCOPE`, and the other codes
  are produced by the eudkit frontends and compiler, not by the LSP.
- **The server never compiles.** No build, no lowering, no effect on compiled
  map bytes.
- **Full-document sync only.** No incremental edits, and files are served from
  the editor buffer rather than disk.
- **No definition, references, rename, formatting, code actions, signature
  help, semantic tokens, or workspace symbols.**
- **Python rules are `languageId`-based.** Only `python` documents get Python
  comment and triple-quoted-string handling.

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
