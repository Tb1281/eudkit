/**
 * Extended contracts for signed div/mod, integer powers, trig, and search.
 *
 * Mirrors `py/tests/test_mathf_ext.py`. Every runtime fixture seeds its
 * operands from memory through `SetDeaths`, so the emitted ladders and tables
 * are exercised instead of build-time folds. The source grep at the bottom
 * pins the "no `Math.pow`" rule and the table-only use of `Math.*`.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { REPO_ROOT, flattenStmts, runSim } from "./helpers.ts";

import {
  EudslError,
  SetDeaths,
  SetTo,
  TRIG_TABLE_Q,
  TRIG_TABLE_SCALE,
  Trigger,
  Var,
  asReadonly,
  atan2,
  atan2Fixed8,
  atan2_256,
  binaryMax,
  binaryMin,
  binary_max,
  binary_min,
  buildModule,
  divEuclid,
  divFloor,
  divTowardsZero,
  div_euclid,
  div_floor,
  div_towards_zero,
  lengthdir,
  lengthdirFixed8,
  lengthdir_256,
  modEuclid,
  modFloor,
  modTowardsZero,
  mod_euclid,
  mod_floor,
  mod_towards_zero,
  pow,
  powInt,
  pow_int,
  when,
  type HirCond,
  type MathOperand,
  type VarImpl,
} from "../index.ts";

const DWORD = 0xffffffff;
type Stmt = Parameters<typeof flattenStmts>[0][number];

/** The §4.1 sign matrix: [dividend, divisor]. */
const SIGN_MATRIX: Array<[number, number]> = [
  [7, 2],
  [-7, 2],
  [7, -2],
  [-7, -2],
  [-1, 2],
  [5, 7],
  [0, 5],
];

/** Expected [quotient, remainder] per mode, in the same order. */
const SIGN_EXPECTED: Record<string, Array<[number, number]>> = {
  towards_zero: [
    [3, 1],
    [-3, -1],
    [-3, 1],
    [3, -1],
    [0, -1],
    [0, 5],
    [0, 0],
  ],
  floor: [
    [3, 1],
    [-4, 1],
    [-4, -1],
    [3, -1],
    [-1, 1],
    [0, 5],
    [0, 0],
  ],
  euclid: [
    [3, 1],
    [-4, 1],
    [-3, 1],
    [4, 1],
    [-1, 1],
    [0, 5],
    [0, 0],
  ],
};

const DIV_FUNCS: Record<
  string,
  [
    (a: MathOperand, b: MathOperand) => VarImpl,
    (a: MathOperand, b: MathOperand) => VarImpl,
  ]
> = {
  towards_zero: [divTowardsZero, modTowardsZero],
  floor: [divFloor, modFloor],
  euclid: [divEuclid, modEuclid],
};

function u(value: number): number {
  return value >>> 0;
}

function s(value: number): number {
  return value | 0;
}

function constantCall(
  fn: (a: number, b: number) => VarImpl | VarImpl,
  a: number,
  b: number,
): { module: ReturnType<typeof buildModule>; id: number } {
  const box = { id: -1 };
  const module = buildModule(() => {
    const out = Var(0);
    box.id = out.id;
    out.set(fn(a, b) as VarImpl);
  });
  return { module, id: box.id };
}

function stmtsOf(module: ReturnType<typeof buildModule>): Stmt[] {
  return module.toHir().module.stmts as unknown as Stmt[];
}

function assertConstantShape(
  module: ReturnType<typeof buildModule>,
  value: number,
): void {
  const stmts = stmtsOf(module);
  assert.deepEqual(
    stmts.map((stmt) => stmt.kind),
    ["trigger", "vartransfer"],
  );
  const trigger = stmts[0] as {
    conds?: unknown[];
    acts?: Array<{ value?: number }>;
  };
  assert.deepEqual(trigger.conds, []);
  assert.equal(trigger.acts?.length, 1);
  assert.equal(trigger.acts?.[0]?.value, u(value));
  assert.equal(
    flattenStmts(stmts).filter((stmt) => stmt.kind === "when").length,
    0,
  );
}

