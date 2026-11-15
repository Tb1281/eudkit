/**
 * Ticket #51 / cycle 5 #07: `dispatch(value, cases, fallback)`.
 *
 * `dispatch` is a frontend-only multi-way integer branch over the existing HIR:
 *
 * - a compile-time plain-integer `value` folds to the selected arm, the
 *   fallback, or nothing (no `when` node at all);
 * - a runtime value with `1 <= N <= 9` cases lowers to a nested
 *   `when(..., else_body)` chain in caller order;
 * - a runtime value with `N >= 10` cases lowers to a balanced `HirWhen` search
 *   tree (`ge(mid)` internal nodes, `eq(key)` leaves);
 * - keys are compile-time plain integers, normalised to 32 bits and unique;
 *   a miss without a fallback is a documented no-op;
 * - the fallback is named `fallback` (not TypeScript's reserved `default`).
 *
 * The cost pins below use one fixture: a page-backed `Var` (the `1T / 64A /
 * 2,408 B` seed) and arms `SetDeaths(9000 + k, SetTo, k, 0)`. The same probes
 * live in `py/tests/test_dispatch.py`, and the parity test compares byte-for-
 * byte HIR JSON between the two frontends.
 *
 * Red before the construct exists: the named `dispatch` import fails.
 */

import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import test from "node:test";

import {
  REPO_ROOT,
  flattenStmts,
  runSim,
  runStatic,
  type SimReport,
} from "./helpers.ts";
import {
  AtLeast,
  EudslError,
  Exactly,
  SetDeaths,
  SetTo,
  Var,
  buildModule,
  dispatch,
} from "../index.ts";

const STATIC_KEYS = ["triggers", "conditions", "actions", "total_bytes"] as const;
const EXEC_KEYS = ["triggers", "conditions", "actions"] as const;
const FALLBACK_DEST = 9999;

function staticCost(hirJson: string): number[] {
  const cost = runStatic(hirJson).static_cost;
  return STATIC_KEYS.map((key) => cost[key]);
}

function execCost(report: SimReport): number[] {
  const cost = report.executed_cost;
  return EXEC_KEYS.map((key) => cost[key]);
}

function epdKeys(report: SimReport): Set<number> {
  const epds =
    (report as { external_epds?: Record<string, number> }).external_epds ?? {};
  return new Set(Object.keys(epds).map(Number));
}

interface RuntimeOptions {
  init?: number;
  keys?: number[];
  fallback?: boolean;
  destBase?: number;
}

interface RuntimeBuild {
  mod: ReturnType<typeof buildModule>;
  report: SimReport;
}

function runtimeModule(n: number, options: RuntimeOptions = {}): RuntimeBuild {
  const init = options.init ?? 0;
  const destBase = options.destBase ?? 9000;
  const keys = options.keys ?? Array.from({ length: n }, (_, k) => k);
  const mod = buildModule(() => {
    const value = Var(init, undefined, undefined, "page");
    const cases: Array<readonly [number, () => void]> = keys.map(
      (key) =>
        [
          key,
          () => {
            SetDeaths(destBase + key, SetTo, key, 0);
          },
        ] as const,
    );
    dispatch(
      value,
      cases,
      options.fallback
        ? () => {
            SetDeaths(FALLBACK_DEST, SetTo, 1, 0);
          }
        : undefined,
    );
  });
  return { mod, report: runSim(mod.toJson(), 1) };
}

function whenNodes(mod: ReturnType<typeof buildModule>): any[] {
  return JSON.parse(mod.toJson()).module.stmts.filter(
    (stmt: any) => stmt.kind === "when",
  );
}

function allWhenNodes(mod: ReturnType<typeof buildModule>): any[] {
  const stmts = JSON.parse(mod.toJson()).module.stmts as any[];
  return flattenStmts(stmts).filter((stmt) => stmt.kind === "when");
}

function condSignature(node: any): [number, number] {
  const cond = node.conds[0];
  return [cond.amount, cond.comparison];
}

