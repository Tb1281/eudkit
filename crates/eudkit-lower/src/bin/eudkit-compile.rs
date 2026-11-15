//! CLI bridge for `eudkit-lower`:
//! - `eudkit-compile lower <hir.json>` -> prints LIR JSON to stdout
//! - `eudkit-compile sim <hir.json>` -> lowers, simulates 1 trigger loop, and prints cost + var values JSON
//! - `eudkit-compile map <hir.json> <basemap.scx> <out.scx> [--single-player|--no-tick-gate]`
//!   -> writes `.scx` and prints `StaticCost` JSON to stdout

use std::env;
use std::fs;
use std::path::Path;
use std::process;

use eudkit_hir::hir_from_json;
use eudkit_lir::dump::render_lir;
use eudkit_lir::{
    ACT_COUNT, EUDX_MAGIC, LirItem, VALUE_FIELD_OFFSET, act_field_byte_offset, epd_u32,
    simulate_trigger_loop, static_cost,
};
use eudkit_lower::{AssemblyOptions, prepare_lowering};
use eudkit_opt::cell::{StorageClass, bit_value};
use serde::Serialize;

#[derive(Serialize)]
struct SimReport {
    static_cost: eudkit_lir::StaticCost,
    executed_cost: eudkit_lir::ExecutedCost,
    /// Simulation lowers and runs the program but never assembles a map, so
    /// its costs exclude the once-per-tick gate that map assembly appends.
    pre_assembly: bool,
    var_values: Vec<u32>,
    self_targeted_invariant_holds: bool,
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    external_epds: std::collections::BTreeMap<u32, u32>,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!(
            "Usage:\n  {} lower <hir.json>\n  {} sim <hir.json> [loops]\n  {} dump-ir <hir.json> [--explain-folds]\n  {} map <hir.json> <basemap.scx> <out.scx> [--single-player|--no-tick-gate]",
            args[0], args[0], args[0], args[0]
        );
        process::exit(2);
    }

    let cmd = args[1].as_str();
    let hir_path = &args[2];
    let hir_json = match fs::read_to_string(hir_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Failed to read {hir_path}: {e}");
            process::exit(1);
        }
    };

    match cmd {
        "lower" => match eudkit_lower::lower_hir_json(&hir_json) {
            Ok(lir_json) => {
                println!("{lir_json}");
            }
            Err(e) => {
                eprintln!("{e}");
                process::exit(1);
            }
        },
        "dump-ir" => {
            let mut explain_folds = false;
            for arg in &args[3..] {
                match arg.as_str() {
                    "--explain-folds" => explain_folds = true,
                    other => {
                        eprintln!("Unknown option '{other}'");
                        process::exit(2);
                    }
                }
            }
            let doc = match hir_from_json(&hir_json) {
                Ok(d) => d,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            };
            let folds = if explain_folds {
                match eudkit_opt::optimize_document_with_folds(&doc) {
                    Ok((_, log)) => Some(log),
                    Err(e) => {
                        eprintln!("{e}");
                        process::exit(1);
                    }
                }
            } else {
                None
            };
            let prepared = match prepare_lowering(&doc, None) {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            };
            print!(
                "{}",
                eudkit_opt::cell::render_dump_ir(&prepared.optimized, &prepared.storage)
            );
            if let Some(log) = &folds {
                print!("{}", eudkit_opt::folds::render_folds(log));
            }
            match render_lir(&prepared.lir) {
                Ok(dump) => println!("\n{dump}"),
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            }
        }
        "sim" => {
            let loops: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(1);
            let doc = match hir_from_json(&hir_json) {
                Ok(d) => d,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            };
            let prepared = match prepare_lowering(&doc, None) {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            };
            let plan = &prepared.storage;
            let lir = &prepared.lir;
            let scost = static_cost(lir);
            let mut sim = match simulate_trigger_loop(lir) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            };
            for _ in 1..loops {
                sim.step_loop();
            }
            let num_pages = plan.trigger_pages;
            // Payload layout (see `lower_document`): executable triggers, then
            // the TriggerPages, then the optional demoted Cell blob, then the
            // HIR blobs. Blob items are therefore all trailing, and the cell
            // blob (when present) sits directly after the pages.
            let blob_items = lir
                .module
                .items
                .iter()
                .filter(|item| matches!(item, LirItem::Blob(_)))
                .count();
            let num_exec = lir
                .module
                .items
                .len()
                .saturating_sub(num_pages)
                .saturating_sub(blob_items);
            let cell_item = num_exec + num_pages;
            let mut var_values = Vec::with_capacity(doc.module.vars.len());
            for v in &doc.module.vars {
                // Read-back goes through the plan accessor: a variable
                // with no allocation is a loud error, never a silent
                // fallback to TriggerPage(0, 0).
                let class = match plan.storage(v.id) {
                    Some(class) => class,
                    None => {
                        eprintln!(
                            "variable {} has no allocation in the storage plan",
                            v.id
                        );
                        process::exit(1);
                    }
                };
                let value = match class {
                    StorageClass::TriggerPage {
                        page_index,
                        slot_index,
                    } => sim.read_page_slot_value(num_exec + page_index, slot_index),
                    StorageClass::DwordCell { dword_index } => {
                        let addr = sim.payload.addrs[cell_item] + (dword_index * 4) as u32;
                        sim.read_epd(epd_u32(addr))
                    }
                    StorageClass::BitCell {
                        dword_index,
                        bit_index,
                    } => {
                        let addr = sim.payload.addrs[cell_item] + (dword_index * 4) as u32;
                        bit_value(bit_index, sim.read_epd(epd_u32(addr)))
                    }
                };
                var_values.push(value);
            }
            let mut invariant_holds = true;
            for p in 0..num_pages {
                let page_item = num_exec + p;
                let raw = sim.raw_trigger(page_item);
                let page_base = sim.payload.addrs[page_item];
                for k in 0..ACT_COUNT {
                    let expected_dest =
                        epd_u32(page_base + act_field_byte_offset(k, VALUE_FIELD_OFFSET) as u32);
                    let a = &raw.acts[k];
                    if a.dest != expected_dest
                        || a.modifier != 7
                        || a.acttype != 45
                        || a.eudx != EUDX_MAGIC
                    {
                        invariant_holds = false;
                        break;
                    }
                }
            }
            let mut external_epds_sorted = std::collections::BTreeMap::new();
            for (k, v) in sim.external_epds {
                external_epds_sorted.insert(k, v);
            }
            let report = SimReport {
                static_cost: scost,
                executed_cost: sim.last_executed_cost,
                pre_assembly: true,
                var_values,
                self_targeted_invariant_holds: invariant_holds,
                external_epds: external_epds_sorted,
            };
            println!("{}", serde_json::to_string(&report).unwrap());
        }
        "map" => {
            if args.len() < 5 {
                eprintln!(
                    "Usage: {} map <hir.json> <basemap.scx> <out.scx> [--single-player|--no-tick-gate]",
                    args[0]
                );
                process::exit(2);
            }
            let basemap = Path::new(&args[3]);
            let out_scx = Path::new(&args[4]);
            let options = match parse_map_options(&args[5..]) {
                Ok(options) => options,
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(2);
                }
            };
            match eudkit_lower::compile_hir_to_map_with_options(
                &hir_json, basemap, out_scx, options,
            ) {
                Ok(cost) => {
                    println!("{}", serde_json::to_string(&cost).unwrap());
                }
                Err(e) => {
                    eprintln!("{e}");
                    process::exit(1);
                }
            }
        }
        other => {
            eprintln!("Unknown command: {other}");
            process::exit(2);
        }
    }
}

