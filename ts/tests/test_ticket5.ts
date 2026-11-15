import assert from "node:assert/strict";
import test from "node:test";

import { runSim } from "./helpers.ts";

import {
  AtLeast,
  AtMost,
  CPCursor,
  CSprite,
  CUnit,
  CondView,
  ActView,
  Deaths,
  DeathsX,
  EPD,
  EPD_BASE,
  EPD_CURRENT_PLAYER,
  EudslError,
  Exactly,
  MemCond,
  MemView,
  SetDeaths,
  SetDeathsX,
  SetTo,
  Trigger,
  Var,
  buildModule,
  defineMemView,
  whileLoop,
  withCP,
  with_cp,
} from "../index.ts";

test("CUnit and CSprite schemas load with correct layout sizes and field offsets", () => {
  const u = CUnit(1000);
  const s = CSprite(2000);

  // CUnit size: 336 bytes (84 dwords)
  assert.equal(u.schema.name, "CUnit");
  assert.equal(u.schema.size, 336);

  // CSprite size: 36 bytes (9 dwords)
  assert.equal(s.schema.name, "CSprite");
  assert.equal(s.schema.size, 36);

  // CUnit fields
  assert.equal(u.hp.byteOffset, 8);
  assert.equal(u.hp.size, 4);
  assert.equal(u.hp.epd, 1002);

  assert.equal(u.posX.byteOffset, 40);
  assert.equal(u.posX.size, 2);
  assert.equal(u.posX.shift, 0);
  assert.equal(u.posX.bitMask, 0x0000ffff);
  assert.equal(u.posX.epd, 1010);

  assert.equal(u.posY.byteOffset, 42);
  assert.equal(u.posY.size, 2);
  assert.equal(u.posY.shift, 16);
  assert.equal(u.posY.bitMask, 0xffff0000);
  assert.equal(u.posY.epd, 1010);

  // CSprite fields
  assert.equal(s.index.byteOffset, 16);
  assert.equal(s.index.size, 2);
  assert.equal(s.index.epd, 2004);
});

test("Direct property access handles aliases and camelCase/snake_case normalization", () => {
  const u = CUnit(1000);

  // hp aliases
  assert.equal(u.hp.byteOffset, 8);
  assert.equal(u.hitPoints.byteOffset, 8);
  assert.equal(u.hit_points.byteOffset, 8);

  // shield aliases
  assert.equal(u.shield.byteOffset, 96);
  assert.equal(u.shields.byteOffset, 96);

  // order aliases
  assert.equal(u.order.byteOffset, 77);
  assert.equal(u.orderID.byteOffset, 77);
  assert.equal(u.order_id.byteOffset, 77);

  // owner aliases
  assert.equal(u.owner.byteOffset, 76);
  assert.equal(u.playerID.byteOffset, 76);
  assert.equal(u.player_id.byteOffset, 76);

  // posX / posY normalization
  assert.equal(u.posX.byteOffset, 40);
  assert.equal(u.pos_x.byteOffset, 40);
  assert.equal(u.posY.byteOffset, 42);
  assert.equal(u.pos_y.byteOffset, 42);

  // Unknown property throws EUDSL-BOUNDS
  assert.throws(() => (u as any).nonExistentProperty, /EUDSL-BOUNDS/);
});

test("Constant EPD field mutations and 1C MemCond comparisons", () => {
  const base = 5000;
  const u = CUnit(base);

  // .act_set() emits 1 SetDeathsX action
  const actSet = u.hp.act_set(1500);
  assert.equal(actSet.acttype, 45);
  assert.equal(actSet.modifier, 7); // SetTo
  assert.equal(actSet.dest, base + 2);
  assert.equal(actSet.value, 1500);
  assert.equal(actSet.mask, 0xffffffff);
  assert.equal(u.hp.set(1500), undefined);

  // Sub-dword .act_set() masks and shifts
  const actPosY = u.posY.act_set(300);
  assert.equal(actPosY.dest, base + 10);
  assert.equal(actPosY.value, 300 << 16);
  assert.equal(actPosY.mask, 0xffff0000);

  // Comparisons emit 1C DeathsX conditions
  const condGe = u.hp.ge(500);
  assert.equal(condGe.condtype, 15);
  assert.equal(condGe.comparison, 0); // AtLeast
  assert.equal(condGe.player, base + 2);
  assert.equal(condGe.amount, 500);

  const condEqSub = u.posY.eq(300);
  assert.equal(condEqSub.player, base + 10);
  assert.equal(condEqSub.comparison, 10); // Exactly
  assert.equal(condEqSub.amount, 300 << 16);
  assert.equal(condEqSub.locid, 0xffff0000);

  // MemCond free function
  const mc1 = MemCond(base + 2, "Exactly", 1500);
  assert.equal(mc1.condtype, 15);
  assert.equal(mc1.comparison, 10);
  assert.equal(mc1.amount, 1500);

  const mc2 = MemCond(base + 10, AtLeast, 200 << 16, 0xffff0000);
  assert.equal(mc2.condtype, 15);
  assert.equal(mc2.comparison, 0);
  assert.equal(mc2.amount, 200 << 16);
  assert.equal(mc2.locid, 0xffff0000);
});

