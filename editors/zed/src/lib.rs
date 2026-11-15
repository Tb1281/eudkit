//! Zed extension for the eudkit language server.
//!
//! The extension itself only resolves and launches `eudkit-lsp`; completion,
//! hover documentation, and capacity diagnostics all live in the language
//! server (`crates/eudkit-lsp`).

use zed_extension_api::{
    self as zed, settings::LspSettings, Command, LanguageServerId, Result, Worktree,
};

/// Must match the key under `[language_servers]` in `extension.toml`.
const LANGUAGE_SERVER_ID: &str = "eudkit-lsp";

/// Binary looked up on `$PATH` when no explicit path is configured.
const LANGUAGE_SERVER_BINARY: &str = "eudkit-lsp";

struct EudkitExtension;

impl zed::Extension for EudkitExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        language_server_id: &LanguageServerId,
        worktree: &Worktree,
    ) -> Result<Command> {
        let language_server_id = language_server_id.as_ref();
        if language_server_id != LANGUAGE_SERVER_ID {
            return Err(format!(
                "eudkit extension does not provide a language server named '{language_server_id}'"
            ));
        }

        let settings = LspSettings::for_worktree(language_server_id, worktree)
            .map_err(|err| format!("failed to read lsp.{language_server_id} settings: {err}"))?;

        let (configured_path, args, env) = match settings.binary {
            Some(binary) => (
                binary.path,
                binary.arguments.unwrap_or_default(),
                binary.env.unwrap_or_default(),
            ),
            None => (None, Vec::new(), Default::default()),
        };

        let command = match configured_path {
            Some(path) if !path.trim().is_empty() => path,
            _ => worktree
                .which(LANGUAGE_SERVER_BINARY)
                .ok_or_else(missing_binary_error)?,
        };

        Ok(Command {
            command,
            args,
            env: env.into_iter().collect(),
        })
    }
}

fn missing_binary_error() -> String {
    format!(
        "could not find `{LANGUAGE_SERVER_BINARY}`. Install it on your PATH \
         (for example `cargo install --path crates/eudkit-lsp --locked`) or set \
         `lsp.{LANGUAGE_SERVER_ID}.binary.path` to the absolute path of the \
         `{LANGUAGE_SERVER_BINARY}` executable in Zed's settings.json."
    )
}

zed::register_extension!(EudkitExtension);
