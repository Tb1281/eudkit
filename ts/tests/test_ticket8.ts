import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  Deque,
  Queue,
  Stack,
  TriggerStruct,
  UnitGroup,
  Var,
  buildModule,
  dword,
  when,
  type TriggerStructInstance,
  type VarImpl,
} from "../index.ts";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..");

interface HirStmtNode {
  kind: string;
  conds?: { relocs?: { target_var?: number }[] }[];
  acts?: { relocs?: { target_var?: number; target_trigger?: number }[] }[];
  body?: HirStmtNode[];
}

function parseModule(hirJson: string): { module: { stmts: HirStmtNode[] } } {
  return JSON.parse(hirJson) as { module: { stmts: HirStmtNode[] } };
}

function whenBody(stmts: HirStmtNode[], index: number): HirStmtNode[] {
  const whenStmts = stmts.filter((s) => s.kind === "when");
  const body = whenStmts[index]?.body;
  assert.ok(body, `expected when statement #${index}`);
  return body;
}

function bodyActionCount(body: HirStmtNode[]): number {
  return body.reduce(
    (n, s) => n + (s.kind === "trigger" ? (s.acts?.length ?? 0) : s.kind === "act" ? 1 : 0),
    0,
  );
}

function bodyTriggerCount(body: HirStmtNode[]): number {
  return body.filter((s) => s.kind === "trigger").length;
}

function bodyKindCount(body: HirStmtNode[], kind: string): number {
  return body.filter((s) => s.kind === kind).length;
}

test("Queue is a FIFO ring that round-trips scalar elements across loops", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const q = new Queue("dword", 4);
    const phase = Var(0);
    const out1 = Var(0);
    const out2 = Var(0);
    when([phase.eq(0)], () => {
      q.append(11);
      q.append(22);
    });
    when([phase.eq(1)], () => out1.set(q.popleft() as VarImpl));
    when([phase.eq(2)], () => out2.set(q.popleft() as VarImpl));
    phase.iadd(1);
    ids.length = q.length.id;
    ids.head = q.head.id;
    ids.tail = q.tail.id;
    ids.out1 = out1.id;
    ids.out2 = out2.id;
  });

  const report = runSim(mod.toJson(), 5);
  assert.equal(report.var_values[ids.out1], 11);
  assert.equal(report.var_values[ids.out2], 22);
  assert.equal(report.var_values[ids.length], 0);
  assert.equal(report.var_values[ids.head], 2);
  assert.equal(report.var_values[ids.tail], 2);
  assert.equal(report.static_cost.triggers >= 2, true);
});

test("Deque supports appendleft, popleft, and pop in mixed order", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const q = new Deque("dword", 4);
    const phase = Var(0);
    const out1 = Var(0);
    const out2 = Var(0);
    const out3 = Var(0);
    when([phase.eq(0)], () => {
      q.append(1);
      q.append(2);
      q.appendleft(0);
    });
    when([phase.eq(1)], () => out1.set(q.popleft() as VarImpl));
    when([phase.eq(2)], () => out2.set(q.pop() as VarImpl));
    when([phase.eq(3)], () => out3.set(q.popleft() as VarImpl));
    phase.iadd(1);
    ids.length = q.length.id;
    ids.out1 = out1.id;
    ids.out2 = out2.id;
    ids.out3 = out3.id;
  });

  const report = runSim(mod.toJson(), 6);
  assert.equal(report.var_values[ids.out1], 0);
  assert.equal(report.var_values[ids.out2], 2);
  assert.equal(report.var_values[ids.out3], 1);
  assert.equal(report.var_values[ids.length], 0);
});

