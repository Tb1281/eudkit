//! `eudkit` CLI driver: compile, dump-ir, and frozen plan builds.

mod args;

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process;

use eudkit::frontend::{Frontend, RuntimeLocation, doctor_report, resolve_runtime};
use eudkit::plan::{BuildPlan, PlanEntry};
use eudkit::{
    AssemblyOptions, CompilePlanOptions, FrontendWarning, PipelineError,
    compile_plan_with_warnings, compile_prepared_with_options, is_json_hir, prepare_source,
    prepare_source_with_folds, rebase_entry_paths, relative_path, synth_hir,
};

const PLAN_OUT_REQUIRES_FRONTEND: &str =
    "compile: --plan-out requires --frontend ts|py or a .ts/.py source";

/// The silent basemap embedded in `eudkit init` output.
const STARTER_BASEMAP: &[u8] =
    include_bytes!("../../../tests/outputmap/basemap/basemap_silent.scx");

fn main() {
    if let Err(err) = run() {
        eprintln!("{err}");
        process::exit(1);
    }
}

/// Render frontend warnings on stderr, optionally labelled with a plan entry.
fn print_warnings(warnings: &[FrontendWarning], no_warnings: bool, entry: Option<&str>) {
    if no_warnings {
        return;
    }
    for warning in warnings {
        match entry {
            Some(entry) => eprintln!("warning: [{entry}] {}", warning.message),
            None => eprintln!("{}", warning.render()),
        }
    }
}

