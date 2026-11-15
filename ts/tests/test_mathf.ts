/**
 * `mathf.isqrt`: fixed-point integer square root (issue #22).
 *
 * `isqrt` is the kernel the camera-follow port shares with future callers, so
 * it gets its own contract tests:
 *
 * - a build-time constant folds to the floor of the scaled root
 *   (`bigintSqrt(value << 2 * fixedBits)`);
 * - a runtime value is read from memory and must agree at the full range
 *   boundary (`2 ** valueBits - 1`) and at the caller's Q8 scale;
 * - the kernel is emitted inline, so running it twice in one module must give
 *   the same answer twice — a stale remainder or root accumulator would only
 *   show up from the second pass on.
 *
 * The Python suite (`py/tests/test_mathf.py`) covers the same cases; the
 * camera modules' emitted HIR is pinned byte-identical across the frontends.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  EudslError,
  MemView,
  SetDeaths,
  SetTo,
  Var,
  asReadonly,
  buildModule,
  defineMemView,
  isqrt,
  when,
  type Expr,
} from "../index.ts";

const WORD_ADDR = 3000;
const WORD = defineMemView("MathfWord", 4, {
  v: { offset: 0, size: 4 },
});

function seed(value: number): void {
  SetDeaths(WORD_ADDR, SetTo, value, 0);
}

function constant(value: number, valueBits: number, fixedBits: number): number {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    const out = Var(0);
    ids.out = out.id;
    out.set(isqrt(value, valueBits, fixedBits));
  });
  return runSim(mod.toJson()).var_values[ids.out];
}

/** Run `isqrt` once per element, feeding each pass from memory. */
function runtime(
  values: number[],
  valueBits: number,
  fixedBits: number,
): number[] {
  const ids: number[] = [];
  const mod = buildModule(() => {
    const loop = Var(0);
    seed(values[0]);
    const view = MemView(WORD, WORD_ADDR);
    values.forEach((value, index) => {
      const out = Var(0);
      ids[index] = out.id;
      when(loop.eq(index), () => {
        seed(value);
        out.set(isqrt(view.v.read(), valueBits, fixedBits));
      });
      loop.iadd(1);
    });
  });
  const report = runSim(mod.toJson(), values.length);
  return ids.map((id) => report.var_values[id]);
}

test("constants fold to the scaled floor root", () => {
  const cases: Array<[number, number, number]> = [
    [0, 32, 0],
    [1, 32, 0],
    [2, 32, 0],
    [3, 32, 0],
    [4, 32, 0],
    [99, 32, 0],
    [100, 32, 0],
    [1600, 32, 0],
    [(1 << 16) - 1, 16, 0],
    [1600, 16, 8],
    [2 ** 32 - 1, 32, 0],
  ];
  for (const [value, valueBits, fixedBits] of cases) {
    assert.equal(
      constant(value, valueBits, fixedBits),
      floorSqrt(value * 2 ** (2 * fixedBits)),
      `value=${value} valueBits=${valueBits} fixedBits=${fixedBits}`,
    );
  }
});

/** Exact floor sqrt for values below 2**53. */
function floorSqrt(value: number): number {
  let x = Math.floor(Math.sqrt(value));
  while (x * x > value) x -= 1;
  while ((x + 1) * (x + 1) <= value) x += 1;
  return x;
}

test("a constant still returns a Var", () => {
  const seen: unknown[] = [];
  const mod = buildModule(() => {
    const result = isqrt(1600, 32, 0);
    seen.push(result);
    const out = Var(0);
    out.set(result);
  });
  assert.ok(seen[0] instanceof Var);
  // Ticket 04 acceptance: a folded constant assigned to a var is still three
  // static triggers (the packed constant updates plus setup/restore).
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.static_cost.triggers, 3);
});

test("floor semantics across the range", () => {
  const values = [0, 1, 2, 3, 4, 5, 8, 9, 15, 16, 99, 100, 1600, 65535];
  assert.deepEqual(
    runtime(values, 16, 0),
    values.map((v) => floorSqrt(v)),
  );
});

test("wider input uses the declared bit width", () => {
  const values = [0, 1, 255, 65536, 2 ** 20, 2 ** 32 - 1];
  assert.deepEqual(
    runtime(values, 32, 0),
    values.map((v) => floorSqrt(v)),
  );
});

test("Q8 fixed point matches the scaled oracle", () => {
  // The camera port asks for `floor(256 * sqrt(sumsq))` with sumsq < 2**16.
  const values = [0, 1, 2, 1600, 3200, 9999, (1 << 16) - 1];
  assert.deepEqual(
    runtime(values, 16, 8),
    values.map((v) => floorSqrt(v * 65536)),
  );
});

test("repeated passes do not accumulate", () => {
  assert.deepEqual(runtime([1600, 1600], 16, 0), [40, 40]);
  assert.deepEqual(runtime([1600, 81], 16, 0), [40, 9]);
  assert.deepEqual(runtime([(1 << 16) - 1, 0], 16, 8), [
    floorSqrt(((1 << 16) - 1) * 65536),
    0,
  ]);
});

test("prototype sweep matches math.isqrt", () => {
  // Exact squares and the uint32 boundary catch both recurrence mistakes
  // found while prototyping: a missing root doubling, and comparing against
  // the pre-doubling `u`.
  const values = [
    0, 1, 2, 3, 4, 8, 9, 15, 16, 99, 100, 1000, 65535, 65536,
    0xffffffff,
  ];
  assert.deepEqual(
    runtime(values, 32, 0),
    values.map((v) => floorSqrt(v)),
  );
});

