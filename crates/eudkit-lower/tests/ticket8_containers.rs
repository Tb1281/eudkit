//! Ticket #8: `UnitGroup.autoPrune` keeps live members and prunes dead ones.
//!
//! The CLI simulator has no external memory, so this test sets the unit sprite
//! EPDs directly through [`eudkit_lir::SimulationState::write_epd`].

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::init_simulation;
use eudkit_lower::lower_document;
use eudkit_opt::cell::{StorageClass, analyze_storage};
use eudkit_opt::optimize_module;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

#[test]
fn unit_group_auto_prune_keeps_live_members_and_prunes_dead_ones() {
    let ts_script = r#"
import { UnitGroup, Var, buildModule, when } from "./ts/index.ts";

let ids = {};
const mod = buildModule(() => {
  const g = new UnitGroup(2);
  const phase = Var(0);
  when([phase.eq(0)], () => {
    g.add(1000);
    g.add(2000);
  });
  phase.iadd(1);
  ids = { length: g.length.id, slot0: g.slots[0].id, slot1: g.slots[1].id };
});
process.stdout.write(JSON.stringify({ ids, hir: JSON.parse(mod.toJson()) }));
"#;

    let out = Command::new("node")
        .args(["--input-type=module", "-e", ts_script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the UnitGroup snippet");
    assert!(
        out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let parsed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let var_id = |name: &str| -> usize { parsed["ids"][name].as_u64().unwrap() as usize };
    let hir_json = parsed["hir"].to_string();

    let doc = hir_from_json(&hir_json).unwrap();
    // Mirror the plan that `lower_document` computes internally so page slots
    // can be read back by variable id.
    let opt = optimize_module(&doc.module).unwrap();
    let plan = analyze_storage(&opt);
    let lir = lower_document(&doc).unwrap();

    let num_pages = plan.trigger_pages;
    let num_exec = lir.module.items.len() - num_pages - usize::from(plan.cell_dwords > 0);
    let read_var = |sim: &eudkit_lir::SimulationState, id: usize| -> u32 {
        match plan
            .storage(id)
            .expect("every declared variable must have a storage class")
        {
            StorageClass::TriggerPage {
                page_index,
                slot_index,
            } => sim.read_page_slot_value(num_exec + page_index, slot_index),
            StorageClass::DwordCell { dword_index } => {
                let addr = sim.payload.addrs[num_exec + num_pages] + (dword_index * 4) as u32;
                sim.read_epd(eudkit_lir::epd_u32(addr))
            }
            StorageClass::BitCell {
                dword_index,
                bit_index,
            } => {
                let addr = sim.payload.addrs[num_exec + num_pages] + (dword_index * 4) as u32;
                (sim.read_epd(eudkit_lir::epd_u32(addr)) >> bit_index) & 1
            }
        }
    };

    let mut sim = init_simulation(&lir).unwrap();
    // Unit 1000 is alive: its vanilla sprite field lives at EPD 1000 + 3.
    sim.write_epd(1003, 1);
    // Unit 2000 is dead: EPD 2003 defaults to zero.
    sim.step_loop(); // phase 0: add both members
    sim.step_loop(); // phase 1: autoPrune sweep runs first

    assert_eq!(read_var(&sim, var_id("slot0")), 1000, "live member was pruned");
    assert_eq!(read_var(&sim, var_id("slot1")), 0, "dead member survived");
    assert_eq!(read_var(&sim, var_id("length")), 1);

    // The surviving member is still there on the next frame.
    sim.step_loop();
    assert_eq!(read_var(&sim, var_id("slot0")), 1000);
    assert_eq!(read_var(&sim, var_id("length")), 1);
}
