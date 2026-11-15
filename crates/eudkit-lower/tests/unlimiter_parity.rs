//! Cross-frontend parity and runtime proof for the unlimiter helper (issue
//! #94, port of euddraft `plugins/unlimiter.py`).
//!
//! Three things are pinned here:
//!
//! * the Python and TypeScript frontends emit byte-identical HIR for the same
//!   `connectDList` / `unlimiter` program, and that HIR lowers;
//! * the lowered loop actually builds the doubly-linked list, read back out of
//!   the simulated blob payload: `prev`/`next` point at the neighbouring
//!   elements, the first `prev` and last `next` are 0, and the head/tail dwords
//!   point at element 0 and element `n - 1`;
//! * the sprite variant seeds each element's `+0x10` field with `10000 + k`.
//!
//! The full `unlimiter()` program is checked structurally only: one pass
//! evaluates 679,936 triggers (204,800 loop checks + 204,800 write triggers +
//! 204,800 patch blocks + 65,536 sprite write triggers — one extra evaluation
//! per element over the source's combined write+patch trigger), which trips
//! the simulator's 100,000-evaluation infinite-loop guard. The per-list runtime
//! proof therefore uses the same shapes with four elements.

use std::path::Path;
use std::process::Command;

use eudkit_hir::hir_from_json;
use eudkit_lir::{SimulationState, epd_u32, init_simulation};
use eudkit_lower::lower_document;

fn repo_root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn run_node(script: &str) -> String {
    let out = Command::new("node")
        .args(["--input-type=module", "-e", script])
        .current_dir(repo_root())
        .output()
        .expect("node must run the TS snippet");
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
        .expect("python3 must run the Python snippet");
    assert!(
        out.status.success(),
        "python3 failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

/// One small list per shape: bullets, sprites (with the `+0x10` field),
/// images and orders, four elements each.
const SMALL_PROGRAM_TS: &str = r#"
import { buildModule, connectDList } from "./ts/index.ts";
const mod = buildModule(() => {
  connectDList(0x64eed8, 0x64eedc, 112, 4, 0);
  connectDList(0x63fe30, 0x63fe34, 36, 4, 1);
  connectDList(0x57eb68, 0x57eb70, 64, 4, 0);
  connectDList(0x64b2e0, 0x64b2e4, 20, 4, 0);
});
process.stdout.write(mod.toJson());
"#;

const SMALL_PROGRAM_PY: &str = r#"
import sys
from py import build_module, connect_dlist
def prog():
    connect_dlist(0x64EED8, 0x64EEDC, 112, 4, 0)
    connect_dlist(0x63FE30, 0x63FE34, 36, 4, 1)
    connect_dlist(0x57EB68, 0x57EB70, 64, 4, 0)
    connect_dlist(0x64B2E0, 0x64B2E4, 20, 4, 0)
sys.stdout.write(build_module(prog).to_json())
"#;

const FULL_PROGRAM_TS: &str = r#"
import { buildModule, unlimiter } from "./ts/index.ts";
const mod = buildModule(() => { unlimiter(); });
process.stdout.write(mod.toJson());
"#;

const FULL_PROGRAM_PY: &str = r#"
import sys
from py import build_module, unlimiter
def prog():
    unlimiter()
sys.stdout.write(build_module(prog).to_json())
"#;

/// The four `connectDList` calls of `unlimiter()`: (head, tail, stride, count).
const LISTS: [(u32, u32, u32, u32); 4] = [
    (0x64EED8, 0x64EEDC, 112, 8192),
    (0x63FE30, 0x63FE34, 36, 65536),
    (0x57EB68, 0x57EB70, 64, 65536),
    (0x64B2E0, 0x64B2E4, 20, 65536),
];

fn simulate(hir: &str) -> SimulationState {
    let doc = hir_from_json(hir).expect("HIR must parse");
    let lir = lower_document(&doc).expect("HIR must lower");
    let mut sim = init_simulation(&lir).expect("simulation must initialize");
    sim.step_loop();
    sim
}

fn read(sim: &SimulationState, addr: u32) -> u32 {
    sim.read_epd(epd_u32(addr))
}

#[test]
fn py_and_ts_emit_byte_identical_unlimiter_hir() {
    let ts = run_node(SMALL_PROGRAM_TS);
    let py = run_python(SMALL_PROGRAM_PY);
    assert_eq!(ts, py, "small unlimiter HIR mismatch");
    let doc = hir_from_json(&ts).expect("HIR must parse");
    assert_eq!(doc.module.blobs.len(), 4, "one blob per list");
    assert_eq!(
        doc.module.blobs.iter().map(|b| b.size).collect::<Vec<_>>(),
        vec![112 * 6, 36 * 6, 64 * 6, 20 * 6],
        "each blob is itemSize * (itemN + 2): one padding element per side"
    );
    lower_document(&doc).expect("small unlimiter HIR must lower");

    let full_ts = run_node(FULL_PROGRAM_TS);
    let full_py = run_python(FULL_PROGRAM_PY);
    assert_eq!(full_ts, full_py, "full unlimiter HIR mismatch");

    let full = hir_from_json(&full_ts).expect("full HIR must parse");
    assert_eq!(
        full.module.blobs.iter().map(|b| b.size).collect::<Vec<_>>(),
        vec![917728, 2359368, 4194432, 1310760],
        "bullet/sprite/image/order blob bytes (itemSize * (itemN + 2))"
    );
    assert_eq!(
        full.module.blobs.iter().map(|b| b.size).sum::<usize>(),
        8_782_288,
        "total unlimiter payload"
    );
    lower_document(&full).expect("full unlimiter HIR must lower");
}

#[test]
fn simulated_loop_connects_every_list() {
    let hir = run_python(SMALL_PROGRAM_PY);
    let sim = simulate(&hir);

    // The small program arms each shape with four elements.
    const COUNT: u32 = 4;
    for (index, &(head, tail, stride, _)) in LISTS.iter().enumerate() {
        let base = read(&sim, head);
        assert_ne!(base, 0, "list {index} head must be patched");
        assert_eq!(
            read(&sim, tail),
            base + stride * (COUNT - 1),
            "list {index} tail must point at the last element"
        );

        for k in 0..COUNT {
            let element = base + stride * k;
            let expected_prev = if k == 0 { 0 } else { element - stride };
            let expected_next = if k + 1 == COUNT { 0 } else { element + stride };
            assert_eq!(
                read(&sim, element),
                expected_prev,
                "list {index} element {k} prev"
            );
            assert_eq!(
                read(&sim, element + 4),
                expected_next,
                "list {index} element {k} next"
            );
            if index == 1 {
                assert_eq!(
                    read(&sim, element + 0x10),
                    10000 + k,
                    "sprite element {k} +0x10 seed"
                );
            }
        }

        // The padding element before the list is only ever used as a value and
        // the padding element after the last element is re-zeroed by the tail.
        assert_eq!(read(&sim, base - stride), 0, "list {index} leading padding");
        assert_eq!(
            read(&sim, base + stride * COUNT),
            0,
            "list {index} trailing padding"
        );
    }
}