test("CondView and ActView subfields support .set(), .iadd(), .eq(), .ge() with masking", () => {
  const t0 = new Trigger({
    conds: [Deaths(10, Exactly, 100, 0)],
    acts: [SetDeaths(20, SetTo, 200, 0)],
  });

  // CondView slot 0
  const c0 = t0.cond(0);
  const actCondAmt = c0.amount.set(500);
  assert.equal(actCondAmt.acttype, 45);
  assert.equal(actCondAmt.value, 500);

  const condCheck = c0.amount.ge(250);
  assert.equal(condCheck.condtype, 15);
  assert.equal(condCheck.comparison, 0); // AtLeast
  assert.equal(condCheck.amount, 250);

  // Sub-dword cond field (unit is 2 bytes at offset 12)
  const actCondUnit = c0.unit.set(84);
  assert.equal(actCondUnit.mask, 0x0000ffff);
  assert.equal(actCondUnit.value, 84);

  // ActView slot 0
  const a0 = t0.act(0);
  const actVal = a0.value.set(999);
  assert.equal(actVal.acttype, 45);
  assert.equal(actVal.value, 999);

  // Sub-dword act field (modifier is 1 byte at offset 27, shift = 24)
  const actMod = a0.modifier.set(8); // Add = 8
  assert.equal(actMod.mask, 0xff000000);
  assert.equal(actMod.value, 8 << 24);
});

test("withCP binds CurrentPlayer (0x6509B0), absolute offset hops, and restores at scope exit", () => {
  const base = 7000;
  const mod = buildModule(() => {
    withCP(base, (cp) => {
      // 1. cp.at(2): writes 100 via Player 13 (CurrentPlayer) at offset +2
      cp.at(2).set(100);

      // 2. cp.view(CUnit): hp is at dword +2 (delta = 0, no extra hop!)
      const u = cp.view(CUnit);
      u.hp.iadd(50);

      // 3. shield is at dword +24 (delta = +22 hop)
      u.shield.set(800);

      // 4. posX is at dword +10 (delta = -14 hop, sub-dword low word)
      u.posX.set(42);
    });
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);
  const epds = report.external_epds ?? {};

  // EPD 7002 (hp): 100 + 50 = 150
  assert.equal(epds["7002"], 150);
  // EPD 7024 (shield): 800
  assert.equal(epds["7024"], 800);
  // EPD 7010 (posX): 42 in low word
  assert.equal(epds["7010"] & 0xffff, 42);

  // CurrentPlayer (EPD 203155) cleanly restored back to base (7000)
  assert.equal(epds["203155"], base);
});

test("with_cp hop inside a runtime loop is absolute", () => {
  let accId = -1;
  const mod = buildModule(() => {
    const n = Var(2);
    const acc = Var(0);
    SetDeaths(5001, SetTo, 0x1234, 0);
    SetDeaths(5002, SetTo, 0x5678, 0);
    withCP(5000, (cp) => {
      whileLoop([n.ge(1)], () => {
        // A non-zero offset hop replayed on every pass must re-anchor at
        // base + 1, not advance the register.
        acc.iadd(cp.at(1).read());
        n.isub(1);
      });
    });
    accId = acc.id;
  });

  const report = runSim(mod.toJson(), 1);
  // Both passes read EPD 5001: 0x1234 + 0x1234, never 5002.
  assert.equal(report.var_values[accId], 0x2468);
  // CurrentPlayer (EPD 203155) is re-anchored to the base at exit.
  assert.equal(report.external_epds["203155"], 5000);
});

test("withCP rejects a dynamic (non-int) base before seeding", () => {
  // The guard fires at withCP entry, before any act is queued, so a non-int
  // base never reaches the builder's act queue.
  assert.throws(
    () => withCP({} as any, () => {}),
    (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-TYPE");
      assert.match(err.message, /base/);
      return true;
    },
  );

  // A Var base is the realistic dynamic case. Constructing one needs a
  // builder, but the guard still fires before the seed act is queued.
  assert.throws(
    () =>
      buildModule(() => {
        withCP(Var(5000), () => {});
      }),
    (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-TYPE");
      assert.match(err.message, /base/);
      return true;
    },
  );

  // Defense in depth: the cursor itself refuses a non-int base.
  assert.throws(
    () => new CPCursor({} as any),
    (err: unknown) => {
      assert.ok(err instanceof EudslError);
      assert.equal(err.code, "EUDSL-TYPE");
      return true;
    },
  );
});

test("trig.fire() splices through trigger and resumes continuation in 1T executed", () => {
  const mod = buildModule(() => {
    const v = Var(10);
    const gate = Var(0);

    // Target routine with initially unmet condition (gate == 1)
    const routine = new Trigger({
      conds: [gate.eq(1)],
      acts: [v.act_add(50)],
      preserved: true,
    });

    // Mutate condition and action via CondView and ActView at runtime
    routine.cond(0).amount.set(2);
    routine.act(0).value.set(100);

    // Code before fire
    v.iadd(5);
    gate.set(2); // gate is now 2, matching mutated condition!

    // Fire routine
    routine.fire();

    // Code after fire
    v.iadd(1);
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);
  // 10 + 5 + 100 + 1 = 116
  assert.equal(report.var_values[0], 116);
});

test("MemFieldHandle .read() extracts memory value into a Var", () => {
  const base = 8000;
  const ids: Record<string, number> = {};
  const mod = buildModule(() => {
    const u = CUnit(base);
    u.hp.set(777);
    const readHp = u.hp.read();
    const result = Var(0);
    result.set(readHp);
    ids.readHp = readHp.id;
    ids.result = result.id;
  });

  const report = runSim(mod.toJson());
  assert.equal(report.self_targeted_invariant_holds, true);
  // The optimizer fuses a call result into its single-use destination (#97),
  // so the value lands directly in `result`; the temporary may stay untouched.
  assert.equal(report.var_values[ids.result], 777);
});
