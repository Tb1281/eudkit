//! Frontend capture: run a `ts/` or `py/` module and read the HIR it builds.

use std::collections::HashSet;
use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};

use crate::PipelineError;

/// Minimum Node.js version the TypeScript runtime supports.
pub const MIN_NODE_VERSION: (u32, u32, u32) = (22, 6, 0);
/// Minimum Python version the Python runtime supports.
pub const MIN_PYTHON_VERSION: (u32, u32, u32) = (3, 11, 0);

pub const CAPTURE_MARKER: &str = "__EUDKIT_HIR__";
/// One structured warning per line on stderr, followed by a JSON object.
pub const WARNING_MARKER: &str = "__EUDKIT_WARN__ ";

/// A non-fatal frontend warning forwarded through the capture marker protocol.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct FrontendWarning {
    pub code: String,
    /// Canonical rendered text, including the code prefix.
    pub message: String,
}

impl FrontendWarning {
    /// Render the CLI-facing line (callers may prepend a plan-entry label).
    pub fn render(&self) -> String {
        format!("warning: {}", self.message)
    }
}

/// HIR JSON plus the warnings emitted while the frontend evaluated the source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapturedSource {
    pub hir_json: String,
    pub warnings: Vec<FrontendWarning>,
}

/// Which DSL frontend a source file uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Frontend {
    Ts,
    Py,
}

impl Frontend {
    pub fn from_path(path: &Path) -> Option<Self> {
        match path.extension().and_then(|e| e.to_str()) {
            Some("ts" | "mts" | "tsx") => Some(Self::Ts),
            Some("py") => Some(Self::Py),
            _ => None,
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "ts" | "typescript" => Some(Self::Ts),
            "py" | "python" => Some(Self::Py),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ts => "ts",
            Self::Py => "py",
        }
    }
}

/// Where the DSL runtime that compiles a source comes from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuntimeLocation {
    /// A repository checkout containing both `ts/index.ts` and `py/__init__.py`.
    Checkout(PathBuf),
    /// Installed `eudkit` packages, resolved relative to the source file.
    Package,
}

/// Whether `dir` is a checkout root that owns both DSL source trees.
pub fn is_checkout_root(dir: &Path) -> bool {
    dir.join("ts/index.ts").is_file() && dir.join("py/__init__.py").is_file()
}

