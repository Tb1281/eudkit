//! Typed `eudkit` build pipeline shared by `compile`, `dump-ir`, and plan builds.

pub mod frontend;
pub mod plan;
pub mod report;

use std::fs;
use std::path::{Path, PathBuf};

use eudkit_hir::{HirDocument, HirError, hir_from_json, hir_to_json};
use eudkit_lir::{
    ExecutedCost, LirDocument, LirError, StaticCost, simulate_trigger_loop, static_cost,
};
use eudkit_lower::{LowerError, prepare_lowering};
pub use eudkit_map::AssemblyOptions;
use eudkit_map::basemap::BasemapContext;
use eudkit_map::mpq::{QueuedContent, QueuedFile};
use eudkit_map::stage::Stage;
use eudkit_map::{MapTailError, TailInputs, assemble_map, assemble_map_with_context};
use eudkit_opt::cell::StoragePlan;
use thiserror::Error;

pub use frontend::{
    CapturedSource, DoctorReport, Frontend, FrontendWarning, RuntimeLocation, ToolCheck,
    capture_source, capture_source_with_warnings, doctor_report, find_checkout_root,
    resolve_runtime,
};
pub use plan::{
    BasemapFingerprint, BoundEntry, BoundPlan, BuildPlan, PLAN_VERSION, PlanEntry, PlanError,
    bind_plan, rebase_entry_paths, relative_path,
};
pub use report::{DumpReport, PackingEntry, StorageSummary};

/// Parsed and lowered program plus its storage/cost metadata.
#[derive(Debug, Clone)]
pub struct PreparedProgram {
    pub doc: HirDocument,
    pub optimized: eudkit_hir::HirModule,
    pub storage: StoragePlan,
    pub lir: LirDocument,
    /// Basemap used for STR addressing, when this is a map build.
    pub basemap: Option<PathBuf>,
    /// The basemap parse from lowering, handed to map assembly so it does not
    /// re-open the MPQ and re-parse `scenario.chk`. `Some` exactly when
    /// `basemap` is `Some`.
    pub basemap_context: Option<BasemapContext>,
    /// Directory relative embedded-file paths are resolved against (the
    /// compiling source's directory). `None` when the program came from a
    /// bare HIR string with no source location.
    pub source_dir: Option<PathBuf>,
    /// Warnings emitted while the frontend evaluated the source. Empty for
    /// pre-synthesized `.hir.json` input.
    pub warnings: Vec<FrontendWarning>,
}

impl PreparedProgram {
    /// Static cost of the lowered program BEFORE map assembly.
    ///
    /// The assembled payload always carries the 35-trigger once-per-tick
    /// gate, which `assemble_map` appends. `dump-ir` builds no map, so its
    /// report labels this value pre-assembly; use
    /// [`Self::assembled_static_cost`] for the map-build contract.
    pub fn static_cost(&self) -> StaticCost {
        static_cost(&self.lir)
    }

    /// Static cost of the map payload `compile_prepared_with_options` would
    /// write: [`Self::static_cost`] plus the once-per-tick gate.
    pub fn assembled_static_cost(&self) -> StaticCost {
        self.assembled_static_cost_with_options(AssemblyOptions::default())
    }

    /// Static cost of the map payload `compile_prepared_with_options` would
    /// write with the specified assembly options.
    pub fn assembled_static_cost_with_options(&self, options: AssemblyOptions) -> StaticCost {
        eudkit_map::assembled_static_cost_with_options(&self.lir, options)
    }

    /// Run `loops` trigger loops and return the last loop's executed cost.
    pub fn simulate(&self, loops: usize) -> Result<ExecutedCost, PipelineError> {
        let mut sim = simulate_trigger_loop(&self.lir)?;
        for _ in 1..loops.max(1) {
            sim.step_loop();
        }
        Ok(sim.last_executed_cost)
    }

    pub fn dump_report(&self, loops: Option<usize>) -> Result<DumpReport, PipelineError> {
        let executed = match loops {
            Some(loops) => Some(self.simulate(loops)?),
            None => None,
        };
        Ok(DumpReport::new(
            self.static_cost(),
            executed,
            self.doc.module.vars.len(),
            &self.storage,
            &self.lir,
        ))
    }

