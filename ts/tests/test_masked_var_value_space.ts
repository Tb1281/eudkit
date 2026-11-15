/**
 * Ticket #33: masked `Var` comparisons fold out-of-space constants and emit
 * the un-masked constant.
 *
 * A `Var` carries a value space `[0, bitmask]` (ADR 0017 §2). Its comparisons
 * used to (1) mask out-of-space constants into the space instead of folding
 * the comparison (`Var(0, undefined, 0xff).eq(0x100)` emitted `Exactly 0` and
 * fired) and (2) re-mask in-space constants before emitting them
 * (`Var(0, undefined, 0x0f00).ge(0x80)` emitted `AtLeast 0` instead of
 * `AtLeast 0x80`). Both are silent wrong answers about memory.
 *
 * The fixes are the two ADR 0017 §2 rules: `eq`/`ne` use the **subset** test
 * (`c & ~mask != 0` folds), the four orderings use the **numeric** test
 * (`c > mask` / `c >= mask` folds). In-space constants are emitted as
 * `c >>> 0`, never re-masked.
 *
 * The masked-`Var` constructor signature is `Var(init?, explicitId?,
 * explicitMask?, storage?)` — every masked literal here is
 * `Var(0, undefined, mask)`; `Var(0, mask)` would be read as an explicit id.
 *
 * Test 4 is a full-mask **no-move guard**: full-mask HIR can never fold, so it
 * passes on `61cf4fc` by design and pins that the rule did not leak into the
 * full-mask path. Tests 1, 2, 3, 5 and 6 are red on `61cf4fc`.
 *
 * Ticket #37 adds the **read side** of the same value space (ADR 0018 §6): a
 * comparison must read the left operand as `raw & bitmask`, not the raw lane.
 * `ne(middle)` was the one constant comparison that did not (its diff temp
 * accumulated the raw lane), so tests 7 and 8 are red on `28bbbdf`; test 9 is
 * a decision pin (variable RHS stays raw by decision) and test 10 is the
 * py<->ts HIR parity golden.
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  MemView,
  SetDeaths,
  SetTo,
  TriggerStruct,
  Var,
  bit,
  buildModule,
  byte,
  defineMemView,
  masked,
  when,
  word,
  type Var as VarType,
} from "../index.ts";
import { runSim, runStatic } from "./helpers.ts";

const OPS = ["eq", "ne", "ge", "le", "gt", "lt"] as const;
type Op = (typeof OPS)[number];

/** Plain unsigned 32-bit comparison, the semantics the value space encodes. */
function compare(a: number, op: Op, b: number): boolean {
  const x = a >>> 0;
  const y = b >>> 0;
  switch (op) {
    case "eq":
      return x === y;
    case "ne":
      return x !== y;
    case "ge":
      return x >= y;
    case "le":
      return x <= y;
    case "gt":
      return x > y;
    case "lt":
      return x < y;
  }
}

function mismatchReport(label: string, mismatches: string[]): void {
  assert.equal(
    mismatches.length,
    0,
    `${label}: ${mismatches.length} row(s) disagree with the value-space rule\n` +
      mismatches.slice(0, 12).join("\n"),
  );
}

test("#33 test 1: 1,248-row truth table over masked Var comparisons", () => {
  const MASKS_RAWS: Record<number, number[]> = {
    0xff: [0, 1, 0x7f, 0xff],
    0xffff: [0, 1, 0x80, 0xff, 0xffff],
    0xffffffff: [0, 1, 0xff, 0x100, 0x7fffffff, 0xffffffff],
    0x0f0f: [0, 0x000f, 0x0f00, 0x0f0f, 0x0102],
    0x0f00: [0, 0x0100, 0x0f00],
    0xe0: [0, 0x20, 0xe0],
  };
  const CONSTS = [0, 1, 0xff, 0x100, 0xffff, 0x10000, 0xffffffff, -1];

  const mismatches: string[] = [];
  let rows = 0;
  for (const [maskText, raws] of Object.entries(MASKS_RAWS)) {
    const mask = Number(maskText);
    for (const raw of raws) {
      const ids: number[] = [];
      const cases: Array<[Op, number]> = [];
      const mod = buildModule(() => {
        const v = Var(0, undefined, mask);
        v.set(raw);
        for (const op of OPS) {
          for (const c of CONSTS) {
            const hit = Var(0);
            ids.push(hit.id);
            cases.push([op, c]);
            when([(v as any)[op](c)], () => hit.iadd(1));
          }
        }
      });
      const report = runSim(mod.toJson(), 1);
      for (const [index, [op, c]] of cases.entries()) {
        rows++;
        const want = compare((raw & mask) >>> 0, op, c);
        const got = (report.var_values[ids[index]] ?? 0) !== 0;
        if (got !== want) {
          mismatches.push(
            `mask=0x${mask.toString(16)} raw=0x${raw.toString(16)} ` +
              `${op}(0x${(c >>> 0).toString(16)}) want=${want} got=${got}`,
          );
        }
      }
    }
  }
  assert.equal(rows, 1248, "truth table must stay at 1,248 rows");
  mismatchReport("masked Var truth table", mismatches);
});

