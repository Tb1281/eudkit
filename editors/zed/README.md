# eudkit LSP for Zed

A Zed extension that launches [`eudkit-lsp`](../../crates/eudkit-lsp), the
language server for the eudkit TypeScript and Python DSLs. It provides
completion, hover documentation, and fixed-capacity diagnostics. The server
never compiles scripts and never changes map bytes.

## Prerequisites

- A Rust toolchain with the WebAssembly target used by Zed extensions:

  ```sh
  rustup target add wasm32-wasip2
  ```

- The `eudkit-lsp` binary. From the repository root:

  ```sh
  cargo build -p eudkit-lsp --release
  ```

  Either put `target/release/eudkit-lsp` on your `PATH`, or configure its
  absolute path in Zed (see below).

## Install as a dev extension

1. Open the Extensions page in Zed.
2. Choose **Install Dev Extension**.
3. Select the `editors/zed` directory of this repository.
4. Restart the language server or reopen the project after changing the
   extension or its settings.

Zed compiles `editors/zed` as a standalone Cargo package targeting
`wasm32-wasip2`; it is not part of the repository's root workspace.

## Configure Zed

`extension.toml` already binds `eudkit-lsp` to both `TypeScript` and `Python`,
so no `language_servers` entry is required. Add one only when you want explicit
ordering relative to your existing servers. The server ID is `eudkit-lsp`:

```json
{
  "languages": {
    "TypeScript": {
      "language_servers": ["eudkit-lsp", "..."]
    },
    "Python": {
      "language_servers": ["eudkit-lsp", "..."]
    }
  }
}
```

Replace `"..."` with your existing servers (for example
`"typescript-language-server"` or `"pyright"`), or remove it if `eudkit-lsp`
should be the only server. Without this block Zed keeps the default ordering
and still starts `eudkit-lsp`.

If `eudkit-lsp` is not on `PATH`, set its absolute path, arguments, and
environment explicitly:

```json
{
  "lsp": {
    "eudkit-lsp": {
      "binary": {
        "path": "/absolute/path/to/eudkit/target/release/eudkit-lsp",
        "arguments": [],
        "env": {}
      }
    }
  }
}
```

`arguments` and `env` are optional; the extension falls back to
`which("eudkit-lsp")` when `binary.path` is omitted. When neither resolves, Zed
shows an error asking you to install the binary or set
`lsp.eudkit-lsp.binary.path`.

## Behaviour

- Completion and hover cover the eudkit DSL vocabulary and receiver members
  after `.`.
- Diagnostics are limited to capacity checks (`FixedString`,
  `FixedArray`/`Queue`/`Deque`/`Stack`/`UnitGroup`, and `Trigger` condition and
  action limits).
- The server uses full-document sync and has no workspace indexing, so files
  are served from the editor buffer.

See [`docs/lsp.md`](../../docs/lsp.md) for the full protocol and feature
reference, including VS Code and Neovim setup.

## License

MIT. See [LICENSE](./LICENSE).
