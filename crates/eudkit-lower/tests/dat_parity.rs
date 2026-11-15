//! Cross-language DAT accessor parity.
//!
//! Both DSLs must resolve the same schema names, fold the same absolute
//! addresses into EPD/byte-lane writes, and emit byte-identical HIR. A shared
//! parity script covers reads/writes across the units, weapons, and tech
//! tables plus an ambiguous-alias-free symbol lookup.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{RawTrigger, lir2payload, static_cost};
use eudkit_lower::{compile_hir_to_map, lower_document};

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn run_node(script: &str) -> String {
    let out = Command::new("node")
        .args(["--input-type=module", "-e", script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the DAT parity script");
    assert!(
        out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn run_python(script: &str) -> String {
    let out = Command::new("python3")
        .args(["-c", script])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run the DAT parity script");
    assert!(
        out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn ts_and_py_dat_accessors_emit_byte_identical_hir_and_addresses() {
    let ts_script = r#"
import {
  DisplayText, TechType, Trigger, UnitType, WeaponType,
  buildModule, techDat, unitsDat, weaponsDat,
} from "./ts/index.ts";

const mod = buildModule(() => {
  unitsDat[UnitType.TerranMarine].maxHp.set(40);
  unitsDat.at(1).armor.set(1);
  weaponsDat[WeaponType.GaussRifle].damage.set(6);
  techDat[TechType.StimPacks].mineralCost.iadd(25);

  new Trigger({
    conds: [
      unitsDat[UnitType.TerranMarine].maxHp.ge(40),
      weaponsDat[WeaponType.GaussRifle].damage.eq(6),
    ],
    acts: [
      unitsDat[UnitType.TerranMarine].maxShield.act_set(100),
      DisplayText("DAT parity!"),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;

    let py_script = r#"
import sys
from py import (
    DisplayText, TechType, Trigger, UnitType, WeaponType,
    build_module, tech_dat, units_dat, weapons_dat,
)

def prog():
    units_dat[UnitType.TerranMarine].maxHp.set(40)
    units_dat.at(1).armor.set(1)
    weapons_dat[WeaponType.GaussRifle].damage.set(6)
    tech_dat[TechType.StimPacks].mineralCost.iadd(25)

    Trigger(
        conds=[
            units_dat[UnitType.TerranMarine].maxHp.ge(40),
            weapons_dat[WeaponType.GaussRifle].damage.eq(6),
        ],
        acts=[
            units_dat[UnitType.TerranMarine].maxShield.act_set(100),
            DisplayText("DAT parity!"),
        ],
        preserved=True,
    )

sys.stdout.write(build_module(prog).to_json())
"#;

    let ts_hir_json = run_node(ts_script);
    let py_hir_json = run_python(py_script);
    assert_eq!(ts_hir_json, py_hir_json, "TS and Python DAT HIR diverged");

    let hir_doc = hir_from_json(&ts_hir_json).unwrap();
    let lir_doc = lower_document(&hir_doc).unwrap();
    let cost = static_cost(&lir_doc);
    assert_eq!(cost.triggers, 2);
    assert_eq!(cost.conditions, 2);
    assert_eq!(cost.actions, 6);

    let payload = lir2payload(&lir_doc).unwrap();
    assert_eq!(payload.data.len(), 2 * 2408);

    let top = RawTrigger::decode(payload.data[0..2408].try_into().unwrap());
    assert_eq!(top.acts[0].acttype, 45);
    assert_eq!(top.acts[0].modifier, 7); // SetTo
    assert_eq!(top.acts[0].dest, 221_179); // Marine maxHp
    assert_eq!(top.acts[0].value, 40);
    assert_eq!(top.acts[0].mask, 0xffff_ffff);

    // Entry 1 armor shares entry 0's EPD at byte lane 1.
    assert_eq!(top.acts[1].dest, 218_841);
    assert_eq!(top.acts[1].value, 0x100);
    assert_eq!(top.acts[1].mask, 0x0000_ff00);

    assert_eq!(top.acts[2].dest, 209_619); // Gauss Rifle damage
    assert_eq!(top.acts[2].value, 6);
    assert_eq!(top.acts[2].mask, 0x0000_ffff);

    assert_eq!(top.acts[3].dest, 208_825); // Stim Packs mineralCost
    assert_eq!(top.acts[3].modifier, 8); // Add
    assert_eq!(top.acts[3].value, 25);
    assert_eq!(top.acts[3].mask, 0x0000_ffff);

    let conditionals = RawTrigger::decode(payload.data[2408..4816].try_into().unwrap());
    assert_eq!(conditionals.conds[0].condtype, 15); // DeathsX
    assert_eq!(conditionals.conds[0].player, 221_179);
    assert_eq!(conditionals.conds[0].amount, 40);
    assert_eq!(conditionals.conds[1].player, 209_619);
    assert_eq!(conditionals.conds[1].amount, 6);

    assert_eq!(conditionals.acts[0].dest, 219_815); // Marine maxShield
    assert_eq!(conditionals.acts[0].value, 100);
    assert_eq!(conditionals.acts[0].mask, 0x0000_ffff);

    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("dat_parity.scx");
    let map_cost = compile_hir_to_map(&ts_hir_json, &basemap, &out_scx).unwrap();
    assert_eq!(
        map_cost.triggers,
        cost.triggers + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
    );
    assert!(out_scx.is_file());
}
