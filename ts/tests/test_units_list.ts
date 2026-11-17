/**
 * T-WP7: `forEachUnitList` — U/T/S/D walks with a `Break` sentinel.
 *
 * The walk is a runtime `while` over the global CUnit list: one ambient
 * CurrentPlayer save per site, the 20T CP-relative reader per node, and an
 * unconditional restore after the loop. `U`/`T`/`S` follow `next` (`ptr + 1`);
 * `D` starts at the tail and follows `prev` (`ptr + 0`). `F` is not a unit list
 * and is rejected.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  AtLeast,
  AtMost,
  BREAK,
  Break,
  EudslError,
  Exactly,
  SetDeaths,
  SetTo,
  UNIT_LIST_D,
  UNIT_LIST_S,
  UNIT_LIST_T,
  UNIT_LIST_U,
  UnitLoop,
  Var,
  buildModule,
  forEachUnitList,
} from "../index.ts";
import { CP_ADDR } from "../cp.ts";
import { REPO_ROOT, runSim } from "./helpers.ts";

import {
  UNIT_LIST_HEADS,
  UNIT_LIST_LINK_OFFSETS,
  actsTargeting,
  cpStores,
  doneOf,
  flatten,
  isEudsl,
  moduleOf,
  ptrOf,
  varInit,
  whileOf,
  type Json,
} from "./units_hir.ts";

test("list names are the four unit lists", () => {
  assert.deepEqual(
    [UNIT_LIST_U, UNIT_LIST_T, UNIT_LIST_S, UNIT_LIST_D],
    ["U", "T", "S", "D"],
  );
});

test("one ambient read, one while, and one final restore", () => {
  const module = moduleOf(() =>
    forEachUnitList(UNIT_LIST_U, () => undefined),
  );
  const funcs = module.funcs as Json[];
  assert.deepEqual(
    funcs.map((f) => f.body.length).sort((a, b) => a - b),
    [20, 33],
  );
  const ambientId = funcs.find((f) => f.body.length === 33)!.id;

  const calls = flatten(module.stmts as Json[]).filter((s) => s.kind === "call");
  assert.equal(
    calls.filter((c) => c.func === ambientId).length,
    1,
    "the 33T ambient CP read must happen exactly once per site",
  );

  const whileStmt = whileOf(module);
  assert.deepEqual(
    whileStmt.conds.map((c: Json) => c.amount),
    [19025, 161741, 0],
  );
  assert.deepEqual(
    whileStmt.conds.map((c: Json) => c.comparison),
    [AtLeast, AtMost, Exactly],
  );

  const ambientCall = (module.stmts as Json[]).find(
    (s) => s.kind === "call" && s.func === ambientId,
  )!;
  const stmts = module.stmts as Json[];
  const last = stmts[stmts.length - 1];
  assert.equal(last.kind, "varstore");
  assert.equal(last.dst_addr, CP_ADDR);
  assert.equal(last.src_var, ambientCall.dst);
});

test("head store uses the documented head literal", () => {
  for (const listName of Object.keys(UNIT_LIST_HEADS)) {
    const module = moduleOf(() =>
      forEachUnitList(listName, () => undefined),
    );
    const head = UNIT_LIST_HEADS[listName];
    const headStores = cpStores(module.stmts as Json[]).filter(
      (s) => varInit(module, s.src_var) === head,
    );
    assert.equal(headStores.length, 1, listName);
    assert.ok(
      (module.vars as Json[]).some((v) => v.init === head),
      `${listName}: head literal ${head} must be emitted`,
    );
  }
});

test("forward lists read next and dying reads prev", () => {
  for (const listName of Object.keys(UNIT_LIST_HEADS)) {
    const module = moduleOf(() =>
      forEachUnitList(listName, () => undefined),
    );
    const whileStmt = whileOf(module);
    const body = whileStmt.body as Json[];
    const ptr = ptrOf(whileStmt);
    const bind = cpStores(body)[0];

    if (UNIT_LIST_LINK_OFFSETS[listName] === 1) {
      assert.notEqual(bind.src_var, ptr, listName);
      const link = bind.src_var;
      assert.ok(
        body.some(
          (s) =>
            s.kind === "vartransfer" &&
            s.dst_var === link &&
            s.src_var === ptr,
        ),
        `${listName}: the forward link must be materialised from ptr`,
      );
      assert.ok(
        actsTargeting(body, link).some((a) => a.value === 1),
        `${listName}: the forward link adds +1 to ptr`,
      );
    } else {
      assert.equal(
        bind.src_var,
        ptr,
        "D must read prev at ptr + 0, not ptr + 1",
      );
    }
  }
});

test("dying head is the tail literal", () => {
  const module = moduleOf(() =>
    forEachUnitList(UNIT_LIST_D, () => undefined),
  );
  const headStores = cpStores(module.stmts as Json[]).filter(
    (s) => varInit(module, s.src_var) === 161842,
  );
  assert.equal(headStores.length, 1);
});

test("free-list dwords never appear in the HIR", () => {
  for (const listName of Object.keys(UNIT_LIST_HEADS)) {
    const text = JSON.stringify(moduleOf(() =>
      forEachUnitList(listName, () => undefined),
    ));
    assert.ok(!text.includes("161845"), listName);
    assert.ok(!text.includes("161846"), listName);
  }
});

test("free list is bounds, not type", () => {
  assert.throws(
    () => buildModule(() => forEachUnitList("F", () => undefined)),
    (err) =>
      isEudsl("EUDSL-BOUNDS")(err) && /free list/.test(String(err)),
  );
});

test("unknown or non-string list names are type errors", () => {
  for (const value of ["f", "X", 5, true]) {
    assert.throws(
      () =>
        buildModule(() =>
          forEachUnitList(value as never, () => undefined),
        ),
      isEudsl("EUDSL-TYPE"),
      String(value),
    );
  }
});

test("reentrant must be a plain boolean", () => {
  assert.throws(
    () =>
      buildModule(() =>
        forEachUnitList(UNIT_LIST_U, () => undefined, {
          reentrant: 1 as never,
        }),
      ),
    isEudsl("EUDSL-TYPE"),
  );
});

test("body must be callable", () => {
  assert.throws(
    () =>
      buildModule(() =>
        forEachUnitList(UNIT_LIST_U, 5 as never),
      ),
    isEudsl("EUDSL-TYPE"),
  );
});

test("break outside a walk is a type error", () => {
  assert.throws(
    () => buildModule(() => new UnitLoop(UNIT_LIST_U, Var(0)).break_()),
    isEudsl("EUDSL-TYPE"),
  );
});

test("break_ emits done.set and can be called twice", () => {
  const module = moduleOf(() => {
    const counter = Var(0);
    forEachUnitList(UNIT_LIST_U, (_unit, loop) => {
      counter.iadd(1);
      loop.break_();
      loop.break_();
    });
  });
  const whileStmt = whileOf(module);
  const done = doneOf(whileStmt);
  const acts = actsTargeting(whileStmt.body as Json[], done);
  assert.equal(acts.length, 2);
  assert.ok(acts.every((a) => a.value === 1));
});

test("returning BREAK emits done after the body", () => {
  let markerId = -1;
  const module = moduleOf(() => {
    const marker = Var(0);
    markerId = marker.id;
    forEachUnitList(UNIT_LIST_U, () => {
      marker.iadd(1);
      return BREAK;
    });
  });
  const whileStmt = whileOf(module);
  const body = whileStmt.body as Json[];
  const done = doneOf(whileStmt);
  const markerIndex = body.findIndex(
    (s) =>
      s.kind === "act" &&
      (s.relocs as Json[] | undefined)?.some((r) => r.target_var === markerId),
  );
  const doneIndices = body
    .map((s, i) => ({ s, i }))
    .filter(
      ({ s }) =>
        s.kind === "act" &&
        (s.relocs as Json[] | undefined)?.some((r) => r.target_var === done),
    )
    .map(({ i }) => i);
  assert.equal(doneIndices.length, 1);
  assert.ok(markerIndex < doneIndices[0]);
});

test("returning true continues and never sets done", () => {
  const module = moduleOf(() => {
    const marker = Var(0);
    forEachUnitList(UNIT_LIST_U, () => {
      marker.iadd(1);
      return true as never;
    });
  });
  const whileStmt = whileOf(module);
  assert.deepEqual(actsTargeting(whileStmt.body as Json[], doneOf(whileStmt)), []);
});

function readerAndBodyIndices(reentrant: boolean): [number, number] {
  let markerId = -1;
  const module = moduleOf(() => {
    const marker = Var(0);
    markerId = marker.id;
    forEachUnitList(
      UNIT_LIST_U,
      () => {
        marker.iadd(1);
      },
      { reentrant },
    );
  });
  const body = whileOf(module).body as Json[];
  const reader = body.findIndex((s) => s.kind === "call");
  const marker = body.findIndex(
    (s) =>
      s.kind === "act" &&
      (s.relocs as Json[] | undefined)?.some((r) => r.target_var === markerId),
  );
  return [reader, marker];
}

test("reentrant reads the link before the body", () => {
  const [reader, marker] = readerAndBodyIndices(true);
  assert.ok(reader < marker);
});

test("default reads the link after the body", () => {
  const [reader, marker] = readerAndBodyIndices(false);
  assert.ok(reader > marker);
});

test("Break is a named singleton, not a boolean", () => {
  assert.ok(BREAK instanceof Break);
  assert.equal(String(BREAK), "BREAK");
  assert.notEqual(BREAK, true);
});

function pythonHir(listName: string): string {
  const script = `
import sys
from py import Var, build_module, for_each_unit_list

def prog():
    count = Var(0)
    def body(unit, loop):
        count.iadd(1)
    for_each_unit_list(${JSON.stringify(listName)}, body)

sys.stdout.write(build_module(prog).to_json())
`;
  return execFileSync("python3", ["-c", script], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
}

function typeScriptHir(listName: string): string {
  return buildModule(() => {
    const count = Var(0);
    forEachUnitList(listName, () => {
      count.iadd(1);
    });
  }).toJson();
}

test("U and D HIR is byte-identical to the Python frontend", () => {
  for (const listName of [UNIT_LIST_U, UNIT_LIST_D]) {
    assert.equal(
      typeScriptHir(listName),
      pythonHir(listName),
      `${listName} HIR`,
    );
  }
});

test("U walk counts three nodes end to end", () => {
  const bases = [0x59cca8, 0x59cdf8, 0x59cf48];
  let countId = -1;
  const mod = buildModule(() => {
    const count = Var(0);
    countId = count.id;
    SetDeaths(161843, SetTo, bases[0], 0);
    SetDeaths(19026, SetTo, bases[1], 0);
    SetDeaths(19110, SetTo, bases[2], 0);
    SetDeaths(19194, SetTo, 0, 0);
    forEachUnitList(UNIT_LIST_U, () => {
      count.iadd(1);
    });
  });
  assert.equal(runSim(mod.toJson(), 1).var_values[countId], 3);
});

test("D walk counts three nodes backward", () => {
  const bases = [0x59cca8, 0x59cdf8, 0x59cf48];
  let countId = -1;
  const mod = buildModule(() => {
    const count = Var(0);
    countId = count.id;
    SetDeaths(161842, SetTo, bases[2], 0);
    SetDeaths(19193, SetTo, bases[1], 0);
    SetDeaths(19109, SetTo, bases[0], 0);
    SetDeaths(19025, SetTo, 0, 0);
    forEachUnitList(UNIT_LIST_D, () => {
      count.iadd(1);
    });
  });
  assert.equal(runSim(mod.toJson(), 1).var_values[countId], 3);
});
