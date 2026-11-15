/**
 * Smoke map 5: `Stack`, `Queue`, `FixedArray`, `TriggerStruct`, `isqrt`, & live `MemFieldHandle`.
 *
 * Mirrors `hud-containers-math.py` 1:1.
 */

import {
  EPD,
  FixedArray,
  FixedString,
  GAME_TICK_ADDR,
  MemFieldHandle,
  Queue,
  Stack,
  UnitType,
  Var,
  arrayDword,
  buildModule,
  byte,
  defineMemView,
  defineTriggerStruct,
  dword,
  every,
  isqrt,
  once,
  unitsDat,
  when,
  word,
} from "../../../ts/index.ts";

const P1_MINERALS_ADDR = 0x57f0f0;

export const module = buildModule(() => {
  const u32Field = { offset: 0, size: 4 as const };
  const u32Schema = defineMemView("U32", 4, { val: u32Field });
  const tickField = new MemFieldHandle(u32Schema, u32Field, EPD(GAME_TICK_ADDR), "val");
  const minField = new MemFieldHandle(u32Schema, u32Field, EPD(P1_MINERALS_ADDR), "val");

  const HeroStruct = defineTriggerStruct({ hp: dword(), team: byte(), tier: word() });
  const hero = HeroStruct();

  const stack = new Stack(8);
  const queue = new Queue(arrayDword, 8);
  const arr = new FixedArray(arrayDword, 6);

  const stackScore = Var(0);
  const queueScore = Var(0);
  const structArrScore = Var(0);
  const sqrtSum = Var(0);
  const idx = Var(2);

  const qPhase = Var(0);
  const q1 = Var(0);
  const q2 = Var(0);
  const q3 = Var(0);

  when([qPhase.eq(4)], () => {
    queueScore.set(q1.mul(1).add(q2.mul(2)).add(q3.mul(3)));
    qPhase.set(5);
  });
  when([qPhase.eq(3)], () => {
    q3.set(queue.pop());
    qPhase.set(4);
  });
  when([qPhase.eq(2)], () => {
    q2.set(queue.pop());
    qPhase.set(3);
  });
  when([qPhase.eq(1)], () => {
    q1.set(queue.pop());
    qPhase.set(2);
  });

  once(() => {
    stack.push(10);
    stack.push(20);
    stack.push(30);
    const s1 = stack.pop();
    const s2 = stack.pop();
    const s3 = stack.pop();
    stackScore.set(s1.mul(1).add(s2.mul(2)).add(s3.mul(3)));

    hero.hp.set(500);
    hero.hp.iadd(25);
    hero.team.set(3);
    hero.tier.set(7);
    arr.set(idx, 42);
    const got = arr.get(idx);
    structArrScore.set(hero.hp.add(hero.team).add(hero.tier).add(got));

    const r1 = isqrt(Var(1000000));
    const r2 = isqrt(Var(65535));
    sqrtSum.set(r1.add(r2));

    queue.push(10);
    queue.push(20);
    queue.push(30);
    qPhase.set(1);
  });

  const marineHp = unitsDat[UnitType.TerranMarine].maxHp.read();

  const line1 = new FixedString(64);
  const line2 = new FixedString(64);
  const line3 = new FixedString(64);
  const verdict = new FixedString(64);

  every(6, () => {
    const tick = tickField.read();
    const minerals = minField.read();

    line1.clear();
    line1.write(
      "STACK={:\x1a>3d}/100 QUEUE={:\x1a>3d}/140 STRUCT+ARR={:\x1a>3d}/577",
      stackScore,
      queueScore,
      structArrScore,
    );
    line1.displayAt(1);

    line2.clear();
    line2.write("ISQRT={:\x1a>4d}/1255 MARINE_HP_HEX={:x}", sqrtSum, marineHp);
    line2.displayAt(2);

    line3.clear();
    line3.write("LIVE TICK={:\x1a>6d} P1_MINERALS={:\x1a>5d}", tick, minerals);
    line3.displayAt(3);

    verdict.clear();
    when(
      [
        stackScore.eq(100),
        queueScore.eq(140),
        structArrScore.eq(577),
        sqrtSum.eq(1255),
      ],
      () => verdict.write("STATUS: PASS"),
      () => verdict.write("STATUS: FAIL"),
    );
    verdict.displayAt(4);
  });
});
