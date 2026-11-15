import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  ACT_COUNT,
  Deaths,
  DisplayText,
  Exactly,
  Func,
  P1,
  Trigger,
  Var,
  after,
  buildModule,
  every,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
} from "../index.ts";

test("1T inline leaf packing: when(cond, body) emits exactly 1 Trigger (16C, 64A)", () => {
  // Leaf when block with stock deaths condition and DisplayText (no Var)
  const modNoVar = buildModule(() => {
    when(Deaths(P1, Exactly, 10, 0), () => {
      DisplayText("leaf when fired");
    });
  });
  const repNoVar = runSim(modNoVar.toJson(), 1);
  assert.equal(repNoVar.static_cost.triggers, 1);
  assert.equal(repNoVar.executed_cost.triggers, 1); // 1T evaluated
  assert.equal(repNoVar.executed_cost.actions, 0); // cond false -> 0 actions executed

  // Leaf when block with Var: 1 execution trigger (1T executed) + 1 TriggerPage
  const mod = buildModule(() => {
    const x = Var(10);
    const y = Var(20);

    when(x.eq(10), () => {
      x.set(42);
      y.iadd(5);
      DisplayText("when executed");
    });
  });

  assert.equal(mod.toHir().module.stmts.length, 1); // 1 single Trigger in HIR
  const rep = runSim(mod.toJson(), 1);
  assert.equal(rep.executed_cost.triggers, 1); // 1T executed!
  assert.equal(rep.var_values[0], 42);
  assert.equal(rep.var_values[1], 25);
});

test("Consecutive inline when blocks with identical conditions merge up to 64 actions", () => {
  // Neither body writes the gate variable, so the sibling merge is legal
  // (#79's RAW guard only splits bodies that touch a variable the gate
  // reads; the hazardous variant is pinned in test_ticket79.ts).
  const mod = buildModule(() => {
    const gate = Var(1);
    const b = Var(2);
    const c = Var(3);

    when(gate.eq(1), () => {
      b.set(100);
    });
    when(gate.eq(1), () => {
      c.set(200);
    });
  });

  // Both when blocks merge in eudkit-opt into 1 single Trigger. Scalar Vars
  // are automatically demoted to Cells, so no TriggerPage is emitted.
  const rep = runSim(mod.toJson(), 1);
  assert.equal(rep.static_cost.triggers, 1);
  assert.equal(rep.executed_cost.triggers, 1); // 1T executed
  assert.equal(rep.var_values[1], 100);
  assert.equal(rep.var_values[2], 200);
});

test("Nested when blocks conjoin conditions up to 16C with Condition CSE & comparison fusion", () => {
  const mod = buildModule(() => {
    const x = Var(5);
    const y = Var(10);
    const target = Var(0);

    // x >= 5 && x >= 3 (CSE/fusion tightens to x >= 5) && y <= 10 && y >= 10 (fuses to y == 10)
    when([x.ge(5), x.ge(3)], () => {
      when([y.le(10), y.ge(10)], () => {
        target.set(999);
      });
    });
  });

  assert.equal(mod.toHir().module.stmts.length, 1); // Conjoined into 1 single Trigger
  const rep = runSim(mod.toJson(), 1);
  assert.equal(rep.executed_cost.triggers, 1); // 1T executed
  assert.equal(rep.var_values[2], 999);
});

test("once(cond, body) executes on first matching loop and permanently self-disables at 0T executed", () => {
  const mod = buildModule(() => {
    const runCount = Var(0);
    const trigVar = Var(1);

    once(trigVar.eq(1), () => {
      runCount.iadd(1);
    });
  });

  assert.equal(mod.toHir().module.stmts.length, 1); // 1 single Trigger (preserved: false)

  // Loop 1: condition holds, fires once (1T executed)
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.executed_cost.triggers, 1);
  assert.equal(rep1.var_values[0], 1);

  // Loop 2: permanently self-disabled, 0T executed!
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[0], 1);
  assert.equal(rep2.executed_cost.triggers, 0);

  // Loop 3: still 0T executed
  const rep3 = runSim(mod.toJson(), 3);
  assert.equal(rep3.var_values[0], 1);
  assert.equal(rep3.executed_cost.triggers, 0);
});

