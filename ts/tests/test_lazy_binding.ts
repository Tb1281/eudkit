/**
 * State-machine tests for the shared `LazyBinding` helper (ADR 0005 §7).
 *
 * `LazyBinding` owns the per-handle deferred-binding machine every lazy
 * declaration uses: eager fast path, active-builder lookup, cached binding,
 * `instantiate` thunk, binding storage, resolution count, first-binding
 * capture, and the post-build zero/one/many policy.
 *
 * These tests exercise the helper at its module seam plus the exact
 * `EUDSL-SCOPE` text every lazy type keeps.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { LazyBinding } from "../lazy-binding.ts";
import {
  TriggerStruct,
  Var,
  buildModule,
  dword,
  lazyDeque,
  lazyFixedArray,
  lazyFixedString,
  lazyQueue,
  lazyStack,
  lazyStruct,
  lazyUnitGroup,
  lazyVar,
} from "../index.ts";

const Point = TriggerStruct("Point", { x: dword(), y: dword() });

function scopeMessage(call: () => unknown): string {
  try {
    call();
  } catch (error) {
    return (error as Error).message;
  }
  throw new Error("expected EUDSL-SCOPE");
}

const never = (): never => {
  throw new Error("post-build read must not instantiate a binding");
};

test("lazy binding: eager handle has no binding state", () => {
  // Eager handles live entirely on their eager fields; this is the helper's
  // no-deferred-state contract. The concrete eager path is exercised by the
  // eager Var below.
  let eager: ReturnType<typeof Var> | undefined;
  buildModule(() => {
    eager = Var(5);
    assert.equal(eager.id, 0);
  });
  assert.equal(eager!.id, 0);
});

test("lazy binding: active builder creates one binding and caches it", () => {
  const state = new LazyBinding<object>("Var");
  const created: object[] = [];
  const instantiate = (): object => {
    created.push({});
    return created[created.length - 1]!;
  };
  const resolved: object[] = [];
  buildModule(() => {
    resolved.push(state.resolve(instantiate));
    resolved.push(state.resolve(instantiate));
  });
  assert.equal(created.length, 1);
  assert.equal(resolved[0], resolved[1]);
  assert.equal(resolved[0], created[0]);
});

test("lazy binding: a single builder captures its binding for post-build reads", () => {
  const state = new LazyBinding<object>("Var");
  const created: object[] = [];
  buildModule(() => {
    state.resolve(() => {
      created.push({});
      return created[0]!;
    });
  });
  assert.equal(created.length, 1);
  assert.equal(state.resolve(never), created[0]);
});

test("lazy binding: two builders keep separate bindings and stay ambiguous", () => {
  const state = new LazyBinding<object>("Var");
  const bindings: object[] = [];
  const body = (): void => {
    bindings.push(state.resolve(() => ({})));
  };
  buildModule(body);
  buildModule(body);
  assert.equal(bindings.length, 2);
  assert.notEqual(bindings[0], bindings[1]);
  assert.equal(
    scopeMessage(() => state.resolve(never)),
    "EUDSL-SCOPE: deferred Var was resolved under 2 builders; post-build read is ambiguous",
  );
});

test("lazy binding: zero builders raises EUDSL-SCOPE without instantiating", () => {
  const state = new LazyBinding<object>("Var");
  assert.equal(
    scopeMessage(() => state.resolve(never)),
    "EUDSL-SCOPE: deferred Var must be used inside buildModule()",
  );
});

test("lazy binding: entry-point spelling is parameterised", () => {
  const state = new LazyBinding<object>("FixedArray", "customEntry");
  assert.equal(
    scopeMessage(() => state.resolve(never)),
    "EUDSL-SCOPE: deferred FixedArray must be used inside customEntry()",
  );
});

test("lazy binding: scope messages are exact for all lazy types", () => {
  const cases: Array<[string, () => unknown]> = [
    ["deferred Var must be used inside buildModule()", () => lazyVar(0).id],
    ["deferred struct must be used inside buildModule()", () => lazyStruct(Point).pageIndex],
    [
      "deferred FixedArray must be used inside buildModule()",
      () => lazyFixedArray("dword", 2).vars,
    ],
    ["deferred Queue must be used inside buildModule()", () => lazyQueue("dword", 2).length],
    ["deferred Deque must be used inside buildModule()", () => lazyDeque("word", 2).length],
    ["deferred Stack must be used inside buildModule()", () => lazyStack(2).pos],
    [
      "deferred UnitGroup must be used inside buildModule()",
      () => lazyUnitGroup(2, { autoPrune: false }).length,
    ],
    [
      "deferred FixedString must be used inside buildModule()",
      () => lazyFixedString(4).messageIndex,
    ],
  ];
  for (const [message, call] of cases) {
    assert.equal(scopeMessage(call), `EUDSL-SCOPE: ${message}`);
  }
});