// One runtime `isqrt` site at the default widths: `o.set(isqrt(x, 32, 0))`.
// The ticket-04 `u = 2*root` recurrence replaces the per-step `trial` rebuild,
// trading 16 static triggers for 17 static actions and shrinking the page by
// 38,528 bytes. Executed cost falls for every pinned input because the three
// var-to-var transfers per step are gone.
const ISQRT_SITE_STATIC = {
  triggers: 211,
  conditions: 48,
  actions: 726,
  trigger_bytes: 508088,
  blob_bytes: 12,
  total_bytes: 508100,
};
const ISQRT_EXECUTED_PINS: Array<[number, [number, number, number]]> = [
  [1000, [268, 48, 6251]],
  [0, [243, 48, 5510]],
  [0x40000000, [248, 48, 5658]],
  [0xffffffff, [323, 48, 7894]],
];

function isqrtCostReport(value: number) {
  const mod = buildModule(() => {
    const src = Var(value);
    const out = Var(0);
    out.set(isqrt(src, 32, 0));
  });
  return runSim(mod.toJson());
}

test("static cost of one isqrt site", () => {
  assert.deepEqual(isqrtCostReport(1000).static_cost, ISQRT_SITE_STATIC);
});

test("executed cost pins", () => {
  for (const [value, want] of ISQRT_EXECUTED_PINS) {
    const cost = isqrtCostReport(value).executed_cost;
    assert.deepEqual(
      [cost.triggers, cost.conditions, cost.actions],
      want,
      `value=${value}`,
    );
  }
});

test("ReadonlyVar is used without a copy", () => {
  const plain = buildModule(() => {
    const src = Var(1600);
    const out = Var(0);
    out.set(isqrt(src, 16, 0));
  });
  const wrapped = buildModule(() => {
    const src = Var(1600);
    const out = Var(0);
    out.set(isqrt(asReadonly(src), 16, 0));
  });
  // A copy would add a var (and its id) to the module, so identical HIR
  // proves the read-only handle forwards the same underlying var.
  assert.deepEqual(wrapped.toJson(), plain.toJson());
});

test("ReadonlyVar simulates like the wrapped var", () => {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    const src = Var(1600);
    const out = Var(0);
    ids.out = out.id;
    out.set(isqrt(asReadonly(src), 16, 0));
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.out], 40);
});

test("Expr materializes one temp like an explicit set", () => {
  const explicit = buildModule(() => {
    const src = Var(9);
    const out = Var(0);
    const tmp = Var(0);
    tmp.set(src.add(5));
    out.set(isqrt(tmp, 16, 0));
  });
  const inline = buildModule(() => {
    const src = Var(9);
    const out = Var(0);
    out.set(isqrt(src.add(5), 16, 0));
  });
  // The canonical materializer allocates exactly the one temp an author would
  // write by hand, in the same order.
  assert.deepEqual(inline.toJson(), explicit.toJson());
});

test("Expr is accepted by the public signature and simulates", () => {
  const ids = { out: -1 };
  const mod = buildModule(() => {
    const src = Var(9);
    // The `Expr` annotation is the type-level check: `isqrt` must accept a
    // bare expression without a cast (ADR 0019 operand domain).
    const expr: Expr = src.add(5);
    const out = Var(0);
    ids.out = out.id;
    out.set(isqrt(expr, 16, 0));
  });
  assert.equal(runSim(mod.toJson()).var_values[ids.out], 3);
});

function errorCode(fn: () => void): string {
  try {
    buildModule(fn);
  } catch (error) {
    assert.ok(error instanceof EudslError, `unexpected error: ${String(error)}`);
    return error.code;
  }
  assert.fail("expected an EudslError");
}

test("bit widths are range checked", () => {
  assert.equal(errorCode(() => isqrt(Var(1), 0)), "EUDSL-RANGE");
  assert.equal(errorCode(() => isqrt(Var(1), 33)), "EUDSL-RANGE");
  assert.equal(errorCode(() => isqrt(Var(1), 32, 17)), "EUDSL-RANGE");
});

test("constants must fit the declared width", () => {
  assert.equal(errorCode(() => isqrt(-1)), "EUDSL-RANGE");
  assert.equal(errorCode(() => isqrt(1 << 16, 16)), "EUDSL-RANGE");
});

test("widths must be plain integers", () => {
  // JS has no `16` vs `16.0` distinction, so only non-numeric widths are
  // type errors here; Python's suite also rejects float widths.
  assert.equal(
    errorCode(() => isqrt(Var(1), true as unknown as number)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => isqrt(Var(1), 16, "0" as unknown as number)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => isqrt(Var(1), 16.5)),
    "EUDSL-TYPE",
  );
});

test("value must be in the operand domain", () => {
  assert.equal(errorCode(() => isqrt(1.5)), "EUDSL-TYPE");
  assert.equal(errorCode(() => isqrt(true as unknown as number)), "EUDSL-TYPE");
  assert.equal(errorCode(() => isqrt("1600" as unknown as number)), "EUDSL-TYPE");
  assert.equal(
    errorCode(() => isqrt(undefined as unknown as number)),
    "EUDSL-TYPE",
  );
  assert.equal(errorCode(() => isqrt(null as unknown as number)), "EUDSL-TYPE");
});