/// Parse the flags accepted after `map <hir.json> <basemap.scx> <out.scx>`.
///
/// This is intentionally separate from the `eudkit` CLI's argument parser:
/// `eudkit-compile` lives in `eudkit-lower`, which cannot depend on the
/// `eudkit` crate (that would invert the dependency). Both spellings are
/// pinned by `tests/map_bridge.rs` so the boundary cannot drift silently.
fn parse_map_options(extra: &[String]) -> Result<AssemblyOptions, String> {
    let mut options = AssemblyOptions::default();
    for arg in extra {
        match arg.as_str() {
            "--single-player" | "--no-tick-gate" => options.enable_tick_gate = false,
            other => return Err(format!("Unknown option '{other}'")),
        }
    }
    Ok(options)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    #[test]
    fn map_options_default_to_the_gate_and_accept_both_opt_out_spellings() {
        assert!(parse_map_options(&[]).unwrap().enable_tick_gate);
        assert!(!parse_map_options(&args(&["--single-player"]))
            .unwrap()
            .enable_tick_gate);
        assert!(
            !parse_map_options(&args(&["--no-tick-gate"]))
                .unwrap()
                .enable_tick_gate
        );
    }

    #[test]
    fn map_options_reject_unknown_flags() {
        let err = parse_map_options(&args(&["--bogus"])).unwrap_err();
        assert_eq!(err, "Unknown option '--bogus'");
    }
}