    /// Resolve `HirModule::files` into `MPQAddFile` queue entries.
    ///
    /// Relative paths are resolved against [`Self::source_dir`] (the
    /// compiling source's directory), exactly as authored by the frontend;
    /// absolute paths are used verbatim.
    pub fn queued_files(&self) -> Vec<QueuedFile> {
        self.doc
            .module
            .files
            .iter()
            .map(|file| {
                let path = Path::new(&file.path);
                let resolved = if path.is_absolute() {
                    path.to_path_buf()
                } else {
                    match &self.source_dir {
                        Some(dir) => dir.join(path),
                        None => path.to_path_buf(),
                    }
                };
                QueuedFile {
                    archived_name: file.name.clone(),
                    content: QueuedContent::Path(resolved),
                }
            })
            .collect()
    }
}

/// The single pipeline stage used by every command.
///
/// * `hir_json` is the frontend output (or a checked-in `.hir.json` fixture).
/// * `basemap` is optional for `dump-ir`; when present, module strings are
///   resolved against the real STR offset table.
pub fn prepare_hir_json(
    hir_json: &str,
    basemap: Option<&Path>,
) -> Result<PreparedProgram, PipelineError> {
    let stage = Stage::start("hir.parse");
    let doc = hir_from_json(hir_json)?;
    stage.finish();
    // The rest of the pre-assembly pipeline lives in `eudkit-lower`, so this
    // crate, `eudkit-compile`, and the tests all run the same sequence:
    // optimize + storage once, then the probe/final lowering passes.
    let lowered = prepare_lowering(&doc, basemap)?;
    Ok(PreparedProgram {
        doc,
        optimized: lowered.optimized,
        storage: lowered.storage,
        lir: lowered.lir,
        basemap: basemap.map(Path::to_path_buf),
        basemap_context: lowered.basemap_context,
        source_dir: None,
        warnings: Vec::new(),
    })
}

/// Run a `.ts`/`.py` source through its frontend and return validated HIR JSON
/// plus the warnings emitted during frontend evaluation.
///
/// `synth` uses this to materialize the HIR artifact without lowering or map
/// assembly, so frontend-only diagnostics surface before any basemap is chosen.
pub fn synth_hir(
    source: &Path,
    frontend: Option<Frontend>,
) -> Result<CapturedSource, PipelineError> {
    let frontend = frontend
        .or_else(|| Frontend::from_path(source))
        .ok_or_else(|| PipelineError::UnknownFrontend(source.display().to_string()))?;
    let stage = Stage::start("frontend");
    let captured = capture_source_with_warnings(source, frontend)?;
    stage.finish();
    let stage = Stage::start("synth.hir");
    let doc = hir_from_json(&captured.hir_json)?;
    let json = hir_to_json(&doc)?;
    stage.finish();
    Ok(CapturedSource {
        hir_json: json,
        warnings: captured.warnings,
    })
}

/// Whether `path` names a synthesized HIR JSON document (`.json`).
///
/// This is deliberately extension-only: HIR fixtures are named
/// `*.hir.json`, but any `.json` source is read directly without a frontend.
pub fn is_json_hir(path: &Path) -> bool {
    path.extension().and_then(|e| e.to_str()) == Some("json")
}

/// Run a `.ts`/`.py` source through its frontend and prepare it.
///
/// Frontend warnings are attached to [`PreparedProgram::warnings`]; callers
/// that render diagnostics should do so before the map tail.
pub fn prepare_source(
    source: &Path,
    frontend: Option<Frontend>,
    basemap: Option<&Path>,
) -> Result<PreparedProgram, PipelineError> {
    let source_dir = source
        .canonicalize()
        .ok()
        .as_deref()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .or_else(|| source.parent().map(Path::to_path_buf));
    if is_json_hir(source) {
        let json = fs::read_to_string(source).map_err(|e| PipelineError::Io {
            path: source.display().to_string(),
            source: e,
        })?;
        let mut prepared = prepare_hir_json(&json, basemap)?;
        prepared.source_dir = source_dir;
        return Ok(prepared);
    }
    let frontend = frontend
        .or_else(|| Frontend::from_path(source))
        .ok_or_else(|| PipelineError::UnknownFrontend(source.display().to_string()))?;
    let stage = Stage::start("frontend");
    let captured = capture_source_with_warnings(source, frontend)?;
    stage.finish();
    let mut prepared = prepare_hir_json(&captured.hir_json, basemap)?;
    prepared.source_dir = source_dir;
    prepared.warnings = captured.warnings;
    Ok(prepared)
}

