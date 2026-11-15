/**
 * `lazyQueue` / `lazyDeque` / `lazyStack` / `lazyUnitGroup` public API
 * tests (globals P3).
 *
 * The factories are thin public wrappers over the P1-01/P2-01 deferred
 * pattern on the eager container classes: a handle declared anywhere (no
 * builder needed) that resolves to a fully eager binding on first
 * resolving use inside `buildModule`, one binding per builder. Binding
 * creation is atomic (R3): one full eager constructor call — every var,
 * out box, slot trigger, and auto-prune trigger in eager order. Eager
 * containers are unchanged; the lazy factories are opt-in only.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  Deque,
  Queue,
  Stack,
  TriggerStruct,
  UnitGroup,
  Var,
  buildModule,
  dword,
  lazyDeque,
  lazyQueue,
  lazyStack,
  lazyUnitGroup,
  when,
  type TriggerStructInstance,
  type VarImpl,
} from "../index.ts";
import {
  dq as fixtureDeque,
  q as fixtureQueue,
  st as fixtureStack,
  ug as fixtureUnitGroup,
} from "./lazy_queue_fixture.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const EUDKIT_COMPILE = path.join(REPO_ROOT, "target", "debug", "eudkit-compile");

const Point = TriggerStruct("Point", { x: dword(), y: dword() });

// Full pipeline comparison: `dump-ir` runs optimize_module then
// lowering, so it exercises the storage analysis and the rendered LIR.
// The comparison is the ENTIRE dump-ir output (storage report including
// the per-var Allocations listing, plus the rendered LIR).
function runDumpIr(hirJson: string): string {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-p3-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf-8");
    const out = execFileSync(EUDKIT_COMPILE, ["dump-ir", hirPath], {
      encoding: "utf-8",
    });
    assert.ok(out.includes("LIR v3.0"), "dump-ir output must contain an LIR section");
    assert.ok(
      out.includes("Allocations:"),
      "dump-ir output must contain the Allocations listing",
    );
    return out;
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function hirJson(mod: { toJson(): string }): any {
  return JSON.parse(mod.toJson());
}

function decls(mod: { toJson(): string }): Array<[number, number]> {
  return hirJson(mod).module.vars.map((v: any) => [v.id, v.init]);
}

function assertCode(fn: () => unknown, code: string): void {
  assert.throws(fn, (e: any) => {
    assert.equal(e.code, code);
    return true;
  });
}

function errorOf(fn: () => unknown): any {
  try {
    fn();
  } catch (e) {
    return e;
  }
  assert.fail("expected the call to throw");
}

function assertDumpEqual(
  lazyBody: () => void,
  eagerBody: () => void,
  label: string,
): void {
  const lazyMod = buildModule(lazyBody);
  const eagerMod = buildModule(eagerBody);
  assert.equal(
    runDumpIr(lazyMod.toJson()),
    runDumpIr(eagerMod.toJson()),
    `${label} must be dump-ir-identical to its eager twin`,
  );
}

// Out-of-order first use: B is touched before A, so allocation order must
// match the eager twin declared in that same order and differ from the
// declaration-order twin.
function outOfOrderCase(
  label: string,
  lazyA: any,
  lazyB: any,
  makeA: () => any,
  makeB: () => any,
  use: (handle: any) => void,
): void {
  const lazyIr = runDumpIr(
    buildModule(() => {
      use(lazyB);
      use(lazyA);
    }).toJson(),
  );
  const bFirstIr = runDumpIr(
    buildModule(() => {
      // Bind B immediately before its first use, then bind A before its
      // first use: first-use order, not declaration order.
      const b = makeB();
      use(b);
      const a = makeA();
      use(a);
    }).toJson(),
  );
  const aFirstIr = runDumpIr(
    buildModule(() => {
      const a = makeA();
      const b = makeB();
      use(b);
      use(a);
    }).toJson(),
  );
  assert.equal(lazyIr, bFirstIr, `${label}: B-first eager twin must match`);
  assert.notEqual(lazyIr, aFirstIr, `${label}: declaration-order twin must differ`);
}

// Conditional first use (R4): the binding's constructor statements land
// in the conditional scope, so the eager twin must be constructed there.
function conditionalFirstUseCase(
  label: string,
  lazy: any,
  make: () => any,
  inside: (handle: any) => void,
  after: (handle: any) => void,
): void {
  const lazyMod = buildModule(() => {
    const flag = Var(1);
    when(flag.eq(1), () => {
      inside(lazy);
    });
    after(lazy);
  });
  const twinMod = buildModule(() => {
    const flag = Var(1);
    let eager: any = null;
    when(flag.eq(1), () => {
      eager = make();
      inside(eager);
    });
    after(eager!);
  });
  assert.equal(
    runDumpIr(lazyMod.toJson()),
    runDumpIr(twinMod.toJson()),
    `${label}: conditional first use must match the same-scope eager twin`,
  );
}

// Separate bindings per builder (D3): the same handle used under two
// builders must compile like an eager container declared in each scope.
function twoBuilderCase(
  label: string,
  lazy: any,
  make: () => any,
  use: (handle: any) => void,
  idOf: (handle: any) => number,
): void {
  let firstId = -1;
  let secondId = -1;
  const mod1 = buildModule(() => {
    use(lazy);
    firstId = idOf(lazy);
  });
  const mod2 = buildModule(() => {
    const first = Var(0);
    first.set(1);
    use(lazy);
    secondId = idOf(lazy);
  });
  const twin1 = buildModule(() => {
    const eager = make();
    use(eager);
  });
  const twin2 = buildModule(() => {
    const first = Var(0);
    first.set(1);
    const eager = make();
    use(eager);
  });
  assert.deepEqual(hirJson(mod1), hirJson(twin1), `${label}: builder 1 twin`);
  assert.deepEqual(hirJson(mod2), hirJson(twin2), `${label}: builder 2 twin`);
  assert.notEqual(firstId, secondId, `${label}: bindings must have different ids`);
}

// Post-build rule (P1-01): one builder -> cached binding; several ->
// ambiguous EUDSL-SCOPE; never resolved -> EUDSL-SCOPE.
function postBuildCase(
  label: string,
  makeLazy: () => any,
  use: (handle: any) => void,
  idOf: (handle: any) => number,
): void {
  const single = makeLazy();
  let captured = -1;
  buildModule(() => {
    use(single);
    captured = idOf(single);
  });
  assert.equal(idOf(single), captured, `${label}: single record must be cached`);

  const multi = makeLazy();
  buildModule(() => use(multi));
  buildModule(() => use(multi));
  assertCode(() => idOf(multi), "EUDSL-SCOPE");

  const never = makeLazy();
  assertCode(() => idOf(never), "EUDSL-SCOPE");
}

test("P3: module-scope declarations allocate nothing; pure reads work", () => {
  const q = lazyQueue("dword", 3);
  const dq = lazyDeque("word", 2);
  const st = lazyStack(4);
  const ug = lazyUnitGroup(2, { autoPrune: false, unlimiter: true });
  const defaultUg = lazyUnitGroup(2);

  assert.equal(q.capacity, 3);
  assert.equal(dq.capacity, 2);
  assert.equal(st.capacity, 4);
  assert.equal(ug.capacity, 2);
  assert.equal(ug.autoPrune, false);
  assert.equal(ug.unlimiter, true);
  assert.equal(defaultUg.autoPrune, true);
  assert.equal(defaultUg.unlimiter, false);

  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandles = buildModule(body);
  const withoutHandles = buildModule(body);
  // Identical output proves the unused handles consumed no ids, emitted
  // no decls/triggers, and no statements.
  assert.deepEqual(hirJson(withHandles), hirJson(withoutHandles));
  assert.deepEqual(decls(withHandles), [[0, 0]]);
});

test("P3: resolving reads outside a builder throw EUDSL-SCOPE", () => {
  const q = lazyQueue("dword", 2);
  assertCode(() => q.length, "EUDSL-SCOPE");
  assertCode(() => q.head, "EUDSL-SCOPE");
  assertCode(() => q.tail, "EUDSL-SCOPE");
  assertCode(() => q.outBox, "EUDSL-SCOPE");
  assertCode(() => q.slots, "EUDSL-SCOPE");
  assertCode(() => q.empty(), "EUDSL-SCOPE");
  assertCode(() => q.full(), "EUDSL-SCOPE");
  assertCode(() => q.append(1), "EUDSL-SCOPE");
  assertCode(() => q.popleft(), "EUDSL-SCOPE");
  assertCode(() => q.clear(), "EUDSL-SCOPE");

  const dq = lazyDeque("dword", 2);
  assertCode(() => dq.length, "EUDSL-SCOPE");
  assertCode(() => dq.left, "EUDSL-SCOPE");
  assertCode(() => dq.right, "EUDSL-SCOPE");
  assertCode(() => dq.outLeft, "EUDSL-SCOPE");
  assertCode(() => dq.outRight, "EUDSL-SCOPE");
  assertCode(() => dq.leftSlots, "EUDSL-SCOPE");
  assertCode(() => dq.rightSlots, "EUDSL-SCOPE");
  assertCode(() => dq.append(1), "EUDSL-SCOPE");
  assertCode(() => dq.appendleft(1), "EUDSL-SCOPE");
  assertCode(() => dq.pop(), "EUDSL-SCOPE");
  assertCode(() => dq.popleft(), "EUDSL-SCOPE");

  const st = lazyStack(2);
  assertCode(() => st.storage, "EUDSL-SCOPE");
  assertCode(() => st.pos, "EUDSL-SCOPE");
  assertCode(() => st.length, "EUDSL-SCOPE");
  assertCode(() => st.empty(), "EUDSL-SCOPE");
  assertCode(() => st.full(), "EUDSL-SCOPE");
  assertCode(() => st.push(1), "EUDSL-SCOPE");
  assertCode(() => st.pop(), "EUDSL-SCOPE");
  assertCode(() => st.clear(), "EUDSL-SCOPE");

  const ug = lazyUnitGroup(2);
  assertCode(() => ug.length, "EUDSL-SCOPE");
  assertCode(() => ug.slots, "EUDSL-SCOPE");
  assertCode(() => ug.empty(), "EUDSL-SCOPE");
  assertCode(() => ug.full(), "EUDSL-SCOPE");
  assertCode(() => ug.add(1), "EUDSL-SCOPE");
  assertCode(() => ug.remove(1), "EUDSL-SCOPE");
  assertCode(() => ug.for_each(() => {}), "EUDSL-SCOPE");
  assertCode(() => ug.clear(), "EUDSL-SCOPE");
});

test("P3: bounds and element-type errors match the eager constructor at declaration", () => {
  const capacityCases: Array<[string, () => unknown, () => unknown]> = [
    ["Queue", () => lazyQueue("dword", 0), () => new Queue("dword", 0)],
    ["Queue fraction", () => lazyQueue("dword", 1.5), () => new Queue("dword", 1.5)],
    ["Deque", () => lazyDeque("word", -1), () => new Deque("word", -1)],
    ["Stack", () => lazyStack(0), () => new Stack(0)],
    ["UnitGroup", () => lazyUnitGroup(-1), () => new UnitGroup(-1)],
  ];
  for (const [label, lazyCall, eagerCall] of capacityCases) {
    const lazyError = errorOf(lazyCall);
    const eagerError = errorOf(eagerCall);
    assert.equal(lazyError.code, "EUDSL-BOUNDS", `${label}: code`);
    assert.equal(lazyError.message, eagerError.message, `${label}: message`);
  }

  for (const [label, eagerCall, lazyCall] of [
    ["Queue", () => new Queue("nope" as any, 2), () => lazyQueue("nope" as any, 2)],
    ["Deque", () => new Deque("nope" as any, 2), () => lazyDeque("nope" as any, 2)],
  ] as Array<[string, () => unknown, () => unknown]>) {
    // The eager constructor allocates vars before validating the element,
    // so capture its error inside a builder.
    let eagerError: any;
    buildModule(() => {
      eagerError = errorOf(eagerCall);
    });
    const lazyError = errorOf(lazyCall);
    assert.equal(lazyError.code, "EUDSL-BOUNDS", `${label}: code`);
    assert.equal(lazyError.message, eagerError.message, `${label}: message`);
  }
});

test("P3: Queue core operations match the eager twin", () => {
  const q = lazyQueue("dword", 3);
  assertDumpEqual(
    () => {
      const out = Var(0);
      q.append(1);
      q.push(2);
      out.set(q.popleft() as VarImpl);
      out.set(q.pop() as VarImpl);
      when(q.empty(), () => out.iadd(1));
      when(q.full(), () => out.iadd(2));
      out.set(q.length);
      out.set(q.head);
      out.set(q.tail);
      out.set(q.outBox as VarImpl);
      q.clear();
    },
    () => {
      const out = Var(0);
      const eager = new Queue("dword", 3);
      eager.append(1);
      eager.push(2);
      out.set(eager.popleft() as VarImpl);
      out.set(eager.pop() as VarImpl);
      when(eager.empty(), () => out.iadd(1));
      when(eager.full(), () => out.iadd(2));
      out.set(eager.length);
      out.set(eager.head);
      out.set(eager.tail);
      out.set(eager.outBox as VarImpl);
      eager.clear();
    },
    "Queue operations",
  );
});

test("P3: Deque core operations match the eager twin", () => {
  const dq = lazyDeque("word", 3);
  assertDumpEqual(
    () => {
      const out = Var(0);
      dq.append(1);
      dq.appendleft(2);
      out.set(dq.pop() as VarImpl);
      out.set(dq.popleft() as VarImpl);
      when(dq.empty(), () => out.iadd(1));
      when(dq.full(), () => out.iadd(2));
      out.set(dq.length);
      out.set(dq.left);
      out.set(dq.right);
      out.set(dq.outLeft as VarImpl);
      out.set(dq.outRight as VarImpl);
    },
    () => {
      const out = Var(0);
      const eager = new Deque("word", 3);
      eager.append(1);
      eager.appendleft(2);
      out.set(eager.pop() as VarImpl);
      out.set(eager.popleft() as VarImpl);
      when(eager.empty(), () => out.iadd(1));
      when(eager.full(), () => out.iadd(2));
      out.set(eager.length);
      out.set(eager.left);
      out.set(eager.right);
      out.set(eager.outLeft as VarImpl);
      out.set(eager.outRight as VarImpl);
    },
    "Deque operations",
  );
});

test("P3: Stack core operations match the eager twin", () => {
  const st = lazyStack(3);
  assertDumpEqual(
    () => {
      const out = Var(0);
      st.push(1);
      st.push(2);
      out.set(st.pop());
      when(st.empty(), () => out.iadd(1));
      when(st.full(), () => out.iadd(2));
      out.set(st.length);
      out.set(st.pos);
      out.set(st.storage.get(0) as VarImpl);
      st.clear();
    },
    () => {
      const out = Var(0);
      const eager = new Stack(3);
      eager.push(1);
      eager.push(2);
      out.set(eager.pop());
      when(eager.empty(), () => out.iadd(1));
      when(eager.full(), () => out.iadd(2));
      out.set(eager.length);
      out.set(eager.pos);
      out.set(eager.storage.get(0) as VarImpl);
      eager.clear();
    },
    "Stack operations",
  );
});

test("P3: UnitGroup core operations and auto-prune match the eager twin", () => {
  const ug = lazyUnitGroup(3);
  assertDumpEqual(
    () => {
      const out = Var(0);
      ug.add(1000);
      ug.add(2000);
      ug.remove(1000);
      ug.for_each((epd) => out.set(epd));
      when(ug.empty(), () => out.iadd(1));
      when(ug.full(), () => out.iadd(2));
      out.set(ug.length);
      out.set(ug.slots[0]);
      ug.clear();
    },
    () => {
      const out = Var(0);
      const eager = new UnitGroup(3);
      eager.add(1000);
      eager.add(2000);
      eager.remove(1000);
      eager.for_each((epd) => out.set(epd));
      when(eager.empty(), () => out.iadd(1));
      when(eager.full(), () => out.iadd(2));
      out.set(eager.length);
      out.set(eager.slots[0]);
      eager.clear();
    },
    "UnitGroup operations",
  );

  const noPrune = lazyUnitGroup(2, { autoPrune: false });
  assertDumpEqual(
    () => {
      noPrune.add(7);
      noPrune.remove(7);
    },
    () => {
      const eager = new UnitGroup(2, { autoPrune: false });
      eager.add(7);
      eager.remove(7);
    },
    "UnitGroup autoPrune=false",
  );

  const unlimiter = lazyUnitGroup(2, { unlimiter: true });
  assertDumpEqual(
    () => {
      unlimiter.add(9);
      unlimiter.remove(9);
    },
    () => {
      const eager = new UnitGroup(2, { unlimiter: true });
      eager.add(9);
      eager.remove(9);
    },
    "UnitGroup unlimiter",
  );
});

test("P3: struct-element Queue and Deque match eager twins", () => {
  const q = lazyQueue(Point, 2);
  assertDumpEqual(
    () => {
      const out = Var(0);
      q.append(Point({ x: 7, y: 9 }));
      q.append(Point({ x: 1, y: 2 }));
      const first = q.popleft() as TriggerStructInstance<"x" | "y">;
      out.set(first.x);
      out.set(first.y);
      const second = q.pop() as TriggerStructInstance<"x" | "y">;
      out.set(second.x);
    },
    () => {
      const out = Var(0);
      // Arguments are evaluated before the resolving call, so the first
      // struct is allocated before the eager queue binding.
      const firstArg = Point({ x: 7, y: 9 });
      const eager = new Queue(Point, 2);
      eager.append(firstArg);
      eager.append(Point({ x: 1, y: 2 }));
      const first = eager.popleft() as TriggerStructInstance<"x" | "y">;
      out.set(first.x);
      out.set(first.y);
      const second = eager.pop() as TriggerStructInstance<"x" | "y">;
      out.set(second.x);
    },
    "struct Queue",
  );

  const dq = lazyDeque(Point, 2);
  assertDumpEqual(
    () => {
      const out = Var(0);
      dq.append(Point({ x: 3, y: 4 }));
      dq.appendleft(Point({ x: 5, y: 6 }));
      const first = dq.popleft() as TriggerStructInstance<"x" | "y">;
      out.set(first.x);
      const second = dq.pop() as TriggerStructInstance<"x" | "y">;
      out.set(second.y);
    },
    () => {
      const out = Var(0);
      // First-use order: the first argument is evaluated before the
      // binding exists, the second after it.
      const firstArg = Point({ x: 3, y: 4 });
      const eager = new Deque(Point, 2);
      eager.append(firstArg);
      eager.appendleft(Point({ x: 5, y: 6 }));
      const first = eager.popleft() as TriggerStructInstance<"x" | "y">;
      out.set(first.x);
      const second = eager.pop() as TriggerStructInstance<"x" | "y">;
      out.set(second.y);
    },
    "struct Deque",
  );
});

test("P3: out-of-order first use follows first-use order (R1)", () => {
  outOfOrderCase(
    "Queue",
    lazyQueue("dword", 2),
    lazyQueue("dword", 2),
    () => new Queue("dword", 2),
    () => new Queue("dword", 2),
    (h) => h.append(1),
  );
  outOfOrderCase(
    "Deque",
    lazyDeque("word", 2),
    lazyDeque("word", 2),
    () => new Deque("word", 2),
    () => new Deque("word", 2),
    (h) => h.appendleft(1),
  );
  outOfOrderCase(
    "Stack",
    lazyStack(2),
    lazyStack(2),
    () => new Stack(2),
    () => new Stack(2),
    (h) => h.push(1),
  );
  outOfOrderCase(
    "UnitGroup",
    lazyUnitGroup(2),
    lazyUnitGroup(2),
    () => new UnitGroup(2),
    () => new UnitGroup(2),
    (h) => h.add(1000),
  );
});

test("P3: conditional first use matches the eager twin constructed in the same scope (R4)", () => {
  conditionalFirstUseCase(
    "Queue",
    lazyQueue("dword", 2),
    () => new Queue("dword", 2),
    (h) => h.append(5),
    (h) => h.popleft(),
  );
  conditionalFirstUseCase(
    "Deque",
    lazyDeque("word", 2),
    () => new Deque("word", 2),
    (h) => h.appendleft(5),
    (h) => h.pop(),
  );
  conditionalFirstUseCase(
    "Stack",
    lazyStack(2),
    () => new Stack(2),
    (h) => h.push(5),
    (h) => h.pop(),
  );
  conditionalFirstUseCase(
    "UnitGroup",
    lazyUnitGroup(2),
    () => new UnitGroup(2),
    (h) => h.add(1000),
    (h) => h.remove(1000),
  );
});

test("P3: never-used handles emit nothing and consume no ids", () => {
  const q = lazyQueue("dword", 2);
  const dq = lazyDeque("word", 2);
  const st = lazyStack(2);
  const ug = lazyUnitGroup(2);
  void q;
  void dq;
  void st;
  void ug;
  const body = () => {
    const v = Var(0);
    v.set(1);
  };
  const withHandles = buildModule(body);
  const withoutHandles = buildModule(body);
  assert.deepEqual(hirJson(withHandles), hirJson(withoutHandles));
  assert.deepEqual(decls(withHandles), [[0, 0]]);
  assert.ok(
    runDumpIr(withHandles.toJson()).includes("Total Variables: 1"),
    "unused handles must not add variables",
  );
});

test("P3: two-builder use creates separate bindings with different ids", () => {
  twoBuilderCase(
    "Queue",
    lazyQueue("dword", 2),
    () => new Queue("dword", 2),
    (h) => h.append(1),
    (h) => h.length.id,
  );
  twoBuilderCase(
    "Deque",
    lazyDeque("word", 2),
    () => new Deque("word", 2),
    (h) => h.appendleft(1),
    (h) => h.length.id,
  );
  twoBuilderCase(
    "Stack",
    lazyStack(2),
    () => new Stack(2),
    (h) => h.push(1),
    (h) => h.length.id,
  );
  twoBuilderCase(
    "UnitGroup",
    lazyUnitGroup(2),
    () => new UnitGroup(2),
    (h) => h.add(1000),
    (h) => h.length.id,
  );
});

test("P3: post-build read follows the single-record rule; nothing allocates post-build", () => {
  postBuildCase(
    "Queue",
    () => lazyQueue("dword", 2),
    (h) => h.append(7),
    (h) => h.length.id,
  );
  postBuildCase(
    "Deque",
    () => lazyDeque("word", 2),
    (h) => h.append(7),
    (h) => h.length.id,
  );
  postBuildCase(
    "Stack",
    () => lazyStack(2),
    (h) => h.push(7),
    (h) => h.length.id,
  );
  postBuildCase(
    "UnitGroup",
    () => lazyUnitGroup(2),
    (h) => h.add(7),
    (h) => h.length.id,
  );

  // A post-build throw and a post-build cached read must both allocate
  // nothing: the next build still starts at id 0 with one var.
  const after = buildModule(() => {
    const v = Var(0);
    v.set(1);
  });
  assert.deepEqual(decls(after), [[0, 0]]);
});

test("P3: cross-file handles match eager twins", () => {
  assertDumpEqual(
    () => {
      const out = Var(0);
      fixtureQueue.append(5);
      out.set(fixtureQueue.popleft() as VarImpl);
      fixtureDeque.append(7);
      fixtureDeque.appendleft(6);
      out.set(fixtureDeque.pop() as VarImpl);
      fixtureStack.push(9);
      out.set(fixtureStack.pop());
      fixtureUnitGroup.add(1000);
      fixtureUnitGroup.remove(1000);
    },
    () => {
      const out = Var(0);
      const q = new Queue("dword", 3);
      q.append(5);
      out.set(q.popleft() as VarImpl);
      const dq = new Deque("word", 2);
      dq.append(7);
      dq.appendleft(6);
      out.set(dq.pop() as VarImpl);
      const st = new Stack(2);
      st.push(9);
      out.set(st.pop());
      const ug = new UnitGroup(2, { autoPrune: false });
      ug.add(1000);
      ug.remove(1000);
    },
    "cross-file containers",
  );
});

test("P3: byte-identity corpus (full dump-ir, all four containers)", () => {
  const cases: Array<{
    name: string;
    lazyBody: () => void;
    eagerBody: () => void;
  }> = [];

  {
    const q = lazyQueue("dword", 4);
    cases.push({
      name: "Queue dword append/appendleft-free",
      lazyBody: () => {
        const out = Var(0);
        q.append(11);
        q.append(22);
        out.set(q.popleft() as VarImpl);
        out.set(q.pop() as VarImpl);
        q.clear();
      },
      eagerBody: () => {
        const out = Var(0);
        const eager = new Queue("dword", 4);
        eager.append(11);
        eager.append(22);
        out.set(eager.popleft() as VarImpl);
        out.set(eager.pop() as VarImpl);
        eager.clear();
      },
    });
  }

  {
    const q = lazyQueue("byte", 3);
    cases.push({
      name: "Queue byte",
      lazyBody: () => {
        const out = Var(0);
        q.append(0x1ff);
        out.set(q.popleft() as VarImpl);
      },
      eagerBody: () => {
        const out = Var(0);
        const eager = new Queue("byte", 3);
        eager.append(0x1ff);
        out.set(eager.popleft() as VarImpl);
      },
    });
  }

  {
    const dq = lazyDeque("word", 4);
    cases.push({
      name: "Deque word",
      lazyBody: () => {
        const out = Var(0);
        dq.append(1);
        dq.appendleft(2);
        out.set(dq.pop() as VarImpl);
        out.set(dq.popleft() as VarImpl);
      },
      eagerBody: () => {
        const out = Var(0);
        const eager = new Deque("word", 4);
        eager.append(1);
        eager.appendleft(2);
        out.set(eager.pop() as VarImpl);
        out.set(eager.popleft() as VarImpl);
      },
    });
  }

  {
    const st = lazyStack(4);
    cases.push({
      name: "Stack",
      lazyBody: () => {
        const out = Var(0);
        st.push(1);
        st.push(2);
        out.set(st.pop());
        st.clear();
      },
      eagerBody: () => {
        const out = Var(0);
        const eager = new Stack(4);
        eager.push(1);
        eager.push(2);
        out.set(eager.pop());
        eager.clear();
      },
    });
  }

  {
    const ug = lazyUnitGroup(3);
    cases.push({
      name: "UnitGroup auto-prune",
      lazyBody: () => {
        const out = Var(0);
        ug.add(1000);
        ug.add(2000);
        ug.remove(1000);
        ug.for_each((epd) => out.set(epd));
        ug.clear();
      },
      eagerBody: () => {
        const out = Var(0);
        const eager = new UnitGroup(3);
        eager.add(1000);
        eager.add(2000);
        eager.remove(1000);
        eager.for_each((epd) => out.set(epd));
        eager.clear();
      },
    });
  }

  {
    const ug = lazyUnitGroup(2, { autoPrune: false, unlimiter: true });
    cases.push({
      name: "UnitGroup no-prune unlimiter",
      lazyBody: () => {
        ug.add(1);
        ug.remove(1);
      },
      eagerBody: () => {
        const eager = new UnitGroup(2, { autoPrune: false, unlimiter: true });
        eager.add(1);
        eager.remove(1);
      },
    });
  }

  {
    const q = lazyQueue(Point, 2);
    cases.push({
      name: "struct Queue",
      lazyBody: () => {
        const out = Var(0);
        q.append(Point({ x: 7, y: 9 }));
        const got = q.popleft() as TriggerStructInstance<"x" | "y">;
        out.set(got.x);
      },
      eagerBody: () => {
        const out = Var(0);
        const arg = Point({ x: 7, y: 9 });
        const eager = new Queue(Point, 2);
        eager.append(arg);
        const got = eager.popleft() as TriggerStructInstance<"x" | "y">;
        out.set(got.x);
      },
    });
  }

  {
    const dq = lazyDeque(Point, 2);
    cases.push({
      name: "struct Deque",
      lazyBody: () => {
        const out = Var(0);
        dq.append(Point({ x: 1, y: 2 }));
        dq.appendleft(Point({ x: 3, y: 4 }));
        const got = dq.pop() as TriggerStructInstance<"x" | "y">;
        out.set(got.y);
      },
      eagerBody: () => {
        const out = Var(0);
        const firstArg = Point({ x: 1, y: 2 });
        const eager = new Deque(Point, 2);
        eager.append(firstArg);
        eager.appendleft(Point({ x: 3, y: 4 }));
        const got = eager.pop() as TriggerStructInstance<"x" | "y">;
        out.set(got.y);
      },
    });
  }

  for (const c of cases) {
    const lazyMod = buildModule(c.lazyBody);
    const eagerMod = buildModule(c.eagerBody);
    assert.equal(
      runDumpIr(lazyMod.toJson()),
      runDumpIr(eagerMod.toJson()),
      `corpus case ${c.name} must be byte-identical`,
    );
  }
});

// ---------------------------------------------------------------------------
// Ticket #48: the container element write is a closed operand domain
// (ADR 0019). Case names are kept identical to the python sibling
// (`py/tests/test_lazy_queue.py`) so the pair is diffable row by row.
// ---------------------------------------------------------------------------

// Non-plain-int element operands. The labels are the python sibling's, with
// the one-per-language "nothing" row spelled out: ts has `undefined` and
// `null`, py has a single `None`.
const BAD_ELEMENT_VALUES: Array<[string, any]> = [
  ["float", 1.5],
  ["nan", NaN],
  ["infinity", Infinity],
  ["bool", true],
  ["str", "5"],
  ["undefined", undefined],
  ["null", null],
];

/**
 * Build `call` inside a fresh module and assert exactly one `EUDSL-TYPE`.
 *
 * Catching inside the builder is the eagerness probe: the returned JSON is
 * the failed module, which T-A3 compares against a container-only control.
 */
