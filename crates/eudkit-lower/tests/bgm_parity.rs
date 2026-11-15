//! Cross-frontend parity for the periodic BGM helper (`play_bgm` / `playBgm`)
//! and the general MPQ embed surface behind it (issue #25).
//!
//! The Python and TypeScript frontends must emit byte-identical HIR, including
//! the embedded-file declaration, and that HIR must lower cleanly.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lower::lower_document;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn run_ts(path: &str, length: &str) -> String {
    let script = format!(
        r#"
import {{ buildModule, playBgm }} from "./ts/index.ts";
const mod = buildModule(() => {{
  playBgm({path:?}, {length});
}});
process.stdout.write(mod.toJson());
"#
    );
    let out = Command::new("node")
        .args(["--input-type=module", "-e", &script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the TS snippet");
    assert!(
        out.status.success(),
        "node failed for ({path}, {length}): {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn run_py(path: &str, length: &str) -> String {
    let script = format!(
        r#"
import sys
from py import buildModule, play_bgm
def prog():
    play_bgm({path:?}, {length})
sys.stdout.write(buildModule(prog).to_json())
"#
    );
    let out = Command::new("python3")
        .args(["-c", &script])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run the Python snippet");
    assert!(
        out.status.success(),
        "python3 failed for ({path}, {length}): {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn py_and_ts_emit_byte_identical_bgm_hir() {
    // Python string literals and JS string literals agree on these paths.
    let cases = [
        ("audio/track.wav", "1.25"),
        ("bgm.wav", "1.0"),
        ("x.wav", "0.042"),
        ("nested/dir/song.wav", "12.5"),
        ("noextension", "0.1"),
    ];

    for (path, length) in cases {
        let ts = run_ts(path, length);
        let py = run_py(path, length);
        assert_eq!(ts, py, "HIR mismatch for ({path}, {length})");

        let doc = hir_from_json(&ts).expect("HIR must parse");
        assert_eq!(
            doc.module.files.len(),
            1,
            "one embedded track for ({path}, {length})"
        );
        assert_eq!(doc.module.files[0].name, "bgm");
        assert_eq!(doc.module.files[0].path, path);

        // The helper lowers to a real program (probe + gate + setup).
        let lir = lower_document(&doc).expect("HIR must lower");
        assert!(
            lir.module.items.len() > 1,
            "lowered BGM program must contain the helper, got {:?}",
            lir.module.items.len()
        );
    }
}

#[test]
fn py_and_ts_reject_the_same_invalid_lengths() {
    for length in ["0", "-1.0", "0.0", "Number.NaN"] {
        // JS and Python differ for NaN spellings; only compare the shared ones.
        if length.starts_with("Number") {
            continue;
        }
        let ts_src = format!(
            r#"import {{ buildModule, playBgm }} from "./ts/index.ts";
try {{
  buildModule(() => {{ playBgm("bgm.wav", {length}); }});
  process.stdout.write("ok");
}} catch (err) {{
  process.stdout.write(String(err.code ?? err.message));
}}
"#
        );
        let py_src = format!(
            r#"
from py import EudslError, buildModule, play_bgm
try:
    buildModule(lambda: play_bgm("bgm.wav", {length}))
except EudslError as err:
    print(err.code, end="")
"#
        );

        let ts_out = Command::new("node")
            .args(["--input-type=module", "-e", &ts_src])
            .current_dir(repo_root())
            .output()
            .expect("node must run the TS snippet");
        assert!(ts_out.status.success());
        let ts_res = String::from_utf8(ts_out.stdout).unwrap();

        let py_out = Command::new("python3")
            .args(["-c", &py_src])
            .current_dir(repo_root())
            .output()
            .expect("python3 must run the Python snippet");
        assert!(py_out.status.success());
        let py_res = String::from_utf8(py_out.stdout).unwrap();

        assert_eq!(ts_res, py_res, "error code mismatch for length {length}");
        assert!(
            ts_res == "EUDSL-RANGE" || ts_res == "EUDSL-TYPE",
            "unexpected error code {ts_res}"
        );
    }
}