/// Assemble a prepared map build with explicit options and return its pinned
/// static cost.
pub fn compile_prepared_with_options(
    prepared: &PreparedProgram,
    out_path: &Path,
    options: AssemblyOptions,
) -> Result<StaticCost, PipelineError> {
    let basemap = prepared
        .basemap
        .as_deref()
        .ok_or(PipelineError::MissingBasemap)?;
    if let Some(parent) = out_path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| PipelineError::Io {
                path: parent.display().to_string(),
                source: e,
            })?;
        }
    }
    let inputs = TailInputs {
        lir: &prepared.lir,
        basemap,
        out_path,
        sector_size: None,
        mrgndata: None,
        draw_log_json: None,
        queued_files: prepared.queued_files(),
        debug_emit_chk: None,
        no_fake_isom: false,
        assembly: options,
    };
    let summary = match prepared.basemap_context.as_ref() {
        // Reuse the parse lowering already paid for instead of re-opening and
        // re-parsing the basemap in the map tail.
        Some(ctx) => assemble_map_with_context(inputs, ctx)?,
        None => assemble_map(inputs)?,
    };
    Ok(summary.static_cost)
}

/// Read and version-check a frozen plan file.
pub fn read_plan(path: &Path) -> Result<BuildPlan, PipelineError> {
    let json = fs::read_to_string(path).map_err(|e| PipelineError::Io {
        path: path.display().to_string(),
        source: e,
    })?;
    Ok(BuildPlan::from_json(&json)?)
}

/// Options for a frozen plan build (see `compile_plan`).
#[derive(Debug, Clone)]
pub struct CompilePlanOptions {
    /// Replace each entry's basemap path (the fingerprint is still verified).
    pub basemap_override: Option<PathBuf>,
    /// Redirect outputs by file name.
    pub out_dir: Option<PathBuf>,
    /// Skip the static-cost gate (`--no-cost-gate`).
    pub skip_cost_gate: bool,
    /// Map-assembly policy, including whether to append the once-per-tick
    /// gate around the compiled payload. Defaults to the gate-enabled
    /// [`AssemblyOptions::default`].
    pub assembly: AssemblyOptions,
}

impl Default for CompilePlanOptions {
    fn default() -> Self {
        Self {
            basemap_override: None,
            out_dir: None,
            skip_cost_gate: false,
            assembly: AssemblyOptions::default(),
        }
    }
}

/// Build every entry of a frozen plan, enforcing the static-cost gate.
///
/// This is the `compile --plan` loop, moved verbatim out of the driver
/// binary so the gate is unit-testable: for each bound entry, prepare the
/// source, compare its static cost against the entry's expected cost
/// (unless the gate is skipped or the entry pins no cost), and only then
/// write the map. On the first mismatch the build aborts, and entries
/// before the failing one keep their maps: partial writes are the
/// preserved driver semantics, not atomicity.
///
/// Each entry's `compiled <name> -> <output> (<n>T static)` line is
/// delivered to `on_line` immediately after that entry completes, in
/// build order — not buffered until success. The old driver printed
/// each line inside the loop, so on a mid-build failure the lines for
/// already-built entries were already on stdout; the callback preserves
/// that failure-path stdout exactly. The driver passes a callback that
/// prints each line; tests collect them.
pub fn compile_plan(
    plan_path: &Path,
    options: &CompilePlanOptions,
    on_line: &mut dyn FnMut(&str),
) -> Result<(), PipelineError> {
    compile_plan_with_warnings(plan_path, options, on_line, &mut |_, _| {})
}

