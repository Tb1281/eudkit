/**
 * Smoke map 1: runtime `when(..., else)` and multi-way `dispatch()`.
 *
 * Mirrors `else-dispatch.py` 1:1.
 */

import { FixedString, Var, buildModule, dispatch, every, once, when } from "../../../ts/index.ts";

export const module = buildModule(() => {
  const chainSum = Var(0);
  const bstSum = Var(0);
  const elseSum = Var(0);

  const chainDispatch = (v: typeof chainSum): void => {
    dispatch(
      v,
      [
        [0, () => chainSum.iadd(10)],
        [1, () => chainSum.iadd(20)],
        [2, () => chainSum.iadd(30)],
        [3, () => chainSum.iadd(40)],
      ],
      () => chainSum.iadd(999),
    );
  };

  const bstDispatch = (v: typeof bstSum, dst: typeof bstSum): void => {
    dispatch(
      v,
      [
        [0, () => dst.iadd(100)],
        [1, () => dst.iadd(101)],
        [2, () => dst.iadd(102)],
        [3, () => dst.iadd(103)],
        [5, () => dst.iadd(105)],
        [8, () => dst.iadd(108)],
        [13, () => dst.iadd(113)],
        [21, () => dst.iadd(121)],
        [34, () => dst.iadd(134)],
        [55, () => dst.iadd(155)],
        [89, () => dst.iadd(189)],
        [144, () => dst.iadd(244)],
      ],
      () => dst.iadd(500),
    );
  };

  once(() => {
    const probe = Var(0);
    for (const k of [0, 1, 2, 3, 99]) {
      probe.set(k);
      chainDispatch(probe);
    }

    for (const k of [0, 5, 21, 144, 7]) {
      probe.set(k);
      bstDispatch(probe, bstSum);
    }

    probe.set(42);
    when([probe.eq(42)], () => elseSum.iadd(10), () => elseSum.iadd(100));
    probe.set(7);
    when([probe.eq(42)], () => elseSum.iadd(100), () => elseSum.iadd(2));
  });

  const step = Var(0);
  const parity = Var(0);
  const branchFlag = Var(0);
  const liveVal = Var(0);

  every(12, () => {
    step.iadd(1);
    when(step.ge(16), () => step.set(0));
    parity.set(step.bitAnd(1));
    when([parity.eq(0)], () => branchFlag.set(0), () => branchFlag.set(1));
    liveVal.set(0);
    bstDispatch(step, liveVal);
  });

  const line1 = new FixedString(64);
  const line2 = new FixedString(64);
  const line3 = new FixedString(64);
  const verdict = new FixedString(64);

  every(6, () => {
    line1.clear();
    line1.write("DISPATCH CHAIN={:\x1a>4d}/1099 BST={:\x1a>4d}/1070", chainSum, bstSum);
    line1.displayAt(1);

    line2.clear();
    line2.write("WHEN ELSE CHECK={:\x1a>2d}/12", elseSum);
    line2.displayAt(2);

    line3.clear();
    line3.write("LIVE STEP={:\x1a>2d} ODD={:\x1a>1d} BST_OUT={:\x1a>3d}", step, branchFlag, liveVal);
    line3.displayAt(3);

    verdict.clear();
    when(
      [chainSum.eq(1099), bstSum.eq(1070), elseSum.eq(12)],
      () => verdict.write("STATUS: PASS"),
      () => verdict.write("STATUS: FAIL"),
    );
    verdict.displayAt(4);
  });
});