function runtimeCallCases(
  fn: (a: VarImpl, b: VarImpl) => VarImpl,
  cases: Array<[number, number]>,
): { module: ReturnType<typeof buildModule>; values: number[] } {
  const ids: number[] = [];
  const module = buildModule(() => {
    const loop = Var(0);
    const a = Var(0);
    const b = Var(0);
    cases.forEach(([aValue, bValue], index) => {
      const out = Var(0);
      ids.push(out.id);
      when(loop.eq(index), () => {
        SetDeaths(a.value, SetTo, u(aValue), 0);
        SetDeaths(b.value, SetTo, u(bValue), 0);
        out.set(fn(a, b));
      });
    });
    loop.iadd(1);
  });
  const report = runSim(module.toJson(), cases.length);
  return { module, values: ids.map((id) => report.var_values[id]) };
}

type Emit = (...inputs: VarImpl[]) => VarImpl | [VarImpl, VarImpl];

function runCases(
  cases: Array<Array<number>>,
  emit: Emit,
  arity: number,
): number[][] {
  const ids: number[][] = [];
  const module = buildModule(() => {
    const loop = Var(0);
    const inputs = Array.from({ length: cases[0].length }, () => Var(0));
    cases.forEach((values, index) => {
      const outs = Array.from({ length: arity }, () => Var(0));
      ids.push(outs.map((out) => out.id));
      when(loop.eq(index), () => {
        values.forEach((value, k) => {
          SetDeaths(inputs[k].value, SetTo, u(Math.trunc(value)), 0);
        });
        const result = emit(...inputs);
        const tuple: VarImpl[] = Array.isArray(result) ? result : [result];
        outs.forEach((dst, k) => dst.set(tuple[k]));
      });
    });
    loop.iadd(1);
  });
  const report = runSim(module.toJson(), cases.length);
  return ids.map((row) => row.map((id) => report.var_values[id]));
}

function binaryResult(
  fn: (
    lo: number,
    hi: number,
    predicate: (x: VarImpl) => HirCond,
  ) => VarImpl,
  lo: number,
  hi: number,
  predicate: (x: VarImpl) => HirCond,
): { module: ReturnType<typeof buildModule>; id: number } {
  const box = { id: -1 };
  const module = buildModule(() => {
    const out = Var(0);
    box.id = out.id;
    out.set(fn(lo, hi, predicate));
  });
  return { module, id: box.id };
}

function errorCode(fn: () => void): string {
  try {
    buildModule(fn);
  } catch (error) {
    assert.ok(error instanceof EudslError, `unexpected error: ${String(error)}`);
    return error.code;
  }
  assert.fail("expected an EudslError");
}

test("sign matrix folds to the documented values", () => {
  for (const [mode, [divFn, modFn]] of Object.entries(DIV_FUNCS)) {
    SIGN_MATRIX.forEach(([a, b], index) => {
      const [q, r] = SIGN_EXPECTED[mode][index];
      const div = constantCall(divFn, a, b);
      assertConstantShape(div.module, q);
      assert.equal(runSim(div.module.toJson()).var_values[div.id], u(q));

      const mod = constantCall(modFn, a, b);
      assertConstantShape(mod.module, r);
      assert.equal(runSim(mod.module.toJson()).var_values[mod.id], u(r));
    });
  }
});

test("sign matrix runs through the runtime ladder", () => {
  for (const [mode, [divFn, modFn]] of Object.entries(DIV_FUNCS)) {
    const div = runtimeCallCases(divFn, SIGN_MATRIX);
    assert.deepEqual(
      div.values,
      SIGN_EXPECTED[mode].map(([q]) => u(q)),
    );
    const stmts = stmtsOf(div.module);
    assert.equal(
      stmts.filter((stmt) => stmt.kind === "when").length,
      SIGN_MATRIX.length,
    );
    assert.ok(
      flattenStmts(stmts).some((stmt) => stmt.kind === "trigger"),
    );

    const mod = runtimeCallCases(modFn, SIGN_MATRIX);
    assert.deepEqual(
      mod.values,
      SIGN_EXPECTED[mode].map(([, r]) => u(r)),
    );
  }
});

test("a game-time zero divisor keeps the unsigned contract", () => {
  const cases: Array<[number, number]> = [
    [-7, 0],
    [7, 0],
  ];
  for (const [mode, [divFn, modFn]] of Object.entries(DIV_FUNCS)) {
    assert.deepEqual(
      runtimeCallCases(divFn, cases).values,
      [0, 0],
      `${mode} quotient`,
    );
    // The remainder is the original signed dividend.
    assert.deepEqual(
      runtimeCallCases(modFn, cases).values,
      [u(-7), 7],
      `${mode} remainder`,
    );
  }
});