/// Like [`compile_plan`], but forwards each entry's frontend warnings before
/// that entry is built. The callback receives the plan entry name and warning.
pub fn compile_plan_with_warnings(
    plan_path: &Path,
    options: &CompilePlanOptions,
    on_line: &mut dyn FnMut(&str),
    on_warning: &mut dyn FnMut(&str, &FrontendWarning),
) -> Result<(), PipelineError> {
    let plan = read_plan(plan_path)?;
    let plan_dir = plan_path.parent().unwrap_or(Path::new("."));
    let bound = bind_plan(
        &plan,
        plan_dir,
        options.basemap_override.as_deref(),
        options.out_dir.as_deref(),
    )?;
    if bound.entries.is_empty() {
        return Err(PipelineError::EmptyPlan);
    }
    for entry in bound.entries {
        let prepared = prepare_source(
            &entry.source,
            Some(entry.entry.frontend),
            Some(&entry.basemap),
        )?;
        for warning in &prepared.warnings {
            on_warning(&entry.entry.name, warning);
        }
        // Gate on the same gate-inclusive cost the map build reports.
        // Computing it before `compile_prepared_with_options` preserves the
        // rule that a cost mismatch never writes the entry's map.
        let cost = prepared.assembled_static_cost_with_options(options.assembly);
        if !options.skip_cost_gate {
            if let Some(expected) = entry.entry.expected_static_cost {
                if expected != cost {
                    return Err(PlanError::CostMismatch {
                        entry: entry.entry.name.clone(),
                        expected,
                        actual: cost,
                    }
                    .into());
                }
            }
        }
        let assembled = compile_prepared_with_options(&prepared, &entry.output, options.assembly)?;
        debug_assert_eq!(
            assembled, cost,
            "plan cost gate and assembled payload must agree"
        );
        on_line(&format!(
            "compiled {} -> {} ({}T static)",
            entry.entry.name,
            entry.output.display(),
            assembled.triggers
        ));
    }
    Ok(())
}

#[derive(Debug, Error)]
pub enum PipelineError {
    #[error(transparent)]
    Hir(#[from] HirError),
    #[error(transparent)]
    Lower(#[from] LowerError),
    #[error(transparent)]
    Lir(#[from] LirError),
    #[error(transparent)]
    Map(#[from] MapTailError),
    #[error(transparent)]
    Plan(#[from] PlanError),
    #[error("Failed to read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("E-FRONTEND: cannot determine frontend for {0} (use --frontend ts|py)")]
    UnknownFrontend(String),
    #[error(
        "E-RUNTIME: EUDKIT_ROOT={path} is not an eudkit checkout \
         (expected both ts/index.ts and py/__init__.py under it)"
    )]
    RuntimeRootInvalid { path: String },
    #[error(
        "E-RUNTIME: cannot locate the eudkit DSL runtime for the {frontend} frontend \
         (source: {source_path}).\n\
         Set EUDKIT_ROOT to an eudkit checkout containing ts/index.ts and \
         py/__init__.py, install the eudkit package, or run from inside the checkout."
    )]
    RuntimeNotFound {
        frontend: String,
        source_path: String,
    },
    #[error("E-FRONTEND: failed to start {program}: {source}")]
    FrontendSpawn {
        program: String,
        #[source]
        source: std::io::Error,
    },
    #[error("{warnings}E-FRONTEND: {path} failed ({status}):\n{stderr}")]
    Frontend {
        path: String,
        status: String,
        warnings: String,
        stderr: String,
    },
    #[error("E-PLAN: map compilation requires a basemap")]
    MissingBasemap,
    /// A plan file with no entries. The message preserves the driver's
    /// historical wording verbatim.
    #[error("compile: plan contains no entries")]
    EmptyPlan,
}

#[cfg(test)]
mod tests {
    use super::*;
    use eudkit_hir::hir_to_json;

