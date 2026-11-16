//! Zed extension for the eudkit language server.
//!
//! Resolves `eudkit-lsp` from Zed's `lsp.eudkit-lsp.binary.path` setting,
//! `$PATH`, or an automatically downloaded GitHub Release bundle. Completion,
//! hover documentation, and capacity diagnostics all live in `crates/eudkit-lsp`.

use std::fs;

use zed_extension_api::{
    self as zed, settings::LspSettings, Architecture, Command, DownloadedFileType,
    GithubReleaseOptions, LanguageServerId, LanguageServerInstallationStatus, Os, Result, Worktree,
};

/// Must match the key under `[language_servers]` in `extension.toml`.
const LANGUAGE_SERVER_ID: &str = "eudkit-lsp";

/// Binary looked up on `$PATH` when no explicit path is configured.
const LANGUAGE_SERVER_BINARY: &str = "eudkit-lsp";

/// GitHub repository serving prebuilt `eudkit` release bundles.
const GITHUB_REPO: &str = "Tb1281/eudkit";

/// Prefix for cached release bundle directories inside the extension work dir.
const VERSION_DIR_PREFIX: &str = "eudkit-lsp-";

struct EudkitExtension {
    cached_binary_path: Option<String>,
}

impl zed::Extension for EudkitExtension {
    fn new() -> Self {
        Self {
            cached_binary_path: None,
        }
    }

    fn language_server_command(
        &mut self,
        language_server_id: &LanguageServerId,
        worktree: &Worktree,
    ) -> Result<Command> {
        if language_server_id.as_ref() != LANGUAGE_SERVER_ID {
            return Err(format!(
                "eudkit extension does not provide a language server named '{}'",
                language_server_id.as_ref()
            ));
        }

        let settings = LspSettings::for_worktree(language_server_id.as_ref(), worktree)
            .map_err(|err| format!("failed to read lsp.{LANGUAGE_SERVER_ID} settings: {err}"))?;

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
            _ => match worktree.which(LANGUAGE_SERVER_BINARY) {
                Some(path) => path,
                None => self.ensure_downloaded_binary(language_server_id)?,
            },
        };

        Ok(Command {
            command,
            args,
            env: env.into_iter().collect(),
        })
    }
}

impl EudkitExtension {
    fn ensure_downloaded_binary(
        &mut self,
        language_server_id: &LanguageServerId,
    ) -> Result<String> {
        if let Some(path) = &self.cached_binary_path {
            if is_file(path) {
                return Ok(path.clone());
            }
        }

        let (target, ext, file_type, binary_name) = platform_bundle_spec()?;

        zed::set_language_server_installation_status(
            language_server_id,
            &LanguageServerInstallationStatus::CheckingForUpdate,
        );

        let release = match zed::latest_github_release(
            GITHUB_REPO,
            GithubReleaseOptions {
                require_assets: true,
                pre_release: false,
            },
        ) {
            Ok(release) => release,
            Err(err) => {
                if let Some(existing) = find_any_cached_binary(binary_name) {
                    zed::set_language_server_installation_status(
                        language_server_id,
                        &LanguageServerInstallationStatus::None,
                    );
                    self.cached_binary_path = Some(existing.clone());
                    return Ok(existing);
                }
                let message = format!(
                    "failed to fetch latest {GITHUB_REPO} release ({err}); {}",
                    manual_install_hint()
                );
                return Err(fail_status(language_server_id, message));
            }
        };

        let expected_suffix = format!("-{target}.{ext}");
        let asset = release
            .assets
            .iter()
            .find(|asset| {
                asset.name.starts_with("eudkit-") && asset.name.ends_with(&expected_suffix)
            })
            .ok_or_else(|| {
                let message = format!(
                    "no release asset matching '*{expected_suffix}' in {} {}; {}",
                    GITHUB_REPO,
                    release.version,
                    manual_install_hint()
                );
                fail_status(language_server_id, message)
            })?;

        let version_dir = format!("{VERSION_DIR_PREFIX}{}", release.version);
        let binary_path = match find_binary_in_dir(&version_dir, binary_name) {
            Some(path) => path,
            None => {
                zed::set_language_server_installation_status(
                    language_server_id,
                    &LanguageServerInstallationStatus::Downloading,
                );

                if let Err(err) = zed::download_file(&asset.download_url, &version_dir, file_type)
                {
                    let message = format!(
                        "failed to download {}: {err}; {}",
                        asset.name,
                        manual_install_hint()
                    );
                    return Err(fail_status(language_server_id, message));
                }

                let binary_path = find_binary_in_dir(&version_dir, binary_name).ok_or_else(|| {
                    let message = format!(
                        "downloaded {} did not contain bin/{binary_name}",
                        asset.name
                    );
                    fail_status(language_server_id, message)
                })?;

                zed::make_file_executable(&binary_path)?;
                remove_stale_version_dirs(&version_dir);
                binary_path
            }
        };

        zed::set_language_server_installation_status(
            language_server_id,
            &LanguageServerInstallationStatus::None,
        );
        self.cached_binary_path = Some(binary_path.clone());
        Ok(binary_path)
    }
}