test("a build-time zero divisor is rejected", () => {
  for (const [mode, [divFn, modFn]] of Object.entries(DIV_FUNCS)) {
    assert.equal(
      errorCode(() => divFn(Var(1), 0)),
      "EUDSL-DIV-ZERO",
      `${mode} div`,
    );
    assert.equal(
      errorCode(() => modFn(0, 0)),
      "EUDSL-DIV-ZERO",
      `${mode} mod`,
    );
  }
});

test("rejection emits no partial statements", () => {
  const baseline = () =>
    buildModule(() => {
      const a = Var(0);
      const out = Var(0);
      out.set(1);
      void a;
    });
  const failingRuntime = () =>
    buildModule(() => {
      const a = Var(0);
      const out = Var(0);
      out.set(1);
      try {
        divFloor(a, 0);
      } catch (error) {
        assert.ok(error instanceof EudslError);
      }
    });
  const failingConstant = () =>
    buildModule(() => {
      const a = Var(0);
      const out = Var(0);
      out.set(1);
      try {
        modFloor(0, 0);
      } catch (error) {
        assert.ok(error instanceof EudslError);
      }
      void a;
    });

  const want = baseline().toJson();
  assert.equal(failingRuntime().toJson(), want);
  assert.equal(failingConstant().toJson(), want);
});

test("operands must be in the documented domain", () => {
  for (const bad of [true, "7", undefined, null]) {
    assert.equal(
      errorCode(() => divFloor(bad as unknown as MathOperand, 1)),
      "EUDSL-TYPE",
    );
  }
  assert.equal(
    errorCode(() =>
      divFloor(
        new Trigger({ conds: [], acts: [] }) as unknown as MathOperand,
        1,
      ),
    ),
    "EUDSL-TYPE",
  );
});

test("plain-int parameters are validated", () => {
  assert.equal(
    errorCode(() => pow(Var(2), true as unknown as number)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => pow(Var(2), 1.5)),
    "EUDSL-TYPE",
  );
  assert.equal(
    errorCode(() => pow(Var(2), 32)),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => pow(Var(2), 2, 0)),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => pow(Var(2), 2, 33)),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => binaryMin(5, 4, (x) => x.ge(0))),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => binaryMin(0, DWORD, (x) => x.ge(0))),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() => binaryMax(0, DWORD, (x) => x.lt(1))),
    "EUDSL-RANGE",
  );
  assert.equal(
    errorCode(() =>
      binaryMin(0, 1, 5 as unknown as (x: VarImpl) => HirCond),
    ),
    "EUDSL-TYPE",
  );
});

test("pow folds constants and runs a memory-seeded base", () => {
  const cases: Array<[number, number, number]> = [
    [2, 0, 32],
    [3, 1, 32],
    [3, 2, 32],
    [3, 5, 32],
    [2, 31, 32],
  ];
  for (const [base, exp, bits] of cases) {
    const expected = (BigInt(base) ** BigInt(exp)) & ((1n << BigInt(bits)) - 1n);
    const result = constantCall(
      (b, e) => pow(b, e, bits),
      base,
      exp,
    );
    assertConstantShape(result.module, Number(expected));
    assert.equal(
      runSim(result.module.toJson()).var_values[result.id],
      Number(expected),
    );
  }

  for (const [base, exp] of [
    [3, 5],
    [7, 31],
    [0xffffffff, 2],
  ]) {
    const box = { src: -1, out: -1 };
    const module = buildModule(() => {
      const src = Var(0);
      const out = Var(0);
      box.src = src.id;
      box.out = out.id;
      SetDeaths(src.value, SetTo, u(base), 0);
      out.set(pow(src, exp));
    });
    const report = runSim(module.toJson());
    const expected = Number((BigInt(base) ** BigInt(exp)) & BigInt(DWORD));
    assert.equal(report.var_values[box.out], expected);
    assert.equal(report.var_values[box.src], u(base), "operand is not mutated");
  }
});

test("bits truncate the result", () => {
  for (const [bits, expected] of [
    [1, 1],
    [8, 243],
    [16, 243],
    [32, 243],
  ]) {
    const result = constantCall((b, e) => pow(b, e, bits), 3, 5);
    const mask = bits === 32 ? DWORD : (1 << bits) - 1;
    assert.equal(
      runSim(result.module.toJson()).var_values[result.id],
      expected & mask,
    );
  }

  const box = { out: -1 };
  const module = buildModule(() => {
    const src = Var(0);
    const out = Var(0);
    box.out = out.id;
    SetDeaths(src.value, SetTo, 3, 0);
    out.set(pow(src, 5, 8));
  });
  assert.equal(runSim(module.toJson()).var_values[box.out], 243 & 0xff);
});