    #[test]
    fn prepare_hir_reports_static_cost_and_storage() {
        let hir_json = r#"{
            "version":"3.2",
            "module":{
                "vars":[{"id":0,"init":1},{"id":1,"init":0,"storage":"page"}],
                "stmts":[
                    {"kind":"vartransfer","dst_var":0,"src_var":1,"modifier":7},
                    {"kind":"trigger","id":0,"conds":[{"locid":0,"player":0,"amount":0,"unit":0,"comparison":0,"condtype":22,"restype":0,"flags":0,"eudx":0}],"acts":[],"preserved":true,"flags":4}
                ]
            }
        }"#;
        let prepared = prepare_hir_json(hir_json, None).unwrap();
        let cost = prepared.static_cost();
        assert!(
            cost.triggers >= 1,
            "lowering must emit the program triggers"
        );
        assert_eq!(
            prepared.assembled_static_cost().triggers,
            cost.triggers + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
        );
        assert_eq!(prepared.storage.trigger_page_vars, 1);
        assert!(prepared.storage.dword_cell_vars + prepared.storage.bit_cell_vars >= 1);

        let report = prepared.dump_report(Some(1)).unwrap();
        assert!(report.executed_cost.is_some());
        assert!(report.pre_assembly);
        assert!(report.render_text().contains("Static cost:"));
        assert!(report.render_text().contains("pre-assembly"));
    }

    #[test]
    fn prepared_hir_round_trips_through_json() {
        let hir_json = r#"{"version":"3.2","module":{"stmts":[]}}"#;
        let prepared = prepare_hir_json(hir_json, None).unwrap();
        let _ = hir_to_json(&prepared.doc);
        assert_eq!(prepared.static_cost().triggers, 0);
    }