fn absolute_dir(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

/// Walk `start` and its ancestors, returning the nearest checkout root.
pub fn find_checkout_root(start: &Path) -> Option<PathBuf> {
    absolute_dir(start)
        .ancestors()
        .find(|dir| is_checkout_root(dir))
        .map(Path::to_path_buf)
}

fn env_runtime_root() -> Result<Option<PathBuf>, PipelineError> {
    let Ok(value) = env::var("EUDKIT_ROOT") else {
        return Ok(None);
    };
    if value.trim().is_empty() {
        return Ok(None);
    }
    let root = PathBuf::from(value);
    if is_checkout_root(&root) {
        Ok(Some(root))
    } else {
        Err(PipelineError::RuntimeRootInvalid {
            path: root.display().to_string(),
        })
    }
}

fn adjacent_share_root() -> Option<PathBuf> {
    let exe = env::current_exe().ok()?;
    let exe_dir = exe.parent()?;
    [
        exe_dir.join("../share/eudkit"),
        exe_dir.join("share/eudkit"),
    ]
    .into_iter()
    .find(|candidate| is_checkout_root(candidate))
}

#[cfg(debug_assertions)]
fn manifest_fallback_enabled() -> bool {
    env::var("EUDKIT_NO_MANIFEST_FALLBACK").ok().as_deref() != Some("1")
}

#[cfg(not(debug_assertions))]
fn manifest_fallback_enabled() -> bool {
    false
}

fn manifest_fallback_root() -> Option<PathBuf> {
    if !manifest_fallback_enabled() {
        return None;
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    is_checkout_root(&root).then_some(root)
}

fn package_probe_file(source: Option<&Path>) -> PathBuf {
    match source {
        Some(path) if !path.as_os_str().is_empty() => path.to_path_buf(),
        _ => env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join("__eudkit_package_probe__.mjs"),
    }
}

const TS_PACKAGE_PROBE: &str = r#"
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
const source = process.env.EUDKIT_SOURCE;
try {
  const require = createRequire(pathToFileURL(source));
  process.stdout.write(require.resolve("eudkit"));
} catch {
  process.exit(1);
}
"#;

const PY_PACKAGE_PROBE: &str = r#"
import importlib.util, sys
spec = importlib.util.find_spec("eudkit")
if spec is None:
    sys.exit(1)
locations = list(spec.submodule_search_locations or [])
print(spec.origin or (locations[0] if locations else "eudkit"))
"#;

fn probe_ts_package(source: Option<&Path>) -> Option<String> {
    let probe = package_probe_file(source);
    let output = Command::new("node")
        .args(["--input-type=module", "-e", TS_PACKAGE_PROBE])
        .env("EUDKIT_SOURCE", &probe)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let resolved = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!resolved.is_empty()).then_some(resolved)
}

fn probe_python_package(source: Option<&Path>) -> Option<String> {
    let mut command = Command::new("python3");
    command.args(["-c", PY_PACKAGE_PROBE]);
    if let Some(parent) = source.and_then(Path::parent) {
        command.current_dir(parent);
    }
    let output = command.output().ok()?;
    if !output.status.success() {
        return None;
    }
    let resolved = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Some(if resolved.is_empty() {
        "found".to_string()
    } else {
        format!("found ({resolved})")
    })
}

/// Resolve a checkout root from the environment and directory ancestors.
///
/// This is the shared pre-package ladder used by [`resolve_runtime`] and
/// [`doctor_report`]: `EUDKIT_ROOT`, ancestors of `cwd` (the process current
/// directory when `None`), ancestors of `source`, then an adjacent
/// `share/eudkit`. The build-time manifest fallback is intentionally separate
/// because it only applies after package probing.
fn checkout_before_packages(
    cwd: Option<&Path>,
    source: Option<&Path>,
) -> Result<Option<PathBuf>, PipelineError> {
    if let Some(root) = env_runtime_root()? {
        return Ok(Some(root));
    }
    let cwd = match cwd {
        Some(cwd) => Some(cwd.to_path_buf()),
        None => env::current_dir().ok(),
    };
    if let Some(root) = cwd.as_deref().and_then(find_checkout_root) {
        return Ok(Some(root));
    }
    if let Some(root) = source.and_then(Path::parent).and_then(find_checkout_root) {
        return Ok(Some(root));
    }
    Ok(adjacent_share_root())
}

/// Resolve the DSL runtime for `source`, preferring a checkout when one exists.
///
/// Ladder: `EUDKIT_ROOT`, ancestors of the current directory, ancestors of the
/// source, an adjacent `share/eudkit`, installed packages, then (debug builds
/// only, unless `EUDKIT_NO_MANIFEST_FALLBACK=1`) the build-time checkout.
pub fn resolve_runtime(
    source: Option<&Path>,
    frontend: Frontend,
) -> Result<RuntimeLocation, PipelineError> {
    if let Some(root) = checkout_before_packages(None, source)? {
        return Ok(RuntimeLocation::Checkout(root));
    }
    let package_available = match frontend {
        Frontend::Ts => probe_ts_package(source).is_some(),
        Frontend::Py => probe_python_package(source).is_some(),
    };
    if package_available {
        return Ok(RuntimeLocation::Package);
    }
    if let Some(root) = manifest_fallback_root() {
        return Ok(RuntimeLocation::Checkout(root));
    }
    Err(PipelineError::RuntimeNotFound {
        frontend: frontend.as_str().to_string(),
        source_path: source
            .map(|path| path.display().to_string())
            .unwrap_or_else(|| "<cwd>".to_string()),
    })
}

/// One external tool's availability and version check.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolCheck {
    pub name: String,
    pub found: bool,
    pub version: Option<String>,
    pub minimum: String,
    pub ok: bool,
}