test("mul sites match the exponent shape", () => {
  for (const exp of [0, 1, 2, 4, 5, 31]) {
    const module = buildModule(() => {
      const src = Var(3);
      const out = Var(0);
      out.set(pow(src, exp));
    });
    const stmts = stmtsOf(module);
    // `mul_shift_add` starts with one constant zero reset, emitted as an
    // unconditional trigger. `exp == 0` has its own `out.set(1)` instead.
    const resets = stmts.filter(
      (stmt) => stmt.kind === "trigger" && (stmt.conds ?? []).length === 0,
    ).length;
    const sites = resets - (exp === 0 ? 1 : 0);
    const expected =
      exp === 0 ? 0 : exp.toString(2).length - 1 + (popcount(exp) - 1);
    assert.equal(sites, expected, `exp=${exp}`);
  }
});

function popcount(value: number): number {
  let count = 0;
  for (let bits = value; bits > 0; bits >>>= 1) count += bits & 1;
  return count;
}

test("atan2 matches the degrees oracle", () => {
  const grid: Array<[number, number]> = [];
  for (const y of [-1000, -1, 0, 1, 1000]) {
    for (const x of [-1000, -1, 0, 1, 1000]) grid.push([y, x]);
  }
  const values = runCases(grid, (y, x) => atan2(y, x), 1);
  grid.forEach(([y, x], index) => {
    const actual = values[index][0];
    const expected = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
    let difference = Math.abs(actual - expected);
    difference = Math.min(difference, 360 - difference);
    assert.ok(
      difference <= 1,
      `atan2(${y}, ${x}) = ${actual}, want ${expected}`,
    );
  });
  assert.equal(values[grid.findIndex(([y, x]) => y === 0 && x === 0)][0], 0);
});

test("atan2_256 matches the turn oracle", () => {
  const grid: Array<[number, number]> = [];
  for (const y of [-1000, -1, 0, 1, 1000]) {
    for (const x of [-1000, -1, 0, 1, 1000]) grid.push([y, x]);
  }
  const values = runCases(grid, (y, x) => atan2_256(y, x), 1);
  grid.forEach(([y, x], index) => {
    const actual = values[index][0];
    const expected =
      (((Math.atan2(y, x) * 180) / Math.PI + 360) % 360) * (256 / 360);
    let difference = Math.abs(actual - expected);
    difference = Math.min(difference, 256 - difference);
    assert.ok(
      difference <= 1,
      `atan2_256(${y}, ${x}) = ${actual}, want ${expected}`,
    );
  });
  assert.equal(values[grid.findIndex(([y, x]) => y === 0 && x === 0)][0], 0);
});

test("cardinal points are exact", () => {
  const cases: Array<[number, number, number]> = [
    [0, 1, 0],
    [1, 0, 90],
    [0, -1, 180],
    [-1, 0, 270],
  ];
  const values = runCases(
    cases.map(([y, x]) => [y, x]),
    (y, x) => atan2(y, x),
    1,
  );
  assert.deepEqual(
    values.map(([value]) => value),
    cases.map(([, , want]) => want),
  );
});

test("lengthdir matches the trig oracle", () => {
  const angles = [0, 45, 90, 135, 180, 225, 270, 315, 359];
  const lengths = [1, 100, -100];
  const cases: Array<[number, number]> = [];
  for (const angle of angles) {
    for (const length of lengths) cases.push([length, angle]);
  }
  const values = runCases(cases, (length, angle) => lengthdir(length, angle), 2);
  cases.forEach(([length, angle], index) => {
    const [dx, dy] = values[index].map(s);
    const expectedDx = Math.trunc(
      length * Math.cos((angle * Math.PI) / 180),
    );
    const expectedDy = Math.trunc(
      length * Math.sin((angle * Math.PI) / 180),
    );
    assert.ok(
      Math.abs(dx - expectedDx) <= 1,
      `lengthdir(${length}, ${angle}) dx = ${dx}, want ${expectedDx}`,
    );
    assert.ok(
      Math.abs(dy - expectedDy) <= 1,
      `lengthdir(${length}, ${angle}) dy = ${dy}, want ${expectedDy}`,
    );
  });
});

