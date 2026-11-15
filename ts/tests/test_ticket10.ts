import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  MAX_FOLD_CHUNKS,
  Var,
  buildModule,
  memcpyEPD,
  memsetEPD,
} from "../index.ts";

import { REPO_ROOT, readDword, runSim, runStatic, type HirStmtNode } from "./helpers.ts";

const FILL = 0xaabbccdd;
const TRIGGER_BYTES = 2408;

interface Report {
  static_cost: {
    triggers: number;
    trigger_bytes: number;
    total_bytes: number;
  };
  executed_cost: { triggers: number };
}

interface ParsedModule {
  module: { stmts: HirStmtNode[]; vars?: { id: number }[] };
}

function parseModule(hirJson: string): ParsedModule {
  return JSON.parse(hirJson) as ReturnType<typeof parseModule>;
}

function report(prog: () => void): Report {
  return runSim(buildModule(prog).toJson(), 1) as unknown as Report;
}

function kinds(mod: ParsedModule): string[] {
  return mod.module.stmts.map((stmt) => stmt.kind);
}

test("constant fills emit one packed SetDeaths trigger per 64 dwords", () => {
  for (const [count, triggers] of [
    [2, 1],
    [64, 1],
    [65, 2],
    [448, 7],
    [1024, 16],
  ] as const) {
    const result = report(() => memsetEPD(3000, FILL, count));
    assert.equal(result.static_cost.triggers, triggers, `count ${count}`);
    assert.equal(result.static_cost.trigger_bytes, triggers * TRIGGER_BYTES);
    assert.equal(result.static_cost.total_bytes, triggers * TRIGGER_BYTES);
    assert.equal(result.executed_cost.triggers, triggers);
  }
});

test("constant fills write every dword and stop after the last one", () => {
  for (const count of [2, 63, 64, 65]) {
    const result = runSim(
      buildModule(() => memsetEPD(3000, FILL, count)).toJson(),
      1,
    );
    for (let i = 0; i < count; i += 1) {
      assert.equal(readDword(result, 3000 + i), FILL, `count ${count} dword ${i}`);
    }
    assert.equal(
      readDword(result, 3000 + count),
      0,
      "the fill must not write past its last dword",
    );
    if (count === 64) {
      // The chunking boundary: dword 63 is in the packed trigger, dword 64 is
      // not written at all.
      assert.equal(readDword(result, 3000 + 63), FILL);
      assert.equal(readDword(result, 3000 + 64), 0);
    }
  }
});

test("count 449 now folds to 8T instead of the 7T loop", () => {
  const mod = buildModule(() => memsetEPD(3000, FILL, 449));
  const result = runSim(mod.toJson(), 1);
  assert.equal(result.static_cost.triggers, 8, "449 must fold to 8T, not keep the loop");
  assert.equal(result.static_cost.trigger_bytes, 8 * TRIGGER_BYTES);
  assert.equal(result.static_cost.total_bytes, 8 * TRIGGER_BYTES);
  assert.equal(result.executed_cost.triggers, 8);
  const parsed = parseModule(mod.toJson());
  assert.ok(
    !kinds(parsed).includes("while"),
    "a folded fill must not lower to the runtime loop",
  );
  assert.equal(parsed.module.vars, undefined, "a folded fill allocates no vars");
  assert.equal(readDword(result, 3000), FILL);
  assert.equal(readDword(result, 3000 + 448), FILL);
  assert.equal(readDword(result, 3000 + 449), 0);
});

test("count 1024 folds to 16T", () => {
  const result = report(() => memsetEPD(3000, FILL, 1024));
  assert.equal(result.static_cost.triggers, 16);
  assert.equal(result.static_cost.trigger_bytes, 16 * TRIGGER_BYTES);
  assert.equal(result.static_cost.total_bytes, 16 * TRIGGER_BYTES);
  assert.equal(result.executed_cost.triggers, 16);
  const sim = runSim(buildModule(() => memsetEPD(3000, FILL, 1024)).toJson(), 1);
  assert.equal(readDword(sim, 3000 + 1023), FILL);
  assert.equal(readDword(sim, 3000 + 1024), 0);
});

test("the fold cap is 512 chunks", () => {
  const count = MAX_FOLD_CHUNKS * 64;
  const mod = buildModule(() => memsetEPD(3000, FILL, count));
  const result = runSim(mod.toJson(), 1);
  assert.equal(result.static_cost.triggers, MAX_FOLD_CHUNKS);
  assert.equal(result.static_cost.trigger_bytes, MAX_FOLD_CHUNKS * TRIGGER_BYTES);
  assert.equal(result.static_cost.total_bytes, MAX_FOLD_CHUNKS * TRIGGER_BYTES);
  assert.equal(result.executed_cost.triggers, MAX_FOLD_CHUNKS);
  const parsed = parseModule(mod.toJson());
  assert.ok(!kinds(parsed).includes("while"));
  assert.equal(parsed.module.vars, undefined);
  assert.equal(readDword(result, 3000), FILL);
  assert.equal(readDword(result, 3000 + count - 1), FILL);
  assert.equal(readDword(result, 3000 + count), 0);
});