/// One installed language package's availability.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PackageCheck {
    pub found: bool,
    pub detail: String,
}

/// Result of `eudkit doctor`: runtime, toolchain, and package checks.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DoctorReport {
    pub eudkit_version: String,
    pub eudkit_root: Option<String>,
    pub checkout_root: Option<String>,
    pub runtime: String,
    /// Set when runtime resolution fails; `compile`/`synth` would emit `E-RUNTIME`.
    pub runtime_error: Option<String>,
    pub node: ToolCheck,
    pub python: ToolCheck,
    pub ts_package: PackageCheck,
    pub python_package: PackageCheck,
    pub ok: bool,
}

impl DoctorReport {
    /// Human-readable text for `eudkit doctor`.
    pub fn render_text(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!("eudkit {}\n", self.eudkit_version));
        out.push_str(&format!(
            "EUDKIT_ROOT: {}\n",
            self.eudkit_root.as_deref().unwrap_or("(not set)")
        ));
        out.push_str(&format!("Runtime: {}\n", self.runtime));
        if let Some(error) = &self.runtime_error {
            out.push_str(&format!("Runtime error: {error}\n"));
        }
        out.push_str(&format!(
            "Checkout root: {}\n",
            self.checkout_root.as_deref().unwrap_or("(not found)")
        ));
        for check in [&self.node, &self.python] {
            out.push_str(&format!(
                "{}: {} (requires >= {}) [{}]\n",
                check.name,
                check.version.as_deref().unwrap_or("not found"),
                check.minimum,
                if check.ok { "ok" } else { "fail" }
            ));
        }
        out.push_str(&format!(
            "TypeScript package eudkit: {}\n",
            self.ts_package.detail
        ));
        out.push_str(&format!(
            "Python package eudkit: {}\n",
            self.python_package.detail
        ));
        out.push_str(&format!(
            "Status: {}\n",
            if self.ok { "ok" } else { "problems found" }
        ));
        out
    }
}

fn format_version(version: (u32, u32, u32)) -> String {
    format!("{}.{}.{}", version.0, version.1, version.2)
}

fn parse_version_token(token: &str) -> Option<(u32, u32, u32)> {
    let candidate = token.trim_start_matches('v');
    let core: String = candidate
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    let core = core.trim_matches('.');
    if core.is_empty() {
        return None;
    }
    let mut parts = core.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next().and_then(|part| part.parse().ok()).unwrap_or(0);
    let patch = parts.next().and_then(|part| part.parse().ok()).unwrap_or(0);
    Some((major, minor, patch))
}

fn parse_version(text: &str) -> Option<(u32, u32, u32)> {
    text.split_whitespace().find_map(parse_version_token)
}

fn first_line(output: &std::process::Output) -> Option<String> {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    stdout
        .lines()
        .chain(stderr.lines())
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_string)
}

fn check_tool(name: &str, program: &str, minimum: (u32, u32, u32), cwd: &Path) -> ToolCheck {
    let output = Command::new(program)
        .arg("--version")
        .current_dir(cwd)
        .output();
    let (found, version) = match output {
        Ok(output) => (true, first_line(&output)),
        Err(_) => (false, None),
    };
    let ok = version
        .as_deref()
        .and_then(parse_version)
        .map(|found| found >= minimum)
        .unwrap_or(false);
    ToolCheck {
        name: name.to_string(),
        found,
        version,
        minimum: format_version(minimum),
        ok,
    }
}