test("lengthdir_256 matches the trig oracle", () => {
  const cases: Array<[number, number]> = [0, 64, 128, 192, 255].map((angle) => [
    100,
    angle,
  ]);
  const values = runCases(
    cases,
    (length, angle) => lengthdir_256(length, angle),
    2,
  );
  cases.forEach(([length, angle], index) => {
    const radians = (2 * Math.PI * angle) / 256;
    const [dx, dy] = values[index].map(s);
    assert.ok(Math.abs(dx - Math.trunc(length * Math.cos(radians))) <= 1);
    assert.ok(Math.abs(dy - Math.trunc(length * Math.sin(radians))) <= 1);
  });
});

test("trig tables are shared per builder", () => {
  const build = () =>
    buildModule(() => {
      const y = Var(0);
      const x = Var(0);
      const a = Var(0);
      const b = Var(0);
      a.set(atan2(y, x));
      b.set(lengthdir(y, x)[0]);
    });

  const first = build();
  const second = build();
  for (const module of [first, second]) {
    assert.equal(module.blobs.length, 3);
    assert.deepEqual(
      module.blobs.map((blob) => blob.size).sort((a, b) => a - b),
      [364, 364, 1028],
    );
  }
  // A second buildModule rebuilds the payloads: a global cache would leave
  // the second module with zero blob declarations.
  assert.notStrictEqual(first.blobs, second.blobs);
});

test("only the tables each entry point uses are materialized", () => {
  const atanModule = (fn: (y: MathOperand, x: MathOperand) => VarImpl) =>
    buildModule(() => {
      const y = Var(0);
      const x = Var(0);
      const out = Var(0);
      out.set(fn(y, x));
    });
  const lengthModule = (
    fn: (length: MathOperand, angle: MathOperand) => [VarImpl, VarImpl],
  ) =>
    buildModule(() => {
      const length = Var(0);
      const angle = Var(0);
      const out = Var(0);
      out.set(fn(length, angle)[0]);
    });

  const cases: Array<[string, ReturnType<typeof buildModule>, number[]]> = [
    ["atan2_256", atanModule(atan2_256), [1028]],
    ["atan2", atanModule(atan2), [1028]],
    ["lengthdir", lengthModule(lengthdir), [364, 364]],
    ["lengthdir_256", lengthModule(lengthdir_256), [260, 260]],
  ];
  for (const [name, module, sizes] of cases) {
    assert.deepEqual(
      module.blobs.map((blob) => blob.size).sort((a, b) => a - b),
      sizes,
      name,
    );
  }
});

test("table scale is Q16", () => {
  assert.equal(TRIG_TABLE_Q, 16);
  assert.equal(TRIG_TABLE_SCALE, 1 << 16);
});

test("binary min and max over spans", () => {
  for (const span of [1, 2, 3, 8, 256, 1000]) {
    const lo = 0;
    const hi = span - 1;
    const threshold = Math.max(lo, Math.floor(hi / 2));

    const found = binaryResult(binaryMin, lo, hi, (x) => x.ge(threshold));
    assert.equal(runSim(found.module.toJson()).var_values[found.id], threshold);

    const missing = binaryResult(binaryMin, lo, hi, (x) => x.ge(hi + 1));
    assert.equal(
      runSim(missing.module.toJson()).var_values[missing.id],
      hi + 1,
    );

    const last = Math.max(lo, Math.floor(hi / 2));
    const max = binaryResult(binaryMax, lo, hi, (x) => x.lt(last + 1));
    assert.equal(runSim(max.module.toJson()).var_values[max.id], last);

    const none = binaryResult(binaryMax, lo, hi, (x) => x.lt(0));
    assert.equal(runSim(none.module.toJson()).var_values[none.id], u(lo - 1));
  }
});

test("step count and predicate actions", () => {
  for (const span of [1, 2, 3, 8, 256, 1000]) {
    const lo = 0;
    const hi = span - 1;
    const steps = (hi - lo + 1).toString(2).length;
    const result = binaryResult(binaryMin, lo, hi, (x) => x.ge(Math.floor(hi / 2)));
    const stmts = stmtsOf(result.module);
    assert.equal(
      stmts.filter((stmt) => stmt.kind === "when").length,
      steps,
      `span=${span} step count`,
    );
    assert.equal(
      flattenStmts(stmts).filter((stmt) => stmt.kind === "act").length,
      steps,
      `span=${span} predicate emitted no acts`,
    );
  }
});