function assertTypeErrorInBuilder(
  call: () => unknown,
  ...needles: string[]
): string {
  const captured: any[] = [];
  const mod = buildModule(() => {
    try {
      call();
    } catch (e) {
      captured.push(e);
    }
  });
  assert.equal(captured.length, 1, "the call must raise inside the builder");
  assert.equal(captured[0].code, "EUDSL-TYPE");
  const message = String(captured[0].message ?? captured[0]);
  for (const needle of needles) {
    assert.ok(message.includes(needle), `${message} must contain ${needle}`);
  }
  return mod.toJson();
}

test("container element write rejects bad operand", () => {
  // HEAD falls through as a `Var`; ts builds HIR byte-identical to `push(1)`
  // (6014), while py dies in `pageSlotBitCond` with a raw AttributeError.
  const writes: Record<string, (value: any) => unknown> = {
    "Queue.push": (value) => new Queue("dword", 4).push(value),
    "Queue.append": (value) => new Queue("dword", 4).append(value),
    "Deque.append": (value) => new Deque("dword", 4).append(value),
    "Deque.appendleft": (value) => new Deque("dword", 4).appendleft(value),
    "Stack.push": (value) => new Stack(4).push(value),
  };
  for (const [name, write] of Object.entries(writes)) {
    for (const [label, value] of BAD_ELEMENT_VALUES) {
      assertTypeErrorInBuilder(() => write(value), "must be a plain integer");
    }
  }
});

