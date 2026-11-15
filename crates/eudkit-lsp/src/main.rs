//! `eudkit-lsp` binary: JSON-RPC over stdio.

use std::process;

fn main() {
    if let Err(err) = eudkit_lsp::server::run_stdio() {
        eprintln!("eudkit-lsp: {err}");
        process::exit(1);
    }
}
