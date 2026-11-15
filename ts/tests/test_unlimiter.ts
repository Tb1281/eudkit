/**
 * The unlimiter core helper (issue #94): a port of euddraft's
 * `plugins/unlimiter.py`. Mirror of `py/tests/test_unlimiter.py`.
 *
 * The source connects four grow-only game lists to module data blobs, then
 * rewrites one dword every trigger loop:
 *
 * | list    | head     | tail     | stride | elements |
 * | ------- | -------- | -------- | ------ | -------- |
 * | bullets | 0x64EED8 | 0x64EEDC | 112    | 8192     |
 * | sprites | 0x63FE30 | 0x63FE34 | 36     | 65536    |
 * | images  | 0x57EB68 | 0x57EB70 | 64     | 65536    |
 * | orders  | 0x64B2E0 | 0x64B2E4 | 20     | 65536    |
 *
 * What is pinned here: the list constants, the emitted HIR shape per list (one
 * `while` loop, one write trigger, the shared patch block, the optional sprite
 * trigger, the four tail acts), the linked-list layout replayed from the
 * relocations for every element, and the per-tick `0x64DEBC = 40` write last.
 *
 * The runtime proof that the patched loop fills the blob lives in
 * `crates/eudkit-lower/tests/unlimiter_parity.rs`; the Python simulator reports
 * only out-of-payload EPDs and the list blob is inside the payload.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Add,
  BULLET_ITEM_COUNT,
  BULLET_ITEM_SIZE,
  BULLET_LIST_ADDR,
  BULLET_LIST_END_ADDR,
  EudslError,
  IMAGE_ITEM_COUNT,
  IMAGE_ITEM_SIZE,
  IMAGE_LIST_ADDR,
  IMAGE_LIST_END_ADDR,
  MIN_ITEM_SIZE,
  NEXT_FIELD_OFFSET,
  ORDER_ITEM_COUNT,
  ORDER_ITEM_SIZE,
  ORDER_LIST_ADDR,
  ORDER_LIST_END_ADDR,
  PER_TICK_ADDR,
  PER_TICK_VALUE,
  PREV_FIELD_OFFSET,
  SPRITE_FIELD_OFFSET,
  SPRITE_FIELD_SEED,
  SPRITE_ITEM_COUNT,
  SPRITE_ITEM_SIZE,
  SPRITE_LIST_ADDR,
  SPRITE_LIST_END_ADDR,
  SetTo,
  buildModule,
  connectDList,
  unlimiter,
} from "../index.ts";

const EPD_BASE = 0x58a364;

/** The four `connectDList` calls of the source's `onPluginStart`, in order. */
const LISTS: readonly (readonly [number, number, number, number, number])[] = [
  [BULLET_LIST_ADDR, BULLET_LIST_END_ADDR, BULLET_ITEM_SIZE, BULLET_ITEM_COUNT, 0],
  [SPRITE_LIST_ADDR, SPRITE_LIST_END_ADDR, SPRITE_ITEM_SIZE, SPRITE_ITEM_COUNT, 1],
  [IMAGE_LIST_ADDR, IMAGE_LIST_END_ADDR, IMAGE_ITEM_SIZE, IMAGE_ITEM_COUNT, 0],
  [ORDER_LIST_ADDR, ORDER_LIST_END_ADDR, ORDER_ITEM_SIZE, ORDER_ITEM_COUNT, 0],
];

const ACT_DEST_OFFSET = 16;
const ACT_VALUE_OFFSET = 20;
const ACT_STRIDE = 32;
const ACT_BASE = 328;

function epd(addr: number): number {
  return Math.floor((addr - EPD_BASE) / 4) >>> 0;
}

function moduleJson(body: () => void): any {
  return JSON.parse(buildModule(body).toJson()).module;
}

function blobReloc(offset: number, mode: number, fieldOffset = ACT_DEST_OFFSET): any {
  return { field_offset: fieldOffset, target_blob: 0, target_offset: offset, mode };
}

function triggerReloc(triggerId: number, offset: number): any {
  return { field_offset: ACT_DEST_OFFSET, target_trigger: triggerId, target_offset: offset, mode: 1 };
}

function flattenActs(stmts: any[]): any[] {
  const out: any[] = [];
  for (const stmt of stmts) {
    if (stmt.kind === "act") {
      out.push(stmt);
    } else if (stmt.kind === "trigger") {
      out.push(...stmt.acts);
    } else {
      out.push(...flattenActs(stmt.body ?? []));
    }
  }
  return out;
}

