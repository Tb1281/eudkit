//! Shared flag-parsing mechanics for the `eudkit` driver, driven by
//! per-command spec tables.
//!
//! Each command keeps its own spec table (flags differ per command, and
//! `--out` means map output in `compile` but HIR output in `synth`, so
//! no merged option struct exists). What is shared is the mechanics:
//! flag matching, value taking, the unknown-flag error, the
//! exactly-one-positional rule, and the `--help` short-circuit. Error
//! strings preserve the old hand-written loops byte-for-byte.

use std::path::PathBuf;

use eudkit::frontend::Frontend;

/// What a flag arm returns: keep parsing, or stop for `--help`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlagOutcome {
    Continue,
    Help,
}

/// One flag in a command's spec table.
pub struct FlagSpec<S> {
    /// All accepted spellings; the first is canonical and is used in
    /// the "requires a value" error, matching the old hand loops
    /// (e.g. `-o` missing a value reports `--out requires a value`).
    pub names: &'static [&'static str],
    /// Whether the flag consumes the following argument as its value.
    pub takes_value: bool,
    /// Placeholder shown in generated help for a value flag, e.g.
    /// `"<basemap.scx>"`; `None` for boolean flags.
    pub value_name: Option<&'static str>,
    /// Crisp one-line description rendered beside the flag in help.
    pub help: &'static str,
    /// Apply the flag to the command state. Receives `Some(value)` for
    /// value flags and `None` for boolean flags.
    pub apply: fn(&mut S, Option<String>) -> Result<FlagOutcome, Box<dyn std::error::Error>>,
}

/// The `--help` / `-h` spec, shared by every command.
pub fn help_spec<S>() -> FlagSpec<S> {
    FlagSpec {
        names: &["--help", "-h"],
        takes_value: false,
        value_name: None,
        help: "Show help for this command.",
        apply: |_, _| Ok(FlagOutcome::Help),
    }
}

/// Parse a `--frontend` value, with the driver's historical error.
pub fn frontend_from_value(value: &str) -> Result<Frontend, Box<dyn std::error::Error>> {
    match Frontend::parse(value) {
        Some(frontend) => Ok(frontend),
        None => Err(format!("Unknown frontend '{value}' (use ts|py)").into()),
    }
}

/// Set a command's single positional (source file); a second positional
/// is the driver's historical "Unexpected argument" error.
pub fn set_source(
    slot: &mut Option<PathBuf>,
    value: String,
) -> Result<(), Box<dyn std::error::Error>> {
    if slot.is_none() {
        *slot = Some(PathBuf::from(value));
        Ok(())
    } else {
        Err(format!("Unexpected argument '{value}'").into())
    }
}

/// Parse `args` against `specs`, calling `positional` for each
/// non-flag argument.
///
/// Returns `Ok(true)` if `--help` was seen: the caller prints help and
/// returns `Ok(())` without validating required arguments, matching
/// the old loops, which returned early on `--help` (so arguments after
/// `--help` are never parsed). Any token starting with `-` that matches
/// no spec is an "Unknown option" error. A value flag consumes the
/// following argument unconditionally, even if it starts with `-`
/// (so `--frontend --compact` reports an unknown frontend, as before).
pub fn parse_flags<S>(
    args: &[String],
    state: &mut S,
    specs: &[FlagSpec<S>],
    mut positional: impl FnMut(&mut S, String) -> Result<(), Box<dyn std::error::Error>>,
) -> Result<bool, Box<dyn std::error::Error>> {
    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        if let Some(spec) = specs.iter().find(|s| s.names.contains(&arg.as_str())) {
            let value = if spec.takes_value {
                i += 1;
                Some(
                    args.get(i)
                        .cloned()
                        .ok_or_else(|| format!("{} requires a value", spec.names[0]))?,
                )
            } else {
                None
            };
            match (spec.apply)(state, value)? {
                FlagOutcome::Continue => {}
                FlagOutcome::Help => return Ok(true),
            }
        } else if arg.starts_with('-') {
            return Err(format!("Unknown option '{arg}'").into());
        } else {
            positional(state, arg.clone())?;
        }
        i += 1;
    }
    Ok(false)
}

/// Parsed `synth` arguments (`None` from `parse_synth` means `--help`).
#[derive(Debug, Default)]
pub struct SynthArgs {
    pub source: Option<PathBuf>,
    pub frontend: Option<Frontend>,
    pub out: Option<PathBuf>,
    pub compact: bool,
    pub no_warnings: bool,
}

