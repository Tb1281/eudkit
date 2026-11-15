//! Frozen build plans: `PlanEntry` -> fingerprint-verified `BoundPlan`.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use eudkit_lir::StaticCost;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::frontend::Frontend;

pub const PLAN_VERSION: &str = "1.0";

/// Stable basemap identity embedded in a frozen plan entry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BasemapFingerprint {
    /// Lowercase hex SHA-256 of the basemap bytes.
    pub sha256: String,
    pub size: u64,
}

impl BasemapFingerprint {
    pub fn of_file(path: &Path) -> Result<Self, PlanError> {
        let bytes = fs::read(path).map_err(|e| PlanError::Io {
            path: path.display().to_string(),
            source: e,
        })?;
        Ok(Self {
            sha256: hex_lower(&Sha256::digest(&bytes)),
            size: bytes.len() as u64,
        })
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// One frozen compilation unit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanEntry {
    pub name: String,
    pub source: PathBuf,
    pub frontend: Frontend,
    pub basemap: PathBuf,
    pub output: PathBuf,
    pub basemap_fingerprint: BasemapFingerprint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_static_cost: Option<StaticCost>,
}

impl PlanEntry {
    /// Freeze one source/basemap/output triple, hashing the basemap now.
    pub fn freeze(
        name: impl Into<String>,
        source: impl Into<PathBuf>,
        frontend: Frontend,
        basemap: &Path,
        output: impl Into<PathBuf>,
        expected_static_cost: Option<StaticCost>,
    ) -> Result<Self, PlanError> {
        Ok(Self {
            name: name.into(),
            source: source.into(),
            frontend,
            basemap: basemap.to_path_buf(),
            output: output.into(),
            basemap_fingerprint: BasemapFingerprint::of_file(basemap)?,
            expected_static_cost,
        })
    }
}

/// A serialized set of frozen plan entries.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BuildPlan {
    pub version: String,
    pub entries: Vec<PlanEntry>,
}

impl BuildPlan {
    pub fn new(entries: Vec<PlanEntry>) -> Self {
        Self {
            version: PLAN_VERSION.to_string(),
            entries,
        }
    }

    pub fn from_json(json: &str) -> Result<Self, PlanError> {
        let plan: Self = serde_json::from_str(json)?;
        if plan.version != PLAN_VERSION {
            return Err(PlanError::UnsupportedVersion(plan.version));
        }
        Ok(plan)
    }

    pub fn to_json(&self) -> Result<String, PlanError> {
        Ok(serde_json::to_string_pretty(self)?)
    }
}

/// A plan entry with every path resolved and its basemap fingerprint verified.
#[derive(Debug, Clone)]
pub struct BoundEntry {
    pub entry: PlanEntry,
    pub source: PathBuf,
    pub basemap: PathBuf,
    pub output: PathBuf,
}

/// A plan whose entries may be compiled without further path/fingerprint checks.
#[derive(Debug, Clone)]
pub struct BoundPlan {
    pub entries: Vec<BoundEntry>,
}

/// Bind a plan to concrete paths and verify every basemap fingerprint.
///
/// Relative paths are resolved against `plan_dir`. A `basemap_override`
/// replaces each entry's basemap path (the fingerprint is still verified).
/// `out_dir` redirects outputs by file name.
pub fn bind_plan(
    plan: &BuildPlan,
    plan_dir: &Path,
    basemap_override: Option<&Path>,
    out_dir: Option<&Path>,
) -> Result<BoundPlan, PlanError> {
    if plan.version != PLAN_VERSION {
        return Err(PlanError::UnsupportedVersion(plan.version.clone()));
    }
    let mut seen = BTreeMap::new();
    let mut entries = Vec::with_capacity(plan.entries.len());
    for entry in &plan.entries {
        if seen.insert(entry.name.clone(), ()).is_some() {
            return Err(PlanError::DuplicateName(entry.name.clone()));
        }
        let source = resolve(plan_dir, &entry.source);
        let basemap = basemap_override
            .map(Path::to_path_buf)
            .unwrap_or_else(|| resolve(plan_dir, &entry.basemap));
        let output = match out_dir {
            Some(dir) => {
                let name = entry
                    .output
                    .file_name()
                    .ok_or_else(|| PlanError::MissingOutput(entry.name.clone()))?;
                dir.join(name)
            }
            None => resolve(plan_dir, &entry.output),
        };

        let actual = BasemapFingerprint::of_file(&basemap)?;
        if actual.sha256 != entry.basemap_fingerprint.sha256 {
            return Err(PlanError::FingerprintMismatch {
                entry: entry.name.clone(),
                path: basemap.display().to_string(),
                expected: entry.basemap_fingerprint.sha256.clone(),
                actual: actual.sha256,
            });
        }
        if actual.size != entry.basemap_fingerprint.size {
            return Err(PlanError::FingerprintMismatch {
                entry: entry.name.clone(),
                path: basemap.display().to_string(),
                expected: format!("{} bytes", entry.basemap_fingerprint.size),
                actual: format!("{} bytes", actual.size),
            });
        }
        entries.push(BoundEntry {
            entry: entry.clone(),
            source,
            basemap,
            output,
        });
    }
    Ok(BoundPlan { entries })
}

