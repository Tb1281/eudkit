/**
 * Issue #76 (C4 follow-up): dynamic container Var writes go through `VarStore`.
 *
 * A `FixedArray` dynamic set and a `Queue`/`Deque` push of a `Var` used to
 * decompose the value into 32 bit-test triggers (38T/37T for `N = 8`; 552T/548T
 * for a 16-slot push). Both now emit one `emitVarStore` into the container's
 * pointer action `.value` field, so the source variable's `TriggerPage` firing
 * delivers the whole dword in one out-of-band transfer:
 *
 * - array dynamic set: `log2(N) + 5` triggers (8T/8T at `N = 8`, no flag gate);
 * - queue push: one pointer plus one store, 32T/29T at capacity 16.
 *
 * The marginal cost of one `VarStore` into a trigger field measures +3T (setup
 * trigger + out-of-band `TriggerPage` firing + restore), not the review's
 * projected 2T; nothing here claims 2T. Provenance: spike #74 on `b299f27`,
 * re-measured on this branch's base `73214aa`.
 *
 * The runtime proof is the repo's LIR payload simulator (`eudkit-compile sim`),
 * compared against the pre-change frontend on the same probe; an in-game run
 * remains open because this environment has no StarCraft install.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  Deque,
  FixedArray,
  Queue,
  Stack,
  TriggerStruct,
  Var,
  arrayByte,
  arrayDword,
  arrayWord,
  buildModule,
  byte,
  dword,
  when,
  type TriggerStructInstance,
  type VarImpl,
} from "../index.ts";

import { EUDKIT_COMPILE, runSim } from "./helpers.ts";

const TRIGGER_STRIDE_EPDS = 602;
const ACTION_DEST_OFFSET = 328 + 16;
const ACTION_VALUE_OFFSET = 328 + 20;
const SLOT_ZERO_VALUE = ACTION_VALUE_OFFSET;

interface LirReloc {
  field_offset: number;
  target_item: number;
  target_offset: number;
  mode: number;
}

// Typed LIR records (issue #101): one `CTrigCondition` / `CTrigAction`
// per cond/act, with the binary layout fields flattened into the JSON.
interface LirCond {
  locid: number;
  player: number;
  amount: number;
  unit: number;
  comparison: number;
  condtype: number;
  restype: number;
  flags: number;
  eudx: number;
  relocs: LirReloc[];
}

interface LirAct {
  mask: number;
  strid: number;
  wavid: number;
  time: number;
  dest: number;
  value: number;
  unit: number;
  acttype: number;
  modifier: number;
  flags: number;
  padding: number;
  eudx: number;
  relocs: LirReloc[];
}

interface LirTriggerItem {
  kind: string;
  conds: LirCond[];
  acts: LirAct[];
  size: number;
}

function runLower(hirJson: string): { module: { items: LirTriggerItem[] } } {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "eudkit-ts-c4-"));
  try {
    const hirPath = path.join(tmpDir, "prog.hir.json");
    fs.writeFileSync(hirPath, hirJson, "utf8");
    const out = execFileSync(EUDKIT_COMPILE, ["lower", hirPath], {
      encoding: "utf8",
    });
    return JSON.parse(out);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function triggerItems(lir: { module: { items: LirTriggerItem[] } }): LirTriggerItem[] {
  return lir.module.items.filter((item) => item.kind === "trigger");
}

function arraySetVar(length: number): () => void {
  return () => {
    const idx = Var(3);
    const val = Var(7);
    const arr = FixedArray(arrayDword, length);
    arr.set(idx, val);
  };
}

function arraySetConst(length: number): () => void {
  return () => {
    const idx = Var(3);
    const arr = FixedArray(arrayDword, length);
    arr.set(idx, 7);
  };
}

function queuePushVar(): void {
  const val = Var(7);
  new Queue("dword", 16).push(val);
}

function queuePushTwo(): void {
  const first = Var(7);
  const second = Var(9);
  const q = new Queue("dword", 16);
  q.push(first);
  q.push(second);
}

function queuePushConst(): void {
  new Queue("dword", 16).push(7);
}

function queuePushStruct(): void {
  const Point = TriggerStruct("Point", { x: dword(), y: dword() });
  new Queue(Point, 16).push(Point({ x: 7, y: 9 }));
}

function dequeAppendVar(): void {
  const val = Var(7);
  new Deque("dword", 4).append(val);
}

function stackPushVar(): void {
  const val = Var(7);
  new Stack(4).push(val);
}

interface CostCase {
  name: string;
  prog: () => void;
  triggers: number;
  triggerBytes: number;
  totalBytes: number;
  executed: number;
}

const COST_CASES: CostCase[] = [
  {
    name: "array dynamic set Var N=8",
    prog: arraySetVar(8),
    triggers: 8,
    triggerBytes: 19_264,
    totalBytes: 19_272,
    executed: 8,
  },
  {
    name: "array dynamic set Var N=16",
    prog: arraySetVar(16),
    triggers: 9,
    triggerBytes: 21_672,
    totalBytes: 21_680,
    executed: 9,
  },
  {
    name: "array dynamic set Var N=32",
    prog: arraySetVar(32),
    triggers: 10,
    triggerBytes: 24_080,
    totalBytes: 24_088,
    executed: 10,
  },
  {
    name: "array dynamic set const N=8",
    prog: arraySetConst(8),
    triggers: 6,
    triggerBytes: 14_448,
    totalBytes: 14_456,
    executed: 5,
  },
  {
    name: "queue push Var cap16",
    prog: queuePushVar,
    triggers: 32,
    triggerBytes: 77_056,
    totalBytes: 77_072,
    executed: 29,
  },
  {
    name: "queue push two Vars cap16",
    prog: queuePushTwo,
    triggers: 47,
    triggerBytes: 113_176,
    totalBytes: 113_192,
    executed: 42,
  },
  {
    name: "queue push const cap16",
    prog: queuePushConst,
    triggers: 40,
    triggerBytes: 96_320,
    totalBytes: 96_332,
    executed: 36,
  },
  {
    name: "queue push struct cap16",
    prog: queuePushStruct,
    triggers: 40,
    triggerBytes: 96_320,
    totalBytes: 96_336,
    executed: 38,
  },
  {
    name: "deque append Var cap4",
    prog: dequeAppendVar,
    triggers: 28,
    triggerBytes: 67_424,
    totalBytes: 67_440,
    executed: 26,
  },
  {
    name: "stack push Var cap4",
    prog: stackPushVar,
    triggers: 8,
    triggerBytes: 19_264,
    totalBytes: 19_272,
    executed: 8,
  },
];

for (const probe of COST_CASES) {
  test(`cost: ${probe.name}`, () => {
    const report = runSim(buildModule(probe.prog).toJson(), 1);
    assert.equal(report.static_cost.triggers, probe.triggers);
    assert.equal(report.static_cost.trigger_bytes, probe.triggerBytes);
    assert.equal(report.static_cost.total_bytes, probe.totalBytes);
    assert.equal(report.executed_cost.triggers, probe.executed);
  });
}

test("array dynamic set writes the zero, mid and last element", () => {
  for (const index of [0, 3, 7]) {
    const elems: VarImpl[] = [];
    const report = runSim(
      buildModule(() => {
        const idx = Var(index);
        const val = Var(7);
        const arr = FixedArray(arrayDword, 8);
        arr.set(idx, val);
        elems.push(...arr.vars);
      }).toJson(),
      1,
    );
    const values = elems.map((v) => report.var_values[v.id]);
    const expected = Array.from({ length: 8 }, (_, i) => (i === index ? 7 : 0));
    assert.deepEqual(values, expected, `index ${index}`);
  }
});

test("array dynamic set keeps the previous frontend's byte and word behaviour", () => {
  // The dynamic path stores and reads back the raw dword. The element mask
  // narrows only the static arr.set(i, const) path; the dynamic const write
  // already behaved this way, and the Var path now matches the pre-change
  // 32-bit-trigger frontend bit for bit.
  for (const [elem, source, stored] of [
    [arrayByte, 0x1ff, 0x1ff],
    [arrayWord, 0x1_2345, 0x1_2345],
  ] as const) {
    const elems: VarImpl[] = [];
    const out = { id: -1 };
    const report = runSim(
      buildModule(() => {
        const idx = Var(3);
        const val = Var(source);
        const arr = FixedArray(elem, 8);
        arr.set(idx, val);
        const sink = Var(0);
        sink.set(arr.get(3));
        elems.push(...arr.vars);
        out.id = sink.id;
      }).toJson(),
      1,
    );
    const values = elems.map((v) => report.var_values[v.id]);
    assert.deepEqual(
      values,
      Array.from({ length: 8 }, (_, i) => (i === 3 ? stored : 0)),
      String(elem),
    );
    assert.equal(report.var_values[out.id], stored);
  }
});

test("queue FIFO round trip lands on every tail slot", () => {
  const outs: VarImpl[] = [];
  const slotIds: number[] = [];
  let lengthId = -1;
  const report = runSim(
    buildModule(() => {
      const q = new Queue("dword", 16);
      const phase = Var(0);
      const values = Array.from({ length: 16 }, (_, i) => Var(100 + i));
      for (let i = 0; i < 16; i++) {
        const value = values[i];
        when([phase.eq(i)], () => q.push(value));
      }
      for (let i = 0; i < 16; i++) {
        const sink = Var(0);
        outs.push(sink);
        when([phase.eq(16 + i)], () => sink.set(q.popleft() as VarImpl));
      }
      phase.iadd(1);
      lengthId = q.length.id;
      slotIds.push(...q.slots.map((slot) => slot.id));
    }).toJson(),
    40,
  );
  assert.equal(report.var_values[lengthId], 0);
  assert.deepEqual(
    outs.map((v) => report.var_values[v.id]),
    Array.from({ length: 16 }, (_, i) => 100 + i),
  );
  assert.deepEqual(
    slotIds,
    Array.from({ length: 16 }, (_, i) => slotIds[0] + i),
    "ring slots non-contiguous",
  );
});

test("two pushes and a wrap keep FIFO order", () => {
  const outs: VarImpl[] = [];
  const ids = { head: -1, tail: -1, length: -1 };
  const report = runSim(
    buildModule(() => {
      const q = new Queue("dword", 4);
      const phase = Var(0);
      const values = [11, 22, 33, 44, 55].map((v) => Var(v));
      for (let i = 0; i < 4; i++) {
        const value = values[i];
        when([phase.eq(i)], () => q.push(value));
      }
      for (let i = 0; i < 2; i++) {
        const sink = Var(0);
        outs.push(sink);
        when([phase.eq(4 + i)], () => sink.set(q.popleft() as VarImpl));
      }
      when([phase.eq(6)], () => q.push(values[4]));
      for (let i = 0; i < 3; i++) {
        const sink = Var(0);
        outs.push(sink);
        when([phase.eq(7 + i)], () => sink.set(q.popleft() as VarImpl));
      }
      phase.iadd(1);
      ids.head = q.head.id;
      ids.tail = q.tail.id;
      ids.length = q.length.id;
    }).toJson(),
    12,
  );
  assert.deepEqual(
    outs.map((v) => report.var_values[v.id]),
    [11, 22, 33, 44, 55],
  );
  assert.equal(report.var_values[ids.length], 0);
  // Two pushes after two pops wrapped the ring: tail went 3 -> 0 -> 1.
  assert.equal(report.var_values[ids.head], 1);
  assert.equal(report.var_values[ids.tail], 1);
});

test("struct element writes every field", () => {
  // Regression for a store offset taken from the *slot's* field k: for k > 0
  // that lands on the pointer's `flag.act_set(0)` action and every field but
  // the first is silently dropped.
  const out = { x: -1, y: -1, z: -1, length: -1 };
  const report = runSim(
    buildModule(() => {
      const Point = TriggerStruct("Point", { x: dword(), y: dword(), z: byte() });
      const q = new Queue(Point, 4);
      const phase = Var(0);
      const ox = Var(0);
      const oy = Var(0);
      const oz = Var(0);
      when([phase.eq(0)], () => q.push(Point({ x: 7, y: 9, z: 0x1ff })));
      when([phase.eq(1)], () => {
        const got = q.popleft() as TriggerStructInstance<"x" | "y" | "z">;
        ox.set(got.x);
        oy.set(got.y);
        oz.set(got.z);
      });
      phase.iadd(1);
      Object.assign(out, { x: ox.id, y: oy.id, z: oz.id, length: q.length.id });
    }).toJson(),
    3,
  );
  const values = report.var_values;
  assert.equal(values[out.x], 7);
  assert.equal(values[out.y], 9);
  assert.equal(values[out.z], 0xff, "byte field mask not applied");
  assert.equal(values[out.length], 0);
});

test("same-page var written in the same loop survives the store", () => {
  // The store fires the source page out of band; other slots self-write.
  for (const order of ["transfer_first", "store_first"] as const) {
    const ids = { a: -1, b: -1 };
    const elems: VarImpl[] = [];
    const report = runSim(
      buildModule(() => {
        const a = Var(11);
        const b = Var(22);
        const idx = Var(3);
        const arr = FixedArray(arrayDword, 8);
        const phase = Var(0);
        when([phase.eq(0)], () => {
          if (order === "transfer_first") {
            a.set(b);
            arr.set(idx, a);
          } else {
            arr.set(idx, a);
            a.set(b);
          }
        });
        phase.iadd(1);
        ids.a = a.id;
        ids.b = b.id;
        elems.push(...arr.vars);
      }).toJson(),
      2,
    );
    const values = report.var_values;
    assert.equal(values[ids.a], 22);
    assert.equal(values[ids.b], 22);
    // The store reads the source at its program position, exactly like the
    // baseline bit-test frontend: when the array statement comes first it
    // captures the pre-transfer value (11).
    const expected = order === "transfer_first" ? 22 : 11;
    assert.equal(values[elems[3].id], expected, order);
    assert.deepEqual(
      elems.filter((_, i) => i !== 3).map((v) => values[v.id]),
      Array(7).fill(0),
    );
  }
});

test("deque append and appendleft of vars round trip", () => {
  // Both ring views take the same pointer path (write index == value).
  const outs: VarImpl[] = [];
  const ids = { length: -1, left: [] as number[], right: [] as number[] };
  const report = runSim(
    buildModule(() => {
      const q = new Deque("dword", 4);
      const phase = Var(0);
      const first = Var(7);
      const second = Var(9);
      const sink = () => {
        const s = Var(0);
        outs.push(s);
        return s;
      };
      const [o0, o1, o2] = [sink(), sink(), sink()];
      when([phase.eq(0)], () => q.append(first));
      when([phase.eq(1)], () => q.appendleft(second));
      when([phase.eq(2)], () => o0.set(q.popleft() as VarImpl));
      when([phase.eq(3)], () => o1.set(q.pop() as VarImpl));
      when([phase.eq(4)], () => q.append(first));
      when([phase.eq(5)], () => o2.set(q.pop() as VarImpl));
      phase.iadd(1);
      ids.length = q.length.id;
      ids.left = q.leftSlots.map((slot) => slot.id);
      ids.right = q.rightSlots.map((slot) => slot.id);
    }).toJson(),
    8,
  );
  assert.deepEqual(
    outs.map((v) => report.var_values[v.id]),
    [9, 7, 7],
  );
  assert.equal(report.var_values[ids.length], 0);
  for (const key of ["left", "right"] as const) {
    const slotIds = ids[key];
    assert.deepEqual(
      slotIds,
      Array.from({ length: 4 }, (_, i) => slotIds[0] + i),
      key,
    );
  }
});

function queueLir(capacity = 16, fields = 1): LirTriggerItem[] {
  return triggerItems(
    runLower(
      buildModule(() => {
        if (fields === 1) {
          new Queue("dword", capacity).push(Var(7));
          return;
        }
        const Point = TriggerStruct("Point", { x: dword(), y: dword() });
        new Queue(Point, capacity).push(Point({ x: 7, y: 9 }));
      }).toJson(),
    ),
  );
}

test("ring slots are the leading contiguous trigger block", () => {
  const items = queueLir();
  for (let i = 0; i < 16; i++) {
    const item = items[i];
    assert.equal(item.conds.length, 1, `item ${i} is not a ring slot`);
    // The slot read gate is `head == i` (Deaths, Exactly).
    assert.equal(item.conds[0].amount, i);
    assert.equal(item.conds[0].comparison, 10);
    assert.equal(item.size, TRIGGER_STRIDE_EPDS * 4);
  }
});

test("reset and store address slot zero and the pointer value field", () => {
  const items = queueLir(16, 2);
  const resets: { pointer: number; offset: number }[] = [];
  for (const item of items) {
    for (const act of item.acts) {
      const dest = act.relocs.filter((r) => r.field_offset === 16);
      const value = act.relocs.filter((r) => r.field_offset === 20);
      if (
        dest.length === 1 &&
        dest[0].target_offset === ACTION_DEST_OFFSET &&
        value.length === 1 &&
        value[0].target_item === 0
      ) {
        resets.push({ pointer: dest[0].target_item, offset: value[0].target_offset });
      }
    }
  }
  assert.equal(resets.length, 2, "one reset per element field");
  const pointerItems = new Set(resets.map((r) => r.pointer));
  assert.equal(pointerItems.size, 2);
  // Field k's reset reads slot 0's action k value field (0x15c + 32k).
  assert.deepEqual(
    resets.map((r) => r.offset).sort((a, b) => a - b),
    [SLOT_ZERO_VALUE, SLOT_ZERO_VALUE + 32],
  );

  for (const pointer of [...pointerItems].sort((a, b) => a - b)) {
    const setups = items.filter((item) =>
      item.acts.some((act) =>
        act.relocs.some(
          (r) =>
            r.field_offset === 20 &&
            r.target_item === pointer &&
            r.target_offset === ACTION_VALUE_OFFSET,
        ),
      ),
    );
    assert.equal(setups.length, 1, "one VarStore setup per pointer");
    // Every reloc into the pointer itself must address its action 0 `.dest`
    // (address walk) or `.value` (store target); the flag lives in action 1,
    // whose value field is 0x17c.
    const pointerRelocs = items.flatMap((item) =>
      item.acts.flatMap((act) =>
        act.relocs.filter((r) => r.target_item === pointer),
      ),
    );
    assert.ok(pointerRelocs.length > 0);
    assert.deepEqual(
      [...new Set(pointerRelocs.map((r) => r.target_offset))].sort(
        (a, b) => a - b,
      ),
      [ACTION_DEST_OFFSET, ACTION_VALUE_OFFSET],
    );
  }
});

test("addr weights are trigger stride shifts", () => {
  const items = queueLir();
  const weights = new Set<number>();
  for (const item of items) {
    for (const act of item.acts) {
      if (act.modifier === 8 && [602, 1204, 2408, 4816].includes(act.value)) {
        weights.add(act.value);
      }
    }
  }
  assert.deepEqual(
    [...weights].sort((a, b) => a - b),
    [0, 1, 2, 3].map((b) => TRIGGER_STRIDE_EPDS << b),
  );
});