fn synth_specs() -> Vec<FlagSpec<SynthArgs>> {
    vec![
        FlagSpec {
            names: &["--frontend"],
            takes_value: true,
            value_name: Some("<ts|py>"),
            help: "Source language; inferred from the file extension when omitted.",
            apply: |state, value| {
                state.frontend = Some(frontend_from_value(&value.unwrap())?);
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--out", "-o"],
            takes_value: true,
            value_name: Some("<hir.json>"),
            help: "Write HIR JSON here instead of stdout.",
            apply: |state, value| {
                state.out = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--compact"],
            takes_value: false,
            value_name: None,
            help: "Emit compact single-line HIR JSON.",
            apply: |state, _| {
                state.compact = true;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--no-warnings"],
            takes_value: false,
            value_name: None,
            help: "Suppress frontend warnings on stderr.",
            apply: |state, _| {
                state.no_warnings = true;
                Ok(FlagOutcome::Continue)
            },
        },
        help_spec(),
    ]
}

/// Parse `synth` arguments. Returns `Ok(None)` when `--help` was seen.
pub fn parse_synth(args: &[String]) -> Result<Option<SynthArgs>, Box<dyn std::error::Error>> {
    let mut state = SynthArgs::default();
    let specs = synth_specs();
    let help = parse_flags(args, &mut state, &specs, |state, value| {
        set_source(&mut state.source, value)
    })?;
    Ok(if help { None } else { Some(state) })
}

/// Parsed `dump-ir` arguments (`None` from `parse_dump_ir` means `--help`).
#[derive(Debug)]
pub struct DumpIrArgs {
    pub source: Option<PathBuf>,
    pub frontend: Option<Frontend>,
    pub json: bool,
    pub loops: usize,
    pub execute: bool,
    pub no_warnings: bool,
    pub explain_folds: bool,
}

impl Default for DumpIrArgs {
    fn default() -> Self {
        Self {
            source: None,
            frontend: None,
            json: false,
            loops: 1,
            execute: true,
            no_warnings: false,
            explain_folds: false,
        }
    }
}

fn dump_ir_specs() -> Vec<FlagSpec<DumpIrArgs>> {
    vec![
        FlagSpec {
            names: &["--frontend"],
            takes_value: true,
            value_name: Some("<ts|py>"),
            help: "Source language; inferred from the file extension when omitted.",
            apply: |state, value| {
                state.frontend = Some(frontend_from_value(&value.unwrap())?);
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--json"],
            takes_value: false,
            value_name: None,
            help: "Print the dump report as JSON.",
            apply: |state, _| {
                state.json = true;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--loops"],
            takes_value: true,
            value_name: Some("<N>"),
            help: "Execute N trigger loops when reporting executed cost (default 1).",
            apply: |state, value| {
                let loops = value
                    .unwrap()
                    .parse()
                    .map_err(|_| "dump-ir: --loops must be a positive integer")?;
                if loops == 0 {
                    return Err("dump-ir: --loops must be a positive integer".into());
                }
                state.loops = loops;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--no-exec"],
            takes_value: false,
            value_name: None,
            help: "Skip execution; report static cost only.",
            apply: |state, _| {
                state.execute = false;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--explain-folds"],
            takes_value: false,
            value_name: None,
            help: "Explain condition/gate folds in the report (default: cost only).",
            apply: |state, _| {
                state.explain_folds = true;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--no-warnings"],
            takes_value: false,
            value_name: None,
            help: "Suppress frontend warnings on stderr.",
            apply: |state, _| {
                state.no_warnings = true;
                Ok(FlagOutcome::Continue)
            },
        },
        help_spec(),
    ]
}

/// Parse `dump-ir` arguments. Returns `Ok(None)` when `--help` was seen.
pub fn parse_dump_ir(args: &[String]) -> Result<Option<DumpIrArgs>, Box<dyn std::error::Error>> {
    let mut state = DumpIrArgs::default();
    let specs = dump_ir_specs();
    let help = parse_flags(args, &mut state, &specs, |state, value| {
        set_source(&mut state.source, value)
    })?;
    Ok(if help { None } else { Some(state) })
}

/// Parsed `doctor` arguments (`None` from `parse_doctor` means `--help`).
#[derive(Debug, Default)]
pub struct DoctorArgs {
    pub json: bool,
}

fn doctor_specs() -> Vec<FlagSpec<DoctorArgs>> {
    vec![
        FlagSpec {
            names: &["--json"],
            takes_value: false,
            value_name: None,
            help: "Print the environment report as JSON.",
            apply: |state, _| {
                state.json = true;
                Ok(FlagOutcome::Continue)
            },
        },
        help_spec(),
    ]
}

/// Parse `doctor` arguments. Returns `Ok(None)` when `--help` was seen.
pub fn parse_doctor(args: &[String]) -> Result<Option<DoctorArgs>, Box<dyn std::error::Error>> {
    let mut state = DoctorArgs::default();
    let specs = doctor_specs();
    let help = parse_flags(args, &mut state, &specs, |_, value| {
        Err(format!("Unexpected argument '{value}'").into())
    })?;
    Ok(if help { None } else { Some(state) })
}

/// Parsed `init` arguments (`None` from `parse_init` means `--help`).
#[derive(Debug)]
pub struct InitArgs {
    pub dir: Option<PathBuf>,
    pub frontend: Frontend,
    ts: bool,
    py: bool,
}

impl Default for InitArgs {
    fn default() -> Self {
        Self {
            dir: None,
            frontend: Frontend::Ts,
            ts: false,
            py: false,
        }
    }
}

fn init_specs() -> Vec<FlagSpec<InitArgs>> {
    vec![
        FlagSpec {
            names: &["--ts"],
            takes_value: false,
            value_name: None,
            help: "Write a TypeScript starter (the default).",
            apply: |state, _| {
                state.ts = true;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--py"],
            takes_value: false,
            value_name: None,
            help: "Write a Python starter.",
            apply: |state, _| {
                state.py = true;
                Ok(FlagOutcome::Continue)
            },
        },
        help_spec(),
    ]
}

/// Parse `init` arguments. Returns `Ok(None)` when `--help` was seen.
pub fn parse_init(args: &[String]) -> Result<Option<InitArgs>, Box<dyn std::error::Error>> {
    let mut state = InitArgs::default();
    let specs = init_specs();
    let help = parse_flags(args, &mut state, &specs, |state, value| {
        set_source(&mut state.dir, value)
    })?;
    if help {
        return Ok(None);
    }
    if state.ts && state.py {
        return Err("init: choose either --ts or --py, not both".into());
    }
    // Default to TypeScript when neither language flag was given.
    state.frontend = if state.py { Frontend::Py } else { Frontend::Ts };
    Ok(Some(state))
}

/// Parsed `compile` arguments (`None` from `parse_compile` means `--help`).
#[derive(Debug)]
pub struct CompileArgs {
    pub source: Option<PathBuf>,
    pub basemap: Option<PathBuf>,
    pub out: Option<PathBuf>,
    pub plan_path: Option<PathBuf>,
    pub plan_out: Option<PathBuf>,
    pub out_dir: Option<PathBuf>,
    pub frontend: Option<Frontend>,
    pub skip_cost_gate: bool,
    pub enable_tick_gate: bool,
    pub no_warnings: bool,
}

impl Default for CompileArgs {
    fn default() -> Self {
        Self {
            source: None,
            basemap: None,
            out: None,
            plan_path: None,
            plan_out: None,
            out_dir: None,
            frontend: None,
            skip_cost_gate: false,
            enable_tick_gate: true,
            no_warnings: false,
        }
    }
}

fn compile_specs() -> Vec<FlagSpec<CompileArgs>> {
    vec![
        FlagSpec {
            names: &["--single-player", "--no-tick-gate"],
            takes_value: false,
            value_name: None,
            help: "Omit the once-per-tick payload gate (single-chain maps; ADR 0013).",
            apply: |state, _| {
                state.enable_tick_gate = false;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--basemap"],
            takes_value: true,
            value_name: Some("<basemap.scx>"),
            help: "Basemap .scx the output map is assembled from.",
            apply: |state, value| {
                state.basemap = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--out", "-o"],
            takes_value: true,
            value_name: Some("<out.scx>"),
            help: "Output .scx path (default: source path with the .scx extension).",
            apply: |state, value| {
                state.out = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--plan"],
            takes_value: true,
            value_name: Some("<plan.json>"),
            help: "Build from a frozen plan JSON instead of a source file.",
            apply: |state, value| {
                state.plan_path = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--plan-out", "--emit-plan"],
            takes_value: true,
            value_name: Some("<plan.json>"),
            help: "Write the frozen plan for this build to this path.",
            apply: |state, value| {
                state.plan_out = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--out-dir"],
            takes_value: true,
            value_name: Some("<dir>"),
            help: "Directory for maps built from --plan.",
            apply: |state, value| {
                state.out_dir = Some(PathBuf::from(value.unwrap()));
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--frontend"],
            takes_value: true,
            value_name: Some("<ts|py>"),
            help: "Source language; inferred from .ts/.py, required for HIR plans with --plan-out.",
            apply: |state, value| {
                state.frontend = Some(frontend_from_value(&value.unwrap())?);
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--check-cost"],
            takes_value: false,
            value_name: None,
            // Preserved quirk: --check-cost is a no-op (skip defaults
            // to false); it is accepted, not rejected, in every mode.
            help: "Accepted for compatibility (deprecated no-op; cost gate is on by default).",
            apply: |state, _| {
                state.skip_cost_gate = false;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--no-cost-gate"],
            takes_value: false,
            value_name: None,
            // Preserved quirk: accepted in single-source compile too,
            // where it is ignored (the gate only exists for plans).
            help: "Skip frozen-plan cost verification (plan builds only).",
            apply: |state, _| {
                state.skip_cost_gate = true;
                Ok(FlagOutcome::Continue)
            },
        },
        FlagSpec {
            names: &["--no-warnings"],
            takes_value: false,
            value_name: None,
            help: "Suppress frontend warnings on stderr.",
            apply: |state, _| {
                state.no_warnings = true;
                Ok(FlagOutcome::Continue)
            },
        },
        help_spec(),
    ]
}

/// Parse `compile` arguments. Returns `Ok(None)` when `--help` was seen.
pub fn parse_compile(args: &[String]) -> Result<Option<CompileArgs>, Box<dyn std::error::Error>> {
    let mut state = CompileArgs::default();
    let specs = compile_specs();
    let help = parse_flags(args, &mut state, &specs, |state, value| {
        set_source(&mut state.source, value)
    })?;
    Ok(if help { None } else { Some(state) })
}

/// Render one flag's canonical signature for help, e.g.
/// `--out, -o <out.scx>`.
fn flag_signature<S>(spec: &FlagSpec<S>) -> String {
    let mut signature = spec.names.join(", ");
    if let Some(value_name) = spec.value_name {
        signature.push(' ');
        signature.push_str(value_name);
    }
    signature
}

/// Append one command's generated flag list to the help text, aligning
/// each description after the widest flag signature.
fn push_command_flags<S>(out: &mut String, command: &str, specs: &[FlagSpec<S>]) {
    out.push_str(&format!("  {command}:\n"));
    let signatures: Vec<String> = specs.iter().map(flag_signature).collect();
    let width = signatures.iter().map(String::len).max().unwrap_or(0);
    for (signature, spec) in signatures.iter().zip(specs) {
        out.push_str(&format!("    {signature:<width$}  {}\n", spec.help));
    }
}

/// The two `compile` usage lines, shared by the global and focused help
/// so the two cannot drift.
fn compile_usage() -> &'static str {
    // NOTE: the usage lines carry no indentation, matching the old
    // hand-written text byte-for-byte: its line-continuation backslashes
    // stripped the source indentation.
    "eudkit compile <source.ts|source.py|module.hir.json> --basemap <basemap.scx> [--out <out.scx>] [--frontend ts|py] [--plan-out <plan.json>] [--single-player|--no-tick-gate] [--check-cost] [--no-warnings]\n\
     eudkit compile --plan <plan.json> [--basemap <override.scx>] [--out-dir <dir>] [--no-cost-gate] [--single-player|--no-tick-gate] [--no-warnings]\n"
}

/// Generate the full help text from the per-command spec tables.
///
/// The header, usage lines, and pipeline paragraph preserve the old
/// hand-written help verbatim; the Flags section is generated from
/// the specs, so a flag cannot exist without appearing in help. That
/// is the fix for the proven drift: `--frontend`, `--check-cost`, and
/// the `--emit-plan` alias were all accepted by the old parsers but
/// missing from the old help text.
pub fn help_text() -> String {
    let mut out = format!(
        // NOTE: the usage lines below carry no indentation, matching
        // the old hand-written text byte-for-byte: its line-continuation
        // backslashes stripped the source indentation.
        "eudkit unified TypeScript/Python compiler\n\n\
         Usage:\n\
         {}\
         eudkit dump-ir <source.ts|source.py|module.hir.json> [--frontend ts|py] [--json] [--loops N] [--no-exec] [--no-warnings] [--explain-folds]\n\
         eudkit synth <source.ts|source.py> [--frontend ts|py] [--out <hir.json>] [--compact] [--no-warnings]\n\
         eudkit doctor [--json]\n\
         eudkit init [<dir>] [--ts|--py]\n\
         eudkit --version\n\n\
         Flags:\n",
        compile_usage()
    );
    push_command_flags(&mut out, "compile", &compile_specs());
    push_command_flags(&mut out, "dump-ir", &dump_ir_specs());
    push_command_flags(&mut out, "synth", &synth_specs());
    push_command_flags(&mut out, "doctor", &doctor_specs());
    push_command_flags(&mut out, "init", &init_specs());
    out.push_str(
        "\nBoth DSLs share one typed pipeline: frontend capture -> HIR -> optimizer ->\n\
         storage plan -> LIR -> .scx payload. Plans embed a SHA-256 basemap fingerprint\n\
         that is verified before every bound build.\n\n\
         Source roots must name the compiled module \"module\".\n\
         TypeScript must export it: export const module = buildModule(...).\n\
         Python must assign it at module scope: module = build_module(...).\n\
         \n\
         The once-per-tick payload gate is on by default. --single-player / --no-tick-gate\n\
         omit it for maps with exactly one active chain, where the payload then runs once\n\
         per trigger loop (ADR 0013). There is no eudkit.toml: configuration is per\n\
         invocation.\n\
         \n\
         Frontend warnings are written to stderr; pass --no-warnings to suppress them.\n",
    );
    out
}

/// Generate focused help for one command: usage, that command's flags
/// with descriptions, and the notes that matter for it.
///
/// Unknown commands fall back to [`help_text`] so callers cannot lose
/// the global overview by asking for an unexpected name.
pub fn command_help_text(command: &str) -> String {
    let mut out = String::new();
    match command {
        "compile" => {
            out.push_str(&format!(
                "eudkit compile: build an .scx map from TypeScript, Python, or synthesized HIR\n\n\
                 Usage:\n\
                 {}\
                 \nFlags:\n",
                compile_usage()
            ));
            push_command_flags(&mut out, command, &compile_specs());
            out.push_str(
                "\nNotes:\n\
                 A .hir.json source is compiled directly and needs no --frontend.\n\
                 --plan-out requires --frontend ts|py or a .ts/.py source.\n\
                 Source roots must name the compiled module \"module\".\n\
                 TypeScript must export it: export const module = buildModule(...).\n\
                 Python must assign it at module scope: module = build_module(...).\n\
                 The once-per-tick payload gate is on by default; --single-player / --no-tick-gate\n\
                 omit it for maps with exactly one active chain (ADR 0013).\n\
                 Frontend warnings are written to stderr; pass --no-warnings to suppress them.\n",
            );
        }
        "dump-ir" => {
            out.push_str(
                "eudkit dump-ir: lower a source to HIR and report cost, storage, and packing\n\n\
                 Usage:\n\
                 eudkit dump-ir <source.ts|source.py|module.hir.json> [--frontend ts|py] [--json] [--loops <N>] [--no-exec] [--no-warnings] [--explain-folds]\n\n\
                 Flags:\n",
            );
            push_command_flags(&mut out, command, &dump_ir_specs());
            out.push_str(
                "\nNotes:\n\
                 Costs are reported before map assembly, so they exclude the tick gate.\n\
                 A .hir.json source is read directly and needs no --frontend.\n\
                 Frontend warnings are written to stderr; pass --no-warnings to suppress them.\n",
            );
        }
        "synth" => {
            out.push_str(
                "eudkit synth: capture validated HIR JSON from TypeScript or Python\n\n\
                 Usage:\n\
                 eudkit synth <source.ts|source.py> [--frontend ts|py] [--out <hir.json>] [--compact] [--no-warnings]\n\n\
                 Flags:\n",
            );
            push_command_flags(&mut out, command, &synth_specs());
            out.push_str(
                "\nNotes:\n\
                 HIR JSON is printed to stdout unless --out is given.\n\
                 Source roots must name the compiled module \"module\".\n\
                 TypeScript must export it: export const module = buildModule(...).\n\
                 Python must assign it at module scope: module = build_module(...).\n\
                 Frontend warnings are written to stderr; pass --no-warnings to suppress them.\n",
            );
        }
        "doctor" => {
            out.push_str(
                "eudkit doctor: check the DSL runtime, toolchain, and installed packages\n\n\
                 Usage:\n\
                 eudkit doctor [--json]\n\n\
                 Flags:\n",
            );
            push_command_flags(&mut out, command, &doctor_specs());
            out.push_str(
                "\nNotes:\n\
                 Checks EUDKIT_ROOT, the resolved checkout root, node (>= 22.6.0),\n\
                 python3 (>= 3.11), and installed eudkit packages.\n\
                 Exits successfully after printing the report; read Status for health.\n",
            );
        }
        "init" => {
            out.push_str(
                "eudkit init: write a minimal compilable starter and its basemap\n\n\
                 Usage:\n\
                 eudkit init [<dir>] [--ts|--py]\n\n\
                 Flags:\n",
            );
            push_command_flags(&mut out, command, &init_specs());
            out.push_str(
                "\nNotes:\n\
                 Creates <dir> (default .), writes basemap.scx and main.ts or main.py\n\
                 (TypeScript by default), and never overwrites an existing starter.\n\
                 Inside a checkout the starter imports the local ts/ or py/ sources;\n\
                 outside it imports the installed eudkit package.\n",
            );
        }
        _ => return help_text(),
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    fn assert_help_lists_specs<S>(help: &str, command: &str, specs: &[FlagSpec<S>]) {
        for spec in specs {
            for name in spec.names {
                assert!(
                    help.contains(name),
                    "generated help must list {name} ({command})"
                );
            }
        }
    }

    #[test]
    fn synth_spec_accepts_its_flags_and_alias() {
        let parsed = parse_synth(&args(&[
            "demo.ts",
            "--frontend",
            "ts",
            "-o",
            "out.json",
            "--compact",
            "--no-warnings",
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(parsed.source, Some(PathBuf::from("demo.ts")));
        assert_eq!(parsed.frontend, Some(Frontend::Ts));
        assert_eq!(parsed.out, Some(PathBuf::from("out.json")));
        assert!(parsed.compact);
        assert!(parsed.no_warnings);

        let parsed = parse_synth(&args(&["demo.py", "--frontend", "py", "--out", "o.json"]))
            .unwrap()
            .unwrap();
        assert_eq!(parsed.frontend, Some(Frontend::Py));
        assert_eq!(parsed.out, Some(PathBuf::from("o.json")));
        assert!(!parsed.compact);
    }

    #[test]
    fn synth_spec_rejects_other_commands_flags() {
        for flag in ["--basemap", "--json", "--plan", "--no-exec", "--loops"] {
            let err = parse_synth(&args(&["demo.ts", flag])).unwrap_err();
            assert_eq!(
                err.to_string(),
                format!("Unknown option '{flag}'"),
                "spec boundary is the test: synth must not accept {flag}"
            );
        }
    }

    #[test]
    fn synth_spec_rejects_unknown_frontend_and_reports_missing_value() {
        let err = parse_synth(&args(&["demo.ts", "--frontend", "xx"])).unwrap_err();
        assert_eq!(err.to_string(), "Unknown frontend 'xx' (use ts|py)");

        // A dash-leading token is consumed as the value, as before.
        let err = parse_synth(&args(&["demo.ts", "--frontend", "--compact"])).unwrap_err();
        assert_eq!(err.to_string(), "Unknown frontend '--compact' (use ts|py)");

        let err = parse_synth(&args(&["demo.ts", "-o"])).unwrap_err();
        assert_eq!(err.to_string(), "--out requires a value");
    }

    #[test]
    fn synth_spec_enforces_exactly_one_positional_and_help_short_circuit() {
        let err = parse_synth(&args(&["a.ts", "b.ts"])).unwrap_err();
        assert_eq!(err.to_string(), "Unexpected argument 'b.ts'");

        // Arguments after --help are never parsed.
        assert!(
            parse_synth(&args(&["--help", "--bogus"]))
                .unwrap()
                .is_none()
        );
        assert!(parse_synth(&args(&["demo.ts", "-h"])).unwrap().is_none());
    }

    #[test]
    fn dump_ir_spec_accepts_its_flags_with_defaults() {
        let parsed = parse_dump_ir(&args(&[
            "demo.ts",
            "--frontend",
            "ts",
            "--json",
            "--loops",
            "3",
            "--no-exec",
            "--no-warnings",
            "--explain-folds",
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(parsed.source, Some(PathBuf::from("demo.ts")));
        assert_eq!(parsed.frontend, Some(Frontend::Ts));
        assert!(parsed.json);
        assert_eq!(parsed.loops, 3);
        assert!(!parsed.execute);
        assert!(parsed.no_warnings);
        assert!(parsed.explain_folds);

        let parsed = parse_dump_ir(&args(&["demo.py"])).unwrap().unwrap();
        assert!(!parsed.json);
        assert_eq!(parsed.loops, 1);
        assert!(parsed.execute);
        assert!(!parsed.no_warnings);
        assert!(!parsed.explain_folds);
    }

    #[test]
    fn compile_spec_accepts_its_flags_and_both_plan_out_spellings() {
        let parsed = parse_compile(&args(&[
            "demo.ts",
            "--basemap",
            "base.scx",
            "--out",
            "out.scx",
            "--plan-out",
            "plan.json",
            "--frontend",
            "ts",
            "--no-warnings",
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(parsed.source, Some(PathBuf::from("demo.ts")));
        assert_eq!(parsed.basemap, Some(PathBuf::from("base.scx")));
        assert_eq!(parsed.out, Some(PathBuf::from("out.scx")));
        assert_eq!(parsed.plan_out, Some(PathBuf::from("plan.json")));
        assert_eq!(parsed.frontend, Some(Frontend::Ts));
        assert!(!parsed.skip_cost_gate);
        assert!(parsed.enable_tick_gate);
        assert!(parsed.no_warnings);

        // The --emit-plan alias writes the same slot.
        let parsed = parse_compile(&args(&["demo.ts", "--emit-plan", "p.json"]))
            .unwrap()
            .unwrap();
        assert_eq!(parsed.plan_out, Some(PathBuf::from("p.json")));

        // --check-cost is a no-op; --no-cost-gate sets the skip flag.
        let parsed = parse_compile(&args(&["demo.ts", "--check-cost"]))
            .unwrap()
            .unwrap();
        assert!(!parsed.skip_cost_gate);
        let parsed = parse_compile(&args(&["demo.ts", "--no-cost-gate"]))
            .unwrap()
            .unwrap();
        assert!(parsed.skip_cost_gate);

        // --single-player and --no-tick-gate both disable the once-per-tick gate.
        let parsed = parse_compile(&args(&["demo.ts", "--single-player"]))
            .unwrap()
            .unwrap();
        assert!(!parsed.enable_tick_gate);
        let parsed = parse_compile(&args(&["demo.ts", "--no-tick-gate"]))
            .unwrap()
            .unwrap();
        assert!(!parsed.enable_tick_gate);
    }

    #[test]
    fn compile_spec_rejects_other_commands_flags_and_reports_canonical_missing_value() {
        for flag in ["--compact", "--json", "--loops", "--no-exec"] {
            let err = parse_compile(&args(&["demo.ts", flag])).unwrap_err();
            assert_eq!(
                err.to_string(),
                format!("Unknown option '{flag}'"),
                "spec boundary is the test: compile must not accept {flag}"
            );
        }
        let err = parse_compile(&args(&["demo.ts", "-o"])).unwrap_err();
        assert_eq!(err.to_string(), "--out requires a value");
        let err = parse_compile(&args(&["demo.ts", "--emit-plan"])).unwrap_err();
        assert_eq!(err.to_string(), "--plan-out requires a value");
        let err = parse_compile(&args(&["demo.ts", "--frontend", "xx"])).unwrap_err();
        assert_eq!(err.to_string(), "Unknown frontend 'xx' (use ts|py)");
        let err = parse_compile(&args(&["a.ts", "b.ts"])).unwrap_err();
        assert_eq!(err.to_string(), "Unexpected argument 'b.ts'");
    }

    #[test]
    fn every_spec_has_a_crisp_one_line_description() {
        fn assert_described<S>(command: &str, specs: &[FlagSpec<S>]) {
            for spec in specs {
                assert!(
                    !spec.help.is_empty(),
                    "{command} {} must have a description",
                    spec.names[0]
                );
                assert!(
                    !spec.help.contains('\n'),
                    "{command} {} description must be one line",
                    spec.names[0]
                );
            }
        }
        assert_described("compile", &compile_specs());
        assert_described("dump-ir", &dump_ir_specs());
        assert_described("synth", &synth_specs());

        let compile = compile_specs();
        let check_cost = compile
            .iter()
            .find(|spec| spec.names.contains(&"--check-cost"))
            .expect("--check-cost spec");
        assert!(check_cost.help.contains("deprecated no-op"));
        assert!(check_cost.help.contains("cost gate is on by default"));
        let no_cost_gate = compile
            .iter()
            .find(|spec| spec.names.contains(&"--no-cost-gate"))
            .expect("--no-cost-gate spec");
        assert!(no_cost_gate.help.contains("plan builds only"));
    }

    #[test]
    fn help_text_renders_every_spec_description() {
        let help = help_text();
        for spec in compile_specs() {
            assert!(help.contains(spec.help), "compile help: {}", spec.names[0]);
        }
        for spec in dump_ir_specs() {
            assert!(help.contains(spec.help), "dump-ir help: {}", spec.names[0]);
        }
        for spec in synth_specs() {
            assert!(help.contains(spec.help), "synth help: {}", spec.names[0]);
        }
    }

    #[test]
    fn command_help_text_is_focused_per_command() {
        let compile = command_help_text("compile");
        assert!(
            compile.contains("eudkit compile <source.ts|source.py|module.hir.json>"),
            "{compile}"
        );
        assert!(compile.contains("--basemap"), "{compile}");
        assert!(compile.contains("--plan-out"), "{compile}");
        assert!(
            compile.contains("deprecated no-op; cost gate is on by default"),
            "{compile}"
        );
        assert!(compile.contains("plan builds only"), "{compile}");
        assert!(
            compile.contains("Source roots must name the compiled module \"module\""),
            "{compile}"
        );
        assert!(!compile.contains("eudkit dump-ir <"), "{compile}");
        assert!(!compile.contains("eudkit synth <"), "{compile}");

        let dump_ir = command_help_text("dump-ir");
        assert!(
            dump_ir.contains("eudkit dump-ir <source.ts|source.py|module.hir.json>"),
            "{dump_ir}"
        );
        assert!(dump_ir.contains("--loops"), "{dump_ir}");
        assert!(dump_ir.contains("--no-exec"), "{dump_ir}");
        assert!(dump_ir.contains("--explain-folds"), "{dump_ir}");
        assert!(
            dump_ir.contains("Explain condition/gate folds in the report (default: cost only)."),
            "{dump_ir}"
        );
        assert!(!dump_ir.contains("--basemap"), "{dump_ir}");
        assert!(!dump_ir.contains("eudkit compile <"), "{dump_ir}");

        let synth = command_help_text("synth");
        assert!(
            synth.contains("eudkit synth <source.ts|source.py>"),
            "{synth}"
        );
        assert!(synth.contains("--compact"), "{synth}");
        assert!(synth.contains("--out <hir.json>"), "{synth}");
        assert!(!synth.contains("--basemap"), "{synth}");
        assert!(!synth.contains("--loops"), "{synth}");
        assert!(!synth.contains("eudkit compile <"), "{synth}");
    }

    #[test]
    fn help_text_compile_usage_lists_hir_json_sources_and_flags() {
        let help = help_text();
        assert!(
            help.contains("eudkit compile <source.ts|source.py|module.hir.json>"),
            "{help}"
        );
        for flag in [
            "--basemap",
            "--out",
            "--plan",
            "--plan-out",
            "--out-dir",
            "--frontend",
            "--check-cost",
            "--no-cost-gate",
            "--single-player",
            "--no-tick-gate",
            "--no-warnings",
        ] {
            assert!(
                help.contains(flag),
                "compile usage must name {flag}: {help}"
            );
        }
    }

    #[test]
    fn help_text_contains_every_spec_flag() {
        let help = help_text();
        assert_help_lists_specs(&help, "compile", &compile_specs());
        assert_help_lists_specs(&help, "dump-ir", &dump_ir_specs());
        assert_help_lists_specs(&help, "synth", &synth_specs());
        // Regression for the proven drift: these were accepted by the
        // old parsers but missing from the old help text.
        for name in ["--frontend", "--check-cost", "--emit-plan"] {
            assert!(
                help.contains(name),
                "generated help must list drifted {name}"
            );
        }
        // Existing lines preserved verbatim.
        assert!(help.contains("eudkit compile --plan <plan.json>"));
        assert!(help.contains("eudkit --version"));
        assert!(help.contains("Both DSLs share one typed pipeline"));
        // The optional tick-gate contract and the unsupported config file
        // are documented in the generated help (ADR 0013).
        assert!(help.contains("--single-player / --no-tick-gate"));
        assert!(help.contains("There is no eudkit.toml"));
        assert!(help.contains("Frontend warnings are written to stderr"));
        // The explicit-entry contract must be documented: source roots
        // provide the compiled module under the name `module`, with the
        // language-specific mechanics named precisely.
        assert!(
            help.contains("Source roots must name the compiled module \"module\""),
            "help must document the root-source entry requirement"
        );
        assert!(
            help.contains("TypeScript must export it"),
            "help must name the TypeScript export requirement"
        );
        assert!(
            help.contains("Python must assign it at module scope"),
            "help must name the Python module-scope assignment requirement"
        );
        assert!(help.contains("buildModule(...)"));
        assert!(help.contains("build_module(...)"));
    }

    #[test]
    fn doctor_spec_accepts_json_and_rejects_positionals() {
        let parsed = parse_doctor(&args(&["--json"])).unwrap().unwrap();
        assert!(parsed.json);
        assert!(
            parse_doctor(&args(&["--help", "--bogus"]))
                .unwrap()
                .is_none()
        );

        let err = parse_doctor(&args(&["unexpected"])).unwrap_err();
        assert_eq!(err.to_string(), "Unexpected argument 'unexpected'");

        let err = parse_doctor(&args(&["--bogus"])).unwrap_err();
        assert_eq!(err.to_string(), "Unknown option '--bogus'");
    }

    #[test]
    fn init_spec_defaults_to_ts_and_accepts_dir_and_py() {
        let parsed = parse_init(&args(&[])).unwrap().unwrap();
        assert_eq!(parsed.dir, None);
        assert_eq!(parsed.frontend, Frontend::Ts);

        let parsed = parse_init(&args(&["demo"])).unwrap().unwrap();
        assert_eq!(parsed.dir, Some(PathBuf::from("demo")));
        assert_eq!(parsed.frontend, Frontend::Ts);

        let parsed = parse_init(&args(&["demo", "--py"])).unwrap().unwrap();
        assert_eq!(parsed.frontend, Frontend::Py);

        let parsed = parse_init(&args(&["--ts", "demo"])).unwrap().unwrap();
        assert_eq!(parsed.frontend, Frontend::Ts);
    }

    #[test]
    fn init_spec_rejects_both_languages_and_extra_positionals() {
        let err = parse_init(&args(&["--ts", "--py"])).unwrap_err();
        assert_eq!(
            err.to_string(),
            "init: choose either --ts or --py, not both"
        );

        let err = parse_init(&args(&["a", "b"])).unwrap_err();
        assert_eq!(err.to_string(), "Unexpected argument 'b'");

        assert!(parse_init(&args(&["--help", "--py"])).unwrap().is_none());
    }

    #[test]
    fn help_text_lists_doctor_and_init_specs() {
        let help = help_text();
        assert_help_lists_specs(&help, "doctor", &doctor_specs());
        assert_help_lists_specs(&help, "init", &init_specs());
        assert!(help.contains("eudkit doctor [--json]"), "{help}");
        assert!(help.contains("eudkit init [<dir>] [--ts|--py]"), "{help}");
        assert!(command_help_text("doctor").contains("Usage:"));
        assert!(command_help_text("init").contains("never overwrites"));
    }

    #[test]
    fn dump_ir_spec_rejects_bad_loops_and_other_commands_flags() {
        for value in ["0", "abc", "-1"] {
            let err = parse_dump_ir(&args(&["demo.ts", "--loops", value])).unwrap_err();
            assert_eq!(
                err.to_string(),
                "dump-ir: --loops must be a positive integer",
                "--loops {value} must fail with the historical message"
            );
        }
        let err = parse_dump_ir(&args(&["demo.ts", "--loops"])).unwrap_err();
        assert_eq!(err.to_string(), "--loops requires a value");

        for flag in ["--out", "--compact", "--basemap", "--plan"] {
            let err = parse_dump_ir(&args(&["demo.ts", flag])).unwrap_err();
            assert_eq!(
                err.to_string(),
                format!("Unknown option '{flag}'"),
                "spec boundary is the test: dump-ir must not accept {flag}"
            );
        }
    }
}
