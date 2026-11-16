/**
 * Private per-builder linear-congruential random state.
 *
 * The state is one `Var` cached per active `buildModule` builder, so two
 * modules compiled in one process cannot share a seed. `srand` writes the
 * seed by value; `rand` is the HIWORD of one LCG draw and `dwrand` joins the
 * HIWORDs of two draws. `randomize` folds the game-tick dword into the
 * current state.
 */

import { GAME_TICK_ADDR } from "./camera.ts";
import { AtLeast, DeathsX } from "./hir.ts";
import { when } from "./temporal.ts";
import { _getActiveModuleBuilder } from "./trigger.ts";
import { Var, VarImpl, divmod, type VarOperand } from "./var.ts";
import { EPD, EudslError, readEpd, requirePlainInt } from "./view.ts";

/** Seed operand domain: the same set `Var.set` accepts. */
export type RandSeed = VarOperand;

/** LCG multiplier (`eudplib` `f_rand`). */
export const LCG_MULT = 1103515245;
/** LCG increment (`eudplib` `f_rand`). */
export const LCG_INC = 12345;

/** One private state `Var` per builder; never a module-level variable. */
const stateCache = new WeakMap<object, VarImpl>();

function state(): VarImpl {
  const builder = _getActiveModuleBuilder();
  if (builder === null) {
    throw new EudslError(
      "EUDSL-SCOPE",
      "random state must be created inside buildModule()",
    );
  }
  const cached = stateCache.get(builder);
  if (cached !== undefined) return cached;
  const created = Var(0);
  stateCache.set(builder, created);
  return created;
}

/** Advance `current` by one LCG step, modulo 2**32. */
function nextInto(current: VarImpl): void {
  current.imul(LCG_MULT);
  current.iadd(LCG_INC);
}

/** Add the source's bits 16..31 to `dst`, shifted down by `downshift`. */
function hiwordInto(source: VarImpl, dst: VarImpl, downshift: number): void {
  for (let bit = 16; bit < 32; bit += 1) {
    const add = (1 << (bit - downshift)) >>> 0;
    const maskBit = (1 << bit) >>> 0;
    // A masked `AtLeast` is a bit test; a plain `ge(1 << bit)` would also
    // fire for every lower bit, so it cannot extract a HIWORD.
    when(DeathsX(source.value, AtLeast, maskBit, 0, maskBit), () =>
      dst.iadd(add)
    );
  }
}

/** Write `seed` into this builder's private LCG state by value. */
export function srand(seed: RandSeed): void {
  state().set(seed);
}

/** Return a fresh copy of the current LCG state without advancing it. */
export function getSeed(): VarImpl {
  const out = Var(0);
  out.set(state());
  return out;
}

export const get_seed = getSeed;

/** Fold the game-tick dword into the current state, then advance once. */
export function randomize(): void {
  const current = state();
  const tick = readEpd(EPD(GAME_TICK_ADDR), 32, 0, 0xffffffff);
  current.iadd(tick);
  current.imul(LCG_MULT);
  current.iadd(LCG_INC);
}

/** Advance the state once and return its high 16 bits as a fresh `Var`. */
export function rand(): VarImpl {
  const current = state();
  nextInto(current);
  const out = Var(0);
  out.set(0);
  hiwordInto(current, out, 16);
  return out;
}

/** Advance twice and join the two HIWORDs into one 32-bit fresh `Var`. */
export function dwrand(): VarImpl {
  const current = state();
  nextInto(current);
  const out = Var(0);
  out.set(0);
  hiwordInto(current, out, 0);
  nextInto(current);
  hiwordInto(current, out, 16);
  return out;
}

export const dwRand = dwrand;

/**
 * Return an inclusive `[lo, hi]` draw as a fresh `Var`.
 *
 * Bounds are build-time integers. `span === 1` folds to `Var(lo)` and
 * consumes no draw; otherwise the result is `lo + dwrand() % span`.
 */
export function randRange(lo: number, hi: number): VarImpl {
  const loValue = requirePlainInt(lo, "randRange(): lo");
  const hiValue = requirePlainInt(hi, "randRange(): hi");
  if (loValue < 0 || hiValue < loValue || hiValue > 0xffffffff) {
    throw new EudslError(
      "EUDSL-RANGE",
      "randRange(): bounds must satisfy 0 <= lo <= hi <= 0xFFFFFFFF, " +
        `got ${loValue}..${hiValue}`,
    );
  }
  const span = hiValue - loValue + 1;
  if (span > 0xffffffff) {
    throw new EudslError(
      "EUDSL-RANGE",
      `randRange(): span ${span} exceeds 0xFFFFFFFF`,
    );
  }
  if (span === 1) return Var(loValue);
  const draw = dwrand();
  const [, rem] = divmod(draw, span);
  const out = Var(loValue);
  out.iadd(rem);
  return out;
}

export const rand_range = randRange;
