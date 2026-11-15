/**
 * Cycle 1 bitwise / shift / constant-multiply runtime optimisation pins.
 *
 * The bitwise kernels used to materialise a fresh temporary and a long
 * per-bit trigger ladder. They now patch a single pre-built Trigger's action
 * mask/value fields from the right-hand operand (`emitVarStore` +
 * `_registerTrigger`), and constant shifts use eudplib's alternating-lane
 * SetTo/Add action sequence, so one Trigger covers the whole shift.
 *
 * These tests pin three things:
 *
 * 1. semantics over the full uint32 edge set and every shift amount 0..33
 *    (including `n >= 32`, which must zero rather than wrap);
 * 2. aliasing: `dst === lhs`, `dst === rhs`, `lhs === rhs`, and in-place
 *    unary / constant-multiply paths;
 * 3. the reduced trigger counts (static and executed) on one-site probes.
 *
 * The expected costs are deliberately exact. Unlike the old bit-ladder
 * kernels they do not depend on operand width, so a regression that brings
 * the ladder back (or adds a temporary/trigger) is immediately visible.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { REPO_ROOT, runSim } from "./helpers.ts";

import {
  TriggerStruct,
  Var,
  buildModule,
  byte,
  dword,
  type VarImpl,
} from "../index.ts";

const EDGE_VALUES = [
  0,
  1,
  0xffffffff,
  0x80000000,
  0x55555555,
  0xaaaaaaaa,
  0xdeadbeef,
] as const;
const EXTRA_CONSTANTS = [0x0f0f0f0f, 0xffff0000, 0x12345678, 0x0000ffff, 0x80000001] as const;
const MUL_CONSTANTS = [0, 1, 0x80000000, 3, 5, 0x80000001, 12, 0xffffffff] as const;

const MASK = 0x0f0f0f0f;
const DIRTY_RAW = 0xa5b6c7d8;
const MASKED_VALUE = DIRTY_RAW & MASK;
const LO_MASK = 0xff;
const HI_MASK = 0xff00;
const LO_RAW = 0xABCD_FF12;
const HI_RAW = 0x12343478;
const MaskedFields = TriggerStruct("MaskedFields", {
  lo: byte(),
  hi: byte(8),
  sink: dword(),
});

const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-perf-dump-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf8");
    return execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf8",
    });
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function allocationLines(dumpIr: string): string[] {
  const lines = dumpIr.split("\n");
  const start = lines.indexOf("Allocations:");
  assert.notEqual(start, -1, "dump-ir output has no Allocations block");
  const out: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (!line.startsWith("  var[")) break;
    out.push(line);
  }
  return out;
}

function runBatched<T>(
  cells: readonly T[],
  buildCell: (cell: T) => number,
  batchSize = 24,
): Array<{ cell: T; got: number }> {
  const out: Array<{ cell: T; got: number }> = [];
  for (let start = 0; start < cells.length; start += batchSize) {
    const batch = cells.slice(start, start + batchSize);
    const slots: number[] = [];
    const mod = buildModule(() => {
      for (const cell of batch) slots.push(buildCell(cell));
    });
    const values = runSim(mod.toJson(), 1).var_values;
    batch.forEach((cell, index) => {
      out.push({ cell, got: values[slots[index]!] ?? 0 });
    });
  }
  return out;
}

function u32(n: number): number {
  return n >>> 0;
}

function expectedShl(value: number, n: number): number {
  return n >= 32 ? 0 : u32(value << n);
}

function expectedShr(value: number, n: number): number {
  return n >= 32 ? 0 : value >>> n;
}

test("bitnot/neg use the zero-temporary in-place action sequence", () => {
  const ids: { notIn: number; notOut: number; negIn: number; negOut: number } = {
    notIn: -1,
    notOut: -1,
    negIn: -1,
    negOut: -1,
  };
  const mod = buildModule(() => {
    // In-place: no copy is needed, the complement adds run on `dst`.
    const notIn = Var(0xdeadbeef);
    notIn.set(notIn.bitNot());
    ids.notIn = notIn.id;

    // Out-of-place: `dst.set(lhs)` then the same two masked adds.
    const notSrc = Var(0xdeadbeef);
    const notOut = Var(0);
    notOut.set(notSrc.bitNot());
    ids.notOut = notOut.id;

    const negIn = Var(0xdeadbeef);
    negIn.set(negIn.neg());
    ids.negIn = negIn.id;

    const negSrc = Var(0xdeadbeef);
    const negOut = Var(0);
    negOut.set(negSrc.neg());
    ids.negOut = negOut.id;
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);
  assert.equal(report.var_values[ids.notIn], (~0xdeadbeef) >>> 0);
  assert.equal(report.var_values[ids.notOut], (~0xdeadbeef) >>> 0);
  assert.equal(report.var_values[ids.negIn], (-0xdeadbeef) >>> 0);
  assert.equal(report.var_values[ids.negOut], (-0xdeadbeef) >>> 0);
});

test("constant shl/shr match uint32 semantics for every n = 0..33", () => {
  for (const value of EDGE_VALUES) {
    const ids: { shl: number[]; shr: number[] } = { shl: [], shr: [] };
    const mod = buildModule(() => {
      const src = Var(value);
      for (let n = 0; n <= 33; n++) {
        const out = Var(0);
        out.set(src.shl(n));
        ids.shl.push(out.id);
      }
      for (let n = 0; n <= 33; n++) {
        const out = Var(0);
        out.set(src.shr(n));
        ids.shr.push(out.id);
      }
    });

    const report = runSim(mod.toJson());
    assert.equal(report.self_targeted_invariant_holds, true);
    for (let n = 0; n <= 33; n++) {
      assert.equal(
        report.var_values[ids.shl[n]],
        expectedShl(value, n),
        `0x${u32(value).toString(16)} << ${n}`,
      );
      assert.equal(
        report.var_values[ids.shr[n]],
        expectedShr(value, n),
        `0x${u32(value).toString(16)} >> ${n}`,
      );
    }
  }
});

test("constant-operand bitwise matrices match Python", () => {
  const constants = [...EDGE_VALUES, ...EXTRA_CONSTANTS];
  const cells: Array<[string, number, number]> = [];
  for (const op of ["bitand", "bitor", "bitxor"]) {
    for (const value of EDGE_VALUES) {
      for (const constant of constants) cells.push([op, value, constant]);
    }
  }

  for (const { cell: [op, value, constant], got } of runBatched(cells, ([op, value, constant]) => {
    const a = Var(value);
    const out = Var(0);
    if (op === "bitand") out.set(a.bitAnd(constant));
    else if (op === "bitor") out.set(a.bitOr(constant));
    else out.set(a.bitXor(constant));
    return out.id;
  }, 30)) {
    const want =
      op === "bitand" ? value & constant : op === "bitor" ? value | constant : value ^ constant;
    assert.equal(got, u32(want), `${op} 0x${u32(value).toString(16)} with 0x${u32(constant).toString(16)}`);
  }
});

test("reverse constant-operand bitwise matrices match Python", () => {
  const cells: Array<[string, number, number]> = [];
  for (const op of ["bitand", "bitor", "bitxor"]) {
    for (const constant of EXTRA_CONSTANTS) {
      for (const value of EDGE_VALUES) cells.push([op, constant, value]);
    }
  }

  for (const { cell: [op, constant, value], got } of runBatched(cells, ([op, constant, value]) => {
    const a = Var(value);
    const out = Var(0);
    if (op === "bitand") out.set(a.bitAnd(constant));
    else if (op === "bitor") out.set(a.bitOr(constant));
    else out.set(a.bitXor(constant));
    return out.id;
  }, 30)) {
    const want =
      op === "bitand" ? constant & value : op === "bitor" ? constant | value : constant ^ value;
    assert.equal(got, u32(want), `${op} 0x${u32(constant).toString(16)} with 0x${u32(value).toString(16)}`);
  }
});

test("7x7 variable bitwise matrix matches Python", () => {
  const cells: Array<[string, number, number]> = [];
  for (const op of ["bitand", "bitor", "bitxor"]) {
    for (const lhs of EDGE_VALUES) {
      for (const rhs of EDGE_VALUES) cells.push([op, lhs, rhs]);
    }
  }

  for (const { cell: [op, lhs, rhs], got } of runBatched(cells, ([op, lhs, rhs]) => {
    const a = Var(lhs);
    const b = Var(rhs);
    const out = Var(0);
    if (op === "bitand") out.set(a.bitAnd(b));
    else if (op === "bitor") out.set(a.bitOr(b));
    else out.set(a.bitXor(b));
    return out.id;
  }, 21)) {
    const want = op === "bitand" ? lhs & rhs : op === "bitor" ? lhs | rhs : lhs ^ rhs;
    assert.equal(got, u32(want), `${op} 0x${u32(lhs).toString(16)} / 0x${u32(rhs).toString(16)}`);
  }
});

test("constant-multiply matrix covers zero, one, top bit, and popcount-2", () => {
  const cells: Array<[number, number]> = [];
  for (const value of EDGE_VALUES) {
    for (const constant of MUL_CONSTANTS) cells.push([value, constant]);
  }

  for (const { cell: [value, constant], got } of runBatched(cells, ([value, constant]) => {
    const a = Var(value);
    const out = Var(0);
    out.set(a.mul(constant));
    return out.id;
  })) {
    assert.equal(
      got,
      Number((BigInt(value) * BigInt(constant)) & 0xffffffffn),
      `0x${u32(value).toString(16)} * 0x${u32(constant).toString(16)}`,
    );
  }
});

test("boolean-init self-store keeps the patched target a DwordCell", () => {
  const ids: Record<string, number> = {};
  const expected: Record<string, number> = {};
  const mod = buildModule(() => {
    for (const op of ["bitand", "bitor", "bitxor"]) {
      const v = Var(0);
      const w = Var(1);
      if (op === "bitand") {
        v.set(v.bitAnd(w));
        expected[op] = 0;
      } else if (op === "bitor") {
        v.set(v.bitOr(w));
        expected[op] = 1;
      } else {
        v.set(v.bitXor(w));
        expected[op] = 1;
      }
      ids[op] = v.id;
    }
  });

  const lines = allocationLines(runDumpIr(mod.toJson()));
  const byId = new Map<number, string>();
  for (const line of lines) {
    const match = /var\[(\d+)\]/.exec(line);
    if (match) byId.set(Number(match[1]), line);
  }
  for (const [op, id] of Object.entries(ids)) {
    const line = byId.get(id) ?? "";
    assert.match(line, /DwordCell\(/, `${op} allocation`);
    assert.doesNotMatch(line, /BitCell\(/, `${op} must not be a BitCell`);
  }

  const report = runSim(mod.toJson());
  for (const [op, id] of Object.entries(ids)) {
    assert.equal(report.var_values[id], expected[op], `${op} value`);
  }
});

test("dynamic shl/shr match uint32 semantics for every n = 0..33", () => {
  for (const value of EDGE_VALUES) {
    const ids: { shl: number[]; shr: number[] } = { shl: [], shr: [] };
    const mod = buildModule(() => {
      const src = Var(value);
      // One amount Var per shift amount keeps every site independent. The
      // amount is a Var, so these all take the five-stage dynamic path.
      const amounts: VarImpl[] = [];
      for (let n = 0; n <= 33; n++) {
        amounts.push(Var(n));
      }
      for (let n = 0; n <= 33; n++) {
        const out = Var(0);
        out.set(src.shl(amounts[n]!));
        ids.shl.push(out.id);
      }
      for (let n = 0; n <= 33; n++) {
        const out = Var(0);
        out.set(src.shr(amounts[n]!));
        ids.shr.push(out.id);
      }
    });

    const report = runSim(mod.toJson());
    assert.equal(report.self_targeted_invariant_holds, true);
    for (let n = 0; n <= 33; n++) {
      assert.equal(
        report.var_values[ids.shl[n]],
        expectedShl(value, n),
        `dynamic 0x${u32(value).toString(16)} << ${n}`,
      );
      assert.equal(
        report.var_values[ids.shr[n]],
        expectedShr(value, n),
        `dynamic 0x${u32(value).toString(16)} >> ${n}`,
      );
    }
  }
});

test("variable bitand/bitor/bitxor survive dst aliasing on lhs, rhs, and same-var", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    // dst === lhs: the kernel runs in place on the destination.
    const andLhs = Var(0xffff00ff);
    const andLhsOther = Var(0x0ff00ff0);
    andLhs.set(andLhs.bitAnd(andLhsOther));
    ids.andLhs = andLhs.id;
    ids.andLhsOther = andLhsOther.id;

    // dst === rhs: the kernel uses a fresh target and copies back.
    const andRhsLhs = Var(0xffff00ff);
    const andRhs = Var(0x0ff00ff0);
    andRhs.set(andRhsLhs.bitAnd(andRhs));
    ids.andRhs = andRhs.id;
    ids.andRhsLhs = andRhsLhs.id;

    // lhs === rhs: `v & v` must be `v`, not zero.
    const andSame = Var(0x12345678);
    andSame.set(andSame.bitAnd(andSame));
    ids.andSame = andSame.id;

    const orLhs = Var(0xffff00ff);
    const orLhsOther = Var(0x0ff00ff0);
    orLhs.set(orLhs.bitOr(orLhsOther));
    ids.orLhs = orLhs.id;
    ids.orLhsOther = orLhsOther.id;

    const orRhsLhs = Var(0xffff00ff);
    const orRhs = Var(0x0ff00ff0);
    orRhs.set(orRhsLhs.bitOr(orRhs));
    ids.orRhs = orRhs.id;
    ids.orRhsLhs = orRhsLhs.id;

    const orSame = Var(0x12345678);
    orSame.set(orSame.bitOr(orSame));
    ids.orSame = orSame.id;

    const xorLhs = Var(0xffff00ff);
    const xorLhsOther = Var(0x0ff00ff0);
    xorLhs.set(xorLhs.bitXor(xorLhsOther));
    ids.xorLhs = xorLhs.id;
    ids.xorLhsOther = xorLhsOther.id;

    const xorRhsLhs = Var(0xffff00ff);
    const xorRhs = Var(0x0ff00ff0);
    xorRhs.set(xorRhsLhs.bitXor(xorRhs));
    ids.xorRhs = xorRhs.id;
    ids.xorRhsLhs = xorRhsLhs.id;

    const xorSame = Var(0x12345678);
    xorSame.set(xorSame.bitXor(xorSame));
    ids.xorSame = xorSame.id;
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);

  const a = 0xffff00ff;
  const b = 0x0ff00ff0;
  assert.equal(report.var_values[ids.andLhs], u32(a & b));
  assert.equal(report.var_values[ids.andLhsOther], b);
  assert.equal(report.var_values[ids.andRhs], u32(a & b));
  assert.equal(report.var_values[ids.andRhsLhs], a);
  assert.equal(report.var_values[ids.andSame], 0x12345678);

  assert.equal(report.var_values[ids.orLhs], u32(a | b));
  assert.equal(report.var_values[ids.orLhsOther], b);
  assert.equal(report.var_values[ids.orRhs], u32(a | b));
  assert.equal(report.var_values[ids.orRhsLhs], a);
  assert.equal(report.var_values[ids.orSame], 0x12345678);

  assert.equal(report.var_values[ids.xorLhs], u32(a ^ b));
  assert.equal(report.var_values[ids.xorLhsOther], b);
  assert.equal(report.var_values[ids.xorRhs], u32(a ^ b));
  assert.equal(report.var_values[ids.xorRhsLhs], a);
  assert.equal(report.var_values[ids.xorSame], 0);
});

type MaskedCase = {
  name: string;
  run: (v: VarImpl, w: VarImpl) => void;
  expected: number;
  rhs: number;
};

function maskedCases(value: number): MaskedCase[] {
  const rhsAnd = 0xf0f0f0f0;
  const rhsOr = 0xff0000ff;
  const rhsXor = 0xa5a5a5a5;
  return [
    { name: "bitnot", run: (v) => v.set(v.bitNot()), expected: (~value) >>> 0, rhs: 0 },
    { name: "neg", run: (v) => v.set(v.neg()), expected: (-value) >>> 0, rhs: 0 },
    {
      name: "const_and",
      run: (v) => v.set(v.bitAnd(0xf0f0f0f0)),
      expected: value & 0xf0f0f0f0,
      rhs: 0,
    },
    {
      name: "const_or",
      run: (v) => v.set(v.bitOr(0xff0000ff)),
      expected: value | 0xff0000ff,
      rhs: 0,
    },
    {
      name: "const_xor",
      run: (v) => v.set(v.bitXor(0xa5a5a5a5)),
      expected: value ^ 0xa5a5a5a5,
      rhs: 0,
    },
    {
      name: "const_shl",
      run: (v) => v.set(v.shl(4)),
      expected: u32(value << 4),
      rhs: 0,
    },
    { name: "const_shr", run: (v) => v.set(v.shr(4)), expected: value >>> 4, rhs: 0 },
    {
      name: "var_and",
      run: (v, w) => v.set(v.bitAnd(w)),
      expected: value & rhsAnd,
      rhs: rhsAnd,
    },
    {
      name: "var_or",
      run: (v, w) => v.set(v.bitOr(w)),
      expected: value | rhsOr,
      rhs: rhsOr,
    },
    {
      name: "var_xor",
      run: (v, w) => v.set(v.bitXor(w)),
      expected: value ^ rhsXor,
      rhs: rhsXor,
    },
    {
      name: "dynamic_shl",
      run: (v, w) => v.set(v.shl(w)),
      expected: u32(value << 4),
      rhs: 4,
    },
    {
      name: "dynamic_shr",
      run: (v, w) => v.set(v.shr(w)),
      expected: value >>> 4,
      rhs: 4,
    },
  ];
}

test("masked Var kernels preserve dirty neighbour bits", () => {
  const dirtyOutside = u32(DIRTY_RAW & ~MASK);
  for (const row of maskedCases(MASKED_VALUE)) {
    let id = -1;
    const mod = buildModule(() => {
      const v = Var(0, undefined, MASK);
      const alias = Var(0, v.id);
      alias.set(DIRTY_RAW);
      const w = Var(row.rhs);
      row.run(v, w);
      id = v.id;
    });
    const report = runSim(mod.toJson());
    assert.equal(report.self_targeted_invariant_holds, true, row.name);
    assert.equal(
      report.var_values[id],
      u32(dirtyOutside | (row.expected & MASK)),
      row.name,
    );
  }
});

test("adjacent TriggerStruct fields preserve neighbouring bits", () => {
  const loValue = LO_RAW & LO_MASK;
  const loOutside = u32(LO_RAW & ~LO_MASK);
  for (const row of maskedCases(loValue)) {
    let loId = -1;
    let hiId = -1;
    let sinkId = -1;
    const mod = buildModule(() => {
      const fields = MaskedFields({ lo: 0x12, hi: 0x3400, sink: 0xdeadbeef });
      const loAlias = Var(0, fields.lo.id);
      const hiAlias = Var(0, fields.hi.id);
      loAlias.set(LO_RAW);
      hiAlias.set(HI_RAW);
      const w = Var(row.rhs);
      row.run(fields.lo, w);
      loId = fields.lo.id;
      hiId = fields.hi.id;
      sinkId = fields.sink.id;
    });
    const report = runSim(mod.toJson());
    assert.equal(report.self_targeted_invariant_holds, true, row.name);
    assert.equal(
      report.var_values[loId],
      u32(loOutside | (row.expected & LO_MASK)),
      `${row.name}: lo`,
    );
    assert.equal(report.var_values[hiId], HI_RAW, `${row.name}: hi`);
    assert.equal(report.var_values[sinkId], 0xdeadbeef, `${row.name}: sink`);
  }
});

test("shift, unary, and constant-multiply paths survive dst aliasing", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    // Constant shift with dst === lhs.
    const cs = Var(0xdeadbeef);
    cs.set(cs.shl(3));
    ids.cs = cs.id;
    const cr = Var(0xdeadbeef);
    cr.set(cr.shr(3));
    ids.cr = cr.id;

    // Dynamic shift with dst === lhs.
    const dsAmt = Var(3);
    const ds = Var(0xdeadbeef);
    ds.set(ds.shl(dsAmt));
    ids.ds = ds.id;
    const drAmt = Var(3);
    const dr = Var(0xdeadbeef);
    dr.set(dr.shr(drAmt));
    ids.dr = dr.id;

    // Dynamic shift with dst === rhs: the kernel must spill to a temporary
    // and copy back only after the shift triggers read the original amount.
    const shAmt = Var(3);
    const shSecond = Var(0xdeadbeef);
    shAmt.set(shSecond.shl(shAmt));
    ids.shAmt = shAmt.id;
    ids.shSecond = shSecond.id;
    const shrAmt = Var(4);
    const shrSecond = Var(0xdeadbeef);
    shrAmt.set(shrSecond.shr(shrAmt));
    ids.shrAmt = shrAmt.id;
    ids.shrSecond = shrSecond.id;

    // Dynamic shift with lhs === rhs, both with and without dst aliasing.
    const selfIn = Var(3);
    selfIn.set(selfIn.shl(selfIn));
    ids.selfIn = selfIn.id;
    const selfSrc = Var(3);
    const selfOut = Var(0);
    selfOut.set(selfSrc.shl(selfSrc));
    ids.selfOut = selfOut.id;

    // `x << x` where x >= 32 must zero through the Deaths(>=32) stage.
    const bigSelf = Var(0xdeadbeef);
    const bigOut = Var(0);
    bigOut.set(bigSelf.shl(bigSelf));
    ids.bigOut = bigOut.id;

    // Constant multiply: -1 (neg), power of two (shift), popcount 2.
    const mulFIn = Var(0xdeadbeef);
    mulFIn.set(mulFIn.mul(0xffffffff));
    ids.mulFIn = mulFIn.id;
    const mulFSrc = Var(0xdeadbeef);
    const mulFOut = Var(0);
    mulFOut.set(mulFSrc.mul(0xffffffff));
    ids.mulFOut = mulFOut.id;

    const mul8In = Var(7);
    mul8In.set(mul8In.mul(8));
    ids.mul8In = mul8In.id;
    const mul8Src = Var(7);
    const mul8Out = Var(0);
    mul8Out.set(mul8Src.mul(8));
    ids.mul8Out = mul8Out.id;

    const mul12In = Var(5);
    mul12In.set(mul12In.mul(12));
    ids.mul12In = mul12In.id;
    const mul12Src = Var(5);
    const mul12Out = Var(0);
    mul12Out.set(mul12Src.mul(12));
    ids.mul12Out = mul12Out.id;

    const mul0 = Var(9);
    mul0.set(mul0.mul(0));
    ids.mul0 = mul0.id;
    const mul1 = Var(9);
    mul1.set(mul1.mul(1));
    ids.mul1 = mul1.id;
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);

  assert.equal(report.var_values[ids.cs], u32(0xdeadbeef << 3));
  assert.equal(report.var_values[ids.cr], 0xdeadbeef >>> 3);
  assert.equal(report.var_values[ids.ds], u32(0xdeadbeef << 3));
  assert.equal(report.var_values[ids.dr], 0xdeadbeef >>> 3);
  assert.equal(report.var_values[ids.shAmt], u32(0xdeadbeef << 3));
  assert.equal(report.var_values[ids.shSecond], 0xdeadbeef);
  assert.equal(report.var_values[ids.shrAmt], 0xdeadbeef >>> 4);
  assert.equal(report.var_values[ids.shrSecond], 0xdeadbeef);
  assert.equal(report.var_values[ids.selfIn], 3 << 3);
  assert.equal(report.var_values[ids.selfOut], 3 << 3);
  assert.equal(report.var_values[ids.bigOut], 0);

  assert.equal(report.var_values[ids.mulFIn], (-0xdeadbeef) >>> 0);
  assert.equal(report.var_values[ids.mulFOut], (-0xdeadbeef) >>> 0);
  assert.equal(report.var_values[ids.mul8In], u32(7 * 8));
  assert.equal(report.var_values[ids.mul8Out], u32(7 * 8));
  assert.equal(report.var_values[ids.mul12In], u32(5 * 12));
  assert.equal(report.var_values[ids.mul12Out], u32(5 * 12));
  assert.equal(report.var_values[ids.mul0], 0);
  assert.equal(report.var_values[ids.mul1], 9);
});

test("dynamic shift reads the amount as it was when the store was built", () => {
  const ids: { r1: number; r2: number; r3: number } = { r1: -1, r2: -1, r3: -1 };
  const mod = buildModule(() => {
    const src = Var(0xdeadbeef);
    const amount = Var(1);
    const r1 = Var(0);
    const r2 = Var(0);
    const r3 = Var(0);

    // The amount is overwritten between sites. Lowering must keep the shift
    // triggers ahead of the later stores in trigger order, so each site sees
    // the amount value that was current when it was built.
    r1.set(src.shl(amount));
    amount.set(4);
    r2.set(src.shl(amount));
    amount.set(33);
    r3.set(src.shl(amount));

    ids.r1 = r1.id;
    ids.r2 = r2.id;
    ids.r3 = r3.id;
  });

  const report = runSim(mod.toJson());
  assert.equal(report.var_values[ids.r1], u32(0xdeadbeef << 1));
  assert.equal(report.var_values[ids.r2], u32(0xdeadbeef << 4));
  assert.equal(report.var_values[ids.r3], 0);
});

test("optimised bitwise/shift/multiply stores are idempotent across loops", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const frame = Var(0);
    frame.iadd(1);
    ids.frame = frame.id;

    // Source and amount never change, so both results must be reproduced on
    // every trigger loop rather than accumulating the previous pass.
    const src = Var(0xdeadbeef);
    const amount = Var(3);
    const shifted = Var(0);
    shifted.set(src.shl(amount));
    ids.shifted = shifted.id;
    const shiftedRight = Var(0);
    shiftedRight.set(src.shr(amount));
    ids.shiftedRight = shiftedRight.id;

    // In-place constant shift: one more *4 per loop.
    const growing = Var(0xdeadbeef);
    growing.set(growing.shl(2));
    ids.growing = growing.id;

    // Popcount-2 constant multiply: one more *12 per loop through its temp.
    const timesTwelve = Var(5);
    timesTwelve.set(timesTwelve.mul(12));
    ids.timesTwelve = timesTwelve.id;
  });

  const loop1 = runSim(mod.toJson(), 1);
  assert.equal(loop1.var_values[ids.frame], 1);
  assert.equal(loop1.var_values[ids.shifted], u32(0xdeadbeef << 3));
  assert.equal(loop1.var_values[ids.shiftedRight], 0xdeadbeef >>> 3);
  assert.equal(loop1.var_values[ids.growing], u32(0xdeadbeef << 2));
  assert.equal(loop1.var_values[ids.timesTwelve], u32(5 * 12));

  const loop2 = runSim(mod.toJson(), 2);
  assert.equal(loop2.var_values[ids.frame], 2);
  assert.equal(loop2.var_values[ids.shifted], u32(0xdeadbeef << 3));
  assert.equal(loop2.var_values[ids.shiftedRight], 0xdeadbeef >>> 3);
  assert.equal(loop2.var_values[ids.growing], u32(0xdeadbeef << 4));
  assert.equal(loop2.var_values[ids.timesTwelve], u32(5 * 12 * 12));
  assert.equal(loop2.self_targeted_invariant_holds, true);
});

test("one-site trigger-cost pins (static / executed)", () => {
  // Probe shape used for every row: three ordinary Vars plus a single store.
  // Costs include the module's setup/page triggers, exactly as measured on
  // the pre-optimisation tree's replacement. Each static count is the key
  // improvement: the old ladders emitted one trigger per source bit (or a
  // temporary per operand); these kernels fit in one action Trigger.
  const cases: {
    name: string;
    store: (a: VarImpl, b: VarImpl, out: VarImpl) => void;
    staticTriggers: number;
    executedTriggers: number;
  }[] = [
    {
      name: "constant shl by 1",
      store: (a, _b, out) => out.set(a.shl(1)),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "constant shr by 1",
      store: (a, _b, out) => out.set(a.shr(1)),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "dynamic shl",
      store: (a, b, out) => out.set(a.shl(b)),
      staticTriggers: 14,
      executedTriggers: 11,
    },
    {
      name: "dynamic shr",
      store: (a, b, out) => out.set(a.shr(b)),
      staticTriggers: 14,
      executedTriggers: 11,
    },
    {
      name: "var & var",
      store: (a, b, out) => out.set(a.bitAnd(b)),
      staticTriggers: 4,
      executedTriggers: 4,
    },
    {
      name: "var | var",
      store: (a, b, out) => out.set(a.bitOr(b)),
      staticTriggers: 4,
      executedTriggers: 4,
    },
    {
      name: "var ^ var",
      store: (a, b, out) => out.set(a.bitXor(b)),
      staticTriggers: 5,
      executedTriggers: 6,
    },
    {
      name: "bitnot",
      store: (a, _b, out) => out.set(a.bitNot()),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "neg",
      store: (a, _b, out) => out.set(a.neg()),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "mul by 0xffffffff",
      store: (a, _b, out) => out.set(a.mul(0xffffffff)),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "mul by power of two",
      store: (a, _b, out) => out.set(a.mul(8)),
      staticTriggers: 3,
      executedTriggers: 3,
    },
    {
      name: "mul by popcount-2 constant",
      store: (a, _b, out) => out.set(a.mul(12)),
      staticTriggers: 6,
      executedTriggers: 8,
    },
    {
      name: "mul by 7",
      store: (a, _b, out) => out.set(a.mul(7)),
      staticTriggers: 7,
      executedTriggers: 9,
    },
    {
      name: "mul by 0xff000000",
      store: (a, _b, out) => out.set(a.mul(0xff000000)),
      staticTriggers: 9,
      executedTriggers: 9,
    },
  ];

  for (const row of cases) {
    const mod = buildModule(() => {
      const a = Var(0xdeadbeef);
      const b = Var(3);
      const out = Var(0);
      row.store(a, b, out);
    });
    const report = runSim(mod.toJson());
    assert.equal(
      report.static_cost.triggers,
      row.staticTriggers,
      `${row.name} static triggers`,
    );
    assert.equal(
      report.executed_cost.triggers,
      row.executedTriggers,
      `${row.name} executed triggers`,
    );
  }
});