/// Inspect the current runtime, toolchain, and installed DSL packages.
///
/// Missing tools and packages are reported, not returned as errors: `doctor`
/// is the command that diagnoses an unusable environment.
pub fn doctor_report(cwd: &Path) -> DoctorReport {
    let cwd = absolute_dir(cwd);
    let raw_root = env::var("EUDKIT_ROOT")
        .ok()
        .filter(|value| !value.trim().is_empty());

    // Resolve the checkout ladder first so an invalid EUDKIT_ROOT fails here
    // instead of silently falling through to the current directory or packages.
    let checkout_result = checkout_before_packages(Some(&cwd), None);

    let ts_source = cwd.join("__eudkit_doctor__.mjs");
    let ts_resolved = probe_ts_package(Some(&ts_source));
    let ts_package = PackageCheck {
        found: ts_resolved.is_some(),
        detail: ts_resolved
            .map(|path| format!("found ({path})"))
            .unwrap_or_else(|| "not found".to_string()),
    };
    let py_resolved = probe_python_package(Some(&cwd.join("__eudkit_doctor__.py")));
    let python_package = PackageCheck {
        found: py_resolved.is_some(),
        detail: py_resolved.unwrap_or_else(|| "not found".to_string()),
    };

    let node = check_tool("node", "node", MIN_NODE_VERSION, &cwd);
    let python = check_tool("python3", "python3", MIN_PYTHON_VERSION, &cwd);
    let (checkout_root, runtime, runtime_error) = match checkout_result {
        Err(error) => (
            None,
            "invalid EUDKIT_ROOT".to_string(),
            Some(error.to_string()),
        ),
        Ok(Some(root)) => (Some(root), "checkout".to_string(), None),
        Ok(None) => {
            if ts_package.found || python_package.found {
                (None, "package".to_string(), None)
            } else if let Some(root) = manifest_fallback_root() {
                (Some(root), "checkout".to_string(), None)
            } else {
                let error = PipelineError::RuntimeNotFound {
                    frontend: "ts|py".to_string(),
                    source_path: cwd.display().to_string(),
                };
                (None, "unresolved".to_string(), Some(error.to_string()))
            }
        }
    };
    let runtime_ok = checkout_root.is_some() || runtime == "package";
    let ok = node.ok && python.ok && runtime_ok;

    DoctorReport {
        eudkit_version: env!("CARGO_PKG_VERSION").to_string(),
        eudkit_root: raw_root,
        checkout_root: checkout_root.map(|root| root.display().to_string()),
        runtime,
        runtime_error,
        node,
        python,
        ts_package,
        python_package,
        ok,
    }
}

const TS_CAPTURE: &str = r#"
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const root = process.env.EUDKIT_ROOT;
const source = process.env.EUDKIT_SOURCE;
const runtimeUrl = root
  ? pathToFileURL(join(realpathSync(root), "ts", "index.ts")).href
  : pathToFileURL(
      createRequire(pathToFileURL(source)).resolve("eudkit"),
    ).href;
const runtime = await import(runtimeUrl);
runtime.onWarning((warning) => {
  process.stderr.write(
    "__EUDKIT_WARN__ " +
      JSON.stringify({ code: warning.code, message: warning.message }) +
      "\n",
  );
});
const ns = await import(pathToFileURL(source).href);

function describe(value) {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  const kind = typeof value;
  if (kind === "number") return `number (${value})`;
  if (kind === "string") {
    const shown = value.length > 60 ? value.slice(0, 57) + "..." : value;
    return `string (${JSON.stringify(shown)})`;
  }
  if (kind === "boolean") return `boolean (${value})`;
  if (kind === "function") return "function";
  if (kind === "object") return "object without toJson()";
  return `${kind} without toJson()`;
}

if (!Object.prototype.hasOwnProperty.call(ns, "module")) {
  console.error(
    `E-ENTRY: ${source} must export a compiled module named "module" ` +
      "from buildModule(...)",
  );
  process.exit(1);
}
const mod = ns.module;
if (
  mod === null ||
  typeof mod !== "object" ||
  typeof mod.toJson !== "function"
) {
  console.error(
    `E-ENTRY: ${source} exports "module" but it is ${describe(mod)}; ` +
      "expected a compiled module from buildModule(...)",
  );
  process.exit(1);
}
process.stdout.write("\n__EUDKIT_HIR__\n" + mod.toJson());
"#;

