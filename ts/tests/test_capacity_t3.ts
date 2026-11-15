/**
 * T-WP10-T2: `freeListWalk` / `freeSlotCount` — tier 3 one-shot scan.
 *
 * Mirrors `py/tests/test_capacity_t3.py`. The free list is a plain forward
 * chain from `0x628438` through each node's `next` link (`ptr + 1`). A
 * `remaining` scan guard (1699) stops a cycle, and one call site per module is
 * enforced before emission. `freeSlotCount()` delegates to `freeListWalk` and
 * shares its call site.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

import {
  AtLeast,
  AtMost,
  BREAK,
  EudslError,
  Exactly,
  SetTo,
  Var,
  buildModule,
} from "../index.ts";
import {
  CUNIT_FREE_HEAD,
  CUNIT_FREE_SCAN_MAX,
  freeListEmpty,
  freeListWalk,
  freeSlotCount,
} from "../capacity.ts";
import { CP_ADDR } from "../cp.ts";
import { Add } from "../hir.ts";
import { CUNIT_BASE_EPD, CUNIT_LAST_EPD, CUNIT_SLOT_COUNT, EPD } from "../view.ts";
import { REPO_ROOT, flattenStmts, type HirStmtNode } from "./helpers.ts";

// HIR JSON is intentionally loosely typed here: the tests read the exact wire
// shape, which the typed builder interfaces deliberately do not expose.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = { [key: string]: any };

function moduleOf(fn: () => void): Json {
  return JSON.parse(buildModule(fn).toJson()).module as Json;
}

function whiles(module: Json): Json[] {
  return (module.stmts as Json[]).filter((s) => s.kind === "while");
}

function whileOf(module: Json): Json {
  const found = whiles(module);
  assert.equal(found.length, 1, JSON.stringify(found));
  return found[0];
}

function ptrOf(whileStmt: Json): number {
  return whileStmt.conds[0].relocs[0].target_var;
}

function doneOf(whileStmt: Json): number {
  return whileStmt.conds[2].relocs[0].target_var;
}

function cpStores(stmts: Json[]): Json[] {
  return stmts.filter((s) => s.kind === "varstore" && s.dst_addr === CP_ADDR);
}

function varInit(module: Json, varId: number): number {
  return (module.vars as Json[]).find((v) => v.id === varId)!.init;
}

function remainingId(module: Json): number {
  const body = whileOf(module).body as Json[];
  const dec = body.find((s) => s.kind === "act" && s.value === 0xffffffff);
  assert.ok(dec, "the scan guard must be decremented per node");
  return dec!.relocs[0].target_var;
}

function actsAnywhere(stmts: Json[]): Json[] {
  const out: Json[] = [];
  for (const stmt of flattenStmts(stmts as HirStmtNode[])) {
    if (stmt.kind === "act") out.push(stmt);
    if (stmt.acts) out.push(...(stmt.acts as Json[]));
  }
  return out;
}

function actsTargeting(stmts: Json[], varId: number): Json[] {
  return stmts.filter(
    (s) =>
      s.kind === "act" &&
      (s.relocs as Json[] | undefined)?.some((r) => r.target_var === varId),
  );
}

function isEudsl(code: string): (err: unknown) => boolean {
  return (err) => err instanceof EudslError && err.code === code;
}

test("scan max is the pool slot count", () => {
  assert.equal(CUNIT_FREE_SCAN_MAX, 1699);
  assert.equal(CUNIT_FREE_SCAN_MAX, CUNIT_SLOT_COUNT);
});

test("free head is the documented dword", () => {
  assert.equal(CUNIT_FREE_HEAD, 0x628438);
  assert.equal(EPD(CUNIT_FREE_HEAD), 161845);
});

test("one ambient read, one while, and one final restore", () => {
  const module = moduleOf(() => {
    freeSlotCount();
  });
  const funcs = module.funcs as Json[];
  assert.deepEqual(
    funcs.map((f) => f.body.length).sort((a, b) => a - b),
    [20, 33],
  );
  const ambientId = funcs.find((f) => f.body.length === 33)!.id;

  const calls = flattenStmts(module.stmts as HirStmtNode[]).filter(
    (s) => s.kind === "call",
  );
  assert.equal(
    calls.filter((c) => c.func === ambientId).length,
    1,
    "the 33T ambient CP read must happen exactly once per site",
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

test("head store uses the free-list head literal", () => {
  const module = moduleOf(() => {
    freeSlotCount();
  });
  const head = EPD(CUNIT_FREE_HEAD);
  const headStores = cpStores(module.stmts as Json[]).filter(
    (s) => varInit(module, s.src_var) === head,
  );
  assert.equal(headStores.length, 1);
  assert.ok((module.vars as Json[]).some((v) => v.init === head));
});

test("gate is the pool bound then done", () => {
  const whileStmt = whileOf(
    moduleOf(() => {
      freeSlotCount();
    }),
  );
  assert.deepEqual(
    whileStmt.conds.map((c: Json) => c.amount),
    [CUNIT_BASE_EPD, CUNIT_LAST_EPD, 0],
  );
  assert.deepEqual(
    whileStmt.conds.map((c: Json) => c.comparison),
    [AtLeast, AtMost, Exactly],
  );
  // The upper bound is what rejects the null sentinel; pin it explicitly.
  assert.equal(whileStmt.conds[1].amount, 161657);
});

test("forward link is ptr + 1", () => {
  const whileStmt = whileOf(
    moduleOf(() => {
      freeSlotCount();
    }),
  );
  const body = whileStmt.body as Json[];
  const ptr = ptrOf(whileStmt);
  const bind = cpStores(body)[0];
  assert.notEqual(bind.src_var, ptr);
  const link = bind.src_var;
  assert.ok(
    body.some(
      (s) => s.kind === "vartransfer" && s.dst_var === link && s.src_var === ptr,
    ),
    "the forward link must be materialised from ptr",
  );
  assert.ok(
    actsTargeting(body, link).some((act) => act.value === 1),
    "the forward link adds +1 to ptr",
  );
});

test("remaining starts at scan max and decrements per node", () => {
  const module = moduleOf(() => {
    freeSlotCount();
  });
  const body = whileOf(module).body as Json[];
  const remaining = remainingId(module);
  assert.equal(varInit(module, remaining), 0);
  const initActs = actsAnywhere(module.stmts as Json[]).filter(
    (act) =>
      act.value === CUNIT_FREE_SCAN_MAX &&
      (act.relocs as Json[] | undefined)?.some(
        (r) => r.target_var === remaining,
      ),
  );
  assert.equal(initActs.length, 1, "the guard starts at 1699");
  const acts = actsTargeting(body, remaining);
  assert.equal(acts.length, 1);
  // `Var.isub(1)` emits the two's-complement add; the value is wrapped -1.
  assert.equal(acts[0].value, 0xffffffff);
  assert.equal(acts[0].modifier, Add);
});

test("remaining zero sets done and stops the advance", () => {
  const module = moduleOf(() => {
    freeSlotCount();
  });
  const whileStmt = whileOf(module);
  const body = whileStmt.body as Json[];
  const done = doneOf(whileStmt);
  const remaining = remainingId(module);
  const stop = body.find(
    (s) =>
      s.kind === "when" &&
      s.conds.length === 1 &&
      s.conds[0].relocs[0].target_var === remaining &&
      s.conds[0].amount === 0,
  );
  assert.ok(stop, "remaining.eq(0) stop gate must exist");
  assert.deepEqual(
    actsTargeting(stop!.body as Json[], done).map((a) => [a.value, a.modifier]),
    [[1, SetTo]],
  );
});

test("second call is rejected before any emission", () => {
  const errors: EudslError[] = [];
  const module = moduleOf(() => {
    const marker = Var(0);
    freeSlotCount();
    try {
      freeListWalk(() => {
        marker.iadd(1);
      });
    } catch (err) {
      errors.push(err as EudslError);
    }
  });
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "EUDSL-BOUNDS");
  assert.match(String(errors[0]), /ADR 0015/);
  assert.match(String(errors[0]), /one call site per module/);
  assert.deepEqual(
    actsTargeting(flattenStmts(module.stmts as HirStmtNode[]) as Json[], 0),
    [],
  );
  assert.equal(whiles(module).length, 1);
});

test("freeSlotCount consumes the call site", () => {
  assert.throws(
    () =>
      buildModule(() => {
        freeSlotCount();
        freeListWalk(() => undefined);
      }),
    (err) =>
      isEudsl("EUDSL-BOUNDS")(err) && /ADR 0015/.test(String(err)),
  );
});

test("freeSlotCount after a walk leaves no var or HIR", () => {
  const control = moduleOf(() => {
    freeListWalk(() => undefined);
  });
  const errors: EudslError[] = [];
  const module = moduleOf(() => {
    freeListWalk(() => undefined);
    try {
      freeSlotCount();
    } catch (err) {
      errors.push(err as EudslError);
    }
  });
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "EUDSL-BOUNDS");
  // The rejected call claims the call site before allocating its count var,
  // so it leaves no variable and no HIR behind.
  assert.deepEqual(module.vars, control.vars);
  assert.equal(whiles(module).length, 1);
  assert.deepEqual(module.stmts, control.stmts);
});

test("two explicit walks are rejected", () => {
  assert.throws(
    () =>
      buildModule(() => {
        freeListWalk(() => undefined);
        freeListWalk(() => undefined);
      }),
    isEudsl("EUDSL-BOUNDS"),
  );
});

test("freeListEmpty does not consume the budget", () => {
  const module = moduleOf(() => {
    freeListEmpty();
    freeListEmpty();
    freeSlotCount();
    freeListEmpty();
  });
  assert.equal(whiles(module).length, 1);
});

test("a fresh module gets a fresh call site", () => {
  const first = moduleOf(() => {
    freeSlotCount();
  });
  const second = moduleOf(() => {
    freeSlotCount();
  });
  assert.equal(whiles(first).length, 1);
  assert.equal(whiles(second).length, 1);
});

test("body must be callable", () => {
  assert.throws(
    () => buildModule(() => freeListWalk(5 as never)),
    isEudsl("EUDSL-TYPE"),
  );
});

test("maxNodes outside the pool is bounds", () => {
  for (const value of [0, 1700]) {
    assert.throws(
      () => buildModule(() => freeListWalk(() => undefined, { maxNodes: value })),
      isEudsl("EUDSL-BOUNDS"),
      String(value),
    );
  }
});

test("maxNodes non-number is a type error", () => {
  for (const value of [true, "1699", null]) {
    assert.throws(
      () =>
        buildModule(() =>
          freeListWalk(() => undefined, { maxNodes: value as never }),
        ),
      isEudsl("EUDSL-TYPE"),
      String(value),
    );
  }
});

test("outside a builder is scope", () => {
  assert.throws(() => freeListWalk(() => undefined), isEudsl("EUDSL-SCOPE"));
});

test("returning BREAK stops after the first node", () => {
  const module = moduleOf(() => {
    const count = Var(0);
    freeListWalk(() => {
      count.iadd(1);
      return BREAK;
    });
  });
  const whileStmt = whileOf(module);
  const done = doneOf(whileStmt);
  const body = whileStmt.body as Json[];
  const markerIndex = body.findIndex(
    (s) =>
      s.kind === "act" &&
      (s.relocs as Json[] | undefined)?.some((r) => r.target_var === 0),
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

test("returning anything else continues", () => {
  const module = moduleOf(() => {
    const count = Var(0);
    freeListWalk(() => {
      count.iadd(1);
      return true as never;
    });
  });
  const whileStmt = whileOf(module);
  assert.deepEqual(actsTargeting(whileStmt.body as Json[], doneOf(whileStmt)), []);
});

test("HIR is byte-identical with Python", () => {
  const tsHir = buildModule(() => {
    freeSlotCount();
  }).toJson();

  const pyScript = `
from py import build_module
from py.capacity import free_slot_count

def prog():
    free_slot_count()

print(build_module(prog).to_json(), end="")
`;
  const pyHir = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(tsHir, pyHir);
});