fn resolve(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    }
}

/// Rewrite a frozen entry's source/basemap/output paths relative to the plan
/// file, so a plan remains valid when the repository is moved as a unit.
pub fn rebase_entry_paths(entry: &mut PlanEntry, plan_dir: &Path) {
    entry.source = relative_path(plan_dir, &entry.source);
    entry.basemap = relative_path(plan_dir, &entry.basemap);
    entry.output = relative_path(plan_dir, &entry.output);
}

/// Compute `target` relative to `base` using lexical path components.
pub fn relative_path(base: &Path, target: &Path) -> PathBuf {
    let base = normalize(&absolute(base));
    let target = normalize(&absolute(target));
    let base_parts: Vec<_> = base.components().collect();
    let target_parts: Vec<_> = target.components().collect();
    let common = base_parts
        .iter()
        .zip(&target_parts)
        .take_while(|(a, b)| a == b)
        .count();
    let mut out = PathBuf::new();
    for _ in common..base_parts.len() {
        out.push("..");
    }
    for part in &target_parts[common..] {
        out.push(part.as_os_str());
    }
    if out.as_os_str().is_empty() {
        out.push(".");
    }
    out
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

fn normalize(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    out.push("..");
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[derive(Debug, Error)]
pub enum PlanError {
    #[error("EUDSL-PLAN: unsupported plan version {0}")]
    UnsupportedVersion(String),
    #[error("E-PLAN-FINGERPRINT: entry '{entry}' basemap {path} mismatch: expected {expected}, got {actual}")]
    FingerprintMismatch {
        entry: String,
        path: String,
        expected: String,
        actual: String,
    },
    #[error("E-PLAN: duplicate entry name '{0}'")]
    DuplicateName(String),
    #[error("E-PLAN: entry '{0}' has no output file name")]
    MissingOutput(String),
    #[error("E-COST-GATE: entry '{entry}' static cost mismatch: expected {expected:?}, got {actual:?}")]
    CostMismatch {
        entry: String,
        expected: StaticCost,
        actual: StaticCost,
    },
    #[error("Failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("Invalid plan JSON: {0}")]
    Json(#[from] serde_json::Error),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_is_stable_sha256() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("base.scx");
        fs::write(&path, b"abc").unwrap();
        let fp = BasemapFingerprint::of_file(&path).unwrap();
        assert_eq!(fp.size, 3);
        assert_eq!(
            fp.sha256,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn binding_verifies_fingerprint_and_rebases_paths() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = dir.path().join("base.scx");
        fs::write(&basemap, b"basemap").unwrap();
        let entry = PlanEntry::freeze(
            "demo",
            "demo.ts",
            Frontend::Ts,
            &basemap,
            dir.path().join("demo.scx"),
            None,
        )
        .unwrap();
        let plan = BuildPlan::new(vec![entry]);
        let bound = bind_plan(&plan, dir.path(), None, Some(dir.path())).unwrap();
        assert_eq!(bound.entries.len(), 1);
        assert_eq!(bound.entries[0].source, dir.path().join("demo.ts"));
        assert_eq!(bound.entries[0].output, dir.path().join("demo.scx"));

        fs::write(&basemap, b"changed").unwrap();
        let err = bind_plan(&plan, dir.path(), None, None).unwrap_err();
        assert!(err.to_string().contains("E-PLAN-FINGERPRINT"));
    }

    #[test]
    fn rebased_plans_resolve_source_basemap_and_output_from_the_plan_dir() {
        let dir = tempfile::tempdir().unwrap();
        let plan_dir = dir.path().join("plans");
        fs::create_dir_all(&plan_dir).unwrap();
        fs::create_dir_all(dir.path().join("src")).unwrap();
        let source = dir.path().join("src/demo.ts");
        fs::write(&source, "// demo").unwrap();
        let basemap = dir.path().join("base.scx");
        fs::write(&basemap, b"basemap").unwrap();
        let output = dir.path().join("build/demo.scx");

        let mut entry = PlanEntry::freeze(
            "demo",
            &source,
            Frontend::Ts,
            &basemap,
            &output,
            None,
        )
        .unwrap();
        rebase_entry_paths(&mut entry, &plan_dir);
        assert_eq!(entry.source, PathBuf::from("../src/demo.ts"));
        assert_eq!(entry.basemap, PathBuf::from("../base.scx"));
        assert_eq!(entry.output, PathBuf::from("../build/demo.scx"));

        let plan = BuildPlan::new(vec![entry]);
        let bound = bind_plan(&plan, &plan_dir, None, None).unwrap();
        assert_eq!(
            fs::canonicalize(&bound.entries[0].source).unwrap(),
            fs::canonicalize(&source).unwrap()
        );
        assert_eq!(relative_path(&plan_dir, &plan_dir), PathBuf::from("."));
    }
}