const PY_CAPTURE: &str = r#"
import json, os, runpy, sys, warnings
root = os.environ.get("EUDKIT_ROOT")
source = os.environ["EUDKIT_SOURCE"]
if root:
    sys.path.insert(0, root)
    from py import EudslWarning, on_warning
else:
    from eudkit import EudslWarning, on_warning

def _forward_warning(code, message):
    sys.stderr.write(
        "__EUDKIT_WARN__ "
        + json.dumps({"code": code, "message": message})
        + "\n"
    )

on_warning(_forward_warning)
warnings.filterwarnings("ignore", category=EudslWarning)
ns = runpy.run_path(source, run_name="__main__")

def describe(value):
    if value is None:
        return "None"
    if isinstance(value, bool):
        return f"bool ({value})"
    if isinstance(value, int):
        return f"int ({value})"
    if isinstance(value, float):
        return f"float ({value})"
    if isinstance(value, str):
        shown = value if len(value) <= 60 else value[:57] + "..."
        return f"str ({shown!r})"
    if isinstance(value, dict):
        return "dict without to_json()"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} without to_json()"
    if callable(value):
        if type(value).__name__ == "function":
            return "function"
        return f"{type(value).__name__} callable"
    return f"{type(value).__name__} without to_json()"

if "module" not in ns:
    sys.stderr.write(
        f'E-ENTRY: {source} must assign a compiled module named "module" '
        "from build_module(...)\n"
    )
    raise SystemExit(1)
mod = ns["module"]
# Match TypeScript: a callable is a function/class, not a compiled module,
# even when it carries a callable to_json attribute.
if mod is None or callable(mod) or not callable(getattr(mod, "to_json", None)):
    sys.stderr.write(
        f'E-ENTRY: {source} assigns "module" but it is {describe(mod)}; '
        "expected a compiled module from build_module(...)\n"
    )
    raise SystemExit(1)
sys.stdout.write("\n__EUDKIT_HIR__\n" + mod.to_json())
"#;

/// Extract the HIR payload emitted after the capture marker.
pub fn extract_payload(stdout: &str) -> Option<String> {
    let idx = stdout.rfind(CAPTURE_MARKER)?;
    let payload = stdout[idx + CAPTURE_MARKER.len()..].trim();
    if payload.is_empty() {
        None
    } else {
        Some(payload.to_string())
    }
}

/// Split structured warning lines from the rest of frontend stderr.
///
/// Duplicate `(code, message)` pairs are dropped in first-seen order. Marker
/// lines with invalid JSON stay in the cleaned stderr so a failure still shows
/// the frontend's original output.
fn split_warnings(stderr: &str) -> (Vec<FrontendWarning>, String) {
    let mut warnings = Vec::new();
    let mut seen = HashSet::new();
    let mut clean = String::new();
    for line in stderr.split_inclusive('\n') {
        let body = line.strip_suffix('\n').unwrap_or(line);
        let body = body.strip_suffix('\r').unwrap_or(body);
        let warning = body
            .strip_prefix(WARNING_MARKER)
            .and_then(|payload| serde_json::from_str::<FrontendWarning>(payload).ok());
        if let Some(warning) = warning {
            if seen.insert((warning.code.clone(), warning.message.clone())) {
                warnings.push(warning);
            }
            continue;
        }
        clean.push_str(line);
    }
    (warnings, clean)
}

fn render_warnings(warnings: &[FrontendWarning]) -> String {
    let mut out = String::new();
    for warning in warnings {
        out.push_str(&warning.render());
        out.push('\n');
    }
    out
}

/// Execute `source` with the matching DSL runtime and capture its HIR JSON.
///
/// This compatibility wrapper discards structured warnings. Prefer
/// [`capture_source_with_warnings`] when warnings should reach the user.
pub fn capture_source(source: &Path, frontend: Frontend) -> Result<String, PipelineError> {
    Ok(capture_source_with_warnings(source, frontend)?.hir_json)
}