fn platform_bundle_spec() -> Result<(&'static str, &'static str, DownloadedFileType, &'static str)>
{
    let (os, arch) = zed::current_platform();
    match (os, arch) {
        (Os::Linux, Architecture::X8664) => Ok((
            "x86_64-unknown-linux-gnu",
            "tar.gz",
            DownloadedFileType::GzipTar,
            "eudkit-lsp",
        )),
        (Os::Linux, Architecture::Aarch64) => Ok((
            "aarch64-unknown-linux-gnu",
            "tar.gz",
            DownloadedFileType::GzipTar,
            "eudkit-lsp",
        )),
        (Os::Mac, Architecture::Aarch64) => Ok((
            "aarch64-apple-darwin",
            "tar.gz",
            DownloadedFileType::GzipTar,
            "eudkit-lsp",
        )),
        (Os::Windows, Architecture::X8664) => Ok((
            "x86_64-pc-windows-msvc",
            "zip",
            DownloadedFileType::Zip,
            "eudkit-lsp.exe",
        )),
        (os, arch) => Err(format!(
            "no prebuilt {LANGUAGE_SERVER_BINARY} release bundle for {os:?}/{arch:?}; {}",
            manual_install_hint()
        )),
    }
}

fn is_file(path: &str) -> bool {
    fs::metadata(path).is_ok_and(|stat| stat.is_file())
}

fn find_binary_in_dir(version_dir: &str, binary_name: &str) -> Option<String> {
    let direct = format!("{version_dir}/bin/{binary_name}");
    if is_file(&direct) {
        return Some(direct);
    }

    let entries = fs::read_dir(version_dir).ok()?;
    for entry in entries.flatten() {
        let candidate = entry.path().join("bin").join(binary_name);
        let candidate_str = candidate.to_string_lossy().into_owned();
        if is_file(&candidate_str) {
            return Some(candidate_str);
        }
    }
    None
}

fn find_any_cached_binary(binary_name: &str) -> Option<String> {
    let entries = fs::read_dir(".").ok()?;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(VERSION_DIR_PREFIX) {
            if let Some(path) = find_binary_in_dir(&name, binary_name) {
                return Some(path);
            }
        }
    }
    None
}

fn remove_stale_version_dirs(active_dir: &str) {
    let Ok(entries) = fs::read_dir(".") else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(VERSION_DIR_PREFIX) && name != active_dir {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

fn fail_status(language_server_id: &LanguageServerId, message: String) -> String {
    zed::set_language_server_installation_status(
        language_server_id,
        &LanguageServerInstallationStatus::Failed(message.clone()),
    );
    message
}

fn manual_install_hint() -> String {
    format!(
        "install `{LANGUAGE_SERVER_BINARY}` on your PATH \
         (for example `cargo install --path crates/eudkit-lsp --locked`) or set \
         `lsp.{LANGUAGE_SERVER_ID}.binary.path` in Zed's settings.json"
    )
}

zed::register_extension!(EudkitExtension);