test("#33 test 2: masked Var comparisons match the field rule (1,392 rows)", () => {
  const MASKS = [0xff, 0xffff, 0xffffffff, 0x0f0f, 0x0f00, 0xe0];
  const RAWS = [0, 1, 0x7f, 0xff, 0x100, 0x1234, 0x0f0f, 0xe0];
  const CONSTS = [0, 1, 0xff, 0x100, 0xffff, 0x10000, 0xffffffff, -1];

  const mismatches: string[] = [];
  let rows = 0;
  for (const mask of MASKS) {
    const schema = defineMemView("VarSpaceRef", 4, {
      f: { offset: 0, size: 4, mask },
    });
    for (const raw of RAWS) {
      if (((raw & ~mask) >>> 0) !== 0) continue; // raw must be in the space
      const aIds: number[] = [];
      const bIds: number[] = [];
      const cases: Array<[Op, number]> = [];
      const mod = buildModule(() => {
        SetDeaths(3000, SetTo, raw, 0);
        const v = Var(0, undefined, mask);
        v.set(raw);
        const f = MemView(schema, 3000).f;
        for (const op of OPS) {
          for (const c of CONSTS) {
            const a = Var(0);
            const b = Var(0);
            aIds.push(a.id);
            bIds.push(b.id);
            cases.push([op, c]);
            when([(v as any)[op](c)], () => a.set(1));
            when([(f as any)[op](c)], () => b.set(1));
          }
        }
      });
      const report = runSim(mod.toJson(), 1);
      for (const [index, [op, c]] of cases.entries()) {
        rows++;
        const varHit = (report.var_values[aIds[index]] ?? 0) !== 0;
        const fieldHit = (report.var_values[bIds[index]] ?? 0) !== 0;
        if (varHit !== fieldHit) {
          mismatches.push(
            `mask=0x${mask.toString(16)} raw=0x${raw.toString(16)} ` +
              `${op}(0x${(c >>> 0).toString(16)}): ` +
              `var=${varHit} field=${fieldHit}`,
          );
        }
      }
    }
  }
  assert.equal(rows, 1392, "cross-surface table must stay at 1,392 rows");
  mismatchReport("masked Var vs field rule", mismatches);
});

test("#33 test 3: shared masked-fixture HIR pin (py<->ts parity golden)", () => {
  const CASES: Array<[number, Op, number]> = [
    [0xff, "eq", 0x100], [0xff, "ne", 0x100], [0xff, "ge", 0x100],
    [0xff, "le", 0x100], [0xff, "gt", 0x100], [0xff, "lt", 0x100],
    [0xff, "eq", 0xff], [0xff, "ne", 0xff], [0xff, "ge", 0x7f],
    [0xff, "le", 0x7f], [0xffff, "eq", 0x10000], [0xffff, "ge", 0x10000],
    [0xffff, "lt", 0x10000], [0x0f0f, "eq", 0xff], [0x0f0f, "ne", 0xff],
    [0x0f0f, "ge", 0xff], [0x0f0f, "lt", 0xff], [0x0f0f, "gt", 0x80],
    [0x0f0f, "le", 0x80], [0xffffffff, "eq", 0x100],
    [0xffffffff, "gt", 0xffffffff],
  ];

  const mod = buildModule(() => {
    const v = Var(0, undefined, 0xff);
    v.set(0x7f);
    const mid = Var(0, undefined, 0xffff);
    mid.set(0x1234);
    const odd = Var(0, undefined, 0x0f0f);
    odd.set(0x0f00);
    const full = Var(0);
    full.set(5);
    const targets: Record<number, VarType> = {
      0xff: v,
      0xffff: mid,
      0x0f0f: odd,
      0xffffffff: full,
    };
    for (const [mask, op, rhs] of CASES) {
      when([(targets[mask] as any)[op](rhs)], () => full.iadd(0));
    }
  });

  const json = mod.toJson();
  const digest = createHash("sha256").update(json).digest("hex");
  // Shared with T2 (py/tests/test_masked_var_value_space.py Test 5): the same
  // fixture in both frontends must produce byte-identical HIR. Re-pinned for
  // HIR 3.1 -> 3.2 (ADR 0021): only the `version` literal changed.
  assert.equal(
    digest,
    "68ae017240617bab2ad03425b48cf1721378deac2da0757e893dc956b3036789",
  );
  assert.equal(json.length, 9443);
  assert.equal(JSON.parse(json).module.stmts.length, 22);
});