test("unlimiter list constants match the source", () => {
  assert.deepEqual(
    LISTS.map((list) => [...list]),
    [
      [0x64eed8, 0x64eedc, 112, 8192, 0],
      [0x63fe30, 0x63fe34, 36, 65536, 1],
      [0x57eb68, 0x57eb70, 64, 65536, 0],
      [0x64b2e0, 0x64b2e4, 20, 65536, 0],
    ],
  );
  assert.equal(PER_TICK_ADDR, 0x64debc);
  assert.equal(PER_TICK_VALUE, 40);
  assert.equal(PREV_FIELD_OFFSET, 0);
  assert.equal(NEXT_FIELD_OFFSET, 4);
  assert.equal(MIN_ITEM_SIZE, 8);
  assert.equal(SPRITE_FIELD_OFFSET, 0x10);
  assert.equal(SPRITE_FIELD_SEED, 10000);
});

test("one list arms a padded blob and a runtime loop", () => {
  const itemSize = 112;
  const itemN = 4;
  const mod = moduleJson(() => connectDList(0x64eed8, 0x64eedc, itemSize, itemN));

  assert.deepEqual(mod.blobs, [{ size: itemSize * (itemN + 2) }]);
  assert.deepEqual(mod.vars, [{ id: 0, init: itemN }]);

  const once = mod.stmts[0];
  assert.equal(once.kind, "once");
  const whileStmt = once.body[0];
  assert.equal(whileStmt.kind, "while");
  assert.deepEqual(whileStmt.conds, [
    {
      locid: 0,
      player: 0,
      amount: 1,
      unit: 0,
      comparison: 0, // AtLeast
      condtype: 15, // Deaths / Memory wire form
      restype: 0,
      flags: 0,
      eudx: 0,
      relocs: [{ field_offset: 4, target_var: 0, target_offset: 20, mode: 1 }],
    },
  ]);

  const body = whileStmt.body as any[];
  assert.deepEqual(body.map((stmt: any) => stmt.kind), ["trigger", "act", "act", "act", "act", "act"]);

  const trigger = body[0];
  assert.equal(trigger.acts.length, 2);
  assert.equal(trigger.acts[0].acttype, 45); // SetMemory / SetDeaths
  assert.equal(trigger.acts[0].modifier, SetTo);
  assert.deepEqual(trigger.acts[0].relocs, [
    blobReloc(itemSize + PREV_FIELD_OFFSET, 1),
    blobReloc(0, 4, ACT_VALUE_OFFSET),
  ]);
  assert.deepEqual(trigger.acts[1].relocs, [
    blobReloc(itemSize + NEXT_FIELD_OFFSET, 1),
    blobReloc(2 * itemSize, 4, ACT_VALUE_OFFSET),
  ]);

  // Four patches advance the write trigger's two actions, then the counter
  // decrement (`isub`) closes the body.
  const stride = itemSize / 4;
  const expected = [
    [trigger.id, ACT_BASE + ACT_DEST_OFFSET, stride],
    [trigger.id, ACT_BASE + ACT_VALUE_OFFSET, itemSize],
    [trigger.id, ACT_BASE + ACT_STRIDE + ACT_DEST_OFFSET, stride],
    [trigger.id, ACT_BASE + ACT_STRIDE + ACT_VALUE_OFFSET, itemSize],
  ];
  const patches = body.slice(1) as any[];
  assert.equal(patches.length, 5);
  for (let i = 0; i < expected.length; i += 1) {
    const [target, fieldOffset, delta] = expected[i];
    assert.equal(patches[i].kind, "act");
    assert.equal(patches[i].modifier, Add);
    assert.equal(patches[i].value, delta);
    assert.deepEqual(patches[i].relocs, [triggerReloc(target, fieldOffset)]);
  }
  const decrement = body[body.length - 1];
  assert.equal(decrement.kind, "act");
  assert.equal(decrement.modifier, Add);
  assert.equal(decrement.value, 0xffffffff); // -1
  assert.equal(decrement.eudx, 0x4353); // EUDX_MAGIC: masked add
  assert.deepEqual(decrement.relocs, [
    { field_offset: 16, target_var: 0, target_offset: 20, mode: 1 },
  ]);

  // Tail: head <- element 0, element 0 prev <- 0, tail <- last, last next <- 0.
  const tail = once.body.slice(1) as any[];
  assert.equal(tail.length, 4);
  assert.equal(tail[0].modifier, SetTo);
  assert.deepEqual(tail[0].relocs, [blobReloc(itemSize, 4, ACT_VALUE_OFFSET)]);
  assert.deepEqual(tail[1].relocs, [blobReloc(itemSize + PREV_FIELD_OFFSET, 1)]);
  assert.equal(tail[1].value, 0);
  assert.deepEqual(tail[2].relocs, [blobReloc(itemSize * itemN, 4, ACT_VALUE_OFFSET)]);
  assert.deepEqual(tail[3].relocs, [blobReloc(itemSize * itemN + NEXT_FIELD_OFFSET, 1)]);
  assert.equal(tail[3].value, 0);
});

