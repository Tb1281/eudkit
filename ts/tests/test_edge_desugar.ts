/**
 * C11-03: edge node emission + opt desugar byte-identity tests.
 *
 * Each program is built twice: once with the `onRising` / `onFalling`
 * node emission, once with the old desugar written out by hand
 * (`Var` + `when` calls, same allocation order, same statement order).
 * The lowered LIR output of both must be byte-identical, and the sim
 * traces over a toggling input must match. LIR JSON carries no HIR
 * trigger ids (lowering uses positional indices), so LIR equality is
 * compiled-output equality.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { runSim } from "./helpers.ts";
import { fileURLToPath } from "node:url";

import {
  Var,
  buildModule,
  onFalling,
  onRising,
  when,
} from "../index.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runTool(args: string[], hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-edge-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    return execFileSync(EUDKIT_COMPILE, [args[0], hirPath, ...(args.slice(1))], {
      encoding: "utf-8",
    }).trim();
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function runLower(hirJson: string): string {
  return runTool(["lower"], hirJson);
}

/**
 * Shared program shape, built two ways. Allocation order (identical in
 * both): frame, input, riseCount, fallCount, currR, prevR, currF, prevF,
 * after. Statement order (identical in both): frame increment, input
 * driver whens, rising block, falling block, trailing set.
 */
function buildNodeProgram() {
  return buildModule(() => {
    const frame = Var(0);
    const input = Var(0);
    const riseCount = Var(0);
    const fallCount = Var(0);

    frame.iadd(1);
    when(frame.eq(1), () => {
      input.set(1);
    });
    when(frame.eq(2), () => {
      input.set(0);
    });
    when(frame.eq(3), () => {
      input.set(1);
    });

    onRising(input.eq(1), () => {
      riseCount.iadd(1);
    });
    onFalling(input.eq(1), () => {
      fallCount.iadd(1);
    });

    const after = Var(0);
    after.set(7);
  });
}

function buildBaselineProgram() {
  return buildModule(() => {
    const frame = Var(0);
    const input = Var(0);
    const riseCount = Var(0);
    const fallCount = Var(0);

    frame.iadd(1);
    when(frame.eq(1), () => {
      input.set(1);
    });
    when(frame.eq(2), () => {
      input.set(0);
    });
    when(frame.eq(3), () => {
      input.set(1);
    });

    // Rising desugar, written out by hand (today's DSL shape).
    const currR = Var(0);
    const prevR = Var(0);
    currR.set(0);
    when(input.eq(1), () => {
      currR.set(1);
    });
    when(currR.eq(1), () => {
      when(prevR.eq(0), () => {
        riseCount.iadd(1);
      });
    });
    when(currR.eq(1), () => {
      prevR.set(1);
    });
    when(currR.eq(0), () => {
      prevR.set(0);
    });

    // Falling desugar: same gates, fire polarities mirrored.
    const currF = Var(0);
    const prevF = Var(0);
    currF.set(0);
    when(input.eq(1), () => {
      currF.set(1);
    });
    when(currF.eq(0), () => {
      when(prevF.eq(1), () => {
        fallCount.iadd(1);
      });
    });
    when(currF.eq(1), () => {
      prevF.set(1);
    });
    when(currF.eq(0), () => {
      prevF.set(0);
    });

    const after = Var(0);
    after.set(7);
  });
}

test("edge nodes are emitted (not desugared) by the DSL", () => {
  const doc = JSON.parse(buildNodeProgram().toJson()) as {
    module: { stmts: { kind: string; direction?: string }[] };
  };
  const edges = doc.module.stmts.filter((s) => s.kind === "edge");
  assert.equal(edges.length, 2);
  assert.deepEqual(
    edges.map((e) => e.direction),
    ["rising", "falling"],
  );
});

test("edge node emission lowers byte-identically to the hand desugar", () => {
  const nodeJson = buildNodeProgram().toJson();
  const baselineJson = buildBaselineProgram().toJson();
  assert.equal(runLower(nodeJson), runLower(baselineJson));
});

test("edge node emission simulates identically over a toggling input", () => {
  const nodeJson = buildNodeProgram().toJson();
  const baselineJson = buildBaselineProgram().toJson();
  // Loops: 1: input 0->1 (rising fires); 2: 1->0 (falling fires);
  // 3: 0->1 (rising fires); 4: input stays 1 (no edge).
  for (const loops of [1, 2, 3, 4]) {
    assert.deepEqual(
      runSim(nodeJson, loops).var_values,
      runSim(baselineJson, loops).var_values,
      `sim traces diverge at ${loops} loops`,
    );
  }
  const rep4 = runSim(nodeJson, 4);
  // Vars: frame, input, riseCount, fallCount, currR, prevR, currF, prevF, after.
  assert.deepEqual(rep4.var_values.slice(0, 4), [4, 1, 2, 1]);
  assert.equal(rep4.var_values[8], 7);
});

test("edge inside when and edge inside edge lower and simulate", () => {
  const nodeMod = buildModule(() => {
    const frame = Var(0);
    const input = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      input.set(1);
    });
    when(frame.eq(2), () => {
      input.set(0);
    });
    when(frame.ge(1), () => {
      onFalling(input.eq(1), () => {
        onRising(input.eq(1), () => {
          count.iadd(1);
        });
      });
    });
  });
  const baselineMod = buildModule(() => {
    const frame = Var(0);
    const input = Var(0);
    const count = Var(0);
    frame.iadd(1);
    when(frame.eq(1), () => {
      input.set(1);
    });
    when(frame.eq(2), () => {
      input.set(0);
    });
    when(frame.ge(1), () => {
      const currO = Var(0);
      const prevO = Var(0);
      currO.set(0);
      when(input.eq(1), () => {
        currO.set(1);
      });
      when(currO.eq(0), () => {
        when(prevO.eq(1), () => {
          const currI = Var(0);
          const prevI = Var(0);
          currI.set(0);
          when(input.eq(1), () => {
            currI.set(1);
          });
          when(currI.eq(1), () => {
            when(prevI.eq(0), () => {
              count.iadd(1);
            });
          });
          when(currI.eq(1), () => {
            prevI.set(1);
          });
          when(currI.eq(0), () => {
            prevI.set(0);
          });
        });
      });
      when(currO.eq(1), () => {
        prevO.set(1);
      });
      when(currO.eq(0), () => {
        prevO.set(0);
      });
    });
  });
  const nodeJson = nodeMod.toJson();
  const baselineJson = baselineMod.toJson();
  assert.equal(runLower(nodeJson), runLower(baselineJson));
  for (const loops of [1, 2, 3]) {
    assert.deepEqual(
      runSim(nodeJson, loops).var_values,
      runSim(baselineJson, loops).var_values,
      `nested sim traces diverge at ${loops} loops`,
    );
  }
});