function targets(n: number): Record<string, number> {
  return {
    hit0: 0,
    hit1: 1,
    mid: Math.floor(n / 2),
    last: n - 1,
    miss: n + 5,
  };
}

const CHAIN_PINS: Record<
  number,
  { static: number[]; exec: Record<string, number[]> }
> = {
  4: {
    static: [8, 4, 74, 19_264],
    exec: {
      hit0: [2, 1, 3],
      hit1: [3, 2, 3],
      mid: [4, 3, 3],
      last: [4, 4, 1],
      miss: [4, 4, 0],
    },
  },
  8: {
    static: [16, 8, 86, 38_528],
    exec: {
      hit0: [2, 1, 3],
      hit1: [3, 2, 3],
      mid: [6, 5, 3],
      last: [8, 8, 1],
      miss: [8, 8, 0],
    },
  },
  9: {
    static: [18, 9, 89, 43_344],
    exec: {
      hit0: [2, 1, 3],
      hit1: [3, 2, 3],
      mid: [6, 5, 3],
      last: [9, 9, 1],
      miss: [9, 9, 0],
    },
  },
};

const BST_PINS: Record<
  number,
  { static: number[]; exec: Record<string, number[]> }
> = {
  10: {
    static: [29, 19, 92, 69_832],
    exec: {
      hit0: [4, 4, 1],
      hit1: [5, 4, 3],
      mid: [5, 4, 3],
      last: [9, 5, 9],
      miss: [9, 5, 8],
    },
  },
  16: {
    static: [47, 31, 110, 113_176],
    exec: {
      hit0: [5, 5, 1],
      hit1: [6, 5, 3],
      mid: [6, 5, 3],
      last: [9, 5, 9],
      miss: [9, 5, 8],
    },
  },
  32: {
    static: [95, 63, 158, 228_760],
    exec: {
      hit0: [6, 6, 1],
      hit1: [7, 6, 3],
      mid: [7, 6, 3],
      last: [11, 6, 11],
      miss: [11, 6, 10],
    },
  },
};

const BST_FALLBACK_PINS: Record<
  number,
  { static: number[]; exec: Record<string, number[]> }
> = {
  10: {
    static: [49, 19, 122, 117_992],
    exec: {
      hit0: [5, 4, 3],
      hit1: [6, 4, 5],
      mid: [6, 4, 5],
      last: [10, 5, 11],
      miss: [10, 5, 9],
    },
  },
};

const CONTROL_HIR =
  '{"version":"3.2","module":{"vars":[{"id":0,"init":0,"storage":"page"},' +
  '{"id":1,"init":0}],"stmts":[{"kind":"trigger","id":0,"conds":[],"acts":[' +
  '{"mask":4294967295,"strid":0,"wavid":0,"time":0,"dest":0,"value":1,' +
  '"unit":0,"acttype":45,"modifier":8,"flags":20,"padding":0,"eudx":17235,' +
  '"relocs":[{"field_offset":16,"target_var":1,"target_offset":20,' +
  '"mode":1}]}],"preserved":true,"flags":4}]}}';

function expectError(
  code: string,
  fn: () => void,
  contains?: string,
): void {
  assert.throws(
    fn,
    (err: unknown) =>
      err instanceof EudslError &&
      err.code === code &&
      (contains === undefined || err.message.includes(contains)),
  );
}

test("dispatch is exported by the barrel", () => {
  assert.equal(typeof dispatch, "function");
});

test("constant value folds to matching arm", () => {
  const mod = buildModule(() => {
    dispatch(3, [
      [1, () => SetDeaths(9001, SetTo, 1, 0)],
      [3, () => SetDeaths(9003, SetTo, 3, 0)],
    ]);
  });
  const stmts = JSON.parse(mod.toJson()).module.stmts as any[];
  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    ["act"],
  );
  assert.equal(stmts[0].dest, 9003);
  assert.equal(stmts[0].value, 3);
  assert.deepEqual(whenNodes(mod), []);
  assert.deepEqual(staticCost(mod.toJson()), [1, 0, 1, 2_408]);
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(execCost(report), [1, 0, 1]);
  assert.deepEqual(epdKeys(report), new Set([9003]));
});