test("sprite list adds the seed trigger and its patches", () => {
  const itemSize = 112;
  const itemN = 4;
  const mod = moduleJson(() => connectDList(0x64eed8, 0x64eedc, itemSize, itemN, 1));
  const body = mod.stmts[0].body[0].body as any[];
  assert.deepEqual(body.map((stmt: any) => stmt.kind), ["trigger", "trigger", ...Array(7).fill("act")]);

  const sprite = body[1];
  assert.equal(sprite.acts.length, 1);
  const seed = sprite.acts[0];
  assert.equal(seed.modifier, SetTo);
  assert.equal(seed.value, SPRITE_FIELD_SEED);
  assert.deepEqual(seed.relocs, [blobReloc(itemSize + SPRITE_FIELD_OFFSET, 1)]);

  // The sprite patches share the write trigger's patch block.
  assert.equal(body[6].value, itemSize / 4);
  assert.equal(body[6].modifier, Add);
  assert.deepEqual(body[6].relocs, [triggerReloc(sprite.id, ACT_BASE + ACT_DEST_OFFSET)]);
  assert.equal(body[7].value, 1);
  assert.deepEqual(body[7].relocs, [triggerReloc(sprite.id, ACT_BASE + ACT_VALUE_OFFSET)]);
});

test("linked-list layout matches the source geometry", () => {
  const itemSize = 112;
  const itemN = 5;
  const mod = moduleJson(() => connectDList(0x64eed8, 0x64eedc, itemSize, itemN));
  const body = mod.stmts[0].body[0].body as any[];
  const writes = body[0];

  const starts: Record<string, number> = {};
  const names = ["prev_dest", "prev_value", "next_dest", "next_value"];
  const relocs = [...writes.acts[0].relocs, ...writes.acts[1].relocs];
  relocs.forEach((reloc: any, index: number) => {
    starts[names[index]] = reloc.target_offset;
  });
  assert.deepEqual(starts, {
    prev_dest: itemSize + PREV_FIELD_OFFSET,
    prev_value: 0,
    next_dest: itemSize + NEXT_FIELD_OFFSET,
    next_value: 2 * itemSize,
  });

  // The tail zeroes element 0's prev and the last element's next, and names
  // element n-1 as the list end.
  const tail = mod.stmts[0].body.slice(1) as any[];
  assert.equal(tail[1].relocs[0].target_offset, itemSize);
  const last = tail[2].relocs[0].target_offset;
  assert.equal(last, itemSize * itemN);
  assert.equal(tail[3].relocs[0].target_offset, last + NEXT_FIELD_OFFSET);

  // Walk the write actions forward with the loop's own patches and pin the
  // last iteration against the tail: element n-1's `prev` must be element
  // n-2, its `next` must be the trailing padding (advanced to n * itemSize,
  // then zeroed by the tail), and the two destinations must be that element's
  // link fields. This ties the patch deltas to the tail offsets instead of
  // restating the arithmetic.
  const advance = itemSize * (itemN - 1);
  assert.equal(starts.prev_dest + advance, last);
  assert.equal(starts.prev_value + advance, last - itemSize);
  assert.equal(starts.next_dest + advance, last + NEXT_FIELD_OFFSET);
  // Element n-1's `next` seeds the (unwritten) trailing padding at
  // itemSize * (n + 1); the tail then zeroes the link.
  assert.equal(starts.next_value + advance, itemSize * (itemN + 1));
});