test("#33 test 4: full-mask HIR must not move (no-move guard)", () => {
  // Passes on `61cf4fc` by design: `~0xffffffff === 0` and `c > 0xffffffff`
  // is impossible, so the fold rule must leave every full-mask comparison
  // byte-identical.
  const mod = buildModule(() => {
    SetDeaths(3000, SetTo, 0x12345678, 0);
    const v = Var(0);
    const h = Var(0);
    for (const rhs of [0, 1, 0x7fffffff, 0xffffffff, 0x100]) {
      for (const op of OPS) {
        when([(v as any)[op](rhs)], () => h.set(1));
      }
    }
    const w = Var(0, undefined, 0xffffffff);
    for (const op of OPS) {
      when([(w as any)[op](5)], () => h.set(1));
    }
  });
  const json = mod.toJson();
  const digest = createHash("sha256").update(json).digest("hex");
  assert.equal(
    digest,
    "0a162b9f9ee1f504b2529b54b615e2c7a47820c2da9d627e32585dccefbd2d60",
  );
  assert.equal(JSON.parse(json).module.stmts.length, 45);
});

test("#33 test 5: public TriggerStruct fields obey the value space (288 rows)", () => {
  const Bits = TriggerStruct("Bits", {
    f: bit(3, 5),
    b1: byte(1),
    w1: word(1),
    m: masked(0x0f00),
  });
  const FIELDS: Array<{ name: "f" | "b1" | "w1" | "m"; mask: number; raws: number[] }> = [
    { name: "f", mask: 0xe0, raws: [0x00, 0x80, 0xe0] },
    { name: "b1", mask: 0x1fe, raws: [0x00, 0x80, 0x1fe] },
    { name: "w1", mask: 0x1fffe, raws: [0x00, 0x80, 0x1fffe] },
    { name: "m", mask: 0x0f00, raws: [0x00, 0x0100, 0x0f00] },
  ];
  const CONSTS = [0, 1, 0x80, 0x8000];

  const mismatches: string[] = [];
  let rows = 0;
  for (const { name, mask, raws } of FIELDS) {
    for (const raw of raws) {
      const ids: number[] = [];
      const cases: Array<[Op, number]> = [];
      const mod = buildModule(() => {
        const inst = Bits.instantiate();
        inst.set({ f: raw, b1: raw, w1: raw, m: raw });
        for (const op of OPS) {
          for (const c of CONSTS) {
            const hit = Var(0);
            ids.push(hit.id);
            cases.push([op, c]);
            when([(inst[name] as any)[op](c)], () => hit.iadd(1));
          }
        }
      });
      const report = runSim(mod.toJson(), 1);
      for (const [index, [op, c]] of cases.entries()) {
        rows++;
        const want = compare((raw & mask) >>> 0, op, c);
        const got = (report.var_values[ids[index]] ?? 0) !== 0;
        if (got !== want) {
          mismatches.push(
            `TriggerStruct.${name} mask=0x${mask.toString(16)} ` +
              `raw=0x${raw.toString(16)} ${op}(0x${(c >>> 0).toString(16)}) ` +
              `want=${want} got=${got}`,
          );
        }
      }
    }
  }
  assert.equal(rows, 288, "TriggerStruct table must stay at 288 rows");
  mismatchReport("TriggerStruct masked fields", mismatches);
});

