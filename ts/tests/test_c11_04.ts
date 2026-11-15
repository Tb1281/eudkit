/**
 * Candidate 11 ticket C11-04 (issue #31): counted forms (`every` + `after`)
 * promoted from DSL desugar to HIR nodes with a single opt desugar home.
 *
 * Mechanism (corrected per the C11-03 finding, superseding design-doc Q6):
 * the DSL allocates the counter variable at node-emission time, in the
 * old allocation order, and the node carries it as a required
 * `counter_var` field; opt desugar uses that id verbatim and never mints
 * var ids. Byte-identical output is therefore by construction for the
 * rank order, and the opt desugar merges a top-level `every` increment
 * into a preceding unconditional trigger (the DSL's own act-run packing)
 * so the act-run packing is identical too.
 *
 * TDD red: on the old DSL these tests fail (whens + DSL-side counter
 * desugar instead of nodes); on C11-01 scaffolding the byte-identity
 * tests fail (nodes pass through opt unchanged and fail loud in lower).
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { Var, after, buildModule, every, once, when, whileLoop } from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runLower(hirJson: string): any {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-c1104-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["lower", hirPath], {
      encoding: "utf-8",
    }).trim();
    return JSON.parse(out);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

// --- Old DSL desugar shapes, written literally (not via the DSL) --------
// These are the shapes the old DSL emitted; the differential tests below
// prove the new node path compiles to exactly the same LIR. The counter
// id comes from the node's `counter_var` field: the DSL allocates it in
// the old order, so the old-shape vars list is already correct and the
// old document needs no extra declarations.

function varDeaths(cnt: number, comparison: number, amount: number): any {
  return {
    locid: 0,
    player: 0,
    amount,
    unit: 0,
    comparison,
    condtype: 15,
    restype: 0,
    flags: 0,
    eudx: 0,
    relocs: [{ field_offset: 4, target_var: cnt, target_offset: 20, mode: 1 }],
  };
}

function setVar(cnt: number, modifier: number, value: number): any {
  return {
    mask: 4294967295,
    strid: 0,
    wavid: 0,
    time: 0,
    dest: 0,
    value,
    unit: 0,
    acttype: 45,
    modifier,
    flags: 20,
    padding: 0,
    eudx: 17235,
    relocs: [{ field_offset: 16, target_var: cnt, target_offset: 20, mode: 1 }],
  };
}

function oldEvery(n: number, body: any[], cnt: number): any[] {
  return [
    { kind: "act", ...setVar(cnt, 8, 1) },
    {
      kind: "when",
      conds: [varDeaths(cnt, 0, n)],
      body: [{ kind: "act", ...setVar(cnt, 7, 0) }, ...body],
    },
  ];
}

function oldAfter(n: number, body: any[], cnt: number): any[] {
  return [
    {
      kind: "when",
      conds: [varDeaths(cnt, 10, 1)],
      body: [{ kind: "act", ...setVar(cnt, 7, 0) }, ...body],
    },
    {
      kind: "when",
      conds: [varDeaths(cnt, 0, 2)],
      body: [{ kind: "act", ...setVar(cnt, 9, 1) }],
    },
    {
      kind: "when",
      conds: [varDeaths(cnt, 10, 0)],
      body: [{ kind: "act", ...setVar(cnt, 7, n) }],
    },
  ];
}

function oldDoc(newDoc: any, replace: (stmts: any[]) => any[]): any {
  return {
    version: newDoc.version,
    module: {
      ...newDoc.module,
      stmts: replace(newDoc.module.stmts),
    },
  };
}

function hirJson(mod: { toJson(): string }): any {
  return JSON.parse(mod.toJson());
}

test("C11-04: every(n, body) emits a HirEvery node carrying its DSL-allocated counter", () => {
  const mod = buildModule(() => {
    const hits = Var(0);
    every(3, () => {
      hits.iadd(1);
    });
  });
  const doc = hirJson(mod);
  assert.equal(doc.version, "3.2");
  // The counter is allocated by the DSL at emission time, in the old
  // order (after the user var), and carried on the node.
  assert.deepEqual(
    doc.module.vars.map((v: any) => v.id),
    [0, 1],
  );
  const kinds = doc.module.stmts.map((s: any) => s.kind);
  assert.ok(kinds.includes("every"), `expected an every node, got ${kinds}`);
  const node = doc.module.stmts.find((s: any) => s.kind === "every");
  assert.equal(node.n, 3);
  assert.equal(node.counter_var, 1);
  assert.equal(node.body.length, 1);
  assert.equal(node.body[0].kind, "act");
});

test("C11-04: after(n, body) emits a HirAfter node carrying its DSL-allocated counter", () => {
  const mod = buildModule(() => {
    const fired = Var(0);
    after(2, () => {
      fired.iadd(1);
    });
  });
  const doc = hirJson(mod);
  assert.deepEqual(
    doc.module.vars.map((v: any) => v.id),
    [0, 1],
  );
  const kinds = doc.module.stmts.map((s: any) => s.kind);
  assert.ok(kinds.includes("after"), `expected an after node, got ${kinds}`);
  const node = doc.module.stmts.find((s: any) => s.kind === "after");
  assert.equal(node.n, 2);
  assert.equal(node.counter_var, 1);
  assert.equal(node.body.length, 1);
  assert.equal(node.body[0].kind, "act");
});

test("C11-04: byte-identical lower output for every+after (clean program)", () => {
  const mod = buildModule(() => {
    const hits = Var(0);
    every(3, () => {
      hits.iadd(1);
    });
    after(2, () => {
      hits.iadd(10);
    });
  });
  const doc = hirJson(mod);
  const everyNode = doc.module.stmts.find((s: any) => s.kind === "every");
  const afterNode = doc.module.stmts.find((s: any) => s.kind === "after");
  const old = oldDoc(doc, () => [
    ...oldEvery(everyNode.n, everyNode.body, everyNode.counter_var),
    ...oldAfter(afterNode.n, afterNode.body, afterNode.counter_var),
  ]);
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for a top-level act run before every+after", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    every(3, () => {
      count.iadd(1);
    });
    after(2, () => {
      count.iadd(10);
    });
  });
  const doc = hirJson(mod);
  const everyNode = doc.module.stmts.find((s: any) => s.kind === "every");
  const afterNode = doc.module.stmts.find((s: any) => s.kind === "after");
  // Old-shape input uses unpacked act+when lists; opt's bin-packing
  // reproduces the DSL trigger packing identically.
  const old = oldDoc(doc, () => [
    { kind: "act", ...setVar(0, 8, 1) },
    ...oldEvery(everyNode.n, everyNode.body, everyNode.counter_var),
    ...oldAfter(afterNode.n, afterNode.body, afterNode.counter_var),
  ]);
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for a dword var created after every", () => {
  const mod = buildModule(() => {
    const hits = Var(0);
    every(3, () => {
      hits.iadd(1);
    });
    const late = Var(0);
    late.iadd(5);
  });
  const doc = hirJson(mod);
  const everyNode = doc.module.stmts.find((s: any) => s.kind === "every");
  const old = oldDoc(doc, () => [
    ...oldEvery(everyNode.n, everyNode.body, everyNode.counter_var),
    { kind: "act", ...setVar(2, 8, 5) },
  ]);
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for consecutive everys after one act run", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const hits = Var(0);
    frame.iadd(1);
    every(3, () => {
      hits.iadd(1);
    });
    every(2, () => {
      hits.iadd(10);
    });
  });
  const doc = hirJson(mod);
  const nodes = doc.module.stmts.filter((s: any) => s.kind === "every");
  const old = oldDoc(doc, () => [
    { kind: "act", ...setVar(0, 8, 1) },
    ...oldEvery(nodes[0].n, nodes[0].body, nodes[0].counter_var),
    ...oldEvery(nodes[1].n, nodes[1].body, nodes[1].counter_var),
  ]);
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for every inside when", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const hits = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      every(2, () => {
        hits.iadd(1);
      });
    });
  });
  const doc = hirJson(mod);
  const outer = doc.module.stmts.find((s: any) => s.kind === "when");
  const everyNode = outer.body.find((s: any) => s.kind === "every");
  const old = oldDoc(doc, (stmts) =>
    stmts.map((s: any) =>
      s.kind === "when"
        ? {
            ...s,
            body: oldEvery(
              everyNode.n,
              everyNode.body,
              everyNode.counter_var,
            ),
          }
        : s,
    ),
  );
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for after inside once", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const fired = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      after(2, () => {
        fired.iadd(1);
      });
    });
  });
  const doc = hirJson(mod);
  const outer = doc.module.stmts.find((s: any) => s.kind === "once");
  const afterNode = outer.body.find((s: any) => s.kind === "after");
  const old = oldDoc(doc, (stmts) =>
    stmts.map((s: any) =>
      s.kind === "once"
        ? {
            ...s,
            body: oldAfter(
              afterNode.n,
              afterNode.body,
              afterNode.counter_var,
            ),
          }
        : s,
    ),
  );
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: byte-identical lower output for every inside whileLoop", () => {
  const mod = buildModule(() => {
    const counter = Var(3);
    const hits = Var(0);
    whileLoop(counter.gt(0), () => {
      counter.isub(1);
      every(2, () => {
        hits.iadd(1);
      });
    });
  });
  const doc = hirJson(mod);
  const outer = doc.module.stmts.find((s: any) => s.kind === "while");
  const everyNode = outer.body.find((s: any) => s.kind === "every");
  const old = oldDoc(doc, (stmts) =>
    stmts.map((s: any) =>
      s.kind === "while"
        ? {
            ...s,
            body: [
              s.body[0],
              ...oldEvery(
                everyNode.n,
                everyNode.body,
                everyNode.counter_var,
              ),
            ],
          }
        : s,
    ),
  );
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
});

test("C11-04: DSL n validation is kept (every/after reject non-positive n)", () => {
  assert.throws(() => {
    buildModule(() => {
      every(0, () => {});
    });
  }, /positive integer/);
  assert.throws(() => {
    buildModule(() => {
      after(-1, () => {});
    });
  }, /positive integer|build-time integer/);
});