test("sprite field runs from 10000 in list order", () => {
  const itemSize = 36;
  const itemN = 6;
  const mod = moduleJson(() => connectDList(0x63fe30, 0x63fe34, itemSize, itemN, 1));
  const body = mod.stmts[0].body[0].body as any[];
  const spriteTrigger = body[1];
  const seed = spriteTrigger.acts[0];
  const seedOffset = seed.relocs[0].target_offset;
  assert.equal(seedOffset, itemSize + SPRITE_FIELD_OFFSET);
  assert.equal(seed.value, SPRITE_FIELD_SEED);

  // The sprite patches advance in lockstep with the write trigger (one stride
  // per element, value +1), so the last seed lands on the final element's
  // +0x10 field and the last value is 10000 + n - 1.
  const destPatch = body[6];
  const valuePatch = body[7];
  assert.equal(destPatch.value, itemSize / 4);
  assert.deepEqual(destPatch.relocs, [
    triggerReloc(spriteTrigger.id, ACT_BASE + ACT_DEST_OFFSET),
  ]);
  assert.equal(valuePatch.value, 1);
  assert.deepEqual(valuePatch.relocs, [
    triggerReloc(spriteTrigger.id, ACT_BASE + ACT_VALUE_OFFSET),
  ]);
  const last = itemSize * itemN;
  assert.equal(seedOffset + itemSize * (itemN - 1), last + SPRITE_FIELD_OFFSET);
  assert.equal(seed.value + (itemN - 1), SPRITE_FIELD_SEED + itemN - 1);
});

test("unlimiter arms all four lists in order", () => {
  const mod = moduleJson(() => unlimiter());
  assert.deepEqual(
    (mod.blobs as any[]).map((blob) => blob.size),
    LISTS.map(([, , stride, count]) => stride * (count + 2)),
  );
  const onceStmts = (mod.stmts as any[]).filter((stmt) => stmt.kind === "once");
  assert.equal(onceStmts.length, 4);
  assert.deepEqual(
    onceStmts.map((once) => once.body[once.body.length - 4].dest),
    LISTS.map(([head]) => epd(head)),
  );
});

test("only the sprite list carries the 10000 seed", () => {
  const mod = moduleJson(() => unlimiter());
  const onceStmts = (mod.stmts as any[]).filter((stmt) => stmt.kind === "once");
  const seeded = onceStmts.map((once) =>
    (once.body[0].body as any[])
      .slice(1)
      .filter((stmt) => stmt.kind === "trigger")
      .some((stmt) => stmt.acts.some((act: any) => act.value === SPRITE_FIELD_SEED)),
  );
  assert.deepEqual(seeded, [false, true, false, false]);
});

test("per-tick write is the last action and appears once", () => {
  const mod = moduleJson(() => unlimiter());
  const acts = flattenActs(mod.stmts as any[]);
  const write = acts[acts.length - 1];
  assert.equal(write.acttype, 45);
  assert.equal(write.modifier, SetTo);
  assert.equal(write.value, PER_TICK_VALUE);
  assert.equal(write.dest, epd(PER_TICK_ADDR));
  assert.deepEqual(write.relocs ?? [], []);

  const writes = acts.filter((act) => act.dest === epd(PER_TICK_ADDR));
  assert.equal(writes.length, 1);
});

test("item size defaults to the smallest node", () => {
  assert.throws(() => buildModule(() => connectDList(0x64eed8, 0x64eedc, 4, 1)), EudslError);
  const mod = moduleJson(() => connectDList(0x64eed8, 0x64eedc, MIN_ITEM_SIZE, 1));
  assert.deepEqual(mod.blobs, [{ size: MIN_ITEM_SIZE * 3 }]);
});

test("validation rejects malformed lists", () => {
  for (const bad of [0, -4, 0x64eed6, 0x1_0000_0000]) {
    assert.throws(() => buildModule(() => connectDList(bad, 0x64eedc, 112, 1)), EudslError);
  }
  for (const bad of [0, 4, 6, 9, 0x1_0000]) {
    assert.throws(() => buildModule(() => connectDList(0x64eed8, 0x64eedc, bad, 1)), EudslError);
  }
  assert.throws(() => buildModule(() => connectDList(0x63fe30, 0x63fe34, 8, 1, 1)), EudslError);
  buildModule(() => connectDList(0x63fe30, 0x63fe34, 20, 1, 1));

  assert.throws(() => buildModule(() => connectDList(0x64eed8, 0x64eedc, 112, 0)), EudslError);
  assert.throws(() => buildModule(() => connectDList(0x64eed8, 0x64eedc, 112, 1, 2)), EudslError);
});

test("connectDList needs the module scope", () => {
  // Db() is the scope gate: a bare call outside buildModule must fail rather
  // than allocate into a stale builder.
  assert.throws(() => connectDList(0x64eed8, 0x64eedc, 112, 1), EudslError);
});
