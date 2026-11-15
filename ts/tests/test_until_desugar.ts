/**
 * C11-02 — Until desugar: DSL node emission + byte-identity tests.
 *
 * The new pipeline (DSL emits `HirUntil`, opt desugars it) must produce
 * compiled output byte-identical to the old pipeline (DSL desugars to
 * 2 whens) for every case below, including variables allocated after
 * the `until` call. That holds because the DSL reserves the latch
 * variable at node-emission time, in exactly the old allocation order,
 * and carries it on the node as `latch_var`; the opt desugar uses that
 * id verbatim and never mints ids. (The design doc's Q6 mechanism —
 * minting in opt via `next_id` + appended declarations — was corrected
 * here: `next_id` is the trigger-id namespace, and appended-late ids
 * sort last in `analyze_storage`'s id-order ranking, shifting ranks.)
 * The old-shape programs below are written with the surviving `when` +
 * `Var` primitives, so they pin the old output without depending on
 * the deleted DSL desugar.
 *
 * P5/P6 are the discriminating cases for the correction: a bit-class
 * variable allocated after `until` must NOT swap bit positions with
 * the latch. Those tests assert the new output equals the direct old
 * output exactly (and differs from the swapped-layout old output).
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  Var,
  buildModule,
  once,
  until,
  when,
  whileLoop,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

// Full pipeline comparison: `dump-ir` runs optimize_module then lowering,
// so it exercises the opt desugar. The comparison is the ENTIRE dump-ir
// output (storage report including the per-var Allocations listing, plus
// the rendered LIR): with the latch reserved at the call site, the new
// program's declarations, ids, and allocation mapping are identical to
// the old program's, so nothing needs to be dropped.
function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-until-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf-8",
    });
    assert.ok(out.includes("LIR v3.0"), "dump-ir output must contain an LIR section");
    return out;
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function findUntil(doc: {
  module: { stmts: readonly { kind: string; latch_var?: number }[] };
}) {
  return doc.module.stmts.find((s) => s.kind === "until");
}

// P1: simple until, no vars allocated after until().
function newP1() {
  return buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(2), () => {
      count.iadd(1);
    });
  });
}

function oldP1() {
  return buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
  });
}

test("until emits a HirUntil node carrying the DSL-reserved latch var", () => {
  const doc = JSON.parse(newP1().toJson()) as {
    module: {
      vars: readonly { id: number; init: number }[];
      stmts: readonly { kind: string; latch_var?: number }[];
    };
  };
  const node = findUntil(doc);
  assert.ok(node, "new HIR must contain an until node");
  // P1 allocates frame (id 0), count (id 1), then the latch at the
  // until() call (id 2), exactly as the old desugar did.
  assert.equal(node.latch_var, 2, "node must carry the reserved latch id");
  assert.equal(doc.module.vars.length, 3, "latch decl is in DSL output");
  assert.deepEqual(
    doc.module.vars.map((v) => [v.id, v.init]),
    [
      [0, 0],
      [1, 0],
      [2, 1],
    ],
    "latch decl (id 2, init 1) sits at the call site, not appended",
  );
});

test("P1 byte-identical: simple until", () => {
  assert.equal(runDumpIr(newP1().toJson()), runDumpIr(oldP1().toJson()));
});

test("P2 byte-identical: later dword var does not shift the latch", () => {
  const newMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(2), () => {
      count.iadd(1);
    });
    const later = Var(0);
    later.iadd(1);
  });
  const oldMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
    const later = Var(0);
    later.iadd(1);
  });
  assert.equal(runDumpIr(newMod.toJson()), runDumpIr(oldMod.toJson()));
});

test("P3 byte-identical: body-allocated dword var", () => {
  const newMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(2), () => {
      const tmp = Var(0);
      tmp.iadd(1);
      count.iadd(1);
    });
  });
  const oldMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      const tmp = Var(0);
      tmp.iadd(1);
      count.iadd(1);
    });
  });
  assert.equal(runDumpIr(newMod.toJson()), runDumpIr(oldMod.toJson()));
});

test("P4 byte-identical: until nested inside when", () => {
  const newMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      until(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  const oldMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      const active = Var(1);
      when(active.eq(1), () => {
        when(frame.eq(3), () => {
          active.set(0);
        });
      });
      when(active.eq(1), () => {
        count.iadd(1);
      });
    });
  });
  assert.equal(runDumpIr(newMod.toJson()), runDumpIr(oldMod.toJson()));
});

test("nesting: until inside once and inside whileLoop", () => {
  const newOnce = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      until(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  const oldOnce = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      const active = Var(1);
      when(active.eq(1), () => {
        when(frame.eq(3), () => {
          active.set(0);
        });
      });
      when(active.eq(1), () => {
        count.iadd(1);
      });
    });
  });
  assert.equal(
    runDumpIr(newOnce.toJson()),
    runDumpIr(oldOnce.toJson()),
  );

  const newWhile = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      until(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  const oldWhile = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      const active = Var(1);
      when(active.eq(1), () => {
        when(frame.eq(3), () => {
          active.set(0);
        });
      });
      when(active.eq(1), () => {
        count.iadd(1);
      });
    });
  });
  assert.equal(
    runDumpIr(newWhile.toJson()),
    runDumpIr(oldWhile.toJson()),
  );
});

test("P5 byte-identical: later unused bit var keeps the latch bit position", () => {
  const newMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(2), () => {
      count.iadd(1);
    });
    const later = Var(0);
    void later;
  });
  // Direct old shape: latch id 2, later id 3.
  const directOld = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
    const later = Var(0);
    void later;
  });
  // Swapped layout: later id 2, latch id 3 — the layout the old
  // opt-minted latch produced. The corrected mechanism must NOT
  // produce it.
  const swappedOld = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const later = Var(0);
    void later;
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
  });
  const newLir = runDumpIr(newMod.toJson());
  assert.equal(
    newLir,
    runDumpIr(directOld.toJson()),
    "new output must equal the direct old output exactly (no rank shift)",
  );
  assert.notEqual(
    newLir,
    runDumpIr(swappedOld.toJson()),
    "new output must differ from the swapped layout (shift is gone)",
  );
});

test("P6 byte-identical: later boolean-style var keeps the latch bit position", () => {
  const newMod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(2), () => {
      count.iadd(1);
    });
    const flag = Var(0);
    when(flag.eq(1), () => {
      flag.set(0);
    });
  });
  const directOld = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
    const flag = Var(0);
    when(flag.eq(1), () => {
      flag.set(0);
    });
  });
  const swappedOld = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    const flag = Var(0);
    const active = Var(1);
    when(active.eq(1), () => {
      when(frame.eq(2), () => {
        active.set(0);
      });
    });
    when(active.eq(1), () => {
      count.iadd(1);
    });
    when(flag.eq(1), () => {
      flag.set(0);
    });
  });
  const newLir = runDumpIr(newMod.toJson());
  assert.equal(
    newLir,
    runDumpIr(directOld.toJson()),
    "new output must equal the direct old output exactly (no rank shift)",
  );
  assert.notEqual(
    newLir,
    runDumpIr(swappedOld.toJson()),
    "new output must differ from the swapped layout (shift is gone)",
  );
});
