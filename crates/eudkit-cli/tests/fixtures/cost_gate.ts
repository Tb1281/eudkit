import {
  ActView,
  FixedArray,
  CSprite,
  CUnit,
  CondView,
  Deaths,
  Deque,
  Exactly,
  Func,
  MemCond,
  MemView,
  PageSlotEPD,
  Queue,
  SetDeaths,
  SetTo,
  Stack,
  FixedString,
  Trigger,
  TriggerStruct,
  UnitGroup,
  Var,
  after,
  arrayDword,
  bit,
  buildModule,
  byte,
  dword,
  every,
  memcpyEPD,
  memsetEPD,
  onFalling,
  onRising,
  once,
  until,
  when,
  whileLoop,
  withCP,
  word,
  type HirAct,
  type HirCond,
  type VarImpl,
} from "../../../../ts/index.ts";

const Point = TriggerStruct("Point", { x: dword(), y: word() });
const Hero = TriggerStruct("Hero", {
  hp: dword(),
  mp: word(),
  armor: byte(),
  flags: bit(4),
});

export const module = buildModule(() => {
  // TriggerStruct: whole-struct copy plus direct field access/mutation.
  const heroSrc = Hero({ hp: 1000, mp: 0x1234, armor: 0xab, flags: 0x0d });
  const heroDst = Hero();
  heroDst.copyFrom(heroSrc);
  heroDst.hp.iadd(5);
  heroDst.armor.set(0x42);
  const hpOut = Var(0);
  hpOut.set(heroDst.hp);

  const pointSrc = Point({ x: 7, y: 9 });
  const pointDst = Point();
  pointDst.set(pointSrc);
  pointDst.x.set(8);

  // MemView layouts: CUnit and CSprite fields resolve to raw EPDs.
  const unit = CUnit(1000);
  unit.hp.set(777);
  unit.posY.set(300);
  const sprite = CSprite(2000);
  sprite.index.set(11);

  // Memory primitives: eq / ne / read / MemCond.
  when(CUnit(1000).hp.eq(500), () => {
    SetDeaths(9000, SetTo, 1, 0);
  });
  when(CUnit(1000).hp.ne(500), () => {
    SetDeaths(9000, SetTo, 1, 0);
  });
  const readOut = Var(0);
  readOut.set(CUnit(1000).hp.read());
  when(MemCond(3000, "atleast", 5), () => {
    SetDeaths(9000, SetTo, 1, 0);
  });

  // Standalone CondView / ActView slots through the generic MemView constructor.
  const condSlot = MemView(CondView, 500);
  condSlot.amount.set(17);
  condSlot.comparison.set(Exactly);
  const actSlot = MemView(ActView, 600);
  actSlot.value.set(42);
  actSlot.acttype.set(45);

  // withCP: CurrentPlayer binding, relative hops, and cursor restore.
  withCP(7000, (cp) => {
    cp.at(2).set(100);
    const viewed = cp.view(CUnit);
    viewed.hp.iadd(5);
    viewed.shield.set(800);
  });

  // FixedArray: writable storage, const table, and dynamic PageSlotEPD index.
  const arr = FixedArray(arrayDword, 4);
  arr.set(0, 11);
  arr.set(3, 44);
  const index = Var(3);
  const dynOut = Var(0);
  dynOut.set(arr.get(index));
  const table = FixedArray.const([10, 20, 30, 40]);
  const constOut = Var(0);
  when(table.get(2).eq(30), () => {
    constOut.set(1);
  });
  const epdBase = Var(100);
  const computed = PageSlotEPD(epdBase, index, 3);
  const epdOut = Var(0);
  epdOut.set(computed);

  // Fixed-out-box containers.
  const queue = new Queue("dword", 2);
  queue.append(11);
  queue.append(22);
  const queueOut = Var(0);
  queueOut.set(queue.popleft() as VarImpl);

  const deque = new Deque("word", 2);
  deque.append(1);
  deque.appendleft(2);
  const dequeOut = Var(0);
  dequeOut.set(deque.pop() as VarImpl);

  const stack = new Stack(2);
  stack.push(11);
  stack.push(22);
  const stackOut = Var(0);
  stackOut.set(stack.pop());

  const group = new UnitGroup(2);
  group.add(1000);
  group.remove(1000);

  // STR-backed FixedString buffer with static writes and a display action.
  const text = new FixedString(16);
  text.write("cost-gate");
  text.append("!");
  text.display();

  // ADR 0011 dynamic fixed-width hex formatting: a two-variable HUD counter.
  const hud = new FixedString(32);
  const wave = Var(1);
  const hp = Var(100);
  hud.write("wave={:x} hp={:x}", wave, hp);

  // Block memory primitives.
  memsetEPD(3000, 0xaabbccdd, 2);
  memcpyEPD(4000, 3000, 2);

  // Variable-count forms keep the runtime loops.
  const n = Var(2);
  memsetEPD(3000, 0x11223344, n);
  memcpyEPD(6000, 3000, n);

  // Every temporal form.
  const tick = Var(0);
  const flag = Var(0);
  when(tick.eq(0), () => {
    flag.set(1);
  });
  once(flag.eq(1), () => {
    tick.iadd(1);
  });
  until(flag.eq(1), () => {
    tick.iadd(1);
  });
  onRising(flag.eq(1), () => {
    tick.iadd(1);
  });
  onFalling(flag.eq(0), () => {
    tick.iadd(1);
  });
  every(2, () => {
    tick.iadd(1);
  });
  after(2, () => {
    tick.iadd(1);
  });
  whileLoop(tick.lt(2), () => {
    tick.iadd(1);
  });
  const bump = Func(() => {
    tick.iadd(1);
  });
  bump();

  // One explicitly packed 16-condition / 64-action trigger.
  const packedConds: HirCond[] = [];
  const packedActs: HirAct[] = [];
  for (let i = 0; i < 16; i++) {
    packedConds.push(Deaths(2000 + i, Exactly, i, 0));
  }
  for (let i = 0; i < 64; i++) {
    packedActs.push(SetDeaths(5000 + i, SetTo, i + 1, 0));
  }
  const packed = new Trigger({ conds: packedConds, acts: packedActs });
  packed.cond(0).amount.set(500);
  packed.act(0).value.set(999);
});
