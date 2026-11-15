/**
 * T-ABS-RAND: private per-builder LCG random state (TypeScript mirror).
 *
 * The sequence is checked against a plain `Math.imul` LCG so a regression in
 * the emitted gated adds cannot hide behind a shared implementation. The
 * Rust suite (`crates/eudkit-cli/tests/random_runtime.rs`) reruns the same
 * sequence through the simulator and the real game-tick read.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { EudslError, EPD, Var, buildModule } from "../index.ts";
import type { VarImpl } from "../var.ts";
import { runSim } from "./helpers.ts";
import {
  LCG_INC,
  LCG_MULT,
  dwrand,
  getSeed,
  get_seed,
  rand,
  randRange,
  rand_range,
  randomize,
  srand,
  dwRand,
  type RandSeed,
} from "../random.ts";
import { GAME_TICK_ADDR } from "../camera.ts";

const MULT = 1103515245;
const INC = 12345;

function nextState(state: number): number {
  return (Math.imul(state, MULT) + INC) >>> 0;
}

function referenceRand(seed: number, count: number): number[] {
  let state = seed >>> 0;
  const draws: number[] = [];
  for (let index = 0; index < count; index += 1) {
    state = nextState(state);
    draws.push(state >>> 16);
  }
  return draws;
}

function referenceDwrand(seed: number): { draw: number; state: number } {
  const first = nextState(seed >>> 0);
  const second = nextState(first);
  return {
    draw: ((first & 0xffff0000) | (second >>> 16)) >>> 0,
    state: second,
  };
}

function randSequence(seed: number, count: number): number[] {
  const ids: number[] = [];
  const mod = buildModule(() => {
    srand(seed);
    for (let index = 0; index < count; index += 1) {
      ids.push(rand().id);
    }
  });
  const report = runSim(mod.toJson(), 1);
  return ids.map((id) => report.var_values[id]);
}

function unseededSequence(count: number): number[] {
  const ids: number[] = [];
  const mod = buildModule(() => {
    for (let index = 0; index < count; index += 1) {
      ids.push(rand().id);
    }
  });
  const report = runSim(mod.toJson(), 1);
  return ids.map((id) => report.var_values[id]);
}

function snapshotState(): VarImpl {
  // getSeed() returns the live state Var, so a capture must copy it first;
  // two captures of the same object would both read its final value.
  const copy = Var(0);
  copy.set(getSeed());
  return copy;
}

test("module identity and public surface", () => {
  assert.equal(typeof srand, "function");
  assert.equal(LCG_MULT, MULT);
  assert.equal(LCG_INC, INC);
  assert.equal(get_seed, getSeed);
  assert.equal(dwRand, dwrand);
  assert.equal(rand_range, randRange);
});

test("function signatures pin the ticket contract", () => {
  const srandFn: (seed: RandSeed) => void = srand;
  const getSeedFn: () => VarImpl = getSeed;
  const randFn: () => VarImpl = rand;
  const dwrandFn: () => VarImpl = dwrand;
  const randRangeFn: (lo: number, hi: number) => VarImpl = randRange;
  assert.equal(typeof srandFn, "function");
  assert.equal(typeof getSeedFn, "function");
  assert.equal(typeof randFn, "function");
  assert.equal(typeof dwrandFn, "function");
  assert.equal(typeof randRangeFn, "function");
});

test("the source has no trigger rewiring and no Math.random", () => {
  const source = fs.readFileSync(
    path.resolve(import.meta.dirname, "..", "random.ts"),
    "utf8",
  );
  for (const token of ["SetNextPtr", "Switch", "Forward", "EUDLightBool", "EUDByteReader", "Math.random"]) {
    assert.equal(source.includes(token), false, `random.ts must not mention ${token}`);
  }
});

test("rand matches the reference sequence", () => {
  for (const seed of [0, 1, 12345, 0xffffffff]) {
    assert.deepEqual(randSequence(seed, 6), referenceRand(seed, 6), `seed ${seed}`);
  }
});

test("dwrand joins two draw hiwords and advances twice", () => {
  for (const seed of [0, 1, 12345, 0xffffffff]) {
    const ids = { draw: -1, state: -1 };
    const mod = buildModule(() => {
      srand(seed);
      ids.draw = dwrand().id;
      ids.state = snapshotState().id;
    });
    const report = runSim(mod.toJson(), 1);
    const expected = referenceDwrand(seed);
    assert.equal(report.var_values[ids.draw], expected.draw, `seed ${seed} draw`);
    assert.equal(report.var_values[ids.state], expected.state, `seed ${seed} state`);
  }
});

test("getSeed returns the current state without advancing", () => {
  const ids = { before: -1, draw: -1, after: -1 };
  const views: VarImpl[] = [];
  const mod = buildModule(() => {
    srand(0xbeef);
    views.push(getSeed(), getSeed());
    ids.before = snapshotState().id;
    ids.draw = rand().id;
    ids.after = snapshotState().id;
  });
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.before], 0xbeef);
  assert.equal(report.var_values[ids.draw], referenceRand(0xbeef, 1)[0]);
  assert.equal(report.var_values[ids.after], nextState(0xbeef));
  assert.equal(views[0], views[1]);
});

test("each module starts from a private state", () => {
  assert.deepEqual(randSequence(12345, 3), randSequence(12345, 3));
  assert.deepEqual(randSequence(12345, 1), referenceRand(12345, 1));
  assert.deepEqual(unseededSequence(2), referenceRand(0, 2));
});

test("two call sites in one module share one state", () => {
  const ids: number[] = [];
  const mod = buildModule(() => {
    srand(7);
    ids.push(rand().id, rand().id);
  });
  const report = runSim(mod.toJson(), 1);
  assert.deepEqual(
    ids.map((id) => report.var_values[id]),
    referenceRand(7, 2),
  );
});

test("randomize folds the tick and updates twice", () => {
  const ids = { first: -1, second: -1 };
  const mod = buildModule(() => {
    srand(0);
    randomize();
    ids.first = snapshotState().id;
    randomize();
    ids.second = snapshotState().id;
  });
  const text = mod.toJson();
  const report = runSim(text, 1);
  // The simulator leaves external memory zeroed, so the tick is 0 here.
  assert.equal(report.var_values[ids.first], INC);
  assert.equal(report.var_values[ids.second], nextState(INC));
  const tickEpd = String((EPD(GAME_TICK_ADDR) >>> 0));
  assert.ok(text.includes(tickEpd), "the game-tick dword must be read");

  const oneMod = buildModule(() => {
    srand(0);
    randomize();
    getSeed();
  });
  assert.equal(text.split(tickEpd).length, oneMod.toJson().split(tickEpd).length);
});

function rangeDraws(lo: number, hi: number, count: number): number[] {
  const ids: number[] = [];
  const mod = buildModule(() => {
    srand(123);
    for (let index = 0; index < count; index += 1) {
      ids.push(randRange(lo, hi).id);
    }
  });
  const report = runSim(mod.toJson(), 1);
  return ids.map((id) => report.var_values[id]);
}

test("randRange draws stay inside the inclusive bounds", () => {
  assert.ok(rangeDraws(0, 100, 8).every((value) => value >= 0 && value <= 100));
  assert.ok(
    rangeDraws(0, 0xfffffffe, 8).every(
      (value) => value >= 0 && value <= 0xfffffffe,
    ),
  );
});

test("a single-value span folds without consuming a draw", () => {
  const ids = { value: -1, state: -1 };
  const mod = buildModule(() => {
    srand(123);
    ids.value = randRange(5, 5).id;
    ids.state = snapshotState().id;
  });
  const report = runSim(mod.toJson(), 1);
  assert.equal(report.var_values[ids.value], 5);
  assert.equal(report.var_values[ids.state], 123);
});

function codeOf(call: () => unknown): string {
  try {
    call();
  } catch (error) {
    assert.ok(error instanceof EudslError, `expected EudslError, got ${String(error)}`);
    return error.code;
  }
  assert.fail("expected the call to raise");
}

test("seed operands are type checked", () => {
  assert.equal(
    codeOf(() => buildModule(() => srand(true as unknown as RandSeed))),
    "EUDSL-TYPE",
  );
  assert.equal(
    codeOf(() => buildModule(() => srand("x" as unknown as RandSeed))),
    "EUDSL-TYPE",
  );
});

test("randRange bounds are type and range checked", () => {
  assert.equal(codeOf(() => randRange(true as unknown as number, 2)), "EUDSL-TYPE");
  assert.equal(codeOf(() => randRange(10, 5)), "EUDSL-RANGE");
  assert.equal(codeOf(() => randRange(-1, 5)), "EUDSL-RANGE");
  assert.equal(codeOf(() => randRange(0, 0xffffffff)), "EUDSL-RANGE");
});

test("calls before buildModule are scope errors", () => {
  assert.equal(codeOf(() => srand(1)), "EUDSL-SCOPE");
  assert.equal(codeOf(() => getSeed()), "EUDSL-SCOPE");
  assert.equal(codeOf(() => randomize()), "EUDSL-SCOPE");
  assert.equal(codeOf(() => rand()), "EUDSL-SCOPE");
  assert.equal(codeOf(() => dwrand()), "EUDSL-SCOPE");
  assert.equal(codeOf(() => randRange(0, 10)), "EUDSL-SCOPE");
});