test("#33 test 6: measured fold cost (out-of-space ne folds, in-space stays)", () => {
  function staticCost(rhs: number, mask = 0xff) {
    const mod = buildModule(() => {
      const v = Var(0, undefined, mask);
      const hit = Var(0);
      v.set(0);
      when([v.ne(rhs)], () => hit.iadd(1));
    });
    return runStatic(mod.toJson()).static_cost;
  }

  // 0x180 & ~0xff != 0: the comparison is trivially true, so the deferred
  // 4T flag form disappears (was 4T / 1C / 74A / 9,640 B on 61cf4fc).
  const folded = staticCost(0x180);
  assert.equal(folded.triggers, 2);
  assert.equal(folded.conditions, 1);
  assert.equal(folded.actions, 2);
  assert.equal(folded.total_bytes, 4824);

  // 0x80 is in-space and a middle value: the deferred form is unchanged
  // except for the #37 read-side fix, which replaces the raw `iadd(target)`
  // with a `target & bitmask` read (74 -> 73 acts on 28bbbdf; total bytes
  // unchanged because the saving is one `SetDeathsX` and the page sizing
  // does not move).
  const inSpace = staticCost(0x80);
  assert.equal(inSpace.triggers, 4);
  assert.equal(inSpace.conditions, 1);
  assert.equal(inSpace.actions, 73);
  assert.equal(inSpace.total_bytes, 9640);

  // #37 masked in-space row on a narrow mask: same one-act saving.
  const narrow = staticCost(0xa0, 0xe0);
  assert.equal(narrow.triggers, 4);
  assert.equal(narrow.conditions, 1);
  assert.equal(narrow.actions, 73);
  assert.equal(narrow.total_bytes, 9640);

  // #37 no-move guard: the full-mask path keeps the raw diff temp
  // (`raw & 0xffffffff` is the raw lane, so emitting a mask read would be
  // pure cost). 4T / 1C / 72A / 9,640 B on 28bbbdf and after.
  const full = staticCost(0x80, 0xffffffff);
  assert.equal(full.triggers, 4);
  assert.equal(full.conditions, 1);
  assert.equal(full.actions, 72);
  assert.equal(full.total_bytes, 9640);
});

test("#37 test 7: dirty-raw ne(middle) reads the value space", () => {
  // HEAD (28bbbdf): every row below returned 1, because the `ne(middle)` diff
  // temp accumulated the raw lane. The value-space read makes it 0. Normal
  // writes mask, so the dirty lane is seeded through a same-id alias, never
  // `v.set(raw)`.
  const ROWS: Array<[number, number, number]> = [
    [0xe0, 0xa5, 0xa0],
    [0x0f0f, 0x1f00, 0x0f00],
    [0xffff, 0x12345, 0x2345],
    [0xff00, 0xa512, 0xa500],
  ];
  for (const [mask, raw, c] of ROWS) {
    const hitIds: number[] = [];
    const ctrlIds: number[] = [];
    const mod = buildModule(() => {
      const v = Var(0, undefined, mask);
      const alias = Var(0, v.id);
      alias.set(raw);
      const hit = Var(0);
      const ctrl = Var(0);
      hitIds.push(hit.id);
      ctrlIds.push(ctrl.id);
      when([v.ne(c)], () => hit.iadd(1));
      when([v.ne(c + 1)], () => ctrl.iadd(1));
    });
    const report = runSim(mod.toJson(), 1);
    const label =
      `mask=0x${mask.toString(16)} raw=0x${raw.toString(16)} ` +
      `ne(0x${c.toString(16)})`;
    assert.equal(report.var_values[hitIds[0]] ?? 0, 0, `${label}: value-space read`);
    assert.equal(report.var_values[ctrlIds[0]] ?? 0, 1, `${label}: positive control`);
  }
});