test("Stack is LIFO over page-backed FixedArray storage", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const s = new Stack(4);
    const phase = Var(0);
    const out1 = Var(0);
    const out2 = Var(0);
    const out3 = Var(0);
    when([phase.eq(0)], () => {
      s.push(11);
      s.push(22);
      s.push(33);
    });
    when([phase.eq(1)], () => out1.set(s.pop()));
    when([phase.eq(2)], () => out2.set(s.pop()));
    when([phase.eq(3)], () => out3.set(s.pop()));
    phase.iadd(1);
    ids.pos = s.pos.id;
    ids.out1 = out1.id;
    ids.out2 = out2.id;
    ids.out3 = out3.id;
  });

  const report = runSim(mod.toJson(), 5);
  assert.equal(report.var_values[ids.out1], 33);
  assert.equal(report.var_values[ids.out2], 22);
  assert.equal(report.var_values[ids.out3], 11);
  assert.equal(report.var_values[ids.pos], 0);
});

test("Queue round-trips a complete TriggerStruct element and binds all field slots statically", () => {
  const Point = TriggerStruct("Point", { x: dword(), y: dword() });
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const q = new Queue(Point, 2);
    const phase = Var(0);
    const ox1 = Var(0);
    const oy1 = Var(0);
    const ox2 = Var(0);
    const oy2 = Var(0);
    when([phase.eq(0)], () => {
      q.append(Point({ x: 7, y: 9 }));
      q.append(Point({ x: 1, y: 2 }));
    });
    when([phase.eq(1)], () => {
      const got = q.popleft() as ReturnType<typeof Point.instantiate>;
      ox1.set(got.x as VarImpl);
      oy1.set(got.y as VarImpl);
    });
    when([phase.eq(2)], () => {
      const got = q.popleft() as ReturnType<typeof Point.instantiate>;
      ox2.set(got.x as VarImpl);
      oy2.set(got.y as VarImpl);
    });
    phase.iadd(1);
    ids.length = q.length.id;
    ids.ox1 = ox1.id;
    ids.oy1 = oy1.id;
    ids.ox2 = ox2.id;
    ids.oy2 = oy2.id;
    ids.outX = (q.outBox as TriggerStructInstance<"x" | "y">).fields[0].id;
    ids.outY = (q.outBox as TriggerStructInstance<"x" | "y">).fields[1].id;
  });

  const parsed = parseModule(mod.toJson());
  const slotTriggers = parsed.module.stmts.filter(
    (s) =>
      s.kind === "trigger" &&
      s.acts?.length === 2 &&
      s.acts.every((a) => a.relocs?.[0]?.target_var === ids.outX || a.relocs?.[0]?.target_var === ids.outY),
  );
  assert.equal(slotTriggers.length, 2);
  for (const slot of slotTriggers) {
    assert.equal(slot.acts?.[0]?.relocs?.[0]?.target_var, ids.outX);
    assert.equal(slot.acts?.[1]?.relocs?.[0]?.target_var, ids.outY);
  }

  const report = runSim(mod.toJson(), 5);
  assert.equal(report.var_values[ids.ox1], 7);
  assert.equal(report.var_values[ids.oy1], 9);
  assert.equal(report.var_values[ids.ox2], 1);
  assert.equal(report.var_values[ids.oy2], 2);
  assert.equal(report.var_values[ids.length], 0);
});

function popSiteCost(
  kind: "queue" | "deque",
  capacity: number,
): { stmts: number; triggers: number; acts: number; transfers: number; actions: number } {
  const mod = buildModule(() => {
    const phase = Var(0);
    const out = Var(0);
    if (kind === "queue") {
      const q = new Queue("dword", capacity);
      when([phase.eq(1)], () => out.set(q.popleft() as VarImpl));
    } else {
      const q = new Deque("dword", capacity);
      when([phase.eq(1)], () => out.set(q.popleft() as VarImpl));
    }
  });
  const body = whenBody(parseModule(mod.toJson()).module.stmts, 0);
  return {
    stmts: body.length,
    triggers: bodyTriggerCount(body),
    acts: bodyKindCount(body, "act"),
    transfers: bodyKindCount(body, "vartransfer"),
    actions: bodyActionCount(body),
  };
}

