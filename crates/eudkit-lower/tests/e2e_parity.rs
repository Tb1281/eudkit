//! End-to-end cross-language (`ts/` + `py/` -> `eudkit-hir` -> `eudkit-opt` ->
//! `eudkit-lower` -> `eudkit-lir` -> `eudkit-map`) integration tests for Ticket #1.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{
    LirDocument, RAW_TRIGGER_BYTE_SIZE, RawTrigger, StaticCost, epd_u32, lir2payload, static_cost,
};
use eudkit_lower::{compile_hir_to_map, lower_document};

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

/// The written map includes the 35-trigger once-per-tick gate, so the cost
/// returned by `compile_hir_to_map` must be gate-inclusive, not the raw
/// lowered program cost.
fn assert_gate_inclusive(map_cost: StaticCost, lir_doc: &LirDocument, program_cost: StaticCost) {
    assert_eq!(map_cost, eudkit_map::assembled_static_cost(lir_doc));
    assert_eq!(
        map_cost.triggers,
        program_cost.triggers + eudkit_map::tick_gate::GATE_TRIGGER_COUNT
    );
}

#[test]
fn ts_and_py_emit_byte_identical_hir_and_lower_to_2408_byte_raw_triggers() {
    let ts_script = r#"
import {
  Always, AtLeast, Deaths, DeathsX, DisplayText, EPD, Exactly,
  Memory, P1, SetDeaths, SetDeathsX, SetMemory, SetNextPtr, SetTo,
  Trigger, buildModule
} from "./ts/index.ts";

const mod = buildModule(() => {
  const t0 = new Trigger({
    conds: [
      Always(),
      DeathsX(P1, Exactly, 42, 0, 0xff00ff00),
    ],
    acts: [
      SetDeathsX(EPD(0x58a368), SetTo, 0x12345678, 0, 0x00ff00ff),
      SetMemory(0x58a36c, SetTo, 999),
      DisplayText("Ticket 1 E2E Parity!"),
    ],
    preserved: true,
  });
  const t1 = new Trigger({
    conds: [Memory(0x58a36c, AtLeast, 999)],
    acts: [
      SetDeaths(t0.act(0).value, SetTo, 0xdeadbeef, 0),
      SetNextPtr(t0, t1),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;

    let py_script = r#"
from py import (
    Always, AtLeast, Deaths, DeathsX, DisplayText, EPD, Exactly,
    Memory, P1, SetDeaths, SetDeathsX, SetMemory, SetNextPtr, SetTo,
    Trigger, buildModule
)
def prog():
    t0 = Trigger(
        conds=[
            Always(),
            DeathsX(P1, Exactly, 42, 0, 0xFF00FF00),
        ],
        acts=[
            SetDeathsX(EPD(0x58A368), SetTo, 0x12345678, 0, 0x00FF00FF),
            SetMemory(0x58A36C, SetTo, 999),
            DisplayText("Ticket 1 E2E Parity!"),
        ],
        preserved=True,
    )
    t1 = Trigger(
        conds=[Memory(0x58A36C, AtLeast, 999)],
        acts=[
            SetDeaths(t0.act(0).value, SetTo, 0xDEADBEEF, 0),
            SetNextPtr(t0, None),
        ],
        preserved=True,
    )
    # Patch SetNextPtr(t0, t1) or construct t1 first:
"#;
    let _ = py_script;

    // Notice in TS `const t1 = new Trigger({ acts: [SetNextPtr(t0, t1)] })` would reference t1 before initialization!
    // Let's write the program cleanly where t0 and t1 are constructed and wired:
    let ts_clean = r#"
import {
  Always, AtLeast, DeathsX, DisplayText, EPD, Exactly,
  Memory, P1, SetDeaths, SetDeathsX, SetMemory, SetNextPtr, SetTo,
  Trigger, buildModule
} from "./ts/index.ts";

const mod = buildModule(() => {
  const t0 = new Trigger({
    conds: [
      Always(),
      DeathsX(P1, Exactly, 42, 0, 0xff00ff00),
    ],
    acts: [
      SetDeathsX(EPD(0x58a368), SetTo, 0x12345678, 0, 0x00ff00ff),
      SetMemory(0x58a36c, SetTo, 999),
      DisplayText("Ticket 1 E2E Parity!"),
    ],
    preserved: true,
  });
  new Trigger({
    conds: [Memory(0x58a36c, AtLeast, 999)],
    acts: [
      SetDeaths(t0.act(0).value, SetTo, 0xdeadbeef, 0),
      SetNextPtr(t0, t0),
    ],
    preserved: true,
  });
});
process.stdout.write(mod.toJson());
"#;

    let py_clean = r#"
import sys
from py import (
    Always, AtLeast, DeathsX, DisplayText, EPD, Exactly,
    Memory, P1, SetDeaths, SetDeathsX, SetMemory, SetNextPtr, SetTo,
    Trigger, buildModule
)
def prog():
    t0 = Trigger(
        conds=[
            Always(),
            DeathsX(P1, Exactly, 42, 0, 0xFF00FF00),
        ],
        acts=[
            SetDeathsX(EPD(0x58A368), SetTo, 0x12345678, 0, 0x00FF00FF),
            SetMemory(0x58A36C, SetTo, 999),
            DisplayText("Ticket 1 E2E Parity!"),
        ],
        preserved=True,
    )
    Trigger(
        conds=[Memory(0x58A36C, AtLeast, 999)],
        acts=[
            SetDeaths(t0.act(0).value, SetTo, 0xDEADBEEF, 0),
            SetNextPtr(t0, t0),
        ],
        preserved=True,
    )
sys.stdout.write(buildModule(prog).to_json())
"#;

    let _ = ts_script;
    let ts_out = Command::new("node")
        .args(["--input-type=module", "-e", ts_clean])
        .current_dir(repo_root())
        .output()
        .expect("node must run TS snippet");
    assert!(
        ts_out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&ts_out.stderr)
    );
    let ts_hir_json = String::from_utf8(ts_out.stdout).unwrap();

    let py_out = Command::new("python3")
        .args(["-c", py_clean])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run Python snippet");
    assert!(
        py_out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&py_out.stderr)
    );
    let py_hir_json = String::from_utf8(py_out.stdout).unwrap();

    // 1. Byte-for-byte HIR 3.0 parity
    assert_eq!(ts_hir_json, py_hir_json);

    // 2. Parse into HirDocument and lower to LIR 3.0
    let hir_doc = hir_from_json(&ts_hir_json).unwrap();
    let lir_doc = lower_document(&hir_doc).unwrap();
    let cost = static_cost(&lir_doc);
    assert_eq!(cost.triggers, 2);
    assert_eq!(cost.conditions, 3);
    assert_eq!(cost.actions, 5);
    assert_eq!(cost.trigger_bytes, 2 * RAW_TRIGGER_BYTE_SIZE);

    let payload = lir2payload(&lir_doc).unwrap();
    assert_eq!(payload.data.len(), 2 * 2408);

    let raw0 = RawTrigger::decode(payload.data[0..2408].try_into().unwrap());
    let raw1 = RawTrigger::decode(payload.data[2408..4816].try_into().unwrap());

    assert_eq!(raw0.conds[0].condtype, 22);
    assert_eq!(raw0.conds[1].condtype, 15);
    assert_eq!(raw0.conds[1].locid, 0xFF00_FF00);
    assert_eq!(raw0.conds[1].eudx, 0x4353);
    assert_eq!(raw0.acts[0].acttype, 45);
    assert_eq!(raw0.acts[0].mask, 0x00FF_00FF);
    assert_eq!(raw0.acts[0].dest, 1);
    assert_eq!(raw0.acts[0].value, 0x1234_5678);
    assert_eq!(raw0.acts[0].eudx, 0x4353);

    // Verify self-modifying write SetDeaths(t0.act(0).value, SetTo, 0xDEADBEEF)
    // t0.act(0).value is at byte offset 328 + 20 = 348 inside t0 (offset 0)
    assert_eq!(raw1.acts[0].dest, epd_u32(348));
    assert_eq!(raw1.acts[0].value, 0xDEAD_BEEF);

    // 3. Compile to .scx map
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("parity_out.scx");
    let map_cost = compile_hir_to_map(&ts_hir_json, &basemap, &out_scx).unwrap();
    assert_gate_inclusive(map_cost, &lir_doc, cost);
    assert!(out_scx.is_file());
}

#[test]
fn ticket2_ts_and_py_emit_byte_identical_hir_and_simulate_trigger_page_batching() {
    use eudkit_lir::{ACT_BYTE_SIZE, ACT_COUNT, ACTS_BYTE_OFFSET, simulate_trigger_loop};

    let ts_clean = r#"
import { DisplayText, Trigger, Var, buildModule } from "./ts/index.ts";

const mod = buildModule(() => {
  const x = Var(10, undefined, undefined, "page");
  const y = Var(20, undefined, undefined, "page");
  const z = Var(30, undefined, undefined, "page");
  const a = Var(0, undefined, undefined, "page");
  const b = Var(0, undefined, undefined, "page");
  const c = Var(5, undefined, undefined, "page");
  const ok = Var(0, undefined, undefined, "page");

  x.iadd(5);
  y.isub(2);
  a.set(x);
  b.set(y);
  c.iadd(z);

  new Trigger({
    conds: [a.eq(15), b.ge(18), c.le(35)],
    acts: [ok.act_set(1), DisplayText("Ticket 2 E2E Parity!")],
  });
});
process.stdout.write(mod.toJson());
"#;

    let py_clean = r#"
import sys
from py import DisplayText, Trigger, Var, buildModule

def prog():
    x = Var(10, storage="page")
    y = Var(20, storage="page")
    z = Var(30, storage="page")
    a = Var(0, storage="page")
    b = Var(0, storage="page")
    c = Var(5, storage="page")
    ok = Var(0, storage="page")

    x += 5
    y -= 2
    a.set(x)
    b.set(y)
    c += z

    Trigger(
        conds=[a == 15, b >= 18, c <= 35],
        acts=[ok.act_set(1), DisplayText("Ticket 2 E2E Parity!")],
    )

sys.stdout.write(buildModule(prog).to_json())
"#;

    let ts_out = Command::new("node")
        .args(["--input-type=module", "-e", ts_clean])
        .current_dir(repo_root())
        .output()
        .expect("node must run TS snippet");
    assert!(
        ts_out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&ts_out.stderr)
    );
    let ts_hir_json = String::from_utf8(ts_out.stdout).unwrap();

    let py_out = Command::new("python3")
        .args(["-c", py_clean])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run Python snippet");
    assert!(
        py_out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&py_out.stderr)
    );
    let py_hir_json = String::from_utf8(py_out.stdout).unwrap();

    // 1. Byte-identical HIR 3.0 parity
    assert_eq!(ts_hir_json, py_hir_json);

    // 2. Lower to LIR 3.0 and simulate trigger loop
    let hir_doc = hir_from_json(&ts_hir_json).unwrap();
    let lir_doc = lower_document(&hir_doc).unwrap();
    let s_cost = static_cost(&lir_doc);
    let sim = simulate_trigger_loop(&lir_doc).unwrap();

    // Triggers: T_setup (0), T_cont (1), conditional Trigger (2), TriggerPage 0 (3)
    assert_eq!(s_cost.triggers, 4);
    assert_eq!(sim.last_executed_cost.triggers, 4);
    assert_eq!(sim.last_executed_cost.visited_items, vec![0, 3, 1, 2]);

    let page_base = sim.payload.addrs[3] as usize;
    let var_vals: Vec<u32> = (0..7)
        .map(|slot| sim.read_page_slot_value(3, slot))
        .collect();
    assert_eq!(var_vals, vec![15, 18, 30, 15, 18, 35, 1]);

    // Verify self-targeted action invariant holds on all 64 slots of TriggerPage 0
    let page_raw = sim.raw_trigger(3);
    for slot in 0..ACT_COUNT {
        let act_base = page_base + ACTS_BYTE_OFFSET + slot * ACT_BYTE_SIZE;
        assert_eq!(page_raw.acts[slot].dest, epd_u32((act_base + 20) as u32));
        assert_eq!(page_raw.acts[slot].modifier, 7);
    }

    // 3. Compile to .scx map
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("ticket2_parity_out.scx");
    let map_cost = compile_hir_to_map(&ts_hir_json, &basemap, &out_scx).unwrap();
    assert_gate_inclusive(map_cost, &lir_doc, s_cost);
    assert!(out_scx.is_file());
}

#[test]
fn ticket5_ts_and_py_emit_byte_identical_hir_and_simulate_cunit_and_with_cp() {
    use eudkit_lir::simulate_trigger_loop;

    let ts_script = r#"
import { CUnit, DisplayText, buildModule, withCP } from "./ts/index.ts";

const mod = buildModule(() => {
  const u = CUnit(5000);
  u.hp.set(1500);
  u.posY.set(300);

  withCP(7000, (cp) => {
    cp.at(2).set(100);
    const cu = cp.view(CUnit);
    cu.hp.iadd(50);
  });
  DisplayText("Ticket 5 Parity!");
});
process.stdout.write(mod.toJson());
"#;

    let py_script = r#"
import sys
from py import CUnit, DisplayText, build_module, with_cp

def prog():
    u = CUnit(5000)
    u.hp.set(1500)
    u.posY.set(300)

    with with_cp(7000) as cp:
        cp.at(2).set(100)
        cu = cp.view(CUnit)
        cu.hp.iadd(50)

    DisplayText("Ticket 5 Parity!")

sys.stdout.write(build_module(prog).to_json())
"#;

    let ts_out = Command::new("node")
        .args(["--input-type=module", "-e", ts_script])
        .current_dir(repo_root())
        .output()
        .expect("node must run TS snippet");
    assert!(
        ts_out.status.success(),
        "node failed: {}",
        String::from_utf8_lossy(&ts_out.stderr)
    );
    let ts_hir_json = String::from_utf8(ts_out.stdout).unwrap();

    let py_out = Command::new("python3")
        .args(["-c", py_script])
        .current_dir(repo_root())
        .output()
        .expect("python3 must run Python snippet");
    assert!(
        py_out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&py_out.stderr)
    );
    let py_hir_json = String::from_utf8(py_out.stdout).unwrap();

    // 1. Byte-identical HIR 3.0 parity
    assert_eq!(ts_hir_json, py_hir_json);

    // 2. Lower and simulate trigger loop
    let hir_doc = hir_from_json(&ts_hir_json).unwrap();
    let lir_doc = lower_document(&hir_doc).unwrap();
    let s_cost = static_cost(&lir_doc);
    let sim = simulate_trigger_loop(&lir_doc).unwrap();

    // Verify constant EPD field writes
    assert_eq!(sim.read_epd(5002), 1500); // CUnit.hp
    assert_eq!(sim.read_epd(5010), 300 << 16); // CUnit.posY (high word)

    // Verify withCP relative hops and writes via Player 13
    assert_eq!(sim.read_epd(7002), 150); // cp.at(2) was 100 + cu.hp 50 = 150
    assert_eq!(sim.read_epd(203155), 7000); // CurrentPlayer restored to base 7000

    // 3. Compile to .scx map
    let basemap = repo_root().join("tests/outputmap/basemap/basemap.scx");
    let dir = tempfile::tempdir().unwrap();
    let out_scx = dir.path().join("ticket5_parity_out.scx");
    let map_cost = compile_hir_to_map(&ts_hir_json, &basemap, &out_scx).unwrap();
    assert_gate_inclusive(map_cost, &lir_doc, s_cost);
    assert!(out_scx.is_file());
}
