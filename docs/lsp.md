# Language server (`eudkit-lsp`)

`crates/eudkit-lsp` is a small Language Server Protocol server for both
frontends. It provides completion, hover documentation, and a narrow set of
capacity diagnostics over TypeScript and Python DSL documents. It is an
editor-surface tool: it never compiles and cannot change map bytes.

Prebuilt integrations live in `editors/vscode/` and `editors/zed/`; see
[docs/release.md](./release.md) for how each extension is packaged and
published. The manual local clients below remain useful while developing
against a checkout build.

## Build

From the repository root:

```sh
cargo build -p eudkit-lsp
```

The binary is `target/debug/eudkit-lsp` (or `target/release/eudkit-lsp` with
`--release`). The crate has no CLI flags; it speaks JSON-RPC over stdio. For the
full build/test matrix, see
[CONTRIBUTING.md#build-and-test](../CONTRIBUTING.md#build-and-test).

The CUnit/CSprite member tables and the stock vocabulary are generated at
build time from `schema/layouts/*.json` and `schema/stock-rows.json`. If the
schema is missing or unparseable, the build fails loudly rather than falling
back to a stale table.

## Run it

The server frames JSON-RPC 2.0 messages with `Content-Length` headers on
stdin/stdout, exactly like `rust-analyzer` or `typescript-language-server`.
Fatal startup errors go to stderr; stdout must stay protocol-clean.

```sh
/path/to/eudkit/target/debug/eudkit-lsp
```

The handshake sequence is the standard one:

1. `initialize`
2. `initialized`
3. `textDocument/didOpen`
4. `textDocument/completion` / `textDocument/hover`
5. `shutdown`
6. `exit` (or EOF)

All editor intelligence is implemented as pure functions in
`crates/eudkit-lsp/src/lib.rs`; `src/server.rs` only frames messages and keeps
the open-document map.

## Supported features

| LSP method | Behaviour |
|---|---|
| `initialize` | Advertises full-document sync (`textDocumentSync: 1`), completion (`triggerCharacters: [".", "(", ":"]`, `resolveProvider: false`), and hover. Returns `serverInfo` from the crate version. |
| `initialized` | No-op. |
| `textDocument/didOpen` | Stores the document and its `languageId`, then publishes diagnostics. |
| `textDocument/didChange` | Replaces the stored text with the **last** full-text `contentChanges` entry (full sync), then republishes diagnostics. |
| `textDocument/didClose` | Drops the text and language, then publishes an empty diagnostic list. |
| `textDocument/completion` | Suggests DSL vocabulary (stock/extension helpers, core identifiers, Python spellings) or receiver members after `.`. |
| `textDocument/hover` | Returns Markdown docs for vocabulary identifiers and members. |
| `shutdown` / `exit` | Sets the shutdown flag and exits cleanly. |
| `$/setTrace`, `workspace/didChangeConfiguration` | Accepted and ignored. |
| Unknown request | JSON-RPC error `-32601` (`method not found`). Unknown notifications are ignored. |

Member completion covers:

- `Trigger` (`act`, `cond`, `id`, `conds`, `acts`, `preserved`, `flags`,
  `next`, `prev`)
- `ActView` (`mask`, `dest`, `value`, `unit`, `modifier`, `acttype`, `flags`,
  `pad`, `eudx`, `strid`, `wavid`, `time`)
- `CondView` (`mask`, `locid`, `player`, `amount`, `unit`, `comparison`,
  `condtype`, `restype`, `flags`, `eudx`)
- `CUnit` / `CSprite` (every `size > 0` schema field, its aliases, and the
  DSL's common aliases such as `hp` → `hitPoints`/`hit_points`)
- `FixedString` (`write`, `append`, `writeFrom`, `print`, `printAt`, `display`,
  `displayAt`, `clear`)
- `FixedArray` (`length`, `elemType`, `vars`, `pages`)

## Diagnostics

Diagnostics are published on open/change as `textDocument/publishDiagnostics`
with `source: "eudkit-lsp"`, severity `1` (error), and code `EUDSL-BOUNDS`.
They check only fixed capacities:

- `FixedString(n)` needs `1 <= n <= 65535`.
- `FixedArray`, `Queue`, `Deque`, `Stack`, and `UnitGroup` capacities must be
  positive.
- `Trigger` may have at most 16 `conds` and 64 `acts`, in both the TypeScript
  object form and the Python `conds=[...]`/`acts=[...]` form.

The scanner treats string literals and comments as single tokens, so a
capacity-looking call inside a literal or comment is never flagged. Python
documents (`languageId: "python"`) additionally skip `#` comments and
triple-quoted strings; all other documents use language-neutral tokenization,
so TypeScript `#private` fields are not hidden.

For every other `EUDSL-*` code, see the
[diagnostics reference](./diagnostics.md); those checks run in the frontends,
not in the LSP.

## VS Code

**Primary integration:** install the shipped extension from the Marketplace or
Open VSX:

```sh
code --install-extension Tb1281.eudkit-lsp
```

The extension source lives in [`editors/vscode/`](../editors/vscode/) and is
packaged as `eudkit-vscode.vsix` on GitHub Releases. It auto-detects a
workspace `target/{release,debug}/eudkit-lsp`, falls back to `PATH`, and honors
the `eudkit.lsp.path` setting (global value only in untrusted workspaces).

The minimal local client below is a development fallback if you do not want to
install the extension. VS Code has no built-in generic LSP client, so this
requires a manual extension folder. Create `.vscode/eudkit-lsp/` with these two
files.

`package.json`:

```json
{
  "name": "eudkit-lsp-client",
  "displayName": "eudkit LSP",
  "publisher": "local",
  "version": "0.0.1",
  "engines": { "vscode": "^1.82.0" },
  "activationEvents": ["onLanguage:typescript", "onLanguage:python"],
  "main": "./extension.js",
  "dependencies": { "vscode-languageclient": "^9.0.1" }
}
```

`extension.js`:

```js
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;

function activate(context) {
  client = new LanguageClient(
    "eudkit-lsp",
    "eudkit LSP",
    {
      command: "/absolute/path/to/eudkit/target/debug/eudkit-lsp",
      args: [],
      transport: TransportKind.stdio,
    },
    {
      documentSelector: [
        { scheme: "file", language: "typescript" },
        { scheme: "file", language: "python" },
      ],
    },
  );
  context.subscriptions.push(client.start());
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
```

Install the client library and launch VS Code with the extension under
development:

```sh
cd .vscode/eudkit-lsp && npm install
cd ../.. && code --extensionDevelopmentPath="$PWD/.vscode/eudkit-lsp" .
```

Replace `/absolute/path/to/eudkit` with the checkout path.

## Neovim

Neovim 0.10+ has `vim.lsp.start` and `vim.fs.root` built in. Add this to
`init.lua` (or a `FileType` autocmd file):

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "typescript", "python" },
  callback = function(args)
    local root = vim.fs.root(args.buf, { "ts/index.ts", "py/__init__.py" })
      or vim.fn.getcwd()
    vim.lsp.start({
      name = "eudkit-lsp",
      cmd = { "/absolute/path/to/eudkit/target/debug/eudkit-lsp" },
      root_dir = root,
    })
  end,
})
```

Open a file under the repository (or a project with a `ts/index.ts` /
`py/__init__.py` above it) and run `:checkhealth lsp` (or `:LspInfo` with
`nvim-lspconfig`); `eudkit-lsp` should be listed as the attached client.

## Documented limitations

The crate-level docs in `crates/eudkit-lsp/src/lib.rs` state these limits
deliberately:

- **Receiver guessing is a heuristic, not inference.** A receiver is matched
  by identifier: `u`/`unit` → `CUnit`, `s`/`sprite` → `CSprite`,
  `trig`/`trigger`/`t` → `Trigger`, `str`/`msg` → `FixedString`,
  `arr`/`array` → `FixedArray`. Consequently `s.` on a `FixedString` variable
  named `s` returns `CSprite` members.
- **No binding tracking.** The server never infers types from assignments such
  as `x = CUnit(...)`; that would require a typechecker, which this crate is
  not.
- **Never compiles.** Completion, hover, and diagnostics are editor-surface
  only. There is no lowering, no HIR validation, and no effect on compiled map
  bytes.
- **Narrow diagnostics.** Only the capacity checks listed above are published.
  `EUDSL-TYPE`, `EUDSL-SCOPE`, `EUDSL-COND-TRUTHIFY`, and the rest stay in the
  frontend/compiler.
- **No definition, references, rename, formatting, code actions, signature
  help, semantic tokens, or workspace symbols.** These are not advertised in
  `initialize`.
- **Full-document sync only.** `didChange` must carry the complete new text;
  incremental range edits are not supported.
- **One document map, no workspace indexing.** Files are served from the
  `didOpen`/`didChange` text, not from disk.
- **Language handling is `languageId`-based.** Only `"python"` enables Python
  comment/string rules; every other id uses the language-neutral tokenizer.