test("container element write controls are unchanged", () => {
  // The lengths are pinned literally and are identical on py and ts, so the
  // two suites pin one number, not two.
  const controls: Array<[string, number, () => unknown]> = [
    ["Queue.push(0)", 6014, () => new Queue("dword", 4).push(0)],
    ["Queue.push(-1)", 6050, () => new Queue("dword", 4).push(-1)],
    ["Queue.push(0xFFFFFFFF)", 6050, () => new Queue("dword", 4).push(0xffffffff)],
    ["Deque.append(0)", 9773, () => new Deque("dword", 4).append(0)],
    ["Deque.append(-1)", 9845, () => new Deque("dword", 4).append(-1)],
    ["Deque.append(0xFFFFFFFF)", 9845, () => new Deque("dword", 4).append(0xffffffff)],
    ["Deque.appendleft(0)", 9773, () => new Deque("dword", 4).appendleft(0)],
    ["Deque.appendleft(-1)", 9845, () => new Deque("dword", 4).appendleft(-1)],
    [
      "Deque.appendleft(0xFFFFFFFF)",
      9845,
      () => new Deque("dword", 4).appendleft(0xffffffff),
    ],
    ["Stack.push(0)", 3190, () => new Stack(4).push(0)],
    ["Stack.push(-1)", 3199, () => new Stack(4).push(-1)],
    ["Stack.push(0xFFFFFFFF)", 3199, () => new Stack(4).push(0xffffffff)],
    [
      "Queue.push(Var(0))",
      6732,
      () => {
        const v = Var(0);
        new Queue("dword", 4).push(v);
      },
    ],
    [
      "Queue(Point,4).push(Point)",
      10247,
      () => new Queue(Point, 4).push(Point({ x: 1, y: 2 })),
    ],
  ];
  for (const [label, want, body] of controls) {
    assert.equal(buildModule(body).toJson().length, want, label);
  }
});