test("lo equals hi still tests once", () => {
  const found = binaryResult(binaryMin, 5, 5, (x) => x.ge(5));
  assert.equal(runSim(found.module.toJson()).var_values[found.id], 5);
  const missing = binaryResult(binaryMin, 5, 5, (x) => x.ge(6));
  assert.equal(runSim(missing.module.toJson()).var_values[missing.id], 6);
  const none = binaryResult(binaryMax, 5, 5, (x) => x.lt(5));
  assert.equal(runSim(none.module.toJson()).var_values[none.id], 4);
});

test("export aliases are the same objects", () => {
  const aliases: Array<[unknown, unknown]> = [
    [divTowardsZero, div_towards_zero],
    [divFloor, div_floor],
    [divEuclid, div_euclid],
    [modTowardsZero, mod_towards_zero],
    [modFloor, mod_floor],
    [modEuclid, mod_euclid],
    [powInt, pow],
    [pow_int, pow],
    [atan2Fixed8, atan2_256],
    [lengthdirFixed8, lengthdir_256],
    [binaryMin, binary_min],
    [binaryMax, binary_max],
  ];
  for (const [alias, target] of aliases) assert.equal(alias, target);
});

test("ReadonlyVar and Expr materialize like explicit temps", () => {
  const plain = buildModule(() => {
    const a = Var(7);
    const b = Var(2);
    const out = Var(0);
    out.set(divFloor(a, b));
  });
  const wrapped = buildModule(() => {
    const a = Var(7);
    const b = Var(2);
    const out = Var(0);
    out.set(divFloor(asReadonly(a), asReadonly(b)));
  });
  const explicit = buildModule(() => {
    const a = Var(7);
    const out = Var(0);
    const tmp = Var(0);
    tmp.set(a.add(1));
    out.set(divFloor(tmp, 2));
  });
  const inline = buildModule(() => {
    const a = Var(7);
    const out = Var(0);
    out.set(divFloor(a.add(1), 2));
  });
  assert.deepEqual(wrapped.toJson(), plain.toJson());
  assert.deepEqual(inline.toJson(), explicit.toJson());
});

test("the §3.2 signature block typechecks", () => {
  const signatures: {
    divTowardsZero: (a: MathOperand, b: MathOperand) => VarImpl;
    divFloor: (a: MathOperand, b: MathOperand) => VarImpl;
    divEuclid: (a: MathOperand, b: MathOperand) => VarImpl;
    modTowardsZero: (a: MathOperand, b: MathOperand) => VarImpl;
    modFloor: (a: MathOperand, b: MathOperand) => VarImpl;
    modEuclid: (a: MathOperand, b: MathOperand) => VarImpl;
    pow: (base: MathOperand, exp: number, bits?: number) => VarImpl;
    atan2: (y: MathOperand, x: MathOperand) => VarImpl;
    atan2_256: (y: MathOperand, x: MathOperand) => VarImpl;
    lengthdir: (length: MathOperand, angle: MathOperand) => [VarImpl, VarImpl];
    lengthdir_256: (
      length: MathOperand,
      angle: MathOperand,
    ) => [VarImpl, VarImpl];
    binaryMin: (
      lo: number,
      hi: number,
      predicate: (x: VarImpl) => HirCond,
    ) => VarImpl;
    binaryMax: (
      lo: number,
      hi: number,
      predicate: (x: VarImpl) => HirCond,
    ) => VarImpl;
  } = {
    divTowardsZero,
    divFloor,
    divEuclid,
    modTowardsZero,
    modFloor,
    modEuclid,
    pow,
    atan2,
    atan2_256,
    lengthdir,
    lengthdir_256,
    binaryMin,
    binaryMax,
  };
  for (const fn of Object.values(signatures)) {
    assert.equal(typeof fn, "function");
  }
});

test("Math.pow is absent and Math.* is table-generation only", () => {
  const source = fs.readFileSync(path.join(REPO_ROOT, "ts", "mathf.ts"), "utf8");
  assert.ok(!/Math\.pow\b/.test(source), "runtime path must not use Math.pow");
  const names = Array.from(
    new Set(Array.from(source.matchAll(/Math\.(\w+)/g), (match) => match[1])),
  ).sort();
  // `abs` is build-time sign handling for constant operands; the trig
  // functions are the only `Math.*` use on the runtime-table path.
  assert.deepEqual(names, ["PI", "abs", "atan", "cos", "floor", "sin"]);
});
