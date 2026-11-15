/**
 * Temporal candidate 2, option C — path-complete restore sim tests.
 *
 * Confirmed tests (D1-D3, plus D2b): each case asserts the CORRECT
 * behaviour. On unmodified code every case failed, confirming the
 * stale-next class that option C (path-complete restore in
 * crates/eudkit-lower/src/control.rs) fixes. These are now passing
 * lock tests for that guarantee.
 *
 * Sim semantics: runSim(mod.toJson(), N) simulates N trigger loops from
 * the initial state, so "loop 2" means the state after two full loops.
 * A frame counter incremented by an unconditional act at the top of the
 * module drives the per-loop conditions.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { Var, buildModule, once, when, whileLoop } from "../index.ts";

test("D1: nested-last stale gate — outer body must not run on an outer-false loop after an inner-false loop", () => {
  // Var order: frame, outerCount, innerCount.
  const mod = buildModule(() => {
    const frame = Var(0);
    const outerCount = Var(0);
    const innerCount = Var(0);

    frame.iadd(1);
    // Outer A: true only on loop 1 (frame == 1 after the increment).
    when(frame.eq(1), () => {
      // Unconditional act in the outer body: second observable.
      outerCount.iadd(1);
      // Inner B: true only on loop 2 (frame == 2), so it is false on
      // loop 1, when the outer restore would have to run.
      when(frame.eq(2), () => {
        innerCount.iadd(1);
      });
    });
  });

  // Loop 1: outer true, inner false. Correct state: frame 1, outer 1, inner 0.
  const rep1 = runSim(mod.toJson(), 1);
  assert.deepEqual(rep1.var_values.slice(0, 3), [1, 1, 0]);

  // Loop 2: outer false, inner true. Correct state: outer body must not
  // run at all, so outer stays 1 and inner stays 0. A stale outer gate
  // (restore skipped on loop 1) enters the body and gives [2, 2, 1].
  const rep2 = runSim(mod.toJson(), 2);
  assert.deepEqual(rep2.var_values.slice(0, 3), [2, 1, 0]);
});

test("D2: while premature exit — a false inner when at the end of the body must loop back to the check, not exit", () => {
  // Var order: counter, sum, innerCount.
  // The inner when body is deliberately non-leaf (it contains a nested
  // when), so the inner when stays a When in lowering instead of being
  // leaf-packed into a single trigger. That is the shape in which a
  // false inner gate jumps straight to the while join.
  const mod = buildModule(() => {
    const counter = Var(3);
    const sum = Var(0);
    const innerCount = Var(0);

    whileLoop(counter.gt(0), () => {
      counter.isub(1);
      sum.iadd(10);
      // Inner when, false on every iteration (counter never reaches 99).
      // It is the last statement of the body.
      when(counter.eq(99), () => {
        innerCount.iadd(1);
        when(innerCount.eq(999), () => {
          innerCount.iadd(10);
        });
      });
    });
  });

  // Correct: three intra-frame iterations, counter 0, sum 30, inner 0.
  // Premature exit after the first iteration gives counter 2, sum 10.
  const rep = runSim(mod.toJson(), 1);
  assert.deepEqual(rep.var_values.slice(0, 3), [0, 30, 0]);
});

test("D2b: leaf-packed inner when in a while body — the same restore-skip defect, manifesting as an infinite loop", () => {
  // Same shape as D2, but the inner when body is a single act, so it
  // leaf-packs into a conditional trigger whose next pointer is the
  // while check. When its condition is false, the while restore (appended
  // to that trigger) is skipped, the check gate's next pointer stays
  // patched to the body start, and the check can never exit: the sim
  // panics with "Infinite trigger loop detected". After the fix, this
  // program must terminate with counter 0, sum 30, inner 0.
  const mod = buildModule(() => {
    const counter = Var(3);
    const sum = Var(0);
    const innerCount = Var(0);

    whileLoop(counter.gt(0), () => {
      counter.isub(1);
      sum.iadd(10);
      when(counter.eq(99), () => {
        innerCount.iadd(1);
      });
    });
  });

  const rep = runSim(mod.toJson(), 1);
  assert.deepEqual(rep.var_values.slice(0, 3), [0, 30, 0]);
});

test("D3: once self-disable plus appended restore — the outer gate must not stay stale after the once leaf disables", () => {
  // Var order: frame, outerCount, onceCount.
  const mod = buildModule(() => {
    const frame = Var(0);
    const outerCount = Var(0);
    const onceCount = Var(0);

    frame.iadd(1);
    // Outer: true on loops 1-2, false on loop 3.
    when(frame.le(2), () => {
      // Unconditional act before the once leaf: observable on loop 3.
      outerCount.iadd(1);
      // Packed once leaf, last in the body: fires only on loop 1.
      once(frame.eq(1), () => {
        onceCount.iadd(1);
      });
    });
  });

  // Loop 1: outer true, once fires. State: frame 1, outer 1, once 1.
  const rep1 = runSim(mod.toJson(), 1);
  assert.deepEqual(rep1.var_values.slice(0, 3), [1, 1, 1]);

  // Loop 2: outer true, once disabled (restore skipped). State: 2, 2, 1.
  const rep2 = runSim(mod.toJson(), 2);
  assert.deepEqual(rep2.var_values.slice(0, 3), [2, 2, 1]);

  // Loop 3: outer false. Correct: body must not run, outer stays 2.
  // A stale outer gate enters the body and gives outer 3.
  const rep3 = runSim(mod.toJson(), 3);
  assert.deepEqual(rep3.var_values.slice(0, 3), [3, 2, 1]);
});