test("container element error emits nothing", () => {
  // HEAD leaks the whole slot write (6014 against a 1984 control); after the
  // guard fires before the first trigger the two are byte-identical.
  const queueFailed = assertTypeErrorInBuilder(
    () => new Queue("dword", 4).push(1.5),
    "must be a plain integer",
  );
  const queueControl = buildModule(() => new Queue("dword", 4)).toJson();
  // 1984 is the python sibling's control length too (same fixture).
  assert.equal(queueControl.length, 1984);
  assert.equal(queueFailed, queueControl);

  const stackFailed = assertTypeErrorInBuilder(
    () => new Stack(4).push(1.5),
    "must be a plain integer",
  );
  const stackControl = buildModule(() => new Stack(4)).toJson();
  assert.equal(stackControl.length, 206);
  assert.equal(stackFailed, stackControl);
});

test("container element shape must match the element", () => {
  // HEAD raises a raw TypeError (a `Var` into a 2-field element) or silently
  // drops the extra fields (177174 / 90688 on both frontends).
  const Point3 = TriggerStruct("Point3", { x: dword(), y: dword(), z: dword() });
  const rows: Array<[string, () => unknown]> = [
    [
      "Queue(Point,3).push(Var(0))",
      () => {
        const v = Var(0);
        return new Queue(Point, 3).push(v);
      },
    ],
    [
      "Queue(Point,3).push(Point3)",
      () => new Queue(Point, 3).push(Point3({ x: 1, y: 2, z: 3 })),
    ],
    ['Queue("dword",4).push(Point)', () => new Queue("dword", 4).push(Point({ x: 1, y: 2 }))],
  ];
  for (const [, call] of rows) {
    assertTypeErrorInBuilder(call, "shape must match the element");
  }
});

test("lazy container element write inherits the guard", () => {
  // The guard sits below the lazy seam: a lazy handle resolves to an eager
  // binding, so the same `EUDSL-TYPE` comes out of the eager write path.
  const q = lazyQueue("dword", 3);
  assertTypeErrorInBuilder(
    () => q.push(1.5 as any),
    "container element must be a plain integer",
  );
});