test("constant value folds to fallback", () => {
  const mod = buildModule(() => {
    dispatch(
      7,
      [[1, () => SetDeaths(9001, SetTo, 1, 0)]],
      () => SetDeaths(FALLBACK_DEST, SetTo, 1, 0),
    );
  });
  const stmts = JSON.parse(mod.toJson()).module.stmts as any[];
  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    ["act"],
  );
  assert.equal(stmts[0].dest, FALLBACK_DEST);
  assert.deepEqual(whenNodes(mod), []);
  assert.deepEqual(staticCost(mod.toJson()), [1, 0, 1, 2_408]);
  assert.deepEqual(execCost(runSim(mod.toJson(), 1)), [1, 0, 1]);
});

test("constant value miss without fallback is a no-op", () => {
  const mod = buildModule(() => {
    dispatch(7, [[1, () => SetDeaths(9001, SetTo, 1, 0)]]);
  });
  assert.deepEqual(JSON.parse(mod.toJson()).module.stmts, []);
  assert.deepEqual(whenNodes(mod), []);
  assert.deepEqual(staticCost(mod.toJson()), [0, 0, 0, 0]);
  assert.deepEqual(execCost(runSim(mod.toJson(), 1)), [0, 0, 0]);
});

test("constant fold does not run unselected bodies", () => {
  const calls: string[] = [];
  buildModule(() => {
    dispatch(3, [
      [
        1,
        () => {
          calls.push("unselected");
          SetDeaths(9001, SetTo, 1, 0);
        },
      ],
      [
        3,
        () => {
          calls.push("selected");
          SetDeaths(9003, SetTo, 3, 0);
        },
      ],
    ]);
  });
  assert.deepEqual(calls, ["selected"]);
});

test("N=1 runtime shape and cost", () => {
  const { mod, report } = runtimeModule(1, { init: 0 });
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  assert.deepEqual(condSignature(nodes[0]), [0, Exactly]);
  assert.equal("else_body" in nodes[0], false);
  assert.deepEqual(staticCost(mod.toJson()), [2, 1, 65, 4_816]);
  assert.deepEqual(execCost(report), [1, 1, 1]);
  assert.deepEqual(execCost(runtimeModule(1, { init: 2 }).report), [1, 1, 0]);
});

test("N=1 runtime fallback", () => {
  const { mod, report } = runtimeModule(1, { init: 0, fallback: true });
  const node = whenNodes(mod)[0];
  assert.ok("else_body" in node);
  assert.deepEqual(
    node.else_body.map((stmt: any) => stmt.kind),
    ["act"],
  );
  assert.deepEqual(staticCost(mod.toJson()), [4, 1, 68, 9_632]);
  assert.deepEqual(execCost(report), [2, 1, 3]);
  assert.deepEqual(
    execCost(runtimeModule(1, { init: 2, fallback: true }).report),
    [2, 1, 1],
  );
});

