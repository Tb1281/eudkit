/**
 * once() non-leaf path-complete restore sim tests.
 *
 * A once whose body contains a control statement (here an inner when)
 * cannot pack into a leaf trigger, so it lowers through
 * crates/eudkit-lower/src/control.rs lower_once. That path built a
 * self-disabling gate with no path-complete restore, so later loops
 * walked through the disabled gate via its stale next pointer and
 * re-executed the body every loop. These tests assert the CORRECT
 * behaviour: the body runs exactly once.
 *
 * Sim semantics: runSim(mod.toJson(), N) simulates N trigger loops from
 * the initial state, so "loop 2" means the state after two full loops.
 * A frame counter incremented by an unconditional act at the top of the
 * module drives the per-loop conditions.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import { Var, buildModule, once, when } from "../index.ts";

test("non-leaf once body runs exactly once across loops 1-4", () => {
  // Var order: frame, count.
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);

    frame.iadd(1);
    // Once fires on loop 1 only (frame == 1). The inner when is true on
    // every loop, so a stale gate re-entering the body is observable.
    once(frame.eq(1), () => {
      when(frame.ge(1), () => {
        count.iadd(1);
      });
    });
  });

  // Loop 1: once fires, inner when true. State: frame 1, count 1.
  const rep1 = runSim(mod.toJson(), 1);
  assert.deepEqual(rep1.var_values.slice(0, 2), [1, 1]);

  // Loops 2-4: once disabled. Correct: count stays 1.
  // A stale once gate re-enters the body and gives count 2, 3, 4.
  const rep2 = runSim(mod.toJson(), 2);
  assert.deepEqual(rep2.var_values.slice(0, 2), [2, 1]);
  const rep3 = runSim(mod.toJson(), 3);
  assert.deepEqual(rep3.var_values.slice(0, 2), [3, 1]);
  const rep4 = runSim(mod.toJson(), 4);
  assert.deepEqual(rep4.var_values.slice(0, 2), [4, 1]);
});

test("non-leaf once nested inside when runs exactly once", () => {
  // Var order: frame, count.
  const mod = buildModule(() => {
    const frame = Var(0);
    const count = Var(0);

    frame.iadd(1);
    // Outer when true on every loop; the once inside must still fire once.
    when(frame.ge(1), () => {
      once(frame.eq(1), () => {
        when(frame.ge(1), () => {
          count.iadd(1);
        });
      });
    });
  });

  // Loop 1: outer true, once fires, inner when true. State: 1, 1.
  const rep1 = runSim(mod.toJson(), 1);
  assert.deepEqual(rep1.var_values.slice(0, 2), [1, 1]);

  // Loops 2 and 4: outer true, once disabled. Correct: count stays 1.
  // A stale once gate re-enters the body and gives count 2 and 4.
  const rep2 = runSim(mod.toJson(), 2);
  assert.deepEqual(rep2.var_values.slice(0, 2), [2, 1]);
  const rep4 = runSim(mod.toJson(), 4);
  assert.deepEqual(rep4.var_values.slice(0, 2), [4, 1]);
});