/// Quote a path for the suggested shell command only when it needs quoting.
fn shell_path(path: &Path) -> String {
    let text = path.display().to_string();
    let needs_quotes = text
        .chars()
        .any(|c| c.is_whitespace() || "\"'\\$`&;|<>*?[]#!~()".contains(c));
    if needs_quotes {
        format!("{text:?}")
    } else {
        text
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "--help" || args[0] == "-h" {
        println!("{}", self::args::help_text());
        return Ok(());
    }
    match args[0].as_str() {
        "compile" => compile(&args[1..]),
        "dump-ir" => dump_ir(&args[1..]),
        "synth" => synth(&args[1..]),
        "doctor" => doctor(&args[1..]),
        "init" => init(&args[1..]),
        "--version" | "version" => {
            println!("eudkit {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        other => Err(format!("Unknown command '{other}'.\n\n{}", self::args::help_text()).into()),
    }
}

fn compile(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(parsed) = self::args::parse_compile(args)
        .map_err(|err| format!("{err}\nRun 'eudkit compile --help' for usage."))?
    else {
        println!("{}", self::args::command_help_text("compile"));
        return Ok(());
    };
    let source = parsed.source;
    let basemap = parsed.basemap;
    let out = parsed.out;
    let plan_out = parsed.plan_out;
    let out_dir = parsed.out_dir;
    let frontend = parsed.frontend;
    let skip_cost_gate = parsed.skip_cost_gate;
    let enable_tick_gate = parsed.enable_tick_gate;
    let no_warnings = parsed.no_warnings;

    if let Some(plan_path) = parsed.plan_path {
        if source.is_some() {
            return Err("compile: use either a source file or --plan, not both".into());
        }
        if out.is_some() {
            return Err("compile: --out is not valid with --plan (use --out-dir)".into());
        }
        if plan_out.is_some() {
            return Err("compile: --plan-out cannot be combined with --plan".into());
        }
        if frontend.is_some() {
            return Err("compile: --frontend is set per plan entry; omit it with --plan".into());
        }
        compile_plan_with_warnings(
            &plan_path,
            &CompilePlanOptions {
                basemap_override: basemap,
                out_dir,
                skip_cost_gate,
                assembly: AssemblyOptions { enable_tick_gate },
            },
            &mut |line| println!("{line}"),
            &mut |entry, warning| {
                print_warnings(std::slice::from_ref(warning), no_warnings, Some(entry));
            },
        )?;
        return Ok(());
    }

    let source = source.ok_or("compile: missing source file (or use --plan <plan.json>)")?;
    let basemap = basemap.ok_or("compile: missing --basemap <basemap.scx>")?;
    // A synthesized `.hir.json` source already carries lowered HIR, so
    // `prepare_source` can read it without a frontend. Frozen plans need
    // a frontend to record for later rebuilds, so requiring one keeps
    // plan-outs buildable from the HIR path.
    let is_hir_json = is_json_hir(&source);
    let frontend = match frontend.or_else(|| Frontend::from_path(&source)) {
        Some(frontend) => Some(frontend),
        None if is_hir_json => {
            if plan_out.is_some() {
                return Err(PLAN_OUT_REQUIRES_FRONTEND.into());
            }
            None
        }
        None => {
            return Err(format!("compile: cannot infer frontend for {}", source.display()).into());
        }
    };
    let out = out.unwrap_or_else(|| source.with_extension("scx"));

    let prepared = prepare_source(&source, frontend, Some(&basemap))?;
    print_warnings(&prepared.warnings, no_warnings, None);
    let cost =
        compile_prepared_with_options(&prepared, &out, AssemblyOptions { enable_tick_gate })?;
    println!(
        "compiled {} -> {} ({}T static)",
        source.display(),
        out.display(),
        cost.triggers
    );

    if let Some(plan_out) = plan_out {
        let frontend = frontend.ok_or(PLAN_OUT_REQUIRES_FRONTEND)?;
        let name = source
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("module")
            .to_string();
        let mut entry = PlanEntry::freeze(name, &source, frontend, &basemap, &out, Some(cost))?;
        let plan_dir = plan_out.parent().unwrap_or(Path::new("."));
        if !plan_dir.as_os_str().is_empty() {
            fs::create_dir_all(plan_dir)?;
        }
        rebase_entry_paths(&mut entry, plan_dir);
        let plan = BuildPlan::new(vec![entry]);
        fs::write(&plan_out, plan.to_json()?)?;
        println!("wrote plan {}", plan_out.display());
    }
    Ok(())
}

fn doctor(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(parsed) = self::args::parse_doctor(args)
        .map_err(|err| format!("{err}\nRun 'eudkit doctor --help' for usage."))?
    else {
        println!("{}", self::args::command_help_text("doctor"));
        return Ok(());
    };
    let cwd = env::current_dir()?;
    let report = doctor_report(&cwd);
    if parsed.json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        print!("{}", report.render_text());
    }
    Ok(())
}

fn init(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(parsed) = self::args::parse_init(args)
        .map_err(|err| format!("{err}\nRun 'eudkit init --help' for usage."))?
    else {
        println!("{}", self::args::command_help_text("init"));
        return Ok(());
    };
    let requested = parsed.dir.unwrap_or_else(|| PathBuf::from("."));
    let source_name = match parsed.frontend {
        Frontend::Ts => "main.ts",
        Frontend::Py => "main.py",
    };

    // Reject a bad EUDKIT_ROOT before touching the filesystem, matching the
    // failure `compile`/`synth` would produce later.
    let early_hint = requested.join(source_name);
    if let Err(PipelineError::RuntimeRootInvalid { path }) =
        resolve_runtime(Some(&early_hint), parsed.frontend)
    {
        return Err(PipelineError::RuntimeRootInvalid { path }.into());
    }

    fs::create_dir_all(&requested).map_err(|source| PipelineError::Io {
        path: requested.display().to_string(),
        source,
    })?;
    let dir = fs::canonicalize(&requested).map_err(|source| PipelineError::Io {
        path: requested.display().to_string(),
        source,
    })?;
    let target_source_hint = dir.join(source_name);
    let (location, runtime_not_found) =
        match resolve_runtime(Some(&target_source_hint), parsed.frontend) {
            Ok(location) => (Some(location), false),
            Err(PipelineError::RuntimeNotFound { .. }) => (None, true),
            Err(error) => return Err(error.into()),
        };
    let checkout_root = match location {
        Some(RuntimeLocation::Checkout(root)) => {
            let canonical = fs::canonicalize(&root).map_err(|source| PipelineError::Io {
                path: root.display().to_string(),
                source,
            })?;
            Some(canonical)
        }
        _ => None,
    };
    if runtime_not_found {
        eprintln!(
            "init: note: no eudkit DSL runtime is currently resolved; the starter imports \
             the installed `eudkit` package. Install it (pip install ./py or npm install ./ts) \
             or set EUDKIT_ROOT=/path/to/eudkit before running `eudkit compile`."
        );
    }

    let basemap = dir.join("basemap.scx");
    for name in [source_name, "basemap.scx"] {
        let path = dir.join(name);
        if path.exists() {
            return Err(format!("init: refusing to overwrite existing {}", path.display()).into());
        }
    }

    fs::write(&basemap, STARTER_BASEMAP).map_err(|source| PipelineError::Io {
        path: basemap.display().to_string(),
        source,
    })?;

    let ts_import = match &checkout_root {
        Some(root) => {
            let relative = relative_path(&dir, &root.join("ts/index.ts"));
            let relative = relative.to_string_lossy().replace('\\', "/");
            if relative.starts_with('.') {
                relative
            } else {
                format!("./{relative}")
            }
        }
        None => "eudkit".to_string(),
    };
    let py_module = if checkout_root.is_some() {
        "py"
    } else {
        "eudkit"
    };
    let source = match parsed.frontend {
        Frontend::Ts => format!(
            "import {{ DisplayText, buildModule, once }} from {ts_import:?};\n\n\
             export const module = buildModule(() => {{\n\
             \x20 once([], () => {{\n\
             \x20   DisplayText(\"Hello from eudkit!\");\n\
             \x20 }});\n\
             }});\n"
        ),
        Frontend::Py => format!(
            "from {py_module} import DisplayText, build_module, once\n\n\n\
             def prog() -> None:\n\
             \x20   once([], lambda: DisplayText(\"Hello from eudkit!\"))\n\n\n\
             module = build_module(prog)\n"
        ),
    };
    let source_path = dir.join(source_name);
    fs::write(&source_path, source).map_err(|source| PipelineError::Io {
        path: source_path.display().to_string(),
        source,
    })?;
    let out = source_path.with_extension("scx");

    println!("created {}", source_path.display());
    println!("created {}", basemap.display());
    println!("Next compile command:");
    println!(
        "  eudkit compile {} --basemap {} --out {}",
        shell_path(&source_path),
        shell_path(&basemap),
        shell_path(&out)
    );
    Ok(())
}

fn synth(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(parsed) = self::args::parse_synth(args)
        .map_err(|err| format!("{err}\nRun 'eudkit synth --help' for usage."))?
    else {
        println!("{}", self::args::command_help_text("synth"));
        return Ok(());
    };
    let source = parsed.source.ok_or("synth: missing source file")?;
    let frontend = parsed.frontend;
    let compact = parsed.compact;
    let out = parsed.out;
    let no_warnings = parsed.no_warnings;

    let captured = synth_hir(&source, frontend)?;
    print_warnings(&captured.warnings, no_warnings, None);
    let text = if compact {
        captured.hir_json
    } else {
        let value: serde_json::Value = serde_json::from_str(&captured.hir_json)?;
        serde_json::to_string_pretty(&value)?
    };
    match out {
        Some(out) => {
            if let Some(parent) = out.parent() {
                if !parent.as_os_str().is_empty() {
                    fs::create_dir_all(parent)?;
                }
            }
            fs::write(&out, &text)?;
            println!("wrote HIR {}", out.display());
        }
        None => println!("{text}"),
    }
    Ok(())
}

fn dump_ir(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(parsed) = self::args::parse_dump_ir(args)
        .map_err(|err| format!("{err}\nRun 'eudkit dump-ir --help' for usage."))?
    else {
        println!("{}", self::args::command_help_text("dump-ir"));
        return Ok(());
    };
    let source = parsed.source.ok_or("dump-ir: missing source file")?;
    let frontend = parsed.frontend;
    let json = parsed.json;
    let loops = parsed.loops;
    let execute = parsed.execute;
    let no_warnings = parsed.no_warnings;
    let explain_folds = parsed.explain_folds;

    let prepared = prepare_source_with_folds(&source, frontend, explain_folds)?;
    print_warnings(&prepared.warnings, no_warnings, None);
    let report = prepared.dump_report(execute.then_some(loops))?;
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        print!(
            "Source: {}\nPacking: {} trigger node(s), {} blob dword(s)\n{}{}",
            source.display(),
            report.packing.len(),
            report.static_cost.blob_bytes / 4,
            report.render_text(),
            eudkit_opt::cell::render_allocations(&prepared.optimized, &prepared.storage)
        );
    }
    Ok(())
}