test("Queue and Deque pop sites are O(1) and identical at capacity 16 and 128", () => {
  for (const kind of ["queue", "deque"] as const) {
    const small = popSiteCost(kind, 16);
    const large = popSiteCost(kind, 128);
    assert.deepEqual(large, small, `${kind} pop site grew with capacity`);
    // Two ring-index triggers, one length action, one out-box transfer. None
    // of these patch a per-slot action destination.
    assert.deepEqual(small, {
      stmts: 4,
      triggers: 2,
      acts: 1,
      transfers: 1,
      actions: 3,
    });
  }
});

test("Queue ring slot destinations are statically bound to the out box", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const q = new Queue("dword", 16);
    ids.head = q.head.id;
    ids.out = (q.outBox as VarImpl).id;
  });

  const slotTriggers = parseModule(mod.toJson()).module.stmts.filter(
    (s) =>
      s.kind === "trigger" &&
      s.conds?.[0]?.relocs?.[0]?.target_var === ids.head,
  );
  assert.equal(slotTriggers.length, 16);
  for (const slot of slotTriggers) {
    assert.equal(slot.acts?.[0]?.relocs?.[0]?.target_var, ids.out);
    assert.equal(slot.acts?.[0]?.relocs?.[0]?.target_trigger, undefined);
  }
});

test("UnitGroup autoPrune sweeps dead members through VarStore indirection", () => {
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const g = new UnitGroup(2);
    const phase = Var(0);
    when([phase.eq(0)], () => {
      g.add(1000);
      g.add(2000);
    });
    phase.iadd(1);
    ids.length = g.length.id;
    ids.slot0 = g.slots[0].id;
    ids.slot1 = g.slots[1].id;
  });

  const hirJson = mod.toJson();
  assert.ok(hirJson.includes('"kind":"varstore"'), "autoPrune must use VarStore indirection");

  // The CLI simulator has no external memory, so both unit EPD fields read 0.
  const report = runSim(hirJson, 2);
  assert.equal(report.var_values[ids.length], 0);
  assert.equal(report.var_values[ids.slot0], 0);
  assert.equal(report.var_values[ids.slot1], 0);
});

test("Byte-identical HIR parity between TypeScript and Python containers", () => {
  const tsMod = buildModule(() => {
    const P = TriggerStruct("P", { x: dword(), y: dword() });
    const q = new Queue("dword", 4);
    q.append(1);
    q.append(2);
    const out = Var(0);
    out.set(q.popleft() as VarImpl);

    const qs = new Queue(P, 2);
    qs.append(P({ x: 7, y: 9 }));
    const got = qs.popleft() as ReturnType<typeof P.instantiate>;
    out.set(got.x as VarImpl);
    out.set(got.y as VarImpl);

    const dq = new Deque("word", 4);
    dq.append(1);
    dq.appendleft(2);
    out.set(dq.pop() as VarImpl);
    out.set(dq.popleft() as VarImpl);

    const st = new Stack(4);
    st.push(1);
    st.push(2);
    out.set(st.pop());

    const g = new UnitGroup(2);
    g.add(1000);
    g.add(2000);
    g.remove(1000);
  });

  const pyScript = `
import sys
from py import (
    Deque, Queue, Stack, TriggerStruct, UnitGroup, Var,
    build_module, dword,
)

def prog():
    P = TriggerStruct("P", x=dword(), y=dword())
    q = Queue("dword", 4)
    q.append(1)
    q.append(2)
    out = Var(0)
    out.set(q.popleft())

    qs = Queue(P, 2)
    qs.append(P(x=7, y=9))
    got = qs.popleft()
    out.set(got.x)
    out.set(got.y)

    dq = Deque("word", 4)
    dq.append(1)
    dq.appendleft(2)
    out.set(dq.pop())
    out.set(dq.popleft())

    st = Stack(4)
    st.push(1)
    st.push(2)
    out.set(st.pop())

    g = UnitGroup(2)
    g.add(1000)
    g.add(2000)
    g.remove(1000)

sys.stdout.write(build_module(prog).to_json())
`;

  const pyHirJson = execFileSync("python3", ["-c", pyScript], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
  });
  assert.equal(tsMod.toJson(), pyHirJson);
});
