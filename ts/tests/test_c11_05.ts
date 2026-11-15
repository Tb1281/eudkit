/**
 * Candidate 11 ticket C11-05 (issue #32): nesting matrix for the four
 * phase-1 temporal nodes (until, edge, every, after).
 *
 * Each program is built once with the new node emission. The old
 * document is derived from the new document's JSON by a recursive
 * transform that replaces every new node with its old DSL expansion
 * (same builders the per-ticket tests use), recursing into bodies at
 * every depth. The lowered LIR of both must be deep-equal.
 *
 * Two invariants keep the transform exact (both stated, not assumed):
 *
 * 1. No bare act precedes an `every` node at the same level in any
 *    matrix program, except the `after`/`every` fire-body `set` act
 *    (which is a bare act, not a pre-packed Trigger, when the Every
 *    arm runs, so the C11-04 merge-backward rule does not engage and
 *    plain bin-packing agrees on both sides). The merge-backward rule
 *    itself stays covered by the C11-04 tests (basic, multi
 *    merge-chaining, refusal cases); this matrix tests nesting
 *    composition, not the merge rule. The four every-outer pair cases
 *    therefore have no top-level act run before the outer `every`
 *    (a top-level run would pre-pack into a Trigger and engage the
 *    merge, which the naive transform does not replicate).
 * 2. Node bodies are copied verbatim from the new document, so any
 *    DSL act-run packing inside bodies is identical on both sides;
 *    only node headers are replaced by expansions.
 *
 * The recursion proof is the flatten test at the end plus the
 * same-kind nesting cases (until-in-until, edge-in-edge,
 * every-in-every, after-in-after) and the 3-deep case: a shallow
 * desugar that does not recurse would leave inner nodes undesugared
 * (lower fails loud on them) or nested whens unflattened (LIR
 * differs from the old program, whose plain whens opt flattens).
 * The shallow-implementation experiment evidencing this is described
 * in the delivery report, not in this file.
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
  after,
  buildModule,
  every,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runLower(hirJson: string): any {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-c1105-"));
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
// Comparison codes: 0 = AtLeast, 10 = Exactly. Modifier codes:
// 7 = SetTo, 8 = Add, 9 = Subtract. These match the per-ticket builders.

function varDeaths(v: number, comparison: number, amount: number): any {
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
    relocs: [{ field_offset: 4, target_var: v, target_offset: 20, mode: 1 }],
  };
}

function setVar(v: number, modifier: number, value: number): any {
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
    relocs: [{ field_offset: 16, target_var: v, target_offset: 20, mode: 1 }],
  };
}

function oldUntil(latch: number, conds: any[], body: any[]): any[] {
  return [
    {
      kind: "when",
      conds: [varDeaths(latch, 10, 1)],
      body: [
        {
          kind: "when",
          conds,
          body: [{ kind: "act", ...setVar(latch, 7, 0) }],
        },
      ],
    },
    {
      kind: "when",
      conds: [varDeaths(latch, 10, 1)],
      body,
    },
  ];
}

function oldEdge(
  direction: string,
  conds: any[],
  body: any[],
  curr: number,
  prev: number,
): any[] {
  const fireCurr = direction === "rising" ? 1 : 0;
  const firePrev = direction === "rising" ? 0 : 1;
  return [
    { kind: "act", ...setVar(curr, 7, 0) },
    {
      kind: "when",
      conds,
      body: [{ kind: "act", ...setVar(curr, 7, 1) }],
    },
    {
      kind: "when",
      conds: [varDeaths(curr, 10, fireCurr)],
      body: [
        {
          kind: "when",
          conds: [varDeaths(prev, 10, firePrev)],
          body,
        },
      ],
    },
    {
      kind: "when",
      conds: [varDeaths(curr, 10, 1)],
      body: [{ kind: "act", ...setVar(prev, 7, 1) }],
    },
    {
      kind: "when",
      conds: [varDeaths(curr, 10, 0)],
      body: [{ kind: "act", ...setVar(prev, 7, 0) }],
    },
  ];
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

// Recursive transform: replace every new node with its old expansion,
// recursing into bodies at every depth (including the bodies of the
// produced expansions, which are the transformed original bodies).
function desugarStmts(stmts: any[]): any[] {
  const out: any[] = [];
  for (const s of stmts) {
    if (s.kind === "until") {
      out.push(...oldUntil(s.latch_var, s.conds, desugarStmts(s.body)));
    } else if (s.kind === "edge") {
      out.push(
        ...oldEdge(
          s.direction,
          s.conds,
          desugarStmts(s.body),
          s.curr_var,
          s.prev_var,
        ),
      );
    } else if (s.kind === "every") {
      out.push(...oldEvery(s.n, desugarStmts(s.body), s.counter_var));
    } else if (s.kind === "after") {
      out.push(...oldAfter(s.n, desugarStmts(s.body), s.counter_var));
    } else if (s.kind === "when") {
      out.push({
        ...s,
        body: desugarStmts(s.body),
        ...(s.else_body ? { else_body: desugarStmts(s.else_body) } : {}),
      });
    } else if (s.kind === "once" || s.kind === "while") {
      out.push({ ...s, body: desugarStmts(s.body) });
    } else {
      out.push(s);
    }
  }
  return out;
}

function hirJson(mod: { toJson(): string }): any {
  return JSON.parse(mod.toJson());
}

function oldDoc(newDoc: any): any {
  return {
    version: newDoc.version,
    module: {
      ...newDoc.module,
      stmts: desugarStmts(newDoc.module.stmts),
    },
  };
}

function assertByteIdentical(mod: { toJson(): string }): void {
  const old = oldDoc(hirJson(mod));
  assert.deepEqual(runLower(mod.toJson()), runLower(JSON.stringify(old)));
}

// --- Nesting inside when / once / whileLoop (4 nodes x 3 contexts) ------

test("C11-05: until inside when is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      until(frame.eq(9), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: until inside once is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      until(frame.eq(9), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: until inside whileLoop is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      until(frame.eq(9), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside when is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      onRising(frame.eq(2), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside once is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      onRising(frame.eq(2), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (falling) inside whileLoop is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      onFalling(frame.eq(2), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside when is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      every(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside once is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      every(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside whileLoop is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      every(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside when is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      after(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside once is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    once(frame.eq(1), () => {
      after(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside whileLoop is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    whileLoop(frame.eq(1), () => {
      after(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

// --- Node inside node (full 4x4 matrix) ----------------------------------
// Outer conds: until frame.eq(9), edge frame.eq(2), every 3, after 2.
// Inner conds differ (until 8, edge 3, every 2, after 3) so inner and
// outer state is distinguishable in the output. The four every-outer
// cases have no top-level act run before the outer `every` (see the
// header note on the merge-backward rule); all other cases do.

test("C11-05: until inside until is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      until(frame.eq(8), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: until inside edge (rising) is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    onRising(frame.eq(2), () => {
      until(frame.eq(8), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: until inside every is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    every(3, () => {
      until(frame.eq(8), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: until inside after is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    after(2, () => {
      until(frame.eq(8), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside until is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      onRising(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside edge (falling) is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    onFalling(frame.eq(2), () => {
      onRising(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside every is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    every(3, () => {
      onRising(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: edge (rising) inside after is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    after(2, () => {
      onRising(frame.eq(3), () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside until is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      every(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside edge (rising) is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    onRising(frame.eq(2), () => {
      every(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside every is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    every(3, () => {
      every(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: every inside after is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    after(2, () => {
      every(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside until is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      after(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside edge (rising) is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    onRising(frame.eq(2), () => {
      after(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside every is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    every(3, () => {
      after(2, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: after inside after is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    after(2, () => {
      after(3, () => {
        count.iadd(1);
      });
    });
  });
  assertByteIdentical(mod);
});

// --- 3-deep nesting + recursion proof --------------------------------------

test("C11-05: every inside when inside until (3-deep) is byte-identical", () => {
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      when(frame.eq(1), () => {
        every(2, () => {
          count.iadd(1);
        });
      });
    });
  });
  assertByteIdentical(mod);
});

test("C11-05: recursion proof — nested whens inside an until body still flatten", () => {
  // If desugar did not recurse, the Until arm would push the raw
  // expansion without optimizing it, so the nested whens below would
  // survive unflattened and lower to a different trigger structure
  // than the old program (whose plain whens opt flattens). Byte
  // equality therefore proves the desugar recursed into the body.
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);
    frame.iadd(1);
    until(frame.eq(9), () => {
      when(frame.eq(1), () => {
        when(frame.eq(2), () => {
          count.iadd(1);
        });
      });
    });
  });
  assertByteIdentical(mod);
});