test("count past the cap keeps the loop (HIR shape and static cost only)", () => {
  // Do NOT simulate: the native simulator panics at 100,000 executed triggers
  // and this loop needs ~196,616. Assert the HIR shape and the static cost
  // only (`dump-ir --json --no-exec`).
  const count = MAX_FOLD_CHUNKS * 64 + 1;
  const mod = buildModule(() => memsetEPD(3000, FILL, count));
  const parsed = parseModule(mod.toJson());
  assert.ok(
    kinds(parsed).includes("while"),
    "an over-cap fill must still lower to the runtime loop",
  );
  // Cycle 27 #01: the destination cursor is the copy trigger's dest field in
  // real memory, and the constant count is initialised straight into the
  // counter var, so only the counter is allocated.
  assert.equal(parsed.module.vars?.length, 1, "counter only");
  const staticCost = runStatic(mod.toJson()).static_cost;
  // Cycle 27 #01: the runtime-zero guard is emitted for every destination
  // form, so the non-folded loop pays one extra trigger.
  assert.equal(staticCost.triggers, 6);
  assert.equal(staticCost.trigger_bytes, 6 * TRIGGER_BYTES);
  assert.equal(staticCost.total_bytes, 6 * TRIGGER_BYTES + 4);
});

test("variable count and variable value keep the runtime loop", () => {
  const countReport = report(() => {
    const n = Var(2);
    memsetEPD(3000, 0x11223344, n);
  });
  // Cycle 27 #01: the destination advances through the copy trigger's dest
  // field, so the loop no longer pays a per-pass dst-cursor store. The
  // runtime-zero guard is emitted for every destination form, which adds one
  // static and one executed trigger (8T / 13T).
  assert.equal(countReport.static_cost.triggers, 8);
  assert.equal(countReport.static_cost.trigger_bytes, 8 * TRIGGER_BYTES);
  assert.equal(countReport.static_cost.total_bytes, 8 * TRIGGER_BYTES + 4);
  assert.equal(countReport.executed_cost.triggers, 13);
  const countSim = runSim(
    buildModule(() => {
      const n = Var(2);
      memsetEPD(3000, 0x11223344, n);
    }).toJson(),
    1,
  );
  assert.equal(readDword(countSim, 3000), 0x11223344);
  assert.equal(readDword(countSim, 3001), 0x11223344);
  assert.equal(readDword(countSim, 3002), 0);

  const valueReport = report(() => {
    const v = Var(FILL);
    memsetEPD(3000, v, 2);
  });
  assert.equal(valueReport.static_cost.triggers, 8);
  assert.equal(valueReport.static_cost.trigger_bytes, 8 * TRIGGER_BYTES);
  assert.equal(valueReport.static_cost.total_bytes, 8 * TRIGGER_BYTES + 4);
  assert.equal(valueReport.executed_cost.triggers, 15);
  const valueSim = runSim(
    buildModule(() => {
      const v = Var(FILL);
      memsetEPD(3000, v, 2);
    }).toJson(),
    1,
  );
  assert.equal(readDword(valueSim, 3000), FILL);
  assert.equal(readDword(valueSim, 3001), FILL);
  assert.equal(readDword(valueSim, 3002), 0);
});

test("constant count 1 emits the copy body once, with no loop or counter", () => {
  // #97: the CP-cursor dword read inside `emitCopy` goes through the shared
  // reader, and the optimizer inlines its single call site, so the 33T reader
  // body is the whole read cost (the old per-site inline read was 40T: a
  // 32-act accumulator reset + 31 bit tests + an EPD-base subtract). Every
  // further read in the module costs 5T static instead of 33T (see
  // `test_shared_reader_cost_is_per_site`).
  const mod = buildModule(() => memcpyEPD(4000, 3000, 1));
  const result = runSim(mod.toJson(), 1);
  assert.equal(result.static_cost.triggers, 38);
  assert.equal(result.static_cost.trigger_bytes, 91_504);
  assert.equal(result.static_cost.total_bytes, 91_508);
  assert.equal(result.executed_cost.triggers, 39);

  const parsed = parseModule(mod.toJson());
  assert.ok(
    !kinds(parsed).includes("while"),
    "a count of 1 must not emit a while_loop",
  );
  // Three block-copy vars plus the inlined reader body's result, which the
  // fusion lands directly in the copy destination (#97); still no
  // remaining-count var.
  assert.equal(
    parsed.module.vars?.length,
    5,
    "a count of 1 must not allocate the remaining-count var",
  );
});

test("constant count 1 copies exactly one dword", () => {
  const result = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xdeadbeef, 4);
      memcpyEPD(4000, 3000, 1);
    }).toJson(),
    1,
  );
  assert.equal(readDword(result, 4000), 0xdeadbeef);
  assert.equal(
    readDword(result, 4001),
    0,
    "count 1 must leave the next dword untouched",
  );
});