test("N=4 else-chain shape and cost", () => {
  const pins = CHAIN_PINS[4];
  const { mod } = runtimeModule(4, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const chain: Array<[number, number]> = [];
  let node: any = nodes[0];
  while (node !== undefined) {
    chain.push(condSignature(node));
    const elseBody = node.else_body;
    node =
      elseBody && elseBody[0]?.kind === "when" ? elseBody[0] : undefined;
  }
  assert.deepEqual(chain, [
    [0, Exactly],
    [1, Exactly],
    [2, Exactly],
    [3, Exactly],
  ]);
  for (const [name, target] of Object.entries(targets(4))) {
    assert.deepEqual(
      execCost(runtimeModule(4, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("N=8 else-chain shape and cost", () => {
  const pins = CHAIN_PINS[8];
  const { mod } = runtimeModule(8, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  for (const [name, target] of Object.entries(targets(8))) {
    assert.deepEqual(
      execCost(runtimeModule(8, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("N=9 else-chain shape and cost", () => {
  const pins = CHAIN_PINS[9];
  const { mod } = runtimeModule(9, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  const chain: Array<[number, number]> = [];
  let node: any = nodes[0];
  let lastNode: any = nodes[0];
  while (node !== undefined) {
    lastNode = node;
    chain.push(condSignature(node));
    const elseBody = node.else_body;
    node =
      elseBody && elseBody[0]?.kind === "when" ? elseBody[0] : undefined;
  }
  assert.deepEqual(
    chain,
    Array.from({ length: 9 }, (_, key) => [key, Exactly]),
  );
  assert.equal("else_body" in lastNode, false);
  for (const [name, target] of Object.entries(targets(9))) {
    assert.deepEqual(
      execCost(runtimeModule(9, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("N=10 BST shape and cost", () => {
  const pins = BST_PINS[10];
  const { mod } = runtimeModule(10, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  assert.deepEqual(condSignature(whenNodes(mod)[0]), [5, AtLeast]);
  const nodes = allWhenNodes(mod);
  assert.equal(nodes.length, 19);
  assert.equal(
    nodes.filter((node) => condSignature(node)[1] === AtLeast).length,
    9,
  );
  assert.equal(
    nodes.filter((node) => condSignature(node)[1] === Exactly).length,
    10,
  );
  for (const [name, target] of Object.entries(targets(10))) {
    assert.deepEqual(
      execCost(runtimeModule(10, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("N=16 BST shape and cost", () => {
  const pins = BST_PINS[16];
  const { mod } = runtimeModule(16, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  for (const [name, target] of Object.entries(targets(16))) {
    assert.deepEqual(
      execCost(runtimeModule(16, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("N=32 BST shape and cost", () => {
  const pins = BST_PINS[32];
  const { mod } = runtimeModule(32, { init: 0 });
  assert.deepEqual(staticCost(mod.toJson()), pins.static);
  for (const [name, target] of Object.entries(targets(32))) {
    assert.deepEqual(
      execCost(runtimeModule(32, { init: target }).report),
      pins.exec[name],
      name,
    );
  }
});

test("fallback runs only on a miss", () => {
  for (const n of [4, 9, 10]) {
    assert.deepEqual(
      epdKeys(runtimeModule(n, { init: 1, fallback: true }).report),
      new Set([9001]),
      `hit N=${n}`,
    );
    assert.deepEqual(
      epdKeys(runtimeModule(n, { init: n + 5, fallback: true }).report),
      new Set([FALLBACK_DEST]),
      `miss N=${n}`,
    );
  }
  const pins = BST_FALLBACK_PINS[10];
  assert.deepEqual(
    staticCost(runtimeModule(10, { init: 0, fallback: true }).mod.toJson()),
    pins.static,
  );
  for (const [name, target] of Object.entries(targets(10))) {
    assert.deepEqual(
      execCost(runtimeModule(10, { init: target, fallback: true }).report),
      pins.exec[name],
      name,
    );
  }
});

test("no fallback miss is a no-op", () => {
  const { report } = runtimeModule(10, { init: 15 });
  assert.deepEqual(epdKeys(report), new Set());
  assert.deepEqual(execCost(report), [9, 5, 8]);
});

test("arms are exclusive and do not cascade", () => {
  const mod = buildModule(() => {
    const value = Var(0, undefined, undefined, "page");
    dispatch(value, [
      [
        0,
        () => {
          value.set(1);
          SetDeaths(9000, SetTo, 1, 0);
        },
      ],
      [1, () => SetDeaths(9001, SetTo, 1, 0)],
    ]);
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(epdKeys(report), new Set([9000]));
});

test("duplicate key is a bounds error", () => {
  expectError(
    "EUDSL-BOUNDS",
    () =>
      buildModule(() => {
        dispatch(0, [
          [1, () => {}],
          [1, () => {}],
        ]);
      }),
    "duplicate key",
  );
  expectError(
    "EUDSL-BOUNDS",
    () =>
      buildModule(() => {
        dispatch(0, [
          [-1, () => {}],
          [0xffffffff, () => {}],
        ]);
      }),
    "duplicate key",
  );
});

test("empty cases is a bounds error even with a fallback", () => {
  expectError("EUDSL-BOUNDS", () =>
    buildModule(() => {
      dispatch(0, []);
    }),
  );
  expectError("EUDSL-BOUNDS", () =>
    buildModule(() => {
      dispatch(0, [], () => {});
    }),
  );
});

test("non-callable arm is a type error", () => {
  expectError("EUDSL-TYPE", () =>
    buildModule(() => {
      dispatch(0, [[0, 1]] as any);
    }),
  );
});

test("non-callable fallback is a type error", () => {
  expectError("EUDSL-TYPE", () =>
    buildModule(() => {
      dispatch(0, [[0, () => {}]], 1 as any);
    }),
  );
});

test("runtime key is a type error", () => {
  expectError(
    "EUDSL-TYPE",
    () =>
      buildModule(() => {
        const key = Var(0);
        dispatch(0, [[key, () => {}]] as any);
      }),
    "runtime keys are not supported",
  );
  expectError(
    "EUDSL-TYPE",
    () =>
      buildModule(() => {
        const base = Var(0);
        dispatch(0, [[base.add(1), () => {}]] as any);
      }),
    "runtime keys are not supported",
  );
});

test("bool or non-integer key is a type error", () => {
  for (const key of [true, 1.5, "1"]) {
    expectError("EUDSL-TYPE", () =>
      buildModule(() => {
        dispatch(0, [[key, () => {}]] as any);
      }),
    );
  }
});

test("invalid value is a type error", () => {
  for (const value of [1.5, "x", true, undefined, null]) {
    expectError("EUDSL-TYPE", () =>
      buildModule(() => {
        dispatch(value as any, [[0, () => {}]]);
      }),
    );
  }
});

test("scope error outside buildModule runs no body", () => {
  const calls: string[] = [];
  expectError("EUDSL-SCOPE", () => {
    dispatch(0, [
      [
        0,
        () => {
          calls.push("body");
        },
      ],
    ], () => {
      calls.push("fallback");
    });
  });
  assert.deepEqual(calls, []);
});

test("negative key normalises", () => {
  const viaNegative = buildModule(() => {
    dispatch(0xffffffff, [[-1, () => SetDeaths(9001, SetTo, 1, 0)]]);
  });
  const viaMasked = buildModule(() => {
    dispatch(0xffffffff, [
      [0xffffffff, () => SetDeaths(9002, SetTo, 2, 0)],
    ]);
  });
  const runtimeValue = buildModule(() => {
    const value = Var(0xffffffff, undefined, undefined, "page");
    dispatch(value, [[-1, () => SetDeaths(9003, SetTo, 3, 0)]]);
  });
  assert.deepEqual(epdKeys(runSim(viaNegative.toJson(), 1)), new Set([9001]));
  assert.deepEqual(epdKeys(runSim(viaMasked.toJson(), 1)), new Set([9002]));
  assert.deepEqual(epdKeys(runSim(runtimeValue.toJson(), 1)), new Set([9003]));
});

test("sparse keys select the correct arm", () => {
  const sparse = [0, 10, 20, 100, 1_000, 2_000, 3_000, 4_000, 5_000, 6_000];
  for (const key of sparse) {
    assert.deepEqual(
      epdKeys(runtimeModule(10, { init: key, keys: sparse }).report),
      new Set([9000 + key]),
      `key ${key}`,
    );
  }
  assert.deepEqual(
    epdKeys(runtimeModule(10, { init: 7, keys: sparse }).report),
    new Set(),
  );
});

test("Expr value is materialised exactly once", () => {
  const mod = buildModule(() => {
    const base = Var(1);
    const value = base.add(1);
    const cases: Array<readonly [number, () => void]> = [0, 1, 2, 3].map(
      (key) =>
        [
          key,
          () => {
            SetDeaths(9000 + key, SetTo, key, 0);
          },
        ] as const,
    );
    dispatch(value, cases);
  });
  const doc = JSON.parse(mod.toJson());
  // base + one materialised Expr temp; never one temp per case.
  assert.equal(doc.module.vars.length, 2);
  const nodes = whenNodes(mod);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].conds[0].relocs[0].target_var, 1);
});

test("dispatch-free control HIR is unchanged", () => {
  const control = buildModule(() => {
    Var(0, undefined, undefined, "page");
    const hit = Var(0);
    hit.iadd(1);
  });
  assert.equal(control.toJson(), CONTROL_HIR);

  const withDispatch = buildModule(() => {
    Var(0, undefined, undefined, "page");
    const hit = Var(0);
    hit.iadd(1);
    dispatch(0, [[0, () => hit.iadd(2)]]);
  });
  const stmts = JSON.parse(withDispatch.toJson()).module.stmts as any[];
  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    ["trigger", "act"],
  );
});

function buildParityModule(): ReturnType<typeof buildModule> {
  return buildModule(() => {
    const value = Var(0, undefined, undefined, "page");
    const arm = (key: number) => () => SetDeaths(9000 + key, SetTo, key, 0);
    const fallback = () => SetDeaths(FALLBACK_DEST, SetTo, 1, 0);
    const range = (n: number) =>
      Array.from({ length: n }, (_, k) => [k, arm(k)] as const);

    dispatch(value, range(1));
    dispatch(value, range(1), fallback);
    for (const n of [4, 8]) {
      dispatch(value, range(n));
      dispatch(value, range(n), fallback);
    }
    for (const n of [9, 10, 16, 32]) {
      dispatch(value, range(n));
    }
    for (const n of [9, 10]) {
      dispatch(value, range(n), fallback);
    }
    dispatch(3, [
      [1, arm(1)],
      [3, arm(3)],
    ]);
    dispatch(7, [[1, arm(1)]], fallback);
    dispatch(7, [[1, arm(1)]]);
    const sparse = [0, 10, 20, 100, 1_000, 2_000, 3_000, 4_000, 5_000, 6_000];
    dispatch(
      value,
      sparse.map((key) => [key, arm(key)] as const),
    );
  });
}

const PY_PARITY_SCRIPT = `
import sys
from py import SetDeaths, SetTo, Var, build_module, dispatch

def prog():
    value = Var(0, storage="page")
    def arm(key):
        return lambda: SetDeaths(9000 + key, SetTo, key, 0)
    def fallback():
        SetDeaths(9999, SetTo, 1, 0)

    dispatch(value, [(k, arm(k)) for k in range(1)])
    dispatch(value, [(k, arm(k)) for k in range(1)], fallback=fallback)
    for n in (4, 8):
        dispatch(value, [(k, arm(k)) for k in range(n)])
        dispatch(value, [(k, arm(k)) for k in range(n)], fallback=fallback)
    for n in (9, 10, 16, 32):
        dispatch(value, [(k, arm(k)) for k in range(n)])
    for n in (9, 10):
        dispatch(value, [(k, arm(k)) for k in range(n)], fallback=fallback)
    dispatch(3, [(1, arm(1)), (3, arm(3))])
    dispatch(7, [(1, arm(1))], fallback=fallback)
    dispatch(7, [(1, arm(1))])
    sparse = [0, 10, 20, 100, 1000, 2000, 3000, 4000, 5000, 6000]
    dispatch(value, [(k, arm(k)) for k in sparse])

sys.stdout.write(build_module(prog).to_json())
`;

test("Py/TS HIR parity for dispatch", () => {
  const tsHir = buildParityModule().toJson();
  const pyHir = execFileSync("python3", ["-c", PY_PARITY_SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});
