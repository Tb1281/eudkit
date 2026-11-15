//! T-WP6: simulator proof for the 20T CUnit pointer reader.
//!
//! The engine semantics live in the simulator, so this test is the only place
//! the reader's arithmetic is exercised against seeded memory:
//!
//! * a pool-base pointer (`0x59CCA8`) reads back as `19025`;
//! * a last-slot pointer (`0x628148`) reads back as `161657`;
//! * a null pointer reads back as `0xFFF9D729`, which `in_cunit_pool` rejects
//!   through its **upper** bound;
//! * the Python and TypeScript frontends emit byte-identical HIR.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use eudkit::Frontend;
use eudkit_lir::init_simulation;

mod common;
use common::{repo_root, tempdir};

/// EPDs of three adjacent dwords that the program reads as CUnit list heads.
const HEAD_EPD: u32 = 161_843; // EPD(0x628430)
const NULL_EPD: u32 = 161_842;
const LAST_EPD: u32 = 161_841;

fn eudkit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_eudkit"))
        .args(args)
        .env("EUDKIT_ROOT", repo_root())
        .output()
        .expect("run eudkit")
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_ok(output: &Output) {
    assert!(
        output.status.success(),
        "eudkit failed\nstdout:\n{}\nstderr:\n{}",
        stdout(output),
        stderr(output)
    );
}

const PY_IMPORTS: &str = "from py import Var, build_module, when\n\
         from py.view import read_cunit_pointer, in_cunit_pool";

const TS_IMPORTS: &str = "import { Var, buildModule, when } from \"{root}/ts/index.ts\";\n\
         import { readCunitPointer, inCunitPool } from \"{root}/ts/view.ts\";";

fn write_py(dir: &Path, name: &str, body: &str) -> PathBuf {
    common::write_py(dir, name, PY_IMPORTS, body)
}

fn write_ts(dir: &Path, name: &str, body: &str) -> PathBuf {
    let imports = TS_IMPORTS.replace("{root}", &common::repo_root().display().to_string());
    common::write_ts(dir, name, &imports, body)
}

/// The same program on both frontends: three head reads plus the pool gate.
fn py_body() -> &'static str {
    "    base = Var(0)\n\
     \x20   base.set(read_cunit_pointer(161843))\n\
     \x20   nullp = Var(0)\n\
     \x20   nullp.set(read_cunit_pointer(161842))\n\
     \x20   last = Var(0)\n\
     \x20   last.set(read_cunit_pointer(161841))\n\
     \x20   hit = Var(0)\n\
     \x20   when([*in_cunit_pool(nullp), hit.eq(0)], lambda: hit.iadd(1))"
}

fn ts_body() -> &'static str {
    "  const base = Var(0);\n\
     \x20 base.set(readCunitPointer(161843));\n\
     \x20 const nullp = Var(0);\n\
     \x20 nullp.set(readCunitPointer(161842));\n\
     \x20 const last = Var(0);\n\
     \x20 last.set(readCunitPointer(161841));\n\
     \x20 const hit = Var(0);\n\
     \x20 when([...inCunitPool(nullp), hit.eq(0)], () => hit.iadd(1));"
}

#[test]
fn python_and_typescript_emit_identical_hir_for_the_reader() {
    let dir = tempdir("cunit-reader-");
    let py_path = write_py(dir.path(), "cunit_reader.py", py_body());
    let ts_path = write_ts(dir.path(), "cunit_reader.ts", ts_body());

    let ts_synth = eudkit(&["synth", ts_path.to_str().unwrap(), "--compact"]);
    assert_ok(&ts_synth);
    let py_synth = eudkit(&["synth", py_path.to_str().unwrap(), "--compact"]);
    assert_ok(&py_synth);
    assert_eq!(
        stdout(&ts_synth),
        stdout(&py_synth),
        "both frontends must emit byte-identical HIR"
    );
}

#[test]
fn simulator_reads_pool_base_last_slot_and_null_sentinel() {
    let dir = tempdir("cunit-reader-sim-");
    let py_path = write_py(dir.path(), "cunit_reader_sim.py", py_body());
    let ts_path = write_ts(dir.path(), "cunit_reader_sim.ts", ts_body());

    for (label, path, frontend) in [
        ("python", py_path.as_path(), Frontend::Py),
        ("typescript", ts_path.as_path(), Frontend::Ts),
    ] {
        let prepared = eudkit::prepare_source(path, Some(frontend), None)
            .unwrap_or_else(|err| panic!("{label}: prepare_source failed: {err}"));
        let mut sim = init_simulation(&prepared.lir)
            .unwrap_or_else(|err| panic!("{label}: init_simulation failed: {err}"));

        // Seed the three head dwords before the first trigger-loop pass.
        sim.external_epds.insert(HEAD_EPD, 0x59CCA8);
        sim.external_epds.insert(NULL_EPD, 0);
        sim.external_epds.insert(LAST_EPD, 0x628148);
        sim.step_loop();

        let ids: Vec<usize> = prepared.doc.module.vars.iter().map(|v| v.id).collect();
        assert_eq!(
            common::read_var(&prepared, &sim, ids[0]),
            19025,
            "{label}: pool base"
        );
        assert_eq!(
            common::read_var(&prepared, &sim, ids[2]),
            0xFFF9D729,
            "{label}: null sentinel"
        );
        assert_eq!(
            common::read_var(&prepared, &sim, ids[4]),
            161657,
            "{label}: last slot"
        );
        assert_eq!(
            common::read_var(&prepared, &sim, ids[6]),
            0,
            "{label}: in_cunit_pool must reject the null sentinel"
        );
    }
}