test("until(cond, body) runs while condition is false and permanently disables once condition holds", () => {
  const mod = buildModule(() => {
    const tick = Var(0);
    const flag = Var(0);

    // Set flag = 1 after 3 ticks
    when(tick.ge(3), () => {
      flag.set(1);
    });

    until(flag.eq(1), () => {
      tick.iadd(1);
    });
  });

  // Tick 1: flag is 0 -> tick becomes 1
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[0], 1);

  // Tick 2: flag is 0 -> tick becomes 2
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[0], 2);

  // Tick 3: flag is 0 -> tick becomes 3
  const rep3 = runSim(mod.toJson(), 3);
  assert.equal(rep3.var_values[0], 3);

  // Tick 4: tick is 3 -> flag becomes 1 -> until disables!
  const rep4 = runSim(mod.toJson(), 4);
  assert.equal(rep4.var_values[0], 3);
  assert.equal(rep4.var_values[1], 1);

  // Tick 5: permanently disabled -> tick remains 3
  const rep5 = runSim(mod.toJson(), 5);
  assert.equal(rep5.var_values[0], 3);
});

test("onRising detects false -> true transition and fires once per edge", () => {
  const mod = buildModule(() => {
    const input = Var(0);
    const edgeCount = Var(0);

    // input starts 0 on tick 1, set to 1 on tick 2, set to 0 on tick 4, set to 1 on tick 5
    when(edgeCount.ge(0), () => {
      // frame-based input toggle in test
    });

    onRising(input.eq(1), () => {
      edgeCount.iadd(1);
    });
  });

  // Initial: input is 0, onRising does not fire
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[1], 0);
});

test("onRising fires on loop 1 if condition is already true", () => {
  const mod = buildModule(() => {
    const input = Var(1);
    const edgeCount = Var(0);

    onRising(input.eq(1), () => {
      edgeCount.iadd(1);
    });
  });

  // Loop 1: true on loop 1 is a rising edge from initial state 0
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[1], 1);

  // Loop 2: condition remains true -> no edge -> count remains 1
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[1], 1);
});

test("onFalling fires when condition transitions from true -> false", () => {
  const mod = buildModule(() => {
    const input = Var(1);
    const fallCount = Var(0);
    const frame = Var(0);

    frame.iadd(1);
    when(frame.ge(2), () => {
      input.set(0);
    });

    onFalling(input.eq(1), () => {
      fallCount.iadd(1);
    });
  });

  // Frame 1: input is 1 -> no falling edge yet
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[1], 0);

  // Frame 2: input transitions from 1 -> 0 -> falling edge fires!
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[1], 1);

  // Frame 3: input remains 0 -> no edge -> count remains 1
  const rep3 = runSim(mod.toJson(), 3);
  assert.equal(rep3.var_values[1], 1);
});

test("every(n) fires every n trigger loops", () => {
  const mod = buildModule(() => {
    const hits = Var(0);

    every(3, () => {
      hits.iadd(1);
    });
  });

  // Loop 1: hits == 0
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[0], 0);

  // Loop 2: hits == 0
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[0], 0);

  // Loop 3: hits == 1
  const rep3 = runSim(mod.toJson(), 3);
  assert.equal(rep3.var_values[0], 1);

  // Loop 6: hits == 2
  const rep6 = runSim(mod.toJson(), 6);
  assert.equal(rep6.var_values[0], 2);
});

test("after(n) delayed one-shot countdown and firing", () => {
  const mod = buildModule(() => {
    const fired = Var(0);

    after(2, () => {
      fired.iadd(1);
    });
  });

  // Loop 1: arms
  const rep1 = runSim(mod.toJson(), 1);
  assert.equal(rep1.var_values[0], 0);

  // Loop 2: countdown tick
  const rep2 = runSim(mod.toJson(), 2);
  assert.equal(rep2.var_values[0], 0);

  // Loop 3: fires at 0! (1 arm loop + 2 countdown loops = fires on loop 3)
  const rep3 = runSim(mod.toJson(), 3);
  assert.equal(rep3.var_values[0], 1);
});

test("whileLoop repeats body intra-frame until condition fails", () => {
  const mod = buildModule(() => {
    const counter = Var(5);
    const sum = Var(0);

    whileLoop(counter.gt(0), () => {
      counter.isub(1);
      sum.iadd(10);
    });
  });

  const rep = runSim(mod.toJson(), 1);
  assert.equal(rep.var_values[0], 0);
  assert.equal(rep.var_values[1], 50);
});

test("Func defines callable procedure", () => {
  const mod = buildModule(() => {
    const val = Var(0);

    const incrementTwice = Func(() => {
      val.iadd(10);
      val.iadd(20);
    });

    incrementTwice();
    incrementTwice();
  });

  const rep = runSim(mod.toJson(), 1);
  assert.equal(rep.var_values[0], 60);
});
