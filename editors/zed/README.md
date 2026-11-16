# eudkit LSP for Zed

> **한국어 안내:** [editors/zed/README.ko.md](./README.ko.md) (English document is the SSOT).

A Zed extension that launches [`eudkit-lsp`](../../crates/eudkit-lsp), the
language server for the eudkit TypeScript and Python DSLs. It provides
completion, hover documentation, and fixed-capacity diagnostics. The server
never compiles scripts and never changes map bytes.

## Prerequisites

- To install as a **Dev Extension** from source, Zed invokes `rustc` and `cargo`
  on the **host OS where the Zed UI runs** with the `wasm32-wasip2` target
  (even when opening a WSL2 remote project on Windows):

  ```sh
  rustup target add wasm32-wasip2
  ```

- The `eudkit-lsp` binary is resolved automatically in this order:
  1. `lsp.eudkit-lsp.binary.path` in Zed settings.
  2. `eudkit-lsp` on `PATH`.
  3. Automatic download of the matching release bundle from `Tb1281/eudkit`
     GitHub Releases into the extension's working directory.

  To build `eudkit-lsp` locally instead, run from the repository root:

  ```sh
  cargo build -p eudkit-lsp --release
  ```

## Install as a dev extension

1. Open the Extensions page in Zed.
2. Choose **Install Dev Extension**.
3. Select the `editors/zed` directory of this repository.
4. Restart the language server or reopen the project after changing the
   extension or its settings.

Zed compiles `editors/zed` from source as a standalone Cargo package targeting
`wasm32-wasip2` on the host OS (ignoring any prebuilt `extension.wasm`); it is
not part of the repository's root workspace.

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

`arguments` and `env` are optional. When `binary.path` is omitted, the
extension checks `which("eudkit-lsp")` and otherwise downloads the matching
release bundle from `Tb1281/eudkit` GitHub Releases automatically.

## Behaviour

The extension launches `eudkit-lsp`, which provides completion, hover
documentation, and fixed-capacity diagnostics over full-document sync. See
[`docs/lsp.md`](../../docs/lsp.md#supported-features) for the full capability
list, diagnostics, and documented limitations, including VS Code and Neovim
setup.

## License

MIT. See [LICENSE](./LICENSE).