test("constant count 2 keeps the runtime loop", () => {
  // #97: same shared-reader re-baseline as the count-1 case above; the loop
  // body still carries exactly one read call site, and that site is inlined.
  // Cycle 27 #01: the source cursor is CurrentPlayer itself and the
  // destination is the copy trigger's dest field, so the executed loop no
  // longer stores two cursor vars (84T -> 81T).
  const mod = buildModule(() => memcpyEPD(4000, 3000, 2));
  const result = runSim(mod.toJson(), 1);
  assert.equal(result.static_cost.triggers, 41);
  assert.equal(result.static_cost.trigger_bytes, 98_728);
  assert.equal(result.static_cost.total_bytes, 98_736);
  assert.equal(result.executed_cost.triggers, 81);
  assert.ok(kinds(parseModule(mod.toJson())).includes("while"));

  const looped = runSim(
    buildModule(() => {
      memsetEPD(3000, 0xdeadbeef, 4);
      memcpyEPD(4000, 3000, 2);
    }).toJson(),
    1,
  );
  assert.equal(readDword(looped, 4000), 0xdeadbeef);
  assert.equal(readDword(looped, 4001), 0xdeadbeef);
  assert.equal(readDword(looped, 4002), 0);
});

test("zero count still no-ops before value validation", () => {
  const mod = buildModule(() => {
    memcpyEPD(9000, 1000, 0);
    memsetEPD(9001, "bad" as unknown as number, 0);
  });
  const result = runSim(mod.toJson(), 1);
  assert.equal(result.static_cost.triggers, 0);
  assert.equal(result.static_cost.total_bytes, 0);
});

test("validation order stays dst then count then value", () => {
  // dstEPD is validated first: the invalid type wins over the bad count.
  assert.throws(
    () =>
      buildModule(() => {
        memsetEPD(-1, "bad" as unknown as number, -1);
      }),
    /EUDSL-TYPE/,
  );
  // count is validated before value: the range error wins over the type.
  assert.throws(
    () =>
      buildModule(() => {
        memsetEPD(3000, "bad" as unknown as number, -1);
      }),
    /EUDSL-RANGE/,
  );
  // ... and value is validated last, after the zero-count early return.
  assert.throws(
    () =>
      buildModule(() => {
        memsetEPD(3000, "bad" as unknown as number, 1);
      }),
    /EUDSL-TYPE/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        memcpyEPD(-1, 3000, 1);
      }),
    /EUDSL-TYPE/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        memcpyEPD(4000, 3000, -1);
      }),
    /EUDSL-RANGE/,
  );
  // Constant counts above 2^32-1 are rejected in both primitives, with the
  // same message; 0xFFFFFFFF itself is still accepted.
  assert.throws(
    () =>
      buildModule(() => {
        memsetEPD(3000, 0xff, 0x100000000);
      }),
    /EUDSL-RANGE: memsetEPD\(\): count must be at most 0xFFFFFFFF dwords, got 4294967296/,
  );
  assert.throws(
    () =>
      buildModule(() => {
        memcpyEPD(4000, 3000, 0x100000000);
      }),
    /EUDSL-RANGE: memcpyEPD\(\): count must be at most 0xFFFFFFFF dwords, got 4294967296/,
  );
});

test("0xFFFFFFFF count is accepted and keeps the loop", () => {
  const mod = buildModule(() => memsetEPD(3000, 0xff, 0xffffffff));
  assert.ok(
    kinds(parseModule(mod.toJson())).includes("while"),
    "0xFFFFFFFF dwords is over the fold cap, so it keeps the loop",
  );
});

test("Byte-identical HIR parity for the constant-fill fold", () => {
  for (const count of [449, 1024, MAX_FOLD_CHUNKS * 64]) {
    const tsHir = buildModule(() => memsetEPD(3000, FILL, count)).toJson();
    const pyScript = [
      "import sys",
      "from py import build_module, memsetEPD",
      `count = ${count}`,
      "sys.stdout.write(build_module(lambda: memsetEPD(3000, 0xAABBCCDD, count)).to_json())",
    ].join("\n");
    const pyHir = execFileSync("python3", ["-c", pyScript], {
      cwd: REPO_ROOT,
      encoding: "utf-8",
      maxBuffer: 64 * 1024 * 1024,
    });
    assert.equal(tsHir, pyHir, `count ${count} must be byte-identical`);
  }
});

test("both frontends reject constant counts above 2^32-1 identically", () => {
  const pyScript = [
    "from py import build_module, memcpyEPD, memsetEPD",
    "for name, call in (",
    "    ('memset', lambda: memsetEPD(3000, 0xFF, 0x100000000)),",
    "    ('memcpy', lambda: memcpyEPD(4000, 3000, 0x100000000)),",
    "):",
    "    try:",
    "        build_module(call)",
    "    except Exception as exc:",
    "        print(exc)",
    "    else:",
    "        raise SystemExit(f'{name} did not raise')",
  ].join("\n");
  const lines = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  })
    .trim()
    .split("\n");
  assert.deepEqual(lines, [
    "EUDSL-RANGE: memsetEPD(): count must be at most 0xFFFFFFFF dwords, got 4294967296",
    "EUDSL-RANGE: memcpyEPD(): count must be at most 0xFFFFFFFF dwords, got 4294967296",
  ]);
});