test("#37 test 8: six-op dirty-raw truth table (1,008 rows)", () => {
  // Expected is `(raw & mask) op c` for a constant RHS. On 28bbbdf the only
  // wrong column is `ne(middle)` (the other five already emit
  // `DeathsX(..., bitMask)`), so this table is a regression guard for those
  // five and a fix test for `ne`.
  const MASKS_RAWS: Record<number, number[]> = {
    0xff: [0x1ff, 0x1234],
    0xe0: [0x0f, 0xa5, 0xef],
    0x0f0f: [0x1f00, 0xffff, 0x0102],
    0xffff: [0x12345, 0xffffffff],
    0xff00: [0xa512, 0xffff],
  };
  const CONSTS = [
    0, 1, 0x20, 0x34, 0x80, 0xa0, 0xff, 0x100,
    0x0f00, 0xa500, 0xffff, 0x10000, 0xffffffff, -1,
  ];

  const mismatches: string[] = [];
  let rows = 0;
  for (const [maskText, raws] of Object.entries(MASKS_RAWS)) {
    const mask = Number(maskText);
    for (const raw of raws) {
      const ids: number[] = [];
      const cases: Array<[Op, number]> = [];
      const mod = buildModule(() => {
        const v = Var(0, undefined, mask);
        const alias = Var(0, v.id);
        alias.set(raw);
        for (const op of OPS) {
          for (const c of CONSTS) {
            const hit = Var(0);
            ids.push(hit.id);
            cases.push([op, c]);
            when([(v as any)[op](c)], () => hit.iadd(1));
          }
        }
      });
      const report = runSim(mod.toJson(), 1);
      for (const [index, [op, c]] of cases.entries()) {
        rows++;
        const want = compare((raw & mask) >>> 0, op, c);
        const got = (report.var_values[ids[index]] ?? 0) !== 0;
        if (got !== want) {
          mismatches.push(
            `mask=0x${mask.toString(16)} raw=0x${raw.toString(16)} ` +
              `${op}(0x${(c >>> 0).toString(16)}) want=${want} got=${got}`,
          );
        }
      }
    }
  }
  assert.equal(rows, 1008, "dirty-raw table must stay at 1,008 rows");
  mismatchReport("dirty-raw six-op truth table", mismatches);
});

test("#37 test 9: variable RHS compares raw by decision (ADR 0018 §6)", () => {
  // A **variable** right operand is compared against the raw lane by
  // decision; fixing it would cost a `TriggerPage` per site and is deferred.
  // Mask 0xe0, dirty raw 0x0f, `w = Var(0)`: exactly `0x0f op 0`.
  // Passes on 28bbbdf too — this is a decision pin, not a fix test.
  const EXPECTED: Record<Op, number> = {
    eq: 0,
    ne: 1,
    ge: 1,
    le: 0,
    gt: 1,
    lt: 0,
  };
  const ids: number[] = [];
  const mod = buildModule(() => {
    const v = Var(0, undefined, 0xe0);
    const alias = Var(0, v.id);
    alias.set(0x0f);
    const w = Var(0);
    for (const op of OPS) {
      const hit = Var(0);
      ids.push(hit.id);
      when([(v as any)[op](w)], () => hit.iadd(1));
    }
  });
  const report = runSim(mod.toJson(), 1);
  for (const [index, op] of OPS.entries()) {
    assert.equal(
      (report.var_values[ids[index]] ?? 0) !== 0,
      EXPECTED[op] !== 0,
      `v.${op}(w) must compare the raw lane`,
    );
  }
});

test("#37 test 10: shared masked-ne HIR pin (py<->ts parity golden)", () => {
  const mod = buildModule(() => {
    const v = Var(0, undefined, 0xe0);
    const alias = Var(0, v.id);
    alias.set(0xa5);
    const h1 = Var(0);
    when([v.ne(0xa0)], () => h1.iadd(1));
    const h2 = Var(0);
    when([v.ne(0x20)], () => h2.iadd(1));
    const h3 = Var(0);
    when([v.ne(0)], () => h3.iadd(1));
    const w = Var(0, undefined, 0xff);
    const h4 = Var(0);
    when([w.ne(0x80)], () => h4.iadd(1));
  });
  const json = mod.toJson();
  const digest = createHash("sha256").update(json).digest("hex");
  // Shared with T2 (py/tests/test_masked_var_value_space.py): the same fixture
  // in both frontends must produce byte-identical HIR. HEAD (28bbbdf):
  // 3,292 chars / 64fd3231590a4b5a63a170119034c107bb1f8e440ddba244c5853d21230b8b48.
  // `h4` (full mask) is the no-move guard inside the same fixture.
  assert.equal(
    digest,
    "d4859e5c94230f46072ef091dcbeebf777248270e47b2afac4b51629c8a028e3",
  );
  assert.equal(json.length, 3977);
  assert.equal(JSON.parse(json).module.stmts.length, 11);
});