/// Execute `source` and capture its HIR JSON plus any structured warnings.
pub fn capture_source_with_warnings(
    source: &Path,
    frontend: Frontend,
) -> Result<CapturedSource, PipelineError> {
    let source = source.canonicalize().map_err(|e| PipelineError::Io {
        path: source.display().to_string(),
        source: e,
    })?;
    let location = resolve_runtime(Some(&source), frontend)?;
    let (program, args) = match frontend {
        Frontend::Ts => (
            "node",
            vec![
                "--experimental-strip-types",
                "--no-warnings",
                "--input-type=module",
                "-e",
                TS_CAPTURE,
            ],
        ),
        Frontend::Py => ("python3", vec!["-c", PY_CAPTURE]),
    };
    let mut command = Command::new(program);
    command.args(&args).env("EUDKIT_SOURCE", &source);
    match &location {
        RuntimeLocation::Checkout(root) => {
            command.env("EUDKIT_ROOT", root);
        }
        RuntimeLocation::Package => {
            command.env_remove("EUDKIT_ROOT");
            // Python discovery is cwd-relative (`find_spec`), so run the
            // capture where the package probe succeeded.
            if let Some(parent) = source.parent().filter(|_| frontend == Frontend::Py) {
                command.current_dir(parent);
            }
        }
    }
    let output = command.output().map_err(|e| PipelineError::FrontendSpawn {
        program: program.to_string(),
        source: e,
    })?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let (warnings, clean_stderr) = split_warnings(&stderr);
    if !output.status.success() {
        return Err(PipelineError::Frontend {
            path: source.display().to_string(),
            status: output.status.to_string(),
            warnings: render_warnings(&warnings),
            stderr: clean_stderr.trim().to_string(),
        });
    }
    let hir_json = extract_payload(&stdout).ok_or_else(|| PipelineError::Frontend {
        path: source.display().to_string(),
        status: "0".to_string(),
        warnings: render_warnings(&warnings),
        stderr: format!(
            "frontend produced no {CAPTURE_MARKER} payload; stdout was: {}",
            stdout.trim()
        ),
    })?;
    Ok(CapturedSource { hir_json, warnings })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn frontend_detection_uses_file_extensions() {
        assert_eq!(Frontend::from_path(Path::new("a.ts")), Some(Frontend::Ts));
        assert_eq!(Frontend::from_path(Path::new("a.py")), Some(Frontend::Py));
        assert_eq!(Frontend::from_path(Path::new("a.hir.json")), None);
    }

    #[test]
    fn payload_extraction_ignores_frontend_logging() {
        let stdout = "debug line\n__EUDKIT_HIR__\n{\"version\":\"3.0\"}\n";
        assert_eq!(
            extract_payload(stdout).as_deref(),
            Some("{\"version\":\"3.0\"}")
        );
        assert_eq!(extract_payload("no marker"), None);
    }

    #[test]
    fn warning_lines_are_parsed_deduped_and_removed_from_stderr() {
        let stderr = concat!(
            "debug line\n",
            "__EUDKIT_WARN__ {\"code\":\"EUDSL-WARN-PERF\",\"message\":\"EUDSL-WARN-PERF: first\"}\n",
            "__EUDKIT_WARN__ {\"code\":\"EUDSL-WARN-SCOPE\",\"message\":\"EUDSL-WARN-SCOPE: second\"}\n",
            "__EUDKIT_WARN__ {\"code\":\"EUDSL-WARN-PERF\",\"message\":\"EUDSL-WARN-PERF: first\"}\n",
            "traceback line\n",
        );
        let (warnings, clean) = split_warnings(stderr);
        assert_eq!(
            warnings,
            vec![
                FrontendWarning {
                    code: "EUDSL-WARN-PERF".into(),
                    message: "EUDSL-WARN-PERF: first".into(),
                },
                FrontendWarning {
                    code: "EUDSL-WARN-SCOPE".into(),
                    message: "EUDSL-WARN-SCOPE: second".into(),
                },
            ]
        );
        assert_eq!(clean, "debug line\ntraceback line\n");
        assert_eq!(warnings[0].render(), "warning: EUDSL-WARN-PERF: first");
    }

    #[test]
    fn malformed_warning_marker_stays_in_stderr() {
        let stderr = "__EUDKIT_WARN__ not-json\nreal error\n";
        let (warnings, clean) = split_warnings(stderr);
        assert!(warnings.is_empty());
        assert_eq!(clean, stderr);
    }

    #[test]
    fn checkout_root_requires_both_frontend_trees() {
        let dir = tempfile::tempdir().unwrap();
        assert!(!is_checkout_root(dir.path()));
        fs::create_dir_all(dir.path().join("ts")).unwrap();
        fs::create_dir_all(dir.path().join("py")).unwrap();
        fs::write(dir.path().join("ts/index.ts"), "").unwrap();
        assert!(
            !is_checkout_root(dir.path()),
            "a checkout needs py/__init__.py too"
        );
        fs::write(dir.path().join("py/__init__.py"), "").unwrap();
        assert!(is_checkout_root(dir.path()));
    }

    #[test]
    fn find_checkout_root_walks_to_the_nearest_ancestor() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("ts")).unwrap();
        fs::create_dir_all(dir.path().join("py")).unwrap();
        fs::write(dir.path().join("ts/index.ts"), "").unwrap();
        fs::write(dir.path().join("py/__init__.py"), "").unwrap();
        let nested = dir.path().join("examples/demo");
        fs::create_dir_all(&nested).unwrap();
        assert_eq!(find_checkout_root(&nested), Some(dir.path().to_path_buf()));
    }

    #[test]
    fn invalid_explicit_runtime_root_reports_an_actionable_error() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("not-a-checkout");
        fs::create_dir_all(&root).unwrap();
        let err = env_runtime_root_for(&root).unwrap_err();
        let text = err.to_string();
        assert!(text.contains("E-RUNTIME"), "{text}");
        assert!(text.contains("EUDKIT_ROOT"), "{text}");
        assert!(text.contains("ts/index.ts"), "{text}");
        assert!(text.contains("py/__init__.py"), "{text}");
    }

    fn env_runtime_root_for(root: &Path) -> Result<Option<PathBuf>, PipelineError> {
        // Mirrors `env_runtime_root` for a path without mutating the
        // process-global environment (unit tests run in parallel).
        if is_checkout_root(root) {
            Ok(Some(root.to_path_buf()))
        } else {
            Err(PipelineError::RuntimeRootInvalid {
                path: root.display().to_string(),
            })
        }
    }

    #[test]
    fn version_checks_compare_semver_with_the_minimum() {
        assert_eq!(parse_version("v22.6.0"), Some((22, 6, 0)));
        assert_eq!(parse_version("Python 3.11.9"), Some((3, 11, 9)));
        assert!(parse_version("v22.6.0").unwrap() >= MIN_NODE_VERSION);
        assert!(parse_version("v22.5.9").unwrap() < MIN_NODE_VERSION);
        assert!(parse_version("Python 3.11.0").unwrap() >= MIN_PYTHON_VERSION);
        assert!(parse_version("Python 3.10.14").unwrap() < MIN_PYTHON_VERSION);
        assert_eq!(parse_version("not a version"), None);
    }

    #[test]
    fn doctor_report_names_the_runtime_and_toolchain() {
        let report = doctor_report(Path::new("."));
        assert_eq!(report.eudkit_version, env!("CARGO_PKG_VERSION"));
        assert!(report.render_text().contains("eudkit "));
        assert!(report.render_text().contains("node:"));
        assert!(report.render_text().contains("python3:"));
        assert_eq!(report.node.minimum, "22.6.0");
        assert_eq!(report.python.minimum, "3.11.0");
        let json = serde_json::to_value(&report).unwrap();
        assert_eq!(json["node"]["minimum"], "22.6.0");
        assert_eq!(json["python"]["minimum"], "3.11.0");
    }
}