    fn repo_basemap() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/outputmap/basemap/basemap.scx")
    }

    fn plan_hir_source(dir: &Path) -> PathBuf {
        let path = dir.join("demo.hir.json");
        fs::write(
            &path,
            r#"{
            "version":"3.2",
            "module":{
                "vars":[{"id":0,"init":1},{"id":1,"init":0,"storage":"page"}],
                "stmts":[
                    {"kind":"vartransfer","dst_var":0,"src_var":1,"modifier":7},
                    {"kind":"trigger","id":0,"conds":[{"locid":0,"player":0,"amount":0,"unit":0,"comparison":0,"condtype":22,"restype":0,"flags":0,"eudx":0}],"acts":[],"preserved":true,"flags":4}
                ]
            }
        }"#,
        )
        .unwrap();
        path
    }

    fn write_plan(dir: &Path, entries: Vec<PlanEntry>) -> PathBuf {
        let plan_path = dir.join("plan.json");
        fs::write(&plan_path, BuildPlan::new(entries).to_json().unwrap()).unwrap();
        plan_path
    }

    #[test]
    fn compile_plan_writes_map_when_expected_cost_is_none() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = repo_basemap();
        let source = plan_hir_source(dir.path());
        let out = dir.path().join("demo.scx");
        let entry = PlanEntry::freeze("demo", &source, Frontend::Ts, &basemap, &out, None).unwrap();
        let plan_path = write_plan(dir.path(), vec![entry]);
        let mut lines = Vec::new();
        compile_plan(&plan_path, &CompilePlanOptions::default(), &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap();
        assert_eq!(lines.len(), 1);
        assert!(lines[0].starts_with("compiled demo -> "), "{}", lines[0]);
        assert!(lines[0].contains("T static"), "{}", lines[0]);
        assert!(out.is_file(), "plan build must write the map");
    }

    #[test]
    fn compile_plan_rejects_drifted_cost_before_writing() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = repo_basemap();
        let source = plan_hir_source(dir.path());
        let prepared = prepare_source(&source, Some(Frontend::Ts), Some(&basemap)).unwrap();
        let mut expected = prepared.assembled_static_cost();
        expected.triggers += 1;
        let out = dir.path().join("demo.scx");
        let entry = PlanEntry::freeze(
            "demo",
            &source,
            Frontend::Ts,
            &basemap,
            &out,
            Some(expected),
        )
        .unwrap();
        let plan_path = write_plan(dir.path(), vec![entry]);
        let mut lines = Vec::new();
        let err = compile_plan(&plan_path, &CompilePlanOptions::default(), &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap_err();
        assert!(err.to_string().contains("E-COST-GATE"), "{err}");
        assert!(!out.is_file(), "a cost-gated build must not write a map");
    }

    #[test]
    fn compile_plan_keeps_first_entry_map_on_second_entry_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = repo_basemap();
        let source = plan_hir_source(dir.path());
        let prepared = prepare_source(&source, Some(Frontend::Ts), Some(&basemap)).unwrap();
        let mut drifted = prepared.assembled_static_cost();
        drifted.triggers += 1;
        let first_out = dir.path().join("first.scx");
        let second_out = dir.path().join("second.scx");
        let first =
            PlanEntry::freeze("first", &source, Frontend::Ts, &basemap, &first_out, None).unwrap();
        let second = PlanEntry::freeze(
            "second",
            &source,
            Frontend::Ts,
            &basemap,
            &second_out,
            Some(drifted),
        )
        .unwrap();
        let plan_path = write_plan(dir.path(), vec![first, second]);
        let mut lines = Vec::new();
        let err = compile_plan(&plan_path, &CompilePlanOptions::default(), &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap_err();
        assert!(err.to_string().contains("E-COST-GATE"), "{err}");
        assert!(
            first_out.is_file(),
            "entries before a failing entry keep their maps"
        );
        assert!(
            !second_out.is_file(),
            "the failing entry must not write a map"
        );
        // Lines stream as entries complete: the first entry's line was
        // already delivered before the second entry failed (the old
        // driver printed it inside the loop; buffering until success
        // would drop it on failure).
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert!(lines[0].starts_with("compiled first -> "), "{}", lines[0]);
    }

    #[test]
    fn compile_plan_skips_gate_with_no_cost_gate() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = repo_basemap();
        let source = plan_hir_source(dir.path());
        let prepared = prepare_source(&source, Some(Frontend::Ts), Some(&basemap)).unwrap();
        let mut drifted = prepared.assembled_static_cost();
        drifted.triggers += 1;
        let out = dir.path().join("demo.scx");
        let entry = PlanEntry::freeze("demo", &source, Frontend::Ts, &basemap, &out, Some(drifted))
            .unwrap();
        let plan_path = write_plan(dir.path(), vec![entry]);
        let options = CompilePlanOptions {
            skip_cost_gate: true,
            ..Default::default()
        };
        let mut lines = Vec::new();
        compile_plan(&plan_path, &options, &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap();
        assert_eq!(lines.len(), 1);
        assert!(out.is_file());
    }

    #[test]
    fn compile_plan_rejects_fingerprint_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let basemap = repo_basemap();
        let mut bytes = fs::read(&basemap).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0xff;
        let tampered = dir.path().join("tampered.scx");
        fs::write(&tampered, bytes).unwrap();
        let source = plan_hir_source(dir.path());
        let out = dir.path().join("demo.scx");
        let entry = PlanEntry::freeze("demo", &source, Frontend::Ts, &basemap, &out, None).unwrap();
        let plan_path = write_plan(dir.path(), vec![entry]);
        let options = CompilePlanOptions {
            basemap_override: Some(tampered),
            ..Default::default()
        };
        let mut lines = Vec::new();
        let err = compile_plan(&plan_path, &options, &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap_err();
        assert!(err.to_string().contains("E-PLAN-FINGERPRINT"), "{err}");
        assert!(!out.is_file());
    }

    #[test]
    fn compile_plan_rejects_empty_plan() {
        let dir = tempfile::tempdir().unwrap();
        let plan_path = write_plan(dir.path(), vec![]);
        let mut lines = Vec::new();
        let err = compile_plan(&plan_path, &CompilePlanOptions::default(), &mut |line| {
            lines.push(line.to_string())
        })
        .unwrap_err();
        assert!(
            err.to_string().contains("plan contains no entries"),
            "{err}"
        );
    }
}
